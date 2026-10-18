"""Painel (Flask) + receptor do webhook do WA-AKG."""

from __future__ import annotations

import csv
import io
import json
import logging
import secrets
from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse

from flask import Flask, Response, abort, flash, redirect, render_template, request, url_for

from . import db
from . import fila as fq
from .app import App
from .finance import brl
from .phone import pretty
from .replies import parse_inbound, verify_signature

log = logging.getLogger("prospector")

LIST_LIMIT = 1000
MANUAL_STATUSES = (db.NOVO, db.DESCARTADO, db.REJEITADO, db.ENVIADO, db.RESPONDEU, db.INTERESSADO,
                   db.CLIENTE, db.NAO_QUER, db.SEM_RESPOSTA)
CSV_COLUMNS = ("id", "nome", "categoria", "endereco", "cidade", "telefone", "telefone_raw", "site", "email",
               "nota", "avaliacoes", "maps_link", "busca", "oferta", "status", "motivo", "resposta",
               "criado_em", "aprovado_em", "enviado_em", "followup_em", "respondeu_em")
MESES_LONGOS = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro",
                "outubro", "novembro", "dezembro")
DIAS_LONGOS = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo")
STATUS_COLORS = {
    db.NOVO: "blue", db.NA_FILA: "blue", db.ENVIADO: "blue", db.FOLLOW_UP: "blue",
    db.RESPONDEU: "amber", db.INTERESSADO: "green", db.CLIENTE: "green",
    db.NAO_QUER: "red", db.ERRO: "red",
}
PROJECT_COLORS = {"em_andamento": "blue", "entregue": "green", "cancelado": "red"}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    # sem JS no painel; style inline só para larguras de barras
    "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                               "script-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
}


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


def today_local(ctx: App) -> date:
    return db.utcnow().astimezone(ctx.settings.tz).date()


def data_br(value: str | None) -> str:
    if not value:
        return ""
    try:
        return date.fromisoformat(value[:10]).strftime("%d/%m/%Y")
    except ValueError:
        return value


def date_long(d: date) -> str:
    return f"{DIAS_LONGOS[d.weekday()]}, {d.day} de {MESES_LONGOS[d.month - 1]} de {d.year}"


def _register_helpers(web: Flask, ctx: App) -> None:
    web.jinja_env.globals.update(
        pretty=pretty, brl=brl, data_br=data_br,
        status_color=lambda s: STATUS_COLORS.get(s, ""),
        project_color=lambda s: PROJECT_COLORS.get(s, ""),
        mes_nome=lambda d: f"{MESES_LONGOS[d.month - 1]} de {d.year}",
        quando=lambda iso: datetime.fromisoformat(iso).astimezone(ctx.settings.tz).strftime("%d/%m %H:%M") if iso else "",
    )

    @web.context_processor
    def nav_context() -> dict[str, Any]:
        conn = ctx.conn
        return {
            "parado": db.stop_reason(conn),
            "nav_counts": {
                "fila": conn.execute("SELECT COUNT(*) FROM fila WHERE status = 'pendente'").fetchone()[0],
                "projetos": conn.execute("SELECT COUNT(*) FROM projetos WHERE status = 'em_andamento'").fetchone()[0],
                "alertas": conn.execute("SELECT COUNT(*) FROM leads WHERE sinal_fechamento = 1 "
                                        "AND status != 'cliente'").fetchone()[0],
            },
        }


def create_app(app_ctx: App | None = None) -> Flask:
    ctx = app_ctx or App.create()
    web = Flask(__name__)
    web.secret_key = secrets.token_hex(32)  # só para as mensagens "flash" do painel
    _register_helpers(web, ctx)

    @web.before_request
    def same_origin_only() -> None:
        """Proteção simples contra CSRF nos POSTs do painel (o webhook usa HMAC)."""
        if request.method != "POST" or request.endpoint == "webhook":
            return
        origin = request.headers.get("Origin") or request.headers.get("Referer")
        if origin and urlparse(origin).netloc != request.host:
            abort(403)

    @web.after_request
    def security_headers(resp: Response) -> Response:
        for k, v in SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        return resp

    @web.get("/leads")
    def leads() -> str:
        f = _filters()
        conn = ctx.conn
        return render_template(
            "leads.html", page="leads", f=f, leads=_query_leads(conn, f, LIST_LIMIT), limite=LIST_LIMIT,
            statuses=db.STATUSES, manual_statuses=MANUAL_STATUSES,
            buscas=[r[0] for r in conn.execute("SELECT DISTINCT busca FROM leads WHERE busca IS NOT NULL ORDER BY 1")],
            cidades=[r[0] for r in conn.execute("SELECT DISTINCT cidade FROM leads WHERE cidade IS NOT NULL ORDER BY 1")],
        )

    @web.post("/leads/<int:lead_id>/status")
    def set_status(lead_id: int) -> Response:
        status = request.form.get("status", "")
        if status not in MANUAL_STATUSES:
            abort(400)
        if status == db.CLIENTE:
            return redirect(url_for("projeto_novo", lead=lead_id))
        with db.tx(ctx.conn):
            lead = db.get_lead(ctx.conn, lead_id)
            if not lead:
                abort(404)
            db.update_lead(ctx.conn, lead_id, status=status)
            if status == db.NAO_QUER and lead["telefone"]:
                db.block(ctx.conn, lead["telefone"], "marcado no painel")
        flash(f"{lead['nome']}: status alterado para {status}.")
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
        return render_template("fila.html", page="fila", itens=itens,
                               pendentes=sum(1 for i in itens if i["status"] == fq.PENDENTE))

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
        try:
            n = fq.approve(ctx.conn, [item_id], texto=request.form.get("texto"))
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
        return render_template("envios.html", page="envios", envios=rows)

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
            log.info("resposta recebida: lead=%s status=%s auto=%s fechamento=%s", outcome.lead_id,
                     outcome.status, outcome.auto_reply, outcome.closing)
        return "ok", 200

    @web.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "parado": db.stop_reason(ctx.conn)}

    from .web_finance import register_finance_routes
    register_finance_routes(web, ctx)
    return web
