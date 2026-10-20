import pytest

from prospector.phone import normalize_any, normalize_mobile, phone_from_jid, pretty, variants


@pytest.mark.parametrize("raw", [
    "(14) 99123-4567", "14991234567", "+55 14 99123-4567", "55 (14) 9 9123 4567",
    "014 99123-4567", "0055 14 991234567", "+55 (14) 99123-4567 ",
])
def test_normalize_mobile_accepts_formats(raw):
    assert normalize_mobile(raw) == "5514991234567"


@pytest.mark.parametrize("raw", [
    "(14) 3761-1234",     # fixo
    "(14) 9123-4567",     # 10 dígitos (sem o nono dígito)
    "(14) 89123-4567",    # 11 dígitos sem 9 depois do DDD
    "(01) 99123-4567",    # DDD inválido
    "(10) 99123-4567",    # DDD inválido
    "0800 123 4567",
    "", None, "abc",
    "+1 415 555 0101",
])
def test_normalize_mobile_rejects(raw):
    assert normalize_mobile(raw) is None


def test_normalize_any_keeps_landlines_for_dedup():
    assert normalize_any("(14) 3761-1234") == "551437611234"
    assert normalize_any("+55 14 3761-1234") == "551437611234"
    assert normalize_any("14 99123 4567") == "5514991234567"
    assert normalize_any("") is None


def test_variants_with_and_without_ninth_digit():
    assert variants("5514991234567") == {"5514991234567", "551491234567"}
    assert variants("551491234567") == {"551491234567", "5514991234567"}


def test_phone_from_jid():
    assert phone_from_jid("5514991234567@s.whatsapp.net") == "5514991234567"
    assert phone_from_jid("5514991234567:12@s.whatsapp.net") == "5514991234567"
    assert phone_from_jid("120363@g.us") is None
    assert phone_from_jid("12345@lid") is None
    assert phone_from_jid(None) is None


def test_pretty():
    assert pretty("5514991234567") == "(14) 99123-4567"
    assert pretty("551437611234") == "(14) 3761-1234"


# ---------- telefones fora do Brasil ----------

import pytest as _pytest  # noqa: E402

from prospector.phone import normalize_any_intl, normalize_mobile_intl, pretty  # noqa: E402


@_pytest.mark.parametrize("raw, region, expected", [
    ("+34 612 34 56 78", "ES", "34612345678"),
    ("612 34 56 78", "ES", "34612345678"),
    ("(212) 555-0147", "US", "12125550147"),       # EUA/Canadá não distinguem fixo de celular: aceita
    ("+1 416-555-0199", "CA", "14165550199"),
    ("07911 123456", "GB", "447911123456"),
    ("+49 151 23456789", "DE", "4915123456789"),
    ("06 12 34 56 78", "FR", "33612345678"),
    ("+39 333 123 4567", "IT", "393331234567"),
    ("912 345 678", "PT", "351912345678"),
])
def test_foreign_mobile_numbers_are_normalized(raw, region, expected):
    assert normalize_mobile_intl(raw, region) == expected


@_pytest.mark.parametrize("raw, region", [
    ("030 123456", "DE"),            # fixo
    ("91 123 45 67", "ES"),          # fixo de Madrid
    ("12345", "US"), ("", "GB"), (None, "FR"), ("not a number", "IT"),
])
def test_foreign_landlines_and_garbage_are_rejected(raw, region):
    assert normalize_mobile_intl(raw, region) is None


def test_any_intl_keeps_landlines_for_deduplication():
    assert normalize_any_intl("91 123 45 67", "ES") == "34911234567"
    assert normalize_any_intl("+34 911 23 45 67", "ES") == "34911234567"
    assert normalize_any_intl(None, "ES") is None


def test_pretty_handles_foreign_numbers():
    assert pretty("5514991234567") == "(14) 99123-4567"
    assert pretty("34612345678") == "+34 612 34 56 78"
    assert pretty("14165550199") == "+1 416-555-0199"
