import json
import random

import pytest

from prospector import db, sitecheck
from prospector.classify import SITE_PROBLEMA, TEM_SITE
from prospector.config import ROOT
from prospector.importer import import_rows
from prospector.messages import TemplateSet, TemplateError, Templates
from prospector.sitecheck import extract_signals

from .conftest import TZ, csv_row, local_dt

GOOD = b"""<html><head><meta name="viewport" content="width=device-width">
<meta name="description" content="Pizzaria em Itai"><title>Bella</title></head>
<body><a href="https://wa.me/5514991234567">Pedir</a> <a href="tel:+5514991234567">Ligar</a>
<footer>&copy; 2026 Bella</footer></body></html>"""

BARE = b"""<html><head><meta name="viewport" content="width=device-width"><title>Bella</title></head>
<body><h1>Pizzaria Bella</h1><p>""" + b"Rua A, 10. " * 300 + b"""</p><footer>&copy; 2018 Bella</footer></body></html>"""


def test_site_with_whatsapp_phone_and_description_has_no_signals():
    assert extract_signals(GOOD, 2026) == ()


def test_signals_found_on_a_bare_site():
    got = extract_signals(BARE, 2026)
    assert {"sem_whatsapp", "sem_telefone", "sem_descricao"} <= set(got)
    assert "rodape_2018" in got


@pytest.mark.parametrize("html", [
    b'<a href="https://api.whatsapp.com/send?phone=5514">x</a>' + b" " * 3000,
    b'<script src="https://cdn.example/whatsapp-widget.js"></script>' + b" " * 3000,
    b'<a href="whatsapp://send?phone=5514">x</a>' + b" " * 3000,
])
def test_any_whatsapp_mention_counts_as_having_it(html):
    assert "sem_whatsapp" not in extract_signals(html, 2026)


def test_tiny_or_script_only_page_is_not_judged():
    # página minúscula ou SPA sem conteúdo: não dá para afirmar que falta botão
    assert extract_signals(b"<html><body><div id='root'></div></body></html>", 2026) == ()


def test_check_site_reports_signals_even_without_problems():
    rep = sitecheck.check_site("https://bella.example", fetch=lambda u: (200, "https://bella.example/", BARE, 0.4))
    assert rep.ok and "sem_whatsapp" in rep.sinais


def test_check_leads_stores_signals_and_rechecks_old_ones_without_them(conn, listas):
    import_rows(conn, [csv_row(website="https://bella.example")], "q", listas)
    lead = conn.execute("SELECT * FROM leads").fetchone()
    assert lead["oferta"] == TEM_SITE
    with db.tx(conn):  # checado antes desta função existir: tem data, mas não tem sinais
        db.update_lead(conn, lead["id"], site_checado_em=db.iso(db.utcnow()))
    rep = sitecheck.SiteReport("https://bella.example", (), 200, 0.3, ("sem_whatsapp", "sem_telefone"))
    checked, bad = sitecheck.check_leads(conn, check=lambda url: rep, out=lambda s: None)
    row = db.get_lead(conn, lead["id"])
    assert (checked, bad) == (1, 0) and json.loads(row["site_sinais"]) == ["sem_whatsapp", "sem_telefone"]
    assert sitecheck.check_leads(conn, check=lambda url: rep, out=lambda s: None) == (0, 0)  # não repete


def test_message_mentions_missing_whatsapp_only_for_that_signal():
    tset = TemplateSet.load(ROOT / "templates.yaml")
    base = {"nome": "Bella", "nota": 4.7, "avaliacoes": 120, "categoria": "Pizzaria", "oferta": "tem_site"}
    with_signal = {**base, "site_sinais": json.dumps(["sem_whatsapp"])}
    for seed in range(15):
        text = tset.get("pt").first_message(with_signal, local_dt(2026, 10, 20, 10), TZ, random.Random(seed))
        assert "WhatsApp" in text and ("botão" in text) and "site" in text
        plain = tset.get("pt").first_message(base, local_dt(2026, 10, 20, 10), TZ, random.Random(seed))
        assert "botão de WhatsApp nele" not in plain and "não vi botão" not in plain and "não tem botão" not in plain


def test_foreign_templates_never_inherit_the_portuguese_signal_texts():
    tset = TemplateSet.load(ROOT / "templates.yaml")
    en = tset.get("en")
    assert "tem_site_sem_whatsapp" not in en.data["primeira"]
    lead = {"nome": "Joe", "oferta": "tem_site", "site_sinais": json.dumps(["sem_whatsapp"]), "categoria": "Pizza"}
    assert "botão" not in en.first_message(lead, local_dt(2026, 10, 20, 10), TZ, random.Random(1))


def test_special_group_needs_three_variants(tmp_path):
    bad = (ROOT / "templates.yaml").read_text(encoding="utf-8").replace("tem_site_sem_whatsapp:", "tem_site_sem_whatsapp: [x]\n  _velho:")
    p = tmp_path / "t.yaml"
    p.write_text(bad, encoding="utf-8")
    with pytest.raises(TemplateError):
        Templates.load(p)
