from datetime import timedelta

import pytest

from prospector.schedule import (
    add_business_days, earliest_send, in_window, limit_reason, next_window_start, typing_seconds,
)

from .conftest import local_dt

# 2026-10-19 é segunda-feira.


@pytest.mark.parametrize("dt,expected", [
    (local_dt(2026, 10, 19, 9, 0), True),
    (local_dt(2026, 10, 19, 8, 59), False),
    (local_dt(2026, 10, 19, 11, 29), True),
    (local_dt(2026, 10, 19, 11, 30), False),   # fim exclusivo
    (local_dt(2026, 10, 19, 12, 30), False),   # almoço
    (local_dt(2026, 10, 19, 14, 0), True),
    (local_dt(2026, 10, 19, 17, 29), True),
    (local_dt(2026, 10, 19, 17, 30), False),
    (local_dt(2026, 10, 23, 10, 0), True),     # sexta
    (local_dt(2026, 10, 24, 10, 0), False),    # sábado
    (local_dt(2026, 10, 25, 10, 0), False),    # domingo
])
def test_in_window(dt, expected, rules):
    assert in_window(dt, rules) is expected


def test_in_window_uses_sao_paulo_timezone(rules):
    from datetime import datetime, timezone
    # 12:30 UTC = 09:30 em São Paulo (UTC-3)
    assert in_window(datetime(2026, 10, 19, 12, 30, tzinfo=timezone.utc), rules)
    # 09:30 UTC = 06:30 em São Paulo
    assert not in_window(datetime(2026, 10, 19, 9, 30, tzinfo=timezone.utc), rules)


def test_next_window_start(rules):
    assert next_window_start(local_dt(2026, 10, 19, 12, 0), rules) == local_dt(2026, 10, 19, 14, 0)
    assert next_window_start(local_dt(2026, 10, 19, 18, 0), rules) == local_dt(2026, 10, 20, 9, 0)
    assert next_window_start(local_dt(2026, 10, 23, 18, 0), rules) == local_dt(2026, 10, 26, 9, 0)  # sex -> seg
    now = local_dt(2026, 10, 19, 10, 0)
    assert next_window_start(now, rules) == now


def test_daily_limit(rules):
    now = local_dt(2026, 10, 19, 17, 0)
    sent = [local_dt(2026, 10, 19, 9, 0) + timedelta(minutes=40 * i) for i in range(8)]
    assert limit_reason(now, sent, rules) == "limite diário atingido (8)"
    assert limit_reason(now, sent[:7], rules) is None
    # envios de ontem não contam
    yesterday = [s - timedelta(days=1) for s in sent]
    assert limit_reason(now, yesterday, rules) is None


def test_hourly_limit(rules):
    now = local_dt(2026, 10, 19, 10, 0)
    sent = [now - timedelta(minutes=m) for m in (5, 20, 50)]
    assert limit_reason(now, sent, rules) == "limite por hora atingido (3)"
    assert limit_reason(now, [now - timedelta(minutes=m) for m in (5, 20, 61)], rules) is None


def test_earliest_send_respects_gap(rules):
    now = local_dt(2026, 10, 19, 10, 0)
    last = now - timedelta(minutes=2)
    assert earliest_send(now, [last], rules, gap_s=300) == last + timedelta(seconds=300)


def test_earliest_send_waits_for_hour_limit(rules):
    now = local_dt(2026, 10, 19, 10, 30)
    sent = [local_dt(2026, 10, 19, 9, 40), local_dt(2026, 10, 19, 10, 0), local_dt(2026, 10, 19, 10, 20)]
    when = earliest_send(now, sent, rules, gap_s=240)
    assert when == local_dt(2026, 10, 19, 10, 44, 1)  # 09:40 + 1h + 1s + (240 % 300)s
    assert limit_reason(when, sent, rules) is None


def test_earliest_send_jumps_lunch_and_returns_none_at_daily_limit(rules):
    now = local_dt(2026, 10, 19, 11, 28)
    assert earliest_send(now, [now - timedelta(minutes=1)], rules, gap_s=600) == local_dt(2026, 10, 19, 14, 0)
    full = [local_dt(2026, 10, 19, 9, 0) + timedelta(minutes=25 * i) for i in range(8)]
    assert earliest_send(local_dt(2026, 10, 19, 15, 0), full, rules, gap_s=240) is None


def test_typing_seconds_bounds():
    assert typing_seconds("oi", 5, 20) == 5
    assert typing_seconds("x" * 2000, 5, 20) == 20
    mid = typing_seconds("x" * 180, 5, 20)
    assert 5 <= mid <= 20


def test_add_business_days_skips_weekend(rules):
    assert add_business_days(local_dt(2026, 10, 23, 10, 0), 3, rules) == local_dt(2026, 10, 28, 10, 0)  # sex -> qua
    assert add_business_days(local_dt(2026, 10, 19, 15, 0), 3, rules) == local_dt(2026, 10, 22, 15, 0)
