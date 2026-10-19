"""Checagem de prontidão: o que está ok e o que falta para rodar de verdade."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import db
from . import httpclient as http
from .config import ROOT

OK = "ok"
AVISO = "aviso"
ERRO = "erro"


@dataclass(frozen=True)
class Check:
    nome: str
    nivel: str
    detalhe: str
    como_resolver: str = ""


def _try(fn, *a, **kw):
    try:
        return fn(*a, **kw), None
    except Exception as exc:  # noqa: BLE001 - checagem nunca pode quebrar
        return None, exc


def run_checks(app: Any, *, network: bool = True, now: datetime | None = None) -> list[Check]:
    s = app.settings
    out: list[Check] = []
    add = lambda *a: out.append(Check(*a))  # noqa: E731

    add(".env", OK if (ROOT / ".env").exists() else AVISO,
        "encontrado" if (ROOT / ".env").exists() else "não existe (usando padrões)", "cp .env.example .env")

    # arquivos de configuração
    for nome, loader in (("templates.yaml", lambda: app.templates), ("listas.yaml", lambda: app.listas),
                         ("financeiro.yaml", lambda: app.finance)):
        _, err = _try(loader)
        add(nome, ERRO if err else OK, f"inválido: {err}" if err else "ok", f"corrija o {nome}")
    from .lote import BatchConfig
    cfg, err = _try(BatchConfig.load, s.buscas_path)
    add("buscas.yaml", ERRO if err else OK,
        f"inválido: {err}" if err else f"{len(cfg.nichos)} nichos × {len(cfg.cidades)} cidades",
        "prospector regiao \"Itaí\" --uf SP --raio 60 --salvar")

    # scraper
    if network:
        resp, err = _try(http.request, "GET", f"{s.gmaps_url}/api/v1/jobs", timeout=5)
        ok = resp is not None and resp.ok
        add("Scraper (Google Maps)", OK if ok else ERRO, f"respondendo em {s.gmaps_url}" if ok else
            f"sem resposta em {s.gmaps_url}", "docker compose up -d gmaps")

    # WhatsApp
    if not s.has_whatsapp:
        add("WA-AKG (WhatsApp)", ERRO, "WAAKG_SESSION e/ou WAAKG_API_KEY vazios no .env",
            "suba o WA-AKG, conecte a sessão e preencha o .env")
    elif network:
        client = app.whatsapp()
        connected, err = _try(client.session_ok)
        if err:
            add("WA-AKG (WhatsApp)", ERRO, f"não respondeu em {s.waakg_url}: {str(err)[:120]}",
                "confira se o WA-AKG está rodando e o WAAKG_URL/API key")
        else:
            add("WA-AKG (WhatsApp)", OK if connected else ERRO,
                f"sessão '{s.waakg_session}' conectada" if connected else f"sessão '{s.waakg_session}' desconectada",
                "conecte a sessão no painel do WA-AKG (QR code)")

    add("Segredo do webhook", OK if s.webhook_secret else AVISO,
        "definido" if s.webhook_secret else "vazio: respostas dos clientes não serão recebidas com segurança",
        "WEBHOOK_SECRET=$(openssl rand -hex 32) no .env e depois prospector webhook http://localhost:5050/webhook")

    add("Pix", OK if (s.pix_chave and s.pix_nome and s.pix_cidade) else AVISO,
        "configurado" if s.pix_chave else "sem chave: cobranças e propostas saem sem QR Code",
        "preencha PIX_CHAVE, PIX_NOME e PIX_CIDADE no .env")

    from .backup import last_backup_time
    last = last_backup_time(s.backup_dir)
    now = now or datetime.now()
    if last is None:
        add("Backup", AVISO, "nenhum backup ainda", "prospector backup")
    else:
        dias = (now - last).days
        add("Backup", OK if dias < 2 else AVISO, f"último há {dias} dia(s)", "prospector backup")

    add("Notificações", OK if shutil.which("notify-send") else AVISO,
        "notify-send disponível" if shutil.which("notify-send") else "notify-send não encontrado (só log)",
        "sudo pacman -S libnotify")

    stop = db.stop_reason(app.conn)
    add("Envio liberado", ERRO if stop else OK, stop or "sem parada de emergência", "prospector retomar")

    conn = app.conn
    novos = conn.execute("SELECT COUNT(*) FROM leads WHERE status = 'novo'").fetchone()[0]
    aprovados = conn.execute("SELECT COUNT(*) FROM fila WHERE status = 'aprovado'").fetchone()[0]
    pendentes = conn.execute("SELECT COUNT(*) FROM fila WHERE status = 'pendente'").fetchone()[0]
    add("Leads para contatar", OK if novos or pendentes or aprovados else AVISO,
        f"{novos} novo(s), {pendentes} na fila pendente(s), {aprovados} aprovado(s)",
        "rode buscas (prospector lote) e gere a fila (prospector fila)")
    return out


def summary(checks: list[Check]) -> str:
    erros = sum(1 for c in checks if c.nivel == ERRO)
    avisos = sum(1 for c in checks if c.nivel == AVISO)
    if erros:
        return f"{erros} problema(s) impedem o envio real"
    if avisos:
        return f"pronto para rodar, com {avisos} aviso(s)"
    return "tudo pronto para rodar"
