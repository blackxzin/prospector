import random

import pytest

from prospector import db
from prospector import fila as fq
from prospector.config import ROOT
from prospector.importer import import_rows
from prospector.indicacoes import contact_name, numbers_in, register_referral
from prospector.messages import TemplateSet
from prospector.replies import Inbound, handle_inbound, parse_inbound

from .conftest import csv_row, local_dt

NOW = local_dt(2026, 10, 21, 10)
OWN = "5514991234567"
VCARD = ("Rafael Garcia\nBEGIN:VCARD\nVERSION:3.0\nN:Garcia;Rafael;;;\nFN:Rafael Garcia\n"
         "TEL;type=CELL;waid=5514997123456:+55 14 99712-3456\nEND:VCARD")


@pytest.mark.parametrize("text, expected", [
    ("o número dele é (14) 99712-3456", ["5514997123456"]),
    ("chama ele: 14 99712 3456", ["5514997123456"]),
    ("+55 14 99712-3456", ["5514997123456"]),
    ("14997123456", ["5514997123456"]),
    (VCARD, ["5514997123456"]),
    ("fixo é (14) 3761-1929", []),                                   # fixo não serve
    ("abrimos às 18 e fechamos às 23", []), ("Pode mandar", []), ("", []),
    ("ele: 14 99712-3456 ou 14 99888-1111", ["5514997123456", "5514998881111"]),
])
def test_numbers_in(text, expected):
    assert numbers_in(text) == expected


def test_contact_name():
    assert contact_name(VCARD) == "Rafael Garcia"
    assert contact_name("Rafael Garcia") == "Rafael Garcia"            # cartão sem vCard: só o nome
    assert contact_name("o número dele é 14 99712-3456") is None


def _lead(conn, listas):
    import_rows(conn, [csv_row(title="Clube da Esquina Botequim", phone="(14) 99123-4567", category="Restaurante",
                               link="l1")], "q", listas)
    lead = conn.execute("SELECT * FROM leads").fetchone()
    with db.tx(conn):
        db.update_lead(conn, lead["id"], status=db.ENVIADO, enviado_em=db.iso(local_dt(2026, 10, 20, 10)),
                       nicho="alimentacao")
    return lead["id"]


def _handle(conn, text, kind="TEXT", notes=None):
    notes = [] if notes is None else notes
    msg = Inbound(numbers=frozenset({OWN}), jids=frozenset({f"{OWN}@s.whatsapp.net"}), text=text, push_name="",
                  msg_id=None, kind=kind)
    out = handle_inbound(conn, msg, now=NOW, in_business_hours=True, notify_fn=lambda t, b, u: notes.append((t, b)))
    return out, notes


def test_number_in_a_human_reply_becomes_a_referred_lead(conn, listas):
    lead = _lead(conn, listas)
    out, notes = _handle(conn, "Fala com o Rafael, o número dele é (14) 99712-3456")
    new = conn.execute("SELECT * FROM leads WHERE telefone = '5514997123456'").fetchone()
    assert new and new["indicado_por"] == lead and new["status"] == db.NOVO
    assert (new["pais"], new["nicho"], new["cidade"]) == ("BR", "alimentacao", "Itaí")
    assert new["presenca_checada_em"]                                       # não passa pela busca de presença
    assert any("indicou" in t.lower() for t, _ in notes)


def test_vcard_gives_number_and_name(conn, listas):
    _lead(conn, listas)
    _handle(conn, VCARD, kind="CONTACT")
    new = conn.execute("SELECT * FROM leads WHERE indicado_por IS NOT NULL").fetchone()
    assert new["telefone"] == "5514997123456" and new["contato_nome"] == "Rafael Garcia"
    assert "Rafael Garcia" in new["nome"] and "Clube da Esquina" in new["nome"]


def test_contact_card_without_number_asks_you_to_register_it(conn, listas):
    lead = _lead(conn, listas)
    _, notes = _handle(conn, "Rafael Garcia", kind="CONTACT")
    assert conn.execute("SELECT COUNT(*) FROM leads WHERE indicado_por IS NOT NULL").fetchone()[0] == 0
    title, body = next(n for n in notes if "cartão de contato" in n[0].lower())
    assert "prospector indicado" in body and f"--de {lead}" in body and '--nome "Rafael Garcia"' in body


@pytest.mark.parametrize("text", [
    "Olá, seja bem-vindo ao nosso atendimento! Pedidos: 14 99712-3456",      # robô de atendimento
    "Pare de mandar mensagem, meu número 14 99712-3456",                      # pediu para parar
    "O meu número é 14 99123-4567",                                           # o próprio número do negócio
])
def test_no_referral_from_bots_opt_outs_or_own_number(conn, listas, text):
    _lead(conn, listas)
    _handle(conn, text)
    assert conn.execute("SELECT COUNT(*) FROM leads WHERE indicado_por IS NOT NULL").fetchone()[0] == 0


def test_existing_blocked_or_already_contacted_numbers_are_not_duplicated(conn, listas):
    lead = _lead(conn, listas)
    import_rows(conn, [csv_row(title="Outro", phone="(14) 99712-3456", link="o1")], "q", listas)
    _handle(conn, "fala com (14) 99712-3456")
    assert conn.execute("SELECT COUNT(*) FROM leads WHERE telefone = '5514997123456'").fetchone()[0] == 1
    db.block(conn, "5514998881111", "pediu para parar")
    _handle(conn, "fala com (14) 99888-1111")
    assert conn.execute("SELECT COUNT(*) FROM leads WHERE telefone = '5514998881111'").fetchone()[0] == 0


def test_same_number_twice_creates_one_lead(conn, listas):
    _lead(conn, listas)
    _handle(conn, "(14) 99712-3456")
    _handle(conn, "(14) 99712-3456 é o dele")
    assert conn.execute("SELECT COUNT(*) FROM leads WHERE telefone = '5514997123456'").fetchone()[0] == 1


def test_manual_registration(conn, listas):
    lead = _lead(conn, listas)
    ids = register_referral(conn, lead, "14 99712-3456", nome="Rafael", now=NOW)
    assert len(ids) == 1
    assert register_referral(conn, lead, "14 99712-3456", nome="Rafael", now=NOW) == []   # já existe
    with pytest.raises(ValueError, match="celular"):
        register_referral(conn, lead, "(14) 3761-1929", nome=None, now=NOW)


# ---------- fila e mensagem ----------

def test_referred_lead_goes_first_with_its_own_text(conn, listas, rules):
    tset = TemplateSet.load(ROOT / "templates.yaml")
    lead = _lead(conn, listas)
    import_rows(conn, [csv_row(title=f"Pizzaria {i}", phone=f"(14) 991{i:02d}-00{i:02d}", link=f"z{i}",
                               review_count="900") for i in range(3)], "q", listas)
    _handle(conn, VCARD, kind="CONTACT")
    fq.build_queue(conn, tset, rules, NOW, size=2, followup_days=3, rng=random.Random(1))
    rows = conn.execute("SELECT l.indicado_por, f.texto FROM fila f JOIN leads l ON l.id = f.lead_id "
                        "ORDER BY f.id").fetchall()
    assert rows[0]["indicado_por"] == lead                                  # indicado antes dos demais
    texto = rows[0]["texto"]
    assert "Clube da Esquina Botequim" in texto and "Rafael" in texto and "Lucas" in texto and "http" not in texto
    assert len(texto.split()) <= 50 and texto.endswith("?")


def test_referral_text_without_contact_name_is_still_clean(conn, listas, rules):
    tset = TemplateSet.load(ROOT / "templates.yaml")
    lead = _lead(conn, listas)
    register_referral(conn, lead, "14 99712-3456", nome=None, now=NOW)
    for seed in range(15):
        conn.execute("DELETE FROM fila")
        conn.execute("UPDATE leads SET status = 'novo' WHERE indicado_por IS NOT NULL")
        conn.commit()
        fq.build_queue(conn, tset, rules, NOW, size=1, followup_days=3, rng=random.Random(seed))
        texto = conn.execute("SELECT texto FROM fila").fetchone()[0]
        assert "{" not in texto and "  " not in texto and "None" not in texto and ",!" not in texto
