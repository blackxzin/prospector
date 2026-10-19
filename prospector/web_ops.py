"""Painel: visão geral operacional, buscas (cidades × nichos) e controle das tarefas em segundo plano."""

from __future__ import annotations

import time
from typing import Any

from flask import Flask, Response, flash, redirect, render_template, request, url_for

from . import checagem, db, insights, lote, metrics, regiao
from .app import App
from .charts import daily_chart, funnel_chart, heatmap, hbar_chart
from .jobs import BUSCA, ENVIO, JobManager

CHECK_TTL_S = 60
MAX_PER_RUN = 40


def _pct(r: dict[str, Any], v: float) -> str:
    return f"{v:.0%} · {r['responderam']}/{r['contatados']}"


def register_ops_routes(web: Flask, ctx: App) -> None:
    from .web import date_long, today_local
    from .web_finance import _ab_rows, _batch_progress, _extra_alerts

    jobs = JobManager(ctx)
    web.extensions["prospector_jobs"] = jobs
    cache: dict[str, Any] = {"at": 0.0, "checks": []}

    def checks(force: bool = False) -> list[checagem.Check]:
        if force or time.monotonic() - cache["at"] > CHECK_TTL_S:
            cache["checks"] = checagem.run_checks(ctx)
            cache["at"] = time.monotonic()
        return cache["checks"]

    @web.context_processor
    def ops_context() -> dict[str, Any]:
        st = {k: jobs.status(k) for k in (BUSCA, ENVIO)}
        running = any(jobs.is_running(k) for k in (BUSCA, ENVIO))
        return {"ops": metrics.today_ops(ctx.conn, ctx.rules, db.utcnow()), "jobs": st,
                "job_rodando": {k: jobs.is_running(k) for k in (BUSCA, ENVIO)}, "auto_refresh": running}

    @web.get("/")
    def dashboard() -> str:
        hoje = today_local(ctx)
        conn = ctx.conn
        painel = insights.build(conn, ctx.finance, hoje, _extra_alerts(ctx))
        f = db.funnel(conn)
        clientes = conn.execute("SELECT COUNT(DISTINCT lead_id) FROM projetos WHERE lead_id IS NOT NULL "
                                "AND status != 'cancelado'").fetchone()[0]
        etapas = [("Encontrados", f["encontrados"]), ("Válidos", f["validos"]), ("Aprovados", f["aprovados"]),
                  ("Enviados", f["enviados"]), ("Responderam", f["responderam"]),
                  ("Interessados", f["interessados"] + clientes), ("Fecharam", clientes)]
        diario = metrics.daily(conn, hoje, ctx.settings.tz, 30)
        env30 = sum(d["enviados"] for d in diario)
        resp30 = sum(d["respostas"] for d in diario)
        cidades = metrics.by_dimension(conn, "cidade")
        nichos = metrics.by_dimension(conn, "nicho")
        estoque = metrics.leads_by_city(conn)
        lista = checks()
        return render_template(
            "dashboard.html", page="dashboard", painel=painel, hoje_extenso=date_long(hoje),
            checks=lista, checks_resumo=checagem.summary(lista),
            checks_erros=[c for c in lista if c.nivel == checagem.ERRO],
            kpi={"env30": env30, "resp30": resp30, "taxa30": (resp30 / env30) if env30 else None,
                 "clientes": clientes, "lucro_mes": painel.resumo.lucro_mes},
            grafico_diario=daily_chart(diario), grafico_funil=funnel_chart(etapas),
            grafico_cidades=hbar_chart(cidades, value="taxa", fmt=_pct, aria="Taxa de resposta por cidade")
            if cidades else "",
            grafico_nichos=hbar_chart(nichos, value="taxa", fmt=_pct, aria="Taxa de resposta por nicho")
            if nichos else "",
            grafico_estoque=hbar_chart(estoque, value="total", fmt=lambda r, v: f"{v} · {r['novos']} novos",
                                       aria="Leads por cidade") if estoque else "",
            grafico_heat=heatmap(metrics.response_heatmap(conn, ctx.settings.tz), metrics.HEAT_HOURS),
            tem_respostas=resp30 > 0 or conn.execute("SELECT COUNT(*) FROM respostas").fetchone()[0] > 0,
            ab=_ab_rows(ctx)[:5], lote=_batch_progress(ctx),
        )

    @web.post("/checagem")
    def recheck() -> Response:
        checks(force=True)
        flash("Checagem atualizada.")
        return redirect(request.referrer or url_for("dashboard"))

    # ---------- buscas ----------

    def _cfg() -> lote.BatchConfig | None:
        try:
            return lote.BatchConfig.load(ctx.settings.buscas_path)
        except ValueError:
            return None

    @web.get("/buscas")
    def buscas() -> str:
        cfg = _cfg()
        pend = lote.pending(ctx.conn, cfg) if cfg else []
        feitas = ctx.conn.execute("SELECT * FROM buscas ORDER BY feita_em DESC LIMIT 60").fetchall()
        return render_template("buscas.html", page="buscas", cfg=cfg, pendentes=pend, feitas=feitas,
                               total=len(cfg.queries()) if cfg else 0, max_run=MAX_PER_RUN,
                               regiao_preview=None)

    @web.post("/buscas/listas")
    def buscas_salvar() -> Response:
        cfg = _cfg() or lote.BatchConfig(("pizzaria",), ("Itaí SP",), 8)
        try:
            novo = cfg.with_lists(request.form.get("nichos", "").splitlines(),
                                  request.form.get("cidades", "").splitlines(),
                                  int(request.form.get("max", cfg.max_por_execucao) or cfg.max_por_execucao))
        except ValueError as exc:
            flash(str(exc))
            return redirect(url_for("buscas"))
        novo.save(ctx.settings.buscas_path)
        flash(f"Salvo: {len(novo.nichos)} nichos × {len(novo.cidades)} cidades = {len(novo.queries())} buscas.")
        return redirect(url_for("buscas"))

    @web.post("/buscas/regiao")
    def buscas_regiao() -> Response | str:
        cidade, uf = request.form.get("cidade", "").strip(), request.form.get("uf", "").strip().upper()
        try:
            raio = float(request.form.get("raio", "50").replace(",", "."))
            base_list = regiao.load_dataset(ctx.settings.data_dir / "municipios.csv")
            base = regiao.find_city(base_list, cidade, uf)
            perto = regiao.nearby(base_list, base, raio, MAX_PER_RUN * 2)
        except (ValueError, regiao.RegiaoError) as exc:
            flash(str(exc))
            return redirect(url_for("buscas"))
        if request.form.get("acao") == "salvar":
            cfg = _cfg() or lote.BatchConfig(("pizzaria",), ("x",), 8)
            modo = request.form.get("modo", "adicionar")
            atuais = [] if modo == "substituir" else list(cfg.cidades)
            novo = cfg.with_lists(list(cfg.nichos), atuais + [c.consulta for c in perto])
            novo.save(ctx.settings.buscas_path)
            flash(f"{len(perto)} cidades até {raio:g} km de {base.consulta} "
                  f"{'colocadas' if modo == 'substituir' else 'adicionadas'}. Total: {len(novo.queries())} buscas.")
            return redirect(url_for("buscas"))
        cfg = _cfg()
        pend = lote.pending(ctx.conn, cfg) if cfg else []
        feitas = ctx.conn.execute("SELECT * FROM buscas ORDER BY feita_em DESC LIMIT 60").fetchall()
        return render_template("buscas.html", page="buscas", cfg=cfg, pendentes=pend, feitas=feitas,
                               total=len(cfg.queries()) if cfg else 0, max_run=MAX_PER_RUN,
                               regiao_preview={"base": base, "cidades": perto, "raio": raio,
                                               "cidade": cidade, "uf": uf})

    # ---------- tarefas ----------

    @web.post("/tarefas/busca")
    def tarefa_busca() -> Response:
        try:
            limit = max(1, min(MAX_PER_RUN, int(request.form.get("quantas", "8"))))
        except ValueError:
            limit = 8
        if not _cfg():
            flash("Configure nichos e cidades antes de rodar as buscas.")
        elif jobs.start_busca(limit):
            flash(f"Buscas iniciadas ({limit}). Cada uma leva alguns minutos; a página atualiza sozinha.")
        else:
            flash("Já existe uma busca rodando.")
        return redirect(request.referrer or url_for("buscas"))

    @web.post("/tarefas/envio")
    def tarefa_envio() -> Response:
        if not ctx.settings.has_whatsapp:
            flash("Configure WAAKG_SESSION e WAAKG_API_KEY no .env (e rode 'prospector checar') antes de enviar.")
            return redirect(request.referrer or url_for("fila"))
        if db.stop_reason(ctx.conn):
            flash("O envio está parado por segurança. Confira a sessão e rode 'prospector retomar'.")
            return redirect(request.referrer or url_for("fila"))
        aguardar = request.form.get("aguardar") == "1"
        if jobs.start_envio(aguardar=aguardar):
            flash("Envio iniciado: respeita horário, limites, intervalo e 'digitando'. Pode pausar a qualquer hora.")
        else:
            flash("O envio já está rodando.")
        return redirect(request.referrer or url_for("fila"))

    @web.post("/tarefas/<kind>/parar")
    def tarefa_parar(kind: str) -> Response:
        if kind in (BUSCA, ENVIO) and jobs.stop(kind):
            flash("Pausando… termina a etapa atual e para.")
        else:
            flash("Nada rodando para pausar.")
        return redirect(request.referrer or url_for("dashboard"))

    @web.post("/fila/gerar-e-voltar")
    def gerar_fila_dashboard() -> Response:
        from . import fila as fq
        s = ctx.settings
        res = fq.build_queue(ctx.conn, ctx.templates, ctx.rules, db.utcnow(), size=s.max_per_day,
                             followup_days=s.followup_business_days, charge_text=ctx.charge_text)
        flash(f"Fila gerada: {res.primeiras} novas, {res.followups} follow-ups, {res.cobrancas} cobranças. "
              "Revise e aprove.")
        return redirect(url_for("fila"))
