"""Normalização de telefones: celulares brasileiros e números de qualquer país (via phonenumbers)."""

from __future__ import annotations

import re

import phonenumbers
from phonenumbers import PhoneNumberType

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


# Tipos aceitos como "pode ter WhatsApp". EUA/Canadá não distinguem fixo de celular (FIXED_LINE_OR_MOBILE):
# a confirmação real vem da checagem no WA-AKG antes do envio.
_MOBILE_TYPES = frozenset({PhoneNumberType.MOBILE, PhoneNumberType.FIXED_LINE_OR_MOBILE})


def _parse_intl(raw: str | None, region: str) -> phonenumbers.PhoneNumber | None:
    if not raw or not raw.strip():
        return None
    try:
        number = phonenumbers.parse(raw.strip(), region.upper())
    except phonenumbers.NumberParseException:
        return None
    return number if phonenumbers.is_valid_number(number) else None


def _e164_digits(number: phonenumbers.PhoneNumber) -> str:
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164).lstrip("+")


def normalize_mobile_intl(raw: str | None, region: str) -> str | None:
    """Celular válido de qualquer país -> dígitos E.164 sem '+' (ex.: '34612345678'). Fixo/lixo -> None.
    `region` é o país ISO-2 usado quando o número vem sem código ('ES', 'US'...)."""
    number = _parse_intl(raw, region)
    if number is None or phonenumbers.number_type(number) not in _MOBILE_TYPES:
        return None
    return _e164_digits(number)


def normalize_any_intl(raw: str | None, region: str) -> str | None:
    """Qualquer telefone válido (fixo ou celular) em E.164 sem '+', para deduplicação."""
    number = _parse_intl(raw, region)
    return _e164_digits(number) if number else None


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
    """'5514991234567' -> '(14) 99123-4567'; número estrangeiro -> formato internacional."""
    if not number:
        return ""
    if not (number.startswith(COUNTRY) and len(number) in (12, 13)) and number.isdigit() and len(number) >= 8:
        try:
            parsed = phonenumbers.parse("+" + number)
            if phonenumbers.is_valid_number(parsed):
                return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
        except phonenumbers.NumberParseException:
            pass
    national = number[2:] if number.startswith(COUNTRY) and len(number) >= 12 else number
    if len(national) == 11:
        return f"({national[:2]}) {national[2:7]}-{national[7:]}"
    if len(national) == 10:
        return f"({national[:2]}) {national[2:6]}-{national[6:]}"
    return number
