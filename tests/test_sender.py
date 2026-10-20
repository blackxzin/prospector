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


@pytest.mark.parametrize("payload, expected", [
    # formato real do WA-AKG v1.7: resultado dentro de "data"
    (b'{"status":true,"data":{"results":[{"number":"5514996112048","exists":true,'
     b'"jid":"5514996112048@s.whatsapp.net"}]}}', "5514996112048@s.whatsapp.net"),
    (b'{"results":[{"exists":true,"jid":"5514996112048@s.whatsapp.net"}]}', "5514996112048@s.whatsapp.net"),
    (b'{"status":true,"data":{"results":[{"number":"5514996112048","exists":false}]}}', None),
])
def test_check_number_reads_api_envelope(payload, expected):
    client = WaClient("http://wa", "s", "k", request=lambda *a, **kw: HttpResponse(200, payload))
    assert client.check_number("5514996112048") == expected


def test_items_approved_while_running_are_picked_up(conn, listas, templates, rules):
    _approved_queue(conn, listas, templates, rules, 2)
    extra = [csv_row(title="Pizzaria Nova", phone="(14) 99888-7766", link="nova")]
    import_rows(conn, extra, "q", listas)

    class Approver(FakeWa):
        def send_text(self, jid, text):
            if not self.sent:  # durante o 1º envio o usuário aprova mais um item
                fq.build_queue(conn, templates, rules, MONDAY_9, size=3, followup_days=3, rng=random.Random(1))
                fq.approve(conn, fq.pending_ids(conn))
            return super().send_text(jid, text)

    client = Approver()
    report = _run(conn, client, rules, VirtualClock(MONDAY_9), wait_next_day=True)
    assert report.enviados == 3 and len(client.sent) == 3


# ---------- horário local do lead ----------

from prospector.importer import import_rows as _imp  # noqa: E402
from prospector.mundo import Origem as _Orig  # noqa: E402
from prospector.schedule import earliest_send_local  # noqa: E402


def test_foreign_window_is_measured_in_the_lead_timezone(rules):
    from zoneinfo import ZoneInfo
    madrid = ZoneInfo("Europe/Madrid")
    # segunda 09:00 em São Paulo = 14:00 em Madrid (UTC+2 em outubro): dentro da janela das 14h às 17h30 de lá
    assert earliest_send_local(MONDAY_9, [], rules, 200, madrid) == MONDAY_9
    # segunda 12:00 em São Paulo = 17:00 em Madrid: ainda dentro; 13:00 em SP = 18:00 em Madrid: fechado
    noon, one = MONDAY_9.replace(hour=12), MONDAY_9.replace(hour=13)
    assert earliest_send_local(noon, [], rules, 200, madrid) == noon
    nxt = earliest_send_local(one, [], rules, 200, madrid)
    assert nxt is not None and nxt.astimezone(madrid).hour == 9 and nxt.astimezone(madrid).date().day == 20
    # Brasil e sem fuso: igual ao de sempre
    assert earliest_send_local(MONDAY_9, [], rules, 200, rules.tz) == MONDAY_9
    assert earliest_send_local(MONDAY_9, [], rules, 200, None) == MONDAY_9


def test_global_daily_limit_still_applies_to_foreign_leads(rules):
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    sent = [MONDAY_9 + timedelta(minutes=10 * i) for i in range(8)]  # limite diário do conftest: 8
    tokyo = ZoneInfo("Europe/Madrid")
    when = earliest_send_local(MONDAY_9 + timedelta(hours=2), sent, rules, 200, tokyo)
    assert when is None or when.astimezone(rules.tz).date() > MONDAY_9.date()


def test_sender_picks_the_lead_that_can_go_now_and_uses_local_greeting(conn, listas, templates, rules):
    from prospector.messages import TemplateSet
    tset = TemplateSet.load(templates_path())
    # São Paulo 20h de domingo→ não usado; aqui: segunda 09:00 SP. Tóquio já está fora do horário; Madrid está dentro.
    _imp(conn, [csv_row(title="Tokyo Cafe", phone="090 1234 5678", link="t1")], "q", listas,
         _Orig("JP", "en", "Asia/Tokyo", "Tokyo"))
    _imp(conn, [csv_row(title="Café Luna", phone="612 34 56 79", link="m1")], "q", listas,
         _Orig("ES", "es", "Europe/Madrid", "Madrid"))
    fq.build_queue(conn, tset, rules, MONDAY_9, size=5, followup_days=3, rng=random.Random(0), pais=("JP", "ES"))
    fq.approve(conn, fq.pending_ids(conn))
    client = FakeWa()
    report = _run(conn, client, rules, VirtualClock(MONDAY_9), templates=tset)
    assert [t for _, t in client.sent][0].startswith(("Buenos días", "Buenas tardes", "Hola"))
    assert client.sent[0][0].startswith("34612345679") and report.enviados >= 1  # Madrid antes de Tóquio


def templates_path():
    from prospector.config import ROOT
    return ROOT / "templates.yaml"


# ---------- liberação agendada ----------

def test_item_with_release_time_waits_and_sender_keeps_running(conn, listas, templates, rules):
    _approved_queue(conn, listas, templates, rules, 1)
    release = MONDAY_9 + timedelta(hours=2)
    conn.execute("UPDATE fila SET liberar_em = ?", (db.iso(release),))
    conn.commit()
    client = FakeWa()
    report = _run(conn, client, rules, VirtualClock(MONDAY_9), wait_next_day=True)
    assert report.enviados == 1 and len(client.sent) == 1
    sent_at = db.parse_iso(conn.execute("SELECT enviado_em FROM envios WHERE ok = 1").fetchone()[0])
    assert sent_at >= release


def test_scheduled_item_without_wait_is_left_for_later(conn, listas, templates, rules):
    _approved_queue(conn, listas, templates, rules, 1)
    conn.execute("UPDATE fila SET liberar_em = ?", (db.iso(MONDAY_9 + timedelta(hours=2)),))
    conn.commit()
    client = FakeWa()
    report = _run(conn, client, rules, VirtualClock(MONDAY_9))
    assert report.enviados == 0 and client.sent == [] and "agendados" in report.motivo_fim


def test_ready_items_go_first_while_another_is_scheduled(conn, listas, templates, rules):
    _approved_queue(conn, listas, templates, rules, 2)
    first, lead = conn.execute("SELECT f.id, l.telefone FROM fila f JOIN leads l ON l.id = f.lead_id "
                               "ORDER BY f.id").fetchone()
    conn.execute("UPDATE fila SET liberar_em = ? WHERE id = ?", (db.iso(MONDAY_9 + timedelta(hours=1)), first))
    conn.commit()
    client = FakeWa()
    _run(conn, client, rules, VirtualClock(MONDAY_9), wait_next_day=True)
    assert len(client.sent) == 2 and client.sent[0][0] != f"{lead}@s.whatsapp.net"  # o agendado sai depois
    assert client.sent[1][0] == f"{lead}@s.whatsapp.net"
