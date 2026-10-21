"""Métricas operacionais para o painel (prospecção): por dia, cidade, nicho e horário."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

from .schedule import Rules, fmt_local, in_window, next_window_start, sent_last_hour, sent_today
from . import db

HEAT_HOURS = tuple(range(7, 23))  # 7h às 22h


def _local_date(iso: str, tz: tzinfo) -> date:
    return datetime.fromisoformat(iso).astimezone(tz).date()


def daily(conn: sqlite3.Connection, today: date, tz: tzinfo, days: int = 30) -> list[dict[str, Any]]:
    """Por dia: primeiras mensagens + follow-ups enviados, respostas recebidas e leads novos."""
    start = today - timedelta(days=days - 1)
    buckets = {start + timedelta(days=i): {"enviados": 0, "respostas": 0, "leads": 0} for i in range(days)}
    since = db.iso(datetime.combine(start, datetime.min.time(), tzinfo=tz) - timedelta(days=1))
    queries = (
        ("enviados", "SELECT enviado_em FROM envios WHERE ok = 1 AND dry_run = 0 "
                     "AND tipo IN ('primeira', 'followup') AND enviado_em >= ?"),
        ("respostas", "SELECT recebido_em FROM respostas WHERE recebido_em >= ?"),
        ("leads", "SELECT criado_em FROM leads WHERE criado_em >= ?"),
    )
    for key, sql in queries:
        for (ts,) in conn.execute(sql, (since,)):
            d = _local_date(ts, tz)
            if d in buckets:
                buckets[d][key] += 1
    return [{"dia": d, **v} for d, v in buckets.items()]


def _nicho(busca: str | None) -> str:
    return (busca or "—").split(" em ")[0].strip() or "—"


def by_dimension(conn: sqlite3.Connection, dim: str, limit: int = 8) -> list[dict[str, Any]]:
    """Conversão por 'cidade' ou 'nicho', só entre leads já contatados. Resposta = gente de verdade (robô não conta)."""
    rows = conn.execute(
        "SELECT l.cidade, l.busca, l.nicho, l.resposta_humana, l.status, "
        "EXISTS (SELECT 1 FROM projetos p WHERE p.lead_id = l.id AND p.status != 'cancelado') AS cliente "
        "FROM leads l WHERE l.enviado_em IS NOT NULL"
    ).fetchall()
    agg: dict[str, dict[str, int]] = defaultdict(lambda: {"contatados": 0, "responderam": 0, "clientes": 0})
    for r in rows:
        key = (r["cidade"] or "—") if dim == "cidade" else (r["nicho"] or _nicho(r["busca"]))
        a = agg[key]
        a["contatados"] += 1
        a["responderam"] += int(bool(r["resposta_humana"]) and r["status"] != db.NAO_QUER)
        a["clientes"] += int(r["cliente"])
    out = [{"nome": k, **v, "taxa": v["responderam"] / v["contatados"] if v["contatados"] else 0.0}
           for k, v in agg.items()]
    # as mais contatadas entram no gráfico; dentro delas, ordena pela taxa de resposta
    top = sorted(out, key=lambda x: (-x["contatados"], x["nome"]))[:limit]
    return sorted(top, key=lambda x: (-x["taxa"], -x["contatados"]))


def nicho_report(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Uma linha por nicho: estoque, enviadas, respostas de gente de verdade, interesse e clientes."""
    rows = conn.execute(
        "SELECT COALESCE(nicho, 'sem nicho') AS nicho, "
        "SUM(status = 'novo') AS estoque, SUM(enviado_em IS NOT NULL) AS enviadas, "
        "SUM(enviado_em IS NOT NULL AND resposta_humana = 1 AND status != 'nao_quer') AS humanas, "
        "SUM(status = 'interessado') AS interessados, "
        "SUM(EXISTS (SELECT 1 FROM projetos p WHERE p.lead_id = leads.id AND p.status != 'cancelado')) AS clientes "
        "FROM leads WHERE pais = 'BR' GROUP BY 1"
    ).fetchall()
    out = [{**dict(r), "taxa": (r["humanas"] or 0) / r["enviadas"] if r["enviadas"] else 0.0} for r in rows]
    return sorted(out, key=lambda x: (-x["enviadas"], -x["estoque"], x["nicho"]))


def leads_by_city(conn: sqlite3.Connection, limit: int = 8) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT COALESCE(cidade, '—') AS nome, COUNT(*) AS total, SUM(status = 'novo') AS novos "
        "FROM leads WHERE status != 'descartado' GROUP BY 1 ORDER BY total DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def response_heatmap(conn: sqlite3.Connection, tz: tzinfo) -> list[list[int]]:
    """Respostas por dia da semana (0 = seg) × hora (HEAT_HOURS)."""
    grid = [[0] * len(HEAT_HOURS) for _ in range(7)]
    for (ts,) in conn.execute("SELECT recebido_em FROM respostas"):
        t = datetime.fromisoformat(ts).astimezone(tz)
        if t.hour in HEAT_HOURS:
            grid[t.weekday()][HEAT_HOURS.index(t.hour)] += 1
    return grid


def today_ops(conn: sqlite3.Connection, rules: Rules, now: datetime) -> dict[str, Any]:
    sent = db.real_send_times(conn, now - timedelta(days=2))
    hoje = sent_today(now, sent, rules.tz)
    count = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "enviados_hoje": hoje,
        "limite_dia": rules.max_per_day,
        "ultima_hora": sent_last_hour(now, sent),
        "limite_hora": rules.max_per_hour,
        "pct_dia": min(100, round(hoje * 100 / rules.max_per_day)) if rules.max_per_day else 0,
        "na_janela": in_window(now, rules),
        "proxima_janela": fmt_local(next_window_start(now, rules), rules.tz),
        "aprovados": count("SELECT COUNT(*) FROM fila WHERE status = 'aprovado'"),
        "pendentes": count("SELECT COUNT(*) FROM fila WHERE status = 'pendente'"),
        "novos": count("SELECT COUNT(*) FROM leads WHERE status = 'novo'"),
        "respostas_hoje": sum(1 for (ts,) in conn.execute(
            "SELECT recebido_em FROM respostas WHERE recebido_em >= ?", (db.iso(now - timedelta(days=1)),))
            if _local_date(ts, rules.tz) == now.astimezone(rules.tz).date()),
        "sem_retorno": count("SELECT COUNT(*) FROM leads WHERE status IN ('respondeu', 'interessado')"),
    }


def channel_report(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Resultado por canal: leads contatados, respostas de gente de verdade e interessados."""
    def row(canal: str, where: str) -> dict[str, object]:
        r = conn.execute(
            f"SELECT COUNT(*) n, COALESCE(SUM(l.resposta_humana = 1), 0) resp, "
            f"COALESCE(SUM(l.status IN ('interessado', 'cliente')), 0) inter FROM leads l WHERE {where}").fetchone()
        n = r["n"]
        return {"canal": canal, "contatados": n, "responderam": r["resp"], "interessados": r["inter"],
                "taxa": (r["resp"] / n) if n else 0.0}

    return [
        row("whatsapp", "l.id IN (SELECT lead_id FROM envios WHERE tipo = 'primeira' AND ok = 1 AND dry_run = 0)"),
        row("e-mail", "l.id IN (SELECT lead_id FROM canal_fila WHERE canal = 'email' AND status = 'enviado')"),
        row("instagram", "l.id IN (SELECT lead_id FROM canal_fila WHERE canal = 'instagram' AND status = 'enviado')"),
    ]
