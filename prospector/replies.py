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

# Opt-out explícito: vale em qualquer tamanho de mensagem (pt, en, es, fr, de, it). Texto já sem acento
# (fold). O rodapé das abordagens pede STOP/PARAR, então os dois sempre contam.
_OPT_OUT_STRONG = re.compile(
    r"\bpare\b|\bparar\b|\bpara de\b|\bremov|\bdescadastr|\bsem interesse\b|\bdesinteress|"
    r"\bnao (tenho interesse|quero|preciso)\b|\bme tira\b|\bbloque|\bspam\b|\bdenunc|\bnao mand|\bstop\b|"
    r"unsubscribe|not interested|no thanks|no thank you|do ?n[o']?t (contact|message|text|write|email)|"
    r"leave me alone|remove me|take me off|stop (messaging|texting|writing)|please stop|"
    r"no me interesa|no nos interesa|no gracias|dej(a|en|e) de|no me (escribas|escriban|molest)|no quiero|"
    r"no estoy interesad|"
    r"pas interesse|non merci|arretez|arrete de|ne m'ecrivez plus|ne me contactez|desabonn|"
    r"kein interesse|nicht interessiert|nein danke|aufhoren|abmelden|schreiben sie mir nicht|lassen sie mich|"
    r"non mi interessa|non ci interessa|no grazie|smettila|smettete|non scrivermi|non contattarmi|\bbasta\b",
)
# Opt-out fraco: só conta em mensagem curta. Em texto longo costuma ser aviso do negócio
# ("não fazemos entrega"), não um pedido para parar.
_OPT_OUT_WEAK = re.compile(
    r"\bnao\b|\bnaum\b|\bnn\b|\bsair\b|\bsai\b|\bchato\b|\bincomod|\bobrigad[oa],? mas\b",
)
WEAK_OPT_OUT_MAX_WORDS = 8
# "no", "non", "nein", "nope"... são ambíguos fora do inglês/espanhol: só valem como resposta de 1 a 3 palavras.
_OPT_OUT_WEAK_SHORT = re.compile(r"^\W*(no|non|nein|nope|nah|nao|nada)\W*(\w+\W*){0,2}$")
_INTEREST = re.compile(
    r"\bsim\b|\bpode\b|\bquero\b|\bmanda\b|\bmande\b|\btenho interesse\b|\binteress|\bclaro\b|"
    r"\bcomo funciona\b|\bquanto\b|\bvalor\b|\bpreco\b|\bbora\b|\bok\b|\bpode ser\b|\bgostaria\b|"
    r"\byes\b|\bsure\b|\bplease\b|\bsend (it|me|over)\b|interested|how much|\bprice\b|tell me more|"
    r"sounds good|\bokay\b|\bgo ahead\b|\bpricing\b|"
    r"\bsi\b|me interesa|mandame|enviame|\bcuanto\b|\bprecio\b|\bvale\b|"
    r"\boui\b|bien sur|\bcombien\b|\btarif\b|envoyez|"
    r"\bja\b|\bgerne\b|interessiert|wie viel|\bpreis\b|schicken sie|"
    r"\bcerto\b|mi interessa|quanto costa|\bprezzo\b|\bmandi\b|\bmandami\b",
)


# Sinais de que o cliente fechou (vira "possível fechamento" para você confirmar no painel).
_CLOSING = re.compile(
    r"\bfechado\b|\bfechou\b|\bfechamos\b|\bvamos fechar\b|\bpode fazer\b|\bpode comecar\b|"
    r"\bpode seguir\b|\bvamos fazer\b|\bbora fazer\b|\bquero fazer\b|\bquero contratar\b|"
    r"\bvou querer\b|\bvou fechar\b|\bqual (o|a) (seu )?pix\b|\bmanda (o )?pix\b|\bchave pix\b|"
    r"\bcomo (eu )?pago\b|\bcomo faco (o|para) pag|\bfaz o contrato\b|\bpode mandar o contrato\b",
)


# Mensagem automática de robô/atendimento do negócio (não é uma pessoa respondendo à nossa abordagem).
_AUTO = re.compile(
    r"auto ?atendimento|atendimento automatic|mensagem automatica|resposta automatica|bem[- ]?vind[oa]|"
    r"agradece (o |seu )?contato|obrigad[oa] pelo seu contato|vamos responder|responderemos em|obrigad[oa] pel[oa] (seu )?contato|horario de atendimento|nosso horario|"
    r"link (do|de nosso) cardapio|segue o (link|cardapio)|como podemos (te )?(ajudar|atender)|"
    r"thank you for (contacting|reaching|your message)|thanks for (contacting|reaching|your message)|"
    r"we(')?ll get back|we will get back|opening hours|business hours|automatic (reply|response)|auto.?reply|"
    r"out of (the )?office|welcome to|currently closed|we are (currently )?closed|how can (we|i) help|"
    r"gracias por (contactar|escribir|comunicarse)|horario de (atencion|apertura)|respuesta automatica|"
    r"estamos cerrados|bienvenid[oa] a|como podemos ayudar|"
    r"merci de nous avoir contact|horaires d'ouverture|reponse automatique|nous sommes fermes|bienvenue chez|"
    r"vielen dank fur ihre nachricht|offnungszeiten|automatische antwort|willkommen bei|derzeit geschlossen|"
    r"grazie per (averci|aver) contattat|orari di apertura|risposta automatica|siamo chiusi|benvenut[oi] da|"
    r"come possiamo aiutar|"
    r"em que (posso|podemos) (te )?ajudar|que bom falar com|seu interesse e\b|1\ufe0f?\u20e3|digite [0-9]|escolha uma opcao|no momento nao (estamos|estou) "
    r"(atendendo|disponivel)|estamos fora do horario",
)


def is_auto_reply(text: str | None) -> bool:
    return bool(_AUTO.search(fold(text or "")))


def is_closing(text: str | None) -> bool:
    return not is_opt_out(text) and bool(_CLOSING.search(fold(text or "")))


def is_opt_out(text: str | None) -> bool:
    folded = fold(text or "")
    if _OPT_OUT_STRONG.search(folded):
        return True
    if _OPT_OUT_WEAK_SHORT.match(folded):
        return True
    return len(folded.split()) <= WEAK_OPT_OUT_MAX_WORDS and bool(_OPT_OUT_WEAK.search(folded))


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
    msg_id: str | None = None  # id da mensagem no WhatsApp (só quando vem do polling)


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
    closing: bool = False
    duplicate: bool = False


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
    if msg.msg_id and conn.execute("SELECT 1 FROM respostas WHERE msg_id = ?", (msg.msg_id,)).fetchone():
        return ReplyOutcome(lead["id"], lead["status"], duplicate=True)
    with db.tx(conn):
        conn.execute("INSERT INTO respostas (lead_id, telefone, texto, recebido_em, msg_id) VALUES (?, ?, ?, ?, ?)",
                     (lead["id"], lead["telefone"], msg.text, db.iso(now), msg.msg_id))
        fields: dict[str, object] = {"resposta": msg.text[:2000]}
        if not lead["respondeu_em"]:
            fields["respondeu_em"] = db.iso(now)
        auto_msg = is_auto_reply(msg.text) and lead["status"] not in (db.NAO_QUER, db.CLIENTE)
        if auto_msg:
            # robô do negócio: guarda, mas não é opt-out nem interesse; não rebaixa quem já mostrou interesse
            new_status = db.INTERESSADO if lead["status"] == db.INTERESSADO else db.RESPONDEU
        elif lead["status"] == db.CLIENTE:
            # já é cliente: conversa normal, um "não" aqui não é pedido de descadastro
            new_status = db.CLIENTE
        elif lead["status"] == db.NAO_QUER or is_opt_out(msg.text):
            new_status = db.NAO_QUER
            for number in variants(lead["telefone"]) | set(msg.numbers):
                db.block(conn, number, f"opt-out: {msg.text[:120]}")
            # cancela qualquer coisa pendente para esse lead
            conn.execute("UPDATE fila SET status = 'cancelado', atualizado_em = ? "
                         "WHERE lead_id = ? AND status IN ('pendente', 'aprovado')", (db.iso(now), lead["id"]))
        elif lead["status"] == db.INTERESSADO or is_interested(msg.text) or is_closing(msg.text):
            new_status = db.INTERESSADO
        else:
            new_status = db.RESPONDEU
        if new_status != db.NAO_QUER:
            conn.execute("UPDATE fila SET status = 'cancelado', atualizado_em = ? "
                         "WHERE lead_id = ? AND tipo = 'followup' AND status IN ('pendente', 'aprovado')",
                         (db.iso(now), lead["id"]))
        fields["status"] = new_status
        closing = new_status == db.INTERESSADO and not auto_msg and is_closing(msg.text)
        if closing:
            fields["sinal_fechamento"] = 1
        auto = (new_status not in (db.NAO_QUER, db.CLIENTE) and not in_business_hours and not auto_msg
                and not lead["auto_resposta_enviada"])
        if auto:
            fields["auto_resposta_enviada"] = 1  # reserva já: no máximo uma vez por contato
        db.update_lead(conn, lead["id"], **fields)

    if auto_msg:  # robô: sem alarme
        return ReplyOutcome(lead["id"], new_status)
    label = {db.NAO_QUER: "pediu para parar (bloqueado)", db.INTERESSADO: "parece interessado"}.get(new_status, "respondeu")
    if closing:
        label = "POSSÍVEL FECHAMENTO, confirme no painel"
    notify_fn(f"{lead['nome']} {label}", f"{msg.text[:300]}", new_status == db.INTERESSADO)
    return ReplyOutcome(lead["id"], new_status, auto, closing)
