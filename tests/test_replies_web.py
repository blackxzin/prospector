import hashlib
import hmac
import json

import pytest

from prospector import db
from prospector.app import App
from prospector.importer import import_rows
from prospector.replies import handle_inbound, is_interested, is_opt_out, parse_inbound, verify_signature
from prospector.web import create_app

from .conftest import csv_row, local_dt

NUMBER = "5514991234567"


@pytest.mark.parametrize("text", [
    "Não", "não, obrigado", "nao tenho interesse", "Pare de mandar mensagem", "sair", "me remove da lista",
    "Sem interesse", "NÃO QUERO", "isso é spam", "para de me mandar isso", "Obrigado, mas não",
])
def test_opt_out_detected(text):
    assert is_opt_out(text)


@pytest.mark.parametrize("text", [
    "Pode mandar sim", "quanto custa?", "Oi, tudo bem? Manda o exemplo", "manda para mim", "ok",
])
def test_not_opt_out(text):
    assert not is_opt_out(text)


def test_interest():
    assert is_interested("Pode mandar!")
    assert not is_interested("não tenho interesse")


def _payload(text, jid=f"{NUMBER}@s.whatsapp.net", **extra):
    data = {"key": {"remoteJid": jid, "fromMe": False, "id": "X"}, "from": jid, "isGroup": False,
            "type": "TEXT", "content": text, "pushName": "Zé", **extra}
    return {"event": "message.received", "sessionId": "s", "data": data}


def test_parse_inbound_ignores_groups_and_own_messages():
    assert parse_inbound(_payload("oi"))
    assert parse_inbound(_payload("oi", isGroup=True)) is None
    p = _payload("oi")
    p["data"]["key"]["fromMe"] = True
    assert parse_inbound(p) is None
    assert parse_inbound({"event": "message.sent", "data": {}}) is None


def test_signature():
    body = b'{"a":1}'
    sig = "sha256=" + hmac.new(b"segredo", body, hashlib.sha256).hexdigest()
    assert verify_signature("segredo", body, sig)
    assert not verify_signature("segredo", body, "sha256=00")
    assert not verify_signature("segredo", body, None)


def _contacted_lead(conn, listas, phone="(14) 99123-4567"):
    import_rows(conn, [csv_row(phone=phone)], "q", listas)
    lead = conn.execute("SELECT * FROM leads").fetchone()
    with db.tx(conn):
        db.update_lead(conn, lead["id"], status=db.ENVIADO, enviado_em=db.iso(local_dt(2026, 10, 19, 10)))
    return lead


def _handle(conn, text, in_hours=True, jid=f"{NUMBER}@s.whatsapp.net"):
    notes = []
    out = handle_inbound(conn, parse_inbound(_payload(text, jid=jid)), now=local_dt(2026, 10, 19, 20),
                         in_business_hours=in_hours, notify_fn=lambda t, b, u: notes.append((t, b)))
    return out, notes


def test_reply_marks_respondeu_and_notifies(conn, listas):
    _contacted_lead(conn, listas)
    out, notes = _handle(conn, "Oi, quem é?")
    assert out.status == db.RESPONDEU and notes
    row = conn.execute("SELECT * FROM leads").fetchone()
    assert row["resposta"] == "Oi, quem é?" and row["respondeu_em"]


def test_opt_out_blocks_forever(conn, listas):
    _contacted_lead(conn, listas)
    out, _ = _handle(conn, "não tenho interesse")
    assert out.status == db.NAO_QUER
    assert db.is_blocked(conn, {NUMBER})
    # mesmo que responda depois de forma positiva, continua bloqueado
    out2, _ = _handle(conn, "pode mandar")
    assert out2.status == db.NAO_QUER


def test_reply_matches_jid_without_ninth_digit(conn, listas):
    _contacted_lead(conn, listas)
    out, _ = _handle(conn, "oi", jid="551491234567@s.whatsapp.net")
    assert out.lead_id is not None


def test_auto_reply_out_of_hours_only_once(conn, listas):
    _contacted_lead(conn, listas)
    first, _ = _handle(conn, "oi", in_hours=False)
    second, _ = _handle(conn, "alô?", in_hours=False)
    assert first.auto_reply is True and second.auto_reply is False


def test_no_auto_reply_in_hours_or_for_opt_out(conn, listas):
    _contacted_lead(conn, listas)
    assert _handle(conn, "oi", in_hours=True)[0].auto_reply is False
    assert _handle(conn, "pare", in_hours=False)[0].auto_reply is False


def test_unknown_number_ignored(conn, listas):
    _contacted_lead(conn, listas)
    out, notes = _handle(conn, "oi", jid="5511988887777@s.whatsapp.net")
    assert out.lead_id is None and not notes


# ---------- painel / webhook ----------

@pytest.fixture
def web(settings, conn, listas):
    from dataclasses import replace
    app = App(replace(settings, webhook_secret="segredo"))
    app.__dict__["conn"] = conn
    flask_app = create_app(app)
    flask_app.config["TESTING"] = True
    return flask_app.test_client(), app


def test_webhook_requires_signature(web, listas):
    client, app = web
    _contacted_lead(app.conn, listas)
    body = json.dumps(_payload("não")).encode()
    assert client.post("/webhook", data=body, content_type="application/json").status_code == 401
    sig = "sha256=" + hmac.new(b"segredo", body, hashlib.sha256).hexdigest()
    resp = client.post("/webhook", data=body, content_type="application/json",
                       headers={"X-Webhook-Signature": sig})
    assert resp.status_code == 200
    assert app.conn.execute("SELECT status FROM leads").fetchone()["status"] == db.NAO_QUER


def test_webhook_connection_drop_stops_sending(web):
    client, app = web
    body = json.dumps({"event": "connection.update", "data": {"status": "LOGGED_OUT"}}).encode()
    sig = "sha256=" + hmac.new(b"segredo", body, hashlib.sha256).hexdigest()
    client.post("/webhook", data=body, content_type="application/json", headers={"X-Webhook-Signature": sig})
    assert "LOGGED_OUT" in db.stop_reason(app.conn)


def test_panel_pages_and_export(web, listas):
    client, app = web
    import_rows(app.conn, [csv_row(title="=cmd|' /C calc'!A0")], "pizzaria em Itaí SP", listas)
    page = client.get("/leads")
    assert page.status_code == 200 and "Pizzaria" in page.get_data(as_text=True)
    assert client.get("/").status_code == 200
    assert client.get("/fila").status_code == 200
    assert client.get("/envios").status_code == 200
    resp = client.post("/fila/gerar")
    assert resp.status_code == 302
    assert client.post("/fila/aprovar-todos").status_code == 302
    assert app.conn.execute("SELECT COUNT(*) FROM fila WHERE status = 'aprovado'").fetchone()[0] == 1
    csv_text = client.get("/export.csv?busca=pizzaria em Itaí SP").get_data(as_text=True)
    assert "'=cmd" in csv_text


def test_panel_escapes_html(web, listas):
    client, app = web
    import_rows(app.conn, [csv_row(title="<script>alert(1)</script>")], "q", listas)
    html = client.get("/leads").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html


def test_panel_rejects_cross_origin_post(web, listas):
    client, app = web
    import_rows(app.conn, [csv_row()], "q", listas)
    resp = client.post("/leads/1/status", data={"status": "interessado"}, headers={"Origin": "http://evil.com"})
    assert resp.status_code == 403


# ---------- projetos / financeiro no painel ----------

def test_register_project_from_lead_flow(web, listas):
    client, app = web
    import_rows(app.conn, [csv_row()], "pizzaria em Itaí SP", listas)
    form = client.get("/projetos/novo?lead=1").get_data(as_text=True)
    preco = app.finance.precos["site"].valor  # pré-preenchido com o preço do financeiro.yaml
    assert "Pizzaria Bella" in form and f'value="{preco // 100},{preco % 100:02d}"' in form
    resp = client.post("/projetos", data={
        "lead_id": "1", "cliente": "Pizzaria Bella", "tipo": "site", "valor": "650,00",
        "fechado_em": "2026-10-19", "prazo": "2026-10-26", "entrada": "50", "mensalidade": "0", "custo": "60"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/projetos/1")
    assert db.get_lead(app.conn, 1)["status"] == db.CLIENTE
    page = client.get("/projetos/1").get_data(as_text=True)
    assert "R$ 650,00" in page and "Entrada" in page
    pay = app.conn.execute("SELECT id FROM pagamentos ORDER BY id").fetchone()["id"]
    client.post(f"/pagamentos/{pay}/toggle", data={"volta": "1"})
    assert app.conn.execute("SELECT pago_em FROM pagamentos WHERE id = ?", (pay,)).fetchone()["pago_em"]
    client.post("/despesas", data={"projeto_id": "1", "descricao": "domínio", "categoria": "dominio",
                                   "valor": "40", "data": "2026-10-19"})
    assert "R$ 40,00" in client.get("/projetos/1").get_data(as_text=True)
    for url in ("/", "/projetos", "/financeiro"):
        assert client.get(url).status_code == 200


def test_invalid_money_shows_error_not_crash(web):
    client, app = web
    resp = client.post("/projetos", data={"cliente": "X", "tipo": "site", "valor": "abc", "fechado_em": "2026-10-19"},
                       follow_redirects=True)
    assert resp.status_code == 200 and "Não deu para registrar" in resp.get_data(as_text=True)
    assert app.conn.execute("SELECT COUNT(*) FROM projetos").fetchone()[0] == 0


def test_security_headers(web):
    client, _ = web
    resp = client.get("/leads")
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "script-src 'none'" in resp.headers["Content-Security-Policy"]


AUTO_TEXTS = [
    "Olá! Que bom falar com o Rancho das Pedras 🧡  Seu interesse é: 1️⃣Hospedagem  2️⃣Eventos",
    "Olá! Obrigado pelo seu contato! ✨ Estamos em atendimento no momento, mas responderemos em breve.",
    "Olá ! Tudo bem ? 😊\n\nNosso horário de atendimento é de segunda a domingo, das 13 às 22hs*. "
    "Não estaremos realizando delivery(entregas) Como podemos te atender ?",
    "Olá LUCAS, seja bem-vindo ao nosso autoatendimento.\nSegue o link de nosso cardápio!\nhttps://eatfood.app/tk7eq1ad",
    "‎Gordinho lanches Avare agradece seu contato. Como podemos ajudar?",
    "Olá, seja bem vindo (a) ao nosso auto atendimento. Para fazer seu pedido, acesso o link do cardápio",
]


@pytest.mark.parametrize("text", AUTO_TEXTS)
def test_business_auto_reply_is_not_an_opt_out(conn, listas, text):
    from prospector.replies import is_auto_reply
    lead = _contacted_lead(conn, listas)
    assert is_auto_reply(text)
    out, notes = _handle(conn, text)
    row = db.get_lead(conn, lead["id"])
    assert out.status == db.RESPONDEU and row["status"] == db.RESPONDEU
    assert not db.is_blocked(conn, {NUMBER}) and notes == []  # sem bloqueio e sem alarme


def test_human_replies_are_not_taken_for_auto_replies():
    from prospector.replies import is_auto_reply
    for text in ["Pode", "Não", "não tenho interesse", "quanto custa?", "Pode mandar o exemplo sim"]:
        assert not is_auto_reply(text)


def test_auto_reply_does_not_downgrade_interest(conn, listas):
    lead = _contacted_lead(conn, listas)
    _handle(conn, "Pode mandar o exemplo")
    _handle(conn, AUTO_TEXTS[2])
    assert db.get_lead(conn, lead["id"])["status"] == db.INTERESSADO


@pytest.mark.parametrize("text", [
    "PENSOU NO DELIVERY ➡️ PALÁCIO DO SORVETE EXPRESS 14996302122 loja 2. Na loja 1 aqui NÃO FAZEMOS ENTREGAS",
    "Nosso atendimento é exclusivamente presencial garantindo que cada produto seja preparado e servido "
    "com a qualidade e o frescor que você merece. Não fazemos entregas.",
    "Oi, tudo bem? Hoje não temos mais pizza de calabresa mas o resto do cardápio está disponível, quer ver?",
])
def test_long_message_with_a_nao_is_not_an_opt_out(text):
    assert not is_opt_out(text)


@pytest.mark.parametrize("text", [
    "Olá, boa tarde, nosso time já viu a mensagem mas pare de mandar por favor, obrigado e bom trabalho a vocês",
    "Obrigado mas não tenho interesse em site nenhum, o meu movimento está bom do jeito que está hoje",
])
def test_long_explicit_opt_out_still_counts(text):
    assert is_opt_out(text)


# ---------- vários idiomas ----------

@pytest.mark.parametrize("text", [
    "STOP", "Please stop messaging me", "Not interested, thanks", "No thanks", "Don't contact me again", "unsubscribe",
    "No me interesa, gracias", "No gracias", "Dejen de escribirme", "no",
    "Pas intéressé, merci", "Non merci", "Arrêtez de m'écrire", "non",
    "Kein Interesse", "Nein danke", "Bitte aufhören", "nein",
    "Non mi interessa", "No grazie", "Smettila di scrivermi", "PARAR",
])
def test_opt_out_in_every_language(text):
    assert is_opt_out(text)


@pytest.mark.parametrize("text", [
    "Yes please send it", "Sure, how much is it?", "Interested, tell me more", "Sí, mándame el ejemplo",
    "Claro, ¿cuánto cuesta?", "Oui, envoyez-moi l'exemple", "Combien ça coûte ?", "Ja, gerne",
    "Wie viel kostet das?", "Sì, mi interessa", "Quanto costa?",
])
def test_interest_in_every_language(text):
    assert is_interested(text) and not is_opt_out(text)


@pytest.mark.parametrize("text", [
    "Thank you for contacting Joe's Pizza! We will get back to you shortly. Our opening hours are 9-5.",
    "Gracias por contactar con nosotros. Nuestro horario de atención es de 9 a 14. No abrimos los domingos.",
    "Merci de nous avoir contactés ! Nos horaires d'ouverture : 9h-18h. Nous ne livrons pas.",
    "Vielen Dank für Ihre Nachricht. Unsere Öffnungszeiten sind 9-18 Uhr. Wir liefern nicht.",
    "Grazie per averci contattato! Il nostro orario di apertura è 9-18. Non facciamo consegne.",
])
def test_foreign_auto_replies_are_not_opt_outs(conn, listas, text):
    from prospector.replies import is_auto_reply
    lead = _contacted_lead(conn, listas)
    assert is_auto_reply(text) and not is_opt_out(text.split(".")[0])
    out, notes = _handle(conn, text)
    assert out.status == db.RESPONDEU and not db.is_blocked(conn, {NUMBER}) and notes == []


def test_ambiguous_no_in_a_sentence_is_not_an_opt_out():
    assert not is_opt_out("No momento estamos fechados, voltamos às 18h")
    assert not is_opt_out("Non abbiamo ancora un sito ma ci stiamo pensando e ci può interessare molto")
