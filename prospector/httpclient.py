"""Cliente HTTP mínimo (stdlib) com JSON."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.body or b"null")
        except json.JSONDecodeError:
            return None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class NetworkError(Exception):
    """Não conseguiu falar com o servidor (conexão recusada, timeout...)."""


def request(
    method: str,
    url: str,
    *,
    json_body: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
) -> HttpResponse:
    data = None
    hdrs = {"Accept": "application/json", **(headers or {})}
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (URL vem do .env)
            return HttpResponse(resp.status, resp.read())
    except urllib.error.HTTPError as exc:
        return HttpResponse(exc.code, exc.read() or b"")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise NetworkError(f"{method} {url}: {exc}") from exc
