import json
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine

import app.db as db

# Volcado de solo lectura del catálogo de producción (docs/AUTOEVALUACION-V3.md §4).
# Se refresca cuando el catálogo vivo cambie de forma relevante: archivo nuevo con su
# fecha y esta constante apuntando a él.
CATALOGO_VIVO = Path(__file__).parent / "fixtures" / "catalogo-prod-2026-10-09.json"


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
    del desarrollador trae ANTHROPIC_API_KEY u OLLAMA_URL reales, las capas
    LLM se activarían solas (lentas, no deterministas y con costo). Las
    pruebas que ejercitan esas capas las configuran explícitamente y ganan."""
    from app.config import settings

    import app.nlu as nlu

    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "ollama_url", "")
    monkeypatch.setattr(settings, "ollama_token", "")
    # Y ningún hilo de precalentado suelto: cada test estrena catálogo (motor
    # nuevo), y sin esto cada primera lectura dispararía el aviso de cambio.
    monkeypatch.setattr(nlu, "PRECALENTADO_ACTIVO", False)


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


@pytest.fixture()
def catalogo_vivo(monkeypatch):
    """Siembra la base de la prueba con el catálogo de PRODUCCIÓN, no con la semilla.

    El panel edita sinónimos en vivo y `app/catalog.json` se queda atrás: en
    producción «NSS en PDF» trae "nss" y en la semilla no, así que una prueba de NLU
    contra la semilla aprueba frases que en producción se trocean. Devuelve el
    volcado para comparar contra él.

    Deja el singleton `catalog` como lo encontró: su caché vuelve al terminar y la
    base en memoria muere con `_isolated_db`.
    """
    from app.catalog import Catalog, catalog

    for atributo in ("_categories", "_service_by_id", "_category_by_id", "_contenido_anterior"):
        monkeypatch.setattr(catalog, atributo, getattr(catalog, atributo))
    volcado = json.loads(CATALOGO_VIVO.read_text(encoding="utf-8"))
    esperados = [s["id"] for c in volcado["categories"] for s in c["services"]]
    # Con las tablas vacías, el primer acceso importa el JSON que recibe Catalog.
    cargados = [s.id for c in Catalog(CATALOGO_VIVO).categories for s in c.services]
    assert cargados == esperados, (
        "la base de la prueba no quedó con el volcado de producción: ya traía un "
        "catálogo (algo leyó `catalog` antes que este fixture) o se importó otro JSON"
    )
    return volcado
