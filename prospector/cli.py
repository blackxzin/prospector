"""CLI: prospector buscar | importar | fila | aprovar | rejeitar | enviar | status | painel | ..."""

from __future__ import annotations

import argparse
import logging
import random
import sys
from datetime import timedelta
from pathlib import Path

from . import db
from . import fila as fq
from .app import App
from .importer import import_csv
from .phone import normalize_mobile, pretty, variants
from .scraper import ScraperError
from .sender import RealClock, run as run_sender, type_and_send
from .schedule import in_window, next_window_start, sent_last_hour, sent_today, typing_seconds
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
    print("    ---")
    print("    " + item["texto"])


# ---------- comandos ----------

def cmd_buscar(app: App, args: argparse.Namespace) -> int:
    try:
        result = app.search(args.consulta)
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


def cmd_fila(app: App, args: argparse.Namespace) -> int:
    s = app.settings
    if not args.so_mostrar:
        res = fq.build_queue(app.conn, app.templates, app.rules, db.utcnow(),
                             size=args.tamanho or s.max_per_day, followup_days=s.followup_business_days,
                             busca=args.busca, cidade=args.cidade)
        print(f"Fila gerada: {res.primeiras} novas, {res.followups} follow-ups "
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


def cmd_aprovar(app: App, args: argparse.Namespace) -> int:
    if args.todos:
        print(f"{fq.approve(app.conn, fq.pending_ids(app.conn))} itens aprovados.")
        return 0
    if args.ids:
        print(f"{fq.approve(app.conn, args.ids)} itens aprovados.")
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
            print(f"Fora do horário comercial agora. Próxima janela: {nxt:%a %d/%m %H:%M}.")
            if not args.aguardar:
                print("Use --aguardar para deixar rodando até lá.")
                return 0
    else:
        print("== DRY-RUN: nada será enviado. Horários abaixo são simulados. ==")
    report = run_sender(
        app.conn, client, app.rules, typing_min_s=s.typing_min_s, typing_max_s=s.typing_max_s,
        dry_run=args.dry_run, wait_next_day=args.aguardar, notify_enabled=s.notify,
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
    print(f"Agora: {now.astimezone(s.tz):%a %d/%m %H:%M} ({window} do horário). Próxima janela: "
          f"{next_window_start(now, app.rules).astimezone(s.tz):%a %d/%m %H:%M}")
    pend = conn.execute("SELECT status, COUNT(*) n FROM fila WHERE status IN ('pendente','aprovado') "
                        "GROUP BY status").fetchall()
    print("Fila: " + (", ".join(f"{r['status']} {r['n']}" for r in pend) or "vazia"))
    last = conn.execute("SELECT * FROM respostas ORDER BY id DESC LIMIT 5").fetchall()
    if last:
        print("Últimas respostas:")
        for r in last:
            print(f"  {r['recebido_em']} {pretty(r['telefone'])}: {(r['texto'] or '')[:100]}")
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
    a.set_defaults(fn=cmd_buscar)

    a = sub.add_parser("importar", help="importa um CSV já baixado do scraper")
    a.add_argument("arquivo")
    a.add_argument("--busca", required=True, help="texto da busca de origem")
    a.set_defaults(fn=cmd_importar)

    a = sub.add_parser("fila", help="gera e mostra a fila do dia (com follow-ups vencidos)")
    a.add_argument("--busca")
    a.add_argument("--cidade")
    a.add_argument("--tamanho", type=int, help="itens abertos na fila (padrão: MAX_POR_DIA)")
    a.add_argument("--so-mostrar", action="store_true", help="não gera, só mostra")
    a.set_defaults(fn=cmd_fila)

    a = sub.add_parser("aprovar", help="aprova itens da fila (interativo, por id, ou --todos)")
    a.add_argument("ids", nargs="*", type=int)
    a.add_argument("--todos", action="store_true")
    a.set_defaults(fn=cmd_aprovar)

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
