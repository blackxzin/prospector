from prospector import db
from prospector.importer import dedup_key, extract_city, first_email, import_rows

from .conftest import csv_row


def test_dedup_key_prefers_phone_then_maps_link():
    assert dedup_key(csv_row(phone="(14) 99123-4567")) == "tel:5514991234567"
    assert dedup_key(csv_row(phone="+55 14 99123-4567")) == "tel:5514991234567"
    assert dedup_key(csv_row(phone="", link="https://maps/x")) == "maps:https://maps/x"
    assert dedup_key(csv_row(phone="", link="")) is None


def test_import_dedups_same_phone_in_different_formats(conn, listas):
    rows = [csv_row(), csv_row(title="Pizzaria Bella (filial)", phone="+55 14 99123-4567", link="other")]
    res = import_rows(conn, rows, "pizzaria em Itaí SP", listas)
    assert (res.novos, res.duplicados) == (1, 1)
    again = import_rows(conn, [csv_row()], "outra busca", listas)
    assert (again.novos, again.duplicados) == (0, 1)
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 1


def test_import_dedups_by_maps_link_without_phone(conn, listas):
    rows = [csv_row(phone="", link="https://maps/a"), csv_row(phone="", link="https://maps/a"),
            csv_row(phone="", link="https://maps/b")]
    res = import_rows(conn, rows, "q", listas)
    assert (res.novos, res.duplicados) == (2, 1)


def test_import_filters_and_classifies(conn, listas):
    rows = [
        csv_row(title="Pizzaria Boa", phone="(14) 99111-1111", website=""),
        csv_row(title="Pizzaria Insta", phone="(14) 99222-2222", website="https://instagram.com/x"),
        csv_row(title="Pizzaria Site", phone="(14) 99333-3333", website="https://pizzaria.com.br"),
        csv_row(title="Pizzaria Fixo", phone="(14) 3761-1234"),
        csv_row(title="Subway Itaí", phone="(14) 99444-4444"),
    ]
    res = import_rows(conn, rows, "q", listas)
    assert (res.novos, res.validos, res.descartados) == (5, 3, 2)
    got = {r["nome"]: (r["status"], r["oferta"], r["telefone"], r["motivo"])
           for r in conn.execute("SELECT * FROM leads")}
    assert got["Pizzaria Boa"] == (db.NOVO, "sem_site", "5514991111111", None)
    assert got["Pizzaria Insta"][:2] == (db.NOVO, "so_social")
    assert got["Pizzaria Site"][:2] == (db.NOVO, "tem_site")
    assert got["Pizzaria Fixo"][0] == db.DESCARTADO and got["Pizzaria Fixo"][3] == "sem celular"
    assert got["Subway Itaí"][0] == db.DESCARTADO and got["Subway Itaí"][3].startswith("franquia")


def test_import_marks_blocked_numbers(conn, listas):
    with db.tx(conn):
        db.block(conn, "5514991234567", "opt-out")
    import_rows(conn, [csv_row()], "q", listas)
    row = conn.execute("SELECT status, motivo FROM leads").fetchone()
    assert (row["status"], row["motivo"]) == (db.DESCARTADO, "bloqueado")


def test_import_parses_fields(conn, listas):
    import_rows(conn, [csv_row(emails="contato@bella.com.br, x@y.com", review_rating="4,5", review_count="33")],
                "pizzaria em Itaí SP", listas)
    row = conn.execute("SELECT * FROM leads").fetchone()
    assert row["email"] == "contato@bella.com.br"
    assert row["nota"] == 4.5 and row["avaliacoes"] == 33
    assert row["cidade"] == "Itaí"
    assert row["busca"] == "pizzaria em Itaí SP"


def test_extract_city_from_complete_address_json():
    assert extract_city({"complete_address": '{"city": "Avaré", "state": "SP"}', "address": ""}) == "Avaré"
    assert extract_city({"address": "Av. Brasil, 500 - Jardim, Itaí - SP, 18730-000"}) == "Itaí"
    assert extract_city({"address": "sem cidade"}) is None


def test_first_email():
    assert first_email('["a@b.com"]') == "a@b.com"
    assert first_email("") is None
