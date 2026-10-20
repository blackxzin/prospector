"""Cliente do WA-AKG (envio, presença 'digitando', checagem de número)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote

from . import httpclient as http

# Palavras que indicam sessão caída ou conta bloqueada no corpo de erro.
_FATAL_HINTS = re.compile(
    r"not connected|not ready|disconnected|logged ?out|session not found|connection closed|"
    r"blocked|banned|ban\b|forbidden|unauthori[sz]ed|rate.?limit|too many|restrict",
    re.IGNORECASE,
)
FATAL_STATUSES = frozenset({401, 403, 404, 429, 503})


class WhatsAppError(Exception):
    def __init__(self, message: str, *, status: int | None = None, body: str = "", fatal: bool = False):
        super().__init__(message)
        self.status = status
        self.body = body
        self.fatal = fatal


@dataclass(frozen=True)
class SendResult:
    status: int
    body: str


def _is_fatal(status: int, body: str) -> bool:
    return status in FATAL_STATUSES or bool(_FATAL_HINTS.search(body or ""))


@dataclass(frozen=True)
class WaClient:
    base_url: str
    session: str
    api_key: str
    request: Callable[..., http.HttpResponse] = http.request

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key}

    def _call(self, method: str, path: str, body: Any = None) -> http.HttpResponse:
        url = f"{self.base_url}/api{path}"
        try:
            resp = self.request(method, url, json_body=body, headers=self._headers(), timeout=45)
        except http.NetworkError as exc:
            raise WhatsAppError(f"WA-AKG inacessível: {exc}", fatal=True) from exc
        data = resp.json()
        flag_false = isinstance(data, dict) and (data.get("status") is False or data.get("success") is False)
        if not resp.ok or flag_false:
            raise WhatsAppError(
                f"WA-AKG {method} {path} -> {resp.status}: {resp.text[:300]}",
                status=resp.status, body=resp.text, fatal=_is_fatal(resp.status, resp.text),
            )
        return resp

    def _jid_path(self, jid: str) -> str:
        return quote(jid, safe="")

    def send_text(self, jid: str, text: str) -> SendResult:
        resp = self._call("POST", f"/messages/{quote(self.session, safe='')}/{self._jid_path(jid)}/send",
                          {"message": {"text": text}})
        return SendResult(resp.status, resp.text)

    def presence(self, jid: str, state: str) -> None:
        self._call("POST", f"/chat/{quote(self.session, safe='')}/{self._jid_path(jid)}/presence",
                   {"presence": state})

    def check_number(self, number: str) -> str | None:
        """JID oficial do número no WhatsApp, ou None se não existir."""
        resp = self._call("POST", f"/chat/{quote(self.session, safe='')}/check", {"numbers": [number]})
        data = resp.json() or {}
        if isinstance(data.get("data"), dict):  # WA-AKG embrulha a resposta em "data"
            data = data["data"]
        for result in data.get("results") or []:
            if result.get("exists"):
                return result.get("jid") or f"{number}@s.whatsapp.net"
        return None

    def chat_messages(self, jid: str) -> list[dict[str, Any]]:
        """Mensagens do chat com `jid` (enviadas e recebidas), mais recentes por último."""
        resp = self._call("GET", f"/chat/{quote(self.session, safe='')}/{self._jid_path(jid)}")
        data = (resp.json() or {}).get("data")
        return [m for m in data if isinstance(m, dict)] if isinstance(data, list) else []

    def create_webhook(self, url: str, secret: str) -> str:
        body = {"name": "prospector", "url": url, "secret": secret,
                "events": ["message.received", "connection.update"]}
        return self._call("POST", f"/webhooks/{quote(self.session, safe='')}", body).text

    def session_ok(self) -> bool:
        resp = self._call("GET", "/sessions")
        data = resp.json()
        items = data.get("data", data) if isinstance(data, dict) else data
        for item in items or []:
            if isinstance(item, dict) and self.session in (item.get("sessionId"), item.get("id"), item.get("name")):
                return str(item.get("status", "")).upper() in ("CONNECTED", "OPEN", "READY")
        return False
