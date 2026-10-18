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
