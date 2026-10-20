"""Geração das mensagens a partir do templates.yaml."""

from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import yaml

from .classify import SEM_SITE, SITE_PROBLEMA, SO_SOCIAL, TEM_SITE, fold

OFFERS = (SEM_SITE, SO_SOCIAL, TEM_SITE, SITE_PROBLEMA)
# Listas simples obrigatórias (pelo menos uma variação cada).
REQUIRED_LISTS = ("followup", "fora_do_horario", "elogios_com_nota", "elogios_sem_nota", "cobranca")
# Valores fictícios para validar variáveis que só existem em alguns tipos de mensagem.
EXTRA_SAMPLE = {"valor": "R$ 300,00", "vencimento": "20/10",
                "descricao": "Entrada", "pix": "000201...", "problema": "o site está fora do ar"}
MIN_VARIANTS = 3
# Grupos opcionais dentro de 'primeira', usados quando o lead tem um sinal específico (ver sitecheck.extract_signals).
EXTRA_GROUPS = ("tem_site_sem_whatsapp", "indicado")
# Campos que nem todo lead tem: a variação que usa um deles só entra no sorteio se o lead tiver o dado.
CONDITIONAL_FIELDS = ("avaliacao", "arroba", "nota_boa", "dor")
MIN_UNCONDITIONAL = 3
_NAME_SUFFIX = re.compile(r"\s+[-|–—•·]\s+.*$")
_PARENS = re.compile(r"\s*\([^)]*\)\s*")
_SPACES = re.compile(r"\s{2,}")
_CONNECTORS = frozenset({"de", "da", "do", "das", "dos", "e", "em", "na", "no"})


class TemplateError(ValueError):
    pass


@dataclass(frozen=True)
class Templates:
    data: Mapping[str, Any]

    @classmethod
    def load(cls, path: Path, base: Templates | None = None) -> Templates:
        """Lê um arquivo de textos. Com `base`, o arquivo sobrescreve só o que traz (idiomas fora do pt)."""
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if base is not None:
            own = set((data.get("primeira") or {}))
            data = _overlay(dict(base.data), data)
            # textos de sinais escritos em português não valem para outro idioma
            data["primeira"] = {k: v for k, v in data["primeira"].items() if k not in EXTRA_GROUPS or k in own}
        tpl = cls(data)
        tpl.validate(path.name)
        return tpl

    @property
    def idioma(self) -> str:
        return str(self.data.get("idioma") or "pt")

    @property
    def saudacoes(self) -> Mapping[str, str]:
        return {**DEFAULT_SAUDACOES, **(self.data.get("saudacoes") or {})}

    def fix_greeting(self, text: str, now_local: datetime) -> str:
        return fix_greeting(text, now_local, self.saudacoes)

    def validate(self, name: str = "templates.yaml") -> None:
        primeira = self.data.get("primeira") or {}
        for offer in OFFERS:
            if len(primeira.get(offer) or []) < MIN_VARIANTS:
                raise TemplateError(f"{name}: 'primeira.{offer}' precisa de pelo menos {MIN_VARIANTS} variações")
        for offer in (*OFFERS, *(g for g in EXTRA_GROUPS if g in primeira)):
            plain = [t for t in primeira.get(offer) or [] if not any("{" + k + "}" in t for k in CONDITIONAL_FIELDS)]
            if len(plain) < MIN_UNCONDITIONAL:
                raise TemplateError(f"{name}: 'primeira.{offer}' precisa de pelo menos {MIN_UNCONDITIONAL} variações "
                                    f"que não dependam de {', '.join(CONDITIONAL_FIELDS)}")
        for group in EXTRA_GROUPS:
            if group in primeira and len(primeira[group] or []) < MIN_VARIANTS:
                raise TemplateError(f"{name}: 'primeira.{group}' precisa de pelo menos {MIN_VARIANTS} variações")
        for key in REQUIRED_LISTS:
            if not self.data.get(key):
                raise TemplateError(f"{name}: '{key}' está vazio")
        if not self.data.get("remetente"):
            raise TemplateError(f"{name}: 'remetente' está vazio")
        # Renderiza tudo uma vez com dados fictícios para pegar {variavel} errada.
        sample = {"nome": "Teste", "nota": 4.8, "avaliacoes": 50, "categoria": "Pizzaria", "oferta": SEM_SITE}
        for text in self._all_texts():
            try:
                text.format(**self._context(sample, datetime(2026, 1, 5, 10), random.Random(0)), **EXTRA_SAMPLE)
            except (KeyError, IndexError, ValueError) as exc:
                raise TemplateError(f"{name}: variável inválida em {text[:60]!r}: {exc}") from exc

    def _all_texts(self) -> list[str]:
        texts = [t for offer in OFFERS for t in self.data["primeira"][offer]]
        texts += [t for group in EXTRA_GROUPS for t in self.data["primeira"].get(group) or []]
        texts += list(self.data.get("portfolio_msg") or [])
        for key in REQUIRED_LISTS:
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

    def _local(self, cidade: str | None) -> str:
        """'de Itaí' só para quem é da cidade-base de quem escreve; nas outras, 'aqui da região' (verdade)."""
        base = str(self.data.get("cidade_base") or "").strip()
        if base and cidade and fold(cidade) == fold(base):
            return f"{self.data.get('de_cidade', 'de')} {base}"
        return str(self.data.get("sem_cidade") or "aqui da região")

    def _nota_boa(self, nota: float | None, avaliacoes: int | None) -> str:
        """Nota só quando é boa (como no elogio); fora disso '', e as variações que a usam saem do sorteio."""
        if nota is None or avaliacoes is None:
            return ""
        if nota >= float(self.data.get("elogio_nota_minima", 4.0)) and \
                avaliacoes >= int(self.data.get("elogio_avaliacoes_minimas", 10)):
            return format_rating(nota, str(self.data.get("decimal") or ","))
        return ""

    def _elogio(self, nota: float | None, avaliacoes: int | None, rng: random.Random, base: dict[str, str]) -> str:
        min_nota = float(self.data.get("elogio_nota_minima", 4.0))
        min_aval = int(self.data.get("elogio_avaliacoes_minimas", 10))
        if nota is not None and avaliacoes is not None and nota >= min_nota and avaliacoes >= min_aval:
            return rng.choice(self.data["elogios_com_nota"]).format(**base)
        return rng.choice(self.data["elogios_sem_nota"]).format(**base)

    def _context(self, lead: Mapping[str, Any], now_local: datetime, rng: random.Random) -> dict[str, str]:
        nota = lead.get("nota")
        avaliacoes = lead.get("avaliacoes")
        hello = greeting(now_local, self.saudacoes)
        portfolio = str(self.data.get("portfolio") or "")
        base = {
            "nome": clean_name(str(lead.get("nome") or ""), lead.get("cidade")),
            "remetente": str(self.data["remetente"]),
            "portfolio": portfolio,
            "apresentacao": str(self.data.get("apresentacao") or "").format(portfolio=portfolio),
            "saudacao": hello,
            "saudacao_minuscula": hello.lower(),
            "nota": format_rating(nota, str(self.data.get("decimal") or ",")),
            "optout": str(self.data.get("optout") or ""),
            "avaliacoes": str(avaliacoes or 0),
            "vitrine": self._vitrine(lead.get("categoria")),
            "local": self._local(lead.get("cidade")),
            "avaliacao": review_snippet(lead.get("avaliacoes_json")),
            "arroba": instagram_handle(lead.get("site")),
            "nota_boa": self._nota_boa(nota, avaliacoes),
            "indicador": str(lead.get("indicador") or ""),   # quem indicou (lead indicado)
            "chamada": str(lead.get("chamada") or ""),       # ", Rafael" quando o nome veio no cartão
            "dor": str(lead.get("dor") or "").strip(),   # pergunta do nicho (nichos.yaml), só para leads de um nicho
        }
        return {**base, "elogio": self._elogio(nota, avaliacoes, rng, base)}

    # ---------- render ----------

    def _render(self, options: list[str], lead: Mapping[str, Any], now: datetime, tz: tzinfo,
                rng: random.Random | None, chosen: str | None = None, **extra: str) -> str:
        rng = rng or random.Random()
        ctx = self._context(lead, now.astimezone(tz), rng)
        template = chosen if chosen is not None else rng.choice(options)
        return tidy(template.format(**ctx, **extra))

    def first_message(self, lead: Mapping[str, Any], now: datetime, tz: tzinfo,
                      rng: random.Random | None = None) -> str:
        return self.first_message_ab(lead, now, tz, rng)[0]

    def first_message_ab(self, lead: Mapping[str, Any], now: datetime, tz: tzinfo,
                         rng: random.Random | None = None,
                         stats: Mapping[str, tuple[int, int]] | None = None) -> tuple[str, str]:
        """(texto, variante). Com `stats` {variante: (enviados, positivos)}, escolhe por Thompson sampling:
        variações que geram mais respostas positivas são sorteadas com mais frequência, sem abandonar as outras."""
        rng = rng or random.Random()
        offer = lead.get("oferta") or SEM_SITE
        if offer not in OFFERS:
            raise TemplateError(f"Oferta desconhecida: {offer}")
        options = list(self.data["primeira"][offer])
        if lead.get("indicado_por") and self.data["primeira"].get("indicado"):
            options = list(self.data["primeira"]["indicado"])      # veio indicado por um atendente: texto próprio
        elif offer == TEM_SITE and "sem_whatsapp" in site_signals(lead):
            options = list(self.data["primeira"].get("tem_site_sem_whatsapp") or options)
        ctx = self._context(lead, now.astimezone(tz), random.Random(0))
        has = lambda o: all(ctx[k] for k in CONDITIONAL_FIELDS if "{" + k + "}" in o)  # noqa: E731
        usable = [o for o in options if has(o)]
        chosen = choose_variant(usable or options, stats or {}, rng)
        if self.idioma == "pt":
            extra = {"problema": str(lead.get("site_problemas") or "o site tem alguns problemas").split(";")[0].strip()}
        else:  # os diagnósticos do sitecheck são em português: fora do pt usa a frase padrão do idioma
            extra = {"problema": str(self.data.get("problema_padrao") or "")}
        return self._render(options, lead, now, tz, rng, chosen=chosen, **extra), variant_id(chosen)

    def charge_message(self, lead: Mapping[str, Any], *, valor: str, vencimento: str, descricao: str, pix: str,
                       now: datetime, tz: tzinfo, rng: random.Random | None = None) -> str:
        return self._render(self.data["cobranca"], lead, now, tz, rng, valor=valor, vencimento=vencimento,
                            descricao=descricao, pix=pix)

    def followup(self, lead: Mapping[str, Any], now: datetime, tz: tzinfo, rng: random.Random | None = None) -> str:
        return self._render(self.data["followup"], lead, now, tz, rng)

    def out_of_hours(self, lead: Mapping[str, Any], now: datetime, tz: tzinfo,
                     rng: random.Random | None = None) -> str:
        return self._render(self.data["fora_do_horario"], lead, now, tz, rng)


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n")
_SNIPPET_BAD = re.compile(r"https?:|www\.|\.\.|\d{4,}|@|!{2,}|\?{2,}")
SNIPPET_MIN_WORDS, SNIPPET_MAX_WORDS, SNIPPET_MAX_CHARS = 3, 12, 70
_IG_SKIP = frozenset({"p", "reel", "reels", "explore", "popular", "accounts", "stories", "tv", "share"})


def review_snippet(avaliacoes_json: str | None) -> str:
    """Primeira frase curta de uma avaliação de 5 estrelas (“Melhor pizza da cidade”), ou ''.

    Só frases limpas: 3 a 12 palavras, sem link, número de telefone, reticências ou exclamação repetida."""
    try:
        reviews = json.loads(avaliacoes_json or "[]")
    except (TypeError, ValueError):
        return ""
    best = ""
    for r in reviews if isinstance(reviews, list) else []:
        if not isinstance(r, dict) or int(r.get("nota") or 0) < 5:
            continue
        first = _SENTENCE_END.split(str(r.get("texto") or "").strip())[0].strip().rstrip(".!…").strip()
        words = first.split()
        if (SNIPPET_MIN_WORDS <= len(words) <= SNIPPET_MAX_WORDS and len(first) <= SNIPPET_MAX_CHARS
                and not _SNIPPET_BAD.search(first) and not first.isupper()):
            if not best or len(first) < len(best):
                best = first
    return best[:1].upper() + best[1:] if best else ""


def instagram_handle(site: str | None) -> str:
    """'https://instagram.com/pizzariabella?igsh=..' -> '@pizzariabella'; senão ''."""
    if not site:
        return ""
    parsed = urlparse(site if "://" in site else "http://" + site)
    host = (parsed.hostname or "").lower()
    if host not in ("instagram.com", "www.instagram.com", "m.instagram.com"):
        return ""
    segment = parsed.path.strip("/").split("/")[0].strip()
    if not segment or segment.lower() in _IG_SKIP or not re.fullmatch(r"[A-Za-z0-9._]{2,30}", segment):
        return ""
    return "@" + segment


def site_signals(lead: Mapping[str, Any]) -> set[str]:
    try:
        raw = json.loads(lead.get("site_sinais") or "[]")
    except (TypeError, ValueError):
        return set()
    return {str(x) for x in raw} if isinstance(raw, list) else set()


def variant_id(template: str) -> str:
    """Identificador estável de uma variação (muda se o texto do template mudar)."""
    return hashlib.sha1(template.strip().encode("utf-8")).hexdigest()[:8]


def choose_variant(options: list[str], stats: Mapping[str, tuple[int, int]], rng: random.Random) -> str:
    """Thompson sampling com Beta(positivos + 1, negativos + 1) por variação."""
    if not options:
        raise TemplateError("Sem variações")
    best, best_score = options[0], -1.0
    for option in options:
        sent, positive = stats.get(variant_id(option), (0, 0))
        positive = max(0, min(positive, sent))
        score = rng.betavariate(positive + 1, sent - positive + 1)
        if score > best_score:
            best, best_score = option, score
    return best


DEFAULT_SAUDACOES = {"manha": "Bom dia", "tarde": "Boa tarde", "noite": "Boa noite"}


def greeting(now_local: datetime, saudacoes: Mapping[str, str] | None = None) -> str:
    words = saudacoes or DEFAULT_SAUDACOES
    if now_local.hour < 12:
        return words["manha"]
    if now_local.hour < 18:
        return words["tarde"]
    return words["noite"]


def format_rating(nota: float | None, decimal: str = ",") -> str:
    if nota is None:
        return ""
    return f"{nota:.1f}".replace(".", decimal)


def _overlay(base: dict[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    """Sobrescreve `base` com `extra`; 'primeira' e 'vitrine' são mesclados por chave interna."""
    for key, value in extra.items():
        if key in ("primeira", "vitrine") and isinstance(value, Mapping) and isinstance(base.get(key), Mapping):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return base


def clean_name(name: str, cidade: str | None = None) -> str:
    """'Pizzaria Bella Itaí - Delivery | Centro' -> 'Pizzaria Bella Itaí'.

    Com `cidade`, tira a cidade do fim do nome ('Yaki Burguer Itai' -> 'Yaki Burguer'),
    desde que sobrem pelo menos duas palavras ('Pizzaria Itaí' fica como está)."""
    out = _NAME_SUFFIX.sub("", name.strip())
    out = _PARENS.sub(" ", out)
    out = _SPACES.sub(" ", out).strip(" -|,")
    if out.isupper() and len(out) > 3:
        out = " ".join(w.lower() if w.lower() in _CONNECTORS else w.capitalize() for w in out.split())
    if cidade and cidade.strip():
        words, n = out.split(), len(cidade.split())
        if len(words) > n + 1 and fold(" ".join(words[-n:])) == fold(cidade):
            out = " ".join(words[:-n])
    return out or name.strip()


def tidy(text: str) -> str:
    text = _SPACES.sub(" ", text)
    text = re.sub(r"\s+([,.!?])", r"\1", text)
    return text.strip()


def fix_greeting(text: str, now_local: datetime, saudacoes: Mapping[str, str] | None = None) -> str:
    """Ajusta 'Bom dia'/'Boa tarde'/'Good morning'... ao horário real do envio (a fila é gerada antes)."""
    words = saudacoes or DEFAULT_SAUDACOES
    hello = greeting(now_local, words)
    options = sorted({w for w in words.values()}, key=len, reverse=True)
    pattern = re.compile(r"(?<!\w)(" + "|".join(re.escape(w) for w in options) + r")(?!\w)", re.IGNORECASE)
    return pattern.sub(lambda m: hello if m.group(1)[0].isupper() else hello.lower(), text)


class TemplateSet:
    """Um Templates por idioma: templates.yaml (pt) + templates.<idioma>.yaml, que sobrescrevem o pt."""

    def __init__(self, default: Templates, by_lang: Mapping[str, Templates]):
        self.default = default
        self.by_lang = dict(by_lang)

    @classmethod
    def load(cls, path: Path) -> TemplateSet:
        default = Templates.load(path)
        by_lang = {default.idioma.lower(): default}
        for extra in sorted(path.parent.glob(f"{path.stem}.*{path.suffix}")):
            lang = extra.name[len(path.stem) + 1:-len(path.suffix)].lower()
            by_lang[lang] = Templates.load(extra, base=default)
        return cls(default, by_lang)

    def get(self, lang: str | None) -> Templates:
        return self.by_lang.get((lang or "pt").lower(), self.default)

    @property
    def languages(self) -> list[str]:
        return sorted(self.by_lang)
