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


# ---------------------------------------------------------------------------
# v2 (protótipo): pizzaria com dados reais do Google + fotos CC0
# ---------------------------------------------------------------------------

STATIC_DEMO = Path(__file__).parent / "static" / "demo"
DIAS = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo")
_SAFE_PHOTO_HOSTS = (".googleusercontent.com", ".ggpht.com")
MAX_PHOTO_BYTES = 5 * 1024 * 1024

PIZZARIA_V2 = {
    "sabores": ["Calabresa", "Marguerita", "Portuguesa", "Frango com catupiry", "Quatro queijos", "Pepperoni",
                "Napolitana", "Chocolate", "Romeu e Julieta"],
    "destaques": [
        {"nome": "Pepperoni", "tag": "A mais pedida", "desc": "Pepperoni fatiado, mussarela e borda dourada.",
         "foto": "mesa-restaurante.jpg"},
        {"nome": "Especial da casa", "tag": "Pra dividir", "desc": "Recheio generoso pra família toda.",
         "foto": "mesa-familia.jpg"},
        {"nome": "Delivery", "tag": "Chega quentinha", "desc": "Do forno pra caixa, da caixa pra sua mesa.",
         "foto": "entrega-caixa.jpg"},
    ],
    "cardapio": [
        {"nome": "Tradicionais", "sub": "As clássicas de sempre", "itens": [
            ["Mussarela", "Molho de tomate, mussarela e orégano", "R$ 44,90"],
            ["Calabresa", "Calabresa fatiada, cebola e azeitona", "R$ 49,90"],
            ["Marguerita", "Mussarela, tomate e manjericão", "R$ 49,90"],
            ["Portuguesa", "Presunto, ovo, cebola e ervilha", "R$ 54,90"]]},
        {"nome": "Especiais", "sub": "Pra quem quer caprichar", "itens": [
            ["Frango com catupiry", "Frango desfiado temperado e catupiry", "R$ 54,90"],
            ["Quatro queijos", "Mussarela, provolone, parmesão e catupiry", "R$ 57,90"],
            ["Pepperoni", "Pepperoni fatiado e mussarela", "R$ 59,90"],
            ["Napolitana", "Mussarela, tomate e parmesão", "R$ 54,90"]]},
        {"nome": "Doces", "sub": "Pra fechar com chave de ouro", "itens": [
            ["Chocolate", "Chocolate ao leite e granulado", "R$ 47,90"],
            ["Romeu e Julieta", "Goiabada cremosa e mussarela", "R$ 47,90"],
            ["Banana com canela", "Banana, açúcar, canela e leite condensado", "R$ 47,90"]]},
    ],
}

_ATTR_SHORT = (
    ("cadeira de rodas", "Acessível para cadeirantes"), ("entrega", "Faz entrega"), ("delivery", "Faz entrega"),
    ("retirada", "Retirada no local"), ("cartão de crédito", "Aceita cartão"), ("cartões de débito", "Aceita cartão"),
    ("pix", "Aceita Pix"), ("crianças", "Bom para crianças"), ("grupos", "Bom para grupos"),
    ("wi-fi", "Wi-Fi"), ("estacionamento", "Estacionamento"), ("consumo no local", "Mesas no local"),
)


def _clean_display_name(nome: str) -> str:
    """'Pizzaria Antônia a' -> 'Pizzaria Antônia' (o Google às vezes traz letras soltas no fim)."""
    words = clean_name(nome).split()
    while len(words) > 1 and len(words[-1]) == 1 and words[-1].isalpha():
        words.pop()
    return " ".join(words)


def _short_name(nome: str) -> str:
    stripped = re.sub(r"^(pizzaria|pizzas?)\s+(d[aeo]s?\s+)?", "", nome, flags=re.IGNORECASE).strip()
    return stripped or nome


def _fmt_time(hh: str, mm: str) -> str:
    if hh in ("00", "24") and mm == "00":
        return "meia-noite"
    return f"{int(hh)}h" + ("" if mm == "00" else mm)


def _fmt_range(faixa: str) -> str:
    out = re.sub(r"(\d{2}):(\d{2})", lambda m: _fmt_time(m.group(1), m.group(2)), faixa).replace("–", " às ")
    return out.replace("às meia-noite", "à meia-noite")


def hours_rows(horarios_json: str | None) -> list[tuple[str, str]]:
    import json
    try:
        data = json.loads(horarios_json) if horarios_json else {}
    except ValueError:
        return []
    if not isinstance(data, dict) or not data:
        return []
    rows = []
    for dia in DIAS:
        faixas = data.get(dia) or []
        txt = ", ".join(_fmt_range(f) for f in faixas if f and "fechad" not in f.lower())
        rows.append((dia.replace("-feira", ""), txt or "Fechado"))
    return rows


_DIA_CURTO = {"segunda": "seg", "terça": "ter", "quarta": "qua", "quinta": "qui", "sexta": "sex",
              "sábado": "sáb", "domingo": "dom"}


def hours_summary(rows: list[tuple[str, str]]) -> str | None:
    """Ex.: 'Ter a dom · a partir das 18h' ou 'Todos os dias · 18h às 23h30'."""
    abertos = [(d, f) for d, f in rows if f != "Fechado"]
    if not abertos:
        return None
    faixas = {f for _, f in abertos}
    if len(abertos) == 7:
        dias = "Todos os dias"
    else:
        idx = [i for i, (_, f) in enumerate(rows) if f != "Fechado"]
        contiguous = idx == list(range(idx[0], idx[-1] + 1))
        if not contiguous:
            return None
        dias = f"{_DIA_CURTO[rows[idx[0]][0]].capitalize()} a {_DIA_CURTO[rows[idx[-1]][0]]}"
    if len(faixas) == 1:
        return f"{dias} · {next(iter(faixas))}"
    inicios = {re.split(r" às? ", f)[0] for f in faixas}
    if len(inicios) == 1:
        return f"{dias} · a partir das {next(iter(inicios))}"
    return dias


def short_attributes(atributos_json: str | None, limit: int = 5) -> list[str]:
    import json
    try:
        attrs = json.loads(atributos_json) if atributos_json else []
    except ValueError:
        return []
    out: list[str] = []
    for a in attrs:
        low = str(a).lower()
        for key, label in _ATTR_SHORT:
            if key in low and label not in out:
                out.append(label)
                break
    return out[:limit]


def good_reviews(avaliacoes_json: str | None, limit: int = 5) -> list[dict[str, Any]]:
    import json
    try:
        reviews = json.loads(avaliacoes_json) if avaliacoes_json else []
    except ValueError:
        return []
    good = [r for r in reviews if int(r.get("nota") or 0) >= 4 and len(r.get("texto") or "") >= 12][:limit]
    # avaliação de anos atrás enfraquece: mostra a data só quando é recente
    return [{**r, "recente": not re.search(r"\d+\s+anos", str(r.get("quando") or ""))} for r in good]


def fetch_photo(url: str | None, dest: Path, opener=None) -> bool:
    """Baixa a foto do Google do negócio (só hosts do Google, com limite de tamanho)."""
    import urllib.request
    from urllib.parse import urlparse
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(_SAFE_PHOTO_HOSTS):
        return False
    # pede uma versão maior da foto (o Google aceita o sufixo =w1200)
    big = re.sub(r"=w\d+-h\d+.*$|=s\d+.*$", "", url) + "=w1200"
    try:
        req = urllib.request.Request(big, headers={"User-Agent": "Mozilla/5.0"})
        with (opener or urllib.request.urlopen)(req, timeout=20) as resp:
            data = resp.read(MAX_PHOTO_BYTES + 1)
    except OSError:
        return False
    if len(data) > MAX_PHOTO_BYTES or not data.startswith((b"\xff\xd8", b"\x89PNG", b"RIFF")):
        return False
    dest.write_bytes(data)
    return True


def render_pizzaria_v2(lead: Mapping[str, Any], autor: str, *, foto_real: str | None, assets: str = "assets",
                       today: date | None = None) -> str:
    nome = _clean_display_name(str(lead.get("nome") or ""))
    nota = lead.get("nota")
    telefone = str(lead.get("telefone") or "")
    rows = hours_rows(lead.get("horarios_json"))
    endereco, cidade = lead.get("endereco"), lead.get("cidade")
    if endereco and " - " in endereco and fold(endereco).startswith(fold(str(lead.get("nome") or ""))[:8]):
        endereco = endereco.split(" - ", 1)[1]  # o Google às vezes põe o nome do negócio antes do endereço
    lat, lng = lead.get("latitude"), lead.get("longitude")
    mapa = f"{lat},{lng}" if lat and lng else ", ".join(x for x in (endereco, cidade) if x) or None
    return _env.get_template("pizzaria.html").render(
        nome=nome, nome_curto=_short_name(nome), cidade=cidade, endereco=endereco,
        nota=format_rating(nota) if nota else "", avaliacoes=lead.get("avaliacoes") or 0,
        telefone=pretty(telefone), telefone_num=telefone, maps_link=lead.get("maps_link"),
        mapa_q=mapa,
        horarios=rows, horario_hoje=hours_summary(rows), atributos=short_attributes(lead.get("atributos_json")),
        reviews=good_reviews(lead.get("avaliacoes_json")), foto_real=foto_real, assets=assets,
        whats=WHATS_ICON, autor=autor, ano=(today or date.today()).year, **PIZZARIA_V2,
    )


def generate_v2(lead: Mapping[str, Any], demos_dir: Path, autor: str) -> Path:
    """Gera o protótipo v2 numa pasta própria, com as fotos copiadas junto (site autossuficiente)."""
    folder = demos_dir / (slug_for(lead) + "-v2")
    assets = folder / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    for f in (STATIC_DEMO / "pizzaria").glob("*.jpg"):
        shutil.copy2(f, assets / f.name)
    foto = "assets/fachada.jpg" if fetch_photo(lead.get("foto_url"), assets / "fachada.jpg") else None
    (folder / "index.html").write_text(render_pizzaria_v2(lead, autor, foto_real=foto), encoding="utf-8")
    return folder
