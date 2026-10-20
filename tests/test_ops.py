import time
from dataclasses import replace
from datetime import date, timedelta

import pytest

from prospector import checagem, db, metrics, regiao
from prospector.app import App
from prospector.charts import daily_chart, hbar_chart, heatmap
from prospector.importer import ImportResult, import_rows
from prospector.jobs import BUSCA, ENVIO, JobManager
from prospector.lote import BatchConfig

from .conftest import TZ, csv_row, local_dt

MUNICIPIOS = """codigo_ibge,nome,latitude,longitude,capital,codigo_uf,siafi_id,ddd,fuso_horario
3521804,Itaí,-23.4213,-49.092,0,35,6537,14,America/Sao_Paulo
3553302,Taquarituba,-23.5307,-49.2410,0,35,7213,14,America/Sao_Paulo
3504503,Avaré,-23.1067,-48.9251,0,35,6249,14,America/Sao_Paulo
3550308,São Paulo,-23.5329,-46.6395,1,35,7107,11,America/Sao_Paulo
4126504,Salto do Itararé,-23.6074,-49.6354,0,41,7895,43,America/Sao_Paulo
"""


@pytest.fixture
def dataset(tmp_path):
    p = tmp_path / "data" / "municipios.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(MUNICIPIOS, encoding="utf-8")
    return p


# ---------- região ----------

def test_nearby_cities_sorted_and_filtered(dataset):
    cs = regiao.load_dataset(dataset)
    base = regiao.find_city(cs, "itai", "sp")
    perto = regiao.nearby(cs, base, 60)
    assert [c.consulta for c in perto] == ["Itaí SP", "Taquarituba SP", "Avaré SP", "Salto do Itararé PR"]
    assert perto[1].distancia_km == pytest.approx(19.5, abs=0.5)
    assert [c.uf for c in regiao.nearby(cs, base, 60, mesma_uf=True)] == ["SP", "SP", "SP"]
    assert len(regiao.nearby(cs, base, 60, limite=2)) == 2


def test_find_city_errors(dataset):
    cs = regiao.load_dataset(dataset)
    with pytest.raises(regiao.RegiaoError, match="Parecidas"):
        regiao.find_city(cs, "Ita", "SP")
    with pytest.raises(regiao.RegiaoError):
        regiao.nearby(cs, cs[0], 0)


def test_dataset_download_failure(tmp_path):
    def boom(req, timeout):
        raise OSError("offline")
    with pytest.raises(regiao.RegiaoError, match="baixar"):
        regiao.load_dataset(tmp_path / "x.csv", fetch=boom)


# ---------- lote: salvar listas ----------

def test_batch_config_save_roundtrip(tmp_path):
    cfg = BatchConfig(("pizzaria",), ("Itaí SP",), 8).with_lists(["pizzaria", " barbearia ", "pizzaria", ""],
                                                                  ["Itaí SP", "Avaré SP"], 5)
    path = tmp_path / "b.yaml"
    cfg.save(path)
    again = BatchConfig.load(path)
    assert again.nichos == ("pizzaria", "barbearia") and again.cidades == ("Itaí SP", "Avaré SP")
    assert again.max_por_execucao == 5 and len(again.queries()) == 4
    with pytest.raises(ValueError):
        cfg.with_lists([], ["x"])


# ---------- tarefas em segundo plano ----------

def _app(settings, conn, tmp_path, **kw):
    app = App(replace(settings, db_path=tmp_path / "jobs.db", **kw))
    app.__dict__["conn"] = conn
    return app


def _wait(jobs, kind, timeout=5):
    end = time.monotonic() + timeout
    while jobs.is_running(kind) and time.monotonic() < end:
        time.sleep(0.02)


def test_job_busca_runs_and_records(settings, tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "jobs.db")
    app = _app(settings, conn, tmp_path)
    BatchConfig(("pizzaria", "academia"), ("Itaí SP",), 5).save(settings.buscas_path)
    monkeypatch.setattr(App, "search", lambda self, q, out=print: ImportResult(lidos=2, novos=2, validos=1))
    jobs = JobManager(app)
    assert jobs.start_busca(limit=5)
    _wait(jobs, BUSCA)
    st = jobs.status(BUSCA)
    assert st["status"] == "concluido" and "2/2 buscas ok" in st["resumo"]
    assert conn.execute("SELECT COUNT(*) FROM buscas WHERE status = 'ok'").fetchone()[0] == 2


def test_job_busca_can_be_paused(settings, tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "jobs.db")
    app = _app(settings, conn, tmp_path)
    BatchConfig(("a", "b", "c", "d"), ("Itaí SP",), 4).save(settings.buscas_path)
    jobs = JobManager(app)

    def slow(self, q, out=print):
        time.sleep(0.15)
        return ImportResult()
    monkeypatch.setattr(App, "search", slow)
    jobs.start_busca(limit=4)
    time.sleep(0.05)
    assert jobs.stop(BUSCA)
    _wait(jobs, BUSCA)
    assert jobs.status(BUSCA)["status"] == "pausado"
    assert conn.execute("SELECT COUNT(*) FROM buscas").fetchone()[0] < 4


def test_job_envio_without_whatsapp_becomes_error(settings, tmp_path):
    conn = db.connect(tmp_path / "jobs.db")
    jobs = JobManager(_app(settings, conn, tmp_path))
    assert jobs.start_envio()
    _wait(jobs, ENVIO)
    st = jobs.status(ENVIO)
    assert st["status"] == "erro" and "WAAKG" in st["resumo"]


def test_stale_running_job_marked_interrupted(settings, tmp_path):
    conn = db.connect(tmp_path / "jobs.db")
    app = _app(settings, conn, tmp_path)
    db.set_state(conn, "job_busca", '{"status": "rodando"}')
    assert JobManager(app).status(BUSCA)["status"] == "interrompido"


# ---------- checagem ----------

def test_checks_offline(settings, conn, tmp_path):
    app = _app(settings, conn, tmp_path)
    checks = {c.nome: c for c in checagem.run_checks(app, network=False)}
    assert checks["WA-AKG (WhatsApp)"].nivel == checagem.ERRO
    assert checks["Pix"].nivel == checagem.AVISO
    assert checks["templates.yaml"].nivel == checagem.OK
    assert "impedem" in checagem.summary(list(checks.values()))


def test_checks_network_reports_scraper_down(settings, conn, tmp_path):
    app = _app(settings, conn, tmp_path)  # GMAPS_URL aponta para uma porta fechada nos testes
    checks = {c.nome: c for c in checagem.run_checks(app)}
    assert checks["Scraper (Google Maps)"].nivel == checagem.ERRO


# ---------- métricas e gráficos ----------

def _contacted(conn, listas):
    rows = [csv_row(title=f"P{i}", phone=f"(14) 991{i:02d}-00{i:02d}", link=f"l{i}",
                    address=f"R. A, {i} - Centro, {'Itaí' if i < 3 else 'Avaré'} - SP") for i in range(5)]
    for i, r in enumerate(rows):
        import_rows(conn, [r], "pizzaria em Itaí SP" if i < 3 else "barbearia em Avaré SP", listas)
    sent = local_dt(2026, 10, 15, 10)
    with db.tx(conn):
        for lead in conn.execute("SELECT * FROM leads").fetchall():
            db.update_lead(conn, lead["id"], status=db.ENVIADO, enviado_em=db.iso(sent))
            db.log_send(conn, lead_id=lead["id"], fila_id=None, telefone=lead["telefone"], tipo="primeira",
                        texto="x", dry_run=False, ok=True, http_status=200, resposta_api="", when=sent)
        db.update_lead(conn, 1, status=db.RESPONDEU, respondeu_em=db.iso(local_dt(2026, 10, 15, 19, 30)),
                       resposta_humana=1)
        conn.execute("INSERT INTO respostas (lead_id, telefone, texto, recebido_em) VALUES (1, 'x', 'oi', ?)",
                     (db.iso(local_dt(2026, 10, 15, 19, 30)),))


def test_metrics(conn, listas, rules):
    _contacted(conn, listas)
    daily = metrics.daily(conn, date(2026, 10, 19), TZ, 7)
    assert len(daily) == 7 and sum(d["enviados"] for d in daily) == 5 and sum(d["respostas"] for d in daily) == 1
    cid = {r["nome"]: r for r in metrics.by_dimension(conn, "cidade")}
    assert cid["Itaí"]["contatados"] == 3 and cid["Itaí"]["responderam"] == 1
    nich = metrics.by_dimension(conn, "nicho")
    assert nich[0]["nome"] == "pizzaria"  # maior taxa primeiro
    grid = metrics.response_heatmap(conn, TZ)
    assert grid[3][metrics.HEAT_HOURS.index(19)] == 1  # quinta, 19h
    ops = metrics.today_ops(conn, rules, local_dt(2026, 10, 15, 16))
    assert ops["enviados_hoje"] == 5 and ops["pct_dia"] == 62 and ops["na_janela"]


def test_charts_render(conn, listas):
    _contacted(conn, listas)
    svg = daily_chart(metrics.daily(conn, date(2026, 10, 19), TZ, 10))
    assert svg.startswith("<svg") and "polyline" in svg and "<title>" in svg
    assert "<svg" in hbar_chart(metrics.by_dimension(conn, "cidade"), value="taxa")
    assert 'class="h5"' in heatmap(metrics.response_heatmap(conn, TZ), metrics.HEAT_HOURS)
    assert daily_chart([]).startswith("<svg")


# ---------- painel ----------

@pytest.fixture
def web(settings, conn, tmp_path, dataset):
    from prospector.web import create_app
    app = _app(settings, conn, tmp_path)
    BatchConfig(("pizzaria",), ("Itaí SP",), 8).save(settings.buscas_path)
    return create_app(app).test_client(), app


def test_dashboard_and_buscas_pages(web, listas):
    client, app = web
    _contacted(app.conn, listas)
    html = client.get("/").get_data(as_text=True)
    assert "Envios e respostas por dia" in html and "Quando os clientes respondem" in html
    assert "WA-AKG" in html  # prontidão aparece quando falta algo
    assert client.get("/buscas").status_code == 200
    assert "Fila e envio" in client.get("/fila").get_data(as_text=True)


def test_buscas_edit_lists_and_region(web):
    client, app = web
    client.post("/buscas/listas", data={"nichos": "pizzaria\nbarbearia", "cidades": "Itaí SP", "max": "4"})
    assert BatchConfig.load(app.settings.buscas_path).nichos == ("pizzaria", "barbearia")
    page = client.post("/buscas/regiao", data={"cidade": "Itaí", "uf": "SP", "raio": "30", "acao": "ver"})
    assert "Taquarituba SP" in page.get_data(as_text=True) and "Avaré" not in page.get_data(as_text=True)
    client.post("/buscas/regiao", data={"cidade": "Itaí", "uf": "SP", "raio": "50", "acao": "salvar",
                                         "modo": "adicionar"})
    assert BatchConfig.load(app.settings.buscas_path).cidades == ("Itaí SP", "Taquarituba SP", "Avaré SP")


def test_send_button_requires_whatsapp(web):
    client, _ = web
    resp = client.post("/tarefas/envio", follow_redirects=True)
    assert "WAAKG_SESSION" in resp.get_data(as_text=True)
