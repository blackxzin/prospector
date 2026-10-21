import pytest

from prospector import db, emails_site
from prospector.config import load_settings

from .conftest import csv_row
from prospector.importer import import_rows


def page(*parts: str) -> bytes:
    return ("<html><body>" + "".join(parts) + "</body></html>").encode()


def test_prefers_own_domain_over_free_and_ignores_agency():
    body = page('<a href="mailto:contato@pizzaria.com.br">x</a>', "dev: suporte@agenciaweb.com.br", "zap bella@gmail.com")
    assert emails_site.extract_emails(body, "www.pizzaria.com.br") == ["contato@pizzaria.com.br", "bella@gmail.com"]


def test_free_provider_used_when_no_own_domain():
    assert emails_site.extract_emails(page("fale: dono@hotmail.com"), "loja.com.br") == ["dono@hotmail.com"]


def test_ignores_images_and_placeholders():
    body = page("logo@2x.png seu@email.com x@sentry.io")
    assert emails_site.extract_emails(body, "loja.com.br") == []


def test_find_email_follows_contact_page():
    pages = {
        "http://loja.com.br/": (200, "http://loja.com.br/", page('<a href="/contato">Contato</a>'), 0.1),
        "http://loja.com.br/contato": (200, "http://loja.com.br/contato", page("oi@loja.com.br"), 0.1),
    }
    assert emails_site.find_email("http://loja.com.br/", fetch=lambda u: pages[u]) == "oi@loja.com.br"


def test_find_email_survives_errors():
    def boom(url):
        raise OSError("timeout")
    assert emails_site.find_email("http://x.com.br", fetch=boom) is None
    assert emails_site.find_email("http://x.com.br", fetch=lambda u: (404, u, b"", 0.1)) is None


def test_enrich_updates_only_own_sites_and_marks_searched(conn, listas):
    import_rows(conn, [csv_row(title="Loja A", phone="(14) 99111-1001", website="http://a.com.br", link="a"),
                       csv_row(title="Ana Doces", phone="(14) 99111-1002", website="https://instagram.com/ana", link="b")],
                "q", listas)
    calls: list[str] = []

    def fake(site):
        calls.append(site)
        return "oi@a.com.br"

    assert emails_site.enrich(conn, find=fake, out=lambda s: None) == (1, 1)
    assert calls == ["http://a.com.br"]
    assert conn.execute("SELECT email FROM leads WHERE nome = 'Loja A'").fetchone()[0] == "oi@a.com.br"
    assert emails_site.enrich(conn, find=fake, out=lambda s: None) == (0, 0)   # não procura duas vezes


@pytest.mark.parametrize("env", [{"MAX_POR_DIA": "40"}, {"MAX_POR_HORA": "10"}, {"INTERVALO_MIN_SEG": "180"},
                                 {"MAX_EMAIL_POR_DIA": "60"}])
def test_hard_limits_reject_risky_env(tmp_path, env):
    with pytest.raises(ValueError, match="teto de segurança"):
        load_settings(env_file=tmp_path / "x.env", overrides={"DATA_DIR": str(tmp_path), **env})


def test_hard_limits_can_be_lifted_on_purpose(tmp_path):
    s = load_settings(env_file=tmp_path / "x.env", overrides={"DATA_DIR": str(tmp_path), "MAX_POR_DIA": "40",
                                                              "LIMITES_LIVRES": "1"})
    assert s.max_per_day == 40
