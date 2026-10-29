import random
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from prospector import backup, db, lote, pix, proposta, relatorio, sitecheck
from prospector import fila as fq
from prospector import finance as fin
from prospector.app import App
from prospector.classify import SITE_PROBLEMA
from prospector.importer import ImportResult, import_rows
from prospector.messages import choose_variant, variant_id
from prospector.pdf import wrap
from prospector.sender import VirtualClock, run

from .conftest import TZ, csv_row, local_dt

HOJE = date(2026, 10, 19)


# ---------- 4. backup ----------

def test_backup_copies_checks_and_rotates(conn, tmp_path):
    conn.execute("INSERT INTO estado (chave, valor, atualizado_em) VALUES ('x', 'y', 'z')")
    conn.commit()
    dest = tmp_path / "bk"
    for i in range(4):
        res = backup.make_backup(conn, dest, keep=2, now=datetime(2026, 10, 19, 10, i))
    assert len(backup.list_backups(dest)) == 2 and len(res.removed) == 1
    copy = sqlite3.connect(res.path / "prospector.db")
    assert copy.execute("SELECT valor FROM estado WHERE chave = 'x'").fetchone()[0] == "y"
    assert backup.last_backup_time(dest) == datetime(2026, 10, 19, 10, 3)


def test_backup_alert():
    from prospector import insights
    now = datetime(2026, 10, 19, 10)
    assert insights.backup_alert(None, now).titulo.startswith("Nenhum backup")
    assert insights.backup_alert(now - timedelta(days=3), now) is not None
    assert insights.backup_alert(now - timedelta(hours=5), now) is None


# ---------- 2. Pix ----------

def test_pix_matches_banco_central_example():
    code = pix.payload("123e4567-e12b-12d1-a456-426655440000", "Fulano de Tal", "BRASILIA")
    assert code == ("00020126580014br.gov.bcb.pix0136123e4567-e12b-12d1-a456-4266554400005204000053039865802BR"
                    "5913Fulano de Tal6008BRASILIA62070503***63041D3D")


def test_pix_with_amount_and_accents():
    code = pix.payload("lucas@exemplo.com", "Lucas Gabriel da Conceição Silva", "São Paulo", 300_50, txid="P-12")
    assert "5406300.50" in code and "5925Lucas Gabriel da Conceica" in code and "6009Sao Paulo" in code
    assert "0503P12" in code
    assert code[-4:] == pix.crc16(code[:-4])


@pytest.mark.parametrize("args", [("", "N", "C"), ("k", "", "C"), ("k", "N", "")])
def test_pix_requires_config(args):
    with pytest.raises(pix.PixError):
        pix.payload(*args)


def test_qr_svg():
    svg = pix.qr_svg("teste")
    assert svg.startswith("<svg") and "<rect" in svg


# ---------- 2b. cobrança na fila ----------

def _client_with_overdue(conn, listas):
    import_rows(conn, [csv_row()], "q", listas)
    lead = conn.execute("SELECT * FROM leads").fetchone()
    pid = fin.create_project(conn, fin.NovoProjeto(cliente="Bella", tipo="site", valor=600_00,
                                                   fechado_em=date(2026, 10, 1), prazo=date(2026, 10, 8),
                                                   lead_id=lead["id"]))
    return lead, pid


def test_charges_queued_once_and_limited(conn, listas, templates, rules):
    _client_with_overdue(conn, listas)
    text = lambda lead, pay: f"cobrança {pay['descricao']}"  # noqa: E731
    now = local_dt(2026, 10, 19, 10)
    res = fq.build_queue(conn, templates, rules, now, size=8, followup_days=3, charge_text=text)
    assert res.cobrancas == 2  # entrada (01/10) e restante (08/10) vencidos
    again = fq.build_queue(conn, templates, rules, now, size=8, followup_days=3, charge_text=text)
    assert again.cobrancas == 0  # já tem item aberto
    items = fq.list_items(conn)
    assert {i["tipo"] for i in items} == {fq.COBRANCA}


def test_charge_sent_updates_payment_and_skips_if_paid(conn, listas, templates, rules):
    lead, pid = _client_with_overdue(conn, listas)
    fq.build_queue(conn, templates, rules, local_dt(2026, 10, 19, 8), size=8, followup_days=3,
                   charge_text=lambda l, p: f"cobrança {p['valor_centavos']}")
    fq.approve(conn, fq.pending_ids(conn))
    first, second = fin.payments(conn, pid)
    fin.mark_paid(conn, second["id"], date(2026, 10, 19))  # pagou antes do envio

    class Wa:
        sent = []
        def check_number(self, n): return f"{n}@s.whatsapp.net"
        def presence(self, j, s): pass
        def send_text(self, j, t):
            self.sent.append(t)
            return type("R", (), {"status": 200, "body": "{}"})()
    wa = Wa()
    report = run(conn, wa, rules, typing_min_s=5, typing_max_s=20, dry_run=False,
                 clock=VirtualClock(local_dt(2026, 10, 19, 9)), notify_enabled=False, out=lambda s: None)
    assert report.enviados == 1 and report.pulados == 1
    row = conn.execute("SELECT cobrancas, ultima_cobranca FROM pagamentos WHERE id = ?", (first["id"],)).fetchone()
    assert row["cobrancas"] == 1 and row["ultima_cobranca"]
    # cobrança não consome o limite de prospecção
    assert db.real_send_times(conn, local_dt(2026, 10, 19, 0)) == []


def test_app_charge_text_has_pix(settings, conn, listas):
    app = App(replace(settings, pix_chave="lucas@exemplo.com", pix_nome="Lucas", pix_cidade="Itai"))
    app.__dict__["conn"] = conn
    lead, pid = _client_with_overdue(conn, listas)
    pay = fin.payments(conn, pid)[0]
    text = app.charge_text(lead, pay)
    assert "R$ 300,00" in text and "br.gov.bcb.pix" in text and "01/10" in text


# ---------- 3. teste A/B ----------

def test_variant_id_stable():
    assert variant_id("abc") == variant_id(" abc ") and len(variant_id("abc")) == 8


def test_thompson_prefers_winner():
    opts = ["A", "B", "C"]
    stats = {variant_id("A"): (40, 20), variant_id("B"): (40, 2), variant_id("C"): (40, 1)}
    rng = random.Random(1)
    picks = [choose_variant(opts, stats, rng) for _ in range(300)]
    assert picks.count("A") > 250
    uniform = [choose_variant(opts, {}, rng) for _ in range(300)]
    assert all(uniform.count(o) > 60 for o in opts)  # sem dados: explora todas


def test_queue_records_variant_and_stats(conn, listas, templates, rules):
    import_rows(conn, [csv_row(), csv_row(phone="(14) 99888-7777", link="x")], "q", listas)
    fq.build_queue(conn, templates, rules, local_dt(2026, 10, 19, 8), size=5, followup_days=3)
    items = fq.list_items(conn)
    assert all(i["variante"] for i in items)
    with db.tx(conn):
        for i in items:
            db.update_lead(conn, i["lead_id"], enviado_em=db.iso(local_dt(2026, 10, 19, 10)), variante=i["variante"])
        db.update_lead(conn, items[0]["lead_id"], respondeu_em=db.iso(local_dt(2026, 10, 19, 11)),
                       status=db.RESPONDEU, resposta_humana=1)
    stats = fq.ab_stats(conn)
    assert sum(v[0] for v in stats.values()) == 2 and sum(v[1] for v in stats.values()) == 1


# ---------- 5. checagem de site ----------

def _fake(status=200, final="https://x.com/", body=b'<meta name="viewport" content="width=device-width">', secs=0.5):
    return lambda url: (status, final, body, secs)


def test_site_ok():
    assert sitecheck.check_site("https://x.com", fetch=_fake()).ok


@pytest.mark.parametrize("fake,expected", [
    (_fake(status=503), "fora do ar"),
    (_fake(final="http://x.com/"), "HTTPS"),
    (_fake(secs=7.2), "demora"),
    (_fake(body=b"<html><head></head></html>"), "celular"),
])
def test_site_problems(fake, expected):
    report = sitecheck.check_site("x.com", fetch=fake)
    assert not report.ok and expected in report.summary()


def test_site_down_on_connection_error():
    def boom(url):
        raise OSError("refused")
    assert "fora do ar" in sitecheck.check_site("x.com", fetch=boom).summary()


def test_invalid_redirect_url_does_not_blame_the_site():
    def bad(url):
        raise ValueError("URL can't contain control characters")   # redireciona para wa.me/send?text=com espaço

    assert sitecheck.check_site("x.com", fetch=bad).ok


def test_ssrf_blocked_for_private_addresses():
    for host in ("localhost", "127.0.0.1", "10.0.0.5", "192.168.0.1"):
        with pytest.raises(sitecheck.UnsafeUrl):
            sitecheck._check_host(host)
    # não acusa problema em URL que não dá para checar com segurança
    assert sitecheck.check_site("http://127.0.0.1:8080/").ok


def test_check_leads_switches_offer(conn, listas, templates):
    import_rows(conn, [csv_row(website="https://bella.com.br")], "q", listas)
    checked, bad = sitecheck.check_leads(
        conn, check=lambda url: sitecheck.SiteReport(url, ("o site está fora do ar",)), out=lambda s: None)
    lead = conn.execute("SELECT * FROM leads").fetchone()
    assert (checked, bad, lead["oferta"]) == (1, 1, SITE_PROBLEMA)
    text = templates.first_message(dict(lead), local_dt(2026, 10, 19, 9), TZ, random.Random(0))
    assert "o site está fora do ar" in text
    assert sitecheck.check_leads(conn, out=lambda s: None) == (0, 0)  # não checa de novo


# ---------- 6. buscas em lote ----------

def test_batch_runs_pending_once_and_survives_errors(conn, tmp_path):
    cfg_path = tmp_path / "buscas.yaml"
    cfg_path.write_text("nichos: [pizzaria, academia]\ncidades: [Itaí SP]\nmax_por_execucao: 5\n", encoding="utf-8")
    cfg = lote.BatchConfig.load(cfg_path)
    assert cfg.queries() == ["pizzaria em Itaí SP", "academia em Itaí SP"]

    def search(q):
        if q.startswith("academia"):
            raise RuntimeError("scraper caiu")
        return ImportResult(lidos=3, novos=3, validos=2)
    results = lote.run_batch(conn, cfg, search, out=lambda s: None)
    assert [r[1].startswith("erro") for r in results] == [False, True]
    assert lote.pending(conn, cfg) == ["academia em Itaí SP"]  # com erro: tenta de novo depois


def test_batch_counts_old_manual_searches_as_done(conn, listas, tmp_path):
    import_rows(conn, [csv_row()], "pizzaria em Itaí SP", listas)
    cfg_path = tmp_path / "b.yaml"
    cfg_path.write_text("nichos: [pizzaria]\ncidades: [Itaí SP]\n", encoding="utf-8")
    assert lote.pending(conn, lote.BatchConfig.load(cfg_path)) == []


# ---------- 7. proposta PDF ----------

def test_proposal_pdf_is_valid(tmp_path):
    proj = {"id": 3, "cliente": "Pizzaria Bella (Itaí) \\ teste", "tipo": "site", "descricao": "Site com cardápio",
            "valor_centavos": 600_00, "mensalidade_centavos": 50_00, "prazo": "2026-10-26", "fechado_em": "2026-10-19"}
    data = proposta.build(proj, autor="Lucas, desenvolvedor", inclui=("Item 1", "Item 2"), entrada_percentual=50,
                          pix_info=proposta.PixInfo("lucas@x.com", "Lucas", "Itai"), today=HOJE)
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    assert b"Pizzaria Bella \\(Ita" in data and b"R$ 600,00" in data
    assert b"startxref" in data
    xref = int(data.rsplit(b"startxref\n", 1)[1].split(b"\n")[0])
    assert data[xref:xref + 4] == b"xref"


def test_wrap_breaks_long_tokens():
    lines = wrap("a" * 300, 8, 100)
    assert len(lines) > 3 and all(len(line) <= 300 for line in lines)


# ---------- 8. relatório ----------

def test_weekly_report(conn, listas):
    import_rows(conn, [csv_row(), csv_row(phone="(14) 99888-7777", link="x")], "pizzaria em Itaí SP", listas)
    with db.tx(conn):
        db.update_lead(conn, 1, enviado_em=db.iso(local_dt(2026, 10, 15, 10)), status=db.RESPONDEU,
                       respondeu_em=db.iso(local_dt(2026, 10, 16, 10)))
        db.update_lead(conn, 2, enviado_em=db.iso(local_dt(2026, 10, 1, 10)), status=db.ENVIADO)  # fora da semana
    pid = fin.create_project(conn, fin.NovoProjeto(cliente="Bella", tipo="site", valor=600_00,
                                                   fechado_em=date(2026, 10, 17), entrada_percentual=100))
    fin.mark_paid(conn, fin.payments(conn, pid)[0]["id"], date(2026, 10, 17))
    rel = relatorio.build(conn, HOJE, TZ)
    assert (rel.enviados, rel.responderam, rel.fechados, rel.recebido) == (1, 1, 1, 600_00)
    md = rel.markdown()
    assert "13/10 a 19/10/2026" in md and "R$ 600,00" in md


# ---------- painel ----------

@pytest.fixture
def web(settings, conn, tmp_path):
    from prospector.web import create_app
    app = App(replace(settings, backup_dir=tmp_path / "bk",
                      pix_chave="lucas@x.com", pix_nome="Lucas", pix_cidade="Itai"))
    app.__dict__["conn"] = conn
    flask_app = create_app(app)
    return flask_app.test_client(), app


def test_panel_pix_and_proposal(web, listas):
    client, app = web
    lead, pid = _client_with_overdue(app.conn, listas)
    page = client.get(f"/projetos/{pid}").get_data(as_text=True)
    assert "br.gov.bcb.pix" in page and "<svg" in page and "Proposta (PDF)" in page
    pdf = client.get(f"/projetos/{pid}/proposta.pdf")
    assert pdf.status_code == 200 and pdf.mimetype == "application/pdf" and pdf.data.startswith(b"%PDF")
    assert client.get("/demos/x/").status_code == 404  # recurso removido
    dash = client.get("/").get_data(as_text=True)
    assert "Nenhum backup do banco ainda" in dash and "Textos que mais funcionam" in dash



def test_site_check_skips_foreign_leads(conn, listas):
    from prospector import sitecheck
    from prospector.importer import import_rows
    from prospector.mundo import Origem
    rows = [csv_row(title="Café Luna", phone="612 34 56 79", website="https://luna.example", link="m1")]
    import_rows(conn, rows, "q", listas, Origem("ES", "es", "Europe/Madrid", "Madrid"))
    calls = []
    sitecheck.check_leads(conn, check=lambda url: calls.append(url) or sitecheck.SiteReport("x", ()), out=lambda s: None)
    assert calls == []
