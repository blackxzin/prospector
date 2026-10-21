"""Envio humanizado dos itens aprovados da fila."""

from __future__ import annotations

import logging
import random
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import Callable, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import db
from . import fila as fq
from .messages import TemplateSet, fix_greeting, tidy
from .notify import notify
from .phone import to_jid, variants
from .schedule import Rules, day_start, earliest_send_local, fmt_local, random_gap, sent_today, typing_seconds
from .whatsapp import WhatsAppError

log = logging.getLogger("prospector")

PRESENCE_REFRESH_S = 8  # WhatsApp apaga o "digitando" depois de ~10s sem renovação


class Messenger(Protocol):
    def send_text(self, jid: str, text: str) -> object: ...
    def presence(self, jid: str, state: str) -> None: ...
    def check_number(self, number: str) -> str | None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
    def sleep(self, seconds: float) -> None: ...


class RealClock:
    def now(self) -> datetime:
        return db.utcnow()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class StoppableClock(RealClock):
    """Relógio real cuja espera pode ser interrompida (botão Pausar do painel)."""

    def __init__(self, stop_event):
        self._stop = stop_event

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._stop.wait(seconds)


class VirtualClock:
    """Relógio simulado para --dry-run: 'dorme' sem esperar de verdade."""

    def __init__(self, start: datetime):
        self._now = start

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += timedelta(seconds=max(0.0, seconds))


@dataclass
class SendReport:
    enviados: int = 0
    pulados: int = 0
    erros: int = 0
    parado: str | None = None
    motivo_fim: str = ""
    linhas: list[str] = field(default_factory=list)


def emergency_stop(conn: sqlite3.Connection, reason: str, notify_enabled: bool) -> None:
    db.set_state(conn, db.PARADO, f"{db.iso(db.utcnow())} {reason}")
    notify("Prospector PAROU", f"{reason}\nRode 'prospector retomar' depois de conferir.", urgent=True,
           enabled=notify_enabled)


def _approved(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT f.*, l.telefone, l.jid, l.nome, l.status AS lead_status, l.fuso, l.idioma, l.pais FROM fila f "
        "JOIN leads l ON l.id = f.lead_id WHERE f.status = ? ORDER BY f.id",
        (fq.APROVADO,),
    ).fetchall()


def _lead_tz(item: sqlite3.Row, rules: Rules) -> tzinfo:
    try:
        return ZoneInfo(item["fuso"]) if item["fuso"] else rules.tz
    except (KeyError, IndexError, ZoneInfoNotFoundError):
        return rules.tz


def _next_item(conn: sqlite3.Connection, seen: set[int], when_for: Callable[[sqlite3.Row], datetime | None],
               now: datetime) -> tuple[sqlite3.Row | None, datetime | None]:
    """(próximo item, None) ou (None, quando acorda) ou (None, None) se não há mais nada.

    Escolhe o item liberado que puder sair mais cedo (lead de outro fuso não trava os demais). Itens com
    `liberar_em` no futuro esperam. A fila é relida a cada chamada: o que for aprovado durante o envio entra."""
    pending = [i for i in _approved(conn) if i["id"] not in seen]
    if not pending:
        return None, None
    ready = [i for i in pending if not i["liberar_em"] or db.parse_iso(i["liberar_em"]) <= now]
    if not ready:
        return None, min(db.parse_iso(i["liberar_em"]) for i in pending)
    best: tuple[datetime, sqlite3.Row] | None = None
    for item in ready:
        when = when_for(item)
        if when is not None and (best is None or when < best[0]):
            best = (when, item)
    return (best[1] if best else ready[0]), None


def _skip(conn: sqlite3.Connection, item: sqlite3.Row) -> None:
    """Cancela o item da fila (não será mais enviado)."""
    with db.tx(conn):
        conn.execute("UPDATE fila SET status = ?, atualizado_em = ? WHERE id = ?",
                     (fq.CANCELADO, db.iso(db.utcnow()), item["id"]))


def _blocked_reason(conn: sqlite3.Connection, item: sqlite3.Row) -> str | None:
    numbers = variants(item["telefone"])
    if db.is_blocked(conn, numbers):
        return "número na lista de bloqueio"
    if item["lead_status"] == db.NAO_QUER:
        return "lead pediu para não receber mensagens"
    prospecting = item["tipo"] in (db.PRIMEIRA, db.FOLLOWUP)
    if prospecting and item["lead_status"] in (db.RESPONDEU, db.INTERESSADO, db.CLIENTE):
        return f"lead com status {item['lead_status']}"
    if item["tipo"] == fq.COBRANCA:
        paid = conn.execute("SELECT pago_em FROM pagamentos WHERE id = ?", (item["pagamento_id"],)).fetchone()
        if not paid or paid["pago_em"]:
            return "parcela já paga ou removida"
    if item["tipo"] == db.PRIMEIRA and db.first_message_already_sent(conn, numbers):
        return "primeira mensagem já enviada para este número"
    return None


def type_and_send(client: Messenger, clock: Clock, jid: str, text: str, typing_s: float) -> object:
    try:
        remaining = typing_s
        while remaining > 0:
            client.presence(jid, "composing")
            step = min(PRESENCE_REFRESH_S, remaining)
            clock.sleep(step)
            remaining -= step
        return client.send_text(jid, text)
    finally:
        try:
            client.presence(jid, "paused")
        except WhatsAppError as exc:
            log.debug("presence paused falhou: %s", exc)


PORTFOLIO_DELAY_S = (25.0, 70.0)   # pausa entre a primeira mensagem e o portfólio
PORTFOLIO_TYPING_S = (3.0, 8.0)
_BLOCKS_PORTFOLIO = (db.NAO_QUER, db.RESPONDEU, db.INTERESSADO, db.CLIENTE)


def _send_portfolio(conn: sqlite3.Connection, client: Messenger, clock: Clock, rng: random.Random,
                    item: sqlite3.Row, jid: str, templates: TemplateSet | None, notify_enabled: bool,
                    should_stop: Callable[[], bool]) -> tuple[bool, float]:
    """Manda o portfólio como 2ª mensagem, 25 a 70 s depois da primeira. (enviou, segundos gastos).

    Só para idiomas que têm `portfolio` e `portfolio_msg` (hoje: português do Brasil). Se o lead respondeu ou pediu
    para parar nesse intervalo, não manda: quem responde é você."""
    tpl = templates.get(item["idioma"]) if templates else None
    options = list((tpl.data.get("portfolio_msg") if tpl else None) or [])
    link = str(tpl.data.get("portfolio") or "") if tpl else ""
    if not options or not link:
        return False, 0.0
    started = clock.now()
    clock.sleep(rng.uniform(*PORTFOLIO_DELAY_S))
    spent = lambda: (clock.now() - started).total_seconds()  # noqa: E731
    fresh = conn.execute("SELECT status FROM leads WHERE id = ?", (item["lead_id"],)).fetchone()
    if should_stop() or db.stop_reason(conn) or (fresh and fresh["status"] in _BLOCKS_PORTFOLIO):
        return False, spent()
    text = tidy(rng.choice(options).format(portfolio=link))
    try:
        result = type_and_send(client, clock, jid, text, round(rng.uniform(*PORTFOLIO_TYPING_S), 1))
    except WhatsAppError as exc:
        with db.tx(conn):
            db.log_send(conn, lead_id=item["lead_id"], fila_id=item["id"], telefone=item["telefone"],
                        tipo="portfolio", texto=text, dry_run=False, ok=False, http_status=exc.status,
                        resposta_api=(exc.body or str(exc))[:2000])
        emergency_stop(conn, f"Erro ao enviar o portfólio para {item['telefone']} ({item['nome']}): {exc}", notify_enabled)
        return False, spent()
    with db.tx(conn):
        db.log_send(conn, lead_id=item["lead_id"], fila_id=item["id"], telefone=item["telefone"], tipo="portfolio",
                    texto=text, dry_run=False, ok=True, http_status=getattr(result, "status", 200),
                    resposta_api=str(getattr(result, "body", ""))[:2000], when=clock.now())
    return True, spent()


def _record_success(conn: sqlite3.Connection, item: sqlite3.Row, text: str, jid: str, result: object,
                    when: datetime) -> None:
    status = getattr(result, "status", 200)
    body = getattr(result, "body", "")
    with db.tx(conn):
        db.log_send(conn, lead_id=item["lead_id"], fila_id=item["id"], telefone=item["telefone"],
                    tipo=item["tipo"], texto=text, dry_run=False, ok=True, http_status=status,
                    resposta_api=str(body)[:2000], when=when)
        conn.execute("UPDATE fila SET status = ?, texto = ?, atualizado_em = ? WHERE id = ?",
                     (fq.ENVIADO, text, db.iso(when), item["id"]))
        if item["tipo"] == db.PRIMEIRA:
            db.update_lead(conn, item["lead_id"], status=db.ENVIADO, enviado_em=db.iso(when), jid=jid,
                           variante=item["variante"])
        elif item["tipo"] == db.FOLLOWUP:
            db.update_lead(conn, item["lead_id"], status=db.FOLLOW_UP, followup_em=db.iso(when))
        elif item["tipo"] == fq.COBRANCA and item["pagamento_id"]:
            conn.execute("UPDATE pagamentos SET cobrancas = cobrancas + 1, ultima_cobranca = ? WHERE id = ?",
                         (when.date().isoformat(), item["pagamento_id"]))


def _record_failure(conn: sqlite3.Connection, item: sqlite3.Row, text: str, exc: WhatsAppError) -> None:
    with db.tx(conn):
        db.log_send(conn, lead_id=item["lead_id"], fila_id=item["id"], telefone=item["telefone"],
                    tipo=item["tipo"], texto=text, dry_run=False, ok=False, http_status=exc.status,
                    resposta_api=(exc.body or str(exc))[:2000])
        conn.execute("UPDATE fila SET status = ?, atualizado_em = ? WHERE id = ?",
                     (fq.ERRO, db.iso(db.utcnow()), item["id"]))
        db.update_lead(conn, item["lead_id"], status=db.ERRO, motivo=str(exc)[:300])


def run(
    conn: sqlite3.Connection,
    client: Messenger | None,
    rules: Rules,
    *,
    typing_min_s: int,
    typing_max_s: int,
    dry_run: bool,
    clock: Clock | None = None,
    wait_next_day: bool = False,
    notify_enabled: bool = True,
    rng: random.Random | None = None,
    out: Callable[[str], None] = print,
    should_stop: Callable[[], bool] = lambda: False,
    templates: TemplateSet | None = None,
    portfolio_apos_primeira: bool = False,
) -> SendReport:
    """Envia os itens aprovados respeitando todas as regras. Em dry-run simula o relógio e não envia nada."""
    rng = rng or random.Random()
    report = SendReport()
    clock = clock or (VirtualClock(db.utcnow()) if dry_run else RealClock())
    if not dry_run and client is None:
        raise ValueError("Envio real precisa do cliente WA-AKG (configure WAAKG_SESSION e WAAKG_API_KEY)")

    stop = db.stop_reason(conn)
    if stop and not dry_run:
        report.parado = stop
        report.motivo_fim = f"Sistema parado: {stop}. Rode 'prospector retomar' para liberar."
        out(report.motivo_fim)
        return report

    # Em dry-run a simulação atravessa dias, para mostrar o cronograma completo.
    cross_days = wait_next_day or dry_run
    since = clock.now() - timedelta(days=2)
    sent_times = db.real_send_times(conn, since)
    foreign_times = db.real_send_times(conn, since, foreign_only=True)
    simulated: list[datetime] = []
    gap = random_gap(rules, rng)

    def when_for(it: sqlite3.Row) -> datetime | None:
        start = clock.now()
        if (rules.max_foreign_per_day and it["pais"] != "BR"
                and sent_today(start, foreign_times, rules.tz) >= rules.max_foreign_per_day):
            start = day_start(start, rules.tz) + timedelta(days=1)   # teto de hoje para fora do Brasil: fica para amanhã
        return earliest_send_local(start, sent_times + simulated, rules, gap, _lead_tz(it, rules))

    seen: set[int] = set()
    while True:
        item, wake = _next_item(conn, seen, when_for, clock.now())
        if item is None:
            if wake is None:
                break
            if not cross_days:
                report.motivo_fim = (f"Itens agendados para {fmt_local(wake, rules.tz)}. "
                                     "Use --aguardar para esperar.")
                break
            out(f"  aguardando liberação dos itens agendados ({fmt_local(wake, rules.tz)})")
            clock.sleep(max(1.0, min((wake - clock.now()).total_seconds(), 300.0)))
            if should_stop():
                report.motivo_fim = "Pausado por você."
                break
            continue
        seen.add(item["id"])
        if should_stop():
            report.motivo_fim = "Pausado por você."
            break
        reason = _blocked_reason(conn, item)
        if reason:
            out(f"  pulando #{item['id']} {item['nome']}: {reason}")
            if not dry_run:
                _skip(conn, item)
            report.pulados += 1
            continue

        when = when_for(item)
        if when is None:
            if not cross_days:
                report.motivo_fim = f"Limite diário ({rules.max_per_day}) atingido. Continua no próximo dia útil."
                break
            tomorrow = clock.now().astimezone(rules.tz).replace(hour=0, minute=0, second=0) + timedelta(days=1)
            out(f"  limite diário ({rules.max_per_day}) atingido; continua em {fmt_local(tomorrow, rules.tz)}")
            clock.sleep((tomorrow - clock.now()).total_seconds())
            when = when_for(item)
            if when is None:
                report.motivo_fim = "Sem horário disponível."
                break
        if when.astimezone(rules.tz).date() != clock.now().astimezone(rules.tz).date() and not cross_days:
            report.motivo_fim = (f"Fora do horário. Próxima janela: {fmt_local(when, rules.tz)}. "
                                 "Use --aguardar para esperar.")
            break
        wait_s = (when - clock.now()).total_seconds()
        if wait_s > 0:
            out(f"  aguardando {wait_s / 60:.1f} min (até {when.astimezone(rules.tz):%H:%M})")
            clock.sleep(wait_s)
            if should_stop():
                report.motivo_fim = "Pausado por você."
                break

        lead_tz = _lead_tz(item, rules)
        now_local = clock.now().astimezone(lead_tz)
        text = (templates.get(item["idioma"]).fix_greeting if templates else fix_greeting)(item["texto"], now_local)
        typing_s = typing_seconds(text, typing_min_s, typing_max_s, rng)

        if dry_run:
            clock.sleep(typing_s)
            line = (f"[DRY-RUN {fmt_local(now_local, rules.tz)}] #{item['id']} {item['tipo']} -> {item['telefone']} "
                    f"({item['nome']}), digitando {typing_s:.0f}s:\n    {text}")
            out(line)
            report.linhas.append(line)
            with db.tx(conn):
                db.log_send(conn, lead_id=item["lead_id"], fila_id=item["id"], telefone=item["telefone"],
                            tipo=item["tipo"], texto=text, dry_run=True, ok=True, http_status=None,
                            resposta_api="dry-run", when=clock.now())
            simulated.append(clock.now())
            if item["pais"] != "BR":
                foreign_times.append(clock.now())
            report.enviados += 1
            gap = random_gap(rules, rng)
            continue

        assert client is not None
        try:
            jid = item["jid"]
            if not jid:
                jid = client.check_number(item["telefone"])
                if not jid:
                    with db.tx(conn):
                        db.update_lead(conn, item["lead_id"], status=db.SEM_WHATSAPP)
                    _skip(conn, item)
                    out(f"  #{item['id']} {item['nome']}: número não tem WhatsApp, pulando")
                    report.pulados += 1
                    continue
            # Revalida depois da espera: o lead pode ter respondido/pedido para parar enquanto aguardávamos.
            fresh = conn.execute("SELECT status FROM leads WHERE id = ?", (item["lead_id"],)).fetchone()
            changed = fresh and (fresh["status"] == db.NAO_QUER or (
                item["tipo"] in (db.PRIMEIRA, db.FOLLOWUP)
                and fresh["status"] in (db.RESPONDEU, db.INTERESSADO, db.CLIENTE)))
            if db.stop_reason(conn) or changed:
                out(f"  #{item['id']}: situação mudou durante a espera, pulando")
                report.pulados += 1
                if db.stop_reason(conn):
                    report.parado = db.stop_reason(conn)
                    break
                _skip(conn, item)
                continue
            result = type_and_send(client, clock, jid or to_jid(item["telefone"]), text, typing_s)
        except WhatsAppError as exc:
            _record_failure(conn, item, text, exc)
            report.erros += 1
            reason = f"Erro no envio para {item['telefone']} ({item['nome']}): {exc}"
            # Regra: qualquer erro de sessão/bloqueio para tudo. Por segurança, outros erros também param.
            emergency_stop(conn, reason, notify_enabled)
            report.parado = reason
            out(f"  PARADO: {reason}")
            break

        sent_at = clock.now()
        _record_success(conn, item, text, jid, result, sent_at)
        sent_times.append(sent_at)
        if item["pais"] != "BR":
            foreign_times.append(sent_at)
        report.enviados += 1
        out(f"  enviado #{item['id']} {item['tipo']} -> {item['telefone']} ({item['nome']}) às {sent_at.astimezone(rules.tz):%H:%M}")
        gap = random_gap(rules, rng)
        if portfolio_apos_primeira and item["tipo"] == db.PRIMEIRA:
            sent_portfolio, seconds = _send_portfolio(conn, client, clock, rng, item, jid or to_jid(item["telefone"]),
                                                      templates, notify_enabled, should_stop)
            gap += int(seconds)   # o intervalo conta a partir da última mensagem enviada, portfólio incluído
            if sent_portfolio:
                out("    + portfólio enviado")
            if db.stop_reason(conn):
                report.parado = db.stop_reason(conn)
                break

    if not report.motivo_fim and not report.parado:
        report.motivo_fim = "Fila de aprovados vazia."
    return report
