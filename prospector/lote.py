"""Buscas em lote: nichos × cidades, cada combinação uma vez só."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from . import db
from .importer import ImportResult


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


def run_batch(conn: sqlite3.Connection, cfg: BatchConfig, search: Callable[[str], ImportResult], *,
              limit: int | None = None, out: Callable[[str], None] = print) -> list[tuple[str, str]]:
    """Roda as buscas pendentes (até `limit`). Erro numa busca não para as outras."""
    todo = pending(conn, cfg)[: limit or cfg.max_por_execucao]
    results = []
    for i, query in enumerate(todo, 1):
        out(f"[{i}/{len(todo)}] {query}")
        try:
            res = search(query)
        except Exception as exc:  # noqa: BLE001 - registra e segue para a próxima busca
            record(conn, query, None, str(exc)[:300])
            out(f"  erro: {exc}")
            results.append((query, f"erro: {exc}"))
            continue
        record(conn, query, res)
        out(f"  {res}")
        results.append((query, str(res)))
    return results
