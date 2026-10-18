"""Fila do dia: gera, lista, aprova e rejeita."""

from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime, tzinfo

from . import db
from .messages import Templates
from .phone import variants
from .schedule import Rules, add_business_days

PENDENTE = "pendente"
APROVADO = "aprovado"
REJEITADO = "rejeitado"
ENVIADO = "enviado"
ERRO = "erro"
CANCELADO = "cancelado"


@dataclass(frozen=True)
class QueueResult:
    primeiras: int
    followups: int
    encerrados: int


def _today(now: datetime, tz: tzinfo) -> str:
    return now.astimezone(tz).date().isoformat()


def _insert(conn: sqlite3.Connection, lead_id: int, tipo: str, texto: str, dia: str) -> None:
    now = db.iso(db.utcnow())
    conn.execute(
        "INSERT INTO fila (lead_id, tipo, texto, dia, status, criado_em, atualizado_em) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (lead_id, tipo, texto, dia, PENDENTE, now, now),
    )


def open_items(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM fila WHERE status IN ('pendente', 'aprovado')").fetchone()[0]


def close_stale_followups(conn: sqlite3.Connection, now: datetime, rules: Rules, business_days: int) -> int:
    """follow_up sem resposta há N dias úteis -> sem_resposta (fim da linha)."""
    rows = conn.execute("SELECT id, followup_em FROM leads WHERE status = ?", (db.FOLLOW_UP,)).fetchall()
    closed = 0
    for row in rows:
        sent = db.parse_iso(row["followup_em"])
        if sent and add_business_days(sent, business_days, rules) <= now:
            db.update_lead(conn, row["id"], status=db.SEM_RESPOSTA)
            closed += 1
    return closed


def due_followups(conn: sqlite3.Connection, now: datetime, rules: Rules, business_days: int) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM leads WHERE status = ? AND respondeu_em IS NULL AND followup_em IS NULL "
        "AND id NOT IN (SELECT lead_id FROM fila WHERE tipo = 'followup')",
        (db.ENVIADO,),
    ).fetchall()
    out = []
    for row in rows:
        sent = db.parse_iso(row["enviado_em"])
        if sent and add_business_days(sent, business_days, rules) <= now:
            out.append(row)
    return out


def build_queue(
    conn: sqlite3.Connection,
    templates: Templates,
    rules: Rules,
    now: datetime,
    *,
    size: int,
    followup_days: int,
    busca: str | None = None,
    cidade: str | None = None,
    rng: random.Random | None = None,
) -> QueueResult:
    """Gera a fila do dia: follow-ups vencidos primeiro, depois leads novos até `size` itens abertos."""
    rng = rng or random.Random()
    dia = _today(now, rules.tz)
    with db.tx(conn):
        closed = close_stale_followups(conn, now, rules, followup_days)
        followups = 0
        for lead in due_followups(conn, now, rules, followup_days):
            if lead["telefone"] and db.is_blocked(conn, variants(lead["telefone"])):
                continue
            _insert(conn, lead["id"], db.FOLLOWUP, templates.followup(dict(lead), now, rules.tz, rng), dia)
            followups += 1

        slots = max(0, size - open_items(conn))
        sql = "SELECT * FROM leads WHERE status = ? AND telefone IS NOT NULL"
        params: list[object] = [db.NOVO]
        if busca:
            sql += " AND busca = ?"
            params.append(busca)
        if cidade:
            sql += " AND cidade = ?"
            params.append(cidade)
        sql += " ORDER BY COALESCE(avaliacoes, 0) DESC, id"
        firsts = 0
        for lead in conn.execute(sql, params).fetchall():
            if firsts >= slots:
                break
            numbers = variants(lead["telefone"])
            if db.is_blocked(conn, numbers):
                db.update_lead(conn, lead["id"], status=db.DESCARTADO, motivo="bloqueado")
                continue
            if db.first_message_already_sent(conn, numbers):
                db.update_lead(conn, lead["id"], status=db.DESCARTADO, motivo="já contatado (outro cadastro)")
                continue
            _insert(conn, lead["id"], db.PRIMEIRA, templates.first_message(dict(lead), now, rules.tz, rng), dia)
            db.update_lead(conn, lead["id"], status=db.NA_FILA)
            firsts += 1
    return QueueResult(primeiras=firsts, followups=followups, encerrados=closed)


def list_items(conn: sqlite3.Connection, statuses: tuple[str, ...] = (PENDENTE, APROVADO)) -> list[sqlite3.Row]:
    marks = ",".join("?" * len(statuses))
    return conn.execute(
        "SELECT f.*, l.nome, l.telefone, l.cidade, l.oferta, l.nota, l.avaliacoes, l.categoria, l.site "
        f"FROM fila f JOIN leads l ON l.id = f.lead_id WHERE f.status IN ({marks}) ORDER BY f.id",
        statuses,
    ).fetchall()


def approve(conn: sqlite3.Connection, item_ids: list[int], texto: str | None = None) -> int:
    """Aprova itens pendentes. `texto` (opcional) substitui a mensagem de um único item."""
    if texto is not None and len(item_ids) != 1:
        raise ValueError("Edição de texto só para um item por vez")
    now = db.iso(db.utcnow())
    n = 0
    with db.tx(conn):
        for item_id in item_ids:
            row = conn.execute("SELECT * FROM fila WHERE id = ? AND status = ?", (item_id, PENDENTE)).fetchone()
            if not row:
                continue
            if texto is not None:
                if not texto.strip():
                    raise ValueError("Mensagem vazia")
                conn.execute("UPDATE fila SET texto = ? WHERE id = ?", (texto.strip(), item_id))
            conn.execute("UPDATE fila SET status = ?, atualizado_em = ? WHERE id = ?", (APROVADO, now, item_id))
            if row["tipo"] == db.PRIMEIRA:
                db.update_lead(conn, row["lead_id"], aprovado_em=now)
            n += 1
    return n


def reject(conn: sqlite3.Connection, item_ids: list[int]) -> int:
    now = db.iso(db.utcnow())
    n = 0
    with db.tx(conn):
        for item_id in item_ids:
            row = conn.execute(
                "SELECT * FROM fila WHERE id = ? AND status IN (?, ?)", (item_id, PENDENTE, APROVADO)
            ).fetchone()
            if not row:
                continue
            conn.execute("UPDATE fila SET status = ?, atualizado_em = ? WHERE id = ?", (REJEITADO, now, item_id))
            if row["tipo"] == db.PRIMEIRA:
                db.update_lead(conn, row["lead_id"], status=db.REJEITADO, aprovado_em=None)
            else:
                db.update_lead(conn, row["lead_id"], status=db.SEM_RESPOSTA)
            n += 1
    return n


def pending_ids(conn: sqlite3.Connection) -> list[int]:
    return [r["id"] for r in conn.execute("SELECT id FROM fila WHERE status = ? ORDER BY id", (PENDENTE,))]
