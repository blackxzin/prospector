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
def test_first_message_is_short_personal_and_asks_who_to_talk_to(templates, offer):
    lead = {"nome": "Pizzaria Bella - Delivery", "nota": 4.7, "avaliacoes": 132, "categoria": "Pizzaria",
            "oferta": offer, "cidade": "Itaí"}
    for seed in range(20):
        text = templates.first_message(lead, NOW, TZ, random.Random(seed))
        assert text.endswith("?") and "Lucas" in text and "desenvolvedor" in text
        assert "http" not in text and "vercel" not in text          # sem link na 1ª mensagem
        assert len(text.split()) <= 50                              # curta
        assert "{" not in text and "  " not in text and "Delivery" not in text
        assert "de Itaí" in text                                    # gancho local


def test_rating_appears_only_when_it_is_good(templates):
    good = {"nome": "Bella", "nota": 4.7, "avaliacoes": 132, "categoria": "Pizzaria", "oferta": "sem_site"}
    texts = {templates.first_message(good, NOW, TZ, random.Random(s)) for s in range(40)}
    assert any("4,7" in t for t in texts)
    bad = {**good, "nota": 3.1, "avaliacoes": 4}
    assert all("3,1" not in templates.first_message(bad, NOW, TZ, random.Random(s)) for s in range(40))


def test_variants_are_drawn(templates):
    lead = {"nome": "Bella", "nota": None, "avaliacoes": None, "categoria": "Academia", "oferta": "tem_site"}
    assert len({templates.first_message(lead, NOW, TZ, random.Random(s)) for s in range(30)}) >= 3


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


def test_clean_name_drops_trailing_city():
    assert clean_name("Yaki Burguer Itai", "Itaí") == "Yaki Burguer"
    assert clean_name("Restaurante Zacota Avare", "Avaré") == "Restaurante Zacota"
    assert clean_name("Pizzaria Itaí", "Itaí") == "Pizzaria Itaí"  # sobraria uma palavra só
    assert clean_name("Bar do Zé", "Itaí") == "Bar do Zé"
    assert clean_name("Lanches Salto do Itararé", "Salto do Itararé") == "Lanches Salto do Itararé"


# ---------- idiomas ----------

from prospector.config import ROOT  # noqa: E402
from prospector.messages import TemplateSet  # noqa: E402

LANGS = ["en", "es", "fr", "de", "it", "pt-pt"]


@pytest.fixture(scope="module")
def tset():
    return TemplateSet.load(ROOT / "templates.yaml")


def test_all_shipped_languages_load(tset):
    assert set(LANGS) <= set(tset.languages) and "pt" in tset.languages


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("offer", ["sem_site", "so_social", "tem_site", "site_problema"])
def test_foreign_messages_are_short_clean_and_localized(tset, lang, offer):
    lead = {"nome": "Napoli Pizza", "nota": 4.6, "avaliacoes": 120, "categoria": "Pizza restaurant",
            "oferta": offer, "cidade": "Madrid"}
    tpl = tset.get(lang)
    for seed in range(20):
        text = tpl.first_message(lead, local_dt(2026, 10, 20, 10), TZ, random.Random(seed))
        assert "{" not in text and "  " not in text and len(text.split()) <= 60
        assert "Lucas" in text and text.endswith(tpl.data["optout"])      # opt-out explícito em toda abordagem
        assert "vercel.app" not in text and "R$" not in text and "http" not in text
        assert "aqui da região" not in text and ("Bom dia" not in text or lang == "pt-pt")
        assert "?" in text                                                # sempre pergunta algo


def test_greeting_and_decimal_follow_the_language(tset):
    lead = {"nome": "Joe's Pizza", "nota": 4.6, "avaliacoes": 120, "categoria": "Pizza", "oferta": "sem_site"}
    en = [tset.get("en").first_message(lead, local_dt(2026, 10, 20, 9), TZ, random.Random(i)) for i in range(60)]
    assert any("4.6" in t for t in en) and not any("4,6" in t for t in en)
    assert any("Good morning" in t for t in en) and not any("Good evening" in t for t in en)
    night = [tset.get("en").first_message(lead, local_dt(2026, 10, 20, 20), TZ, random.Random(i)) for i in range(60)]
    assert any("Good evening" in t for t in night) and not any("Good morning" in t for t in night)
    es = [tset.get("es").first_message(lead, local_dt(2026, 10, 20, 9), TZ, random.Random(i)) for i in range(60)]
    assert any("4,6" in t for t in es) and not any("4.6" in t for t in es)


def test_fix_greeting_works_per_language(tset):
    en = tset.get("en")
    afternoon = local_dt(2026, 10, 20, 15)
    assert en.fix_greeting("Good morning, Joe! Good morning", afternoon) == "Good afternoon, Joe! Good afternoon"
    assert tset.get("es").fix_greeting("Buenos días, Ana", afternoon) == "Buenas tardes, Ana"
    assert tset.get("de").fix_greeting("Guten Morgen, Herr Kaiser", local_dt(2026, 10, 20, 19)) == "Guten Abend, Herr Kaiser"
    assert fix_greeting("Bom dia, Zé", afternoon) == "Boa tarde, Zé"      # compatível com o uso antigo


def test_barbershop_gets_services_not_menu_in_every_language(tset):
    cats = {"en": "Barber shop", "es": "Barbería", "fr": "Barbier", "de": "Barbershop", "it": "Barbiere",
            "pt-pt": "Barbearia"}
    for lang, cat in cats.items():
        lead = {"nome": "Style", "categoria": cat, "oferta": "sem_site"}
        words = tset.get(lang)._vitrine(cat)
        assert words == tset.get(lang).data["vitrine"]["por_categoria"][0]["texto"], lang


def test_unknown_language_falls_back_to_portuguese(tset):
    assert tset.get("xx") is tset.get("pt") and tset.get(None) is tset.get("pt")


# ---------- fila por país ----------

from prospector.importer import import_rows as _import_rows  # noqa: E402
from prospector.mundo import Origem as _Origem  # noqa: E402


def _foreign_and_local(conn, listas):
    _import_rows(conn, [csv_row(title="Pizzería Napoli", phone="612 34 56 78", link="es1")], "q", listas,
                 _Origem("ES", "es", "Europe/Madrid", "Madrid"))
    _import_rows(conn, [csv_row(title="Pizzaria Bella", phone="(14) 99123-4567", link="br1")], "q", listas)


def test_queue_is_brazil_only_by_default_and_uses_language_when_asked(conn, listas, tset, rules):
    _foreign_and_local(conn, listas)
    now = local_dt(2026, 10, 20, 9)
    fq.build_queue(conn, tset, rules, now, size=5, followup_days=3, rng=random.Random(0), checar_presenca=None)
    only_br = [r["nome"] for r in conn.execute("SELECT l.nome FROM fila f JOIN leads l ON l.id = f.lead_id")]
    assert only_br == ["Pizzaria Bella"]
    fq.build_queue(conn, tset, rules, now, size=5, followup_days=3, rng=random.Random(0), pais=("ES",))
    texto = conn.execute("SELECT f.texto FROM fila f JOIN leads l ON l.id = f.lead_id WHERE l.pais = 'ES'").fetchone()[0]
    assert "Lucas" in texto and "STOP" in texto and "Bom dia" not in texto
    assert texto.startswith(("Buenos días", "Buenas tardes", "Buenas noches", "Hola"))


def test_greeting_in_queue_uses_the_lead_timezone(conn, listas, tset, rules):
    # 22h em Madrid (UTC+2) = 17h em São Paulo: para o lead é noite, apesar de ser tarde no Brasil
    _import_rows(conn, [csv_row(title="Café Luna", phone="612 34 56 79", link="es2")], "q", listas,
                 _Origem("ES", "es", "Europe/Madrid", "Madrid"))
    now = local_dt(2026, 10, 20, 17)
    fq.build_queue(conn, tset, rules, now, size=3, followup_days=3, rng=random.Random(5), pais=("ES",))
    texto = conn.execute("SELECT texto FROM fila").fetchone()[0]
    assert "Buenas tardes" not in texto
