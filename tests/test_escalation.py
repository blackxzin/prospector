from datetime import date

import pytest

from prospector import db, insights
from prospector.importer import import_rows
from prospector.replies import handle_inbound, is_escalation, parse_inbound

from .conftest import csv_row, local_dt

NUMBER = "5514991234567"


@pytest.mark.parametrize("text", [
    "Vou chamar meu advogado se continuarem me mandando isso",
    "Vou denunciar vocês por spam",
    "Isso é assédio, já registrei no Procon",
    "Vou fazer boletim de ocorrência",
    "This is spam, I'll report you to the authorities",
    "I'm calling my lawyer. This violates GDPR.",
    "We will take legal action",
    "Esto es acoso, voy a denunciarlos a la policía",
    "Esto incumple el RGPD y la protección de datos",
    "Je vais contacter mon avocat, c'est du harcèlement",
    "Das verstößt gegen die DSGVO, ich zeige Sie an",
    "Sono un avvocato, questo è illegale: ci rivolgeremo al Garante della privacy",
    "Cease and desist",
])
def test_legal_and_angry_messages_are_escalations(text):
    assert is_escalation(text)


@pytest.mark.parametrize("text", [
    "Pode mandar o exemplo", "Quanto custa?", "Não tenho interesse, obrigado", "stop", "Sí, mándame el ejemplo",
    "Oi, tudo bem? Estamos fechados agora", "Ja, gerne", "Mi interessa, grazie",
])
def test_ordinary_replies_are_not_escalations(text):
    assert not is_escalation(text)


def _lead(conn, listas):
    import_rows(conn, [csv_row(phone="(14) 99123-4567")], "q", listas)
    lead = conn.execute("SELECT * FROM leads").fetchone()
    with db.tx(conn):
        db.update_lead(conn, lead["id"], status=db.ENVIADO, enviado_em=db.iso(local_dt(2026, 10, 19, 10)))
    return lead["id"]


def _handle(conn, text, in_hours=True):
    notes = []
    msg = parse_inbound({"event": "message.received",
                         "data": {"key": {"remoteJid": f"{NUMBER}@s.whatsapp.net"}, "content": text}})
    out = handle_inbound(conn, msg, now=local_dt(2026, 10, 20, 9), in_business_hours=in_hours,
                         notify_fn=lambda t, b, u: notes.append((t, b, u)))
    return out, notes


def test_escalation_flags_lead_alerts_urgently_and_never_auto_replies(conn, listas):
    lead = _lead(conn, listas)
    out, notes = _handle(conn, "Vou chamar meu advogado, isso é assédio", in_hours=False)
    row = db.get_lead(conn, lead)
    assert out.escalated and not out.auto_reply and row["escalar"] == 1
    assert row["auto_resposta_enviada"] == 0
    (title, body, urgent), = notes
    assert urgent and "não responda" in title.lower() and "advogado" in body


def test_escalation_without_optout_does_not_blocklist_a_possible_client(conn, listas):
    lead = _lead(conn, listas)
    _handle(conn, "Meu advogado precisa de um site, pode mandar o exemplo?")
    assert db.get_lead(conn, lead)["escalar"] == 1 and not db.is_blocked(conn, {NUMBER})
    assert db.get_lead(conn, lead)["status"] != db.NAO_QUER


def test_escalation_with_optout_blocks_too(conn, listas):
    lead = _lead(conn, listas)
    _handle(conn, "Pare de mandar mensagens ou vou denunciar vocês")
    assert db.get_lead(conn, lead)["status"] == db.NAO_QUER and db.is_blocked(conn, {NUMBER})
    assert db.get_lead(conn, lead)["escalar"] == 1


def test_manager_alert_lists_recent_escalations(conn, listas):
    _lead(conn, listas)
    _handle(conn, "Vou denunciar vocês por spam")
    alertas = insights.alerts(conn, date(2026, 10, 21))
    assert any("Não responda" in a.titulo and a.nivel == insights.CRITICO for a in alertas)
    assert not any("Não responda" in a.titulo for a in insights.alerts(conn, date(2026, 11, 20)))  # some depois
