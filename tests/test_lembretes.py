from datetime import timedelta

from prospector import db, lembretes
from prospector.canais import CanalTemplates
from prospector.config import ROOT
from prospector.importer import import_rows

from .conftest import csv_row, local_dt

NOW = local_dt(2026, 10, 27, 10, 0)   # terça


def _lead(conn, listas, status=db.INTERESSADO, name="Pizzaria Bella", phone="(14) 99111-3001", replied=NOW, text="Pode mandar"):
    import_rows(conn, [csv_row(title=name, phone=phone, link=phone)], "q", listas)
    lead = conn.execute("SELECT id FROM leads ORDER BY id DESC LIMIT 1").fetchone()
    with db.tx(conn):
        db.update_lead(conn, lead["id"], status=status, respondeu_em=db.iso(replied), resposta=text, resposta_humana=1)
    return lead["id"]


def test_interested_lead_gets_default_reminder_two_business_days_later(conn, listas, rules):
    lead = _lead(conn, listas)
    assert lembretes.ensure_defaults(conn, NOW, rules) == 1
    row = conn.execute("SELECT proximo_contato_em FROM leads WHERE id = ?", (lead,)).fetchone()
    assert db.parse_iso(row[0]).astimezone(NOW.tzinfo).date().isoformat() == "2026-10-29"
    assert lembretes.ensure_defaults(conn, NOW, rules) == 0   # não duplica


def test_plain_replies_do_not_get_automatic_reminders(conn, listas, rules):
    _lead(conn, listas, status=db.RESPONDEU, text="Obrigado!")
    assert lembretes.ensure_defaults(conn, NOW, rules) == 0


def test_old_conversations_do_not_flood(conn, listas, rules):
    _lead(conn, listas, replied=NOW - timedelta(days=40))
    assert lembretes.ensure_defaults(conn, NOW, rules) == 0


def test_due_and_upcoming_split_by_date(conn, listas, rules):
    a = _lead(conn, listas, phone="(14) 99111-3002")
    b = _lead(conn, listas, phone="(14) 99111-3003")
    lembretes.schedule(conn, a, NOW - timedelta(hours=1), "ver exemplo")
    lembretes.schedule(conn, b, NOW + timedelta(days=2))
    assert [r["id"] for r in lembretes.due(conn, NOW)] == [a]
    assert [r["id"] for r in lembretes.upcoming(conn, NOW)] == [b]


def test_done_snooze_and_finish(conn, listas, rules):
    lead = _lead(conn, listas)
    lembretes.schedule(conn, lead, NOW - timedelta(hours=1), "nota")
    lembretes.snooze(conn, lead, NOW, rules, 2)
    row = conn.execute("SELECT proximo_contato_em, proximo_contato_nota FROM leads WHERE id = ?", (lead,)).fetchone()
    assert db.parse_iso(row[0]) > NOW and row[1] == "nota"
    lembretes.finish(conn, lead)
    assert lembretes.due(conn, NOW + timedelta(days=30)) == []
    assert lembretes.ensure_defaults(conn, NOW, rules) == 0   # encerrado não volta sozinho


def test_lead_that_opted_out_or_became_client_is_not_listed(conn, listas, rules):
    lead = _lead(conn, listas)
    lembretes.schedule(conn, lead, NOW - timedelta(hours=1))
    with db.tx(conn):
        db.update_lead(conn, lead, status=db.NAO_QUER)
    assert lembretes.due(conn, NOW) == []


def test_suggestion_uses_contact_name_and_singular_for_solo_professional(conn, listas):
    data = CanalTemplates.load(ROOT / "templates.canais.yaml").data
    lead = _lead(conn, listas, name="Psicóloga Ana Souza", phone="(14) 99111-3004")
    with db.tx(conn):
        db.update_lead(conn, lead, contato_nome="Juliana")
    row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead,)).fetchone()
    assert "{" not in lembretes.suggestion(data, row)
    assert ", Juliana" in lembretes.suggestion(data, row)


def test_notify_due_once_per_day(conn, listas, rules):
    lead = _lead(conn, listas)
    lembretes.schedule(conn, lead, NOW - timedelta(hours=1))
    sent: list = []
    assert lembretes.notify_due(conn, NOW, lambda t, b, u: sent.append(t), rules) == 1
    assert lembretes.notify_due(conn, NOW + timedelta(hours=1), lambda t, b, u: sent.append(t), rules) == 0
    assert len(sent) == 1


def test_followup_page_and_actions(settings):
    from prospector.app import App
    from prospector.classify import Listas
    from prospector.web import create_app

    app = App(settings)
    import_rows(app.conn, [csv_row(title="Loja X", phone="(14) 99111-3005", link="x")], "q", Listas.load(ROOT / "listas.yaml"))
    lead = app.conn.execute("SELECT id FROM leads").fetchone()["id"]
    with db.tx(app.conn):
        db.update_lead(app.conn, lead, status=db.INTERESSADO)
    lembretes.schedule(app.conn, lead, db.utcnow() - timedelta(hours=1), "ver exemplo")
    web = create_app(app).test_client()
    page = web.get("/lembretes")
    assert page.status_code == 200 and b"Loja X" in page.data and b"Retomei" in page.data
    assert web.post(f"/lembretes/{lead}/feito").status_code == 302
    assert lembretes.due(app.conn, db.utcnow()) == []
    assert web.post(f"/lembretes/{lead}/encerrar").status_code == 302


def test_own_text_replaces_generic_suggestion(conn, listas, rules):
    data = CanalTemplates.load(ROOT / "templates.canais.yaml").data
    lead = _lead(conn, listas, phone="(14) 99111-3006")
    lembretes.schedule(conn, lead, NOW - timedelta(hours=1), "nota", "Oi! Conseguiu repassar minha mensagem?")
    row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead,)).fetchone()
    assert lembretes.suggestion(data, row) == "Oi! Conseguiu repassar minha mensagem?"
    # reagendar sem texto novo mantém o texto próprio; reagendar com texto vazio volta à sugestão genérica
    lembretes.snooze(conn, lead, NOW, rules, 2)
    assert conn.execute("SELECT proximo_contato_texto FROM leads WHERE id = ?", (lead,)).fetchone()[0]
    lembretes.schedule(conn, lead, NOW, "nota", "")
    row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead,)).fetchone()
    assert lembretes.suggestion(data, row) != "Oi! Conseguiu repassar minha mensagem?"
