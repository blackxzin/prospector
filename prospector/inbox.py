"""Lê a caixa do Gmail (IMAP) e trata as respostas dos e-mails enviados, como o poll faz com o WhatsApp.

Cada resposta de lead entra no mesmo fluxo do WhatsApp (`replies.handle_inbound`): robô não conta, "parar" bloqueia,
interesse e fechamento viram alerta, ameaça/denúncia vira alerta urgente sem resposta automática. E-mail que volta
(endereço inexistente) descarta o lead. A caixa é aberta só para leitura e nada é apagado nem marcado como lido."""

from __future__ import annotations

import email
import email.policy
import html
import imaplib
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parseaddr, parsedate_to_datetime
from typing import Callable

from . import canais, db
from .replies import Inbound, ReplyOutcome, handle_inbound

log = logging.getLogger(__name__)

LOOKBACK_DAYS = 14
MAX_MAILS = 200
_BOUNCE_SENDER = re.compile(r"mailer-daemon|postmaster", re.IGNORECASE)
_BOUNCE_SUBJECT = re.compile(r"delivery status|undeliver|failure notice|returned mail|falha na entrega|não entregue", re.IGNORECASE)
_QUOTE_START = re.compile(r"^(em .{5,120} escreveu:?|on .{5,120} wrote:?|-{2,}\s*(original message|mensagem original).*|de:\s.+@.+)$",
                          re.IGNORECASE)
_TAGS = re.compile(r"<[^>]+>")


class InboxError(Exception):
    """Falha ao ler a caixa (login, rede). Não afeta o WhatsApp."""


@dataclass(frozen=True)
class ImapConfig:
    host: str
    port: int
    user: str
    password: str


@dataclass(frozen=True)
class Mail:
    msg_id: str
    sender: str
    subject: str
    text: str
    when: datetime


@dataclass
class InboxResult:
    lidos: int = 0
    respostas: int = 0
    bounces: int = 0
    ignorados: int = 0


def strip_quoted(text: str) -> str:
    """Só o que a pessoa escreveu: corta a citação da nossa mensagem ('Em ... escreveu:' e linhas com '>')."""
    kept: list[str] = []
    for line in text.replace("\r", "").split("\n"):
        if _QUOTE_START.match(line.strip()):
            break
        if not line.lstrip().startswith(">"):
            kept.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def _body(msg: email.message.EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = part.get_content()
    return html.unescape(_TAGS.sub(" ", content)) if part.get_content_type() == "text/html" else content


def imap_fetch(cfg: ImapConfig, since: datetime) -> list[Mail]:
    """Mensagens da caixa de entrada desde `since` (somente leitura, sem marcar como lidas)."""
    mails: list[Mail] = []
    try:
        with imaplib.IMAP4_SSL(cfg.host, cfg.port, timeout=45) as conn:
            conn.login(cfg.user, cfg.password)
            conn.select("INBOX", readonly=True)
            _, data = conn.search(None, "SINCE", since.strftime("%d-%b-%Y"))
            for num in (data[0].split() if data and data[0] else [])[-MAX_MAILS:]:
                _, parts = conn.fetch(num, "(BODY.PEEK[])")
                raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
                if not raw:
                    continue
                msg = email.message_from_bytes(raw, policy=email.policy.default)
                try:
                    when = parsedate_to_datetime(str(msg["Date"]))
                except (TypeError, ValueError):
                    when = datetime.now().astimezone()
                mails.append(Mail(str(msg["Message-ID"] or f"{num!r}-{when:%s}"), parseaddr(str(msg["From"] or ""))[1].lower(),
                                  str(msg["Subject"] or ""), _body(msg), when))
    except (imaplib.IMAP4.error, OSError) as exc:
        raise InboxError(f"IMAP: {exc}") from exc
    return mails


def _sent_items(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT id, lead_id, destino FROM canal_fila WHERE canal = ? AND status = ?",
                        (canais.EMAIL, canais.ENVIADO)).fetchall()


def _match(items: list[sqlite3.Row], sender: str) -> sqlite3.Row | None:
    """Item enviado para esse endereço; senão, o único enviado para o mesmo domínio (outro funcionário respondeu)."""
    exact = [i for i in items if i["destino"] == sender]
    if exact:
        return exact[0]
    domain = sender.rsplit("@", 1)[-1]
    same = [i for i in items if i["destino"].rsplit("@", 1)[-1] == domain and domain not in _FREE]
    return same[0] if len(same) == 1 else None


_FREE = frozenset({"gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "yahoo.com.br", "live.com", "icloud.com"})


def _bounce(conn: sqlite3.Connection, items: list[sqlite3.Row], mail: Mail, now: datetime) -> bool:
    if not (_BOUNCE_SENDER.search(mail.sender) or _BOUNCE_SUBJECT.search(mail.subject)):
        return False
    haystack = mail.text.lower()
    hit = next((i for i in items if i["destino"] in haystack), None)
    if hit is None:
        return True   # é aviso de entrega, mas não sabemos de quem: ignora sem tratar como resposta
    with db.tx(conn):
        conn.execute("UPDATE canal_fila SET status = ?, erro = ? WHERE id = ?", (canais.ERRO, "e-mail voltou (bounce)", hit["id"]))
    canais.block(conn, hit["destino"], "bounce", now, lead_status=db.NOVO)   # o WhatsApp ainda pode alcançar o lead
    return True


def process(conn: sqlite3.Connection, mails: list[Mail], *, own_address: str, now: datetime,
            notify_fn: Callable[[str, str, bool], None]) -> InboxResult:
    """Aplica cada e-mail recebido: resposta de lead, 'parar' ou bounce. O resto da caixa é ignorado."""
    result = InboxResult(lidos=len(mails))
    items = _sent_items(conn)
    for mail in mails:
        if mail.sender == own_address.lower():
            result.ignorados += 1
            continue
        if _bounce(conn, items, mail, now):
            result.bounces += 1
            continue
        item = _match(items, mail.sender)
        lead = conn.execute("SELECT telefone FROM leads WHERE id = ?", (item["lead_id"],)).fetchone() if item else None
        if not item or not lead or not lead["telefone"]:
            result.ignorados += 1
            continue
        text = strip_quoted(mail.text) or mail.subject
        inbound = Inbound(numbers=frozenset({lead["telefone"]}), jids=frozenset(), text=f"[e-mail] {text}", push_name="",
                          msg_id=f"email:{mail.msg_id}", kind="EMAIL")
        outcome: ReplyOutcome = handle_inbound(conn, inbound, now=mail.when.astimezone(now.tzinfo), in_business_hours=True,
                                               notify_fn=notify_fn)
        if outcome.duplicate:
            result.ignorados += 1
            continue
        result.respostas += 1
        if outcome.status == db.NAO_QUER:
            canais.block(conn, item["destino"], "pediu para parar por e-mail", now)
    return result


def poll(conn: sqlite3.Connection, cfg: ImapConfig, notify_fn: Callable[[str, str, bool], None], now: datetime, *,
         fetch: Callable[[ImapConfig, datetime], list[Mail]] = imap_fetch) -> InboxResult:
    mails = fetch(cfg, now - timedelta(days=LOOKBACK_DAYS))
    return process(conn, mails, own_address=cfg.user, now=now, notify_fn=notify_fn)


def loop(conn: sqlite3.Connection, cfg: ImapConfig, notify_fn: Callable[[str, str, bool], None], every_s: int,
         out: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep) -> None:
    """Consulta a caixa a cada `every_s` segundos. Erro de rede só é registrado; erro de login encerra (evita bloqueio)."""
    while True:
        try:
            res = poll(conn, cfg, notify_fn, db.utcnow())
            out(f"e-mail: {res.lidos} lidos, {res.respostas} respostas, {res.bounces} voltaram")
        except InboxError as exc:
            log.warning("caixa ilegível: %s", exc)
            out(f"erro: {exc}")
            if "AUTHENTICATIONFAILED" in str(exc).upper() or "invalid credentials" in str(exc).lower():
                return
        sleep(every_s)
