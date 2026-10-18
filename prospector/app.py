"""Monta as peças (config, banco, templates, clientes) e expõe os casos de uso."""

from __future__ import annotations

import logging
import random
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime
from functools import cached_property
from pathlib import Path
from typing import Callable

from . import db, demo, fila, lote, pix, sitecheck
from .classify import Listas
from .finance import FinanceConfig, brl
from .config import Settings, load_settings
from .importer import ImportResult, import_rows, read_csv_text
from .messages import Templates
from .notify import notify
from .phone import to_jid
from .replies import Inbound, ReplyOutcome, handle_inbound
from .schedule import Rules, in_window, typing_seconds
from .scraper import GMapsClient
from .sender import RealClock, type_and_send
from .whatsapp import WaClient, WhatsAppError

log = logging.getLogger("prospector")


@dataclass
class App:
    settings: Settings

    @classmethod
    def create(cls, env_file: Path | None = None) -> App:
        return cls(load_settings(env_file))

    @cached_property
    def conn(self) -> sqlite3.Connection:
        return db.connect(self.settings.db_path)

    @cached_property
    def listas(self) -> Listas:
        return Listas.load(self.settings.listas_path)

    @cached_property
    def finance(self) -> FinanceConfig:
        return FinanceConfig.load(self.settings.financeiro_path)

    @cached_property
    def templates(self) -> Templates:
        return Templates.load(self.settings.templates_path)

    @cached_property
    def rules(self) -> Rules:
        return Rules.from_settings(self.settings)

    @cached_property
    def gmaps(self) -> GMapsClient:
        return GMapsClient(self.settings.gmaps_url)

    def whatsapp(self) -> WaClient:
        s = self.settings
        if not s.has_whatsapp:
            raise RuntimeError("Configure WAAKG_SESSION e WAAKG_API_KEY no .env")
        return WaClient(s.waakg_url, s.waakg_session, s.waakg_api_key)

    def notify(self, title: str, body: str, urgent: bool = False) -> None:
        notify(f"Prospector: {title}", body, urgent=urgent, enabled=self.settings.notify)

    # ---------- busca ----------

    def search(self, query: str, out: Callable[[str], None] = print) -> ImportResult:
        s = self.settings
        job_id = self.gmaps.create_job(query, depth=s.gmaps_depth, max_time_s=s.gmaps_max_time_s)
        out(f"Job criado no scraper: {job_id} (leva no mínimo 3 minutos)")
        self.gmaps.wait(job_id, timeout_s=s.gmaps_max_time_s + 900,
                        on_tick=lambda st, w: out(f"  [{w // 60:02d}:{w % 60:02d}] status: {st or '?'}"))
        csv_text = self.gmaps.download_csv(job_id)
        csv_dir = s.data_dir / "csv"
        csv_dir.mkdir(parents=True, exist_ok=True)
        (csv_dir / f"{job_id}.csv").write_text(csv_text, encoding="utf-8")
        result = import_rows(self.conn, read_csv_text(csv_text), query, self.listas)
        lote.record(self.conn, query, result)
        checked, bad = sitecheck.check_leads(self.conn, out=out)
        if checked:
            out(f"Sites checados: {checked}, com problema: {bad} (viram oferta site_problema)")
        return result

    # ---------- site de exemplo ----------

    def make_demo(self, lead_id: int, *, queue_message: bool = True) -> demo.DemoResult:
        """Gera o site de exemplo do lead e, se houver DEMO_BASE_URL, põe a mensagem com o link na fila."""
        s = self.settings
        result = demo.generate(self.conn, lead_id, s.demos_dir, s.demo_base_url, str(self.templates.data["remetente"]))
        if queue_message and result.url:
            lead = db.get_lead(self.conn, lead_id)
            text = self.templates.demo_message(dict(lead), result.url, db.utcnow(), s.tz)
            fila.queue_demo(self.conn, lead_id, text, db.utcnow(), s.tz)
        return result

    def proposal_pdf(self, proj: dict) -> bytes:
        from . import proposta
        s = self.settings
        preco = self.finance.precos.get(proj["tipo"])
        return proposta.build(
            proj, autor=str(self.templates.data["remetente"]), inclui=preco.inclui if preco else (),
            entrada_percentual=self.finance.entrada_percentual,
            pix_info=proposta.PixInfo(s.pix_chave, s.pix_nome, s.pix_cidade),
            today=db.utcnow().astimezone(s.tz).date(),
        )

    def pix_for(self, valor_centavos: int, txid: str) -> str | None:
        s = self.settings
        try:
            return pix.payload(s.pix_chave, s.pix_nome, s.pix_cidade, valor_centavos, txid=txid)
        except pix.PixError:
            return None

    def charge_text(self, lead, payment) -> str:
        """Mensagem de cobrança com Pix copia e cola da parcela (se a chave Pix estiver configurada)."""
        s = self.settings
        try:
            code = pix.payload(s.pix_chave, s.pix_nome, s.pix_cidade, payment["valor_centavos"],
                               txid=f"P{payment['id']}")
        except pix.PixError:
            code = "(me avisa que eu te mando a chave Pix)"
        venc = date.fromisoformat(payment["vencimento"]).strftime("%d/%m")
        return self.templates.charge_message(dict(lead), valor=brl(payment["valor_centavos"]), vencimento=venc,
                                             descricao=payment["descricao"], pix=code, now=db.utcnow(), tz=s.tz)

    # ---------- respostas ----------

    def handle_inbound(self, msg: Inbound, now: datetime | None = None,
                       background: bool = True) -> ReplyOutcome:
        now = now or db.utcnow()
        outcome = handle_inbound(
            self.conn, msg, now=now, in_business_hours=in_window(now, self.rules),
            notify_fn=lambda t, b, urgent: self.notify(t, b, urgent),
        )
        if outcome.status == db.INTERESSADO and not outcome.closing and outcome.lead_id is not None:
            self._demo_for_interested(outcome.lead_id)
        if outcome.auto_reply and outcome.lead_id is not None:
            if background:
                threading.Thread(target=self._auto_reply, args=(outcome.lead_id,), daemon=True).start()
            else:
                self._auto_reply(outcome.lead_id)
        return outcome

    def _demo_for_interested(self, lead_id: int) -> None:
        """Lead interessado sem exemplo ainda: gera o site e deixa a mensagem na fila para aprovação."""
        lead = db.get_lead(self.conn, lead_id)
        if not lead or lead["demo_url"]:
            return
        try:
            result = self.make_demo(lead_id)
        except Exception as exc:  # noqa: BLE001 - falha aqui não pode derrubar o webhook
            log.exception("falha ao gerar exemplo do lead %s", lead_id)
            self.notify("erro ao gerar exemplo", str(exc))
            return
        if result.url:
            self.notify(f"exemplo pronto para {lead['nome']}",
                        "Mensagem com o link está na fila. Publique (prospector demo publicar) e aprove.")
        else:
            self.notify(f"exemplo gerado para {lead['nome']}",
                        "Configure DEMO_BASE_URL para o link ir na mensagem automaticamente.")

    def _auto_reply(self, lead_id: int) -> None:
        """Resposta curta fora do horário (uma vez por contato; já reservada em handle_inbound)."""
        conn = db.connect(self.settings.db_path)  # conexão própria: roda em outra thread
        try:
            lead = db.get_lead(conn, lead_id)
            if not lead or db.stop_reason(conn) or not self.settings.has_whatsapp:
                return
            now = db.utcnow()
            text = self.templates.out_of_hours(dict(lead), now, self.settings.tz)
            jid = lead["jid"] or to_jid(lead["telefone"])
            s = self.settings
            typing_s = typing_seconds(text, s.typing_min_s, s.typing_max_s, random.Random())
            try:
                result = type_and_send(self.whatsapp(), RealClock(), jid, text, typing_s)
                with db.tx(conn):
                    db.log_send(conn, lead_id=lead_id, fila_id=None, telefone=lead["telefone"],
                                tipo=db.AUTO_RESPOSTA, texto=text, dry_run=False, ok=True,
                                http_status=getattr(result, "status", None),
                                resposta_api=str(getattr(result, "body", ""))[:2000])
            except WhatsAppError as exc:
                with db.tx(conn):
                    db.log_send(conn, lead_id=lead_id, fila_id=None, telefone=lead["telefone"],
                                tipo=db.AUTO_RESPOSTA, texto=text, dry_run=False, ok=False,
                                http_status=exc.status, resposta_api=(exc.body or str(exc))[:2000])
                if exc.fatal:
                    db.set_state(conn, db.PARADO, f"{db.iso(db.utcnow())} auto-resposta: {exc}")
                self.notify("erro na resposta automática", str(exc), urgent=True)
        finally:
            conn.close()
