import random
import re
import smtplib

import pytest

from prospector import canais, db
from prospector.config import ROOT
from prospector.importer import import_rows
from prospector.sender import VirtualClock

from .conftest import TZ, csv_row, local_dt

MONDAY_9 = local_dt(2026, 10, 19, 9, 0)
CFG = canais.SmtpConfig("smtp.test", 587, "eu@gmail.com", "senha", "Lucas")


@pytest.fixture
def tpl():
    return canais.CanalTemplates.load(ROOT / "templates.canais.yaml")


def _import(conn, listas, **kw):
    import_rows(conn, [csv_row(**kw)], "q", listas)


def _build(conn, tpl, canal, limit=10):
    return canais.build(conn, canal, limit, tpl, MONDAY_9, TZ, random.Random(1))


@pytest.mark.parametrize("addr,ok", [
    ("contato@pizzaria.com.br", True), ("a.b+c@gmail.com", True), ("logo@2x.png", False),
    ("x@sentry.io", False), ("", False), (None, False), ("sem-arroba.com", False), ("atendimento@doctoralia.com.br", False),
    ("naoresponda@sus.gov.br", False),
])
def test_valid_email(addr, ok):
    assert canais.valid_email(addr) is ok


def test_templates_never_mention_city_or_price(tpl):
    lead = {"nome": "Pizzaria Bella - Centro", "oferta": "tem_site", "cidade": "Itaí", "site": ""}
    for canal in canais.CANAIS:
        for _ in range(20):
            subject, text = tpl.render(canal, lead, MONDAY_9, TZ)
            assert "Itaí" not in text + subject and "R$" not in text
            assert "Campinas e região" in text
            assert "{" not in text + subject


def test_email_text_has_optout_and_portfolio(tpl):
    _, text = tpl.render("email", {"nome": "X", "oferta": "tem_site"}, MONDAY_9, TZ)
    assert "parar" in text and "portifolio-dupla.vercel.app" in text


def test_templates_avoid_article_before_name_and_time_greeting():
    raw = (ROOT / "templates.canais.yaml").read_text(encoding="utf-8")
    body = raw.split("\nemail:", 1)[1]
    assert not re.search(r"\b(?:[ao]|d[ao]|n[ao])\s+\{nome\}", body, re.I)
    assert "{saudacao" not in body


def test_instagram_render_uses_default_when_offer_unknown(tpl):
    _, text = tpl.render("instagram", {"nome": "X", "oferta": "tem_site"}, MONDAY_9, TZ)
    assert text


def test_build_email_only_for_brazil_leads_with_valid_email(conn, listas, tpl):
    _import(conn, listas, title="Com Email", phone="(14) 99111-0001", website="http://a.com.br", emails="oi@a.com.br", link="1")
    _import(conn, listas, title="Sem Email", phone="(14) 99111-0002", website="http://b.com.br", link="2")
    _import(conn, listas, title="Lixo", phone="(14) 99111-0003", website="http://c.com.br", emails="x@sentry.io", link="3")
    res = _build(conn, tpl, canais.EMAIL)
    assert res.criados == 1
    item = canais.list_items(conn, canais.EMAIL)[0]
    assert item["destino"] == "oi@a.com.br" and item["assunto"]
    lead = conn.execute("SELECT status FROM leads WHERE id = ?", (item["lead_id"],)).fetchone()
    assert lead["status"] == db.NA_FILA


def test_build_is_idempotent_and_respects_limit(conn, listas, tpl):
    for i in range(4):
        _import(conn, listas, title=f"Loja {i}", phone=f"(14) 99111-00{i}0", website="http://x.com.br",
                emails=f"l{i}@x.com.br", link=str(i))
    assert _build(conn, tpl, canais.EMAIL, limit=2).criados == 2
    assert _build(conn, tpl, canais.EMAIL, limit=10).criados == 2
    assert _build(conn, tpl, canais.EMAIL, limit=10).criados == 0


def test_build_instagram_from_profile_link(conn, listas, tpl):
    _import(conn, listas, title="Doces da Ana", phone="(14) 99111-0004",
            website="https://instagram.com/docesdaana?igsh=1", link="4")
    assert _build(conn, tpl, canais.INSTAGRAM).criados == 1
    item = canais.list_items(conn, canais.INSTAGRAM)[0]
    assert item["destino"] == "@docesdaana" and item["assunto"] is None


def test_reject_returns_lead_to_novo_and_block_is_permanent(conn, listas, tpl):
    _import(conn, listas, title="Loja", phone="(14) 99111-0005", website="http://x.com.br", emails="a@x.com.br", link="5")
    _build(conn, tpl, canais.EMAIL)
    item = canais.list_items(conn, canais.EMAIL)[0]
    assert canais.reject(conn, [item["id"]]) == 1
    assert conn.execute("SELECT status FROM leads WHERE id = ?", (item["lead_id"],)).fetchone()[0] == db.NOVO
    canais.block(conn, "A@x.com.br", "parar", MONDAY_9)
    conn.execute("DELETE FROM canal_fila")
    assert _build(conn, tpl, canais.EMAIL).criados == 0


def test_lead_already_messaged_on_whatsapp_is_not_reused(conn, listas, tpl):
    _import(conn, listas, title="Loja", phone="(14) 99111-0006", website="http://x.com.br", emails="a@x.com.br", link="6")
    lead = conn.execute("SELECT id, telefone FROM leads").fetchone()
    with db.tx(conn):
        db.log_send(conn, lead_id=lead["id"], fila_id=None, telefone=lead["telefone"], tipo="primeira", texto="oi",
                    dry_run=False, ok=True, http_status=200, resposta_api="", when=MONDAY_9)
    assert _build(conn, tpl, canais.EMAIL).criados == 0


def _approved_emails(conn, listas, tpl, n):
    for i in range(n):
        _import(conn, listas, title=f"Loja {i}", phone=f"(14) 99111-01{i:02d}", website="http://x.com.br",
                emails=f"l{i}@x.com.br", link=f"e{i}")
    _build(conn, tpl, canais.EMAIL, limit=n)
    canais.approve(conn, canais.EMAIL)


def _send(conn, rules, clock, mailer, max_day=15, wait=False):
    return canais.send_emails(conn, CFG, rules, max_day, mailer=mailer, clock=clock, wait_next_day=wait,
                              rng=random.Random(3), out=lambda s: None)


def test_send_emails_spaces_messages_and_marks_sent(conn, listas, tpl, rules):
    _approved_emails(conn, listas, tpl, 3)
    sent: list[str] = []
    clock = VirtualClock(MONDAY_9)
    report = _send(conn, rules, clock, lambda cfg, to, subj, body: sent.append(to))
    assert report.enviados == 3 and sent == ["l0@x.com.br", "l1@x.com.br", "l2@x.com.br"]
    times = canais.email_times(conn, MONDAY_9.replace(hour=0))
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    assert all(g >= rules.interval_min_s for g in gaps)
    assert conn.execute("SELECT COUNT(*) FROM leads WHERE status = 'enviado'").fetchone()[0] == 3


def test_send_emails_stops_at_daily_limit(conn, listas, tpl, rules):
    _approved_emails(conn, listas, tpl, 4)
    sent: list[str] = []
    report = _send(conn, rules, VirtualClock(MONDAY_9), lambda c, to, s, b: sent.append(to), max_day=2)
    assert report.enviados == 2 and "Limite diário" in report.motivo_fim
    assert canais.list_items(conn, canais.EMAIL, (canais.APROVADO,)).__len__() == 2


def test_smtp_failure_stops_everything(conn, listas, tpl, rules):
    _approved_emails(conn, listas, tpl, 3)

    def boom(cfg, to, subject, body):
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    report = _send(conn, rules, VirtualClock(MONDAY_9), boom)
    assert report.enviados == 0 and report.erros == 1 and report.parado
    assert db.stop_reason(conn)


def test_send_test_goes_only_to_given_address_and_keeps_queue(conn, listas, tpl):
    _approved_emails(conn, listas, tpl, 2)
    sent: list[tuple[str, str]] = []
    subject, _ = canais.send_test(conn, CFG, "eu@gmail.com", tpl, MONDAY_9, TZ,
                                  mailer=lambda c, to, s, b: sent.append((to, s)))
    assert sent == [("eu@gmail.com", subject)] and subject.startswith("[TESTE] ")
    assert len(canais.list_items(conn, canais.EMAIL, (canais.APROVADO,))) == 2
    assert conn.execute("SELECT COUNT(*) FROM leads WHERE status = 'enviado'").fetchone()[0] == 0


def test_send_test_without_queue_uses_sample_and_smtp_error_stops(conn, tpl):
    sent: list[str] = []
    canais.send_test(conn, CFG, "eu@gmail.com", tpl, MONDAY_9, TZ, mailer=lambda c, to, s, b: sent.append(b))
    assert "Campinas e região" in sent[0]

    def boom(cfg, to, subject, body):
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    with pytest.raises(canais.CanalError):
        canais.send_test(conn, CFG, "eu@gmail.com", tpl, MONDAY_9, TZ, mailer=boom)
    assert db.stop_reason(conn)


def test_send_refuses_when_system_stopped(conn, listas, tpl, rules):
    _approved_emails(conn, listas, tpl, 1)
    db.set_state(conn, db.PARADO, "teste")
    sent: list[str] = []
    report = _send(conn, rules, VirtualClock(MONDAY_9), lambda c, to, s, b: sent.append(to))
    assert sent == [] and report.parado


def test_blocked_destination_is_not_emailed(conn, listas, tpl, rules):
    _approved_emails(conn, listas, tpl, 1)
    canais.block(conn, "l0@x.com.br", "parar", MONDAY_9)
    sent: list[str] = []
    _send(conn, rules, VirtualClock(MONDAY_9), lambda c, to, s, b: sent.append(to))
    assert sent == []


def test_mark_sent_is_idempotent_for_instagram(conn, listas, tpl):
    _import(conn, listas, title="Doces", phone="(14) 99111-0007", website="https://instagram.com/doces", link="7")
    _build(conn, tpl, canais.INSTAGRAM)
    item = canais.list_items(conn, canais.INSTAGRAM)[0]
    assert canais.mark_sent(conn, item["id"], MONDAY_9) is True
    assert canais.mark_sent(conn, item["id"], MONDAY_9) is False


def test_canais_page_and_instagram_flow(settings, tmp_path):
    from prospector.app import App
    from prospector.web import create_app

    app = App(settings)
    from prospector.classify import Listas
    import_rows(app.conn, [csv_row(title="Doces da Ana", phone="(14) 99111-0008",
                                   website="https://instagram.com/docesdaana", link="8")], "q", Listas.load(ROOT / "listas.yaml"))
    web = create_app(app).test_client()
    assert web.post("/canais/instagram/montar").status_code == 302
    page = web.get("/canais")
    assert page.status_code == 200 and b"@docesdaana" in page.data
    item = canais.list_items(app.conn, canais.INSTAGRAM)[0]
    assert web.post(f"/canais/item/{item['id']}/enviado").status_code == 302
    assert canais.list_items(app.conn, canais.INSTAGRAM) == []
    assert app.conn.execute("SELECT status FROM leads").fetchone()[0] == db.ENVIADO
