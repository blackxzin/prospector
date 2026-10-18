"""Normalização de telefones brasileiros."""

from __future__ import annotations

import re

COUNTRY = "55"
_NON_DIGIT = re.compile(r"\D+")


def _national(raw: str | None) -> str:
    """Devolve só os dígitos nacionais (DDD + número), sem 55 e sem zero de tronco."""
    if not raw:
        return ""
    digits = _NON_DIGIT.sub("", raw)
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith(COUNTRY) and len(digits) in (12, 13):
        digits = digits[2:]
    if digits.startswith("0") and len(digits) in (11, 12):
        digits = digits[1:]
    return digits


def _valid_ddd(ddd: str) -> bool:
    return len(ddd) == 2 and ddd[0] != "0" and ddd[1] != "0"


def normalize_mobile(raw: str | None) -> str | None:
    """Celular BR válido para WhatsApp -> '55DDXXXXXXXXX' (13 dígitos). Senão None.

    Regra: 11 dígitos nacionais, DDD válido e '9' logo depois do DDD.
    """
    national = _national(raw)
    if len(national) != 11 or not _valid_ddd(national[:2]) or national[2] != "9":
        return None
    return COUNTRY + national


def normalize_any(raw: str | None) -> str | None:
    """Qualquer telefone (fixo ou celular) em formato canônico, para deduplicação."""
    national = _national(raw)
    if len(national) in (10, 11) and _valid_ddd(national[:2]):
        return COUNTRY + national
    return national or None


def to_jid(number: str) -> str:
    return f"{number}@s.whatsapp.net"


def variants(number: str) -> set[str]:
    """Formas que o mesmo celular pode ter num JID (com e sem o nono dígito)."""
    out = {number}
    if number.startswith(COUNTRY) and len(number) == 13 and number[4] == "9":
        out.add(number[:4] + number[5:])
    if number.startswith(COUNTRY) and len(number) == 12:
        out.add(number[:4] + "9" + number[4:])
    return out


def phone_from_jid(jid: str | None) -> str | None:
    """'5514999999999@s.whatsapp.net' (ou com ':device') -> '5514999999999'.

    JIDs de grupo (@g.us) e LIDs (@lid) não têm telefone e devolvem None.
    """
    if not jid:
        return None
    base, _, domain = jid.partition("@")
    base = base.split(":")[0]
    if domain == "s.whatsapp.net" and base.isdigit() and 10 <= len(base) <= 15:
        return base
    return None


def pretty(number: str | None) -> str:
    """'5514991234567' -> '(14) 99123-4567'."""
    if not number:
        return ""
    national = number[2:] if number.startswith(COUNTRY) and len(number) >= 12 else number
    if len(national) == 11:
        return f"({national[:2]}) {national[2:7]}-{national[7:]}"
    if len(national) == 10:
        return f"({national[:2]}) {national[2:6]}-{national[6:]}"
    return number
