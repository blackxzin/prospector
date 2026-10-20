from datetime import date

import pytest

from prospector.config import ROOT
from prospector.nichos import Nichos

YAML = """
inicio: 2026-10-21
ordem: [clinicas, mecanicos, corretores]
nichos:
  clinicas: {nome: Clínicas, buscas: [dentista, psicólogo], categorias: [dentista, psicolog, clinica], dor: "Paciente chama, quem responde?"}
  mecanicos: {nome: Oficinas, buscas: [oficina mecânica], categorias: [oficina, mecanic], dor: "Orçamento, quem responde?"}
  corretores: {nome: Imóveis, buscas: [imobiliária], categorias: [imobiliaria, corretor], dor: "Imóvel, quem responde?"}
  alimentacao: {nome: Comida, buscas: [], categorias: [pizza, restaurante, lanch], dor: ""}
"""


@pytest.fixture
def nichos(tmp_path):
    p = tmp_path / "nichos.yaml"
    p.write_text(YAML, encoding="utf-8")
    return Nichos.load(p)


@pytest.mark.parametrize("busca, categoria, expected", [
    ("dentista em Itaí SP", "Dentista", "clinicas"),
    ("pizzaria em Itaí SP", "Pizzaria", "alimentacao"),
    ("x", "Oficina mecânica", "mecanicos"),
    ("imobiliária em Avaré SP", "Agência", "corretores"),        # categoria não bate, a busca sim
    ("x", "Clínica Odontológica", "clinicas"),
    ("x", "Papelaria", None), ("", "", None),
])
def test_classify(nichos, busca, categoria, expected):
    assert nichos.classify(busca, categoria) == expected


def test_one_niche_per_business_day_in_order_and_restarts(nichos):
    seq = {d: nichos.do_dia(date(2026, 10, d)).id for d in (21, 22, 23, 26, 27, 28, 29, 30)}
    assert [seq[21], seq[22], seq[23]] == ["clinicas", "mecanicos", "corretores"]
    assert [seq[26], seq[27], seq[28]] == ["clinicas", "mecanicos", "corretores"]   # fim de semana não conta
    assert nichos.do_dia(date(2026, 10, 24)) is None and nichos.do_dia(date(2026, 10, 25)) is None


def test_before_start_uses_the_first_niche(nichos):
    assert nichos.do_dia(date(2026, 10, 20)).id == "clinicas"


def test_queries_use_each_search_term_for_each_city(nichos):
    assert nichos.queries("clinicas", ("Itaí SP", "Avaré SP")) == [
        "dentista em Itaí SP", "psicólogo em Itaí SP", "dentista em Avaré SP", "psicólogo em Avaré SP"]


def test_dor_and_validation(nichos, tmp_path):
    assert nichos.dor_de("mecanicos") == "Orçamento, quem responde?" and nichos.dor_de(None) == ""
    bad = tmp_path / "b.yaml"
    bad.write_text(YAML.replace("[clinicas, mecanicos, corretores]", "[clinicas, voador]"), encoding="utf-8")
    with pytest.raises(ValueError, match="voador"):
        Nichos.load(bad)
    bad.write_text(YAML.replace("[clinicas, mecanicos, corretores]", "[alimentacao]"), encoding="utf-8")
    with pytest.raises(ValueError, match="buscas"):
        Nichos.load(bad)
    bad.write_text(YAML.replace("inicio: 2026-10-21", "inicio: amanhã"), encoding="utf-8")
    with pytest.raises(ValueError, match="inicio"):
        Nichos.load(bad)


def test_shipped_catalog_is_valid_and_restaurants_stay_tagged():
    cat = Nichos.load(ROOT / "nichos.yaml")
    assert cat.do_dia(date(2026, 10, 21)).id == "clinicas" and cat.do_dia(date(2026, 10, 22)).id == "mecanicos"
    assert cat.classify("pizzaria em Itaí SP", "Pizzaria") == "alimentacao"
    assert cat.classify("barbearia em Avaré SP", "Barbearia") == "saloes"
    assert cat.classify("x", "Escritório de advocacia") == "advogados"
    for nid in cat.ordem:
        assert cat.itens[nid].dor.endswith("?") and len(cat.itens[nid].dor.split()) <= 22
