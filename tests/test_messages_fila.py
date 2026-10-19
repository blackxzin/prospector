import random

import pytest

from prospector import db
from prospector import fila as fq
from prospector.importer import import_rows
from prospector.messages import TemplateError, Templates, clean_name, fix_greeting

from .conftest import TZ, csv_row, local_dt

NOW = local_dt(2026, 10, 19, 8, 30)


def test_templates_have_three_variants_per_offer(templates):
    for offer in ("sem_site", "so_social", "tem_site"):
        assert len(templates.data["primeira"][offer]) >= 3


@pytest.mark.parametrize("offer", ["sem_site", "so_social", "tem_site"])
def test_first_message_is_personal_and_ends_with_question(templates, offer):
    lead = {"nome": "Pizzaria Bella - Delivery", "nota": 4.7, "avaliacoes": 132, "categoria": "Pizzaria",
            "oferta": offer}
    for seed in range(10):
        text = templates.first_message(lead, NOW, TZ, random.Random(seed))
        assert text.endswith("?") and "exemplo" in text.rsplit(".", 1)[-1]
        assert "Lucas" in text and "desenvolvedores aqui da região" in text
        assert "{" not in text and "  " not in text
        assert "Delivery" not in text


def test_rating_used_only_when_good(templates):
    good = {"nome": "Bella", "nota": 4.7, "avaliacoes": 132, "categoria": "Pizzaria", "oferta": "sem_site"}
    texts = {templates.first_message(good, NOW, TZ, random.Random(s)) for s in range(20)}
    assert all("4,7" in t and "132" in t for t in texts)
    bad = {**good, "nota": 3.1, "avaliacoes": 4}
    assert all("3,1" not in templates.first_message(bad, NOW, TZ, random.Random(s)) for s in range(20))


def test_variants_are_drawn(templates):
    lead = {"nome": "Bella", "nota": None, "avaliacoes": None, "categoria": "Academia", "oferta": "tem_site"}
    assert len({templates.first_message(lead, NOW, TZ, random.Random(s)) for s in range(30)}) >= 3


def test_vitrine_by_category(templates):
    gym = {"nome": "Academia Força", "categoria": "Academia", "oferta": "sem_site", "nota": None, "avaliacoes": None}
    texts = [templates.first_message(gym, NOW, TZ, random.Random(s)) for s in range(10)]
    assert all("planos" in t for t in texts)


def test_invalid_template_variable_rejected(tmp_path):
    bad = tmp_path / "t.yaml"
    bad.write_text("remetente: x\nprimeira:\n  sem_site: ['{naoexiste}', a, b]\n  so_social: [a,b,c]\n"
                   "  tem_site: [a,b,c]\nfollowup: [a]\nfora_do_horario: [a]\nelogios_com_nota: [a]\n"
                   "elogios_sem_nota: [a]\n", encoding="utf-8")
    with pytest.raises(TemplateError):
        Templates.load(bad)


def test_clean_name():
    assert clean_name("Pizzaria Bella Itaí - Delivery | Centro") == "Pizzaria Bella Itaí"
    assert clean_name("PIZZARIA DO ZÉ") == "Pizzaria do Zé"
    assert clean_name("Bella (Itaí)") == "Bella"


def test_fix_greeting():
    afternoon = local_dt(2026, 10, 19, 15, 0)
    assert fix_greeting("Bom dia, Bella! Oi, bom dia!", afternoon) == "Boa tarde, Bella! Oi, boa tarde!"


# ---------- fila ----------

def _seed(conn, listas, n=3):
    rows = [csv_row(title=f"Pizzaria {i}", phone=f"(14) 9912{i}-000{i}", link=f"l{i}") for i in range(n)]
    import_rows(conn, rows, "pizzaria em Itaí SP", listas)


def test_build_queue_limits_size_and_marks_leads(conn, listas, templates, rules):
    _seed(conn, listas, 5)
    res = fq.build_queue(conn, templates, rules, NOW, size=3, followup_days=3, rng=random.Random(1))
    assert res.primeiras == 3
    assert conn.execute("SELECT COUNT(*) FROM leads WHERE status = 'na_fila'").fetchone()[0] == 3
    again = fq.build_queue(conn, templates, rules, NOW, size=3, followup_days=3)
    assert again.primeiras == 0  # fila já cheia


def test_approve_and_reject(conn, listas, templates, rules):
    _seed(conn, listas, 2)
    fq.build_queue(conn, templates, rules, NOW, size=5, followup_days=3)
    a, b = fq.pending_ids(conn)
    assert fq.approve(conn, [a], texto="Texto editado?") == 1
    assert fq.reject(conn, [b]) == 1
    items = {i["id"]: i for i in fq.list_items(conn, (fq.APROVADO, fq.REJEITADO))}
    assert items[a]["texto"] == "Texto editado?" and items[a]["status"] == fq.APROVADO
    assert items[b]["status"] == fq.REJEITADO
    lead_b = conn.execute("SELECT status FROM leads WHERE id = ?", (items[b]["lead_id"],)).fetchone()
    assert lead_b["status"] == db.REJEITADO


def test_queue_skips_blocked_and_already_contacted(conn, listas, templates, rules):
    _seed(conn, listas, 2)
    leads = conn.execute("SELECT * FROM leads ORDER BY id").fetchall()
    with db.tx(conn):
        db.block(conn, leads[0]["telefone"], "opt-out")
        db.log_send(conn, lead_id=None, fila_id=None, telefone=leads[1]["telefone"], tipo="primeira", texto="x",
                    dry_run=False, ok=True, http_status=200, resposta_api="")
    res = fq.build_queue(conn, templates, rules, NOW, size=5, followup_days=3)
    assert res.primeiras == 0


def test_followup_after_three_business_days_then_closed(conn, listas, templates, rules):
    _seed(conn, listas, 1)
    lead = conn.execute("SELECT * FROM leads").fetchone()
    sent_at = local_dt(2026, 10, 16, 10, 0)  # sexta
    with db.tx(conn):
        db.update_lead(conn, lead["id"], status=db.ENVIADO, enviado_em=db.iso(sent_at))
    # terça: só 2 dias úteis
    assert fq.build_queue(conn, templates, rules, local_dt(2026, 10, 20, 11, 0), size=5,
                          followup_days=3).followups == 0
    # quarta 10h: 3 dias úteis
    res = fq.build_queue(conn, templates, rules, local_dt(2026, 10, 21, 10, 0), size=5, followup_days=3)
    assert res.followups == 1
    # não duplica
    assert fq.build_queue(conn, templates, rules, local_dt(2026, 10, 22, 10, 0), size=5,
                          followup_days=3).followups == 0
    # depois de enviado o follow-up e 3 dias úteis sem resposta -> sem_resposta
    with db.tx(conn):
        db.update_lead(conn, lead["id"], status=db.FOLLOW_UP, followup_em=db.iso(local_dt(2026, 10, 21, 10, 5)))
    res = fq.build_queue(conn, templates, rules, local_dt(2026, 10, 26, 11, 0), size=5, followup_days=3)
    assert res.encerrados == 1
    assert conn.execute("SELECT status FROM leads").fetchone()["status"] == db.SEM_RESPOSTA


def test_no_followup_for_who_replied(conn, listas, templates, rules):
    _seed(conn, listas, 1)
    lead = conn.execute("SELECT * FROM leads").fetchone()
    with db.tx(conn):
        db.update_lead(conn, lead["id"], status=db.RESPONDEU, enviado_em=db.iso(local_dt(2026, 10, 1, 10)),
                       respondeu_em=db.iso(local_dt(2026, 10, 2, 10)))
    assert fq.build_queue(conn, templates, rules, NOW, size=5, followup_days=3).followups == 0
