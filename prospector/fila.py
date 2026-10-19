"""Fila do dia: gera, lista, aprova e rejeita."""

from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Callable, Mapping

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

COBRANCA = "cobranca"
MAX_CHECAGENS_POR_VAGA = 3  # teto de buscas de presença: 3x as vagas da fila
MAX_COBRANCAS = 2          # no máximo 2 lembretes por parcela
DIAS_ENTRE_COBRANCAS = 5


@dataclass(frozen=True)
class QueueResult:
    primeiras: int
    followups: int
    encerrados: int
    cobrancas: int = 0


def _today(now: datetime, tz: tzinfo) -> str:
    return now.astimezone(tz).date().isoformat()


def _insert(conn: sqlite3.Connection, lead_id: int, tipo: str, texto: str, dia: str, *,
            variante: str | None = None, pagamento_id: int | None = None) -> None:
    now = db.iso(db.utcnow())
    conn.execute(
        "INSERT INTO fila (lead_id, tipo, texto, dia, status, variante, pagamento_id, criado_em, atualizado_em) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (lead_id, tipo, texto, dia, PENDENTE, variante, pagamento_id, now, now),
    )


def ab_stats(conn: sqlite3.Connection) -> dict[str, tuple[int, int]]:
    """{variante: (enviados, respostas positivas)}. "nao_quer" conta como resposta negativa."""
    rows = conn.execute(
        "SELECT variante, COUNT(*) AS enviados, "
        "SUM(respondeu_em IS NOT NULL AND status != 'nao_quer') AS positivos "
        "FROM leads WHERE enviado_em IS NOT NULL AND variante IS NOT NULL GROUP BY variante"
    ).fetchall()
    return {r["variante"]: (r["enviados"], r["positivos"] or 0) for r in rows}


def due_charges(conn: sqlite3.Connection, today: date) -> list[sqlite3.Row]:
    """Parcelas vencidas de clientes com WhatsApp, ainda dentro do limite de lembretes."""
    limite = (today - timedelta(days=DIAS_ENTRE_COBRANCAS)).isoformat()
    return conn.execute(
        "SELECT g.*, l.id AS lead_id FROM pagamentos g JOIN projetos p ON p.id = g.projeto_id "
        "JOIN leads l ON l.id = p.lead_id "
        "WHERE g.pago_em IS NULL AND g.vencimento < ? AND p.status != 'cancelado' AND l.telefone IS NOT NULL "
        "AND g.cobrancas < ? AND (g.ultima_cobranca IS NULL OR g.ultima_cobranca <= ?) "
        "AND g.id NOT IN (SELECT pagamento_id FROM fila WHERE pagamento_id IS NOT NULL "
        "AND status IN ('pendente', 'aprovado'))",
        (today.isoformat(), MAX_COBRANCAS, limite),
    ).fetchall()


def open_items(conn: sqlite3.Connection, tipos: tuple[str, ...] | None = None) -> int:
    sql = "SELECT COUNT(*) FROM fila WHERE status IN ('pendente', 'aprovado')"
    params: tuple = ()
    if tipos:
        sql += f" AND tipo IN ({','.join('?' * len(tipos))})"
        params = tipos
    return conn.execute(sql, params).fetchone()[0]


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


def _new_leads_query(busca: str | None, cidade: str | None) -> tuple[str, list[object]]:
    sql = "SELECT * FROM leads WHERE status = ? AND telefone IS NOT NULL"
    params: list[object] = [db.NOVO]
    if busca:
        sql += " AND busca = ?"
        params.append(busca)
    if cidade:
        sql += " AND cidade = ?"
        params.append(cidade)
    return sql + " ORDER BY COALESCE(avaliacoes, 0) DESC, id", params


Checker = Callable[[sqlite3.Connection, Mapping], bool]


def check_presence(conn: sqlite3.Connection, checker: Checker, slots: int,
                   busca: str | None = None, cidade: str | None = None) -> int:
    """Checa a presença online dos próximos leads novos até sobrarem `slots` válidos. Fora da transação
    da fila: a busca demora e não pode segurar o banco. Devolve quantos foram buscados."""
    sql, params = _new_leads_query(busca, cidade)
    kept = searched = 0
    for lead in conn.execute(sql, params).fetchall():
        if kept >= slots or searched >= slots * MAX_CHECAGENS_POR_VAGA:
            break
        if not lead["presenca_checada_em"]:
            searched += 1
            checker(conn, lead)
            if db.get_lead(conn, lead["id"])["status"] != db.NOVO:
                continue
        kept += 1
    return searched


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
    charge_text: Callable[[sqlite3.Row, sqlite3.Row], str] | None = None,
    checar_presenca: Checker | None = None,
) -> QueueResult:
    """Gera a fila do dia: follow-ups vencidos primeiro, depois leads novos até `size` itens abertos.

    Com `checar_presenca`, os leads novos ainda não checados passam antes pela busca de presença online
    (presenca.py): quem já usa cardápio online com robô é descartado e não entra na fila."""
    rng = rng or random.Random()
    dia = _today(now, rules.tz)
    if checar_presenca:
        check_presence(conn, checar_presenca, max(0, size - open_items(conn, (db.PRIMEIRA, db.FOLLOWUP))),
                       busca, cidade)
    with db.tx(conn):
        closed = close_stale_followups(conn, now, rules, followup_days)
        followups = 0
        for lead in due_followups(conn, now, rules, followup_days):
            if lead["telefone"] and db.is_blocked(conn, variants(lead["telefone"])):
                continue
            _insert(conn, lead["id"], db.FOLLOWUP, templates.followup(dict(lead), now, rules.tz, rng), dia)
            followups += 1

        charges = 0
        if charge_text:
            for payment in due_charges(conn, now.astimezone(rules.tz).date()):
                lead = db.get_lead(conn, payment["lead_id"])
                _insert(conn, lead["id"], COBRANCA, charge_text(lead, payment), dia, pagamento_id=payment["id"])
                charges += 1

        slots = max(0, size - open_items(conn, (db.PRIMEIRA, db.FOLLOWUP)))
        stats = ab_stats(conn)
        sql, params = _new_leads_query(busca, cidade)
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
            texto, variante = templates.first_message_ab(dict(lead), now, rules.tz, rng, stats)
            _insert(conn, lead["id"], db.PRIMEIRA, texto, dia, variante=variante)
            db.update_lead(conn, lead["id"], status=db.NA_FILA)
            firsts += 1
    return QueueResult(primeiras=firsts, followups=followups, encerrados=closed, cobrancas=charges)


def list_items(conn: sqlite3.Connection, statuses: tuple[str, ...] = (PENDENTE, APROVADO)) -> list[sqlite3.Row]:
    marks = ",".join("?" * len(statuses))
    return conn.execute(
        "SELECT f.*, l.nome, l.telefone, l.cidade, l.oferta, l.nota, l.avaliacoes, l.categoria, l.site, l.motivo "
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
            elif row["tipo"] == db.FOLLOWUP:
                db.update_lead(conn, row["lead_id"], status=db.SEM_RESPOSTA)
            # cobrança rejeitada: o lead fica como está
            n += 1
    return n


def pending_ids(conn: sqlite3.Connection) -> list[int]:
    return [r["id"] for r in conn.execute("SELECT id FROM fila WHERE status = ? ORDER BY id", (PENDENTE,))]
