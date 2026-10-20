from pathlib import Path

import pytest

from prospector import db, lote
from prospector.config import ROOT
from prospector.importer import ImportResult
from prospector.mundo import MundoConfig, Origem

YAML = """
max_por_execucao: 3
idiomas: {en: {prep: in}, es: {prep: en}, fr: {prep: à}}
nichos:
  en: [pizza restaurant, barber shop]
  es: [pizzería, barbería]
  fr: [pizzeria, barbier]
paises:
  ES: {nome: España, idioma: es, cidades: [{nome: Madrid, fuso: Europe/Madrid}, {nome: Sevilla, fuso: Europe/Madrid}]}
  US: {nome: United States, idioma: en, cidades: [{nome: "Austin, TX", fuso: America/Chicago}]}
  CA: {nome: Canada, idioma: en, cidades: [{nome: "Montréal, QC", fuso: America/Toronto, idioma: fr}]}
"""


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "mundo.yaml"
    p.write_text(YAML, encoding="utf-8")
    return MundoConfig.load(p)


def test_queries_use_local_language_and_are_interleaved(cfg):
    buscas = cfg.buscas()
    assert [b.consulta for b in buscas[:3]] == [
        "pizzería en Madrid, España", "pizza restaurant in Austin, TX, United States",
        "pizzeria à Montréal, QC, Canada"]
    assert buscas[0].origem == Origem("ES", "es", "Europe/Madrid", "Madrid")
    assert buscas[2].origem.idioma == "fr" and buscas[2].origem.pais == "CA"  # cidade francófona do Canadá
    assert len({b.consulta for b in buscas}) == len(buscas)


def test_filter_by_country(cfg):
    assert {b.origem.pais for b in cfg.buscas({"US"})} == {"US"}


def test_missing_language_or_bad_timezone_fails_early(tmp_path):
    bad = tmp_path / "b.yaml"
    bad.write_text(YAML.replace("fr: [pizzeria, barbier]", ""), encoding="utf-8")
    with pytest.raises(ValueError, match="fr"):
        MundoConfig.load(bad)
    bad.write_text(YAML.replace("Europe/Madrid", "Europe/Atlantis"), encoding="utf-8")
    with pytest.raises(Exception):
        MundoConfig.load(bad)


def test_world_batch_passes_origin_and_skips_done(conn, cfg):
    seen = []

    def search(query, origem):
        seen.append((query, origem.pais, origem.idioma))
        return ImportResult(lidos=1, novos=1, validos=1)

    first = lote.run_world(conn, cfg, search, out=lambda s: None)
    assert len(first) == 3 and seen[0] == ("pizzería en Madrid, España", "ES", "es")
    second = lote.run_world(conn, cfg, search, limit=2, out=lambda s: None)
    assert {q for q, *_ in seen[3:]}.isdisjoint({q for q, *_ in seen[:3]})  # nada repetido
    assert len(second) == 2


def test_world_batch_error_does_not_stop_the_rest(conn, cfg):
    def search(query, origem):
        if origem.pais == "US":
            raise RuntimeError("scraper caiu")
        return ImportResult()

    results = lote.run_world(conn, cfg, search, out=lambda s: None)
    assert [r.startswith("erro") for _, r in results] == [False, True, False]


def test_shipped_mundo_yaml_is_valid():
    cfg = MundoConfig.load(ROOT / "mundo.yaml")
    countries = {m[0] for m in cfg.mercados}
    assert {"US", "CA", "ES", "PT", "IT", "GB", "DE", "FR"} <= countries
    assert len(cfg.buscas()) > 500
