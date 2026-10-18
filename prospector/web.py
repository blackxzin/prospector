"""Painel (Flask) + receptor do webhook do WA-AKG."""

from __future__ import annotations

import csv
import io
import json
import logging
import secrets
from typing import Any
from urllib.parse import urlparse

from flask import Flask, Response, abort, flash, redirect, render_template, request, url_for
from jinja2 import DictLoader

from . import db
from . import fila as fq
from .app import App
from .phone import pretty
from .replies import parse_inbound, verify_signature
from .web_html import BASE, ENVIOS, FILA, LEADS

log = logging.getLogger("prospector")

LIST_LIMIT = 1000
MANUAL_STATUSES = (db.NOVO, db.DESCARTADO, db.REJEITADO, db.ENVIADO, db.RESPONDEU, db.INTERESSADO,
                   db.NAO_QUER, db.SEM_RESPOSTA)
CSV_COLUMNS = ("id", "nome", "categoria", "endereco", "cidade", "telefone", "telefone_raw", "site", "email",
               "nota", "avaliacoes", "maps_link", "busca", "oferta", "status", "motivo", "resposta",
               "criado_em", "aprovado_em", "enviado_em", "followup_em", "respondeu_em")


def _csv_safe(value: Any) -> Any:
    """Evita injeção de fórmula ao abrir o CSV em planilha."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _filters() -> dict[str, str]:
    return {k: v for k in ("busca", "cidade", "status", "q") if (v := request.args.get(k, "").strip())}


def _query_leads(conn, f: dict[str, str], limit: int | None) -> list:
    sql, params = "SELECT * FROM leads WHERE 1=1", []
    for col in ("busca", "cidade", "status"):
        if f.get(col):
            sql += f" AND {col} = ?"
            params.append(f[col])
    if f.get("q"):
        sql += " AND nome LIKE ?"
        params.append(f"%{f['q']}%")
    sql += " ORDER BY atualizado_em DESC, id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


def create_app(app_ctx: App | None = None) -> Flask:
    ctx = app_ctx or App.create()
    web = Flask(__name__)
    web.secret_key = secrets.token_hex(32)  # só para as mensagens "flash" do painel
    web.jinja_loader = DictLoader({  # extensão .html liga o autoescape do Flask
        "base.html": BASE, "leads.html": LEADS, "fila.html": FILA, "envios.html": ENVIOS})
    web.jinja_env.globals["pretty"] = pretty

    def render(name: str, **kw: Any) -> str:
        return render_template(name, parado=db.stop_reason(ctx.conn), **kw)

    @web.before_request
    def same_origin_only() -> None:
        """Proteção simples contra CSRF nos POSTs do painel (o webhook usa HMAC)."""
        if request.method != "POST" or request.endpoint == "webhook":
            return
        origin = request.headers.get("Origin") or request.headers.get("Referer")
        if origin and urlparse(origin).netloc != request.host:
            abort(403)

    @web.get("/")
    def leads() -> str:
        f = _filters()
        conn = ctx.conn
        return render(
            "leads.html", page="leads", f=f, leads=_query_leads(conn, f, LIST_LIMIT), limite=LIST_LIMIT,
            funil=db.funnel(conn), statuses=db.STATUSES, manual_statuses=MANUAL_STATUSES,
            buscas=[r[0] for r in conn.execute("SELECT DISTINCT busca FROM leads WHERE busca IS NOT NULL ORDER BY 1")],
            cidades=[r[0] for r in conn.execute("SELECT DISTINCT cidade FROM leads WHERE cidade IS NOT NULL ORDER BY 1")],
        )

    @web.post("/leads/<int:lead_id>/status")
    def set_status(lead_id: int) -> Response:
        status = request.form.get("status", "")
        if status not in MANUAL_STATUSES:
            abort(400)
        with db.tx(ctx.conn):
            lead = db.get_lead(ctx.conn, lead_id)
            if not lead:
                abort(404)
            db.update_lead(ctx.conn, lead_id, status=status)
            if status == db.NAO_QUER and lead["telefone"]:
                db.block(ctx.conn, lead["telefone"], "marcado no painel")
        flash(f"{lead['nome']}: status -> {status}")
        return redirect(request.referrer or url_for("leads"))

    @web.get("/export.csv")
    def export_csv() -> Response:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(CSV_COLUMNS)
        for row in _query_leads(ctx.conn, _filters(), None):
            writer.writerow([_csv_safe(row[c]) for c in CSV_COLUMNS])
        return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=leads.csv"})

    @web.get("/fila")
    def fila() -> str:
        itens = fq.list_items(ctx.conn)
        return render("fila.html", page="fila", itens=itens, pendentes=sum(1 for i in itens if i["status"] == fq.PENDENTE))

    @web.post("/fila/gerar")
    def gerar_fila() -> Response:
        s = ctx.settings
        res = fq.build_queue(ctx.conn, ctx.templates, ctx.rules, db.utcnow(), size=s.max_per_day,
                             followup_days=s.followup_business_days)
        flash(f"Fila gerada: {res.primeiras} novas, {res.followups} follow-ups, {res.encerrados} encerrados.")
        return redirect(url_for("fila"))

    @web.post("/fila/aprovar-todos")
    def aprovar_todos() -> Response:
        n = fq.approve(ctx.conn, fq.pending_ids(ctx.conn))
        flash(f"{n} itens aprovados.")
        return redirect(url_for("fila"))

    @web.post("/fila/<int:item_id>/aprovar")
    def aprovar_item(item_id: int) -> Response:
        texto = request.form.get("texto")
        try:
            n = fq.approve(ctx.conn, [item_id], texto=texto)
        except ValueError as exc:
            flash(str(exc))
            return redirect(url_for("fila"))
        flash(f"#{item_id} aprovado." if n else f"#{item_id} não estava pendente.")
        return redirect(url_for("fila"))

    @web.post("/fila/<int:item_id>/rejeitar")
    def rejeitar_item(item_id: int) -> Response:
        n = fq.reject(ctx.conn, [item_id])
        flash(f"#{item_id} rejeitado." if n else f"#{item_id} não pôde ser rejeitado.")
        return redirect(url_for("fila"))

    @web.get("/envios")
    def envios() -> str:
        rows = ctx.conn.execute("SELECT * FROM envios ORDER BY id DESC LIMIT 500").fetchall()
        return render("envios.html", page="envios", envios=rows)

    @web.post("/webhook")
    def webhook() -> tuple[str, int]:
        body = request.get_data()
        if not verify_signature(ctx.settings.webhook_secret, body, request.headers.get("X-Webhook-Signature")):
            log.warning("webhook com assinatura inválida de %s", request.remote_addr)
            return "assinatura inválida", 401
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return "json inválido", 400
        if payload.get("event") == "connection.update":
            status = str((payload.get("data") or {}).get("status", "")).upper()
            if status in ("DISCONNECTED", "LOGGED_OUT", "STOPPED"):
                db.set_state(ctx.conn, db.PARADO, f"{db.iso(db.utcnow())} sessão WhatsApp: {status}")
                ctx.notify("sessão do WhatsApp caiu", f"Status: {status}. Envios parados.", urgent=True)
            return "ok", 200
        msg = parse_inbound(payload)
        if msg:
            outcome = ctx.handle_inbound(msg)
            log.info("resposta recebida: lead=%s status=%s auto=%s", outcome.lead_id, outcome.status,
                     outcome.auto_reply)
        return "ok", 200

    @web.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "parado": db.stop_reason(ctx.conn)}

    return web
