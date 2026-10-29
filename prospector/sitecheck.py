"""Checa o site de quem já tem site: fora do ar, sem HTTPS, lento, ruim no celular.

Os links vêm do Google Maps (dado externo), então a checagem só acessa IPs públicos,
segue no máximo 3 redirecionamentos (cada um revalidado), tem timeout e limite de bytes.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Callable
from urllib.parse import urljoin, urlparse

from . import db
from .classify import SITE_PROBLEMA, TEM_SITE

TIMEOUT_S = 10
SLOW_S = 5.0
MAX_BYTES = 512 * 1024
MAX_REDIRECTS = 3
USER_AGENT = "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/124 Mobile Safari/537.36"
_VIEWPORT = re.compile(rb"<meta[^>]+name=[\"']?viewport", re.IGNORECASE)
_WHATSAPP = re.compile(rb"wa\.me|whatsapp", re.IGNORECASE)  # qualquer menção, inclusive em script de widget
_TEL = re.compile(rb"href=[\"']?tel:", re.IGNORECASE)
_DESCRIPTION = re.compile(rb"<meta[^>]+name=[\"']?description", re.IGNORECASE)
_COPYRIGHT_YEAR = re.compile(rb"(?:&copy;|&#169;|\xc2\xa9|copyright)[^0-9<]{0,30}((?:19|20)\d\d)", re.IGNORECASE)
MIN_BODY_FOR_SIGNALS = 1500   # página minúscula/SPA sem conteúdo: não dá para afirmar que falta alguma coisa
STALE_FOOTER_YEARS = 3


class UnsafeUrl(Exception):
    pass


@dataclass(frozen=True)
class SiteReport:
    url: str
    problems: tuple[str, ...]
    status: int | None = None
    seconds: float | None = None
    sinais: tuple[str, ...] = ()   # fatos lidos no site que não são "problema" (sem_whatsapp, sem_telefone...)

    @property
    def ok(self) -> bool:
        return not self.problems

    def summary(self) -> str:
        return "; ".join(self.problems)


def _check_host(host: str) -> None:
    if not host:
        raise UnsafeUrl("sem host")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeUrl(f"domínio não existe ({host})") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise UnsafeUrl(f"endereço não público ({ip})")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # type: ignore[override]
        return None  # tratamos o redirecionamento na mão, para revalidar o destino


_opener = urllib.request.build_opener(_NoRedirect)


def _fetch(url: str) -> tuple[int, str, bytes, float]:
    """(status, url_final, corpo, segundos) seguindo redirecionamentos com validação."""
    start = time.monotonic()
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        parsed = urlparse(current)
        if parsed.scheme not in ("http", "https"):
            raise UnsafeUrl(f"esquema não suportado ({parsed.scheme})")
        _check_host(parsed.hostname or "")
        req = urllib.request.Request(current, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
        try:
            resp = _opener.open(req, timeout=TIMEOUT_S)  # noqa: S310 (URL validada acima)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
                current = urljoin(current, exc.headers["Location"]).replace(" ", "%20")   # Location com espaço (wa.me/send?text=...)
                continue
            return exc.code, current, b"", time.monotonic() - start
        with resp:
            body = resp.read(MAX_BYTES)
            return resp.status, current, body, time.monotonic() - start
    raise UnsafeUrl("redirecionamentos demais")


def extract_signals(body: bytes, year: int | None = None) -> tuple[str, ...]:
    """Fatos objetivos do HTML da página inicial, usados para personalizar a abordagem (nunca para acusar)."""
    if len(body) < MIN_BODY_FOR_SIGNALS:
        return ()
    found: list[str] = []
    if not _WHATSAPP.search(body):
        found.append("sem_whatsapp")
    if not _TEL.search(body):
        found.append("sem_telefone")
    if not _DESCRIPTION.search(body):
        found.append("sem_descricao")
    years = [int(m.group(1)) for m in _COPYRIGHT_YEAR.finditer(body)]
    if years and max(years) <= (year or date.today().year) - STALE_FOOTER_YEARS:
        found.append(f"rodape_{max(years)}")
    return tuple(found)


def check_site(url: str, fetch: Callable[[str], tuple[int, str, bytes, float]] = _fetch) -> SiteReport:
    url = url.strip()
    if "://" not in url:
        url = "http://" + url
    try:
        status, final, body, seconds = fetch(url)
    except UnsafeUrl as exc:
        msg = str(exc)
        if msg.startswith("domínio não existe"):
            return SiteReport(url, ("o endereço do site não abre (domínio não existe mais)",))
        return SiteReport(url, ())  # não checável com segurança: não acusa problema
    except ValueError:   # URL inválida num redirecionamento (http.client.InvalidURL): não dá para checar, não acusa
        return SiteReport(url, ())
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return SiteReport(url, ("o site está fora do ar",))
    problems: list[str] = []
    if status >= 400:
        problems.append("o site está fora do ar" if status >= 500 or status == 404 else f"o site dá erro ({status})")
        return SiteReport(final, tuple(problems), status, seconds, extract_signals(body))
    if urlparse(final).scheme != "https":
        problems.append("o site aparece como “não seguro” no navegador (sem HTTPS)")
    if seconds >= SLOW_S:
        problems.append(f"o site demora uns {seconds:.0f} segundos para abrir")
    if body and not _VIEWPORT.search(body):
        problems.append("o site não se adapta bem ao celular")
    return SiteReport(final, tuple(problems), status, seconds, extract_signals(body))


def check_leads(conn: sqlite3.Connection, *, limit: int = 50, check: Callable[[str], SiteReport] = check_site,
                out: Callable[[str], None] = print) -> tuple[int, int]:
    """Checa leads novos com site próprio. Com problema: oferta vira site_problema. Devolve (checados, com_problema)."""
    rows = conn.execute(
        # só Brasil: os diagnósticos são em português e, de fora, muito site "fora do ar" é só bloqueio (Cloudflare)
        "SELECT id, nome, site FROM leads WHERE status = ? AND oferta = ? AND site IS NOT NULL AND pais = 'BR' "
        "AND (site_checado_em IS NULL OR site_sinais IS NULL) ORDER BY id LIMIT ?",
        (db.NOVO, TEM_SITE, limit),
    ).fetchall()
    bad = 0
    for row in rows:
        report = check(row["site"])
        fields: dict[str, object] = {"site_checado_em": db.iso(db.utcnow()),
                                     "site_problemas": report.summary() or None,
                                     "site_sinais": json.dumps(list(report.sinais))}
        if not report.ok:
            fields["oferta"] = SITE_PROBLEMA
            bad += 1
            out(f"  {row['nome']}: {report.summary()}")
        with db.tx(conn):
            db.update_lead(conn, row["id"], **fields)
    return len(rows), bad
