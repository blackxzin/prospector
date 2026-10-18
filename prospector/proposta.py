"""Proposta comercial em PDF gerada a partir de um projeto."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Mapping, Sequence

from . import pix
from .finance import brl
from .pdf import A4_W, Document

ACCENT = "#1f6fd1"
INK = "#14202e"
MUTED = "#5e6b7a"
LINE = "#e2e8f0"
SOFT = "#f2f6fb"
VALIDADE_DIAS = 7
TIPO_NOME = {"site": "Site profissional", "automacao": "Automação de atendimento no WhatsApp", "outro": "Projeto"}


@dataclass(frozen=True)
class PixInfo:
    chave: str
    nome: str
    cidade: str

    @property
    def ok(self) -> bool:
        return bool(self.chave and self.nome and self.cidade)


def build(project: Mapping[str, Any], *, autor: str, inclui: Sequence[str], entrada_percentual: int,
          pix_info: PixInfo, today: date) -> bytes:
    doc = Document(title=f"Proposta - {project['cliente']}")
    p = doc.new_page()
    margin, width = 48, A4_W - 96

    # faixa de topo
    p.rect(0, 0, A4_W, 118, ACCENT)
    p.text(margin, 52, "PROPOSTA", 11, bold=True, color="#cfe0f7")
    p.text(margin, 82, TIPO_NOME.get(project["tipo"], "Projeto"), 22, bold=True, color="#ffffff")
    p.text(A4_W - margin, 52, f"Emitida em {today:%d/%m/%Y}", 10, color="#e6efff", align="right")
    p.text(A4_W - margin, 68, f"Válida até {today + timedelta(days=VALIDADE_DIAS):%d/%m/%Y}", 10,
           color="#e6efff", align="right")

    y = 160
    p.text(margin, y, "PARA", 9, bold=True, color=MUTED)
    y_left = p.paragraph(margin, y + 18, str(project["cliente"]), width / 2 - 16, 15, leading=1.25, bold=True,
                         color=INK)
    p.text(margin + width / 2, y, "DE", 9, bold=True, color=MUTED)
    y_right = p.paragraph(margin + width / 2, y + 18, autor, width / 2, 12, color=INK)

    y = max(y_left, y_right) + 18
    p.line(margin, y, A4_W - margin, y, LINE)
    y += 28
    p.text(margin, y, "O QUE ESTÁ INCLUÍDO", 9, bold=True, color=MUTED)
    y += 20
    if project.get("descricao"):
        y = p.paragraph(margin, y, str(project["descricao"]), width, 11.5, color=INK) + 4
    for item in inclui:
        p.rect(margin, y - 7, 5, 5, ACCENT)
        y = p.paragraph(margin + 14, y, str(item), width - 14, 11, color=INK) + 2

    # investimento
    y += 18
    box_h = 112 if project.get("mensalidade_centavos") else 92
    p.rect(margin, y, width, box_h, SOFT)
    p.text(margin + 18, y + 26, "INVESTIMENTO", 9, bold=True, color=MUTED)
    p.text(margin + 18, y + 58, brl(project["valor_centavos"]), 26, bold=True, color=INK)
    entrada = project["valor_centavos"] * entrada_percentual // 100
    restante = project["valor_centavos"] - entrada
    cond = (f"{entrada_percentual}% de entrada ({brl(entrada)}) e {brl(restante)} na entrega"
            if entrada and restante else "Pagamento único na entrega" if not entrada else "Pagamento à vista")
    p.text(margin + 18, y + 80, cond, 10.5, color=MUTED)
    if project.get("mensalidade_centavos"):
        p.text(margin + 18, y + 98, f"+ manutenção mensal de {brl(project['mensalidade_centavos'])} "
               "(começa um mês após a entrega)", 10.5, color=MUTED)
    prazo_txt = "a combinar"
    if project.get("prazo") and project.get("fechado_em"):
        dias = (date.fromisoformat(project["prazo"]) - date.fromisoformat(project["fechado_em"])).days
        prazo_txt = f"{dias} dias (até {date.fromisoformat(project['prazo']):%d/%m})" if dias > 0 else "imediato"
    p.text(A4_W - margin - 18, y + 26, "PRAZO", 9, bold=True, color=MUTED, align="right")
    p.text(A4_W - margin - 18, y + 50, prazo_txt, 13, bold=True, color=INK, align="right")
    y += box_h + 30

    # pagamento via Pix
    if pix_info.ok:
        amount = entrada or project["valor_centavos"]
        code = pix.payload(pix_info.chave, pix_info.nome, pix_info.cidade, amount, txid=f"PROP{project['id']}")
        qr = 120
        p.text(margin, y, "PAGAMENTO VIA PIX", 9, bold=True, color=MUTED)
        p.matrix(margin, y + 14, pix.qr_matrix(code), qr)
        tx = margin + qr + 22
        p.text(tx, y + 30, f"{'Entrada' if entrada and restante else 'Valor'}: {brl(amount)}", 13, bold=True,
               color=INK)
        p.text(tx, y + 50, f"Chave: {pix_info.chave}", 10.5, color=INK)
        p.text(tx, y + 66, f"Favorecido: {pix_info.nome}", 10.5, color=INK)
        p.text(tx, y + 88, "Pix copia e cola:", 9, bold=True, color=MUTED)
        p.paragraph(tx, y + 102, code, A4_W - margin - tx, 7.5, leading=1.3, color=MUTED)
        y += qr + 40

    p.line(margin, 790, A4_W - margin, 790, LINE)
    p.text(margin, 808, "Dúvidas? É só responder pelo WhatsApp.", 9.5, color=MUTED)
    p.text(A4_W - margin, 808, autor.split(",")[0], 9.5, bold=True, color=INK, align="right")
    return doc.build()
