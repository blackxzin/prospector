"""Follow-up de conversas em andamento: lembretes para você retomar quem respondeu ou demonstrou interesse.

Diferente do follow-up automático (um lembrete único para quem nunca respondeu), aqui a conversa é sua: o sistema só
marca a data, avisa e sugere o texto. Nada é enviado sozinho para quem já está conversando."""

from __future__ import annotations

import random
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Mapping

from . import db
from .pessoa import is_individual, singularize
from .schedule import Rules, add_business_days

ABERTOS = (db.INTERESSADO, db.RESPONDEU)
DIAS_UTEIS_PADRAO = {db.INTERESSADO: 2}   # depois da última resposta; "respondeu" só tem lembrete se você pedir
JANELA_DIAS = 14   # só agenda sozinho para quem respondeu nos últimos N dias (não inunda com conversas antigas)


_RECUSA = re.compile(r"n[aã]o (?:tenho|temos|estou|estamos) (?:interesse|investindo|precisando)|sem interesse|"
                     r"n[aã]o precisa|j[aá] (?:tenho|temos|possuo|possu[ií]mos)|no momento n[aã]o|n[aã]o (?:é|e) o momento|"
                     r"agrade[cç]o,? mas|n[aã]o obrigad[oa]|n[aã]o quero", re.IGNORECASE)


def is_soft_decline(text: str | None) -> bool:
    """'No momento não temos interesse. Sucesso!': recusa educada. Não vira lembrete (insistir só queima o lead)."""
    return bool(_RECUSA.search(text or ""))


def schedule(conn: sqlite3.Connection, lead_id: int, when: datetime, nota: str = "") -> None:
    with db.tx(conn):
        db.update_lead(conn, lead_id, proximo_contato_em=db.iso(when), proximo_contato_nota=nota or None,
                       lembrete_encerrado=0)


def finish(conn: sqlite3.Connection, lead_id: int) -> None:
    """Para de lembrar desse lead (conversa resolvida ou sem futuro)."""
    with db.tx(conn):
        db.update_lead(conn, lead_id, proximo_contato_em=None, lembrete_encerrado=1)


def done(conn: sqlite3.Connection, lead_id: int, now: datetime, rules: Rules, business_days: int = 3) -> None:
    """Você retomou a conversa: próximo lembrete em alguns dias úteis."""
    schedule(conn, lead_id, add_business_days(now, business_days, rules))


def snooze(conn: sqlite3.Connection, lead_id: int, now: datetime, rules: Rules, business_days: int) -> None:
    schedule(conn, lead_id, add_business_days(now, business_days, rules),
             (conn.execute("SELECT proximo_contato_nota FROM leads WHERE id = ?", (lead_id,)).fetchone() or [None])[0] or "")


def ensure_defaults(conn: sqlite3.Connection, now: datetime, rules: Rules) -> int:
    """Agenda o primeiro lembrete de quem demonstrou interesse e ainda não tem data. Devolve quantos agendou.

    Quem só respondeu (muitas vezes robô ou recusa educada) não ganha lembrete sozinho: use `prospector lembretes agendar`."""
    since = db.iso(now - timedelta(days=JANELA_DIAS))
    rows = conn.execute(
        "SELECT id, respondeu_em FROM leads WHERE status = ? AND proximo_contato_em IS NULL AND lembrete_encerrado = 0 "
        "AND respondeu_em IS NOT NULL AND respondeu_em >= ?", (db.INTERESSADO, since)).fetchall()
    made = 0
    for row in rows:
        base = db.parse_iso(row["respondeu_em"])
        if base is None:
            continue
        schedule(conn, row["id"], add_business_days(base, DIAS_UTEIS_PADRAO[db.INTERESSADO], rules), "follow-up automático")
        made += 1
    return made


def _open_query(extra: str) -> str:
    return (f"SELECT * FROM leads WHERE status IN ({','.join('?' * len(ABERTOS))}) AND lembrete_encerrado = 0 "
            f"AND proximo_contato_em IS NOT NULL {extra} ORDER BY proximo_contato_em")


def due(conn: sqlite3.Connection, now: datetime) -> list[sqlite3.Row]:
    return conn.execute(_open_query("AND proximo_contato_em <= ?"), (*ABERTOS, db.iso(now))).fetchall()


def upcoming(conn: sqlite3.Connection, now: datetime) -> list[sqlite3.Row]:
    return conn.execute(_open_query("AND proximo_contato_em > ?"), (*ABERTOS, db.iso(now))).fetchall()


def suggestion(data: Mapping[str, Any], lead: Mapping[str, Any], rng: random.Random | None = None) -> str:
    """Texto sugerido para retomar a conversa (você revisa e manda)."""
    group = (data.get("acompanhamento") or {}).get(lead["status"]) or []
    if not group:
        return ""
    contact = (lead["contato_nome"] if "contato_nome" in lead.keys() else "") or ""
    text = (rng or random.Random()).choice(group).format(chamada=f", {contact}" if contact else "")
    return singularize(text) if is_individual(lead["nome"]) else text


def notify_due(conn: sqlite3.Connection, now: datetime, notify_fn: Any, rules: Rules) -> int:
    """Um aviso por dia com quantos follow-ups vencem. Devolve a quantidade avisada (0 se já avisou hoje)."""
    ensure_defaults(conn, now, rules)
    today = now.astimezone(rules.tz).strftime("%Y-%m-%d")
    rows = due(conn, now)
    if not rows or db.get_state(conn, "lembretes_avisado") == today:
        return 0
    db.set_state(conn, "lembretes_avisado", today)
    names = ", ".join(re.sub(r"\s+", " ", r["nome"])[:28] for r in rows[:4])
    notify_fn(f"{len(rows)} follow-up(s) para hoje", names + ("…" if len(rows) > 4 else ""), False)
    return len(rows)
