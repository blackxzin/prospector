"""Nichos para testar um por dia: catálogo (nichos.yaml), reconhecimento do nicho de cada lead e agenda."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import yaml

from .classify import fold

BUSINESS_DAYS = frozenset({0, 1, 2, 3, 4})


@dataclass(frozen=True)
class Nicho:
    id: str
    nome: str
    buscas: tuple[str, ...]
    categorias: tuple[str, ...]
    dor: str


@dataclass(frozen=True)
class Nichos:
    itens: dict[str, Nicho]
    ordem: tuple[str, ...]
    inicio: date

    @classmethod
    def load(cls, path: Path) -> Nichos:
        if not path.exists():
            raise ValueError(f"{path.name} não encontrado")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        itens: dict[str, Nicho] = {}
        for nid, cfg in (data.get("nichos") or {}).items():
            cfg = cfg or {}
            itens[str(nid)] = Nicho(
                id=str(nid), nome=str(cfg.get("nome") or nid),
                buscas=tuple(str(b).strip() for b in cfg.get("buscas") or () if str(b).strip()),
                categorias=tuple(fold(str(c)) for c in cfg.get("categorias") or () if str(c).strip()),
                dor=str(cfg.get("dor") or "").strip(),
            )
        if not itens:
            raise ValueError(f"{path.name}: preencha 'nichos'")
        ordem = tuple(str(n) for n in data.get("ordem") or ())
        unknown = [n for n in ordem if n not in itens]
        if unknown:
            raise ValueError(f"{path.name}: 'ordem' cita nicho que não existe: {unknown}")
        for n in ordem:
            if not itens[n].buscas:
                raise ValueError(f"{path.name}: o nicho {n!r} está na 'ordem' mas não tem 'buscas'")
        inicio = data.get("inicio")
        if not isinstance(inicio, date):
            raise ValueError(f"{path.name}: 'inicio' precisa ser uma data (AAAA-MM-DD)")
        return cls(itens, ordem, inicio)

    def classify(self, busca: str | None, categoria: str | None) -> str | None:
        """Nicho de um lead pela categoria do Maps. Só sem categoria usa os termos da busca: a busca de um nicho
        traz muito negócio de outro (lava-rápido na busca de "clínica de estética") e a mensagem do nicho não serve."""
        cat, termo = fold(categoria or ""), fold(busca or "")
        if cat:
            for nicho in self.itens.values():
                if any(word in cat for word in nicho.categorias):
                    return nicho.id
            return None
        for nicho in self.itens.values():
            if termo and any(fold(b) in termo for b in nicho.buscas):
                return nicho.id
        return None

    def do_dia(self, day: date) -> Nicho | None:
        """Nicho do dia: um por dia útil, na ordem, a partir de `inicio` (antes dele, o primeiro). Fim de semana: nenhum."""
        if not self.ordem or day.weekday() not in BUSINESS_DAYS:
            return None
        count, cursor = 0, self.inicio
        while cursor < day:
            cursor += timedelta(days=1)
            count += cursor.weekday() in BUSINESS_DAYS
        return self.itens[self.ordem[count % len(self.ordem)]]

    def queries(self, nicho_id: str, cidades: tuple[str, ...]) -> list[str]:
        nicho = self.itens[nicho_id]
        return [f"{termo} em {cidade}" for cidade in cidades for termo in nicho.buscas]

    def dor_de(self, nicho_id: str | None) -> str:
        return self.itens[nicho_id].dor if nicho_id in self.itens else ""


def reclassificar(conn, nichos: Nichos) -> tuple[int, int]:
    """Recalcula o nicho dos leads do Brasil (regras atuais do nichos.yaml) e 'resposta_humana' pelo texto das
    respostas. Devolve (leads cujo nicho mudou, leads com resposta de gente de verdade)."""
    from . import db
    from .replies import is_human_reply

    tagged = 0
    with db.tx(conn):
        for lead in conn.execute("SELECT id, busca, categoria, nicho FROM leads WHERE pais = 'BR'").fetchall():
            nid = nichos.classify(lead["busca"], lead["categoria"])
            if nid != lead["nicho"]:
                db.update_lead(conn, lead["id"], nicho=nid)
                tagged += 1
        for lead in conn.execute("SELECT id, resposta_humana FROM leads WHERE respondeu_em IS NOT NULL").fetchall():
            texts = [r["texto"] for r in conn.execute("SELECT texto FROM respostas WHERE lead_id = ?", (lead["id"],))]
            human = int(any(is_human_reply(t) for t in texts))
            if human != lead["resposta_humana"]:
                db.update_lead(conn, lead["id"], resposta_humana=human)
    humans = conn.execute("SELECT COUNT(*) FROM leads WHERE resposta_humana = 1").fetchone()[0]
    return tagged, humans
