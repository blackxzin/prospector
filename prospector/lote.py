"""Buscas em lote: nichos × cidades, cada combinação uma vez só."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from . import db
from .importer import ImportResult
from .mundo import BuscaMundo, MundoConfig


@dataclass(frozen=True)
class BatchConfig:
    nichos: tuple[str, ...]
    cidades: tuple[str, ...]
    max_por_execucao: int

    @classmethod
    def load(cls, path: Path) -> BatchConfig:
        if not path.exists():
            raise ValueError(f"{path.name} não encontrado")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        nichos = tuple(str(n).strip() for n in data.get("nichos") or () if str(n).strip())
        cidades = tuple(str(c).strip() for c in data.get("cidades") or () if str(c).strip())
        if not nichos or not cidades:
            raise ValueError(f"{path.name}: preencha 'nichos' e 'cidades'")
        return cls(nichos, cidades, max(1, int(data.get("max_por_execucao", 6))))

    def queries(self) -> list[str]:
        return [f"{n} em {c}" for c in self.cidades for n in self.nichos]

    def save(self, path: Path) -> None:
        """Regrava o buscas.yaml (usado pelo painel e pelo comando 'regiao')."""
        header = ("# Buscas em lote: `prospector lote` roda cada combinação nicho × cidade uma vez só\n"
                  "# (as já feitas ficam registradas no banco e são puladas). Edite aqui ou pelo painel (Buscas).\n"
                  "# Para trocar as cidades por todas num raio: prospector regiao \"Itaí\" --uf SP --raio 60 --salvar\n\n")
        body = yaml.safe_dump({"nichos": list(self.nichos), "cidades": list(self.cidades),
                               "max_por_execucao": self.max_por_execucao}, allow_unicode=True, sort_keys=False)
        path.write_text(header + body, encoding="utf-8")

    def with_lists(self, nichos: list[str], cidades: list[str], max_por_execucao: int | None = None) -> BatchConfig:
        clean = lambda xs: tuple(dict.fromkeys(x.strip() for x in xs if x and x.strip()))  # noqa: E731
        n, c = clean(nichos), clean(cidades)
        if not n or not c:
            raise ValueError("Informe pelo menos um nicho e uma cidade")
        return BatchConfig(n, c, max(1, max_por_execucao or self.max_por_execucao))


def done_queries(conn: sqlite3.Connection) -> set[str]:
    """Buscas já feitas: registradas no lote ou que já trouxeram leads (buscas manuais antigas)."""
    done = {r["consulta"] for r in conn.execute("SELECT consulta FROM buscas WHERE status = 'ok'")}
    return done | {r["busca"] for r in conn.execute("SELECT DISTINCT busca FROM leads WHERE busca IS NOT NULL")}


def pending(conn: sqlite3.Connection, cfg: BatchConfig) -> list[str]:
    done = done_queries(conn)
    return [q for q in cfg.queries() if q not in done]


def record(conn: sqlite3.Connection, query: str, result: ImportResult | None, error: str | None = None) -> None:
    with db.tx(conn):
        conn.execute(
            "INSERT INTO buscas (consulta, status, importados, validos, erro, feita_em) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(consulta) DO UPDATE SET status = excluded.status, importados = excluded.importados, "
            "validos = excluded.validos, erro = excluded.erro, feita_em = excluded.feita_em",
            (query, "erro" if error else "ok", result.novos if result else 0, result.validos if result else 0,
             error, db.iso(db.utcnow())),
        )


def _run(conn: sqlite3.Connection, todo: list[tuple[str, dict]], search: Callable[..., ImportResult],
         out: Callable[[str], None], should_stop: Callable[[], bool]) -> list[tuple[str, str]]:
    results = []
    for i, (query, kwargs) in enumerate(todo, 1):
        if should_stop():
            out("Pausado por você.")
            break
        out(f"[{i}/{len(todo)}] {query}")
        try:
            res = search(query, **kwargs)
        except Exception as exc:  # noqa: BLE001 - registra e segue para a próxima busca
            record(conn, query, None, str(exc)[:300])
            out(f"  erro: {exc}")
            results.append((query, f"erro: {exc}"))
            continue
        record(conn, query, res)
        out(f"  {res}")
        results.append((query, str(res)))
    return results


def run_batch(conn: sqlite3.Connection, cfg: BatchConfig, search: Callable[[str], ImportResult], *,
              limit: int | None = None, out: Callable[[str], None] = print,
              should_stop: Callable[[], bool] = lambda: False) -> list[tuple[str, str]]:
    """Roda as buscas pendentes (até `limit`). Erro numa busca não para as outras."""
    todo = pending(conn, cfg)[: limit or cfg.max_por_execucao]
    return _run(conn, [(q, {}) for q in todo], search, out, should_stop)


def pending_world(conn: sqlite3.Connection, cfg: MundoConfig, paises: set[str] | None = None) -> list[BuscaMundo]:
    done = done_queries(conn)
    return [b for b in cfg.buscas(paises) if b.consulta not in done]


def run_world(conn: sqlite3.Connection, cfg: MundoConfig, search: Callable[..., ImportResult], *,
              limit: int | None = None, paises: set[str] | None = None, out: Callable[[str], None] = print,
              should_stop: Callable[[], bool] = lambda: False) -> list[tuple[str, str]]:
    """Como run_batch, para o mundo.yaml: `search(consulta, origem=Origem)` recebe país, idioma e fuso."""
    todo = pending_world(conn, cfg, paises)[: limit or cfg.max_por_execucao]
    return _run(conn, [(b.consulta, {"origem": b.origem}) for b in todo], search, out, should_stop)
