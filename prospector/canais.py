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
from .schedule import Rules, add_business_days, earliest_send, random_gap

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

    def render_followup(self, lead: Mapping[str, Any], original_subject: str, rng: random.Random | None = None) -> tuple[str, str]:
        options = list(self.data.get("email_followup") or [])
        if not options:
            raise CanalError("faltam textos em 'email_followup'")
        ctx = {"nome": clean_name(lead["nome"], lead.get("cidade")), "quem": self.data["quem"],
               "portfolio": self.data["portfolio"], "assinatura": self.data["assinatura"], "optout": self.data["optout"]}
        text = re.sub(r"\n{3,}", "\n\n", (rng or random.Random()).choice(options).format(**ctx)).strip()
        return f"Re: {original_subject}", text

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


def block(conn: sqlite3.Connection, destino: str, motivo: str, now: datetime, lead_status: str = db.NAO_QUER) -> None:
    """Destino que nunca mais entra em fila (e-mail ou @perfil). Por padrão o lead vira 'não quer' (pediu para parar)."""
    d = destino.strip().lower()
    with db.tx(conn):
        conn.execute("INSERT OR REPLACE INTO canal_bloqueio (destino, motivo, criado_em) VALUES (?,?,?)",
                     (d, motivo, db.iso(now)))
        for row in conn.execute("SELECT id, lead_id FROM canal_fila WHERE destino = ?", (d,)).fetchall():
            conn.execute("UPDATE canal_fila SET status = ? WHERE id = ? AND status IN (?, ?)",
                         (REJEITADO, row["id"], PENDENTE, APROVADO))
            db.update_lead(conn, row["lead_id"], status=lead_status)


# ---------- follow-up de e-mail (um lembrete só, depois de N dias úteis sem resposta) ----------

def build_followups(conn: sqlite3.Connection, templates: CanalTemplates, rules: Rules, now: datetime, business_days: int,
                    rng: random.Random | None = None) -> int:
    rows = conn.execute(
        "SELECT c.id, c.assunto, c.enviado_em, l.* FROM canal_fila c JOIN leads l ON l.id = c.lead_id "
        "WHERE c.canal = ? AND c.status = ? AND c.followup_status IS NULL AND l.status = ? AND l.respondeu_em IS NULL",
        (EMAIL, ENVIADO, db.ENVIADO)).fetchall()
    made = 0
    for row in rows:
        sent = db.parse_iso(row["enviado_em"])
        if not sent or add_business_days(sent, business_days, rules) > now:
            continue
        subject, text = templates.render_followup(dict(row), row["assunto"] or "", rng)
        with db.tx(conn):
            conn.execute("UPDATE canal_fila SET followup_status = ?, followup_assunto = ?, followup_texto = ? WHERE id = ?",
                         (PENDENTE, subject, text, row["id"]))
        made += 1
    return made


def approve_followups(conn: sqlite3.Connection) -> int:
    with db.tx(conn):
        return conn.execute("UPDATE canal_fila SET followup_status = ? WHERE canal = ? AND followup_status = ?",
                            (APROVADO, EMAIL, PENDENTE)).rowcount


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


def send_test(conn: sqlite3.Connection, cfg: SmtpConfig, to: str, templates: CanalTemplates, now: datetime, tz: tzinfo,
              *, mailer: Mailer = smtp_send) -> tuple[str, str]:
    """Manda só para `to` o 1º e-mail da fila (ou um exemplo), com [TESTE] no assunto; não mexe na fila.

    Erro de SMTP aciona a parada de emergência, como no envio real."""
    item = conn.execute("SELECT assunto, texto FROM canal_fila WHERE canal = ? AND status IN (?, ?) ORDER BY id LIMIT 1",
                        (EMAIL, PENDENTE, APROVADO)).fetchone()
    if item:
        subject, text = item["assunto"] or "", item["texto"]
    else:
        subject, text = templates.render(EMAIL, {"nome": "Negócio Exemplo", "oferta": "tem_site"}, now, tz)
    subject = f"[TESTE] {subject}"
    try:
        mailer(cfg, to, subject, text)
    except (smtplib.SMTPException, OSError) as exc:
        reason = f"Erro no teste de e-mail: {exc}"
        db.set_state(conn, db.PARADO, f"{db.iso(db.utcnow())} {reason}")
        raise CanalError(reason) from exc
    return subject, text


def email_times(conn: sqlite3.Connection, since: datetime) -> list[datetime]:
    rows = conn.execute(
        "SELECT enviado_em AS t FROM canal_fila WHERE canal = ? AND status = ? AND enviado_em >= ? "
        "UNION ALL SELECT followup_em FROM canal_fila WHERE canal = ? AND followup_status = ? AND followup_em >= ? "
        "ORDER BY 1", (EMAIL, ENVIADO, db.iso(since), EMAIL, ENVIADO, db.iso(since))).fetchall()
    return [datetime.fromisoformat(r["t"]) for r in rows]


@dataclass
class EmailReport:
    enviados: int = 0
    erros: int = 0
    parado: str | None = None
    motivo_fim: str = ""


def _next_email(conn: sqlite3.Connection) -> tuple[sqlite3.Row | None, bool]:
    """Próximo e-mail aprovado: 1ª mensagem primeiro, depois lembretes. (linha, é_lembrete)"""
    base = "SELECT c.*, l.status AS lead_status FROM canal_fila c JOIN leads l ON l.id = c.lead_id WHERE c.canal = ? AND "
    row = conn.execute(base + "c.status = ? ORDER BY c.id LIMIT 1", (EMAIL, APROVADO)).fetchone()
    if row:
        return row, False
    row = conn.execute(base + "c.followup_status = ? ORDER BY c.id LIMIT 1", (EMAIL, APROVADO)).fetchone()
    return row, True


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
        item, followup = _next_email(conn)
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
        gone = item["lead_status"] == db.NAO_QUER or is_blocked(conn, item["destino"])
        if gone or (followup and item["lead_status"] != db.ENVIADO):   # respondeu/pediu para parar: lembrete cai
            with db.tx(conn):
                if followup:
                    conn.execute("UPDATE canal_fila SET followup_status = 'cancelado' WHERE id = ?", (item["id"],))
                else:
                    conn.execute("UPDATE canal_fila SET status = ? WHERE id = ?", (REJEITADO, item["id"]))
            continue
        subject, body = ((item["followup_assunto"], item["followup_texto"]) if followup
                         else (item["assunto"] or "", item["texto"]))
        try:
            mailer(cfg, item["destino"], subject, body)
        except (smtplib.SMTPException, OSError) as exc:
            with db.tx(conn):
                if followup:
                    conn.execute("UPDATE canal_fila SET followup_status = 'erro', erro = ? WHERE id = ?", (str(exc)[:300], item["id"]))
                else:
                    conn.execute("UPDATE canal_fila SET status = ?, erro = ? WHERE id = ?", (ERRO, str(exc)[:300], item["id"]))
            report.erros += 1
            # Qualquer falha de SMTP (login, bloqueio, cota) para tudo, como no WhatsApp.
            reason = f"Erro de e-mail para {item['destino']}: {exc}"
            db.set_state(conn, db.PARADO, f"{db.iso(db.utcnow())} {reason}")
            report.parado = reason
            out(f"  PARADO: {reason}")
            return report
        if followup:
            with db.tx(conn):
                conn.execute("UPDATE canal_fila SET followup_status = ?, followup_em = ? WHERE id = ?",
                             (ENVIADO, db.iso(clock.now()), item["id"]))
        else:
            mark_sent(conn, item["id"], clock.now())
        report.enviados += 1
        out(f"  e-mail{' (lembrete)' if followup else ''} #{item['id']} -> {item['destino']}")
    report.motivo_fim = "Interrompido."
    return report
