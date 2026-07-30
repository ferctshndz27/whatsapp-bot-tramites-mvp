import pytest
from sqlmodel import Session, SQLModel, create_engine

import app.db as db


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch):
    """Cada test corre contra su propia base en memoria: el catálogo ahora
    vive en SQLite, así que sin esto los tests que no parchan el engine
    escribirían en la base local real."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)
    yield


@pytest.fixture(autouse=True)
def _stripe_en_modo_prueba(monkeypatch):
    """Ninguna prueba puede depender del .env del desarrollador.

    `app.config` hace load_dotenv() al importarse, así que si en el .env vive
    una llave REAL (y una `rk_live_` restringida lo es), el aplicador
    clasifica los eventos sintéticos —que van con livemode=False— como "modo
    no coincide" y medio módulo de cobranza falla por una razón que no tiene
    nada que ver con el código. Pasó de verdad.

    Las pruebas que necesitan otro modo lo sobreescriben después y ganan.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_suite")


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
