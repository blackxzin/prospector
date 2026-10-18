from datetime import date

import pytest

from prospector import db
from prospector import finance as fin
from prospector import insights
from prospector.config import ROOT
from prospector.importer import import_rows
from prospector.replies import handle_inbound, is_closing, parse_inbound

from .conftest import csv_row, local_dt

HOJE = date(2026, 10, 19)


@pytest.mark.parametrize("raw,cents", [
    ("600", 600_00), ("1.500,90", 1500_90), ("1500,9", 1500_90), ("1500.50", 1500_50), ("R$ 2.000", 2000_00),
    ("0", 0), (150, 150_00), (99.9, 99_90),
])
def test_parse_money(raw, cents):
    assert fin.parse_money(raw) == cents


@pytest.mark.parametrize("raw", ["", "abc", "-10", "1,2,3", "12.34.5", "99999999999"])
def test_parse_money_rejects(raw):
    with pytest.raises(fin.FinanceError):
        fin.parse_money(raw)


def test_brl():
    assert fin.brl(1500_90) == "R$ 1.500,90"
    assert fin.brl(5) == "R$ 0,05"
    assert fin.brl(-12_00) == "-R$ 12,00"
    assert fin.brl_short(1250_00) == "R$ 1,2 mil"
    assert fin.brl_short(950_00) == "R$ 950"


def test_finance_config_loads_and_maps_offer():
    cfg = fin.FinanceConfig.load(ROOT / "financeiro.yaml")
    tipo, preco = cfg.defaults_for("so_social")
    assert tipo == "site" and preco.valor > 0
    assert cfg.defaults_for("tem_site")[0] == "automacao"


def _lead(conn, listas):
    import_rows(conn, [csv_row()], "pizzaria em Itaí SP", listas)
    lead = conn.execute("SELECT * FROM leads").fetchone()
    with db.tx(conn):
        db.update_lead(conn, lead["id"], status=db.INTERESSADO, enviado_em=db.iso(local_dt(2026, 10, 10, 10)),
                       respondeu_em=db.iso(local_dt(2026, 10, 12, 10)))
    return lead


def _project(conn, **kw):
    base = dict(cliente="Pizzaria Bella", tipo="site", valor=600_00, fechado_em=date(2026, 10, 5),
                prazo=date(2026, 10, 12), entrada_percentual=50)
    return fin.create_project(conn, fin.NovoProjeto(**{**base, **kw}))


def test_create_project_splits_payments_and_marks_client(conn, listas):
    lead = _lead(conn, listas)
    pid = _project(conn, lead_id=lead["id"], valor=601_00)
    pays = fin.payments(conn, pid)
    assert [(p["descricao"], p["valor_centavos"], p["vencimento"]) for p in pays] == [
        ("Entrada", 300_50, "2026-10-05"), ("Restante (na entrega)", 300_50, "2026-10-12")]
    assert sum(p["valor_centavos"] for p in pays) == 601_00
    assert db.get_lead(conn, lead["id"])["status"] == db.CLIENTE


def test_project_without_entry_has_single_payment(conn):
    pid = _project(conn, entrada_percentual=0)
    assert [p["descricao"] for p in fin.payments(conn, pid)] == ["Pagamento"]


@pytest.mark.parametrize("kw", [dict(cliente=" "), dict(valor=0), dict(tipo="x"), dict(prazo=date(2026, 10, 1)),
                                dict(entrada_percentual=120)])
def test_project_validation(conn, kw):
    with pytest.raises(fin.FinanceError):
        _project(conn, **kw)


def test_profit_and_month_numbers(conn):
    pid = _project(conn)
    first, second = fin.payments(conn, pid)
    fin.mark_paid(conn, first["id"], date(2026, 10, 6))
    fin.add_expense(conn, "domínio", 40_00, date(2026, 10, 7), "dominio", pid)
    fin.add_expense(conn, "ferramenta", 10_00, date(2026, 10, 8), "ferramenta")
    p = fin.project(conn, pid)
    assert (p["recebido"], p["a_receber"], p["despesas"], p["lucro"]) == (300_00, 300_00, 40_00, 260_00)
    r = insights.summary(conn, HOJE, meta=3000_00)
    assert r.recebido_mes == 300_00 and r.despesas_mes == 50_00 and r.lucro_mes == 250_00
    assert r.vencido == 300_00  # restante venceu dia 12
    assert r.meta_pct == 10


def test_cancel_removes_unpaid_installments(conn):
    pid = _project(conn)
    first, _ = fin.payments(conn, pid)
    fin.mark_paid(conn, first["id"], date(2026, 10, 5))
    fin.set_project_status(conn, pid, fin.CANCELADO, HOJE)
    assert [p["pago_em"] for p in fin.payments(conn, pid)] == ["2026-10-05"]


def test_monthly_fees_generated_once(conn):
    pid = _project(conn, tipo="automacao", mensalidade=150_00)
    fin.set_project_status(conn, pid, fin.ENTREGUE, date(2026, 8, 31))
    # entregue 31/08: vence 30/09 (gera) e 31/10 (só gera 7 dias antes, hoje é 19/10)
    assert fin.ensure_monthly_fees(conn, HOJE) == 1
    assert fin.ensure_monthly_fees(conn, HOJE) == 0
    assert fin.ensure_monthly_fees(conn, date(2026, 10, 24)) == 1
    comps = [p["competencia"] for p in fin.payments(conn, pid) if p["competencia"]]
    assert comps == ["2026-09", "2026-10"]
    assert insights.summary(conn, HOJE, 0).mrr == 150_00


def test_future_fees_forecast(conn):
    pid = _project(conn, tipo="automacao", mensalidade=100_00)
    fin.set_project_status(conn, pid, fin.ENTREGUE, date(2026, 10, 15))
    assert fin.future_fees(conn, HOJE, 30) == 100_00  # vence 15/11


# ---------- detecção de fechamento ----------

@pytest.mark.parametrize("text", ["Fechado! pode fazer", "qual o seu pix?", "como eu pago?", "vou querer",
                                  "Bora fazer então", "pode começar"])
def test_closing_detected(text):
    assert is_closing(text)


@pytest.mark.parametrize("text", ["quanto custa?", "não quero", "me manda o exemplo"])
def test_closing_not_detected(text):
    assert not is_closing(text)


def _reply(conn, text):
    payload = {"event": "message.received", "data": {"from": "5514991234567@s.whatsapp.net", "content": text,
                                                     "key": {"fromMe": False}}}
    return handle_inbound(conn, parse_inbound(payload), now=local_dt(2026, 10, 19, 10), in_business_hours=True,
                          notify_fn=lambda *a: None)


def test_closing_reply_flags_lead(conn, listas):
    lead = _lead(conn, listas)
    out = _reply(conn, "Fechado, pode fazer! Qual o pix?")
    assert out.closing and out.status == db.INTERESSADO
    assert db.get_lead(conn, lead["id"])["sinal_fechamento"] == 1
    alerts = insights.alerts(conn, HOJE)
    assert any(a.nivel == insights.BOM and "Possível fechamento" in a.titulo for a in alerts)


def test_client_saying_nao_is_not_blocked(conn, listas):
    lead = _lead(conn, listas)
    _project(conn, lead_id=lead["id"])
    out = _reply(conn, "não, tá tudo certo")
    assert out.status == db.CLIENTE
    assert not db.is_blocked(conn, {"5514991234567"})


# ---------- gerenciador ----------

def test_alerts_overdue_deadline_and_due_soon(conn):
    pid = _project(conn, prazo=date(2026, 10, 15))       # prazo estourado, restante vencido
    _project(conn, cliente="Lanches Zé", fechado_em=date(2026, 10, 19), prazo=date(2026, 10, 20))
    alerts = insights.alerts(conn, HOJE)
    titles = [a.titulo for a in alerts]
    assert any(t.startswith("Cobrar Pizzaria Bella") for t in titles)
    assert any(t.startswith("Prazo estourado") for t in titles)
    assert any(t.startswith("Entrega próxima: Lanches Zé") for t in titles)
    assert any(t.startswith("Pagamento vence hoje") for t in titles)
    assert all(a.link.startswith("/") for a in alerts)
    assert pid


def test_suggestions_goal_and_segments(conn, listas):
    rows = [csv_row(title=f"P{i}", phone=f"(14) 991{i:02d}-00{i:02d}", link=f"a{i}") for i in range(6)]
    rows += [csv_row(title=f"A{i}", phone=f"(14) 992{i:02d}-00{i:02d}", link=f"b{i}") for i in range(6)]
    import_rows(conn, rows[:6], "pizzaria em Itaí SP", listas)
    import_rows(conn, rows[6:], "academia em Itaí SP", listas)
    sent = db.iso(local_dt(2026, 10, 1, 10))
    with db.tx(conn):
        for lead in conn.execute("SELECT * FROM leads").fetchall():
            fields = {"status": db.ENVIADO, "enviado_em": sent}
            if lead["busca"].startswith("pizzaria") and lead["nome"] in ("P0", "P1", "P2"):
                fields.update(status=db.RESPONDEU, respondeu_em=sent)
            db.update_lead(conn, lead["id"], **fields)
    p0 = conn.execute("SELECT id FROM leads WHERE nome = 'P0'").fetchone()["id"]
    _project(conn, lead_id=p0, cliente="P0")
    cfg = fin.FinanceConfig.load(ROOT / "financeiro.yaml")
    painel = insights.build(conn, cfg, HOJE)
    text = " ".join(painel.sugestoes)
    assert "Faltam" in text and "meta" in text
    assert "pizzaria em Itaí SP" in text and "converte melhor" in text
    assert "academia em Itaí SP" in text  # responde pouco
    seg = {s["busca"]: s for s in painel.segmentos}
    assert seg["pizzaria em Itaí SP"]["clientes"] == 1
    assert seg["pizzaria em Itaí SP"]["faturado"] == 600_00


def test_monthly_series_six_months(conn):
    serie = fin.monthly_series(conn, HOJE)
    assert len(serie) == 6 and serie[-1]["mes"] == date(2026, 10, 1) and serie[0]["mes"] == date(2026, 5, 1)
