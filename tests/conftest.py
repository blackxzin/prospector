from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from prospector import db
from prospector.classify import Listas
from prospector.config import ROOT, load_settings
from prospector.messages import Templates
from prospector.schedule import Rules

TZ = ZoneInfo("America/Sao_Paulo")


def local_dt(*args: int) -> datetime:
    return datetime(*args, tzinfo=TZ)


@pytest.fixture
def settings(tmp_path: Path):
    return load_settings(env_file=tmp_path / "nao-existe.env", overrides={
        "DATA_DIR": str(tmp_path / "data"), "WAAKG_SESSION": "", "WAAKG_API_KEY": "", "WEBHOOK_SECRET": "",
        "NOTIFICAR": "0", "MAX_POR_DIA": "8", "MAX_POR_HORA": "3",
        "JANELAS": "09:00-11:30,14:00-17:30", "DIAS_UTEIS": "0,1,2,3,4",
    })


@pytest.fixture
def rules(settings) -> Rules:
    return Rules.from_settings(settings)


@pytest.fixture
def conn(tmp_path: Path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


@pytest.fixture
def listas() -> Listas:
    return Listas.load(ROOT / "listas.yaml")


@pytest.fixture
def templates() -> Templates:
    return Templates.load(ROOT / "templates.yaml")


def csv_row(**kw: str) -> dict[str, str]:
    base = {"title": "Pizzaria Bella", "category": "Pizzaria", "address": "R. A, 10 - Centro, Itaí - SP, 18730-000",
            "phone": "(14) 99123-4567", "website": "", "emails": "", "review_rating": "4.7", "review_count": "120",
            "link": "https://www.google.com/maps/place/x", "complete_address": ""}
    return {**base, **kw}
