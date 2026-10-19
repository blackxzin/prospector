"""Presença online do lead: busca "nome + cidade" no DuckDuckGo antes da 1ª mensagem.

O Google Maps muitas vezes não traz o site/Instagram. Aqui o lead vira so_social (achou
Instagram/Facebook), tem_site (achou site próprio) ou é descartado (já usa cardápio online
com robô de WhatsApp). Um link só conta se o nome do negócio bater com o endereço; robô só
descarta se a cidade também aparecer no resultado. Erro ou bloqueio na busca não muda nada.
"""

from __future__ import annotations

import logging
import random
import re
import sqlite3
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Callable, Mapping
from urllib.parse import parse_qs, urlparse

from . import db
from . import httpclient as http
from .classify import SEM_SITE, SO_SOCIAL, TEM_SITE, Listas, tokens

log = logging.getLogger("prospector")

DDG_URL = "https://html.duckduckgo.com/html/"
TIMEOUT_S = 10
PAUSA_S = (1.0, 2.0)
MAX_FALHAS_SEGUIDAS = 2  # bloqueado duas vezes seguidas: para de buscar nesta rodada
MIN_NOME = 5  # nome (sem conectivos) curto demais bate com qualquer endereço
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
_CONECTIVOS = frozenset({"de", "da", "do", "das", "dos", "e", "a", "o", "the", "and", "ltda", "me", "eireli"})
_SEPARADOR = re.compile(r"\s+[-–—|]\s+|\s*\|\s*")


class PresencaErro(Exception):
    """Busca falhou ou foi bloqueada: o lead fica como está."""


@dataclass(frozen=True)
class Resultado:
    url: str
    titulo: str = ""
    trecho: str = ""


@dataclass(frozen=True)
class Resumo:
    checados: int
    mudados: int
    erros: int


# ---------- busca ----------

class _Parser(HTMLParser):
    """Lê os resultados do html.duckduckgo.com (título, .result__url e trecho)."""

    def __init__(self) -> None:
        super().__init__()
        self.itens: list[dict[str, str]] = []
        self._campo: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        a = {k: v or "" for k, v in attrs}
        classes = a.get("class", "").split()
        if "result__a" in classes:
            self.itens.append({"url": _desembrulha(a.get("href", "")), "titulo": "", "trecho": ""})
            self._campo = "titulo"
        elif "result__url" in classes and self.itens:
            self.itens[-1]["url"] = _desembrulha(a.get("href", "")) or self.itens[-1]["url"]
            self._campo = None
        elif "result__snippet" in classes and self.itens:
            self._campo = "trecho"

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._campo = None

    def handle_data(self, data: str) -> None:
        if self._campo and self.itens:
            self.itens[-1][self._campo] += data


def _desembrulha(href: str) -> str:
    """//duckduckgo.com/l/?uddg=<url> -> <url>."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
        alvo = parse_qs(parsed.query).get("uddg")
        return alvo[0] if alvo else ""
    return href if parsed.scheme in ("http", "https") else ""


def parse_resultados(html: str) -> list[Resultado]:
    parser = _Parser()
    parser.feed(html)
    return [Resultado(i["url"], " ".join(i["titulo"].split()), " ".join(i["trecho"].split()))
            for i in parser.itens if i["url"]]


def _fetch_ddg(consulta: str) -> str:
    # GET cai no desafio anti-robô (HTTP 202); o formulário (POST) responde normal.
    resp = http.request("POST", DDG_URL, form={"q": consulta}, timeout=TIMEOUT_S, headers={
        "User-Agent": USER_AGENT, "Accept": "text/html", "Accept-Language": "pt-BR,pt;q=0.9"})
    if resp.status != 200 or "anomaly-modal" in resp.text:
        raise PresencaErro(f"DuckDuckGo bloqueou ou falhou (HTTP {resp.status})")
    return resp.text


def buscar(nome: str, cidade: str | None, fetch: Callable[[str], str] = _fetch_ddg) -> list[Resultado]:
    consulta = " ".join(p for p in (nome.strip(), (cidade or "").strip()) if p)
    try:
        return parse_resultados(fetch(consulta))
    except http.NetworkError as exc:
        raise PresencaErro(str(exc)) from exc


# ---------- classificação ----------

def _compacto(text: str) -> str:
    return "".join(tokens(text))


def _partes(url: str) -> tuple[str, str]:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    return (host[4:] if host.startswith("www.") else host), parsed.path or "/"


def _dominio(host: str, path: str, lista: tuple[str, ...]) -> str | None:
    """Item da lista que casa com o link ("ifood.com.br/delivery" exige o caminho)."""
    for item in lista:
        dom, _, prefixo = item.partition("/")
        if (host == dom or host.endswith("." + dom)) and path.lower().startswith("/" + prefixo):
            return item
    return None


def _palavras_nome(nome: str, cidade: str | None) -> list[str]:
    base = _SEPARADOR.split(nome.strip())[0] or nome
    palavras = [t for t in tokens(base) if t not in _CONECTIVOS]
    sem_cidade = [t for t in palavras if t not in set(tokens(cidade or ""))]
    return sem_cidade or palavras


def _nome_bate(palavras: list[str], alvo: str) -> bool:
    chave = _compacto(alvo)
    return len("".join(palavras)) >= MIN_NOME and all(p in chave for p in palavras)


def _cidade_bate(cidade: str | None, r: Resultado) -> bool:
    alvo = tokens(cidade or "")
    if not alvo:
        return False
    texto = tokens(f"{r.titulo} {r.trecho} {r.url}")
    n = len(alvo)
    return any(texto[i:i + n] == alvo for i in range(len(texto) - n + 1))


def avaliar(lead: Mapping, resultados: list[Resultado], listas: Listas) -> dict[str, object]:
    """Campos a mudar no lead (vazio = nada confirmado)."""
    palavras = _palavras_nome(lead["nome"], lead["cidade"])
    nota = social = proprio = None
    for r in resultados:
        host, path = _partes(r.url)
        if not host or _dominio(host, path, listas.dominios_diretorio):
            continue
        robo = _dominio(host, path, listas.dominios_robo)
        if robo:
            if not _nome_bate(palavras, host + path):
                continue
            if _cidade_bate(lead["cidade"], r):
                return {"status": db.DESCARTADO, "motivo": f"já usa cardápio online/robô ({robo})"}
            nota = nota or f"possível cardápio online/robô (cidade não confirmada): {host}{path}"
        elif _dominio(host, path, listas.dominios_social):
            social = social or (r.url if _nome_bate(palavras, host + path) else None)
        else:
            proprio = proprio or (r.url if _nome_bate(palavras, host) else None)
    fields: dict[str, object] = {"motivo": nota} if nota else {}
    if proprio and lead["oferta"] in (SEM_SITE, SO_SOCIAL):
        return {**fields, "oferta": TEM_SITE, "site": proprio}
    if social and lead["oferta"] == SEM_SITE:
        return {**fields, "oferta": SO_SOCIAL, "site": social}
    return fields


# ---------- aplicação ----------

class Verificador:
    """Checa um lead por vez, com pausa entre as buscas para não ser bloqueado."""

    def __init__(self, listas: Listas, *, buscar_fn: Callable[[str, str | None], list[Resultado]] = buscar,
                 sleep: Callable[[float], None] = time.sleep, rng: random.Random | None = None) -> None:
        self.listas = listas
        self._buscar = buscar_fn
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._buscou = False
        self._falhas = 0

    def __call__(self, conn: sqlite3.Connection, lead: Mapping) -> bool:
        """True se checou (e gravou presenca_checada_em); False se a busca falhou ou está suspensa."""
        if self._falhas >= MAX_FALHAS_SEGUIDAS:
            return False
        if self._buscou:
            self._sleep(self._rng.uniform(*PAUSA_S))
        self._buscou = True
        try:
            resultados = self._buscar(lead["nome"], lead["cidade"])
        except PresencaErro as exc:
            self._falhas += 1
            log.warning("checagem de presença falhou para %s (#%s): %s", lead["nome"], lead["id"], exc)
            if self._falhas >= MAX_FALHAS_SEGUIDAS:
                log.warning("checagem de presença suspensa nesta rodada (%d falhas seguidas)", self._falhas)
            return False
        self._falhas = 0
        fields = avaliar(lead, resultados, self.listas)
        if fields:
            log.info("presença de %s (#%s): %s", lead["nome"], lead["id"], fields)
        with db.tx(conn):
            db.update_lead(conn, lead["id"], **fields, presenca_checada_em=db.iso(db.utcnow()))
        return True


def checar_leads(conn: sqlite3.Connection, verificar: Callable[[sqlite3.Connection, Mapping], bool], *,
                 limite: int = 20, out: Callable[[str], None] = lambda _: None) -> Resumo:
    """Leads novos com WhatsApp ainda não checados, dos mais avaliados para os menos."""
    rows = conn.execute(
        "SELECT * FROM leads WHERE status = ? AND telefone IS NOT NULL AND presenca_checada_em IS NULL "
        "ORDER BY COALESCE(avaliacoes, 0) DESC, id LIMIT ?",
        (db.NOVO, limite),
    ).fetchall()
    checados = mudados = erros = 0
    for lead in rows:
        if not verificar(conn, lead):
            erros += 1
            out(f"  {lead['nome']}: busca falhou (fica como está)")
            continue
        checados += 1
        depois = db.get_lead(conn, lead["id"])
        mudou = any(depois[k] != lead[k] for k in ("status", "oferta", "site", "motivo"))
        mudados += mudou
        if mudou:
            out(f"  {lead['nome']}: {depois['status']} · {depois['oferta']} · "
                f"{depois['motivo'] or depois['site'] or '-'}")
    return Resumo(checados, mudados, erros)

