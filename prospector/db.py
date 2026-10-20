"""SQLite: esquema e acesso aos dados."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# Status do lead
NOVO = "novo"
DESCARTADO = "descartado"
NA_FILA = "na_fila"
REJEITADO = "rejeitado"
ENVIADO = "enviado"
FOLLOW_UP = "follow_up"
RESPONDEU = "respondeu"
INTERESSADO = "interessado"
NAO_QUER = "nao_quer"
SEM_RESPOSTA = "sem_resposta"
SEM_WHATSAPP = "sem_whatsapp"
CLIENTE = "cliente"
ERRO = "erro"

STATUSES = (
    NOVO, DESCARTADO, NA_FILA, REJEITADO, ENVIADO, FOLLOW_UP,
    RESPONDEU, INTERESSADO, NAO_QUER, SEM_RESPOSTA, SEM_WHATSAPP, ERRO, CLIENTE,
)

# Tipos de envio
PRIMEIRA = "primeira"
FOLLOWUP = "followup"
AUTO_RESPOSTA = "auto_resposta"

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY,
    chave TEXT NOT NULL UNIQUE,
    nome TEXT NOT NULL,
    categoria TEXT,
    endereco TEXT,
    cidade TEXT,
    telefone_raw TEXT,
    telefone TEXT,              -- celular normalizado 55DDXXXXXXXXX (ou NULL)
    jid TEXT,                   -- JID confirmado pelo WA-AKG
    site TEXT,
    email TEXT,
    nota REAL,
    avaliacoes INTEGER,
    maps_link TEXT,
    busca TEXT,
    oferta TEXT,
    status TEXT NOT NULL DEFAULT 'novo',
    motivo TEXT,
    resposta TEXT,
    auto_resposta_enviada INTEGER NOT NULL DEFAULT 0,
    sinal_fechamento INTEGER NOT NULL DEFAULT 0,  -- resposta parece fechamento (confirmar no painel)
    criado_em TEXT NOT NULL,
    atualizado_em TEXT NOT NULL,
    aprovado_em TEXT,
    enviado_em TEXT,
    followup_em TEXT,
    respondeu_em TEXT
);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_telefone ON leads(telefone);

CREATE TABLE IF NOT EXISTS fila (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    tipo TEXT NOT NULL,          -- primeira | followup
    texto TEXT NOT NULL,
    dia TEXT NOT NULL,           -- YYYY-MM-DD (fuso local)
    status TEXT NOT NULL DEFAULT 'pendente',  -- pendente|aprovado|rejeitado|enviado|erro|cancelado
    criado_em TEXT NOT NULL,
    atualizado_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fila_status ON fila(status);

CREATE TABLE IF NOT EXISTS envios (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER REFERENCES leads(id),
    fila_id INTEGER REFERENCES fila(id),
    telefone TEXT NOT NULL,
    tipo TEXT NOT NULL,          -- primeira | followup | auto_resposta
    texto TEXT NOT NULL,
    dry_run INTEGER NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 0,
    http_status INTEGER,
    resposta_api TEXT,
    enviado_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_envios_tel ON envios(telefone);
-- Garante no banco: nunca duas primeiras mensagens reais para o mesmo número.
CREATE UNIQUE INDEX IF NOT EXISTS uq_primeira_por_numero
    ON envios(telefone) WHERE tipo = 'primeira' AND ok = 1 AND dry_run = 0;

CREATE TABLE IF NOT EXISTS respostas (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER REFERENCES leads(id),
    telefone TEXT NOT NULL,
    texto TEXT,
    recebido_em TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bloqueio (
    telefone TEXT PRIMARY KEY,
    motivo TEXT,
    criado_em TEXT NOT NULL
);

-- Financeiro: valores sempre em centavos (INTEGER), nunca float.
CREATE TABLE IF NOT EXISTS projetos (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER REFERENCES leads(id),
    cliente TEXT NOT NULL,
    tipo TEXT NOT NULL,              -- site | automacao | outro
    descricao TEXT,
    valor_centavos INTEGER NOT NULL,
    custo_estimado_centavos INTEGER NOT NULL DEFAULT 0,
    mensalidade_centavos INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'em_andamento',  -- em_andamento | entregue | cancelado
    fechado_em TEXT NOT NULL,        -- YYYY-MM-DD
    prazo TEXT,                      -- YYYY-MM-DD
    entregue_em TEXT,                -- YYYY-MM-DD
    criado_em TEXT NOT NULL,
    atualizado_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_projetos_status ON projetos(status);

CREATE TABLE IF NOT EXISTS pagamentos (
    id INTEGER PRIMARY KEY,
    projeto_id INTEGER NOT NULL REFERENCES projetos(id) ON DELETE CASCADE,
    descricao TEXT NOT NULL,
    valor_centavos INTEGER NOT NULL,
    vencimento TEXT NOT NULL,        -- YYYY-MM-DD
    pago_em TEXT,                    -- YYYY-MM-DD
    competencia TEXT,                -- YYYY-MM das mensalidades (evita duplicar)
    criado_em TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_mensalidade
    ON pagamentos(projeto_id, competencia) WHERE competencia IS NOT NULL;

CREATE TABLE IF NOT EXISTS despesas (
    id INTEGER PRIMARY KEY,
    projeto_id INTEGER REFERENCES projetos(id) ON DELETE SET NULL,
    descricao TEXT NOT NULL,
    categoria TEXT NOT NULL DEFAULT 'outro',
    valor_centavos INTEGER NOT NULL,
    data TEXT NOT NULL,              -- YYYY-MM-DD
    criado_em TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS buscas (
    id INTEGER PRIMARY KEY,
    consulta TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,            -- ok | erro
    importados INTEGER NOT NULL DEFAULT 0,
    validos INTEGER NOT NULL DEFAULT 0,
    erro TEXT,
    feita_em TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS estado (
    chave TEXT PRIMARY KEY,
    valor TEXT,
    atualizado_em TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def connect(path: Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# Colunas adicionadas depois da primeira versão: (tabela, coluna, definição).
_MIGRATIONS = (
    ("leads", "sinal_fechamento", "INTEGER NOT NULL DEFAULT 0"),
    ("leads", "variante", "TEXT"),            # hash do template da 1ª mensagem (teste A/B)
    ("leads", "site_problemas", "TEXT"),      # resultado da checagem do site
    ("leads", "site_checado_em", "TEXT"),
    ("leads", "demo_url", "TEXT"),            # site de exemplo gerado para o lead
    ("fila", "variante", "TEXT"),
    ("fila", "pagamento_id", "INTEGER"),      # itens de cobrança
    ("pagamentos", "cobrancas", "INTEGER NOT NULL DEFAULT 0"),
    ("pagamentos", "ultima_cobranca", "TEXT"),
    ("leads", "foto_url", "TEXT"),            # capa do Google Maps
    ("leads", "avaliacoes_json", "TEXT"),     # até 5 avaliações reais [{nome, nota, texto, quando}]
    ("leads", "horarios_json", "TEXT"),       # {"segunda-feira": ["18:00–23:30"], ...}
    ("leads", "atributos_json", "TEXT"),      # comodidades marcadas no Google ["Entrega", ...]
    ("leads", "latitude", "REAL"),
    ("leads", "longitude", "REAL"),
    ("leads", "presenca_checada_em", "TEXT"),  # busca de site/Instagram/cardápio online (presenca.py)
    ("respostas", "msg_id", "TEXT"),          # id da mensagem no WhatsApp (evita contar a mesma resposta 2x)
    ("fila", "liberar_em", "TEXT"),            # item aprovado só sai a partir deste instante (UTC iso)
    ("leads", "pais", "TEXT NOT NULL DEFAULT 'BR'"),                     # ISO-2 do mercado do lead
    ("leads", "idioma", "TEXT NOT NULL DEFAULT 'pt'"),                   # idioma da mensagem (pt, en, es, fr, de, it)
    ("leads", "fuso", "TEXT NOT NULL DEFAULT 'America/Sao_Paulo'"),      # horário comercial do lead
)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, definition in _MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_resposta_msg ON respostas(msg_id) WHERE msg_id IS NOT NULL")
    conn.commit()


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Transação explícita: commit no sucesso, rollback em erro."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------- estado global (parada de emergência) ----------

PARADO = "parado"


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT valor FROM estado WHERE chave = ?", (key,)).fetchone()
    return row["valor"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    with tx(conn):
        conn.execute(
            "INSERT INTO estado (chave, valor, atualizado_em) VALUES (?, ?, ?) "
            "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor, atualizado_em = excluded.atualizado_em",
            (key, value, iso(utcnow())),
        )


def stop_reason(conn: sqlite3.Connection) -> str | None:
    return get_state(conn, PARADO) or None


# ---------- bloqueio permanente ----------

def is_blocked(conn: sqlite3.Connection, numbers: set[str]) -> bool:
    if not numbers:
        return False
    marks = ",".join("?" * len(numbers))
    row = conn.execute(f"SELECT 1 FROM bloqueio WHERE telefone IN ({marks})", tuple(numbers)).fetchone()
    return row is not None


def block(conn: sqlite3.Connection, number: str, reason: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO bloqueio (telefone, motivo, criado_em) VALUES (?, ?, ?)",
        (number, reason, iso(utcnow())),
    )


# ---------- leads ----------

_LEAD_COLUMNS = frozenset({
    "nome", "categoria", "endereco", "cidade", "telefone_raw", "telefone", "jid", "site",
    "email", "nota", "avaliacoes", "maps_link", "busca", "oferta", "status", "motivo",
    "resposta", "auto_resposta_enviada", "sinal_fechamento", "variante", "site_problemas", "site_checado_em",
    "demo_url", "foto_url", "avaliacoes_json", "horarios_json", "atributos_json", "latitude", "longitude",
    "presenca_checada_em", "aprovado_em", "enviado_em", "followup_em", "respondeu_em",
})


def update_lead(conn: sqlite3.Connection, lead_id: int, **fields: object) -> None:
    if not fields:
        return
    unknown = set(fields) - _LEAD_COLUMNS
    if unknown:
        raise ValueError(f"Colunas desconhecidas: {sorted(unknown)}")
    if "status" in fields and fields["status"] not in STATUSES:
        raise ValueError(f"Status inválido: {fields['status']}")
    fields = {**fields, "atualizado_em": iso(utcnow())}
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE leads SET {cols} WHERE id = ?", (*fields.values(), lead_id))


def get_lead(conn: sqlite3.Connection, lead_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()


def first_message_already_sent(conn: sqlite3.Connection, numbers: set[str]) -> bool:
    if not numbers:
        return False
    marks = ",".join("?" * len(numbers))
    row = conn.execute(
        f"SELECT 1 FROM envios WHERE tipo = 'primeira' AND ok = 1 AND dry_run = 0 AND telefone IN ({marks})",
        tuple(numbers),
    ).fetchone()
    return row is not None


def log_send(
    conn: sqlite3.Connection,
    *,
    lead_id: int | None,
    fila_id: int | None,
    telefone: str,
    tipo: str,
    texto: str,
    dry_run: bool,
    ok: bool,
    http_status: int | None,
    resposta_api: str | None,
    when: datetime | None = None,
) -> None:
    conn.execute(
        "INSERT INTO envios (lead_id, fila_id, telefone, tipo, texto, dry_run, ok, http_status, resposta_api, enviado_em) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (lead_id, fila_id, telefone, tipo, texto, int(dry_run), int(ok), http_status,
         resposta_api, iso(when or utcnow())),
    )


def real_send_times(conn: sqlite3.Connection, since: datetime) -> list[datetime]:
    """Horários dos envios reais que contam para os limites (primeira + followup)."""
    rows = conn.execute(
        "SELECT enviado_em FROM envios WHERE ok = 1 AND dry_run = 0 "
        "AND tipo IN ('primeira', 'followup') AND enviado_em >= ? ORDER BY enviado_em",
        (iso(since),),
    ).fetchall()
    return [datetime.fromisoformat(r["enviado_em"]) for r in rows]


def funnel(conn: sqlite3.Connection) -> dict[str, int]:
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "encontrados": q("SELECT COUNT(*) FROM leads"),
        "validos": q("SELECT COUNT(*) FROM leads WHERE status != 'descartado'"),
        "aprovados": q("SELECT COUNT(*) FROM leads WHERE aprovado_em IS NOT NULL"),
        "enviados": q("SELECT COUNT(*) FROM leads WHERE enviado_em IS NOT NULL"),
        "responderam": q("SELECT COUNT(*) FROM leads WHERE respondeu_em IS NOT NULL"),
        "interessados": q("SELECT COUNT(*) FROM leads WHERE status = 'interessado'"),
    }


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM leads GROUP BY status ORDER BY n DESC").fetchall()
    return {r["status"]: r["n"] for r in rows}
