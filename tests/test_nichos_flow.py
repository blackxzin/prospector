import json
import random
from datetime import date

import pytest

from prospector import db, lote, metrics
from prospector import fila as fq
from prospector.config import ROOT
from prospector.importer import ImportResult, import_rows
from prospector.messages import TemplateSet
from prospector.nichos import Nichos
from prospector.replies import handle_inbound, parse_inbound

from .conftest import TZ, csv_row, local_dt

NOW = local_dt(2026, 10, 21, 9)


@pytest.fixture(scope="module")
def cat():
    return Nichos.load(ROOT / "nichos.yaml")


@pytest.fixture(scope="module")
def tset():
    return TemplateSet.load(ROOT / "templates.yaml")


def _row(title, phone, cat_name, link):
    return csv_row(title=title, phone=phone, category=cat_name, link=link,
                   address="R. A, 1 - Centro, Itaí - SP, 18730-000")


def _seed(conn, listas, cat):
    import_rows(conn, [_row("Odonto Sorriso", "(14) 99100-0001", "Dentista", "c1"),
                       _row("Auto Center Zé", "(14) 99100-0002", "Oficina mecânica", "m1"),
                       _row("Pizzaria Bella", "(14) 99100-0003", "Pizzaria", "p1")],
                "dentista em Itaí SP", listas, nichos=cat)


def test_import_tags_each_lead_with_its_niche(conn, listas, cat):
    _seed(conn, listas, cat)
    got = {r["nome"]: r["nicho"] for r in conn.execute("SELECT nome, nicho FROM leads")}
    assert got == {"Odonto Sorriso": "clinicas", "Auto Center Zé": "mecanicos", "Pizzaria Bella": "alimentacao"}


def test_duplicate_import_fills_missing_niche(conn, listas, cat):
    import_rows(conn, [_row("Odonto Sorriso", "(14) 99100-0001", "Dentista", "c1")], "q", listas)  # sem catálogo
    assert conn.execute("SELECT nicho FROM leads").fetchone()[0] is None
    import_rows(conn, [_row("Odonto Sorriso", "(14) 99100-0001", "Dentista", "c1")], "q", listas, nichos=cat)
    assert conn.execute("SELECT nicho FROM leads").fetchone()[0] == "clinicas"


def test_foreign_leads_have_no_niche(conn, listas, cat):
    from prospector.mundo import Origem
    import_rows(conn, [_row("Dr Smith", "(512) 555-0147", "Dentist", "x")], "dentist in Austin", listas,
                Origem("US", "en", "America/Chicago", "Austin"), nichos=cat)
    assert conn.execute("SELECT nicho FROM leads").fetchone()[0] is None


# ---------- resposta humana ----------

def _contacted(conn, listas, cat, n=1):
    _seed(conn, listas, cat)
    with db.tx(conn):
        for lead in conn.execute("SELECT id FROM leads").fetchall():
            db.update_lead(conn, lead["id"], status=db.ENVIADO, enviado_em=db.iso(local_dt(2026, 10, 20, 10)),
                           variante="v1")


def _reply(conn, phone, text):
    msg = parse_inbound({"event": "message.received",
                         "data": {"key": {"remoteJid": f"{phone}@s.whatsapp.net"}, "content": text}})
    handle_inbound(conn, msg, now=NOW, in_business_hours=True, notify_fn=lambda *a: None)


def test_only_real_people_count_as_human_replies(conn, listas, cat):
    _contacted(conn, listas, cat)
    _reply(conn, "5514991000001", "Olá, seja bem-vindo ao nosso atendimento automático!")      # robô
    _reply(conn, "5514991000002", "Pode mandar o exemplo")                                        # gente
    rows = {r["nome"]: r["resposta_humana"] for r in conn.execute("SELECT nome, resposta_humana FROM leads")}
    assert rows == {"Odonto Sorriso": 0, "Auto Center Zé": 1, "Pizzaria Bella": 0}


def test_ab_stats_and_niche_metrics_ignore_bot_replies(conn, listas, cat):
    _contacted(conn, listas, cat)
    _reply(conn, "5514991000001", "Olá, seja bem-vindo ao nosso atendimento automático!")
    _reply(conn, "5514991000002", "Pode mandar o exemplo")
    assert fq.ab_stats(conn)["v1"] == (3, 1)                                                      # 3 enviadas, 1 humana
    by_niche = {r["nome"]: r for r in metrics.by_dimension(conn, "nicho")}
    assert by_niche["mecanicos"]["responderam"] == 1 and by_niche["clinicas"]["responderam"] == 0
    assert by_niche["clinicas"]["contatados"] == 1


# ---------- fila do nicho ----------

def _stock(conn, listas, cat):
    rows = [_row(f"Odonto {i}", f"(14) 9910{i}-000{i}", "Dentista", f"c{i}") for i in range(3)]
    rows += [_row(f"Auto {i}", f"(14) 9920{i}-000{i}", "Oficina mecânica", f"m{i}") for i in range(3)]
    import_rows(conn, rows, "q", listas, nichos=cat)


def test_queue_restricted_to_a_niche_and_text_uses_its_pain(conn, listas, cat, tset, rules):
    _stock(conn, listas, cat)
    fq.build_queue(conn, tset, rules, NOW, size=3, followup_days=3, rng=random.Random(2), nicho="clinicas",
                   nichos=cat)
    names = [r["nome"] for r in conn.execute("SELECT l.nome FROM fila f JOIN leads l ON l.id = f.lead_id")]
    assert len(names) == 3 and all(n.startswith("Odonto") for n in names)


def test_queue_prefers_the_day_niche_but_fills_with_others(conn, listas, cat, tset, rules):
    _stock(conn, listas, cat)
    fq.build_queue(conn, tset, rules, NOW, size=5, followup_days=3, rng=random.Random(2), preferir_nicho="mecanicos",
                   nichos=cat)
    order = [r["nicho"] for r in conn.execute(
        "SELECT l.nicho FROM fila f JOIN leads l ON l.id = f.lead_id ORDER BY f.id")]
    assert order[:3] == ["mecanicos"] * 3 and len(order) == 5


def test_pain_variants_are_used_for_niche_leads_and_never_without_one(conn, listas, cat, tset, rules):
    pt = tset.get("pt")
    dor = cat.dor_de("clinicas")
    base = {"nome": "Odonto", "oferta": "tem_site", "nota": None, "avaliacoes": None, "categoria": "Dentista",
            "cidade": "Itaí"}
    with_dor = [pt.first_message({**base, "dor": dor}, NOW, TZ, random.Random(s)) for s in range(80)]
    assert any(dor in t for t in with_dor)
    without = [pt.first_message(base, NOW, TZ, random.Random(s)) for s in range(80)]
    assert not any("paciente" in t for t in without) and not any("  " in t for t in without)
    for offer in ("sem_site", "so_social", "tem_site"):
        for s in range(40):
            t = pt.first_message({**base, "oferta": offer, "dor": dor}, NOW, TZ, random.Random(s))
            assert len(t.split()) <= 55 and t.endswith("?") and "http" not in t


def test_regenerate_texts_adds_the_niche_pain(conn, listas, cat, tset, rules):
    _stock(conn, listas, cat)
    fq.build_queue(conn, tset, rules, NOW, size=6, followup_days=3, rng=random.Random(1), nichos=cat)
    fq.regenerate_texts(conn, tset, rules, NOW, rng=random.Random(3), nichos=cat)
    texts = [r["texto"] for r in conn.execute("SELECT texto FROM fila")]
    assert len(texts) == 6 and all("{" not in t for t in texts)


# ---------- buscas por nicho ----------

def test_batch_runs_the_niche_terms_over_the_cities_and_skips_done(conn, cat):
    seen = []

    def search(q):
        seen.append(q)
        return ImportResult(lidos=1, novos=1, validos=1)

    cities = ("Itaí SP", "Avaré SP")
    first = lote.run_nicho(conn, cat, "clinicas", cities, search, limit=3, out=lambda s: None)
    assert len(first) == 3 and seen[0] == "dentista em Itaí SP"
    lote.run_nicho(conn, cat, "clinicas", cities, search, limit=3, out=lambda s: None)
    assert len(seen) == len(set(seen)) == 6                                      # nada repetido
    assert len(lote.pending_nicho(conn, cat, "clinicas", cities)) == len(cat.itens["clinicas"].buscas) * 2 - 6


# ---------- reclassificar o que já existia ----------

def test_backfill_tags_old_leads_and_recomputes_human_replies(conn, listas, cat):
    from prospector.nichos import reclassificar
    import_rows(conn, [_row("Odonto Sorriso", "(14) 99100-0001", "Dentista", "c1"),
                       _row("Pizzaria Bella", "(14) 99100-0003", "Pizzaria", "p1")], "q", listas)   # sem catálogo
    with db.tx(conn):
        for lead in conn.execute("SELECT id, nome FROM leads").fetchall():
            db.update_lead(conn, lead["id"], status=db.RESPONDEU, respondeu_em=db.iso(NOW))
            texto = "Olá, seja bem-vindo ao nosso atendimento!" if lead["nome"] == "Pizzaria Bella" else "Pode mandar"
            conn.execute("INSERT INTO respostas (lead_id, telefone, texto, recebido_em) VALUES (?, 'x', ?, ?)",
                         (lead["id"], texto, db.iso(NOW)))
    tagged, humans = reclassificar(conn, cat)
    got = {r["nome"]: (r["nicho"], r["resposta_humana"]) for r in conn.execute("SELECT * FROM leads")}
    assert got == {"Odonto Sorriso": ("clinicas", 1), "Pizzaria Bella": ("alimentacao", 0)}
    assert (tagged, humans) == (2, 1)
    assert reclassificar(conn, cat) == (0, 1)                                    # idempotente


def test_niche_report_lists_stock_sent_human_replies(conn, listas, cat):
    _contacted(conn, listas, cat)
    import_rows(conn, [_row("Odonto Novo", "(14) 99100-0009", "Dentista", "c9")], "q", listas, nichos=cat)
    _reply(conn, "5514991000002", "Pode mandar o exemplo")
    rep = {r["nicho"]: r for r in metrics.nicho_report(conn)}
    assert rep["clinicas"]["enviadas"] == 1 and rep["clinicas"]["estoque"] == 1 and rep["clinicas"]["humanas"] == 0
    assert rep["mecanicos"]["humanas"] == 1 and rep["mecanicos"]["taxa"] == 1.0


# ---------- o que é "gente de verdade" ----------

@pytest.mark.parametrize("text, human", [
    ("Pode", True), ("Boa tarde. Veja nesse contato", True), ("Rafael Garcia", True), ("quanto custa?", True),
    ("Não tenho interesse", True), ("Yes please send it", True), ("Sí, mándame el ejemplo", True),
    ("[text]", False), ("[audio]", False), ("", False), ("👍", True),
    ("Boa Noite!!😊", False), ("Oi", False), ("Hello!", False),
    ("Sejam bem vindos!!", False),
    ("Olá, LUCAS, seja bem-vindo ao nosso autoatendimento.\nSegue o link de nosso cardápio! https://eatfood.app/x", False),
    ("Faça seu pedido https://delivery.marcomapa.com/x", False),
    ("Agradecemos sua mensagem. Responderemos em sequência", False),
    ("Nosso atendimento é exclusivamente presencial garantindo a qualidade", False),
    ("PENSOU NO DELIVERY ➡️ PALÁCIO DO SORVETE EXPRESS 14996302122 loja 2. Na loja 1 NÃO FAZEMOS ENTREGAS", False),
    ("Bem vindo ao atendimento Tio Toko, faça seu pedido utilizando o link", False),
])
def test_is_human_reply(text, human):
    from prospector.replies import is_human_reply
    assert is_human_reply(text) is human
