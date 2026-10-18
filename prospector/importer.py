"""Importação do CSV do google-maps-scraper para a tabela leads."""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import db
from .classify import Listas, classify_offer, is_franchise
from .phone import normalize_any, normalize_mobile, variants

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# "..., Itaí - SP, 18730-000"
_CITY_IN_ADDRESS = re.compile(r",\s*([^,\d][^,]*?)\s*-\s*[A-Z]{2}\b")


@dataclass(frozen=True)
class ImportResult:
    lidos: int = 0
    novos: int = 0
    duplicados: int = 0
    validos: int = 0
    descartados: int = 0
    ignorados: int = 0

    def __str__(self) -> str:
        return (f"{self.lidos} lidos, {self.novos} novos ({self.validos} válidos, "
                f"{self.descartados} descartados), {self.duplicados} duplicados, "
                f"{self.ignorados} ignorados (sem nome/telefone/link)")


def _float(value: str | None) -> float | None:
    try:
        return float(str(value).replace(",", ".")) if value not in (None, "") else None
    except ValueError:
        return None


def _int(value: str | None) -> int | None:
    f = _float(value)
    return int(f) if f is not None else None


def first_email(value: str | None) -> str | None:
    if not value:
        return None
    m = _EMAIL.search(value)
    return m.group(0).lower() if m else None


def extract_city(row: dict[str, str]) -> str | None:
    raw = row.get("complete_address") or ""
    if raw.strip().startswith("{"):
        try:
            city = (json.loads(raw).get("city") or "").strip()
            if city:
                return city
        except (json.JSONDecodeError, AttributeError):
            pass
    m = _CITY_IN_ADDRESS.search(row.get("address") or "")
    return m.group(1).strip() if m else None


def dedup_key(row: dict[str, str]) -> str | None:
    """Telefone normalizado; sem telefone, o link do Maps."""
    phone = normalize_any(row.get("phone"))
    if phone:
        return f"tel:{phone}"
    link = (row.get("link") or "").strip()
    return f"maps:{link}" if link else None


def _evaluate(row: dict[str, str], listas: Listas) -> tuple[str | None, str, str | None]:
    """(celular normalizado, status, motivo)."""
    mobile = normalize_mobile(row.get("phone"))
    franchise = is_franchise(row.get("title") or "", listas.franquias)
    if franchise:
        return mobile, db.DESCARTADO, f"franquia: {franchise}"
    if not mobile:
        return None, db.DESCARTADO, "sem celular"
    return mobile, db.NOVO, None


def import_rows(conn: sqlite3.Connection, rows: list[dict[str, str]], busca: str, listas: Listas) -> ImportResult:
    counts = {"lidos": 0, "novos": 0, "duplicados": 0, "validos": 0, "descartados": 0, "ignorados": 0}
    now = db.iso(db.utcnow())
    with db.tx(conn):
        for row in rows:
            counts["lidos"] += 1
            key = dedup_key(row)
            name = (row.get("title") or "").strip()
            if not key or not name:
                counts["ignorados"] += 1
                continue
            if conn.execute("SELECT 1 FROM leads WHERE chave = ?", (key,)).fetchone():
                counts["duplicados"] += 1
                continue
            mobile, status, motivo = _evaluate(row, listas)
            if mobile and db.is_blocked(conn, variants(mobile)):
                status, motivo = db.DESCARTADO, "bloqueado"
            conn.execute(
                "INSERT INTO leads (chave, nome, categoria, endereco, cidade, telefone_raw, telefone, site, email, "
                "nota, avaliacoes, maps_link, busca, oferta, status, motivo, criado_em, atualizado_em) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key, name, row.get("category") or None, row.get("address") or None, extract_city(row),
                    row.get("phone") or None, mobile, (row.get("website") or "").strip() or None,
                    first_email(row.get("emails")), _float(row.get("review_rating")),
                    _int(row.get("review_count")), row.get("link") or None, busca,
                    classify_offer(row.get("website"), listas.dominios_social), status, motivo, now, now,
                ),
            )
            counts["novos"] += 1
            counts["validos" if status == db.NOVO else "descartados"] += 1
    return ImportResult(**counts)


def read_csv_text(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))


def import_csv(conn: sqlite3.Connection, path: Path, busca: str, listas: Listas) -> ImportResult:
    return import_rows(conn, read_csv_text(path.read_text(encoding="utf-8", errors="replace")), busca, listas)
