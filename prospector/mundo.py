"""Buscas fora do Brasil: país, idioma, fuso e cidades de cada mercado (mundo.yaml)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml


@dataclass(frozen=True)
class Origem:
    """De onde veio o lead: define como ler o telefone, em que idioma falar e em que fuso é o horário comercial."""

    pais: str = "BR"
    idioma: str = "pt"
    fuso: str = "America/Sao_Paulo"
    cidade: str | None = None  # cidade da busca, usada quando o endereço do Maps não traz a cidade

    @property
    def estrangeiro(self) -> bool:
        return self.pais.upper() != "BR"

    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.fuso)


BRASIL = Origem()


@dataclass(frozen=True)
class BuscaMundo:
    consulta: str
    origem: Origem


@dataclass(frozen=True)
class MundoConfig:
    max_por_execucao: int
    prep: dict[str, str]
    nichos: dict[str, tuple[str, ...]]
    mercados: tuple[tuple[str, str, str, tuple[tuple[str, str, str], ...]], ...]  # (ISO, nome, idioma, cidades)

    @classmethod
    def load(cls, path: Path) -> MundoConfig:
        if not path.exists():
            raise ValueError(f"{path.name} não encontrado")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        nichos = {str(lang): tuple(str(n).strip() for n in items or () if str(n).strip())
                  for lang, items in (data.get("nichos") or {}).items()}
        prep = {str(lang): str((cfg or {}).get("prep", "")).strip() for lang, cfg in (data.get("idiomas") or {}).items()}
        mercados = []
        for iso, cfg in (data.get("paises") or {}).items():
            idioma = str(cfg.get("idioma", "en"))
            cidades = []
            for c in cfg.get("cidades") or ():
                fuso = str(c["fuso"])
                ZoneInfo(fuso)  # falha cedo se o fuso estiver errado
                cidades.append((str(c["nome"]), fuso, str(c.get("idioma", idioma))))
            if not cidades:
                raise ValueError(f"{path.name}: país {iso} sem cidades")
            mercados.append((str(iso).upper(), str(cfg.get("nome") or iso), idioma, tuple(cidades)))
        for _, _, idioma, cidades in mercados:
            for lang in {idioma, *(c[2] for c in cidades)}:
                if not nichos.get(lang) or lang not in prep:
                    raise ValueError(f"{path.name}: falta 'nichos' ou 'idiomas' para {lang!r}")
        if not mercados:
            raise ValueError(f"{path.name}: preencha 'paises'")
        return cls(max(1, int(data.get("max_por_execucao", 8))), prep, nichos, tuple(mercados))

    def buscas(self, paises: set[str] | None = None) -> list[BuscaMundo]:
        """Todas as buscas, intercaladas entre países para cada execução trazer mercados variados."""
        per_country: list[list[BuscaMundo]] = []
        for iso, nome, _, cidades in self.mercados:
            if paises and iso not in paises:
                continue
            items = []
            for i, (cidade, fuso, lang) in enumerate(cidades):
                nichos = self.nichos[lang]
                for j in range(len(nichos)):
                    nicho = nichos[(i + j) % len(nichos)]  # gira o nicho entre as cidades
                    consulta = f"{nicho} {self.prep[lang]} {cidade}, {nome}"
                    items.append(BuscaMundo(consulta, Origem(iso, lang, fuso, cidade.split(",")[0].strip())))
            per_country.append(items)
        out: list[BuscaMundo] = []
        for r in range(max((len(x) for x in per_country), default=0)):
            out.extend(items[r] for items in per_country if r < len(items))
        return out
