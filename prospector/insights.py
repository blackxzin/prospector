"""Gerenciador inteligente: resumo, alertas, previsão e sugestões a partir dos dados reais.

Tudo é regra explícita sobre o banco (sem IA externa): dá para conferir de onde sai cada número.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from . import db
from . import finance as fin

CRITICO = "critico"
ATENCAO = "atencao"
BOM = "bom"
INFO = "info"

MIN_AMOSTRA = 5            # envios mínimos para comparar conversão entre segmentos
CONVERSAO_ALTA = 0.30      # acima disso, sugere testar preço maior
DIAS_INTERESSADO_PARADO = 1
DIAS_ENTREGUE_SEM_PAGAR = 7
DIAS_ESCALACAO = 14         # quanto tempo um alerta de ameaça/denúncia fica no painel


@dataclass(frozen=True)
class Alerta:
    nivel: str
    titulo: str
    detalhe: str
    link: str = ""


@dataclass(frozen=True)
class Resumo:
    recebido_mes: int
    despesas_mes: int
    lucro_mes: int
    a_receber: int
    vencido: int
    previsao_30d: int
    mrr: int
    ticket_medio: int
    projetos_ativos: int
    projetos_total: int
    meta: int
    meta_pct: int
    margem_pct: int | None


@dataclass
class Painel:
    resumo: Resumo
    alertas: list[Alerta] = field(default_factory=list)
    sugestoes: list[str] = field(default_factory=list)
    segmentos: list[dict[str, Any]] = field(default_factory=list)
    serie: list[dict[str, Any]] = field(default_factory=list)


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return conn.execute(sql, params).fetchone()[0] or 0


def summary(conn: sqlite3.Connection, today: date, meta: int) -> Resumo:
    start, end = fin.month_bounds(today)
    rec = fin.received_between(conn, start, end)
    desp = fin.spent_between(conn, start, end)
    a_receber = _scalar(conn, "SELECT SUM(g.valor_centavos) FROM pagamentos g JOIN projetos p ON p.id = g.projeto_id "
                              "WHERE g.pago_em IS NULL AND p.status != 'cancelado'")
    vencido = _scalar(conn, "SELECT SUM(g.valor_centavos) FROM pagamentos g JOIN projetos p ON p.id = g.projeto_id "
                            "WHERE g.pago_em IS NULL AND g.vencimento < ? AND p.status != 'cancelado'",
                      (today.isoformat(),))
    em_30 = _scalar(conn, "SELECT SUM(g.valor_centavos) FROM pagamentos g JOIN projetos p ON p.id = g.projeto_id "
                          "WHERE g.pago_em IS NULL AND g.vencimento <= ? AND p.status != 'cancelado'",
                    ((today + timedelta(days=30)).isoformat(),))
    mrr = _scalar(conn, "SELECT SUM(mensalidade_centavos) FROM projetos WHERE status = 'entregue'")
    ticket = _scalar(conn, "SELECT CAST(AVG(valor_centavos) AS INTEGER) FROM projetos WHERE status != 'cancelado'")
    ativos = _scalar(conn, "SELECT COUNT(*) FROM projetos WHERE status = 'em_andamento'")
    total = _scalar(conn, "SELECT COUNT(*) FROM projetos WHERE status != 'cancelado'")
    return Resumo(
        recebido_mes=rec, despesas_mes=desp, lucro_mes=rec - desp, a_receber=a_receber, vencido=vencido,
        # parcelas em aberto até 30 dias (inclui vencidas) + mensalidades que ainda não viraram cobrança
        previsao_30d=em_30 + fin.future_fees(conn, today, 30),
        mrr=mrr, ticket_medio=ticket, projetos_ativos=ativos, projetos_total=total, meta=meta,
        meta_pct=min(100, round(rec * 100 / meta)) if meta else 0,
        margem_pct=round((rec - desp) * 100 / rec) if rec else None,
    )


def segments(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Conversão por busca de origem: contatados -> responderam -> clientes."""
    rows = conn.execute(
        "SELECT l.busca AS busca, l.oferta AS oferta, COUNT(*) AS contatados, "
        "SUM(l.respondeu_em IS NOT NULL) AS responderam, "
        "SUM(l.status = 'nao_quer') AS recusaram, "
        "SUM(EXISTS (SELECT 1 FROM projetos p WHERE p.lead_id = l.id AND p.status != 'cancelado')) AS clientes, "
        "COALESCE(SUM((SELECT SUM(p.valor_centavos) FROM projetos p WHERE p.lead_id = l.id "
        "AND p.status != 'cancelado')), 0) AS faturado "
        "FROM leads l WHERE l.enviado_em IS NOT NULL GROUP BY l.busca, l.oferta ORDER BY clientes DESC, responderam DESC"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["taxa_resposta"] = d["responderam"] / d["contatados"] if d["contatados"] else 0.0
        d["taxa_fechamento"] = d["clientes"] / d["contatados"] if d["contatados"] else 0.0
        out.append(d)
    return out


def alerts(conn: sqlite3.Connection, today: date) -> list[Alerta]:
    out: list[Alerta] = []
    stop = db.stop_reason(conn)
    if stop:
        out.append(Alerta(CRITICO, "Envio parado", stop, "/"))

    desde = (today - timedelta(days=DIAS_ESCALACAO)).isoformat()
    for e in conn.execute("SELECT id, nome, resposta FROM leads WHERE escalar = 1 AND substr(escalar_em, 1, 10) >= ? "
                          "ORDER BY escalar_em DESC", (desde,)):
        out.append(Alerta(CRITICO, f"Não responda sozinho: {e['nome']}",
                          f"Falou em advogado, denúncia ou golpe: “{(e['resposta'] or '')[:120]}”. Leia a conversa e decida você.",
                          "/leads"))

    for g in conn.execute(
        "SELECT g.*, p.cliente, p.id AS pid FROM pagamentos g JOIN projetos p ON p.id = g.projeto_id "
        "WHERE g.pago_em IS NULL AND g.vencimento < ? AND p.status != 'cancelado' ORDER BY g.vencimento",
        (today.isoformat(),),
    ):
        dias = (today - date.fromisoformat(g["vencimento"])).days
        out.append(Alerta(CRITICO, f"Cobrar {g['cliente']}",
                          f"{g['descricao']} de {fin.brl(g['valor_centavos'])} venceu há {dias} dia(s).",
                          f"/projetos/{g['pid']}"))

    for g in conn.execute(
        "SELECT g.*, p.cliente, p.id AS pid FROM pagamentos g JOIN projetos p ON p.id = g.projeto_id "
        "WHERE g.pago_em IS NULL AND g.vencimento BETWEEN ? AND ? AND p.status != 'cancelado' ORDER BY g.vencimento",
        (today.isoformat(), (today + timedelta(days=3)).isoformat()),
    ):
        quando = "hoje" if g["vencimento"] == today.isoformat() else f"em {fin.parse_date(g['vencimento']):%d/%m}"
        out.append(Alerta(ATENCAO, f"Pagamento vence {quando}",
                          f"{g['cliente']}: {g['descricao']} de {fin.brl(g['valor_centavos'])}.",
                          f"/projetos/{g['pid']}"))

    for p in conn.execute("SELECT * FROM projetos WHERE status = 'em_andamento' AND prazo IS NOT NULL ORDER BY prazo"):
        prazo = date.fromisoformat(p["prazo"])
        if prazo < today:
            out.append(Alerta(CRITICO, f"Prazo estourado: {p['cliente']}",
                              f"Era para {prazo:%d/%m}. Entregue ou combine nova data.", f"/projetos/{p['id']}"))
        elif prazo <= today + timedelta(days=2):
            out.append(Alerta(ATENCAO, f"Entrega próxima: {p['cliente']}",
                              f"Prazo {prazo:%d/%m}.", f"/projetos/{p['id']}"))

    limite = (today - timedelta(days=DIAS_ENTREGUE_SEM_PAGAR)).isoformat()
    for p in conn.execute(
        "SELECT p.* FROM projetos p WHERE p.status = 'entregue' AND p.entregue_em <= ? AND EXISTS "
        "(SELECT 1 FROM pagamentos g WHERE g.projeto_id = p.id AND g.pago_em IS NULL AND g.competencia IS NULL)",
        (limite,),
    ):
        out.append(Alerta(ATENCAO, f"Entregue e não quitado: {p['cliente']}",
                          f"Entregue em {fin.parse_date(p['entregue_em']):%d/%m} e ainda tem parcela em aberto.",
                          f"/projetos/{p['id']}"))

    for lead in conn.execute("SELECT * FROM leads WHERE sinal_fechamento = 1 AND status != 'cliente'"):
        out.append(Alerta(BOM, f"Possível fechamento: {lead['nome']}",
                          f"Respondeu: “{(lead['resposta'] or '')[:90]}”. Confirme e registre o projeto.",
                          f"/projetos/novo?lead={lead['id']}"))

    corte = db.iso(db.utcnow() - timedelta(days=DIAS_INTERESSADO_PARADO))
    for lead in conn.execute(
        "SELECT * FROM leads WHERE status = 'interessado' AND sinal_fechamento = 0 AND respondeu_em <= ?", (corte,)
    ):
        out.append(Alerta(ATENCAO, f"Interessado esperando: {lead['nome']}",
                          "Respondeu com interesse e ainda não recebeu retorno. Responda pelo WhatsApp.",
                          f"/?status=interessado"))

    pend = _scalar(conn, "SELECT COUNT(*) FROM fila WHERE status = 'pendente'")
    if pend:
        out.append(Alerta(INFO, f"{pend} mensagem(ns) aguardando aprovação", "Revise a fila antes do envio.", "/fila"))
    priority = {CRITICO: 0, BOM: 1, ATENCAO: 2, INFO: 3}
    return sorted(out, key=lambda a: priority[a.nivel])  # sorted é estável: mantém a ordem dentro do nível


def suggestions(conn: sqlite3.Connection, resumo: Resumo, segs: list[dict[str, Any]], prices: dict[str, int],
                today: date) -> list[str]:
    out: list[str] = []
    # Meta do mês
    if resumo.meta and resumo.recebido_mes < resumo.meta:
        falta = resumo.meta - resumo.recebido_mes
        ticket = resumo.ticket_medio or max(prices.values(), default=0)
        if ticket:
            n = -(-falta // ticket)
            out.append(f"Faltam {fin.brl(falta)} para a meta do mês: cerca de {n} projeto(s) no seu ticket "
                       f"médio de {fin.brl(ticket)}. Já há {fin.brl(resumo.previsao_30d)} previstos para 30 dias.")
    elif resumo.meta and resumo.recebido_mes >= resumo.meta:
        out.append(f"Meta do mês batida ({fin.brl(resumo.recebido_mes)}). Bom momento para subir a meta "
                   "no financeiro.yaml.")

    # Melhor e pior segmento
    amostra = [s for s in segs if s["contatados"] >= MIN_AMOSTRA]
    if len(amostra) >= 2:
        best = max(amostra, key=lambda s: (s["taxa_fechamento"], s["taxa_resposta"]))
        worst = min(amostra, key=lambda s: (s["taxa_fechamento"], s["taxa_resposta"]))
        if best is not worst:
            out.append(f"“{best['busca']}” ({best['oferta']}) converte melhor: "
                       f"{best['taxa_resposta']:.0%} respondem e {best['taxa_fechamento']:.0%} fecham. "
                       "Repita essa busca em cidades vizinhas.")
            if worst["taxa_resposta"] < best["taxa_resposta"] / 2:
                out.append(f"“{worst['busca']}” responde pouco ({worst['taxa_resposta']:.0%}). "
                           "Teste outro texto no templates.yaml ou pause esse nicho.")
    elif segs:
        total = sum(s["contatados"] for s in segs)
        if total < MIN_AMOSTRA * 2:
            out.append(f"Ainda são só {total} contatos: com uns {MIN_AMOSTRA * 2} já dá para comparar nichos "
                       "e dizer o que converte melhor.")

    # Preço
    for s in amostra:
        if s["taxa_fechamento"] >= CONVERSAO_ALTA:
            out.append(f"{s['taxa_fechamento']:.0%} de fechamento em “{s['busca']}” é alto: "
                       "seu preço pode estar baixo. Teste +20% nos próximos.")
            break

    # Receita recorrente
    sem_mensalidade = _scalar(conn, "SELECT COUNT(*) FROM projetos WHERE status = 'entregue' "
                                    "AND mensalidade_centavos = 0")
    if sem_mensalidade:
        out.append(f"{sem_mensalidade} cliente(s) entregue(s) sem mensalidade. Oferecer manutenção "
                   "(atualizar cardápio, hospedagem) gera receita todo mês.")
    if resumo.mrr:
        out.append(f"Receita recorrente: {fin.brl(resumo.mrr)}/mês, que já cobre "
                   f"{min(100, round(resumo.mrr * 100 / resumo.meta)) if resumo.meta else 0}% da meta.")

    # Margem
    if resumo.margem_pct is not None and resumo.margem_pct < 50:
        out.append(f"A margem do mês está em {resumo.margem_pct}%. Revise as despesas por categoria.")

    # Funil parado
    novos = _scalar(conn, "SELECT COUNT(*) FROM leads WHERE status = 'novo'")
    if novos < 8:
        out.append(f"Só {novos} lead(s) novo(s) disponível(is). Rode uma busca nova para não parar a fila amanhã.")
    return out


def backup_alert(last: datetime | None, now: datetime, max_days: int = 2) -> Alerta | None:
    if last is None:
        return Alerta(ATENCAO, "Nenhum backup do banco ainda",
                      "Rode 'prospector backup' e coloque no cron (veja o README).")
    days = (now - last).days
    if days >= max_days:
        return Alerta(ATENCAO, f"Último backup há {days} dias", "Rode 'prospector backup' (ou confira o cron).")
    return None


def build(conn: sqlite3.Connection, cfg: fin.FinanceConfig, today: date,
          extra_alerts: list[Alerta] | None = None) -> Painel:
    fin.ensure_monthly_fees(conn, today)
    resumo = summary(conn, today, cfg.meta_mensal)
    segs = segments(conn)
    prices = {k: v.valor for k, v in cfg.precos.items()}
    priority = {CRITICO: 0, BOM: 1, ATENCAO: 2, INFO: 3}
    return Painel(
        resumo=resumo,
        alertas=sorted(alerts(conn, today) + list(extra_alerts or []), key=lambda a: priority[a.nivel]),
        sugestoes=suggestions(conn, resumo, segs, prices, today),
        segmentos=segs,
        serie=fin.monthly_series(conn, today),
    )
