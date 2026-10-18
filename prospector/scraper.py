"""Cliente da API REST do gosom/google-maps-scraper (modo web)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from . import httpclient as http

POLL_SECONDS = 15
DONE_OK = "ok"
DONE_FAILED = "failed"


class ScraperError(Exception):
    pass


@dataclass(frozen=True)
class GMapsClient:
    base_url: str
    request: Callable[..., http.HttpResponse] = http.request

    def create_job(self, query: str, *, depth: int, max_time_s: int, lang: str = "pt") -> str:
        body = {
            "name": query[:100],
            "keywords": [query],
            "lang": lang,
            "depth": depth,
            "email": True,
            "max_time": max_time_s,  # a API converte para segundos
        }
        resp = self.request("POST", f"{self.base_url}/api/v1/jobs", json_body=body)
        data = resp.json() or {}
        if not resp.ok or not data.get("id"):
            raise ScraperError(f"Falha ao criar job ({resp.status}): {resp.text[:300]}")
        return str(data["id"])

    def job_status(self, job_id: str) -> str:
        resp = self.request("GET", f"{self.base_url}/api/v1/jobs/{job_id}")
        if not resp.ok:
            raise ScraperError(f"Falha ao consultar job ({resp.status}): {resp.text[:300]}")
        data = resp.json() or {}
        # A struct Go não tem tags JSON: o campo vem como "Status".
        return str(data.get("Status") or data.get("status") or "").lower()

    def download_csv(self, job_id: str) -> str:
        resp = self.request("GET", f"{self.base_url}/api/v1/jobs/{job_id}/download", timeout=120)
        if not resp.ok:
            raise ScraperError(f"Falha ao baixar CSV ({resp.status}): {resp.text[:300]}")
        return resp.text

    def wait(
        self,
        job_id: str,
        *,
        timeout_s: int,
        on_tick: Callable[[str, int], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        poll_s: int = POLL_SECONDS,
    ) -> None:
        waited = 0
        while True:
            status = self.job_status(job_id)
            if on_tick:
                on_tick(status, waited)
            if status == DONE_OK:
                return
            if status == DONE_FAILED:
                raise ScraperError(f"Job {job_id} falhou no scraper")
            if waited >= timeout_s:
                raise ScraperError(f"Job {job_id} não terminou em {timeout_s}s (status {status!r})")
            sleep(poll_s)
            waited += poll_s
