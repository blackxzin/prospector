"""Indicações: quando um atendente passa o contato de quem decide, esse número vira um lead novo.

O número chega num cartão de contato (vCard) ou digitado na resposta ("fala com o Rafael, (14) 99712-3456").
O lead indicado entra na frente da fila com uma mensagem própria ("falei com a equipe de X e me passaram seu contato").
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from typing import Callable

from . import db
from .classify import SEM_SITE
from .phone import normalize_mobile, variants

_VCARD_TEL = re.compile(r"^TEL[^:\n]*:(.+)$", re.IGNORECASE | re.MULTILINE)
_VCARD_WAID = re.compile(r"waid=(\d{10,15})", re.IGNORECASE)
_VCARD_FN = re.compile(r"^FN[^:\n]*:(.+)$", re.IGNORECASE | re.MULTILINE)
_PHONE_TEXT = re.compile(r"(?<!\d)(?:\+?55[\s.-]*)?\(?\d{2}\)?[\s.-]*9\d{4}[\s.-]?\d{4}(?!\d)")
_NAME_OK = re.compile(r"^[A-Za-zÀ-ÿ'’.\- ]{2,60}$")
MAX_REFERRALS_PER_MESSAGE = 3


def numbers_in(text: str | None) -> list[str]:
    """Celulares brasileiros (55DDXXXXXXXXX) achados no texto ou no vCard, sem repetir, na ordem."""
    if not text:
        return []
    found: list[str] = []

    def add(raw: str) -> None:
        number = normalize_mobile(raw)
        if number and number not in found:
            found.append(number)

    for m in _VCARD_TEL.finditer(text):
        line = m.group(0)
        waid = _VCARD_WAID.search(line)
        add(waid.group(1) if waid else m.group(1))
    for m in _PHONE_TEXT.finditer(text):
        add(m.group(0))
    return found


def contact_name(text: str | None) -> str | None:
    """Nome do contato: 'FN:' do vCard ou, num cartão sem vCard, a primeira linha (o nome exibido)."""
    if not text:
        return None
    fn = _VCARD_FN.search(text)
    if fn:
        name = fn.group(1).strip()
    elif "BEGIN:VCARD" in text.upper() or numbers_in(text):
        return None
    else:
        name = text.strip().splitlines()[0].strip()
    return name if _NAME_OK.match(name) else None


def register_referral(conn: sqlite3.Connection, referrer_id: int, raw_number: str, *, nome: str | None,
                      now: datetime, display: str | None = None) -> list[int]:
    """Cria o lead indicado. [] se o número já existe, já foi contatado ou está bloqueado."""
    number = normalize_mobile(raw_number)
    if not number:
        raise ValueError("Informe um celular com DDD (ex.: 14 99712-3456); telefone fixo não serve.")
    referrer = db.get_lead(conn, referrer_id)
    if referrer is None:
        raise ValueError(f"Lead {referrer_id} não existe.")
    key = f"tel:{number}"
    numbers = variants(number)
    own = variants(referrer["telefone"]) if referrer["telefone"] else set()
    if (number in own or db.is_blocked(conn, numbers) or db.first_message_already_sent(conn, numbers)
            or conn.execute("SELECT 1 FROM leads WHERE chave = ?", (key,)).fetchone()):
        return []
    from .messages import clean_name
    origem = clean_name(referrer["nome"], referrer["cidade"])
    contato = (nome or "").strip() or None
    stamp = db.iso(now)
    with db.tx(conn):
        conn.execute(
            "INSERT INTO leads (chave, nome, categoria, endereco, cidade, telefone_raw, telefone, busca, oferta, status, "
            "criado_em, atualizado_em, pais, idioma, fuso, nicho, indicado_por, contato_nome, presenca_checada_em) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key, f"{contato or 'Responsável'} ({origem})", referrer["categoria"], referrer["endereco"],
             referrer["cidade"], raw_number, number, f"indicação de {referrer['nome']}", SEM_SITE, db.NOVO,
             stamp, stamp, referrer["pais"], referrer["idioma"], referrer["fuso"], referrer["nicho"], referrer_id,
             contato, stamp),
        )
        new_id = conn.execute("SELECT id FROM leads WHERE chave = ?", (key,)).fetchone()["id"]
    return [new_id]


def register_from_reply(conn: sqlite3.Connection, referrer: sqlite3.Row, text: str, kind: str, now: datetime,
                        notify_fn: Callable[[str, str, bool], None]) -> list[int]:
    """Procura números na resposta de um lead e cria os indicados (no máximo 3 por mensagem)."""
    numbers = numbers_in(text)[:MAX_REFERRALS_PER_MESSAGE]
    name = contact_name(text) if kind == "CONTACT" or "BEGIN:VCARD" in (text or "").upper() else None
    created: list[int] = []
    for number in numbers:
        for new_id in register_referral(conn, referrer["id"], number, nome=name if len(numbers) == 1 else None, now=now):
            created.append(new_id)
            who = f"{name} ({number})" if name else number
            notify_fn(f"{referrer['nome']} indicou {who}",
                      "O contato entrou na frente da fila, com mensagem pronta. Confira e aprove.", False)
    if kind == "CONTACT" and not numbers:
        notify_fn(f"{referrer['nome']} mandou um cartão de contato",
                  f"O WhatsApp não trouxe o número{f' de {name}' if name else ''}. Abra a conversa para ver e registre com: "
                  f"prospector indicado NUMERO --de {referrer['id']}" + (f' --nome "{name}"' if name else ""), False)
    return created
