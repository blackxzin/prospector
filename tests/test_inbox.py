from datetime import timedelta

import pytest

from prospector import canais, db, inbox
from prospector.importer import import_rows

from .conftest import csv_row, local_dt

NOW = local_dt(2026, 10, 22, 10, 0)


def _sent_email(conn, listas, addr="dono@loja.com.br", phone="(14) 99111-2001"):
    import_rows(conn, [csv_row(title="Loja Bela", phone=phone, website="http://loja.com.br", emails=addr, link=addr)], "q", listas)
    lead = conn.execute("SELECT id FROM leads ORDER BY id DESC LIMIT 1").fetchone()
    with db.tx(conn):
        conn.execute("INSERT INTO canal_fila (lead_id, canal, destino, assunto, texto, status, criado_em, enviado_em) "
                     "VALUES (?, 'email', ?, 's', 't', 'enviado', ?, ?)", (lead["id"], addr, db.iso(NOW), db.iso(NOW)))
        db.update_lead(conn, lead["id"], status=db.ENVIADO, enviado_em=db.iso(NOW))
    return lead["id"]


def mail(sender="dono@loja.com.br", text="Oi, me interessei. Pode ligar amanhã?", subject="Re: ideia", mid="<1@x>"):
    return inbox.Mail(mid, sender, subject, text, NOW + timedelta(hours=1))


def run(conn, mails, alerts=None):
    alerts = alerts if alerts is not None else []
    return inbox.process(conn, mails, own_address="eu@gmail.com", now=NOW + timedelta(hours=2),
                         notify_fn=lambda t, b, u: alerts.append((t, u)))


def status(conn, lead_id):
    return conn.execute("SELECT status FROM leads WHERE id = ?", (lead_id,)).fetchone()[0]


def test_strip_quoted_removes_our_original_message():
    text = "Sim, me manda mais.\n\nEm qua., 21 de out. de 2026 às 10:00, Lucas <eu@gmail.com> escreveu:\n> Olá, tudo bem?\n> ..."
    assert inbox.strip_quoted(text) == "Sim, me manda mais."
    assert inbox.strip_quoted("> só citação\nok") == "ok"


def test_human_reply_marks_lead_and_alerts(conn, listas):
    lead = _sent_email(conn, listas)
    res = run(conn, [mail()])
    assert res.respostas == 1
    assert status(conn, lead) in (db.RESPONDEU, db.INTERESSADO)
    assert conn.execute("SELECT texto FROM respostas").fetchone()[0].startswith("[e-mail]")


def test_same_message_is_not_processed_twice(conn, listas):
    _sent_email(conn, listas)
    run(conn, [mail()])
    res = run(conn, [mail()])
    assert res.respostas == 0 and res.ignorados == 1


def test_opt_out_blocks_address_and_lead(conn, listas):
    lead = _sent_email(conn, listas)
    run(conn, [mail(text="parar")])
    assert status(conn, lead) == db.NAO_QUER
    assert canais.is_blocked(conn, "dono@loja.com.br")


def test_auto_reply_does_not_count_as_human(conn, listas):
    lead = _sent_email(conn, listas)
    run(conn, [mail(text="Obrigado pelo contato! Este é um e-mail automático, responderemos em breve.")])
    assert conn.execute("SELECT resposta_humana FROM leads WHERE id = ?", (lead,)).fetchone()[0] in (0, None)


def test_other_employee_at_same_domain_is_matched(conn, listas):
    lead = _sent_email(conn, listas)
    assert run(conn, [mail(sender="maria@loja.com.br")]).respostas == 1
    assert status(conn, lead) != db.ENVIADO


def test_unrelated_and_own_mail_ignored(conn, listas):
    _sent_email(conn, listas)
    res = run(conn, [mail(sender="spam@outro.com"), mail(sender="eu@gmail.com", mid="<2@x>")])
    assert res.respostas == 0 and res.ignorados == 2


def test_bounce_frees_lead_for_whatsapp_and_blocks_address(conn, listas):
    lead = _sent_email(conn, listas)
    bounce = mail(sender="mailer-daemon@googlemail.com", subject="Delivery Status Notification (Failure)",
                  text="A mensagem para dono@loja.com.br não foi entregue: endereço não encontrado", mid="<3@x>")
    res = run(conn, [bounce])
    assert res.bounces == 1 and status(conn, lead) == db.NOVO
    assert canais.is_blocked(conn, "dono@loja.com.br")


def test_threat_triggers_urgent_alert(conn, listas):
    _sent_email(conn, listas)
    alerts: list = []
    run(conn, [mail(text="Vou denunciar vocês por spam")], alerts)
    assert any(urgent for _, urgent in alerts)


def test_loop_stops_on_login_failure_but_survives_network_error():
    calls = []

    def fake_poll(*a, **k):
        calls.append(1)
        raise inbox.InboxError("IMAP: [AUTHENTICATIONFAILED] Invalid credentials" if len(calls) > 1 else "IMAP: timeout")

    import prospector.inbox as m
    orig = m.poll
    m.poll = fake_poll
    try:
        inbox.loop(None, inbox.ImapConfig("h", 993, "u", "p"), lambda *a: None, 1, out=lambda s: None, sleep=lambda s: None)
    finally:
        m.poll = orig
    assert len(calls) == 2
