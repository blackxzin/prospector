import pytest

from prospector.classify import SEM_SITE, SO_SOCIAL, TEM_SITE, classify_offer, is_franchise


@pytest.mark.parametrize("site,expected", [
    ("", SEM_SITE),
    (None, SEM_SITE),
    ("   ", SEM_SITE),
    ("https://www.instagram.com/pizzariabella", SO_SOCIAL),
    ("instagram.com/pizzariabella", SO_SOCIAL),
    ("http://facebook.com/bella", SO_SOCIAL),
    ("https://m.facebook.com/bella", SO_SOCIAL),
    ("https://linktr.ee/bella", SO_SOCIAL),
    ("https://wa.me/5514991234567", SO_SOCIAL),
    ("https://api.whatsapp.com/send?phone=55", SO_SOCIAL),
    ("https://www.ifood.com.br/delivery/itai-sp/bella", SO_SOCIAL),
    ("https://pizzariabella.business.site/", SO_SOCIAL),
    ("https://www.pizzariabella.com.br", TEM_SITE),
    ("pizzariabella.com.br", TEM_SITE),
    ("https://notinstagram.com", TEM_SITE),   # não confunde sufixo de domínio
])
def test_classify_offer(site, expected, listas):
    assert classify_offer(site, listas.dominios_social) == expected


@pytest.mark.parametrize("name", [
    "Smart Fit - Itaí", "SMARTFIT Centro", "Academia Smart-Fit", "McDonald's", "Mc Donald's Avaré",
    "Subway", "Subway Itaí Shopping", "Skyfit Academia", "Bluefit", "Habib's",
])
def test_franchise_detected(name, listas):
    assert is_franchise(name, listas.franquias)


@pytest.mark.parametrize("name", [
    "Pizzaria Bella", "Academia Fit Center", "Smart Pizza", "Subwayy Lanches", "Shopee Point",
    "Lanchonete do Bob", "Mc Lanches",
])
def test_not_franchise(name, listas):
    assert is_franchise(name, listas.franquias) is None
