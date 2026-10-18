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
