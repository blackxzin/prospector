"""Filtros e classificação de leads: franquias e tipo de oferta."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

SEM_SITE = "sem_site"
SO_SOCIAL = "so_social"
TEM_SITE = "tem_site"


def fold(text: str) -> str:
    """Minúsculas, sem acentos."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", fold(text).replace("'", "").replace("’", "")) if t]


@dataclass(frozen=True)
class Listas:
    franquias: tuple[str, ...]
    dominios_social: tuple[str, ...]

    @classmethod
    def load(cls, path: Path) -> Listas:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(
            franquias=tuple(str(x) for x in data.get("franquias") or ()),
            dominios_social=tuple(str(x).lower() for x in data.get("dominios_social") or ()),
        )


def is_franchise(name: str, franquias: tuple[str, ...]) -> str | None:
    """Devolve o nome da franquia encontrada no nome do lead, ou None.

    Compara sequências de palavras inteiras, ignorando espaços/pontuação:
    'Academia Smart Fit Itaí' e 'SMARTFIT' batem com 'Smart Fit'.
    """
    tokens = _tokens(name)
    if not tokens:
        return None
    for franchise in franquias:
        target = "".join(_tokens(franchise))
        if not target:
            continue
        for i in range(len(tokens)):
            joined = ""
            for tok in tokens[i:]:
                joined += tok
                if joined == target:
                    return franchise
                if len(joined) >= len(target) or not target.startswith(joined):
                    break
    return None


def _host(url: str) -> str:
    url = url.strip()
    if "://" not in url:
        url = "http://" + url
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def classify_offer(website: str | None, dominios_social: tuple[str, ...]) -> str:
    """sem_site | so_social | tem_site."""
    if not website or not website.strip():
        return SEM_SITE
    host = _host(website)
    if not host:
        return SEM_SITE
    for domain in dominios_social:
        if host == domain or host.endswith("." + domain):
            return SO_SOCIAL
    return TEM_SITE
