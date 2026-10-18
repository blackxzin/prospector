"""Geração das mensagens a partir do templates.yaml."""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Mapping

import yaml

from .classify import SEM_SITE, SO_SOCIAL, TEM_SITE, fold

OFFERS = (SEM_SITE, SO_SOCIAL, TEM_SITE)
MIN_VARIANTS = 3
_NAME_SUFFIX = re.compile(r"\s+[-|–—•·]\s+.*$")
_PARENS = re.compile(r"\s*\([^)]*\)\s*")
_SPACES = re.compile(r"\s{2,}")


class TemplateError(ValueError):
    pass


@dataclass(frozen=True)
class Templates:
    data: Mapping[str, Any]

    @classmethod
    def load(cls, path: Path) -> Templates:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        tpl = cls(data)
        tpl.validate()
        return tpl

    def validate(self) -> None:
        primeira = self.data.get("primeira") or {}
        for offer in OFFERS:
            if len(primeira.get(offer) or []) < MIN_VARIANTS:
                raise TemplateError(f"templates.yaml: 'primeira.{offer}' precisa de pelo menos {MIN_VARIANTS} variações")
        for key in ("followup", "fora_do_horario", "elogios_com_nota", "elogios_sem_nota"):
            if not self.data.get(key):
                raise TemplateError(f"templates.yaml: '{key}' está vazio")
        if not self.data.get("remetente"):
            raise TemplateError("templates.yaml: 'remetente' está vazio")
        # Renderiza tudo uma vez com dados fictícios para pegar {variavel} errada.
        sample = {"nome": "Teste", "nota": 4.8, "avaliacoes": 50, "categoria": "Pizzaria", "oferta": SEM_SITE}
        for text in self._all_texts():
            try:
                text.format(**self._context(sample, datetime(2026, 1, 5, 10), random.Random(0)))
            except (KeyError, IndexError, ValueError) as exc:
                raise TemplateError(f"templates.yaml: variável inválida em {text[:60]!r}: {exc}") from exc

    def _all_texts(self) -> list[str]:
        texts = [t for offer in OFFERS for t in self.data["primeira"][offer]]
        for key in ("followup", "fora_do_horario", "elogios_com_nota", "elogios_sem_nota"):
            texts.extend(self.data[key])
        return texts

    # ---------- contexto ----------

    def _vitrine(self, categoria: str | None) -> str:
        cfg = self.data.get("vitrine") or {}
        cat = fold(categoria or "")
        for rule in cfg.get("por_categoria") or []:
            if any(fold(str(w)) in cat for w in rule.get("palavras") or []):
                return str(rule.get("texto"))
        return str(cfg.get("padrao") or "os serviços")

    def _elogio(self, nota: float | None, avaliacoes: int | None, rng: random.Random, base: dict[str, str]) -> str:
        min_nota = float(self.data.get("elogio_nota_minima", 4.0))
        min_aval = int(self.data.get("elogio_avaliacoes_minimas", 10))
        if nota is not None and avaliacoes is not None and nota >= min_nota and avaliacoes >= min_aval:
            return rng.choice(self.data["elogios_com_nota"]).format(**base)
        return rng.choice(self.data["elogios_sem_nota"]).format(**base)

    def _context(self, lead: Mapping[str, Any], now_local: datetime, rng: random.Random) -> dict[str, str]:
        nota = lead.get("nota")
        avaliacoes = lead.get("avaliacoes")
        hello = greeting(now_local)
        base = {
            "nome": clean_name(str(lead.get("nome") or "")),
            "remetente": str(self.data["remetente"]),
            "saudacao": hello,
            "saudacao_minuscula": hello.lower(),
            "nota": format_rating(nota),
            "avaliacoes": str(avaliacoes or 0),
            "vitrine": self._vitrine(lead.get("categoria")),
        }
        return {**base, "elogio": self._elogio(nota, avaliacoes, rng, base)}

    # ---------- render ----------

    def _render(self, options: list[str], lead: Mapping[str, Any], now: datetime, tz: tzinfo,
                rng: random.Random | None) -> str:
        rng = rng or random.Random()
        ctx = self._context(lead, now.astimezone(tz), rng)
        return tidy(rng.choice(options).format(**ctx))

    def first_message(self, lead: Mapping[str, Any], now: datetime, tz: tzinfo,
                      rng: random.Random | None = None) -> str:
        offer = lead.get("oferta") or SEM_SITE
        if offer not in OFFERS:
            raise TemplateError(f"Oferta desconhecida: {offer}")
        return self._render(self.data["primeira"][offer], lead, now, tz, rng)

    def followup(self, lead: Mapping[str, Any], now: datetime, tz: tzinfo, rng: random.Random | None = None) -> str:
        return self._render(self.data["followup"], lead, now, tz, rng)

    def out_of_hours(self, lead: Mapping[str, Any], now: datetime, tz: tzinfo,
                     rng: random.Random | None = None) -> str:
        return self._render(self.data["fora_do_horario"], lead, now, tz, rng)


def greeting(now_local: datetime) -> str:
    if now_local.hour < 12:
        return "Bom dia"
    if now_local.hour < 18:
        return "Boa tarde"
    return "Boa noite"


def format_rating(nota: float | None) -> str:
    if nota is None:
        return ""
    return f"{nota:.1f}".replace(".", ",")


def clean_name(name: str) -> str:
    """'Pizzaria Bella Itaí - Delivery | Centro' -> 'Pizzaria Bella Itaí'."""
    out = _NAME_SUFFIX.sub("", name.strip())
    out = _PARENS.sub(" ", out)
    out = _SPACES.sub(" ", out).strip(" -|,")
    if out.isupper() and len(out) > 3:
        out = " ".join(w.capitalize() if len(w) > 2 else w.lower() for w in out.split())
    return out or name.strip()


def tidy(text: str) -> str:
    text = _SPACES.sub(" ", text)
    text = re.sub(r"\s+([,.!?])", r"\1", text)
    return text.strip()


_GREETING = re.compile(r"\b(Bom dia|Boa tarde|Boa noite|bom dia|boa tarde|boa noite)\b")


def fix_greeting(text: str, now_local: datetime) -> str:
    """Ajusta 'Bom dia'/'Boa tarde' ao horário real do envio (a fila é gerada antes)."""
    hello = greeting(now_local)
    return _GREETING.sub(lambda m: hello if m.group(1)[0].isupper() else hello.lower(), text)
