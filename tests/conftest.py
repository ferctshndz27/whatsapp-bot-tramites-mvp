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


@pytest.fixture(autouse=True)
def _nlu_sin_red(monkeypatch):
    """Ningún test sale a la red por la cadena NLU por accidente: si el .env
    del desarrollador trae ANTHROPIC_API_KEY, OPENROUTER_API_KEY u OLLAMA_URL
    reales, las capas LLM se activarían solas (lentas, no deterministas y con
    costo). Las pruebas que ejercitan esas capas las configuran
    explícitamente y ganan."""
    from app.config import settings

    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "ollama_url", "")
    monkeypatch.setattr(settings, "ollama_token", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "")


@pytest.fixture(autouse=True)
def _horario_de_oficina(monkeypatch):
    """Fija el reloj del negocio en lunes 10:00 (dentro de horario).

    Desde la fase 3 el bot agrega un aviso a sus promesas de atención cuando
    está cerrado (app/horario.py), así que sin fijar la hora el texto esperado
    dependería del momento en que corra la suite — un test verde a mediodía y
    rojo de madrugada, sin que nadie entienda por qué. Los tests del horario
    sobrescriben esto con la hora que quieran probar.
    """
    from datetime import datetime

    from app import horario
    from app.templating import BUSINESS_TZ

    monkeypatch.setattr(
        horario, "ahora", lambda: datetime(2026, 8, 24, 10, 0, tzinfo=BUSINESS_TZ)
    )


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
