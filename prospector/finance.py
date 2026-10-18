"""Projetos fechados, pagamentos, despesas e números do financeiro (valores em centavos)."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping

import yaml

from . import db

TIPOS = ("site", "automacao", "outro")
EM_ANDAMENTO = "em_andamento"
ENTREGUE = "entregue"
CANCELADO = "cancelado"
PROJECT_STATUSES = (EM_ANDAMENTO, ENTREGUE, CANCELADO)
MAX_CENTAVOS = 10_000_000_00  # R$ 10 milhões: teto de sanidade para digitação errada


class FinanceError(ValueError):
    pass


# ---------- dinheiro ----------

def parse_money(raw: str | int | float | None) -> int:
    """'1.500,90' / '1500.90' / '1500' / 'R$ 600' -> centavos. Erro se inválido ou negativo."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise FinanceError("Valor vazio")
    if isinstance(raw, (int, float)):
        cents = round(float(raw) * 100)
    else:
        text = re.sub(r"[R$\s]", "", raw.strip())
        if not re.fullmatch(r"\d{1,3}(\.\d{3})*(,\d{1,2})?|\d+([.,]\d{1,2})?", text):
            raise FinanceError(f"Valor inválido: {raw!r}")
        if "," in text:
            text = text.replace(".", "").replace(",", ".")
        elif re.fullmatch(r"\d{1,3}(\.\d{3})+", text):
            text = text.replace(".", "")
        cents = round(float(text) * 100)
    if cents < 0 or cents > MAX_CENTAVOS:
        raise FinanceError(f"Valor fora do limite: {raw!r}")
    return cents


def brl(cents: int | None) -> str:
    """12345 -> 'R$ 123,45'."""
    cents = cents or 0
    sign = "-" if cents < 0 else ""
    reais, cent = divmod(abs(cents), 100)
    return f"{sign}R$ {reais:,}".replace(",", ".") + f",{cent:02d}"


def brl_short(cents: int | None) -> str:
    """Compacto para gráficos: R$ 950 / R$ 1,2 mil / R$ 3,4 mi."""
    value = (cents or 0) / 100
    if abs(value) >= 1_000_000:
        return f"R$ {value / 1_000_000:.1f} mi".replace(".", ",")
    if abs(value) >= 1_000:
        return f"R$ {value / 1_000:.1f} mil".replace(".", ",").replace(",0 mil", " mil")
    return f"R$ {value:.0f}"


def parse_date(raw: str | None, field: str = "data") -> date:
    try:
        return date.fromisoformat((raw or "").strip())
    except ValueError as exc:
        raise FinanceError(f"{field} inválida: {raw!r} (use AAAA-MM-DD)") from exc


# ---------- configuração ----------

@dataclass(frozen=True)
class Preco:
    descricao: str
    valor: int
    custo_estimado: int
    mensalidade: int
    prazo_dias: int
    inclui: tuple[str, ...] = ()


@dataclass(frozen=True)
class FinanceConfig:
    meta_mensal: int
    precos: Mapping[str, Preco]
    oferta_para_tipo: Mapping[str, str]
    entrada_percentual: int
    categorias_despesa: tuple[str, ...]

    @classmethod
    def load(cls, path: Path) -> FinanceConfig:
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        data = data or {}
        precos = {}
        for tipo, p in (data.get("precos") or {}).items():
            if tipo not in TIPOS:
                raise FinanceError(f"financeiro.yaml: tipo desconhecido {tipo!r}")
            precos[tipo] = Preco(
                descricao=str(p.get("descricao") or tipo), valor=parse_money(p.get("valor", 0)),
                custo_estimado=parse_money(p.get("custo_estimado", 0)),
                mensalidade=parse_money(p.get("mensalidade", 0)), prazo_dias=int(p.get("prazo_dias", 7)),
                inclui=tuple(str(x) for x in p.get("inclui") or ()),
            )
        entrada = int(data.get("entrada_percentual", 50))
        if not 0 <= entrada <= 100:
            raise FinanceError("financeiro.yaml: entrada_percentual precisa estar entre 0 e 100")
        return cls(
            meta_mensal=parse_money(data.get("meta_mensal", 0)),
            precos=precos,
            oferta_para_tipo=dict(data.get("oferta_para_tipo") or {"sem_site": "site", "so_social": "site",
                                                                   "tem_site": "automacao",
                                                                   "site_problema": "site"}),
            entrada_percentual=entrada,
            categorias_despesa=tuple(data.get("categorias_despesa") or ("outro",)),
        )

    def defaults_for(self, oferta: str | None) -> tuple[str, Preco]:
        tipo = self.oferta_para_tipo.get(oferta or "", "site")
        preco = self.precos.get(tipo) or Preco(tipo, 0, 0, 0, 7)
        return tipo, preco


# ---------- projetos ----------

@dataclass(frozen=True)
class NovoProjeto:
    cliente: str
    tipo: str
    valor: int
    fechado_em: date
    prazo: date | None = None
    descricao: str = ""
    custo_estimado: int = 0
    mensalidade: int = 0
    lead_id: int | None = None
    entrada_percentual: int = 50

    def validate(self) -> None:
        if not self.cliente.strip():
            raise FinanceError("Cliente vazio")
        if self.tipo not in TIPOS:
            raise FinanceError(f"Tipo inválido: {self.tipo}")
        if self.valor <= 0:
            raise FinanceError("Valor do projeto precisa ser maior que zero")
        if self.prazo and self.prazo < self.fechado_em:
            raise FinanceError("Prazo antes da data de fechamento")
        if not 0 <= self.entrada_percentual <= 100:
            raise FinanceError("Entrada precisa estar entre 0% e 100%")


def create_project(conn: sqlite3.Connection, p: NovoProjeto) -> int:
    """Cria o projeto, as parcelas (entrada + restante na entrega) e marca o lead como cliente."""
    p.validate()
    now = db.iso(db.utcnow())
    with db.tx(conn):
        cur = conn.execute(
            "INSERT INTO projetos (lead_id, cliente, tipo, descricao, valor_centavos, custo_estimado_centavos, "
            "mensalidade_centavos, status, fechado_em, prazo, criado_em, atualizado_em) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (p.lead_id, p.cliente.strip(), p.tipo, p.descricao.strip() or None, p.valor, p.custo_estimado,
             p.mensalidade, EM_ANDAMENTO, p.fechado_em.isoformat(), p.prazo.isoformat() if p.prazo else None,
             now, now),
        )
        project_id = int(cur.lastrowid)
        entrada = p.valor * p.entrada_percentual // 100
        restante = p.valor - entrada
        due_rest = (p.prazo or p.fechado_em).isoformat()
        if entrada:
            _add_payment(conn, project_id, "Entrada", entrada, p.fechado_em.isoformat(), now)
        if restante:
            _add_payment(conn, project_id, "Restante (na entrega)" if entrada else "Pagamento", restante,
                         due_rest, now)
        if p.lead_id:
            db.update_lead(conn, p.lead_id, status=db.CLIENTE, sinal_fechamento=0)
    return project_id


def _add_payment(conn: sqlite3.Connection, project_id: int, descricao: str, valor: int, vencimento: str,
                 now: str, competencia: str | None = None) -> None:
    conn.execute(
        "INSERT INTO pagamentos (projeto_id, descricao, valor_centavos, vencimento, competencia, criado_em) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (project_id, descricao, valor, vencimento, competencia, now),
    )


def add_payment(conn: sqlite3.Connection, project_id: int, descricao: str, valor: int, vencimento: date) -> None:
    if valor <= 0:
        raise FinanceError("Valor precisa ser maior que zero")
    with db.tx(conn):
        _require_project(conn, project_id)
        _add_payment(conn, project_id, descricao.strip() or "Pagamento", valor, vencimento.isoformat(),
                     db.iso(db.utcnow()))


def mark_paid(conn: sqlite3.Connection, payment_id: int, paid_on: date | None) -> None:
    """paid_on=None desfaz o pagamento."""
    with db.tx(conn):
        cur = conn.execute("UPDATE pagamentos SET pago_em = ? WHERE id = ?",
                           (paid_on.isoformat() if paid_on else None, payment_id))
        if not cur.rowcount:
            raise FinanceError("Pagamento não encontrado")


def delete_payment(conn: sqlite3.Connection, payment_id: int) -> None:
    with db.tx(conn):
        conn.execute("DELETE FROM pagamentos WHERE id = ?", (payment_id,))


def set_project_status(conn: sqlite3.Connection, project_id: int, status: str, today: date) -> None:
    if status not in PROJECT_STATUSES:
        raise FinanceError(f"Status inválido: {status}")
    with db.tx(conn):
        _require_project(conn, project_id)
        now = db.iso(db.utcnow())
        if status == ENTREGUE:
            conn.execute("UPDATE projetos SET status = ?, entregue_em = COALESCE(entregue_em, ?), "
                         "atualizado_em = ? WHERE id = ?", (status, today.isoformat(), now, project_id))
        elif status == EM_ANDAMENTO:
            conn.execute("UPDATE projetos SET status = ?, entregue_em = NULL, atualizado_em = ? WHERE id = ?",
                         (status, now, project_id))
        else:
            conn.execute("UPDATE projetos SET status = ?, atualizado_em = ? WHERE id = ?",
                         (status, now, project_id))
        if status == CANCELADO:
            # parcelas futuras não pagas deixam de existir
            conn.execute("DELETE FROM pagamentos WHERE projeto_id = ? AND pago_em IS NULL", (project_id,))


def _require_project(conn: sqlite3.Connection, project_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM projetos WHERE id = ?", (project_id,)).fetchone()
    if not row:
        raise FinanceError("Projeto não encontrado")
    return row


def add_expense(conn: sqlite3.Connection, descricao: str, valor: int, data: date, categoria: str,
                project_id: int | None = None) -> None:
    if not descricao.strip():
        raise FinanceError("Descrição vazia")
    if valor <= 0:
        raise FinanceError("Valor precisa ser maior que zero")
    with db.tx(conn):
        if project_id:
            _require_project(conn, project_id)
        conn.execute(
            "INSERT INTO despesas (projeto_id, descricao, categoria, valor_centavos, data, criado_em) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, descricao.strip(), categoria, valor, data.isoformat(), db.iso(db.utcnow())),
        )


def delete_expense(conn: sqlite3.Connection, expense_id: int) -> None:
    with db.tx(conn):
        conn.execute("DELETE FROM despesas WHERE id = ?", (expense_id,))


# ---------- mensalidades ----------

def _month_add(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    last_day = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(d.day, last_day))


def ensure_monthly_fees(conn: sqlite3.Connection, today: date) -> int:
    """Gera a cobrança da mensalidade de cada mês desde a entrega até o mês atual (idempotente)."""
    rows = conn.execute(
        "SELECT * FROM projetos WHERE status = ? AND mensalidade_centavos > 0 AND entregue_em IS NOT NULL",
        (ENTREGUE,),
    ).fetchall()
    created = 0
    now = db.iso(db.utcnow())
    with db.tx(conn):
        for p in rows:
            start = date.fromisoformat(p["entregue_em"])
            n = 1
            while (due := _month_add(start, n)) <= today + timedelta(days=7):  # avisa 7 dias antes
                comp = f"{due:%Y-%m}"
                cur = conn.execute(
                    "INSERT OR IGNORE INTO pagamentos (projeto_id, descricao, valor_centavos, vencimento, "
                    "competencia, criado_em) VALUES (?, ?, ?, ?, ?, ?)",
                    (p["id"], f"Mensalidade {due:%m/%Y}", p["mensalidade_centavos"], due.isoformat(), comp, now),
                )
                created += cur.rowcount
                n += 1
    return created


def future_fees(conn: sqlite3.Connection, today: date, days: int) -> int:
    """Mensalidades que vencem nos próximos `days` dias e ainda não viraram cobrança."""
    total = 0
    horizon = today + timedelta(days=days)
    for p in conn.execute("SELECT * FROM projetos WHERE status = ? AND mensalidade_centavos > 0 "
                          "AND entregue_em IS NOT NULL", (ENTREGUE,)).fetchall():
        start = date.fromisoformat(p["entregue_em"])
        n = 1
        while (due := _month_add(start, n)) <= horizon:
            if due > today and not conn.execute(
                "SELECT 1 FROM pagamentos WHERE projeto_id = ? AND competencia = ?", (p["id"], f"{due:%Y-%m}")
            ).fetchone():
                total += p["mensalidade_centavos"]
            n += 1
    return total


# ---------- consultas ----------

def projects(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT p.*, "
        "COALESCE((SELECT SUM(valor_centavos) FROM pagamentos g WHERE g.projeto_id = p.id AND g.pago_em IS NOT NULL), 0) AS recebido, "
        "COALESCE((SELECT SUM(valor_centavos) FROM pagamentos g WHERE g.projeto_id = p.id AND g.pago_em IS NULL), 0) AS a_receber, "
        "COALESCE((SELECT SUM(valor_centavos) FROM despesas d WHERE d.projeto_id = p.id), 0) AS despesas "
        "FROM projetos p ORDER BY CASE p.status WHEN 'em_andamento' THEN 0 WHEN 'entregue' THEN 1 ELSE 2 END, "
        "p.fechado_em DESC, p.id DESC"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["lucro"] = d["recebido"] - d["despesas"]
        out.append(d)
    return out


def project(conn: sqlite3.Connection, project_id: int) -> dict[str, Any] | None:
    for p in projects(conn):
        if p["id"] == project_id:
            return p
    return None


def payments(conn: sqlite3.Connection, project_id: int | None = None) -> list[sqlite3.Row]:
    sql = ("SELECT g.*, p.cliente FROM pagamentos g JOIN projetos p ON p.id = g.projeto_id")
    params: tuple = ()
    if project_id is not None:
        sql += " WHERE g.projeto_id = ?"
        params = (project_id,)
    return conn.execute(sql + " ORDER BY g.pago_em IS NOT NULL, g.vencimento, g.id", params).fetchall()


def expenses(conn: sqlite3.Connection, project_id: int | None = None, limit: int = 200) -> list[sqlite3.Row]:
    sql = "SELECT d.*, p.cliente FROM despesas d LEFT JOIN projetos p ON p.id = d.projeto_id"
    params: tuple = ()
    if project_id is not None:
        sql += " WHERE d.projeto_id = ?"
        params = (project_id,)
    return conn.execute(sql + f" ORDER BY d.data DESC, d.id DESC LIMIT {int(limit)}", params).fetchall()


def month_bounds(day: date) -> tuple[str, str]:
    first = day.replace(day=1)
    return first.isoformat(), _month_add(first, 1).isoformat()


def received_between(conn: sqlite3.Connection, start: str, end: str) -> int:
    return conn.execute("SELECT COALESCE(SUM(valor_centavos), 0) FROM pagamentos WHERE pago_em >= ? AND pago_em < ?",
                        (start, end)).fetchone()[0]


def spent_between(conn: sqlite3.Connection, start: str, end: str) -> int:
    return conn.execute("SELECT COALESCE(SUM(valor_centavos), 0) FROM despesas WHERE data >= ? AND data < ?",
                        (start, end)).fetchone()[0]


def monthly_series(conn: sqlite3.Connection, today: date, months: int = 6) -> list[dict[str, Any]]:
    """Receita recebida x despesas dos últimos `months` meses (o atual incluso)."""
    first = today.replace(day=1)
    out = []
    for i in range(months - 1, -1, -1):
        start = _month_add(first, -i)
        s, e = start.isoformat(), _month_add(start, 1).isoformat()
        rec, desp = received_between(conn, s, e), spent_between(conn, s, e)
        out.append({"mes": start, "receita": rec, "despesas": desp, "lucro": rec - desp})
    return out
