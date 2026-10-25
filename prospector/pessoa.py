"""Profissional autônomo (psicóloga, nutricionista, dra. fulana): fala no singular ("você") e sem o cargo no nome.

Negócio com equipe ("Clínica Sorriso", "Instituto X") continua no plural ("vocês")."""

from __future__ import annotations

import re

from .classify import fold

# Cargo que aparece no nome de quem trabalha sozinho. Dr./Dra. ficam no nome (tratamento), o resto sai.
_TITULOS_DOUTOR = r"dr|dra|doutor|doutora"
_PROFISSOES = (r"psic[oó]log[oa]|psicanalista|nutricionista|nutri|fisioterapeuta|terapeuta|advogad[oa]|dentista|"
               r"m[eé]dic[oa]|esteticista|massoterapeuta|enfermeir[oa]|fonoaudi[oó]log[oa]|veterin[aá]ri[oa]|"
               r"cabeleireir[oa]|manicure|personal|professor[a]?|prof|corretor[a]?|contador[a]?")
_PROFISSAO = re.compile(rf"^(?:{_PROFISSOES})\b\.?\s*", re.IGNORECASE)
_TEM_CARGO = re.compile(rf"\b(?:{_TITULOS_DOUTOR}|{_PROFISSOES})\b", re.IGNORECASE)
# Palavras de empresa: se aparecem, o negócio tem equipe e fica no plural.
_EMPRESA = re.compile(r"\b(cl[ií]nica|clinic|instituto|centro|espa[cç]o|studio|est[uú]dio|consult[oó]rio|odonto|odontologia|"
                      r"laborat[oó]rio|hospital|ltda|multidisciplinar|associad|grupo|rede|&)\b", re.IGNORECASE)
_RESTO_DEPOIS_TRACO = re.compile(r"\s*[-–|:]\s+.*$")

# (padrão, troca): as frases do templates.yaml escritas para uma equipe, ditas a uma pessoa só.
_SINGULAR = [
    (r"\b(o|no|do|pelo|ao) (Instagram|perfil|site) de vocês", r"\1 seu \2"),
    (r"(@[\w.]+) de vocês", r"\1"),
    (r"\bsite de vocês", "seu site"),
    (r"\ba divulgação de vocês", "a sua divulgação"),
    (r"\bavaliação de cliente de vocês", "avaliação de um cliente seu"),
    (r"\bcliente escreveu de vocês", "cliente escreveu sobre você"),
    (r"\bcliente de vocês", "cliente seu"),
    (r"\bVocês têm\b", "Você tem"),
    (r"\bvocês têm\b", "você tem"),
    (r"\bJá pensaram\b", "Já pensou"),
    (r"\bJá sabiam\b", "Já sabia"),
    (r"\bvocês\b", "você"),
    (r"\bVocês\b", "Você"),
]
_SINGULAR_RE = [(re.compile(p), r) for p, r in _SINGULAR]


def is_individual(nome: str | None) -> bool:
    """Tem cargo/tratamento no nome (Psicóloga, Dra.) e nenhuma palavra de empresa."""
    text = fold(nome or "")
    head = re.split(r"\s*[-–|:]\s*", text, maxsplit=1)[0]   # 'Amanda Cassu- Psicóloga Clínica': a empresa só vale antes do traço
    return bool(_TEM_CARGO.search(text)) and not _EMPRESA.search(head)


def individual_name(nome: str) -> str:
    """'Psicóloga Larissa Noveli' -> 'Larissa Noveli'; 'Dra.Thaina Cassu' -> 'Dra. Thaina Cassu'; 'Ana - Psicóloga' -> 'Ana'."""
    out = _RESTO_DEPOIS_TRACO.sub("", nome.strip())
    out = re.sub(r"\b(dra?|doutora?)\.(?=\S)", lambda m: m.group(0) + " ", out, flags=re.IGNORECASE)
    stripped = _PROFISSAO.sub("", out).strip()
    return stripped if len(stripped.split()) >= 1 and stripped else out


def singularize(text: str) -> str:
    for pattern, repl in _SINGULAR_RE:
        text = pattern.sub(repl, text)
    return text
