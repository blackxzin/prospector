from datetime import timedelta, timezone

import pytest

from prospector import db
from prospector.importer import import_rows
from prospector.poll import poll_replies
from prospector.replies import Inbound, handle_inbound
from prospector.whatsapp import WaClient, WhatsAppError

from .conftest import csv_row, local_dt

NUMBER = "5514991234567"
JID = f"{NUMBER}@s.whatsapp.net"
SENT = local_dt(2026, 10, 19, 10)
NOW = local_dt(2026, 10, 19, 15)


class FakeClient:
    def __init__(self, chats):
        self.chats = chats  # jid -> lista de mensagens do WA-AKG
        self.asked = []

    def chat_messages(self, jid):
        self.asked.append(jid)
        return self.chats.get(jid, [])


def _msg(text, at, *, key, from_me=False, jid=JID, kind="TEXT"):
    return {"keyId": key, "remoteJid": jid, "fromMe": from_me, "content": text, "type": kind,
            "pushName": "Dono", "timestamp": at.astimezone(timezone.utc).isoformat().replace("+00:00", ".000Z")}


def _lead(conn, listas, phone="(14) 99123-4567", status=db.ENVIADO):
    import_rows(conn, [csv_row(phone=phone)], "q", listas)
    lead = conn.execute("SELECT * FROM leads ORDER BY id DESC").fetchone()
    with db.tx(conn):
        db.update_lead(conn, lead["id"], status=status, enviado_em=db.iso(SENT))
    return lead["id"]


def _handler(conn, notes, in_hours=True):
    def handle(msg, at):
        return handle_inbound(conn, msg, now=at, in_business_hours=in_hours,
                              notify_fn=lambda t, b, u: notes.append((t, b)))
    return handle


def test_reply_after_send_is_recorded(conn, listas):
    lead = _lead(conn, listas)
    client = FakeClient({JID: [
        _msg("Oi, tudo bem?", SENT, key="A", from_me=True),
        _msg("Pode mandar o exemplo", SENT + timedelta(hours=2), key="B"),
    ]})
    notes = []
    result = poll_replies(conn, client, _handler(conn, notes), NOW)
    row = db.get_lead(conn, lead)
    assert result.novas == 1 and row["status"] == db.INTERESSADO
    assert row["respondeu_em"] and "exemplo" in row["resposta"]
    assert notes


def test_same_message_is_not_processed_twice(conn, listas):
    lead = _lead(conn, listas)
    client = FakeClient({JID: [_msg("quanto custa?", SENT + timedelta(hours=1), key="B")]})
    notes = []
    poll_replies(conn, client, _handler(conn, notes), NOW)
    again = poll_replies(conn, client, _handler(conn, notes), NOW + timedelta(minutes=5))
    assert again.novas == 0
    assert conn.execute("SELECT COUNT(*) FROM respostas WHERE lead_id = ?", (lead,)).fetchone()[0] == 1
    assert len(notes) == 1


def test_opt_out_found_by_polling_blocks_number(conn, listas):
    lead = _lead(conn, listas)
    client = FakeClient({JID: [_msg("Não tenho interesse, pare", SENT + timedelta(hours=1), key="C")]})
    poll_replies(conn, client, _handler(conn, []), NOW)
    assert db.get_lead(conn, lead)["status"] == db.NAO_QUER
    assert db.is_blocked(conn, {NUMBER})


def test_messages_before_the_send_and_own_messages_are_ignored(conn, listas):
    lead = _lead(conn, listas)
    client = FakeClient({JID: [
        _msg("conversa antiga", SENT - timedelta(days=3), key="OLD"),
        _msg("mandei pelo celular", SENT + timedelta(hours=1), key="ME", from_me=True),
    ]})
    result = poll_replies(conn, client, _handler(conn, []), NOW)
    assert result.novas == 0 and db.get_lead(conn, lead)["status"] == db.ENVIADO


def test_only_contacted_leads_are_looked_up(conn, listas):
    _lead(conn, listas)
    import_rows(conn, [csv_row(phone="(14) 98888-7777", link="outro")], "q", listas)  # novo, nunca contatado
    client = FakeClient({})
    poll_replies(conn, client, _handler(conn, []), NOW)
    assert client.asked == [JID]  # nenhum outro chat (pessoal ou não) é lido


def test_media_reply_is_kept_as_placeholder(conn, listas):
    lead = _lead(conn, listas)
    client = FakeClient({JID: [_msg("", SENT + timedelta(hours=1), key="D", kind="AUDIO")]})
    poll_replies(conn, client, _handler(conn, []), NOW)
    assert db.get_lead(conn, lead)["resposta"] == "[audio]"


def test_old_leads_are_not_polled(conn, listas):
    _lead(conn, listas)
    client = FakeClient({JID: [_msg("oi", SENT + timedelta(hours=1), key="E")]})
    result = poll_replies(conn, client, _handler(conn, []), NOW + timedelta(days=40))
    assert client.asked == [] and result.novas == 0


def test_non_fatal_error_skips_chat_and_fatal_error_stops(conn, listas):
    _lead(conn, listas)

    class Broken:
        def __init__(self, fatal):
            self.fatal = fatal

        def chat_messages(self, jid):
            raise WhatsAppError("falhou", fatal=self.fatal)

    assert poll_replies(conn, Broken(False), _handler(conn, []), NOW).erros == 1
    with pytest.raises(WhatsAppError):
        poll_replies(conn, Broken(True), _handler(conn, []), NOW)


def test_chat_messages_reads_list_from_api_envelope():
    from prospector.httpclient import HttpResponse
    body = b'{"status":true,"data":[{"keyId":"A","fromMe":false,"content":"oi"}]}'
    client = WaClient("http://wa", "s", "k", request=lambda *a, **kw: HttpResponse(200, body))
    assert client.chat_messages(JID) == [{"keyId": "A", "fromMe": False, "content": "oi"}]
    empty = WaClient("http://wa", "s", "k", request=lambda *a, **kw: HttpResponse(200, b'{"status":true,"data":null}'))
    assert empty.chat_messages(JID) == []


def test_inbound_msg_id_dedupes_in_handler(conn, listas):
    lead = _lead(conn, listas)
    msg = Inbound(numbers=frozenset({NUMBER}), jids=frozenset({JID}), text="oi", push_name="", msg_id="X1")
    notes = []
    handle_inbound(conn, msg, now=NOW, in_business_hours=True, notify_fn=lambda t, b, u: notes.append(t))
    out = handle_inbound(conn, msg, now=NOW, in_business_hours=True, notify_fn=lambda t, b, u: notes.append(t))
    assert out.duplicate and len(notes) == 1
    assert conn.execute("SELECT COUNT(*) FROM respostas WHERE lead_id = ?", (lead,)).fetchone()[0] == 1
