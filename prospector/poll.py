"""Busca respostas no WA-AKG quando o webhook não está disponível.

O WA-AKG só entrega webhook para URL pública em HTTPS, então o prospector pergunta a ele: para cada lead
já contatado, lê o chat com aquele número e trata o que chegou depois do envio, exatamente como se
viesse pelo webhook (resposta, opt-out, interesse, fechamento). Só chats de leads são lidos.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Protocol

from . import db
from .phone import to_jid
from .replies import Inbound, ReplyOutcome
from .whatsapp import WhatsAppError

log = logging.getLogger(__name__)

POLL_DAYS = 21  # depois disso o lead deixa de ser consultado
POLL_STATUSES = (db.ENVIADO, db.RESPONDEU, db.INTERESSADO, db.SEM_RESPOSTA)

Handler = Callable[[Inbound, datetime], ReplyOutcome]


class ChatReader(Protocol):
    def chat_messages(self, jid: str) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class PollResult:
    chats: int = 0
    novas: int = 0
    erros: int = 0


def _parse_ts(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    return f"[{str(message.get('type') or 'mensagem').lower()}]"


def poll_replies(conn: sqlite3.Connection, client: ChatReader, handle: Handler, now: datetime) -> PollResult:
    """Consulta os chats dos leads contatados e entrega cada resposta nova a `handle(msg, recebida_em)`."""
    since = db.iso(now - timedelta(days=POLL_DAYS))
    leads = conn.execute(
        f"SELECT * FROM leads WHERE enviado_em IS NOT NULL AND enviado_em >= ? AND telefone IS NOT NULL "
        f"AND status IN ({','.join('?' * len(POLL_STATUSES))}) ORDER BY enviado_em DESC",
        (since, *POLL_STATUSES),
    ).fetchall()
    chats = novas = erros = 0
    for lead in leads:
        jid = lead["jid"] or to_jid(lead["telefone"])
        try:
            messages = client.chat_messages(jid)
        except WhatsAppError as exc:
            if exc.fatal:
                raise
            log.warning("chat de %s ilegível: %s", lead["nome"], exc)
            erros += 1
            continue
        chats += 1
        sent_at = datetime.fromisoformat(lead["enviado_em"])
        for message in messages:
            at = _parse_ts(message.get("timestamp"))
            key = message.get("keyId")
            if message.get("fromMe") or not key or at is None or at <= sent_at:
                continue
            inbound = Inbound(numbers=frozenset({lead["telefone"]}), jids=frozenset({jid}),
                              text=_text(message), push_name=str(message.get("pushName") or ""), msg_id=str(key),
                              kind=str(message.get("type") or "").upper())
            if not handle(inbound, at).duplicate:
                novas += 1
    return PollResult(chats, novas, erros)
