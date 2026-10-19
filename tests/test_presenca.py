import random

import pytest

from prospector import db
from prospector import fila as fq
from prospector import httpclient as http
from prospector import presenca
from prospector.importer import import_rows
from prospector.presenca import PresencaErro, Resultado, Verificador

from .conftest import csv_row, local_dt

NOW = local_dt(2026, 10, 19, 8, 30)

DDG_HTML = """
<div class="result results_links results_links_deep web-result">
  <h2 class="result__title">
    <a rel="nofollow" class="result__a" href="https://www.instagram.com/pizzaria_antonia_/">Pizzaria Antônia - Instagram</a>
  </h2>
  <a class="result__url" href="https://www.instagram.com/pizzaria_antonia_/">www.instagram.com/pizzaria_antonia_/</a>
  <a class="result__snippet" href="https://www.instagram.com/pizzaria_antonia_/"><b>Pizzaria</b> em Itaí</a>
</div>
<div class="result">
  <h2 class="result__title"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexemplo.com.br%2Fx&rut=1">Outro</a></h2>
  <a class="result__url" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexemplo.com.br%2Fx&rut=1">exemplo.com.br/x</a>
</div>
"""


def _lead(conn, listas, **kw):
    import_rows(conn, [csv_row(**kw)], "busca", listas)
    return conn.execute("SELECT * FROM leads ORDER BY id DESC LIMIT 1").fetchone()


def _fixo(*resultados: Resultado):
    return lambda nome, cidade: list(resultados)


def _verificador(listas, buscar_fn):
    return Verificador(listas, buscar_fn=buscar_fn, sleep=lambda s: None, rng=random.Random(0))


def test_parse_reads_result_urls_titles_and_unwraps_redirects():
    res = presenca.parse_resultados(DDG_HTML)
    assert [r.url for r in res] == ["https://www.instagram.com/pizzaria_antonia_/", "https://exemplo.com.br/x"]
    assert "Pizzaria Antônia" in res[0].titulo and "Itaí" in res[0].trecho


def test_buscar_uses_fake_fetch_and_sends_name_and_city():
    seen = []
    res = presenca.buscar("Pizzaria Antônia", "Itaí", fetch=lambda q: seen.append(q) or DDG_HTML)
    assert seen == ["Pizzaria Antônia Itaí"]
    assert len(res) == 2


def test_buscar_raises_on_network_error():
    def boom(q):
        raise http.NetworkError("timeout")
    with pytest.raises(PresencaErro):
        presenca.buscar("X", "Itaí", fetch=boom)


def test_instagram_makes_lead_so_social(conn, listas):
    lead = _lead(conn, listas, title="Pizzaria Antônia")
    ig = Resultado("https://www.instagram.com/pizzaria_antonia_/", "Pizzaria Antônia (@pizzaria_antonia_)")
    assert _verificador(listas, _fixo(ig))(conn, lead) is True
    row = db.get_lead(conn, lead["id"])
    assert row["oferta"] == "so_social" and row["site"] == ig.url
    assert row["status"] == db.NOVO and row["presenca_checada_em"]


def test_anota_ai_with_city_discards_lead(conn, listas):
    lead = _lead(conn, listas, title="Pizzaria Antônia")
    anota = Resultado("https://pedido.anota.ai/loja/pizzaria-antonia", "Pizzaria Antônia - Itaí", "Peça já")
    _verificador(listas, _fixo(anota))(conn, lead)
    row = db.get_lead(conn, lead["id"])
    assert row["status"] == db.DESCARTADO
    assert row["motivo"] == "já usa cardápio online/robô (anota.ai)"


def test_robot_menu_without_city_only_annotates(conn, listas):
    lead = _lead(conn, listas, title="Armando Lanches")
    wm = Resultado("https://whatsmenu.com.br/armandolanches1", "Armando Lanches - WhatsMenu", "Faça seu pedido")
    _verificador(listas, _fixo(wm))(conn, lead)
    row = db.get_lead(conn, lead["id"])
    assert row["status"] == db.NOVO and row["oferta"] == "sem_site"
    assert "whatsmenu.com.br/armandolanches1" in row["motivo"]


def test_ifood_listing_does_not_discard(conn, listas):
    lead = _lead(conn, listas, title="Pizzaria Antônia")
    ifood = Resultado("https://www.ifood.com.br/delivery/itai-sp/pizzaria-antonia-centro/abc", "Pizzaria Antônia")
    _verificador(listas, _fixo(ifood))(conn, lead)
    row = db.get_lead(conn, lead["id"])
    assert row["status"] == db.NOVO and row["oferta"] == "sem_site"


def test_directory_is_ignored(conn, listas):
    lead = _lead(conn, listas, title="Big Pizza Magu")
    guru = Resultado("https://restaurantguru.com.br/BIG-PIZZA-MAGU-Itai", "Big Pizza Magu, Itaí")
    trip = Resultado("https://www.tripadvisor.com.br/Restaurant-bigpizzamagu", "Big Pizza Magu")
    _verificador(listas, _fixo(guru, trip))(conn, lead)
    row = db.get_lead(conn, lead["id"])
    assert row["oferta"] == "sem_site" and row["site"] is None and row["presenca_checada_em"]


def test_name_mismatch_changes_nothing(conn, listas):
    lead = _lead(conn, listas, title="Pizzaria Antônia")
    outros = (Resultado("https://www.instagram.com/bigpizzamagu/"),
              Resultado("https://anota.ai/bigpizzamagu", "Big Pizza Magu - Itaí"),
              Resultado("https://g1.globo.com/sp/pizzaria-antonia-itai"))
    _verificador(listas, _fixo(*outros))(conn, lead)
    row = db.get_lead(conn, lead["id"])
    assert row["status"] == db.NOVO and row["oferta"] == "sem_site" and row["site"] is None
    assert row["motivo"] is None and row["presenca_checada_em"]


def test_own_site_makes_lead_tem_site(conn, listas):
    lead = _lead(conn, listas, title="Pizzaria Antônia - Delivery")
    site = Resultado("https://www.pizzariaantonia.com.br/", "Pizzaria Antônia")
    ig = Resultado("https://www.instagram.com/pizzaria_antonia_/")
    _verificador(listas, _fixo(ig, site))(conn, lead)
    row = db.get_lead(conn, lead["id"])
    assert row["oferta"] == "tem_site" and row["site"] == site.url


def test_social_does_not_downgrade_lead_with_site(conn, listas):
    lead = _lead(conn, listas, title="Pizzaria Antônia", website="https://pizzariaantonia.com.br")
    _verificador(listas, _fixo(Resultado("https://www.instagram.com/pizzaria_antonia_/")))(conn, lead)
    row = db.get_lead(conn, lead["id"])
    assert row["oferta"] == "tem_site" and row["site"] == "https://pizzariaantonia.com.br"


def test_network_error_changes_nothing(conn, listas, caplog):
    lead = _lead(conn, listas, title="Pizzaria Antônia")

    def boom(q):
        raise http.NetworkError("bloqueado")
    ok = _verificador(listas, lambda n, c: presenca.buscar(n, c, fetch=boom))(conn, lead)
    row = db.get_lead(conn, lead["id"])
    assert ok is False
    assert row["status"] == db.NOVO and row["oferta"] == "sem_site" and row["presenca_checada_em"] is None
    assert "presença" in caplog.text


def test_stops_searching_after_repeated_blocks(conn, listas):
    chamadas = []

    def bloqueado(nome, cidade):
        chamadas.append(nome)
        raise PresencaErro("HTTP 202")
    v = _verificador(listas, bloqueado)
    for i in range(4):
        _lead(conn, listas, title=f"Lanchonete Numero {i}", phone=f"(14) 99123-000{i}")
    assert presenca.checar_leads(conn, v, limite=10).erros == 4
    assert len(chamadas) == presenca.MAX_FALHAS_SEGUIDAS


def test_pauses_between_searches(conn, listas):
    a = _lead(conn, listas, title="Pizzaria Antônia", phone="(14) 99123-0001")
    b = _lead(conn, listas, title="Big Pizza Magu", phone="(14) 99123-0002")
    pausas = []
    v = Verificador(listas, buscar_fn=_fixo(), sleep=pausas.append, rng=random.Random(0))
    v(conn, a)
    v(conn, b)
    assert len(pausas) == 1 and 1.0 <= pausas[0] <= 2.0


def test_checar_leads_skips_already_checked(conn, listas):
    _lead(conn, listas, title="Pizzaria Antônia", phone="(14) 99123-0001")
    _lead(conn, listas, title="Big Pizza Magu", phone="(14) 99123-0002")
    chamadas = []
    v = _verificador(listas, lambda n, c: chamadas.append(n) or [])
    assert presenca.checar_leads(conn, v, limite=10).checados == 2
    assert presenca.checar_leads(conn, v, limite=10).checados == 0
    assert len(chamadas) == 2


def test_build_queue_checks_presence_and_skips_discarded(conn, listas, templates, rules):
    _lead(conn, listas, title="Pizzaria Antônia", phone="(14) 99123-0001", review_count="500")
    _lead(conn, listas, title="Big Pizza Magu", phone="(14) 99123-0002", review_count="10")
    anota = Resultado("https://anota.ai/pizzariaantonia", "Pizzaria Antônia | Itaí")
    v = _verificador(listas, lambda n, c: [anota] if "Antônia" in n else [])
    res = fq.build_queue(conn, templates, rules, NOW, size=8, followup_days=3, rng=random.Random(1),
                         checar_presenca=v)
    assert res.primeiras == 1
    nomes = [i["nome"] for i in fq.list_items(conn)]
    assert nomes == ["Big Pizza Magu"]
    checados = conn.execute("SELECT COUNT(*) FROM leads WHERE presenca_checada_em IS NOT NULL").fetchone()[0]
    assert checados == 2


def test_build_queue_without_checker_does_not_search(conn, listas, templates, rules):
    _lead(conn, listas, title="Pizzaria Antônia")
    res = fq.build_queue(conn, templates, rules, NOW, size=8, followup_days=3, rng=random.Random(1))
    assert res.primeiras == 1
    assert db.get_lead(conn, 1)["presenca_checada_em"] is None


def test_setting_can_disable_check(settings, tmp_path):
    from prospector.config import load_settings
    assert settings.checar_presenca is True
    off = load_settings(env_file=tmp_path / "x.env", overrides={"DATA_DIR": str(tmp_path), "CHECAR_PRESENCA": "0"})
    assert off.checar_presenca is False
