"""Janela de horário comercial, limites de envio e dias úteis (funções puras)."""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, tzinfo

HOUR = timedelta(hours=1)
MAX_LOOKAHEAD_DAYS = 14


@dataclass(frozen=True)
class Rules:
    tz: tzinfo
    windows: tuple[tuple[time, time], ...]
    weekdays: frozenset[int]
    max_per_day: int
    max_per_hour: int
    interval_min_s: int
    interval_max_s: int

    @classmethod
    def from_settings(cls, s: object) -> Rules:
        return cls(
            tz=s.tz, windows=s.windows, weekdays=s.weekdays,  # type: ignore[attr-defined]
            max_per_day=s.max_per_day, max_per_hour=s.max_per_hour,  # type: ignore[attr-defined]
            interval_min_s=s.interval_min_s, interval_max_s=s.interval_max_s,  # type: ignore[attr-defined]
        )


def local(dt: datetime, tz: tzinfo) -> datetime:
    return dt.astimezone(tz)


def in_window(now: datetime, rules: Rules) -> bool:
    """True se `now` está num dia útil e dentro de alguma janela (fim exclusivo)."""
    t = local(now, rules.tz)
    if t.weekday() not in rules.weekdays:
        return False
    return any(start <= t.time() < end for start, end in rules.windows)


def next_window_start(now: datetime, rules: Rules) -> datetime:
    """Próximo instante >= now dentro da janela (o próprio now, se já estiver dentro)."""
    if in_window(now, rules):
        return now
    t = local(now, rules.tz)
    for offset in range(MAX_LOOKAHEAD_DAYS):
        day = t.date() + timedelta(days=offset)
        if day.weekday() not in rules.weekdays:
            continue
        for start, _ in rules.windows:
            candidate = datetime.combine(day, start, tzinfo=rules.tz)
            if candidate > t:
                return candidate
    raise ValueError("Nenhuma janela de envio nos próximos 14 dias; confira JANELAS/DIAS_UTEIS")


def window_end(now: datetime, rules: Rules) -> datetime | None:
    """Fim da janela atual, ou None se fora da janela."""
    t = local(now, rules.tz)
    if t.weekday() not in rules.weekdays:
        return None
    for start, end in rules.windows:
        if start <= t.time() < end:
            return datetime.combine(t.date(), end, tzinfo=rules.tz)
    return None


def day_start(now: datetime, tz: tzinfo) -> datetime:
    t = local(now, tz)
    return datetime.combine(t.date(), time(0), tzinfo=tz)


def sent_today(now: datetime, sent: list[datetime], tz: tzinfo) -> int:
    start = day_start(now, tz)
    return sum(1 for s in sent if start <= s <= now)


def sent_last_hour(now: datetime, sent: list[datetime]) -> int:
    return sum(1 for s in sent if now - HOUR < s <= now)


def limit_reason(now: datetime, sent: list[datetime], rules: Rules) -> str | None:
    """Motivo para não enviar agora por limite (dia/hora), ou None."""
    if sent_today(now, sent, rules.tz) >= rules.max_per_day:
        return f"limite diário atingido ({rules.max_per_day})"
    if sent_last_hour(now, sent) >= rules.max_per_hour:
        return f"limite por hora atingido ({rules.max_per_hour})"
    return None


def earliest_send(now: datetime, sent: list[datetime], rules: Rules, gap_s: int) -> datetime | None:
    """Próximo horário permitido para um envio, respeitando:

    janela comercial, intervalo `gap_s` desde o último envio, limite por hora e limite diário.
    Devolve None se não couber mais nada hoje (limite diário) — o chamador decide esperar amanhã.
    """
    candidate = now
    if sent:
        candidate = max(candidate, max(sent) + timedelta(seconds=gap_s))
    for _ in range(100):
        candidate = next_window_start(candidate, rules)
        if sent_today(candidate, sent, rules.tz) >= rules.max_per_day:
            return None
        recent = sorted(s for s in sent if candidate - HOUR < s <= candidate)
        if len(recent) >= rules.max_per_hour:
            # espera o envio mais antigo da última hora "sair" da janela de 60 min
            # (+ um pedaço do intervalo sorteado, para não cair sempre no mesmo minuto)
            candidate = recent[len(recent) - rules.max_per_hour] + HOUR + timedelta(seconds=1 + gap_s % 300)
            continue
        return candidate
    return None


_ALL_DAY = ((time(0), time(23, 59, 59, 999999)),)


def same_zone(a: tzinfo, b: tzinfo) -> bool:
    return a is b or getattr(a, "key", None) == getattr(b, "key", object())


def earliest_send_local(now: datetime, sent: list[datetime], rules: Rules, gap_s: int, tz: tzinfo | None) -> datetime | None:
    """Como earliest_send, mas a janela comercial é a do fuso do lead (`tz`).

    Limites de intervalo, hora e dia continuam globais (contados no fuso de `rules`); só o horário em que o lead
    pode receber (dias úteis e janelas) é medido no relógio dele. `tz` None ou igual ao de `rules`: comportamento
    de sempre."""
    if tz is None or same_zone(tz, rules.tz):
        return earliest_send(now, sent, rules, gap_s)
    lead_rules = replace(rules, tz=tz)
    limits_only = replace(rules, windows=_ALL_DAY, weekdays=frozenset(range(7)))
    candidate = now
    for _ in range(60):
        by_limits = earliest_send(candidate, sent, limits_only, gap_s)
        if by_limits is None:
            return None
        by_window = next_window_start(by_limits, lead_rules)
        if by_window == by_limits:
            return by_limits
        candidate = by_window
    return None


def random_gap(rules: Rules, rng: random.Random | None = None) -> int:
    return (rng or random).randint(rules.interval_min_s, rules.interval_max_s)


def typing_seconds(text: str, min_s: int, max_s: int, rng: random.Random | None = None) -> float:
    """Tempo de 'digitando' proporcional ao texto (~ 18 caracteres/s), limitado a [min_s, max_s]."""
    base = len(text) / 18
    jitter = (rng or random).uniform(0.85, 1.15)
    return round(min(max_s, max(min_s, base * jitter)), 1)


def add_business_days(start: datetime, days: int, rules: Rules) -> datetime:
    """Soma `days` dias úteis mantendo o horário (fuso local)."""
    t = local(start, rules.tz)
    current: date = t.date()
    added = 0
    while added < days:
        current += timedelta(days=1)
        if current.weekday() in rules.weekdays:
            added += 1
    return datetime.combine(current, t.timetz().replace(tzinfo=None), tzinfo=rules.tz)


_WEEKDAYS_PT = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")


def fmt_local(dt: datetime, tz: tzinfo) -> str:
    """'seg 19/10 09:00' no fuso local."""
    t = local(dt, tz)
    return f"{_WEEKDAYS_PT[t.weekday()]} {t:%d/%m %H:%M}"
