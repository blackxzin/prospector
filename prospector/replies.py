"""Respostas dos leads (webhook do WA-AKG): opt-out, notificação e resposta fora do horário."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from . import db
from .classify import fold
from .phone import phone_from_jid, variants

log = logging.getLogger("prospector")

# Opt-out: qualquer "não" isolado conta (regra pedida: na dúvida, não insistir).
_OPT_OUT = re.compile(
    r"\bnao\b|\bnaum\b|\bnn\b|\bpare\b|\bparar\b|\bpara de\b|\bsair\b|\bsai\b|\bremov|\bdescadastr|"
    r"\bsem interesse\b|\bdesinteress|\bnao quero\b|\bnao preciso\b|\bme tira\b|\bbloque|\bspam\b|"
    r"\bstop\b|\bdenunc|\bchato\b|\bincomod|\bnao mand|\bobrigad[oa],? mas\b",
)
_INTEREST = re.compile(
    r"\bsim\b|\bpode\b|\bquero\b|\bmanda\b|\bmande\b|\btenho interesse\b|\binteress|\bclaro\b|"
    r"\bcomo funciona\b|\bquanto\b|\bvalor\b|\bpreco\b|\bbora\b|\bok\b|\bpode ser\b|\bgostaria\b",
)


def is_opt_out(text: str | None) -> bool:
    return bool(_OPT_OUT.search(fold(text or "")))


def is_interested(text: str | None) -> bool:
    return not is_opt_out(text) and bool(_INTEREST.search(fold(text or "")))


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """HMAC-SHA256 do corpo cru, no formato 'sha256=<hex>' (padrão do WA-AKG)."""
    if not secret:
        return True
    if not header:
        return False
    expected = header.strip().removeprefix("sha256=")
    actual = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, actual)


@dataclass(frozen=True)
class Inbound:
    numbers: frozenset[str]
    jids: frozenset[str]
    text: str
    push_name: str


def _content(data: dict[str, Any]) -> str:
    content = data.get("content") or data.get("caption") or data.get("text")
    if isinstance(content, str) and content.strip():
        return content.strip()
    kind = str(data.get("type") or "mensagem").lower()
    return f"[{kind}]"


def parse_inbound(payload: dict[str, Any]) -> Inbound | None:
    """Extrai a mensagem recebida em chat privado. None para eventos irrelevantes."""
    if not isinstance(payload, dict) or payload.get("event") != "message.received":
        return None
    data = payload.get("data") or {}
    key = data.get("key") or {}
    if key.get("fromMe") or data.get("fromMe") or data.get("isGroup"):
        return None
    candidates = {
        data.get("from"), data.get("sender"), key.get("remoteJid"),
        key.get("senderPn"), key.get("remoteJidAlt"), data.get("senderPn"),
    }
    jids = frozenset(j for j in candidates if isinstance(j, str) and j)
    if any(j.endswith("@g.us") for j in jids):
        return None
    numbers = frozenset(n for n in (phone_from_jid(j) for j in jids) if n)
    if not numbers and not jids:
        return None
    return Inbound(numbers=numbers, jids=jids, text=_content(data), push_name=str(data.get("pushName") or ""))


def find_lead(conn: sqlite3.Connection, msg: Inbound) -> sqlite3.Row | None:
    """Só leads já contatados (com envio real)."""
    numbers: set[str] = set()
    for n in msg.numbers:
        numbers |= variants(n)
    clauses, params = [], []
    if numbers:
        clauses.append(f"telefone IN ({','.join('?' * len(numbers))})")
        params.extend(numbers)
    if msg.jids:
        clauses.append(f"jid IN ({','.join('?' * len(msg.jids))})")
        params.extend(msg.jids)
    if not clauses:
        return None
    return conn.execute(
        f"SELECT * FROM leads WHERE ({' OR '.join(clauses)}) AND enviado_em IS NOT NULL "
        "ORDER BY enviado_em DESC LIMIT 1",
        params,
    ).fetchone()


@dataclass(frozen=True)
class ReplyOutcome:
    lead_id: int | None
    status: str | None
    auto_reply: bool = False


def handle_inbound(
    conn: sqlite3.Connection,
    msg: Inbound,
    *,
    now: datetime,
    in_business_hours: bool,
    notify_fn: Callable[[str, str, bool], None],
) -> ReplyOutcome:
    """Grava a resposta, aplica opt-out e decide se cabe resposta automática fora do horário."""
    lead = find_lead(conn, msg)
    if not lead:
        return ReplyOutcome(None, None)
    with db.tx(conn):
        conn.execute("INSERT INTO respostas (lead_id, telefone, texto, recebido_em) VALUES (?, ?, ?, ?)",
                     (lead["id"], lead["telefone"], msg.text, db.iso(now)))
        fields: dict[str, object] = {"resposta": msg.text[:2000]}
        if not lead["respondeu_em"]:
            fields["respondeu_em"] = db.iso(now)
        if lead["status"] == db.NAO_QUER or is_opt_out(msg.text):
            new_status = db.NAO_QUER
            for number in variants(lead["telefone"]) | set(msg.numbers):
                db.block(conn, number, f"opt-out: {msg.text[:120]}")
            # cancela qualquer coisa pendente para esse lead
            conn.execute("UPDATE fila SET status = 'cancelado', atualizado_em = ? "
                         "WHERE lead_id = ? AND status IN ('pendente', 'aprovado')", (db.iso(now), lead["id"]))
        elif lead["status"] == db.INTERESSADO or is_interested(msg.text):
            new_status = db.INTERESSADO
        else:
            new_status = db.RESPONDEU
        if new_status != db.NAO_QUER:
            conn.execute("UPDATE fila SET status = 'cancelado', atualizado_em = ? "
                         "WHERE lead_id = ? AND tipo = 'followup' AND status IN ('pendente', 'aprovado')",
                         (db.iso(now), lead["id"]))
        fields["status"] = new_status
        auto = (new_status != db.NAO_QUER and not in_business_hours and not lead["auto_resposta_enviada"])
        if auto:
            fields["auto_resposta_enviada"] = 1  # reserva já: no máximo uma vez por contato
        db.update_lead(conn, lead["id"], **fields)

    label = {db.NAO_QUER: "pediu para parar (bloqueado)", db.INTERESSADO: "parece interessado"}.get(new_status, "respondeu")
    notify_fn(f"{lead['nome']} {label}", f"{msg.text[:300]}", new_status == db.INTERESSADO)
    return ReplyOutcome(lead["id"], new_status, auto)
