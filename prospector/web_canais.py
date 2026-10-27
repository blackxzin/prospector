"""Painel dos canais alternativos: DM do Instagram (você envia à mão) e fila de e-mail."""

from __future__ import annotations

from flask import Flask, Response, flash, redirect, render_template, request, url_for

from . import canais, db, lembretes, metrics
from .app import App


def register_canais_routes(web: Flask, ctx: App) -> None:
    @web.get("/canais")
    def canais_page() -> str:
        conn = ctx.conn
        enviados_hoje = conn.execute(
            "SELECT canal, COUNT(*) n FROM canal_fila WHERE status = 'enviado' AND enviado_em >= ? GROUP BY canal",
            (db.iso(db.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)),)).fetchall()
        return render_template(
            "canais.html", page="canais_page", resumo=metrics.channel_report(conn),
            aguardando=conn.execute(
                "SELECT c.id, c.destino, l.nome, l.status, c.enviado_em FROM canal_fila c JOIN leads l ON l.id = c.lead_id "
                "WHERE c.canal = 'instagram' AND c.status = 'enviado' AND l.status = 'enviado' ORDER BY c.enviado_em DESC LIMIT 50"
            ).fetchall(),
            instagram=canais.list_items(conn, canais.INSTAGRAM), email=canais.list_items(conn, canais.EMAIL),
            hoje={r["canal"]: r["n"] for r in enviados_hoje}, tem_email=ctx.settings.has_email,
            limite_email=ctx.settings.max_email_per_day)

    @web.get("/lembretes")
    def lembretes_page() -> str:
        now = db.utcnow()
        lembretes.ensure_defaults(ctx.conn, now, ctx.rules)
        vencidos = lembretes.due(ctx.conn, now)
        return render_template(
            "lembretes.html", page="lembretes_page", vencidos=vencidos, futuros=lembretes.upcoming(ctx.conn, now),
            textos={r["id"]: lembretes.suggestion(ctx.canal_templates.data, r) for r in vencidos})

    @web.post("/lembretes/<int:lead_id>/<acao>")
    def lembretes_acao(lead_id: int, acao: str) -> Response:
        now = db.utcnow()
        if acao == "feito":
            lembretes.done(ctx.conn, lead_id, now, ctx.rules, 3)
            flash("Anotado. Próximo lembrete em 3 dias úteis.")
        elif acao == "adiar2":
            lembretes.snooze(ctx.conn, lead_id, now, ctx.rules, 2)
        elif acao == "adiar7":
            lembretes.snooze(ctx.conn, lead_id, now, ctx.rules, 5)
        elif acao == "encerrar":
            lembretes.finish(ctx.conn, lead_id)
            flash("Sem mais lembretes para esse lead.")
        return redirect(url_for("lembretes_page"))

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
        elif acao in ("respondeu", "interessado"):
            row = ctx.conn.execute("SELECT lead_id FROM canal_fila WHERE id = ?", (item_id,)).fetchone()
            if row:
                with db.tx(ctx.conn):
                    db.update_lead(ctx.conn, row["lead_id"], status=db.INTERESSADO if acao == "interessado" else db.RESPONDEU,
                                   respondeu_em=db.iso(db.utcnow()), resposta_humana=1)
                flash("Resposta registrada.")
        elif acao == "parar":
            row = ctx.conn.execute("SELECT destino FROM canal_fila WHERE id = ?", (item_id,)).fetchone()
            if row:
                canais.block(ctx.conn, row["destino"], "pediu para parar", db.utcnow())
                flash(f"{row['destino']} bloqueado.")
        return redirect(url_for("canais_page"))
