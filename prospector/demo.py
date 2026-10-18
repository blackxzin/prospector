"""Site de exemplo personalizado por lead (HTML estático, sem dependências externas)."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import db
from .classify import fold
from .messages import clean_name, format_rating
from .phone import pretty

TEMPLATE_DIR = Path(__file__).parent / "templates" / "demo"
DEFAULT_NICHOS = Path(__file__).resolve().parent.parent / "nichos.yaml"
_env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=select_autoescape(["html"]))

WHATS_ICON = ('<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2a10 10 0 0 0-8.6 15.1L2 22l5-1.3A10 10 0 1 0 12 2Zm0 18.2a8.2 8.2 0 0 1-4.2-1.2l-.3-.2-3 .8.8-2.9-.2-.3A8.2 8.2 0 1 1 12 20.2Zm4.5-6.1c-.2-.1-1.5-.7-1.7-.8s-.4-.1-.6.1l-.8 1c-.1.2-.3.2-.5.1a6.7 6.7 0 0 1-3.3-2.9c-.3-.4.2-.4.7-1.3.1-.2 0-.3 0-.4l-.8-1.8c-.2-.5-.4-.4-.6-.4h-.5a1 1 0 0 0-.7.3 3 3 0 0 0-.9 2.2 5.2 5.2 0 0 0 1.1 2.7 11.8 11.8 0 0 0 4.5 4c1.7.7 2.3.8 3.2.6.5-.1 1.5-.6 1.7-1.2s.2-1.1.2-1.2-.2-.2-.5-.3Z"/></svg>')
WHATS_ICON_WHITE = WHATS_ICON.replace('fill="currentColor"', 'fill="#fff"')

SECTION_COPY = {
    "cardapio": dict(id="cardapio", kicker="Escolha o seu", acao="fazer um pedido",
                     texto="Toque em um item para pedir direto pelo WhatsApp.",
                     passos_titulo="Pedir é rápido",
                     passos=[("Escolha", "Veja o cardápio e escolha o que vai pedir."),
                             ("Chame no WhatsApp", "Toque no item ou no botão: a mensagem já vai pronta."),
                             ("Receba ou retire", "A gente confirma e prepara na hora.")],
                     final_titulo="Bateu a vontade?", final_texto="Seu pedido está a uma mensagem de distância."),
    "planos": dict(id="planos", kicker="Para cada objetivo", acao="saber mais sobre os planos",
                   texto="Escolha o plano e fale com a gente para começar.",
                   passos_titulo="Comece hoje",
                   passos=[("Escolha o plano", "Compare e veja o que combina com você."),
                           ("Chame no WhatsApp", "Tire dúvidas e combine o melhor horário."),
                           ("Comece", "Venha conhecer e já sair com tudo pronto.")],
                   final_titulo="Bora começar?", final_texto="Mande uma mensagem e a gente te ajuda a escolher."),
    "servicos": dict(id="servicos", kicker="O que fazemos", acao="agendar um horário",
                     texto="Toque em um serviço para agendar pelo WhatsApp.",
                     passos_titulo="Agendar é fácil",
                     passos=[("Escolha o serviço", "Veja os serviços e valores."),
                             ("Chame no WhatsApp", "A mensagem já vai pronta com o serviço."),
                             ("Pronto", "A gente confirma o melhor horário para você.")],
                     final_titulo="Vamos marcar?", final_texto="Seu horário está a uma mensagem de distância."),
}


def load_nichos(path: Path | None = None) -> dict[str, dict[str, Any]]:
    path = path or Path(os.environ.get("NICHOS_PATH") or DEFAULT_NICHOS)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if "geral" not in data:
        raise ValueError("nichos.yaml precisa ter o nicho 'geral'")
    for key, n in data.items():
        for field in ("arte", "fontes", "tema", "frase", "sub", "botao", "ticker", "destaques", "secao"):
            if field not in n:
                raise ValueError(f"nichos.yaml: '{key}' sem o campo '{field}'")
        if n["secao"].get("tipo") not in SECTION_COPY:
            raise ValueError(f"nichos.yaml: '{key}' com secao.tipo inválido")
    return data


def _matches(text: str, keywords: list[str]) -> bool:
    return any(re.search(r"(?<![a-z0-9])" + re.escape(fold(str(k))), text) for k in keywords)


def pick_niche(nichos: Mapping[str, Mapping[str, Any]], categoria: str, nome: str) -> str:
    """Primeiro nicho cuja palavra bate com a categoria do Maps; se nenhum bater, tenta pelo nome."""
    for text in (fold(categoria), fold(nome)):
        if not text:
            continue
        for key, n in nichos.items():
            if n.get("match") and _matches(text, n["match"]):
                return key
    return "geral"


def _initials(nome: str) -> str:
    words = [w for w in re.split(r"\s+", nome) if w and w[0].isalnum() and fold(w) not in ("de", "da", "do", "e")]
    return "".join(w[0].upper() for w in words[:2]) or "•"


def slug_for(lead: Mapping[str, Any]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", fold(clean_name(str(lead.get("nome") or "negocio")))).strip("-")[:40] or "negocio"
    # sufixo derivado da chave do lead: link difícil de adivinhar e estável entre execuções
    suffix = hashlib.sha1(f"{lead.get('chave') or lead.get('id')}".encode()).hexdigest()[:6]
    return f"{base}-{suffix}"


def render(lead: Mapping[str, Any], autor: str, today: date | None = None,
           nichos: Mapping[str, Mapping[str, Any]] | None = None) -> str:
    nichos = nichos or load_nichos()
    nome = clean_name(str(lead.get("nome") or ""))
    key = pick_niche(nichos, str(lead.get("categoria") or ""), nome)
    n = nichos[key]
    copy = SECTION_COPY[n["secao"]["tipo"]]
    nota = lead.get("nota")
    telefone = str(lead.get("telefone") or "")
    cidade = lead.get("cidade")
    endereco = lead.get("endereco")
    return _env.get_template("site.html").render(
        n=n, nicho=key, nome=nome, iniciais=_initials(nome), categoria=lead.get("categoria"), cidade=cidade,
        endereco=endereco, nota=format_rating(nota) if nota else "", avaliacoes=lead.get("avaliacoes") or 0,
        estrelas_pct=round(min(5.0, float(nota)) / 5 * 100) if nota else 0,
        telefone=pretty(telefone), telefone_num=telefone, maps_link=lead.get("maps_link"),
        mapa_q=", ".join(x for x in (endereco, cidade) if x) or None,
        secao_id=copy["id"], secao_kicker=copy["kicker"], secao_texto=copy["texto"], acao_wa=copy["acao"],
        passos_titulo=copy["passos_titulo"], passos=copy["passos"],
        final_titulo=copy["final_titulo"], final_texto=copy["final_texto"],
        whats_icon=WHATS_ICON, whats_icon_branco=WHATS_ICON_WHITE, autor=autor, ano=(today or date.today()).year,
    )


@dataclass(frozen=True)
class DemoResult:
    slug: str
    path: Path
    url: str | None


def generate(conn: sqlite3.Connection, lead_id: int, demos_dir: Path, base_url: str, autor: str,
             nichos: Mapping[str, Mapping[str, Any]] | None = None) -> DemoResult:
    lead = db.get_lead(conn, lead_id)
    if not lead:
        raise ValueError("Lead não encontrado")
    slug = slug_for(dict(lead))
    folder = demos_dir / slug
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "index.html").write_text(render(dict(lead), autor, nichos=nichos), encoding="utf-8")
    url = f"{base_url}/{slug}/" if base_url else None
    with db.tx(conn):
        db.update_lead(conn, lead_id, demo_url=url or f"local:{slug}")
    return DemoResult(slug, folder / "index.html", url)


def publish(demos_dir: Path, repo_dir: Path, run=subprocess.run) -> str:
    """Copia os exemplos para o repositório de publicação (ex.: GitHub Pages) e faz commit + push."""
    if not (repo_dir / ".git").exists():
        raise ValueError(f"{repo_dir} não é um repositório git (configure DEMO_REPO_DIR)")
    count = 0
    for folder in sorted(p for p in demos_dir.glob("*") if p.is_dir()):
        dest = repo_dir / folder.name
        dest.mkdir(exist_ok=True)
        shutil.copy2(folder / "index.html", dest / "index.html")
        count += 1
    (repo_dir / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    run(["git", "-C", str(repo_dir), "add", "-A"], check=True)
    status = run(["git", "-C", str(repo_dir), "status", "--porcelain"], check=True, capture_output=True, text=True)
    if not status.stdout.strip():
        return f"{count} exemplo(s); nada novo para publicar."
    run(["git", "-C", str(repo_dir), "commit", "-m", f"exemplos: {count} site(s)"], check=True)
    run(["git", "-C", str(repo_dir), "push"], check=True)
    return f"{count} exemplo(s) publicados."
