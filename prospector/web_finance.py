"""Rotas do painel: visão geral (gerenciador), projetos e financeiro."""

from __future__ import annotations

from datetime import datetime, timedelta

import re

from flask import Flask, Response, abort, flash, redirect, render_template, request, url_for

from . import backup, db, lote, pix
from . import finance as fin
from . import fila as fq
from . import insights
from .app import App
from .messages import variant_id
from .charts import revenue_chart


def _money_field(name: str, default: str = "0") -> int:
    return fin.parse_money(request.form.get(name, "").strip() or default)


def _back(default: str) -> Response:
    """Volta para o projeto informado em 'volta', ou para a página de origem."""
    volta = request.form.get("volta", "")
    if volta.isdigit():
        return redirect(url_for("projeto", project_id=int(volta)))
    return redirect(request.referrer or default)


def _reais(cents: int) -> str:
    return f"{cents / 100:.2f}".replace(".", ",")


def _extra_alerts(ctx: App) -> list[insights.Alerta]:
    alert = insights.backup_alert(backup.last_backup_time(ctx.settings.backup_dir), datetime.now())
    return [alert] if alert else []


def _ab_rows(ctx: App) -> list[dict]:
    stats = fq.ab_stats(ctx.conn)
    rows = []
    for oferta, options in ctx.templates.data["primeira"].items():
        for text in options:
            sent, pos = stats.get(variant_id(text), (0, 0))
            if sent:
                rows.append({"oferta": oferta, "texto": text, "enviados": sent, "positivos": pos,
                             "taxa": pos / sent})
    return sorted(rows, key=lambda r: (-r["taxa"], -r["enviados"]))


def _batch_progress(ctx: App) -> dict | None:
    try:
        cfg = lote.BatchConfig.load(ctx.settings.buscas_path)
    except ValueError:
        return None
    total = len(cfg.queries())
    return {"total": total, "pendentes": len(lote.pending(ctx.conn, cfg))}


def register_finance_routes(web: Flask, ctx: App) -> None:
    from .web import today_local

    # ---------- projetos ----------

    @web.get("/projetos")
    def projetos() -> str:
        fechamentos = ctx.conn.execute(
            "SELECT * FROM leads WHERE sinal_fechamento = 1 AND status != 'cliente' ORDER BY respondeu_em DESC"
        ).fetchall()
        return render_template("projetos.html", page="projetos", projetos=fin.projects(ctx.conn),
                               fechamentos=fechamentos)

    @web.get("/projetos/novo")
    def projeto_novo() -> str:
        lead = None
        lead_id = request.args.get("lead", "")
        if lead_id.isdigit():
            lead = db.get_lead(ctx.conn, int(lead_id))
        tipo, preco = ctx.finance.defaults_for(lead["oferta"] if lead else None)
        hoje = today_local(ctx)
        form = {
            "cliente": lead["nome"] if lead else "", "tipo": tipo, "valor": _reais(preco.valor),
            "fechado_em": hoje.isoformat(), "prazo": (hoje + timedelta(days=preco.prazo_dias)).isoformat(),
            "entrada": ctx.finance.entrada_percentual, "mensalidade": _reais(preco.mensalidade),
            "custo": _reais(preco.custo_estimado), "descricao": preco.descricao,
        }
        return render_template("projeto_novo.html", page="projetos", lead=lead, form=form, tipos=fin.TIPOS)

    @web.post("/projetos")
    def projeto_criar() -> Response:
        lead_id = request.form.get("lead_id", "")
        try:
            prazo = request.form.get("prazo", "").strip()
            novo = fin.NovoProjeto(
                cliente=request.form.get("cliente", ""), tipo=request.form.get("tipo", ""),
                valor=_money_field("valor", ""), fechado_em=fin.parse_date(request.form.get("fechado_em"), "Fechado em"),
                prazo=fin.parse_date(prazo, "Prazo") if prazo else None,
                descricao=request.form.get("descricao", ""), custo_estimado=_money_field("custo"),
                mensalidade=_money_field("mensalidade"),
                lead_id=int(lead_id) if lead_id.isdigit() else None,
                entrada_percentual=int(request.form.get("entrada", "50") or 50),
            )
            project_id = fin.create_project(ctx.conn, novo)
        except (fin.FinanceError, ValueError) as exc:
            flash(f"Não deu para registrar: {exc}")
            return redirect(url_for("projeto_novo", lead=lead_id) if lead_id else url_for("projeto_novo"))
        flash(f"Projeto de {novo.cliente} registrado. Parcelas criadas.")
        return redirect(url_for("projeto", project_id=project_id))

    @web.get("/projetos/<int:project_id>")
    def projeto(project_id: int) -> str:
        p = fin.project(ctx.conn, project_id)
        if not p:
            abort(404)
        pagamentos = fin.payments(ctx.conn, project_id)
        pix_codes = {}
        for g in pagamentos:
            if not g["pago_em"]:
                code = ctx.pix_for(g["valor_centavos"], f"P{g['id']}")
                if code:
                    pix_codes[g["id"]] = {"code": code, "svg": pix.qr_svg(code, 150)}
        return render_template("projeto.html", page="projetos", p=p, pagamentos=pagamentos, pix_codes=pix_codes,
                               pix_ok=bool(ctx.settings.pix_chave), despesas=fin.expenses(ctx.conn, project_id),
                               hoje=today_local(ctx).isoformat(), categorias=ctx.finance.categorias_despesa)

    @web.get("/projetos/<int:project_id>/proposta.pdf")
    def proposta_pdf(project_id: int) -> Response:
        p = fin.project(ctx.conn, project_id)
        if not p:
            abort(404)
        name = re.sub(r"[^a-z0-9]+", "-", p["cliente"].lower()).strip("-") or "cliente"
        return Response(ctx.proposal_pdf(p), mimetype="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="proposta-{name}.pdf"'})

    @web.post("/projetos/<int:project_id>/status")
    def projeto_status(project_id: int) -> Response:
        try:
            fin.set_project_status(ctx.conn, project_id, request.form.get("status", ""), today_local(ctx))
        except fin.FinanceError as exc:
            flash(str(exc))
        return redirect(url_for("projeto", project_id=project_id))

    @web.post("/projetos/<int:project_id>/pagamentos")
    def pagamento_novo(project_id: int) -> Response:
        try:
            fin.add_payment(ctx.conn, project_id, request.form.get("descricao", ""), _money_field("valor", ""),
                            fin.parse_date(request.form.get("vencimento"), "Vencimento"))
        except fin.FinanceError as exc:
            flash(str(exc))
        return redirect(url_for("projeto", project_id=project_id))

    @web.post("/pagamentos/<int:payment_id>/toggle")
    def pagamento_toggle(payment_id: int) -> Response:
        row = ctx.conn.execute("SELECT pago_em FROM pagamentos WHERE id = ?", (payment_id,)).fetchone()
        if not row:
            abort(404)
        fin.mark_paid(ctx.conn, payment_id, None if row["pago_em"] else today_local(ctx))
        flash("Pagamento desfeito." if row["pago_em"] else "Pagamento registrado.")
        return _back(url_for("financeiro"))

    @web.post("/despesas")
    def despesa_nova() -> Response:
        projeto_id = request.form.get("projeto_id", "")
        categoria = request.form.get("categoria", "outro")
        if categoria not in ctx.finance.categorias_despesa:
            categoria = "outro"
        try:
            fin.add_expense(ctx.conn, request.form.get("descricao", ""), _money_field("valor", ""),
                            fin.parse_date(request.form.get("data"), "Data"), categoria,
                            int(projeto_id) if projeto_id.isdigit() else None)
            flash("Despesa lançada.")
        except fin.FinanceError as exc:
            flash(str(exc))
        if projeto_id.isdigit():
            return redirect(url_for("projeto", project_id=int(projeto_id)))
        return redirect(url_for("financeiro"))

    @web.post("/despesas/<int:expense_id>/apagar")
    def despesa_apagar(expense_id: int) -> Response:
        fin.delete_expense(ctx.conn, expense_id)
        flash("Despesa apagada.")
        return _back(url_for("financeiro"))

    @web.post("/leads/<int:lead_id>/nao-fechou")
    def descartar_fechamento(lead_id: int) -> Response:
        with db.tx(ctx.conn):
            db.update_lead(ctx.conn, lead_id, sinal_fechamento=0)
        flash("Ok, marcado como não fechado.")
        return redirect(url_for("projetos"))

    # ---------- financeiro ----------

    @web.get("/financeiro")
    def financeiro() -> str:
        hoje = today_local(ctx)
        painel = insights.build(ctx.conn, ctx.finance, hoje, _extra_alerts(ctx))
        abertos = [g for g in fin.payments(ctx.conn) if not g["pago_em"]]
        return render_template(
            "financeiro.html", page="financeiro", painel=painel, abertos=abertos,
            despesas=fin.expenses(ctx.conn), hoje=hoje.isoformat(), categorias=ctx.finance.categorias_despesa,
            grafico_receita=revenue_chart(painel.serie),
        )
