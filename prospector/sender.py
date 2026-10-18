"""Envio humanizado dos itens aprovados da fila."""

from __future__ import annotations

import logging
import random
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Protocol

from . import db
from . import fila as fq
from .messages import fix_greeting
from .notify import notify
from .phone import to_jid, variants
from .schedule import fmt_local, Rules, earliest_send, random_gap, typing_seconds
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
        "SELECT f.*, l.telefone, l.jid, l.nome, l.status AS lead_status FROM fila f "
        "JOIN leads l ON l.id = f.lead_id WHERE f.status = ? ORDER BY f.id",
        (fq.APROVADO,),
    ).fetchall()


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
    simulated: list[datetime] = []
    gap = random_gap(rules, rng)

    for item in _approved(conn):
        reason = _blocked_reason(conn, item)
        if reason:
            out(f"  pulando #{item['id']} {item['nome']}: {reason}")
            if not dry_run:
                _skip(conn, item)
            report.pulados += 1
            continue

        when = earliest_send(clock.now(), sent_times + simulated, rules, gap)
        if when is None:
            if not cross_days:
                report.motivo_fim = f"Limite diário ({rules.max_per_day}) atingido. Continua no próximo dia útil."
                break
            tomorrow = clock.now().astimezone(rules.tz).replace(hour=0, minute=0, second=0) + timedelta(days=1)
            clock.sleep((tomorrow - clock.now()).total_seconds())
            when = earliest_send(clock.now(), sent_times + simulated, rules, gap)
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

        now_local = clock.now().astimezone(rules.tz)
        text = fix_greeting(item["texto"], now_local)
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
        report.enviados += 1
        out(f"  enviado #{item['id']} {item['tipo']} -> {item['telefone']} ({item['nome']}) às {sent_at.astimezone(rules.tz):%H:%M}")
        gap = random_gap(rules, rng)

    if not report.motivo_fim and not report.parado:
        report.motivo_fim = "Fila de aprovados vazia."
    return report
