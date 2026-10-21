"""Canais alternativos ao WhatsApp: e-mail (Gmail/SMTP, envio automático) e DM do Instagram (envio manual).

Cada lead entra num canal só: ao montar a fila ele sai de 'novo' (para o WhatsApp não abordar de novo) e,
se o texto for rejeitado, volta. E-mail respeita as mesmas janelas e intervalos do WhatsApp, com limite próprio
por dia; o Instagram nunca é automatizado (DM automática viola os termos e derruba a conta)."""

from __future__ import annotations

import random
import re
import smtplib
import sqlite3
import ssl
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, tzinfo
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from . import db
from .messages import clean_name, greeting, instagram_handle
from .schedule import Rules, earliest_send, random_gap

EMAIL = "email"
INSTAGRAM = "instagram"
CANAIS = (EMAIL, INSTAGRAM)

PENDENTE, APROVADO, ENVIADO, REJEITADO, ERRO = "pendente", "aprovado", "enviado", "rejeitado", "erro"

_EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+(?:\.[\w-]+)+$")
_EMAIL_LIXO = re.compile(r"\.(png|jpe?g|gif|webp|svg|css|js)$|example\.|sentry|wixpress|sua@|seu@|email@|\.gov\.br|doctoralia|datasus|facebook|instagram|google|godaddy|registro\.br|locaweb|noreply|no-reply", re.I)


class CanalError(ValueError):
    pass


def valid_email(addr: str | None) -> bool:
    a = (addr or "").strip()
    return bool(a) and len(a) <= 120 and bool(_EMAIL_RE.match(a)) and not _EMAIL_LIXO.search(a)


@dataclass(frozen=True)
class CanalTemplates:
    data: Mapping[str, Any]

    @classmethod
    def load(cls, path: Path) -> CanalTemplates:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for canal in CANAIS:
            if not data.get(canal):
                raise CanalError(f"{path.name}: faltam textos para '{canal}'")
        for key in ("quem", "portfolio", "assinatura", "optout"):
            if not str(data.get(key) or "").strip():
                raise CanalError(f"{path.name}: falta '{key}'")
        return cls(data)

    def _options(self, canal: str, oferta: str) -> list[Any]:
        group = self.data[canal]
        return list(group.get(oferta) or group.get("padrao") or [])

    def render(self, canal: str, lead: Mapping[str, Any], now: datetime, tz: tzinfo,
               rng: random.Random | None = None) -> tuple[str, str]:
        """(assunto, texto) pronto para o lead; assunto vazio no Instagram."""
        rng = rng or random.Random()
        options = self._options(canal, lead.get("oferta") or "")
        if not options:
            raise CanalError(f"sem texto de {canal} para a oferta {lead.get('oferta')!r}")
        option = rng.choice(options)
        subject, body = (option["assunto"], option["texto"]) if isinstance(option, dict) else ("", option)
        saud = greeting(now.astimezone(tz))
        ctx = {
            "saudacao": saud, "saudacao_minuscula": saud[:1].lower() + saud[1:],
            "nome": clean_name(lead["nome"], lead.get("cidade")), "quem": self.data["quem"],
            "portfolio": self.data["portfolio"], "assinatura": self.data["assinatura"], "optout": self.data["optout"],
            "arroba": instagram_handle(lead.get("site")),
        }
        text = re.sub(r"\n{3,}", "\n\n", body.format(**ctx)).strip()
        return subject.format(**ctx).strip(), text


# ---------- montagem da fila ----------

@dataclass
class BuildResult:
    criados: int = 0
    itens: list[int] = field(default_factory=list)


def is_blocked(conn: sqlite3.Connection, destino: str) -> bool:
    return conn.execute("SELECT 1 FROM canal_bloqueio WHERE destino = ?", (destino.lower(),)).fetchone() is not None


def _destino(canal: str, lead: sqlite3.Row) -> str | None:
    if canal == EMAIL:
        addr = (lead["email"] or "").strip().lower()
        return addr if valid_email(addr) else None
    handle = instagram_handle(lead["site"])
    return handle.lower() or None


def build(conn: sqlite3.Connection, canal: str, limit: int, templates: CanalTemplates, now: datetime, tz: tzinfo,
          rng: random.Random | None = None) -> BuildResult:
    """Cria itens pendentes para leads 'novo' que têm o canal (e-mail público ou perfil de Instagram)."""
    if canal not in CANAIS:
        raise CanalError(f"canal desconhecido: {canal}")
    rng = rng or random.Random()
    rows = conn.execute(
        "SELECT * FROM leads WHERE status = ? AND oferta IS NOT NULL AND pais = 'BR' "
        "AND id NOT IN (SELECT lead_id FROM canal_fila) "
        "AND (telefone IS NULL OR telefone NOT IN (SELECT telefone FROM envios WHERE ok = 1 AND dry_run = 0)) "
        "ORDER BY COALESCE(avaliacoes, 0) DESC, id", (db.NOVO,)).fetchall()
    result = BuildResult()
    for lead in rows:
        if result.criados >= limit:
            break
        destino = _destino(canal, lead)
        if not destino or is_blocked(conn, destino):
            continue
        if conn.execute("SELECT 1 FROM canal_fila WHERE canal = ? AND destino = ?", (canal, destino)).fetchone():
            continue
        subject, text = templates.render(canal, dict(lead), now, tz, rng)
        with db.tx(conn):
            cur = conn.execute(
                "INSERT INTO canal_fila (lead_id, canal, destino, assunto, texto, criado_em) VALUES (?,?,?,?,?,?)",
                (lead["id"], canal, destino, subject or None, text, db.iso(now)))
            db.update_lead(conn, lead["id"], status=db.NA_FILA)
        result.criados += 1
        result.itens.append(cur.lastrowid)
    return result


def list_items(conn: sqlite3.Connection, canal: str, statuses: tuple[str, ...] = (PENDENTE, APROVADO)) -> list[sqlite3.Row]:
    marks = ",".join("?" * len(statuses))
    return conn.execute(
        f"SELECT c.*, l.nome, l.cidade, l.categoria, l.site FROM canal_fila c JOIN leads l ON l.id = c.lead_id "
        f"WHERE c.canal = ? AND c.status IN ({marks}) ORDER BY c.id", (canal, *statuses)).fetchall()


def approve(conn: sqlite3.Connection, canal: str, ids: list[int] | None = None) -> int:
    """Aprova os itens (ou todos os pendentes do canal, se `ids` for None)."""
    q = "UPDATE canal_fila SET status = ? WHERE canal = ? AND status = ?"
    args: list[Any] = [APROVADO, canal, PENDENTE]
    if ids is not None:
        if not ids:
            return 0
        q += f" AND id IN ({','.join('?' * len(ids))})"
        args += ids
    with db.tx(conn):
        return conn.execute(q, args).rowcount


def reject(conn: sqlite3.Connection, ids: list[int]) -> int:
    """Rejeita itens que ainda não saíram e devolve o lead para 'novo'."""
    n = 0
    with db.tx(conn):
        for i in ids:
            row = conn.execute("SELECT lead_id FROM canal_fila WHERE id = ? AND status IN (?, ?)",
                               (i, PENDENTE, APROVADO)).fetchone()
            if row:
                conn.execute("UPDATE canal_fila SET status = ? WHERE id = ?", (REJEITADO, i))
                db.update_lead(conn, row["lead_id"], status=db.NOVO)
                n += 1
    return n


def mark_sent(conn: sqlite3.Connection, item_id: int, when: datetime) -> bool:
    """Registra o envio (e-mail enviado pelo sistema ou DM de Instagram enviada à mão)."""
    row = conn.execute("SELECT lead_id, status FROM canal_fila WHERE id = ?", (item_id,)).fetchone()
    if not row or row["status"] in (ENVIADO, REJEITADO):
        return False
    with db.tx(conn):
        conn.execute("UPDATE canal_fila SET status = ?, enviado_em = ?, erro = NULL WHERE id = ?",
                     (ENVIADO, db.iso(when), item_id))
        db.update_lead(conn, row["lead_id"], status=db.ENVIADO, enviado_em=db.iso(when))
    return True


def block(conn: sqlite3.Connection, destino: str, motivo: str, now: datetime) -> None:
    """Quem pediu para parar (e-mail ou @perfil): nunca mais entra em fila, e o lead vira 'não quer'."""
    d = destino.strip().lower()
    with db.tx(conn):
        conn.execute("INSERT OR REPLACE INTO canal_bloqueio (destino, motivo, criado_em) VALUES (?,?,?)",
                     (d, motivo, db.iso(now)))
        for row in conn.execute("SELECT id, lead_id FROM canal_fila WHERE destino = ?", (d,)).fetchall():
            conn.execute("UPDATE canal_fila SET status = ? WHERE id = ? AND status IN (?, ?)",
                         (REJEITADO, row["id"], PENDENTE, APROVADO))
            db.update_lead(conn, row["lead_id"], status=db.NAO_QUER)


# ---------- envio de e-mail ----------

@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    nome: str


Mailer = Callable[[SmtpConfig, str, str, str], None]  # (config, destino, assunto, texto)


def smtp_send(cfg: SmtpConfig, to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = formataddr((cfg.nome, cfg.user))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Reply-To"] = cfg.user
    msg.set_content(body)
    with smtplib.SMTP(cfg.host, cfg.port, timeout=45) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(cfg.user, cfg.password)
        smtp.send_message(msg)


def email_times(conn: sqlite3.Connection, since: datetime) -> list[datetime]:
    rows = conn.execute("SELECT enviado_em FROM canal_fila WHERE canal = ? AND status = ? AND enviado_em >= ? "
                        "ORDER BY enviado_em", (EMAIL, ENVIADO, db.iso(since))).fetchall()
    return [datetime.fromisoformat(r["enviado_em"]) for r in rows]


@dataclass
class EmailReport:
    enviados: int = 0
    erros: int = 0
    parado: str | None = None
    motivo_fim: str = ""


def send_emails(conn: sqlite3.Connection, cfg: SmtpConfig, rules: Rules, max_per_day: int, *, mailer: Mailer = smtp_send,
                clock: Any, wait_next_day: bool = False, rng: random.Random | None = None,
                out: Callable[[str], None] = print, should_stop: Callable[[], bool] = lambda: False) -> EmailReport:
    """Envia os e-mails aprovados, um por vez, com janela, intervalo e teto diário próprios."""
    rng = rng or random.Random()
    rules = replace(rules, max_per_day=max_per_day, max_foreign_per_day=0)
    report = EmailReport()
    stop = db.stop_reason(conn)
    if stop:
        report.parado = stop
        report.motivo_fim = f"Sistema parado: {stop}. Rode 'prospector retomar' para liberar."
        out(report.motivo_fim)
        return report
    while not should_stop():
        item = conn.execute("SELECT c.*, l.status AS lead_status FROM canal_fila c JOIN leads l ON l.id = c.lead_id "
                            "WHERE c.canal = ? AND c.status = ? ORDER BY c.id LIMIT 1", (EMAIL, APROVADO)).fetchone()
        if item is None:
            report.motivo_fim = "Fila de e-mail vazia."
            return report
        now = clock.now()
        sent = email_times(conn, now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=2))
        when = earliest_send(now, sent, rules, random_gap(rules, rng))
        if when is None:
            if not wait_next_day:
                report.motivo_fim = f"Limite diário de e-mail ({max_per_day}) atingido."
                out(report.motivo_fim)
                return report
            when = now + timedelta(hours=1)
        if when > now:
            if not wait_next_day and (when - now).total_seconds() > rules.interval_max_s * 2:
                report.motivo_fim = "Fora do horário de envio."
                out(report.motivo_fim)
                return report
            clock.sleep((when - now).total_seconds())
            continue
        if db.stop_reason(conn):
            report.parado = db.stop_reason(conn)
            return report
        if item["lead_status"] == db.NAO_QUER or is_blocked(conn, item["destino"]):
            with db.tx(conn):
                conn.execute("UPDATE canal_fila SET status = ? WHERE id = ?", (REJEITADO, item["id"]))
            continue
        try:
            mailer(cfg, item["destino"], item["assunto"] or "", item["texto"])
        except (smtplib.SMTPException, OSError) as exc:
            with db.tx(conn):
                conn.execute("UPDATE canal_fila SET status = ?, erro = ? WHERE id = ?", (ERRO, str(exc)[:300], item["id"]))
            report.erros += 1
            # Qualquer falha de SMTP (login, bloqueio, cota) para tudo, como no WhatsApp.
            reason = f"Erro de e-mail para {item['destino']}: {exc}"
            db.set_state(conn, db.PARADO, f"{db.iso(db.utcnow())} {reason}")
            report.parado = reason
            out(f"  PARADO: {reason}")
            return report
        mark_sent(conn, item["id"], clock.now())
        report.enviados += 1
        out(f"  e-mail #{item['id']} -> {item['destino']}")
    report.motivo_fim = "Interrompido."
    return report
