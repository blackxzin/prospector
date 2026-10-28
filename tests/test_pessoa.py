import random

import pytest

from prospector.config import ROOT
from prospector.messages import Templates
from prospector.pessoa import individual_name, is_individual, singularize

from .conftest import TZ, local_dt

NOW = local_dt(2026, 10, 26, 9, 0)


@pytest.mark.parametrize("nome,expected", [
    ("Psicóloga Larissa Noveli", True), ("Nutricionista Maria Negrão", True), ("Dra.Thaina Cassu", True),
    ("Amanda Cassu- Psicóloga Clínica", True), ("Terapeuta Leila Campos", True),
    ("Clínica Barbosa Odontologia", False), ("Conceito Odontologia Piraju", False), ("Instituto Recovery | Clínica", False),
    ("Espaço Erus", False), ("Garrote Academia", False), ("Pizzaria Bella", False),
])
def test_is_individual(nome, expected):
    assert is_individual(nome) is expected


@pytest.mark.parametrize("nome,expected", [
    ("Psicóloga Larissa Noveli", "Larissa Noveli"), ("Dra.Thaina Cassu", "Dra. Thaina Cassu"),
    ("Psicóloga Josielen Ariane | Ansiedade", "Josielen Ariane"), ("Amanda Cassu- Psicóloga Clínica", "Amanda Cassu"),
])
def test_individual_name_drops_profession_keeps_doctor_title(nome, expected):
    assert individual_name(nome) == expected


def test_singularize_known_phrases():
    assert singularize("Vi o Instagram de vocês. Já pensaram em ter um site?") == "Vi o seu Instagram. Já pensou em ter um site?"
    assert singularize("Vi vocês no Google. Vocês têm site?") == "Vi você no Google. Você tem site?"


def test_every_variant_is_singular_for_a_solo_professional():
    tpl = Templates.load(ROOT / "templates.yaml")
    ctx_lead = {"nome": "Psicóloga Larissa Noveli", "cidade": "Avaré", "nota": 4.9, "avaliacoes": 120,
                "categoria": "Psicólogo", "site": "https://instagram.com/larissa", "site_problemas": "o site está fora do ar",
                "avaliacoes_json": '[{"text": "Atendimento excelente e muito acolhedor", "rating": 5}]', "dor": "Dor X?"}
    for offer, options in tpl.data["primeira"].items():
        if offer not in ("sem_site", "so_social", "tem_site", "site_problema"):
            continue
        for option in options:
            text = tpl._render([option], {**ctx_lead, "oferta": offer}, NOW, TZ, random.Random(0), chosen=option,
                               problema="o site está fora do ar")
            assert "vocês" not in text.lower() and "pensaram" not in text and "sabiam" not in text, text
            assert "Psicóloga Larissa" not in text, text


def test_team_business_keeps_plural():
    tpl = Templates.load(ROOT / "templates.yaml")
    text = tpl.first_message({"nome": "Clínica Sorriso", "oferta": "so_social", "site": "https://instagram.com/x"},
                             NOW, TZ, random.Random(1))
    assert "Clínica Sorriso" in text or "vocês" in text or "Fazemos" in text   # variante sem nome nem pronome também vale
    assert "você marca" not in text and "seu site" not in text


def test_singular_verbs_follow_the_pronoun_for_a_solo_professional():
    out = singularize("Vocês marcam horário? Quando estão atendendo, quem responde?")
    assert out == "Você marca horário? Quando está atendendo, quem responde?"


def test_hairdresser_misspelling_is_individual():
    assert is_individual("Marcelo Gonçalves Cabelereiro Avaré")
