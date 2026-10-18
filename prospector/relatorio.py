"""Relatório semanal: o que aconteceu nos últimos N dias, em texto (Markdown)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

from .finance import brl


@dataclass(frozen=True)
class Relatorio:
    inicio: date
    fim: date
    leads_novos: int
    enviados: int
    responderam: int
    nao_quer: int
    interessados: int
    fechados: int
    faturado: int
    recebido: int
    despesas: int
    melhor_busca: str | None
    buscas_feitas: int

    @property
    def taxa_resposta(self) -> float:
        return self.responderam / self.enviados if self.enviados else 0.0

    def markdown(self) -> str:
        linhas = [
            f"# Relatório {self.inicio:%d/%m} a {self.fim:%d/%m/%Y}",
            "",
            "## Prospecção",
            f"- Leads novos encontrados: **{self.leads_novos}** ({self.buscas_feitas} busca(s))",
            f"- Primeiras mensagens enviadas: **{self.enviados}**",
            f"- Responderam: **{self.responderam}** ({self.taxa_resposta:.0%})",
            f"- Interessados: **{self.interessados}** · pediram para parar: {self.nao_quer}",
            "",
            "## Dinheiro",
            f"- Projetos fechados: **{self.fechados}** ({brl(self.faturado)})",
            f"- Recebido: **{brl(self.recebido)}** · despesas: {brl(self.despesas)} · "
            f"lucro: **{brl(self.recebido - self.despesas)}**",
        ]
        if self.melhor_busca:
            linhas += ["", f"Melhor busca da semana: **{self.melhor_busca}**"]
        return "\n".join(linhas) + "\n"

    def resumo_curto(self) -> str:
        return (f"{self.enviados} enviados, {self.responderam} responderam, {self.fechados} fechados. "
                f"Recebido {brl(self.recebido)}, lucro {brl(self.recebido - self.despesas)}.")


def build(conn: sqlite3.Connection, today: date, tz: tzinfo, days: int = 7) -> Relatorio:
    inicio = today - timedelta(days=days - 1)
    # timestamps do banco são ISO em UTC: compara como datetime (com fuso) para não errar o dia
    ini_dt = datetime.combine(inicio, time(0), tzinfo=tz)
    fim_dt = datetime.combine(today + timedelta(days=1), time(0), tzinfo=tz)

    def count_ts(sql: str) -> int:
        rows = conn.execute(sql).fetchall()
        return sum(1 for r in rows if r[0] and ini_dt <= datetime.fromisoformat(r[0]) < fim_dt)

    d0, d1 = inicio.isoformat(), (today + timedelta(days=1)).isoformat()
    best = conn.execute(
        "SELECT busca, SUM(respondeu_em IS NOT NULL AND status != 'nao_quer') AS pos, COUNT(*) AS n "
        "FROM leads WHERE enviado_em IS NOT NULL GROUP BY busca HAVING n >= 3 ORDER BY pos * 1.0 / n DESC LIMIT 1"
    ).fetchone()
    return Relatorio(
        inicio=inicio, fim=today,
        leads_novos=count_ts("SELECT criado_em FROM leads"),
        enviados=count_ts("SELECT enviado_em FROM leads"),
        responderam=count_ts("SELECT respondeu_em FROM leads"),
        nao_quer=count_ts("SELECT respondeu_em FROM leads WHERE status = 'nao_quer'"),
        interessados=count_ts("SELECT respondeu_em FROM leads WHERE status IN ('interessado', 'cliente')"),
        fechados=conn.execute("SELECT COUNT(*) FROM projetos WHERE fechado_em >= ? AND fechado_em < ? "
                              "AND status != 'cancelado'", (d0, d1)).fetchone()[0],
        faturado=conn.execute("SELECT COALESCE(SUM(valor_centavos), 0) FROM projetos WHERE fechado_em >= ? "
                              "AND fechado_em < ? AND status != 'cancelado'", (d0, d1)).fetchone()[0],
        recebido=conn.execute("SELECT COALESCE(SUM(valor_centavos), 0) FROM pagamentos WHERE pago_em >= ? "
                              "AND pago_em < ?", (d0, d1)).fetchone()[0],
        despesas=conn.execute("SELECT COALESCE(SUM(valor_centavos), 0) FROM despesas WHERE data >= ? AND data < ?",
                              (d0, d1)).fetchone()[0],
        melhor_busca=best["busca"] if best and best["pos"] else None,
        buscas_feitas=count_ts("SELECT feita_em FROM buscas"),
    )
