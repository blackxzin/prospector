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
SITE_PROBLEMA = "site_problema"  # tem site, mas fora do ar / sem HTTPS / lento / ruim no celular


def fold(text: str) -> str:
    """Minúsculas, sem acentos."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", fold(text).replace("'", "").replace("’", "")) if t]


@dataclass(frozen=True)
class Listas:
    franquias: tuple[str, ...]
    dominios_social: tuple[str, ...]
    dominios_robo: tuple[str, ...] = ()       # cardápio/pedido online com robô de WhatsApp
    dominios_diretorio: tuple[str, ...] = ()  # guias e diretórios: ignorados na checagem de presença

    @classmethod
    def load(cls, path: Path) -> Listas:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(
            franquias=tuple(str(x) for x in data.get("franquias") or ()),
            dominios_social=_lower(data.get("dominios_social")),
            dominios_robo=_lower(data.get("dominios_robo")),
            dominios_diretorio=_lower(data.get("dominios_diretorio")),
        )


def _lower(items: list | None) -> tuple[str, ...]:
    return tuple(str(x).strip().lower() for x in items or ())


def is_franchise(name: str, franquias: tuple[str, ...]) -> str | None:
    """Devolve o nome da franquia encontrada no nome do lead, ou None.

    Compara sequências de palavras inteiras, ignorando espaços/pontuação:
    'Academia Smart Fit Itaí' e 'SMARTFIT' batem com 'Smart Fit'.
    """
    words = tokens(name)
    if not words:
        return None
    for franchise in franquias:
        target = "".join(tokens(franchise))
        if not target:
            continue
        for i in range(len(words)):
            joined = ""
            for tok in words[i:]:
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
