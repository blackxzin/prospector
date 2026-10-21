"""Painel dos canais alternativos: DM do Instagram (você envia à mão) e fila de e-mail."""

from __future__ import annotations

from flask import Flask, Response, flash, redirect, render_template, request, url_for

from . import canais, db
from .app import App


def register_canais_routes(web: Flask, ctx: App) -> None:
    @web.get("/canais")
    def canais_page() -> str:
        conn = ctx.conn
        enviados_hoje = conn.execute(
            "SELECT canal, COUNT(*) n FROM canal_fila WHERE status = 'enviado' AND enviado_em >= ? GROUP BY canal",
            (db.iso(db.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)),)).fetchall()
        return render_template(
            "canais.html", page="canais_page",
            instagram=canais.list_items(conn, canais.INSTAGRAM), email=canais.list_items(conn, canais.EMAIL),
            hoje={r["canal"]: r["n"] for r in enviados_hoje}, tem_email=ctx.settings.has_email,
            limite_email=ctx.settings.max_email_per_day)

    @web.post("/canais/<canal>/montar")
    def canais_montar(canal: str) -> Response:
        if canal not in canais.CANAIS:
            return redirect(url_for("canais_page"))
        res = canais.build(ctx.conn, canal, 15, ctx.canal_templates, db.utcnow(), ctx.settings.tz)
        flash(f"{res.criados} itens de {canal} criados.")
        return redirect(url_for("canais_page"))

    @web.post("/canais/<canal>/aprovar-todos")
    def canais_aprovar_todos(canal: str) -> Response:
        if canal in canais.CANAIS:
            flash(f"{canais.approve(ctx.conn, canal)} aprovados.")
        return redirect(url_for("canais_page"))

    @web.post("/canais/item/<int:item_id>/<acao>")
    def canais_item(item_id: int, acao: str) -> Response:
        if acao == "aprovar":
            canais.approve(ctx.conn, request.form.get("canal", ""), [item_id])
        elif acao == "enviado":
            canais.mark_sent(ctx.conn, item_id, db.utcnow())
            flash("Marcado como enviado.")
        elif acao == "pular":
            canais.reject(ctx.conn, [item_id])
        elif acao == "parar":
            row = ctx.conn.execute("SELECT destino FROM canal_fila WHERE id = ?", (item_id,)).fetchone()
            if row:
                canais.block(ctx.conn, row["destino"], "pediu para parar", db.utcnow())
                flash(f"{row['destino']} bloqueado.")
        return redirect(url_for("canais_page"))
