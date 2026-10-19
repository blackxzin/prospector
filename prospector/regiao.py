"""Cidades num raio a partir de uma cidade-base (base de municípios do IBGE com coordenadas)."""

from __future__ import annotations

import csv
import io
import math
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .classify import fold

DATASET_URL = "https://raw.githubusercontent.com/kelvins/municipios-brasileiros/main/csv/municipios.csv"
UF_CODES = {
    "RO": 11, "AC": 12, "AM": 13, "RR": 14, "PA": 15, "AP": 16, "TO": 17, "MA": 21, "PI": 22, "CE": 23,
    "RN": 24, "PB": 25, "PE": 26, "AL": 27, "SE": 28, "BA": 29, "MG": 31, "ES": 32, "RJ": 33, "SP": 35,
    "PR": 41, "SC": 42, "RS": 43, "MS": 50, "MT": 51, "GO": 52, "DF": 53,
}
UF_BY_CODE = {v: k for k, v in UF_CODES.items()}
EARTH_KM = 6371.0


class RegiaoError(ValueError):
    pass


@dataclass(frozen=True)
class Cidade:
    nome: str
    uf: str
    lat: float
    lng: float
    ddd: str
    distancia_km: float = 0.0

    @property
    def consulta(self) -> str:
        """Como a cidade entra na busca do Google Maps: 'Itaí SP'."""
        return f"{self.nome} {self.uf}"


def haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_KM * math.asin(math.sqrt(a))


def load_dataset(cache: Path, fetch=None) -> list[Cidade]:
    """Lê a base (baixa uma vez e guarda em cache)."""
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        try:
            req = urllib.request.Request(DATASET_URL, headers={"User-Agent": "prospector"})
            data = (fetch or urllib.request.urlopen)(req, timeout=30).read()
        except OSError as exc:
            raise RegiaoError(f"Não consegui baixar a base de municípios: {exc}") from exc
        cache.write_bytes(data)
    out = []
    for row in csv.DictReader(io.StringIO(cache.read_text(encoding="utf-8"))):
        try:
            out.append(Cidade(row["nome"], UF_BY_CODE[int(row["codigo_uf"])], float(row["latitude"]),
                              float(row["longitude"]), row.get("ddd") or ""))
        except (KeyError, ValueError):
            continue
    if not out:
        raise RegiaoError("Base de municípios vazia ou inválida")
    return out


def find_city(cidades: list[Cidade], nome: str, uf: str) -> Cidade:
    alvo, uf = fold(nome.strip()), uf.strip().upper()
    for c in cidades:
        if c.uf == uf and fold(c.nome) == alvo:
            return c
    parecidas = [c.nome for c in cidades if c.uf == uf and alvo in fold(c.nome)][:5]
    dica = f" Parecidas: {', '.join(parecidas)}." if parecidas else ""
    raise RegiaoError(f"Cidade '{nome}/{uf}' não encontrada.{dica}")


def nearby(cidades: list[Cidade], base: Cidade, raio_km: float, limite: int | None = None,
           mesma_uf: bool = False) -> list[Cidade]:
    """Cidades até `raio_km` da base (inclui a própria), da mais perto para a mais longe."""
    if raio_km <= 0:
        raise RegiaoError("O raio precisa ser maior que zero")
    found = []
    for c in cidades:
        if mesma_uf and c.uf != base.uf:
            continue
        d = haversine(base.lat, base.lng, c.lat, c.lng)
        if d <= raio_km:
            found.append(Cidade(c.nome, c.uf, c.lat, c.lng, c.ddd, round(d, 1)))
    found.sort(key=lambda c: c.distancia_km)
    return found[:limite] if limite else found
