"""CLI: prospector buscar | importar | fila | aprovar | rejeitar | enviar | status | painel | ..."""

from __future__ import annotations

import argparse
import logging
import random
import sys
from datetime import timedelta
from pathlib import Path

from . import db
from . import canais, emails_site, inbox
from . import fila as fq
from .app import App
from .classify import classify_offer, is_franchise
from .importer import import_csv
from .phone import normalize_mobile, pretty, variants
from .scraper import ScraperError
from .sender import RealClock, run as run_sender, type_and_send
from .schedule import fmt_local, in_window, next_window_start, sent_last_hour, sent_today, typing_seconds
from .whatsapp import WhatsAppError

log = logging.getLogger("prospector")


def _setup_logging(app: App, verbose: bool) -> None:
    app.settings.data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(app.settings.data_dir / "prospector.log", encoding="utf-8")],
    )


def _print_item(item) -> None:
    print(f"\n#{item['id']}  {item['nome']}  [{item['tipo']} · {item['oferta']} · {item['status']}]")
    extra = f" · nota {item['nota']} ({item['avaliacoes'] or 0})" if item["nota"] else ""
    print(f"    {pretty(item['telefone'])} · {item['cidade'] or '-'}{extra}")
    print(f"    site: {item['site'] or '-'}")
    if item["motivo"]:
        print(f"    obs: {item['motivo']}")
    print("    ---")
    print("    " + item["texto"])


# ---------- comandos ----------

def _origem(app: App, args: argparse.Namespace):
    """Origem da busca manual: --pais pega idioma e fuso do mundo.yaml (dá para sobrescrever)."""
    from .mundo import BRASIL, Origem
    pais = (args.pais or "BR").upper()
    if pais == "BR":
        return BRASIL
    market = next((m for m in app.mundo.mercados if m[0] == pais), None)
    if market is None and not (args.idioma and args.fuso):
        raise ValueError(f"País {pais} não está no mundo.yaml: informe --idioma e --fuso")
    idioma = args.idioma or (market[2] if market else "en")
    fuso = args.fuso or (market[3][0][1] if market else "UTC")
    return Origem(pais, idioma, fuso, args.cidade)


def cmd_buscar(app: App, args: argparse.Namespace) -> int:
    try:
        result = app.search(args.consulta, origem=_origem(app, args))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    except ScraperError as exc:
        print(f"Erro no scraper: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - mensagem amigável para falha de rede
        print(f"Não consegui falar com o scraper em {app.settings.gmaps_url}: {exc}", file=sys.stderr)
        print("Ele está rodando? docker compose up -d gmaps", file=sys.stderr)
        return 1
    print(f"Importação: {result}")
    return 0


def cmd_importar(app: App, args: argparse.Namespace) -> int:
    result = import_csv(app.conn, Path(args.arquivo), args.busca, app.listas)
    print(f"Importação: {result}")
    return 0


def cmd_reclassificar(app: App, _args: argparse.Namespace) -> int:
    """Reaplica listas.yaml (franquias e domínios) nos leads ainda não contatados."""
    changed = 0
    with db.tx(app.conn):
        for lead in app.conn.execute("SELECT * FROM leads WHERE status IN ('novo', 'descartado')").fetchall():
            oferta = classify_offer(lead["site"], app.listas.dominios_social)
            franchise = is_franchise(lead["nome"], app.listas.franquias)
            fields: dict[str, object] = {}
            if oferta != lead["oferta"]:
                fields["oferta"] = oferta
            if franchise and lead["status"] == db.NOVO:
                fields.update(status=db.DESCARTADO, motivo=f"franquia: {franchise}")
            elif not franchise and (lead["motivo"] or "").startswith("franquia") and lead["telefone"]:
                fields.update(status=db.NOVO, motivo=None)
            if fields:
                db.update_lead(app.conn, lead["id"], **fields)
                changed += 1
    print(f"{changed} leads atualizados.")
    return 0


def _nicho_do_dia(app: App) -> str | None:
    if app.nichos is None:
        return None
    nicho = app.nichos.do_dia(db.utcnow().astimezone(app.settings.tz).date())
    return nicho.id if nicho else None


def cmd_fila(app: App, args: argparse.Namespace) -> int:
    s = app.settings
    if not args.so_mostrar:
        pais = None if args.pais.lower() == "todos" else tuple(p.strip().upper() for p in args.pais.split(","))
        res = fq.build_queue(app.conn, app.template_set, app.rules, db.utcnow(),
                             size=args.tamanho or s.max_per_day, followup_days=s.followup_business_days,
                             busca=args.busca, cidade=args.cidade, pais=pais, charge_text=app.charge_text,
                             nicho=args.nicho, nichos=app.nichos,
                             preferir_nicho=_nicho_do_dia(app) if args.nicho_do_dia else None,
                             checar_presenca=app.presence_checker())
        print(f"Fila gerada: {res.primeiras} novas, {res.followups} follow-ups, {res.cobrancas} cobranças "
              f"({res.encerrados} leads encerrados como sem_resposta).")
    itens = fq.list_items(app.conn)
    if not itens:
        print("Fila vazia (sem leads novos válidos). Rode 'prospector buscar ...' primeiro.")
        return 0
    for item in itens:
        _print_item(item)
    pend = sum(1 for i in itens if i["status"] == fq.PENDENTE)
    print(f"\n{len(itens)} itens na fila, {pend} pendentes. Aprove com 'prospector aprovar' ou no painel.")
    return 0


def _release_time(app: App, raw: str | None):
    """'2026-10-21 09:00' (horário do Brasil) -> instante UTC; None se não informado."""
    if not raw:
        return None
    from datetime import datetime
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=app.settings.tz)
    except ValueError as exc:
        raise ValueError("Use --liberar-em 'AAAA-MM-DD HH:MM' (horário do Brasil)") from exc


def cmd_aprovar(app: App, args: argparse.Namespace) -> int:
    try:
        liberar = _release_time(app, args.liberar_em)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    if args.todos:
        print(f"{fq.approve(app.conn, fq.pending_ids(app.conn), liberar_em=liberar)} itens aprovados.")
        return 0
    if args.ids:
        print(f"{fq.approve(app.conn, args.ids, liberar_em=liberar)} itens aprovados."
              + (f" Saem a partir de {args.liberar_em}." if liberar else ""))
        return 0
    pend = [i for i in fq.list_items(app.conn, (fq.PENDENTE,))]
    if not pend:
        print("Nada pendente. Rode 'prospector fila'.")
        return 0
    print("Para cada item: [s]im, [n]ão (rejeitar), [e]ditar texto, [p]ular, [q] sair")
    approved = rejected = 0
    for item in pend:
        _print_item(item)
        while True:
            choice = input("  aprovar? [s/n/e/p/q] ").strip().lower()[:1]
            if choice == "s":
                approved += fq.approve(app.conn, [item["id"]])
            elif choice == "n":
                rejected += fq.reject(app.conn, [item["id"]])
            elif choice == "e":
                novo = input("  novo texto (uma linha): ").strip()
                if not novo:
                    print("  texto vazio, mantendo o original")
                    continue
                approved += fq.approve(app.conn, [item["id"]], texto=novo)
            elif choice == "q":
                print(f"{approved} aprovados, {rejected} rejeitados.")
                return 0
            elif choice != "p":
                continue
            break
    print(f"{approved} aprovados, {rejected} rejeitados.")
    return 0


def cmd_refazer_textos(app: App, _args: argparse.Namespace) -> int:
    """Refaz com os templates atuais o texto das primeiras mensagens ainda não enviadas."""
    n = fq.regenerate_texts(app.conn, app.template_set, app.rules, db.utcnow(), nichos=app.nichos)
    print(f"{n} texto(s) refeito(s). Status e horário de liberação ficaram como estavam.")
    return 0


def cmd_rejeitar(app: App, args: argparse.Namespace) -> int:
    print(f"{fq.reject(app.conn, args.ids)} itens rejeitados.")
    return 0


def cmd_enviar(app: App, args: argparse.Namespace) -> int:
    s = app.settings
    client = None
    if not args.dry_run:
        try:
            client = app.whatsapp()
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 1
        now = db.utcnow()
        if not in_window(now, app.rules):
            nxt = next_window_start(now, app.rules).astimezone(s.tz)
            print(f"Fora do horário comercial agora. Próxima janela: {fmt_local(nxt, s.tz)}.")
            if not args.aguardar:
                print("Use --aguardar para deixar rodando até lá.")
                return 0
    else:
        print("== DRY-RUN: nada será enviado. Horários abaixo são simulados. ==")
    report = run_sender(
        app.conn, client, app.rules, typing_min_s=s.typing_min_s, typing_max_s=s.typing_max_s,
        dry_run=args.dry_run, wait_next_day=args.aguardar, notify_enabled=s.notify, templates=app.template_set,
        portfolio_apos_primeira=s.portfolio_apos_primeira,
    )
    print(f"\nResumo: {report.enviados} {'simulados' if args.dry_run else 'enviados'}, "
          f"{report.pulados} pulados, {report.erros} erros. {report.motivo_fim}")
    return 2 if report.parado and not args.dry_run else 0


def cmd_teste(app: App, args: argparse.Namespace) -> int:
    """Envia UMA mensagem de exemplo para o seu próprio número (não usa a fila)."""
    number = normalize_mobile(args.numero)
    if not number:
        print("Número inválido: informe celular com DDD, ex. 14991234567", file=sys.stderr)
        return 1
    if db.stop_reason(app.conn):
        print(f"Sistema parado: {db.stop_reason(app.conn)}. Rode 'prospector retomar'.", file=sys.stderr)
        return 1
    fake = {"nome": "Pizzaria Teste", "nota": 4.7, "avaliacoes": 132, "categoria": "Pizzaria", "oferta": args.oferta}
    text = app.templates.first_message(fake, db.utcnow(), app.settings.tz)
    typing_s = typing_seconds(text, app.settings.typing_min_s, app.settings.typing_max_s, random.Random())
    print(f"Enviando para {pretty(number)} (digitando {typing_s:.0f}s):\n  {text}")
    try:
        client = app.whatsapp()
        jid = client.check_number(number)
        if not jid:
            print("Esse número não está no WhatsApp.", file=sys.stderr)
            return 1
        result = type_and_send(client, RealClock(), jid, text, typing_s)
    except (WhatsAppError, RuntimeError) as exc:
        print(f"Falhou: {exc}", file=sys.stderr)
        return 1
    with db.tx(app.conn):
        db.log_send(app.conn, lead_id=None, fila_id=None, telefone=number, tipo="teste", texto=text,
                    dry_run=False, ok=True, http_status=getattr(result, "status", None),
                    resposta_api=str(getattr(result, "body", ""))[:2000])
    print(f"Enviado! JID: {jid}")
    return 0


def cmd_status(app: App, _args: argparse.Namespace) -> int:
    conn, s = app.conn, app.settings
    stop = db.stop_reason(conn)
    print("ENVIO PARADO: " + stop if stop else "Envio liberado.")
    f = db.funnel(conn)
    print("Funil: " + " -> ".join(f"{k} {f[k]}" for k in ("encontrados", "validos", "aprovados", "enviados",
                                                              "responderam", "interessados")))
    print("Por status: " + ", ".join(f"{k} {v}" for k, v in db.status_counts(conn).items()))
    now = db.utcnow()
    sent = db.real_send_times(conn, now - timedelta(days=2))
    print(f"Hoje: {sent_today(now, sent, s.tz)}/{s.max_per_day} envios · última hora: "
          f"{sent_last_hour(now, sent)}/{s.max_per_hour}")
    window = "dentro" if in_window(now, app.rules) else "fora"
    print(f"Agora: {fmt_local(now, s.tz)} ({window} do horário). Próxima janela: "
          f"{fmt_local(next_window_start(now, app.rules), s.tz)}")
    pend = conn.execute("SELECT status, COUNT(*) n FROM fila WHERE status IN ('pendente','aprovado') "
                        "GROUP BY status").fetchall()
    print("Fila: " + (", ".join(f"{r['status']} {r['n']}" for r in pend) or "vazia"))
    last = conn.execute("SELECT * FROM respostas ORDER BY id DESC LIMIT 5").fetchall()
    if last:
        print("Últimas respostas:")
        for r in last:
            print(f"  {r['recebido_em']} {pretty(r['telefone'])}: {(r['texto'] or '')[:100]}")
    return 0


def cmd_financeiro(app: App, args: argparse.Namespace) -> int:
    """Resumo do mês, alertas e sugestões do gerenciador."""
    from . import insights
    from .finance import brl
    hoje = db.utcnow().astimezone(app.settings.tz).date()
    painel = insights.build(app.conn, app.finance, hoje)
    r = painel.resumo
    print(f"Mês: recebido {brl(r.recebido_mes)} · despesas {brl(r.despesas_mes)} · lucro {brl(r.lucro_mes)}"
          + (f" · meta {r.meta_pct}% de {brl(r.meta)}" if r.meta else ""))
    print(f"A receber {brl(r.a_receber)} (vencido {brl(r.vencido)}) · previsto 30 dias {brl(r.previsao_30d)} · "
          f"recorrente {brl(r.mrr)}/mês · {r.projetos_ativos} projeto(s) em andamento")
    marks = {insights.CRITICO: "[URGENTE]", insights.ATENCAO: "[atenção]", insights.BOM: "[oportunidade]",
             insights.INFO: "[info]"}
    if painel.alertas:
        print("\nO que fazer agora:")
        for a in painel.alertas:
            print(f"  {marks[a.nivel]} {a.titulo}: {a.detalhe}")
    if painel.sugestoes:
        print("\nSugestões:")
        for i, sug in enumerate(painel.sugestoes, 1):
            print(f"  {i}. {sug}")
    if args.notificar:
        urgent = [a for a in painel.alertas if a.nivel in (insights.CRITICO, insights.BOM)]
        if urgent:
            app.notify(f"{len(urgent)} item(ns) pedem atenção", "\n".join(a.titulo for a in urgent[:5]),
                       urgent=any(a.nivel == insights.CRITICO for a in urgent))
    return 0


def cmd_checar(app: App, _args: argparse.Namespace) -> int:
    from . import checagem
    checks = checagem.run_checks(app)
    marks = {checagem.OK: "OK   ", checagem.AVISO: "AVISO", checagem.ERRO: "ERRO "}
    for c in checks:
        print(f"[{marks[c.nivel]}] {c.nome}: {c.detalhe}")
        if c.nivel != checagem.OK and c.como_resolver:
            print(f"         -> {c.como_resolver}")
    print(f"\nResultado: {checagem.summary(checks)}.")
    return 1 if any(c.nivel == checagem.ERRO for c in checks) else 0


def cmd_regiao(app: App, args: argparse.Namespace) -> int:
    """Lista (e opcionalmente salva no buscas.yaml) as cidades num raio a partir de uma cidade-base."""
    from . import regiao
    from .lote import BatchConfig
    try:
        cidades = regiao.load_dataset(app.settings.data_dir / "municipios.csv")
        base = regiao.find_city(cidades, args.cidade, args.uf)
        perto = regiao.nearby(cidades, base, args.raio, args.max, mesma_uf=args.mesma_uf)
    except regiao.RegiaoError as exc:
        print(exc, file=sys.stderr)
        return 1
    for c in perto:
        print(f"  {c.consulta:<32} {c.distancia_km:>5.1f} km  DDD {c.ddd}")
    print(f"{len(perto)} cidade(s) até {args.raio:g} km de {base.consulta}.")
    if args.salvar:
        try:
            atual = BatchConfig.load(app.settings.buscas_path)
        except ValueError:
            atual = BatchConfig(("pizzaria",), ("x",), 8)
        novo = atual.with_lists(list(atual.nichos), [c.consulta for c in perto])
        novo.save(app.settings.buscas_path)
        print(f"buscas.yaml atualizado: {len(novo.nichos)} nichos × {len(novo.cidades)} cidades = "
              f"{len(novo.queries())} buscas.")
    return 0


def cmd_backup(app: App, args: argparse.Namespace) -> int:
    from .backup import BackupError, list_backups, make_backup
    from .config import ROOT
    dest = Path(args.destino).expanduser() if args.destino else app.settings.backup_dir
    if args.listar:
        for b in list_backups(dest):
            print(b)
        return 0
    try:
        res = make_backup(app.conn, dest, keep=args.manter or app.settings.backup_keep, config_root=ROOT)
    except (BackupError, OSError) as exc:
        print(f"Backup falhou: {exc}", file=sys.stderr)
        app.notify("backup falhou", str(exc), urgent=True)
        return 1
    print(f"Backup ok: {res.path} ({res.size / 1024:.0f} KB). Removidos {len(res.removed)} antigos.")
    return 0


def cmd_ab(app: App, _args: argparse.Namespace) -> int:
    """Desempenho de cada variação da primeira mensagem."""
    from .messages import variant_id
    stats = fq.ab_stats(app.conn)
    for oferta, options in app.templates.data["primeira"].items():
        print(f"\n{oferta}:")
        for text in options:
            sent, pos = stats.get(variant_id(text), (0, 0))
            rate = f"{pos / sent:.0%}" if sent else "  -"
            print(f"  [{variant_id(text)}] {sent:3d} enviados · {pos:3d} positivas · {rate:>4}  {text[:70]}...")
    print("\nAs variações com mais respostas positivas passam a ser sorteadas com mais frequência.")
    return 0


def cmd_checar_sites(app: App, args: argparse.Namespace) -> int:
    from .sitecheck import check_leads
    checked, bad = check_leads(app.conn, limit=args.limite)
    print(f"{checked} site(s) checado(s), {bad} com problema (oferta virou site_problema).")
    return 0


def cmd_checar_presenca(app: App, args: argparse.Namespace) -> int:
    from .presenca import checar_leads
    res = checar_leads(app.conn, app.presence_checker(force=True), limite=args.limite, out=print)
    print(f"{res.checados} lead(s) checado(s), {res.mudados} mudaram, {res.erros} com erro na busca "
          "(ficam para a próxima).")
    return 0


def cmd_lote_mundo(app: App, args: argparse.Namespace) -> int:
    from .lote import pending_world, run_world
    try:
        cfg = app.mundo
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    paises = {p.strip().upper() for p in args.pais.split(",")} if args.pais else None
    todo = pending_world(app.conn, cfg, paises)
    if args.listar or not todo:
        print(f"{len(todo)} busca(s) pendente(s) de {len(cfg.buscas(paises))}:")
        for b in todo[:60]:
            print(f"  [{b.origem.pais}] {b.consulta}")
        return 0
    results = run_world(app.conn, cfg, lambda q, origem: app.search(q, origem=origem), limit=args.max, paises=paises)
    ok = sum(1 for _, r in results if not r.startswith("erro"))
    app.notify("buscas pelo mundo", f"{ok}/{len(results)} buscas ok. {len(pending_world(app.conn, cfg, paises))} pendentes.")
    return 0


def cmd_lote_nicho(app: App, args: argparse.Namespace) -> int:
    from .lote import BatchConfig, pending_nicho, run_nicho
    if app.nichos is None:
        print(f"{app.settings.nichos_path.name} não encontrado ou inválido.", file=sys.stderr)
        return 1
    nid = _nicho_do_dia(app) if args.nicho_do_dia else args.nicho
    if nid not in app.nichos.itens or not app.nichos.itens[nid].buscas:
        print(f"Nicho {nid!r} não existe (ou não tem buscas). Veja: prospector nichos", file=sys.stderr)
        return 1
    try:
        cidades = BatchConfig.load(app.settings.buscas_path).cidades
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    todo = pending_nicho(app.conn, app.nichos, nid, cidades)
    print(f"Nicho: {app.nichos.itens[nid].nome} ({nid}). {len(todo)} busca(s) pendente(s).")
    if args.listar or not todo:
        for q in todo[:40]:
            print(f"  {q}")
        return 0
    results = run_nicho(app.conn, app.nichos, nid, cidades, app.search, limit=args.max or 8)
    ok = sum(1 for _, r in results if not r.startswith("erro"))
    app.notify(f"buscas do nicho {nid}", f"{ok}/{len(results)} buscas ok.")
    return 0


def cmd_lote(app: App, args: argparse.Namespace) -> int:
    from .lote import BatchConfig, pending, run_batch
    if args.nicho or args.nicho_do_dia:
        return cmd_lote_nicho(app, args)
    if args.mundo:
        return cmd_lote_mundo(app, args)
    try:
        cfg = BatchConfig.load(app.settings.buscas_path)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    todo = pending(app.conn, cfg)
    if args.listar or not todo:
        print(f"{len(todo)} busca(s) pendente(s) de {len(cfg.queries())}:")
        for q in todo:
            print(f"  {q}")
        return 0
    results = run_batch(app.conn, cfg, app.search, limit=args.max)
    ok = sum(1 for _, r in results if not r.startswith("erro"))
    app.notify("buscas em lote", f"{ok}/{len(results)} buscas ok. {len(pending(app.conn, cfg))} pendentes.")
    return 0


def cmd_proposta(app: App, args: argparse.Namespace) -> int:
    from . import finance as fin
    proj = fin.project(app.conn, args.projeto)
    if not proj:
        print("Projeto não encontrado.", file=sys.stderr)
        return 1
    data = app.proposal_pdf(proj)
    out = Path(args.saida) if args.saida else app.settings.data_dir / "propostas" / f"proposta-{proj['id']}.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"Proposta salva em {out}")
    return 0


def cmd_relatorio(app: App, args: argparse.Namespace) -> int:
    from . import relatorio
    s = app.settings
    hoje = db.utcnow().astimezone(s.tz).date()
    rel = relatorio.build(app.conn, hoje, s.tz, days=args.dias)
    text = rel.markdown()
    print(text)
    folder = s.data_dir / "relatorios"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"relatorio-{hoje:%Y-%m-%d}.md"
    path.write_text(text, encoding="utf-8")
    print(f"Salvo em {path}")
    if args.notificar:
        app.notify("relatório da semana", rel.resumo_curto())
    return 0


def _print_canal_item(item) -> None:
    print(f"\n#{item['id']}  {item['nome']}  [{item['canal']} · {item['status']}]  {item['destino']}")
    if item["assunto"]:
        print(f"    assunto: {item['assunto']}")
    print("    " + item["texto"].replace("\n", "\n    "))


def cmd_canais(app: App, args: argparse.Namespace) -> int:
    """E-mail (Gmail) e DM do Instagram: montar, listar, aprovar, enviar."""
    conn, s = app.conn, app.settings
    canal, acao = args.canal, args.acao
    if acao == "montar":
        res = canais.build(conn, canal, args.limite, app.canal_templates, db.utcnow(), s.tz)
        print(f"{res.criados} itens de {canal} criados. Veja com: prospector canais {canal} listar")
        return 0
    if acao == "listar":
        items = canais.list_items(conn, canal)
        for item in items:
            _print_canal_item(item)
        print(f"\n{len(items)} itens." if items else "Nada pendente.")
        return 0
    if acao == "aprovar":
        n = canais.approve(conn, canal, None if args.todos else args.ids)
        print(f"{n} aprovados.")
        return 0
    if acao == "rejeitar":
        print(f"{canais.reject(conn, args.ids)} rejeitados.")
        return 0
    if acao == "enviado":  # DM de Instagram enviada à mão
        done = sum(canais.mark_sent(conn, i, db.utcnow()) for i in args.ids)
        print(f"{done} marcados como enviados.")
        return 0
    if acao == "parar":
        for d in args.ids_ou_destinos:
            canais.block(conn, d, "pediu para parar", db.utcnow())
        print("Bloqueado: nunca mais entra em fila.")
        return 0
    if acao == "testar":
        if canal != canais.EMAIL or not s.has_email:
            print("Teste só para e-mail, com SMTP_USUARIO e SMTP_SENHA no .env.", file=sys.stderr)
            return 1
        para = args.para or s.smtp_user
        cfg = canais.SmtpConfig(s.smtp_host, s.smtp_port, s.smtp_user, s.smtp_password, s.smtp_nome)
        try:
            subject, _ = canais.send_test(conn, cfg, para, app.canal_templates, db.utcnow(), s.tz)
        except canais.CanalError as exc:
            print(f"PARADO: {exc}", file=sys.stderr)
            return 2
        print(f"Teste enviado para {para} ({subject}). Confira a caixa de entrada e o spam.")
        return 0
    if acao == "resumo":
        from .metrics import channel_report
        for r in channel_report(conn):
            print(f"{r['canal']:<10} contatados {r['contatados']:>4} · responderam {r['responderam']:>3} "
                  f"({r['taxa']:.0%}) · interessados {r['interessados']}")
        return 0
    if acao == "followups":
        made = canais.build_followups(conn, app.canal_templates, app.rules, db.utcnow(), s.followup_business_days)
        if args.todos:
            canais.approve_followups(conn)
        print(f"{made} lembretes criados" + (" e aprovados." if args.todos else ". Aprove com --todos."))
        return 0
    if acao == "respostas":
        if canal != canais.EMAIL or not s.has_email:
            print("Leitura de respostas só para e-mail, com SMTP_USUARIO e SMTP_SENHA no .env.", file=sys.stderr)
            return 1
        cfg = inbox.ImapConfig(s.imap_host, s.imap_port, s.smtp_user, s.smtp_password)
        try:
            if args.loop:
                inbox.loop(conn, cfg, app.notify, args.loop)
                return 1   # o loop só termina por erro de login
            res = inbox.poll(conn, cfg, app.notify, db.utcnow())
        except inbox.InboxError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"{res.lidos} e-mails lidos, {res.respostas} resposta(s) de leads, {res.bounces} voltaram, "
              f"{res.ignorados} ignorados.")
        return 0
    if acao == "enviar":
        if canal != canais.EMAIL:
            print("Instagram é manual: abra a página Instagram no painel.", file=sys.stderr)
            return 1
        if not s.has_email:
            print("Configure SMTP_USUARIO e SMTP_SENHA (senha de app do Gmail) no .env.", file=sys.stderr)
            return 1
        cfg = canais.SmtpConfig(s.smtp_host, s.smtp_port, s.smtp_user, s.smtp_password, s.smtp_nome)
        report = canais.send_emails(conn, cfg, app.rules, s.max_email_per_day, clock=RealClock(),
                                    wait_next_day=args.aguardar)
        print(f"\nResumo: {report.enviados} enviados, {report.erros} erros. {report.motivo_fim}")
        return 2 if report.parado else 0
    print(f"Ação desconhecida: {acao}", file=sys.stderr)
    return 1


def cmd_buscar_emails(app: App, args: argparse.Namespace) -> int:
    checked, found = emails_site.enrich(app.conn, limit=args.limite)
    print(f"{checked} sites consultados, {found} e-mails encontrados. Monte a fila: prospector canais email montar")
    return 0


def cmd_retomar(app: App, _args: argparse.Namespace) -> int:
    db.set_state(app.conn, db.PARADO, None)
    print("Envio liberado de novo.")
    return 0


def cmd_desbloquear(app: App, args: argparse.Namespace) -> int:
    number = normalize_mobile(args.numero)
    if not number:
        print("Número inválido.", file=sys.stderr)
        return 1
    with db.tx(app.conn):
        n = app.conn.execute(
            f"DELETE FROM bloqueio WHERE telefone IN ({','.join('?' * len(variants(number)))})",
            tuple(variants(number)),
        ).rowcount
    print(f"{n} registro(s) removido(s) da lista de bloqueio. O status do lead não foi alterado.")
    return 0


def cmd_webhook(app: App, args: argparse.Namespace) -> int:
    """Cadastra o webhook do prospector no WA-AKG."""
    s = app.settings
    if not s.webhook_secret:
        print("Defina WEBHOOK_SECRET no .env antes (ex.: openssl rand -hex 32).", file=sys.stderr)
        return 1
    try:
        answer = app.whatsapp().create_webhook(args.url, s.webhook_secret)
    except (WhatsAppError, RuntimeError) as exc:
        print(f"Falhou: {exc}", file=sys.stderr)
        return 1
    print(f"Webhook cadastrado: {answer[:300]}")
    return 0


def cmd_respostas(app: App, args: argparse.Namespace) -> int:
    """Busca no WA-AKG as respostas dos leads (o webhook dele só aceita HTTPS público)."""
    import time

    from .poll import poll_replies

    if not app.settings.has_whatsapp:
        print("Configure WAAKG_SESSION e WAAKG_API_KEY no .env", file=sys.stderr)
        return 1
    client = app.whatsapp()
    while True:
        try:
            res = poll_replies(app.conn, client,
                               lambda msg, at: app.handle_inbound(msg, received_at=at, background=False),
                               db.utcnow())
            if res.novas or not args.loop:
                print(f"{res.chats} chats lidos, {res.novas} resposta(s) nova(s), {res.erros} erro(s).", flush=True)
        except WhatsAppError as exc:
            print(f"WhatsApp: {exc}", file=sys.stderr, flush=True)
            if not args.loop:
                return 1
        if not args.loop:
            return 0
        time.sleep(args.loop)


def cmd_indicado(app: App, args: argparse.Namespace) -> int:
    """Registra à mão o contato que um atendente passou (cartão de contato sem número, ou número falado)."""
    from .indicacoes import register_referral
    try:
        ids = register_referral(app.conn, args.de, args.numero, nome=args.nome, now=db.utcnow())
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    if not ids:
        print("Esse número já está no sistema (ou já foi contatado, ou está bloqueado). Nada criado.")
        return 0
    print(f"Lead indicado criado (id {ids[0]}). Ele entra na frente da próxima fila: rode 'prospector fila'.")
    return 0


def cmd_nichos(app: App, args: argparse.Namespace) -> int:
    from . import metrics
    from .nichos import reclassificar
    if app.nichos is None:
        print(f"{app.settings.nichos_path.name} não encontrado ou inválido.", file=sys.stderr)
        return 1
    if args.reclassificar:
        tagged, humans = reclassificar(app.conn, app.nichos)
        print(f"{tagged} lead(s) marcado(s) com nicho; {humans} com resposta de gente de verdade.\n")
    hoje = _nicho_do_dia(app)
    if hoje:
        print(f"Nicho de hoje: {app.nichos.itens[hoje].nome} ({hoje})\n")
    print(f"{'nicho':<14}{'estoque':>8}{'enviadas':>9}{'humanas':>8}{'interes.':>9}{'clientes':>9}{'taxa':>7}")
    for r in metrics.nicho_report(app.conn):
        print(f"{r['nicho']:<14}{r['estoque'] or 0:>8}{r['enviadas'] or 0:>9}{r['humanas'] or 0:>8}"
              f"{r['interessados'] or 0:>9}{r['clientes'] or 0:>9}{r['taxa']:>7.0%}")
    print("\nhumanas = respostas de gente de verdade (robô de atendimento não conta).")
    return 0


def cmd_painel(app: App, args: argparse.Namespace) -> int:
    from .web import create_app
    web = create_app(app)
    host, port = args.host or app.settings.web_host, args.port or app.settings.web_port
    print(f"Painel em http://{host}:{port}  ·  webhook em http://{host}:{port}/webhook")
    web.run(host=host, port=port, debug=False, threaded=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="prospector", description="Prospecção local: Google Maps -> WhatsApp")
    p.add_argument("--env", type=Path, help="arquivo .env (padrão: .env na raiz do projeto)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("buscar", help='busca no Google Maps, ex.: prospector buscar "pizzaria em Itaí SP"')
    a.add_argument("consulta")
    a.add_argument("--pais", help="país ISO-2 da busca (padrão BR); pega idioma e fuso do mundo.yaml")
    a.add_argument("--idioma", help="idioma da mensagem: pt, en, es, fr, de, it")
    a.add_argument("--fuso", help="fuso IANA do horário comercial, ex.: Europe/Madrid")
    a.add_argument("--cidade", help="cidade (usada se o endereço do Maps não trouxer)")
    a.set_defaults(fn=cmd_buscar)

    a = sub.add_parser("importar", help="importa um CSV já baixado do scraper")
    a.add_argument("arquivo")
    a.add_argument("--busca", required=True, help="texto da busca de origem")
    a.set_defaults(fn=cmd_importar)

    a = sub.add_parser("respostas", help="busca no WA-AKG as respostas dos leads (sem webhook)")
    a.add_argument("--loop", type=int, metavar="SEGUNDOS", help="repete a cada N segundos até ser parado")
    a.set_defaults(fn=cmd_respostas)

    sub.add_parser("reclassificar", help="reaplica listas.yaml nos leads não contatados").set_defaults(
        fn=cmd_reclassificar)

    a = sub.add_parser("fila", help="gera e mostra a fila do dia (com follow-ups vencidos)")
    a.add_argument("--busca")
    a.add_argument("--cidade")
    a.add_argument("--pais", default="BR",
                   help="países dos leads novos (ISO-2 separados por vírgula, ou 'todos'); padrão: BR")
    a.add_argument("--nicho", help="só leads deste nicho (id do nichos.yaml)")
    a.add_argument("--nicho-do-dia", action="store_true", help="começa pelo nicho do dia e completa com os demais")
    a.add_argument("--tamanho", type=int, help="itens abertos na fila (padrão: MAX_POR_DIA)")
    a.add_argument("--so-mostrar", action="store_true", help="não gera, só mostra")
    a.set_defaults(fn=cmd_fila)

    a = sub.add_parser("aprovar", help="aprova itens da fila (interativo, por id, ou --todos)")
    a.add_argument("ids", nargs="*", type=int)
    a.add_argument("--todos", action="store_true")
    a.add_argument("--liberar-em", metavar="'AAAA-MM-DD HH:MM'",
                   help="aprova agora, mas só envia a partir deste horário (horário do Brasil)")
    a.set_defaults(fn=cmd_aprovar)

    a = sub.add_parser("indicado", help="registra o contato que um atendente passou (número de quem decide)")
    a.add_argument("numero", help="celular com DDD, ex.: '14 99712-3456'")
    a.add_argument("--de", type=int, required=True, metavar="LEAD_ID", help="id do lead que indicou (aparece no aviso)")
    a.add_argument("--nome", help="nome da pessoa indicada, ex.: Rafael")
    a.set_defaults(fn=cmd_indicado)

    a = sub.add_parser("nichos", help="nicho de hoje e resultado de cada nicho (enviadas, respostas humanas...)")
    a.add_argument("--reclassificar", action="store_true", help="marca o nicho dos leads antigos e recalcula respostas humanas")
    a.set_defaults(fn=cmd_nichos)

    sub.add_parser("refazer-textos", help="refaz o texto das mensagens pendentes/aprovadas com os templates atuais"
                   ).set_defaults(fn=cmd_refazer_textos)

    a = sub.add_parser("rejeitar", help="rejeita itens da fila por id")
    a.add_argument("ids", nargs="+", type=int)
    a.set_defaults(fn=cmd_rejeitar)

    a = sub.add_parser("enviar", help="envia os aprovados com as regras de humanização")
    a.add_argument("--dry-run", action="store_true", help="faz tudo menos enviar (simula horários)")
    a.add_argument("--aguardar", action="store_true", help="espera as próximas janelas/dias em vez de sair")
    a.set_defaults(fn=cmd_enviar)

    a = sub.add_parser("teste", help="manda UMA mensagem de exemplo para o seu número")
    a.add_argument("numero")
    a.add_argument("--oferta", default="sem_site", choices=("sem_site", "so_social", "tem_site"))
    a.set_defaults(fn=cmd_teste)

    sub.add_parser("status", help="funil, limites e situação do envio").set_defaults(fn=cmd_status)
    a = sub.add_parser("financeiro", help="resumo financeiro, alertas e sugestões do gerenciador")
    a.add_argument("--notificar", action="store_true", help="manda notify-send dos alertas importantes (bom p/ cron)")
    a.set_defaults(fn=cmd_financeiro)

    sub.add_parser("checar", help="confere se está tudo pronto para rodar (WhatsApp, scraper, .env...)").set_defaults(
        fn=cmd_checar)

    a = sub.add_parser("regiao", help="cidades num raio (ex.: regiao Itaí --uf SP --raio 60 --salvar)")
    a.add_argument("cidade")
    a.add_argument("--uf", required=True)
    a.add_argument("--raio", type=float, default=50, help="km (padrão 50)")
    a.add_argument("--max", type=int, help="no máximo N cidades (as mais perto)")
    a.add_argument("--mesma-uf", action="store_true", help="só cidades do mesmo estado")
    a.add_argument("--salvar", action="store_true", help="grava as cidades no buscas.yaml")
    a.set_defaults(fn=cmd_regiao)

    a = sub.add_parser("backup", help="copia o banco para BACKUP_DIR (mantém os últimos BACKUP_MANTER)")
    a.add_argument("--destino")
    a.add_argument("--manter", type=int)
    a.add_argument("--listar", action="store_true")
    a.set_defaults(fn=cmd_backup)

    sub.add_parser("ab", help="resultado do teste A/B das primeiras mensagens").set_defaults(fn=cmd_ab)

    a = sub.add_parser("checar-sites", help="checa os sites dos leads (fora do ar, sem HTTPS, lento, celular)")
    a.add_argument("--limite", type=int, default=50)
    a.set_defaults(fn=cmd_checar_sites)

    a = sub.add_parser("checar-presenca", help="busca Instagram/site/cardápio online dos leads novos no DuckDuckGo")
    a.add_argument("--limite", type=int, default=20)
    a.set_defaults(fn=cmd_checar_presenca)

    a = sub.add_parser("lote", help="roda as buscas de buscas.yaml (nichos × cidades) ainda não feitas")
    a.add_argument("--max", type=int, help="máximo de buscas nesta execução")
    a.add_argument("--listar", action="store_true", help="só lista as pendentes")
    a.add_argument("--mundo", action="store_true", help="usa o mundo.yaml (Europa, EUA, Canadá...) em vez do buscas.yaml")
    a.add_argument("--pais", help="com --mundo: só estes países (ISO-2 separados por vírgula, ex.: ES,PT,US)")
    a.add_argument("--nicho", help="roda só as buscas deste nicho do nichos.yaml nas cidades do buscas.yaml")
    a.add_argument("--nicho-do-dia", action="store_true", help="idem, para o nicho do dia")
    a.set_defaults(fn=cmd_lote)

    a = sub.add_parser("proposta", help="gera a proposta em PDF de um projeto")
    a.add_argument("projeto", type=int)
    a.add_argument("--saida")
    a.set_defaults(fn=cmd_proposta)

    a = sub.add_parser("relatorio", help="relatório dos últimos dias (padrão: 7)")
    a.add_argument("--dias", type=int, default=7)
    a.add_argument("--notificar", action="store_true")
    a.set_defaults(fn=cmd_relatorio)

    a = sub.add_parser("canais", help="e-mail e Instagram: prospector canais email montar|listar|aprovar|enviar")
    a.add_argument("canal", choices=canais.CANAIS)
    a.add_argument("acao", choices=("montar", "listar", "aprovar", "rejeitar", "enviado", "testar", "enviar", "parar", "respostas", "followups", "resumo"))
    a.add_argument("ids", nargs="*", type=int, help="ids dos itens (aprovar/rejeitar/enviado)")
    a.add_argument("--limite", type=int, default=15, help="montar: quantos itens criar")
    a.add_argument("--todos", action="store_true", help="aprovar: todos os pendentes")
    a.add_argument("--aguardar", action="store_true", help="enviar: espera a próxima janela")
    a.add_argument("--loop", type=int, default=0, help="respostas: repete a cada N segundos")
    a.add_argument("--para", help="testar: destino do teste (padrão: SMTP_USUARIO)")
    a.add_argument("--destino", dest="ids_ou_destinos", action="append", default=[], help="parar: e-mail ou @perfil")
    a.set_defaults(fn=cmd_canais)

    a = sub.add_parser("buscar-emails", help="procura e-mail de contato nos sites dos leads novos")
    a.add_argument("--limite", type=int, default=50)
    a.set_defaults(fn=cmd_buscar_emails)

    sub.add_parser("retomar", help="libera o envio depois de uma parada de emergência").set_defaults(fn=cmd_retomar)

    a = sub.add_parser("desbloquear", help="remove um número da lista de bloqueio (correção manual)")
    a.add_argument("numero")
    a.set_defaults(fn=cmd_desbloquear)

    a = sub.add_parser("webhook", help="cadastra o webhook do prospector no WA-AKG")
    a.add_argument("url", help="ex.: http://localhost:5050/webhook")
    a.set_defaults(fn=cmd_webhook)

    a = sub.add_parser("painel", help="sobe o painel web + receptor de webhook")
    a.add_argument("--host")
    a.add_argument("--port", type=int)
    a.set_defaults(fn=cmd_painel)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        app = App.create(args.env)
    except ValueError as exc:
        print(f"Configuração inválida: {exc}", file=sys.stderr)
        return 1
    _setup_logging(app, args.verbose)
    try:
        return args.fn(app, args)
    except KeyboardInterrupt:
        print("\nInterrompido.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
