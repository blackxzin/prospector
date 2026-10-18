"""Gerador de PDF mínimo (uma ou mais páginas A4, Helvetica, retângulos). Sem dependências.

Coordenadas em pontos, origem no canto SUPERIOR esquerdo (mais natural para montar layout).
"""

from __future__ import annotations

from dataclasses import dataclass, field

A4_W, A4_H = 595.28, 841.89
# Larguras médias da Helvetica (1/1000 em) — suficiente para quebrar linhas com folga.
_NARROW = set("iljtfI.,;:!'|()[] ")
_WIDE = set("mwMW@%")


def _escape(text: str) -> bytes:
    raw = text.encode("cp1252", errors="replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)").replace(b"\r", b"").replace(b"\n", b" ")


def text_width(text: str, size: float, bold: bool = False) -> float:
    units = 0
    for ch in text:
        units += 300 if ch in _NARROW else 850 if ch in _WIDE else 640 if ch.isupper() else 540
    return units * size / 1000 * (1.06 if bold else 1.0)


def wrap(text: str, size: float, width: float, bold: bool = False) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words, line = paragraph.split(" "), ""
        for word in words:
            candidate = f"{line} {word}".strip()
            if text_width(candidate, size, bold) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
            # palavra maior que a linha (ex.: código Pix): quebra por caractere
            while text_width(word, size, bold) > width:
                cut = max(1, int(len(word) * width / text_width(word, size, bold)))
                lines.append(word[:cut])
                word = word[cut:]
            line = word
        lines.append(line)
    return lines


def _rgb(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return f"{r:.3f} {g:.3f} {b:.3f}"


@dataclass
class Page:
    ops: list[str] = field(default_factory=list)

    def rect(self, x: float, y: float, w: float, h: float, color: str) -> None:
        self.ops.append(f"{_rgb(color)} rg {x:.2f} {A4_H - y - h:.2f} {w:.2f} {h:.2f} re f")

    def line(self, x1: float, y1: float, x2: float, y2: float, color: str = "#dddddd", width: float = 0.8) -> None:
        self.ops.append(f"{_rgb(color)} RG {width} w {x1:.2f} {A4_H - y1:.2f} m {x2:.2f} {A4_H - y2:.2f} l S")

    def text(self, x: float, y: float, text: str, size: float = 11, bold: bool = False, color: str = "#111111",
             align: str = "left") -> None:
        if align == "right":
            x -= text_width(text, size, bold)
        font = "F2" if bold else "F1"
        payload = _escape(text).decode("latin-1")
        self.ops.append(f"BT {_rgb(color)} rg /{font} {size} Tf {x:.2f} {A4_H - y:.2f} Td ({payload}) Tj ET")

    def paragraph(self, x: float, y: float, text: str, width: float, size: float = 11, leading: float = 1.4,
                  bold: bool = False, color: str = "#111111") -> float:
        """Escreve o texto quebrando linhas; devolve o y logo abaixo do parágrafo."""
        for line in wrap(text, size, width, bold):
            self.text(x, y, line, size, bold, color)
            y += size * leading
        return y

    def matrix(self, x: float, y: float, cells: list[list[bool]], size: float, color: str = "#000000") -> None:
        """Desenha uma matriz (ex.: QR Code) como quadradinhos."""
        n = len(cells)
        module = size / n
        parts = [f"{_rgb(color)} rg"]
        for r, row in enumerate(cells):
            for c, on in enumerate(row):
                if on:
                    parts.append(f"{x + c * module:.2f} {A4_H - y - (r + 1) * module:.2f} {module + .05:.2f} "
                                 f"{module + .05:.2f} re")
        parts.append("f")
        self.ops.append(" ".join(parts))


@dataclass
class Document:
    title: str = ""
    pages: list[Page] = field(default_factory=list)

    def new_page(self) -> Page:
        page = Page()
        self.pages.append(page)
        return page

    def build(self) -> bytes:
        objects: list[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)

        catalog = add(b"")  # preenchido depois
        pages_id = add(b"")
        font1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        font2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
        kids = []
        for page in self.pages:
            stream = "\n".join(page.ops).encode("latin-1")
            content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
            kids.append(add(
                f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {A4_W} {A4_H}] "
                f"/Resources << /Font << /F1 {font1} 0 R /F2 {font2} 0 R >> >> /Contents {content} 0 R >>".encode()))
        objects[catalog - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode()
        objects[pages_id - 1] = (f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] "
                                 f"/Count {len(kids)} >>").encode()
        info = add(b"<< /Title (" + _escape(self.title) + b") /Producer (prospector) >>")

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, body in enumerate(objects, 1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
        for off in offsets:
            out += f"{off:010d} 00000 n \n".encode()
        out += (f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R /Info {info} 0 R >>\n"
                f"startxref\n{xref}\n%%EOF\n").encode()
        return bytes(out)
