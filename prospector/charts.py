"""Gráficos em SVG gerados no servidor (sem JS nem CDN). Cores vêm de variáveis CSS do painel."""

from __future__ import annotations

from html import escape
from typing import Any, Sequence

from .finance import brl, brl_short

MESES = ("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez")
BAR_MAX = 24        # px: barras finas, nunca preenchem o slot
RADIUS = 4          # ponta arredondada, base reta
GAP = 2             # respiro entre barras vizinhas


def _nice_max(value: int) -> int:
    """Topo 'redondo' do eixo (em centavos)."""
    if value <= 0:
        return 100_00
    reais = value / 100
    magnitude = 10 ** (len(str(int(reais))) - 1)
    for step in (1, 2, 2.5, 5, 10):
        top = step * magnitude
        if top >= reais:
            return int(top * 100)
    return int(10 * magnitude * 100)


def _col(x: float, y: float, w: float, h: float) -> str:
    """Coluna com topo arredondado e base reta."""
    if h <= 0:
        return ""
    r = min(RADIUS, w / 2, h)
    return (f"M{x:.1f},{y + h:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
            f"H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{y + h:.1f} Z")


def _bar(x: float, y: float, w: float, h: float) -> str:
    """Barra horizontal com ponta direita arredondada."""
    if w <= 0:
        return ""
    r = min(RADIUS, h / 2, w)
    return (f"M{x:.1f},{y:.1f} H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} "
            f"V{y + h - r:.1f} Q{x + w:.1f},{y + h:.1f} {x + w - r:.1f},{y + h:.1f} H{x:.1f} Z")


def revenue_chart(series: Sequence[dict[str, Any]]) -> str:
    """Receita x despesas por mês: colunas agrupadas, um eixo só (R$)."""
    width, height = 640, 240
    left, right, top, bottom = 56, 12, 12, 28
    plot_w, plot_h = width - left - right, height - top - bottom
    top_value = _nice_max(max([max(m["receita"], m["despesas"]) for m in series] or [0]))
    slot = plot_w / max(1, len(series))
    bar_w = min(BAR_MAX, (slot - 16) / 2 - GAP)
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" '
             f'aria-label="Receita e despesas dos últimos {len(series)} meses">']
    for i in range(5):
        v = top_value * i // 4
        y = top + plot_h - plot_h * i / 4
        cls = "axis" if i == 0 else "grid"
        parts.append(f'<line x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}" class="{cls}"/>')
        parts.append(f'<text x="{left - 8}" y="{y + 4:.1f}" class="tick" text-anchor="end">{brl_short(v)}</text>')
    for idx, m in enumerate(series):
        cx = left + slot * idx + slot / 2
        mes = m["mes"]
        label = f"{MESES[mes.month - 1]}/{mes:%y}"
        for j, (key, cls) in enumerate((("receita", "s1"), ("despesas", "s2"))):
            v = m[key]
            h = plot_h * v / top_value if top_value else 0
            x = cx - bar_w - GAP / 2 if j == 0 else cx + GAP / 2
            path = _col(x, top + plot_h - h, bar_w, h)
            if path:
                parts.append(f'<path d="{path}" class="{cls}"/>')
        tip = escape(f"{label}: receita {brl(m['receita'])} · despesas {brl(m['despesas'])} · "
                     f"lucro {brl(m['lucro'])}")
        parts.append(f'<rect x="{cx - slot / 2:.1f}" y="{top}" width="{slot:.1f}" height="{plot_h}" '
                     f'class="hit"><title>{tip}</title></rect>')
        parts.append(f'<text x="{cx:.1f}" y="{height - 8}" class="tick" text-anchor="middle">{label}</text>')
    parts.append("</svg>")
    return "".join(parts)


def funnel_chart(steps: Sequence[tuple[str, int]]) -> str:
    """Funil em barras horizontais (uma série: sem legenda; rótulo do valor na ponta)."""
    row_h, bar_h = 34, 20
    width, label_w, value_w = 640, 120, 92
    height = row_h * len(steps) + 4
    plot_w = width - label_w - value_w
    biggest = max([v for _, v in steps] or [0]) or 1
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="Funil de prospecção">']
    prev = None
    for i, (name, value) in enumerate(steps):
        y = 2 + i * row_h + (row_h - bar_h) / 2
        w = max(2.0, plot_w * value / biggest) if value else 0
        conv = f" ({value / prev:.0%} da etapa anterior)" if prev else ""
        parts.append(f'<text x="{label_w - 10}" y="{y + bar_h / 2 + 4:.1f}" class="lbl" text-anchor="end">'
                     f'{escape(name)}</text>')
        path = _bar(label_w, y, w, bar_h)
        if path:
            parts.append(f'<path d="{path}" class="s1"/>')
        else:
            parts.append(f'<line x1="{label_w}" x2="{label_w}" y1="{y:.1f}" y2="{y + bar_h:.1f}" class="axis"/>')
        pct = f" · {value / prev:.0%}" if prev else ""
        parts.append(f'<text x="{label_w + w + 8:.1f}" y="{y + bar_h / 2 + 4:.1f}" class="val">{value}'
                     f'<tspan class="tick">{pct}</tspan></text>')
        parts.append(f'<rect x="0" y="{y - 6:.1f}" width="{width}" height="{row_h}" class="hit">'
                     f'<title>{escape(name)}: {value}{escape(conv)}</title></rect>')
        prev = value if value else None
    parts.append("</svg>")
    return "".join(parts)


DIAS_SEMANA = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")


def _nice_int(value: int) -> int:
    """Topo do eixo para contagens: 4 divisões inteiras e 'redondas'."""
    for step in (1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50, 60, 80, 100, 150, 200, 250, 500, 1000):
        if 4 * step >= value:
            return 4 * step
    return value


def daily_chart(rows: Sequence[dict[str, Any]]) -> str:
    """Envios x respostas por dia: duas linhas (mesma unidade, um eixo só), área suave nos envios."""
    width, height = 640, 220
    left, right, top, bottom = 34, 40, 14, 26
    pw, ph = width - left - right, height - top - bottom
    n = max(1, len(rows))
    peak = max([max(r["enviados"], r["respostas"]) for r in rows] or [0])
    top_v = _nice_int(peak)
    x = lambda i: left + (pw * i / (n - 1) if n > 1 else pw / 2)  # noqa: E731
    y = lambda v: top + ph - ph * v / top_v  # noqa: E731
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" '
             f'aria-label="Envios e respostas por dia nos últimos {len(rows)} dias">']
    for i in range(5):
        v = top_v * i / 4
        yy = y(v)
        parts.append(f'<line x1="{left}" x2="{width - right}" y1="{yy:.1f}" y2="{yy:.1f}" '
                     f'class="{"axis" if i == 0 else "grid"}"/>')
        parts.append(f'<text x="{left - 8}" y="{yy + 4:.1f}" class="tick" text-anchor="end">{v:g}</text>')
    for key, cls in (("enviados", "s1"), ("respostas", "s2")):
        pts = " ".join(f"{x(i):.1f},{y(r[key]):.1f}" for i, r in enumerate(rows))
        if key == "enviados" and rows:
            parts.append(f'<polygon points="{left:.1f},{y(0):.1f} {pts} {x(n - 1):.1f},{y(0):.1f}" class="{cls}-wash"/>')
        parts.append(f'<polyline points="{pts}" class="line {cls}-line"/>')
        if rows:
            last = rows[-1][key]
            parts.append(f'<circle cx="{x(n - 1):.1f}" cy="{y(last):.1f}" r="4" class="{cls} dot"/>')
            parts.append(f'<text x="{x(n - 1) + 8:.1f}" y="{y(last) + 4:.1f}" class="val">{last}</text>')
    for i, r in enumerate(rows):
        if i == n - 1 or (i % 7 == 0 and n - 1 - i >= 4):
            parts.append(f'<text x="{x(i):.1f}" y="{height - 6}" class="tick" text-anchor="middle">'
                         f'{r["dia"]:%d/%m}</text>')
        slot = pw / max(1, n - 1)
        tip = escape(f"{r['dia']:%d/%m}: {r['enviados']} enviados · {r['respostas']} respostas · "
                     f"{r['leads']} leads novos")
        parts.append(f'<rect x="{x(i) - slot / 2:.1f}" y="{top}" width="{slot:.1f}" height="{ph}" class="hit">'
                     f'<title>{tip}</title></rect>')
    parts.append("</svg>")
    return "".join(parts)


def hbar_chart(rows: Sequence[dict[str, Any]], *, value: str, label: str = "nome",
               fmt=lambda r, v: str(v), aria: str = "Comparação") -> str:
    """Barras horizontais de uma série (sem legenda), valor na ponta."""
    row_h, bar_h, width, label_w, value_w = 30, 16, 640, 150, 120
    height = row_h * max(1, len(rows)) + 4
    pw = width - label_w - value_w
    biggest = max([r[value] for r in rows] or [0]) or 1
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="{escape(aria)}">']
    for i, r in enumerate(rows):
        yy = 2 + i * row_h + (row_h - bar_h) / 2
        w = pw * r[value] / biggest if r[value] else 0
        name = str(r[label])
        short = name if len(name) <= 22 else name[:21] + "…"
        parts.append(f'<text x="{label_w - 10}" y="{yy + bar_h / 2 + 4:.1f}" class="lbl" text-anchor="end">'
                     f'{escape(short)}</text>')
        path = _bar(label_w, yy, max(w, 2.0), bar_h) if r[value] else ""
        if path:
            parts.append(f'<path d="{path}" class="s1"/>')
        else:
            parts.append(f'<line x1="{label_w}" x2="{label_w}" y1="{yy:.1f}" y2="{yy + bar_h:.1f}" class="axis"/>')
        parts.append(f'<text x="{label_w + w + 8:.1f}" y="{yy + bar_h / 2 + 4:.1f}" class="val">'
                     f'{escape(fmt(r, r[value]))}</text>')
        parts.append(f'<rect x="0" y="{yy - 7:.1f}" width="{width}" height="{row_h}" class="hit">'
                     f'<title>{escape(name)}: {escape(fmt(r, r[value]))}</title></rect>')
    parts.append("</svg>")
    return "".join(parts)


def heatmap(grid: Sequence[Sequence[int]], hours: Sequence[int]) -> str:
    """Respostas por dia da semana × hora: escala sequencial de um tom (claro = pouco, escuro = muito)."""
    cell, gap, left, top = 30, 3, 40, 22
    width = left + len(hours) * (cell + gap)
    height = top + 7 * (cell + gap)
    peak = max([max(r) for r in grid] or [0])
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart heat" role="img" '
             f'aria-label="Respostas por dia da semana e hora">']
    for j, h in enumerate(hours):
        if h % 2 == 1:
            parts.append(f'<text x="{left + j * (cell + gap) + cell / 2:.1f}" y="14" class="tick" '
                         f'text-anchor="middle">{h}h</text>')
    for i, row in enumerate(grid):
        yy = top + i * (cell + gap)
        parts.append(f'<text x="{left - 8}" y="{yy + cell / 2 + 4:.1f}" class="tick" text-anchor="end">'
                     f'{DIAS_SEMANA[i]}</text>')
        for j, v in enumerate(row):
            level = 0 if not v or not peak else min(5, 1 + int(4 * v / peak))
            parts.append(f'<rect x="{left + j * (cell + gap)}" y="{yy}" width="{cell}" height="{cell}" rx="5" '
                         f'class="h{level}"><title>{DIAS_SEMANA[i]} {hours[j]}h: {v} resposta(s)</title></rect>')
    parts.append("</svg>")
    return "".join(parts)
