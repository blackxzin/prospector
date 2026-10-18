import random
from datetime import timedelta

import pytest

from prospector import db
from prospector import fila as fq
from prospector.importer import import_rows
from prospector.schedule import in_window
from prospector.sender import VirtualClock, run
from prospector.whatsapp import WaClient, WhatsAppError
from prospector.httpclient import HttpResponse

from .conftest import csv_row, local_dt

MONDAY_9 = local_dt(2026, 10, 19, 9, 0)


class FakeWa:
    def __init__(self, fail_on: int | None = None, error: WhatsAppError | None = None, exists: bool = True):
        self.sent: list[tuple[str, str]] = []
        self.presences: list[tuple[str, str]] = []
        self.fail_on = fail_on
        self.error = error or WhatsAppError("503 session not connected", status=503, fatal=True)
        self.exists = exists

    def check_number(self, number):
        return f"{number}@s.whatsapp.net" if self.exists else None

    def presence(self, jid, state):
        self.presences.append((jid, state))

    def send_text(self, jid, text):
        if self.fail_on is not None and len(self.sent) == self.fail_on:
            raise self.error
        self.sent.append((jid, text))
        return type("R", (), {"status": 200, "body": '{"status":true}'})()


def _approved_queue(conn, listas, templates, rules, n):
    rows = [csv_row(title=f"Pizzaria {i}", phone=f"(14) 991{i:02d}-00{i:02d}", link=f"l{i}") for i in range(n)]
    import_rows(conn, rows, "q", listas)
    fq.build_queue(conn, templates, rules, MONDAY_9, size=n, followup_days=3, rng=random.Random(0))
    fq.approve(conn, fq.pending_ids(conn))


def _run(conn, client, rules, clock, **kw):
    return run(conn, client, rules, typing_min_s=5, typing_max_s=20, dry_run=kw.pop("dry_run", False),
               clock=clock, notify_enabled=False, rng=random.Random(42), out=lambda s: None, **kw)


def test_dry_run_sends_nothing_and_respects_limits(conn, listas, templates, rules):
    _approved_queue(conn, listas, templates, rules, 10)
    client = FakeWa()
    report = _run(conn, client, rules, VirtualClock(MONDAY_9), dry_run=True)
    assert client.sent == [] and client.presences == []
    assert report.enviados == 10  # simulação atravessa para o dia seguinte
    times = [db.parse_iso(r["enviado_em"]) for r in conn.execute(
        "SELECT enviado_em FROM envios WHERE dry_run = 1 ORDER BY id")]
    assert all(in_window(t, rules) for t in times)
    by_day: dict = {}
    for t in times:
        by_day.setdefault(t.astimezone(rules.tz).date(), []).append(t)
    assert max(len(v) for v in by_day.values()) <= 8
    for a, b in zip(times, times[1:]):
        if a.date() == b.date():
            assert (b - a) >= timedelta(minutes=4)
    for i, t in enumerate(times):
        assert sum(1 for s in times if t - timedelta(hours=1) < s <= t) <= 3
    # lead e fila intactos
    assert conn.execute("SELECT COUNT(*) FROM fila WHERE status = 'aprovado'").fetchone()[0] == 10
    assert conn.execute("SELECT COUNT(*) FROM leads WHERE enviado_em IS NOT NULL").fetchone()[0] == 0


def test_real_send_types_then_sends_and_logs(conn, listas, templates, rules):
    _approved_queue(conn, listas, templates, rules, 2)
    client = FakeWa()
    clock = VirtualClock(MONDAY_9)
    report = _run(conn, client, rules, clock)
    assert report.enviados == 2 and len(client.sent) == 2
    assert ("composing" in [p[1] for p in client.presences]) and client.presences[-1][1] == "paused"
    logs = conn.execute("SELECT * FROM envios WHERE dry_run = 0").fetchall()
    assert len(logs) == 2 and all(r["ok"] == 1 and r["http_status"] == 200 for r in logs)
    assert conn.execute("SELECT COUNT(*) FROM leads WHERE status = 'enviado'").fetchone()[0] == 2


def test_stops_at_daily_limit_without_wait(conn, listas, templates, rules):
    _approved_queue(conn, listas, templates, rules, 10)
    client = FakeWa()
    report = _run(conn, client, rules, VirtualClock(MONDAY_9))
    assert len(client.sent) == 8
    assert "Limite diário" in report.motivo_fim


def test_outside_window_does_not_send(conn, listas, templates, rules):
    _approved_queue(conn, listas, templates, rules, 1)
    client = FakeWa()
    report = _run(conn, client, rules, VirtualClock(local_dt(2026, 10, 19, 18, 0)))
    assert client.sent == [] and "Fora do horário" in report.motivo_fim


def test_never_sends_first_message_twice(conn, listas, templates, rules):
    _approved_queue(conn, listas, templates, rules, 1)
    item = fq.list_items(conn, (fq.APROVADO,))[0]
    # outro cadastro / execução anterior já mandou para o mesmo número
    with db.tx(conn):
        db.log_send(conn, lead_id=None, fila_id=None, telefone=item["telefone"], tipo="primeira", texto="x",
                    dry_run=False, ok=True, http_status=200, resposta_api="")
    client = FakeWa()
    report = _run(conn, client, rules, VirtualClock(MONDAY_9))
    assert client.sent == [] and report.pulados == 1


def test_db_unique_index_blocks_duplicate_first_message(conn):
    import sqlite3
    with db.tx(conn):
        db.log_send(conn, lead_id=None, fila_id=None, telefone="5514991234567", tipo="primeira", texto="a",
                    dry_run=False, ok=True, http_status=200, resposta_api="")
    with pytest.raises(sqlite3.IntegrityError):
        db.log_send(conn, lead_id=None, fila_id=None, telefone="5514991234567", tipo="primeira", texto="b",
                    dry_run=False, ok=True, http_status=200, resposta_api="")


def test_session_error_stops_everything(conn, listas, templates, rules):
    _approved_queue(conn, listas, templates, rules, 3)
    client = FakeWa(fail_on=1)
    report = _run(conn, client, rules, VirtualClock(MONDAY_9))
    assert len(client.sent) == 1 and report.parado
    assert db.stop_reason(conn)
    # próxima execução recusa até 'retomar'
    client2 = FakeWa()
    report2 = _run(conn, client2, rules, VirtualClock(MONDAY_9 + timedelta(hours=1)))
    assert client2.sent == [] and report2.parado
    failed = conn.execute("SELECT * FROM envios WHERE ok = 0").fetchone()
    assert failed["http_status"] == 503


def test_skips_numbers_without_whatsapp(conn, listas, templates, rules):
    _approved_queue(conn, listas, templates, rules, 1)
    report = _run(conn, FakeWa(exists=False), rules, VirtualClock(MONDAY_9))
    assert report.pulados == 1
    assert conn.execute("SELECT status FROM leads").fetchone()["status"] == db.SEM_WHATSAPP


# ---------- cliente HTTP do WA-AKG ----------

def _client(responses):
    calls = []

    def fake_request(method, url, **kw):
        calls.append((method, url, kw))
        return responses.pop(0)
    return WaClient("http://wa", "minha-sessao", "wag_x", request=fake_request), calls


def test_wa_client_builds_documented_urls():
    client, calls = _client([HttpResponse(200, b'{"status":true}'), HttpResponse(200, b'{"success":true}')])
    client.send_text("5514991234567@s.whatsapp.net", "oi")
    client.presence("5514991234567@s.whatsapp.net", "composing")
    assert calls[0][1] == "http://wa/api/messages/minha-sessao/5514991234567%40s.whatsapp.net/send"
    assert calls[0][2]["json_body"] == {"message": {"text": "oi"}}
    assert calls[0][2]["headers"]["X-API-Key"] == "wag_x"
    assert calls[1][1] == "http://wa/api/chat/minha-sessao/5514991234567%40s.whatsapp.net/presence"
    assert calls[1][2]["json_body"] == {"presence": "composing"}


@pytest.mark.parametrize("status,body,fatal", [
    (503, b'{"error":"Session not connected"}', True),
    (401, b'{"error":"Unauthorized"}', True),
    (500, b'{"error":"account banned"}', True),
    (400, b'{"error":"bad"}', False),
])
def test_wa_client_error_classification(status, body, fatal):
    client, _ = _client([HttpResponse(status, body)])
    with pytest.raises(WhatsAppError) as exc:
        client.send_text("x@s.whatsapp.net", "oi")
    assert exc.value.fatal is fatal


def test_wa_client_status_false_is_error():
    client, _ = _client([HttpResponse(200, b'{"status":false,"message":"falhou"}')])
    with pytest.raises(WhatsAppError):
        client.send_text("x@s.whatsapp.net", "oi")
