"""Pix "copia e cola" (BR Code estático, padrão EMV do Banco Central) e QR Code em SVG.

Tudo gerado localmente: não fala com banco nenhum. O dinheiro cai direto na sua chave.
"""

from __future__ import annotations

import re
import unicodedata

import qrcode

GUI = "br.gov.bcb.pix"
MAX_NOME = 25
MAX_CIDADE = 15
MAX_TXID = 25


class PixError(ValueError):
    pass


def _ascii(text: str) -> str:
    """Nome/cidade no BR Code: sem acento, só ASCII imprimível."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^A-Za-z0-9 .\-]", "", plain).strip()


def _field(tag: str, value: str) -> str:
    if len(value) > 99:
        raise PixError(f"Campo {tag} grande demais")
    return f"{tag}{len(value):02d}{value}"


def crc16(payload: str) -> str:
    """CRC16-CCITT (polinômio 0x1021, valor inicial 0xFFFF), como pede o BR Code."""
    crc = 0xFFFF
    for byte in payload.encode("utf-8"):
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return f"{crc:04X}"


def payload(chave: str, nome: str, cidade: str, valor_centavos: int | None = None, txid: str = "***",
            descricao: str = "") -> str:
    chave = (chave or "").strip()
    if not chave:
        raise PixError("Configure PIX_CHAVE no .env")
    nome_a, cidade_a = _ascii(nome)[:MAX_NOME], _ascii(cidade)[:MAX_CIDADE]
    if not nome_a or not cidade_a:
        raise PixError("Configure PIX_NOME e PIX_CIDADE no .env")
    txid = re.sub(r"[^A-Za-z0-9]", "", txid or "")[:MAX_TXID] or "***"
    conta = _field("00", GUI) + _field("01", chave)
    if descricao:
        conta += _field("02", _ascii(descricao)[:40])
    parts = [
        _field("00", "01"),
        _field("26", conta),
        _field("52", "0000"),
        _field("53", "986"),
    ]
    if valor_centavos:
        if valor_centavos < 0:
            raise PixError("Valor negativo")
        parts.append(_field("54", f"{valor_centavos / 100:.2f}"))
    parts += [_field("58", "BR"), _field("59", nome_a), _field("60", cidade_a), _field("62", _field("05", txid))]
    body = "".join(parts) + "6304"
    return body + crc16(body)


def qr_svg(data: str, size: int = 180) -> str:
    """QR Code como SVG puro (sem imagem, sem dependência extra)."""
    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)
    rects = "".join(f'<rect x="{x}" y="{y}" width="1" height="1"/>'
                    for y, row in enumerate(matrix) for x, cell in enumerate(row) if cell)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" width="{size}" height="{size}" '
            f'shape-rendering="crispEdges" role="img" aria-label="QR Code Pix">'
            f'<rect width="{n}" height="{n}" fill="#fff"/><g fill="#000">{rects}</g></svg>')


def qr_matrix(data: str) -> list[list[bool]]:
    qr = qrcode.QRCode(border=0, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(data)
    qr.make(fit=True)
    return qr.get_matrix()
