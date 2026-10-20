"""Monta as peças (config, banco, templates, clientes) e expõe os casos de uso."""

from __future__ import annotations

import logging
import random
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import cached_property
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from . import db, lote, pix, sitecheck
from .classify import Listas
from .finance import FinanceConfig, brl
from .config import Settings, load_settings
from .importer import ImportResult, import_rows, read_csv_text
from .messages import TemplateSet, Templates
from .mundo import BRASIL, MundoConfig, Origem
from .nichos import Nichos
from .notify import notify
from .phone import to_jid
from .presenca import Verificador
from .replies import Inbound, ReplyOutcome, handle_inbound
from .schedule import Rules, in_window, typing_seconds
from .scraper import GMapsClient
from .sender import RealClock, type_and_send
from .whatsapp import WaClient, WhatsAppError

log = logging.getLogger("prospector")

AUTO_REPLY_MAX_AGE = timedelta(minutes=30)  # não responde "fora do horário" a mensagem velha


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
    def template_set(self) -> TemplateSet:
        return TemplateSet.load(self.settings.templates_path)

    @property
    def templates(self) -> Templates:
        """Textos em português (Brasil): padrão do sistema."""
        return self.template_set.default

    @cached_property
    def rules(self) -> Rules:
        return Rules.from_settings(self.settings)

    @cached_property
    def gmaps(self) -> GMapsClient:
        return GMapsClient(self.settings.gmaps_url)

    def presence_checker(self, force: bool = False) -> Verificador | None:
        """Busca de presença online antes da 1ª mensagem (desligue com CHECAR_PRESENCA=0)."""
        return Verificador(self.listas) if force or self.settings.checar_presenca else None

    @cached_property
    def nichos(self) -> Nichos | None:
        """Catálogo de nichos (nichos.yaml); sem o arquivo o sistema funciona sem marcar nicho."""
        try:
            return Nichos.load(self.settings.nichos_path)
        except ValueError:
            return None

    @cached_property
    def mundo(self) -> MundoConfig:
        return MundoConfig.load(self.settings.mundo_path)

    def whatsapp(self) -> WaClient:
        s = self.settings
        if not s.has_whatsapp:
            raise RuntimeError("Configure WAAKG_SESSION e WAAKG_API_KEY no .env")
        return WaClient(s.waakg_url, s.waakg_session, s.waakg_api_key)

    def notify(self, title: str, body: str, urgent: bool = False) -> None:
        notify(f"Prospector: {title}", body, urgent=urgent, enabled=self.settings.notify)

    # ---------- busca ----------

    def search(self, query: str, out: Callable[[str], None] = print, origem: Origem = BRASIL) -> ImportResult:
        s = self.settings
        job_id = self.gmaps.create_job(query, depth=s.gmaps_depth, max_time_s=s.gmaps_max_time_s,
                                       lang=origem.idioma)
        out(f"Job criado no scraper: {job_id} (leva no mínimo 3 minutos)")
        self.gmaps.wait(job_id, timeout_s=s.gmaps_max_time_s + 900,
                        on_tick=lambda st, w: out(f"  [{w // 60:02d}:{w % 60:02d}] status: {st or '?'}"))
        csv_text = self.gmaps.download_csv(job_id)
        csv_dir = s.data_dir / "csv"
        csv_dir.mkdir(parents=True, exist_ok=True)
        (csv_dir / f"{job_id}.csv").write_text(csv_text, encoding="utf-8")
        result = import_rows(self.conn, read_csv_text(csv_text), query, self.listas, origem, self.nichos)
        lote.record(self.conn, query, result)
        checked, bad = sitecheck.check_leads(self.conn, out=out)
        if checked:
            out(f"Sites checados: {checked}, com problema: {bad} (viram oferta site_problema)")
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

    def handle_inbound(self, msg: Inbound, now: datetime | None = None, background: bool = True,
                       received_at: datetime | None = None) -> ReplyOutcome:
        """`received_at`: quando a mensagem chegou de fato (polling). Resposta automática só para mensagem recente."""
        now = now or db.utcnow()
        at = received_at or now
        recent = now - at <= AUTO_REPLY_MAX_AGE
        outcome = handle_inbound(
            self.conn, msg, now=at, in_business_hours=in_window(at, self.rules) or not recent,
            notify_fn=lambda t, b, urgent: self.notify(t, b, urgent),
        )
        if outcome.auto_reply and outcome.lead_id is not None:
            if background:
                threading.Thread(target=self._auto_reply, args=(outcome.lead_id,), daemon=True).start()
            else:
                self._auto_reply(outcome.lead_id)
        return outcome

    def _auto_reply(self, lead_id: int) -> None:
        """Resposta curta fora do horário (uma vez por contato; já reservada em handle_inbound)."""
        conn = db.connect(self.settings.db_path)  # conexão própria: roda em outra thread
        try:
            lead = db.get_lead(conn, lead_id)
            if not lead or db.stop_reason(conn) or not self.settings.has_whatsapp:
                return
            now = db.utcnow()
            tpl = self.template_set.get(lead["idioma"])
            lead_tz = ZoneInfo(lead["fuso"]) if lead["fuso"] else self.settings.tz
            text = tpl.out_of_hours(dict(lead), now, lead_tz)
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
