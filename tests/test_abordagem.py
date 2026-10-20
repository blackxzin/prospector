import json
import random

import pytest

from prospector import db
from prospector import fila as fq
from prospector.config import ROOT
from prospector.importer import import_rows
from prospector.messages import TemplateSet, Templates, instagram_handle, review_snippet

from .conftest import TZ, csv_row, local_dt

NOW = local_dt(2026, 10, 20, 10)
OFFERS = ["sem_site", "so_social", "tem_site", "site_problema"]


def _reviews(*items):
    return json.dumps([{"nome": "A", "nota": n, "texto": t, "quando": "1 mês"} for n, t in items])


@pytest.mark.parametrize("raw, expected", [
    ([(5, "Melhor pizza da cidade! Sou de Jundiaí e adorei.")], "Melhor pizza da cidade"),
    ([(5, "Lugar maravilhoso...td delicioso muito bom mesmo")], ""),                  # reticências: texto sujo
    ([(4, "Muito bom o atendimento aqui")], ""),                                      # só 5 estrelas
    ([(5, "Bom")], ""),                                                              # curta demais
    ([(5, "Ligue 14 99123-4567 para pedir muito bom")], ""),                         # número no meio
    ([(5, "MELHOR PIZZA DA CIDADE COM CERTEZA")], ""),                               # gritando
    ([(5, "Ambiente familiar e de amigos. As pizzas são saborosas."), (5, "Melhor pizza da cidade")],
     "Melhor pizza da cidade"),                                                      # escolhe a mais curta
    ([(5, "x " * 40)], ""),                                                          # longa demais
])
def test_review_snippet(raw, expected):
    assert review_snippet(_reviews(*raw)) == expected


def test_review_snippet_handles_missing_or_broken_data():
    assert review_snippet(None) == "" and review_snippet("") == "" and review_snippet("não é json") == ""
    assert review_snippet('{"a": 1}') == ""


@pytest.mark.parametrize("site, expected", [
    ("https://instagram.com/pizzariabella?igshid=abc", "@pizzariabella"),
    ("https://www.instagram.com/esfihariaflavinho_ofc/", "@esfihariaflavinho_ofc"),
    ("http://instagram.com/docabbq", "@docabbq"),
    ("https://www.instagram.com/p/DMeAxVwsrqs/", ""), ("https://www.instagram.com/", ""),
    ("https://www.facebook.com/pizzariabella", ""), ("https://pizzaria.com.br", ""), (None, ""),
])
def test_instagram_handle(site, expected):
    assert instagram_handle(site) == expected


@pytest.fixture(scope="module")
def pt():
    return Templates.load(ROOT / "templates.yaml")


@pytest.mark.parametrize("offer", OFFERS)
def test_every_variant_is_short_without_link_and_asks_for_the_responsible(pt, offer):
    lead = {"nome": "Pizzaria Bella", "nota": 4.7, "avaliacoes": 132, "categoria": "Pizzaria", "oferta": offer,
            "cidade": "Itaí", "site": "https://instagram.com/pizzariabella",
            "avaliacoes_json": _reviews((5, "Melhor pizza da cidade!")), "site_problemas": "o site está fora do ar"}
    seen = set()
    for seed in range(80):
        text = pt.first_message(lead, NOW, TZ, random.Random(seed))
        seen.add(text)
        assert len(text.split()) <= 50 and "http" not in text and text.endswith("?") and "{" not in text
        assert "Lucas" in text
    assert len(seen) >= 3                                                      # as variações realmente variam


def test_variants_that_need_data_are_not_used_without_it(pt):
    bare = {"nome": "Bella", "nota": 3.0, "avaliacoes": 3, "categoria": "Pizzaria", "oferta": "so_social",
            "cidade": "Itaí", "site": "https://facebook.com/bella"}
    for offer in ("sem_site", "so_social", "tem_site"):
        for seed in range(60):
            text = pt.first_message({**bare, "oferta": offer}, NOW, TZ, random.Random(seed))
            assert "“" not in text and "@" not in text and "nota" not in text.lower() and "  " not in text
            assert "vi o de vocês" not in text.lower()


def test_review_quote_and_handle_appear_when_available(pt):
    lead = {"nome": "Bella", "nota": 4.8, "avaliacoes": 90, "categoria": "Pizzaria", "oferta": "so_social",
            "cidade": "Itaí", "site": "https://instagram.com/pizzariabella",
            "avaliacoes_json": _reviews((5, "Melhor pizza da cidade!"))}
    texts = [pt.first_message(lead, NOW, TZ, random.Random(s)) for s in range(80)]
    assert any("“Melhor pizza da cidade”" in t for t in texts) and any("@pizzariabella" in t for t in texts)


def test_without_city_it_says_aqui_da_regiao(pt):
    lead = {"nome": "Bella", "oferta": "sem_site", "nota": None, "avaliacoes": None, "categoria": "Pizzaria"}
    texts = [pt.first_message(lead, NOW, TZ, random.Random(s)) for s in range(30)]
    assert all("aqui da região" in t and "de None" not in t for t in texts)


def test_followups_do_not_repeat_the_pitch_and_only_the_last_has_the_portfolio(pt):
    lead = {"nome": "Bella", "oferta": "sem_site", "categoria": "Pizzaria", "cidade": "Itaí"}
    texts = {pt.followup(lead, NOW, TZ, random.Random(s)) for s in range(30)}
    assert len(texts) == 3
    assert sum("vercel.app" in t for t in texts) == 1
    assert not any("exemplo" in t for t in texts)


def test_template_needs_three_unconditional_variants(tmp_path):
    from prospector.messages import TemplateError
    text = (ROOT / "templates.yaml").read_text(encoding="utf-8")
    broken = text.replace('"Oi, {saudacao_minuscula}! Sou o Lucas, desenvolvedor {local}. Procurei {nome} no Google e não achei site de vocês. Com quem eu falo sobre isso?"',
                          '"Oi! {avaliacao} Quem cuida disso?"')
    assert broken != text
    p = tmp_path / "t.yaml"
    p.write_text(broken, encoding="utf-8")
    with pytest.raises(TemplateError, match="não dependam"):
        Templates.load(p)


def test_foreign_languages_still_load_and_need_no_new_fields():
    tset = TemplateSet.load(ROOT / "templates.yaml")
    lead = {"nome": "Joe's", "oferta": "sem_site", "nota": 4.6, "avaliacoes": 100, "categoria": "Pizza"}
    assert "STOP" in tset.get("en").first_message(lead, NOW, TZ, random.Random(1))


# ---------- refazer o texto da fila ----------

def _queued(conn, listas, tset, rules, n=3):
    rows = [csv_row(title=f"Pizzaria {i}", phone=f"(14) 991{i:02d}-00{i:02d}", link=f"l{i}",
                    address=f"R. A, {i} - Centro, Itaí - SP") for i in range(n)]
    import_rows(conn, rows, "q", listas)
    fq.build_queue(conn, tset, rules, NOW, size=n, followup_days=3, rng=random.Random(0))


def test_regenerate_texts_keeps_status_and_release_time(conn, listas, rules):
    tset = TemplateSet.load(ROOT / "templates.yaml")
    _queued(conn, listas, tset, rules)
    ids = fq.pending_ids(conn)
    fq.approve(conn, ids[:2], liberar_em=local_dt(2026, 10, 21, 9))
    conn.execute("UPDATE fila SET texto = 'texto velho comprido e genérico'")
    conn.commit()
    n = fq.regenerate_texts(conn, tset, rules, NOW, rng=random.Random(1))
    rows = conn.execute("SELECT * FROM fila ORDER BY id").fetchall()
    assert n == 3 and all("texto velho" not in r["texto"] for r in rows)
    assert [r["status"] for r in rows] == ["aprovado", "aprovado", "pendente"]
    assert rows[0]["liberar_em"] and rows[2]["liberar_em"] is None
    assert all(r["variante"] for r in rows)


def test_regenerate_texts_skips_items_already_sent_and_edited_ones(conn, listas, rules):
    tset = TemplateSet.load(ROOT / "templates.yaml")
    _queued(conn, listas, tset, rules)
    first, second, third = [r["id"] for r in conn.execute("SELECT id FROM fila ORDER BY id")]
    conn.execute("UPDATE fila SET status = 'enviado' WHERE id = ?", (first,))
    conn.execute("UPDATE fila SET texto = 'editado por mim', editado = 1 WHERE id = ?", (second,))
    conn.commit()
    assert fq.regenerate_texts(conn, tset, rules, NOW, rng=random.Random(1)) == 1
    assert conn.execute("SELECT texto FROM fila WHERE id = ?", (second,)).fetchone()[0] == "editado por mim"


def test_only_the_base_city_is_claimed_as_home(pt):
    for cidade, expected, forbidden in [("Itaí", "de Itaí", None), ("Avaré", "aqui da região", "de Avaré"),
                                        ("ITAI", "de Itaí", None), (None, "aqui da região", "de None")]:
        lead = {"nome": "Bella", "oferta": "sem_site", "nota": None, "avaliacoes": None, "categoria": "x",
                "cidade": cidade}
        for seed in range(12):
            text = pt.first_message(lead, NOW, TZ, random.Random(seed))
            assert expected in text and (forbidden is None or forbidden not in text)
