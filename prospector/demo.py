"""Site de exemplo personalizado por lead (HTML estático, sem dependências externas)."""

from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import db
from .classify import fold
from .messages import clean_name, format_rating
from .phone import pretty

TEMPLATE_DIR = Path(__file__).parent / "templates" / "demo"
_env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=select_autoescape(["html"]))

WHATS_ICON = ('<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2a10 10 0 0 0-8.6 15.1L2 22l5-1.3A10 10 0 1 0 12 2Zm0 18.2a8.2 8.2 0 0 1-4.2-1.2l-.3-.2-3 .8.8-2.9-.2-.3A8.2 8.2 0 1 1 12 20.2Zm4.5-6.1c-.2-.1-1.5-.7-1.7-.8s-.4-.1-.6.1l-.8 1c-.1.2-.3.2-.5.1a6.7 6.7 0 0 1-3.3-2.9c-.3-.4.2-.4.7-1.3.1-.2 0-.3 0-.4l-.8-1.8c-.2-.5-.4-.4-.6-.4h-.5a1 1 0 0 0-.7.3 3 3 0 0 0-.9 2.2 5.2 5.2 0 0 0 1.1 2.7 11.8 11.8 0 0 0 4.5 4c1.7.7 2.3.8 3.2.6.5-.1 1.5-.6 1.7-1.2s.2-1.1.2-1.2-.2-.2-.5-.3Z"/></svg>')
WHATS_ICON_WHITE = WHATS_ICON.replace('fill="currentColor"', 'fill="#fff"')

THEMES = {
    "comida": dict(bg="#fbf7f2", surface="#ffffff", ink="#1f1a17", muted="#6f655d", accent="#c8361f",
                   accent_ink="#ffffff", line="#ece3d9"),
    "academia": dict(bg="#0f1113", surface="#181b1f", ink="#f2f4f5", muted="#9aa3ab", accent="#b8f13c",
                     accent_ink="#0f1113", line="#262a30"),
    "beleza": dict(bg="#fbf6f6", surface="#ffffff", ink="#2a1d22", muted="#7c6870", accent="#b0476a",
                   accent_ink="#ffffff", line="#efe1e5"),
    "geral": dict(bg="#f6f8fb", surface="#ffffff", ink="#14202e", muted="#5e6b7a", accent="#1f6fd1",
                  accent_ink="#ffffff", line="#e2e8f0"),
}

CATALOG = {
    "pizza": ("Cardápio", [("Calabresa", "Calabresa, cebola e azeitona", "R$ 49,90"),
                           ("Mussarela", "Molho de tomate e mussarela", "R$ 44,90"),
                           ("Portuguesa", "Presunto, ovo, cebola e ervilha", "R$ 54,90"),
                           ("Frango com catupiry", "Frango desfiado e catupiry", "R$ 54,90"),
                           ("Quatro queijos", "Mussarela, provolone, parmesão e catupiry", "R$ 57,90"),
                           ("Chocolate", "Pizza doce com chocolate ao leite", "R$ 47,90")]),
    "esfih": ("Cardápio", [("Esfiha de carne", "Massa fina, carne temperada", "R$ 5,50"),
                           ("Esfiha de queijo", "Mussarela e orégano", "R$ 5,50"),
                           ("Esfiha de calabresa", "Calabresa e cebola", "R$ 6,00"),
                           ("Esfiha de frango", "Frango com catupiry", "R$ 6,50"),
                           ("Kibe", "Frito na hora", "R$ 6,00"),
                           ("Combo família", "20 esfihas + refrigerante 2L", "R$ 109,90")]),
    "lanch|hamburg|burger": ("Cardápio", [("X-Burguer", "Pão, hambúrguer, queijo e salada", "R$ 22,90"),
                                          ("X-Bacon", "Hambúrguer, queijo e bacon crocante", "R$ 27,90"),
                                          ("X-Tudo", "O completo da casa", "R$ 32,90"),
                                          ("Batata frita", "Porção individual", "R$ 16,90"),
                                          ("Combo", "Lanche + batata + refrigerante", "R$ 39,90"),
                                          ("Milkshake", "Chocolate, morango ou baunilha", "R$ 18,90")]),
    "acai|açai|sorvete": ("Cardápio", [("Açaí 300 ml", "Com 3 acompanhamentos", "R$ 16,90"),
                                       ("Açaí 500 ml", "Com 4 acompanhamentos", "R$ 22,90"),
                                       ("Açaí 700 ml", "Com 5 acompanhamentos", "R$ 28,90"),
                                       ("Barca", "Para dividir", "R$ 49,90")]),
    "padaria|confeitaria|doce|bolo": ("Cardápio", [("Pão francês", "Quentinho toda hora (kg)", "R$ 16,90"),
                                                   ("Café da manhã", "Café, pão na chapa e suco", "R$ 18,90"),
                                                   ("Bolo caseiro", "Fatia", "R$ 8,90"),
                                                   ("Salgados", "Coxinha, risole, esfiha", "R$ 7,50"),
                                                   ("Torta salgada", "Fatia", "R$ 12,90"),
                                                   ("Encomendas", "Bolos e salgados para festa", "consulte")]),
    "restaurante|comida|marmit|churrasc|bar": ("Cardápio", [("Prato feito", "Arroz, feijão, salada e mistura", "R$ 24,90"),
                                                            ("Marmitex", "Grande, com duas misturas", "R$ 27,90"),
                                                            ("Executivo", "Prato do dia com sobremesa", "R$ 32,90"),
                                                            ("Porção", "Frango, mandioca ou calabresa", "R$ 39,90")]),
    "academia|fitness|crossfit|pilates|studio|muscula|funcional": (
        "Planos", [("Mensal", "Musculação livre", "R$ 99,90"), ("Trimestral", "Musculação + avaliação", "R$ 269,90"),
                   ("Semestral", "Musculação + avaliação física", "R$ 499,90"),
                   ("Anual", "Melhor preço por mês", "R$ 899,90"), ("Diária", "Treine quando quiser", "R$ 20,00"),
                   ("Personal", "Aulas acompanhadas", "consulte")]),
    "barbe": ("Serviços", [("Corte", "Máquina e tesoura", "R$ 35,00"), ("Barba", "Toalha quente e navalha", "R$ 25,00"),
                           ("Corte + barba", "Combo completo", "R$ 55,00"), ("Sobrancelha", "Na navalha", "R$ 15,00"),
                           ("Pigmentação", "Barba ou cabelo", "R$ 30,00"), ("Pacote mensal", "4 cortes", "R$ 120,00")]),
    "salao|salão|beleza|estetica|estética|manicure|cabele": (
        "Serviços", [("Corte feminino", "Lavagem e finalização", "R$ 60,00"), ("Escova", "Modelada", "R$ 45,00"),
                     ("Coloração", "A partir de", "R$ 120,00"), ("Manicure e pedicure", "Pés e mãos", "R$ 50,00"),
                     ("Design de sobrancelha", "Com henna", "R$ 35,00"), ("Progressiva", "A partir de", "R$ 180,00")]),
}
DEFAULT_CATALOG = ("Serviços", [("Atendimento", "Fale com a gente pelo WhatsApp", "consulte"),
                                ("Orçamento", "Sem compromisso", "grátis"),
                                ("Entrega", "Consulte a região", "consulte")])


def _match(categoria: str, nome: str) -> tuple[str, list[tuple[str, str, str]], str]:
    text = fold(f"{categoria} {nome}")
    for keys, (titulo, itens) in CATALOG.items():
        if any(fold(k) in text for k in keys.split("|")):
            tema = ("academia" if titulo == "Planos" else "beleza" if keys.startswith(("salao", "barbe"))
                    else "comida" if titulo == "Cardápio" else "geral")
            return titulo, itens, tema
    titulo, itens = DEFAULT_CATALOG
    return titulo, itens, "geral"


def slug_for(lead: Mapping[str, Any]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", fold(clean_name(str(lead.get("nome") or "negocio")))).strip("-")[:40] or "negocio"
    # sufixo derivado da chave do lead: link difícil de adivinhar e estável entre execuções
    suffix = hashlib.sha1(f"{lead.get('chave') or lead.get('id')}".encode()).hexdigest()[:6]
    return f"{base}-{suffix}"


def render(lead: Mapping[str, Any], autor: str, today: date | None = None) -> str:
    nome = clean_name(str(lead.get("nome") or ""))
    titulo, itens, tema = _match(str(lead.get("categoria") or ""), nome)
    telefone = str(lead.get("telefone") or "")
    msg = quote("Olá! Vi o site de vocês e gostaria de fazer um pedido." if titulo == "Cardápio"
                else "Olá! Vi o site de vocês e gostaria de mais informações.")
    chamadas = {
        "Cardápio": "Peça agora pelo WhatsApp: rápido, fácil e sem taxa de aplicativo.",
        "Planos": "Comece hoje. Veja os planos e fale com a gente pelo WhatsApp.",
        "Serviços": "Agende seu horário em poucos segundos pelo WhatsApp.",
    }
    return _env.get_template("site.html").render(
        nome=nome, categoria=lead.get("categoria"), cidade=lead.get("cidade"), endereco=lead.get("endereco"),
        nota=format_rating(lead.get("nota")) if lead.get("nota") else "", avaliacoes=lead.get("avaliacoes") or 0,
        telefone=pretty(telefone) or "consulte", maps_link=lead.get("maps_link"),
        whatsapp=f"https://wa.me/{telefone}?text={msg}" if telefone else "#",
        chamada=chamadas[titulo], botao="Pedir pelo WhatsApp" if titulo == "Cardápio" else "Chamar no WhatsApp",
        secao_titulo=titulo,
        secao_sub="Itens e valores ilustrativos: na versão final entram os de vocês.",
        itens=itens, tema=THEMES[tema], icone_whats=WHATS_ICON, icone_whats_branco=WHATS_ICON_WHITE,
        autor=autor, ano=(today or date.today()).year,
    )


@dataclass(frozen=True)
class DemoResult:
    slug: str
    path: Path
    url: str | None


def generate(conn: sqlite3.Connection, lead_id: int, demos_dir: Path, base_url: str, autor: str) -> DemoResult:
    lead = db.get_lead(conn, lead_id)
    if not lead:
        raise ValueError("Lead não encontrado")
    slug = slug_for(dict(lead))
    folder = demos_dir / slug
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "index.html").write_text(render(dict(lead), autor), encoding="utf-8")
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
