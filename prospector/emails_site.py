"""Procura o e-mail de contato no site do próprio lead (mailto: e página de contato).

Só usa o domínio do próprio negócio ou e-mail pessoal (gmail, hotmail...), para não pegar o e-mail da agência que
fez o site. Reaproveita o fetch do sitecheck (só IPs públicos, redirecionamentos validados, limite de bytes)."""

from __future__ import annotations

import html
import re
import sqlite3
from typing import Callable
from urllib.parse import urljoin, urlparse

from . import db
from .canais import valid_email
from .classify import SITE_PROBLEMA, TEM_SITE
from .sitecheck import UnsafeUrl, _fetch

Fetch = Callable[[str], tuple[int, str, bytes, float]]

MAX_CONTACT_PAGES = 2
FREE_PROVIDERS = frozenset({"gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "yahoo.com.br", "live.com", "icloud.com",
                            "uol.com.br", "bol.com.br", "terra.com.br"})
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_HREF = re.compile(r"""href=["']([^"'#]+)["']""", re.IGNORECASE)
_CONTACT_HINT = re.compile(r"contato|contact|fale-?conosco|atendimento|sobre", re.IGNORECASE)
_NOT_OWN_SITE = re.compile(r"instagram\.com|facebook\.com|linktr\.ee|play\.google|wa\.me|whatsapp|ifood|wixsite|ueniweb|"
                           r"beacons\.ai|bio\.link|tiktok", re.IGNORECASE)


def _root_domain(host: str) -> str:
    parts = (host or "").lower().removeprefix("www.").split(".")
    return ".".join(parts[-3:] if len(parts) >= 3 and parts[-2] in ("com", "org", "net", "gov") else parts[-2:])


def extract_emails(body: bytes, site_host: str) -> list[str]:
    """E-mails válidos da página, do domínio do site primeiro e e-mails pessoais depois; o resto é descartado."""
    text = html.unescape(body.decode("utf-8", errors="ignore"))
    own = _root_domain(site_host)
    own_hits: list[str] = []
    free_hits: list[str] = []
    for raw in _EMAIL.findall(text):
        addr = raw.strip(".").lower()
        if not valid_email(addr):
            continue
        domain = addr.rsplit("@", 1)[1]
        bucket = own_hits if _root_domain(domain) == own else free_hits if domain in FREE_PROVIDERS else None
        if bucket is not None and addr not in own_hits + free_hits:
            bucket.append(addr)
    return own_hits + free_hits


def _contact_links(body: bytes, base: str) -> list[str]:
    host = urlparse(base).netloc
    links: list[str] = []
    for href in _HREF.findall(body.decode("utf-8", errors="ignore")):
        url = urljoin(base, html.unescape(href))
        if urlparse(url).netloc == host and _CONTACT_HINT.search(urlparse(url).path) and url not in links:
            links.append(url)
    return links[:MAX_CONTACT_PAGES]


def find_email(site: str, fetch: Fetch = _fetch) -> str | None:
    """Primeiro e-mail do negócio na página inicial ou nas páginas de contato; None se não achar."""
    try:
        status, final, body, _ = fetch(site)
    except (UnsafeUrl, OSError, ValueError):
        return None
    if status >= 400 or not body:
        return None
    host = urlparse(final).hostname or ""
    found = extract_emails(body, host)
    for url in _contact_links(body, final):
        if found:
            break
        try:
            status, _, page, _ = fetch(url)
        except (UnsafeUrl, OSError, ValueError):
            continue
        if status < 400:
            found = extract_emails(page, host)
    return found[0] if found else None


def enrich(conn: sqlite3.Connection, *, limit: int = 50, find: Callable[[str], str | None] = find_email,
           out: Callable[[str], None] = print) -> tuple[int, int]:
    """Procura e-mail nos leads brasileiros novos com site próprio. Devolve (consultados, com e-mail achado)."""
    rows = conn.execute(
        "SELECT id, nome, site FROM leads WHERE status = ? AND oferta IN (?, ?) AND site IS NOT NULL AND pais = 'BR' "
        "AND COALESCE(email, '') = '' AND email_buscado_em IS NULL ORDER BY COALESCE(avaliacoes, 0) DESC, id LIMIT ?",
        (db.NOVO, TEM_SITE, SITE_PROBLEMA, limit)).fetchall()
    found = checked = 0
    for row in rows:
        if _NOT_OWN_SITE.search(row["site"]):
            fields: dict[str, object] = {"email_buscado_em": db.iso(db.utcnow())}
        else:
            checked += 1
            email = find(row["site"])
            fields = {"email_buscado_em": db.iso(db.utcnow())}
            if email:
                fields["email"] = email
                found += 1
                out(f"  {row['nome']}: {email}")
        with db.tx(conn):
            db.update_lead(conn, row["id"], **fields)
    return checked, found
