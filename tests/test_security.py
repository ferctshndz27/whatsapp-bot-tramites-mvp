import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

import app.db as db
from app.config import settings
from app.main import app
from app.models import User

# Sin context manager: no dispara el lifespan/startup (que tocaría MSSQL y el
# backfill); los middlewares se registran al importar la app, así que /health
# ya devuelve las cabeceras de seguridad.
client = TestClient(app)


def test_security_headers_present_on_every_response():
    r = client.get("/health")

    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "SAMEORIGIN"
    assert "noindex" in r.headers["x-robots-tag"]  # el panel no se indexa
    assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert r.headers["strict-transport-security"].startswith("max-age=")


def test_csp_blocks_framing_and_foreign_origins():
    csp = client.get("/health").headers["content-security-policy"]

    assert "frame-ancestors 'none'" in csp  # anti-clickjacking
    assert "default-src 'self'" in csp


def test_el_esquema_openapi_no_se_publica():
    """REGLA: este servicio es un ERP interno detrás de un webhook público, y
    FastAPI abre `/docs`, `/redoc` y `/openapi.json` por omisión. Ningún guard
    del repo los cubre —los middlewares agregan cabeceras y contexto, no
    autenticación—, así que estuvieron sirviendo el mapa completo del panel a
    cualquiera: los nombres de las 98 rutas, sus métodos y sus parámetros.

    No es una fuga de datos, es el plano del edificio: `/oficinas`,
    `/agente/config`, `/system`, `/clients/upsert`. Se cierra en el
    constructor (`app/main.py`) porque un middleware que los bloqueara seguiría
    dependiendo de que nadie reordene los cuatro que ya hay.
    """
    for ruta in ("/docs", "/redoc", "/openapi.json"):
        r = client.get(ruta)
        assert r.status_code == 404, f"{ruta} sigue publicándose ({r.status_code})"


def test_las_rutas_del_panel_no_se_enumeran_desde_afuera():
    """El complemento del anterior: no basta con que `/openapi.json` dé 404 si
    el nombre de alguna ruta interna se cuela por otra vía. Lo que se afirma es
    que la app SIGUE teniendo sus rutas —no se rompió nada— y que ninguna
    respuesta pública las lista."""
    rutas = {r.path for r in app.routes if hasattr(r, "path")}
    assert "/oficinas" in rutas and "/system" in rutas, "la app perdió rutas, no era el objetivo"

    for ruta in ("/docs", "/openapi.json"):
        cuerpo = client.get(ruta).text
        assert "/oficinas" not in cuerpo and "/agente/config" not in cuerpo


# --- El middleware de nav: lo que NO debe consultar -------------------------
# Va con sesión REAL a propósito. `_nav_context` solo consulta si hay usuario
# en sesión, así que un TestClient anónimo pasa estas pruebas con o sin el
# arreglo: mediría nada. Es el patrón de tests/test_oficinas.py:30-56.

CLAVE_NAV = "clave-de-prueba"


@pytest.fixture()
def panel(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "geoip_enabled", False)
    with Session(engine) as s:
        s.add(User(username="jefa", is_admin=True,
                   password_hash=bcrypt.hashpw(CLAVE_NAV.encode(),
                                               bcrypt.gensalt(rounds=4)).decode()))
        s.commit()
    with TestClient(app, base_url="https://testserver") as c:
        r = c.post("/login", data={"username": "jefa", "password": CLAVE_NAV},
                   follow_redirects=False)
        assert r.status_code == 303, "el login de la prueba dejó de funcionar"
        yield c


def _sesiones_durante(cliente, ruta):
    """Cuántas veces se abre una sesión de base al pedir `ruta`."""
    n = 0
    original = db.get_session

    def contando(*args, **kwargs):
        nonlocal n
        n += 1
        return original(*args, **kwargs)

    db.get_session = contando
    try:
        respuesta = cliente.get(ruta)
    finally:
        db.get_session = original
    return n, respuesta


def test_un_estatico_no_consulta_la_base(panel):
    """REGLA: `_nav_context` corre en CADA petición y cuenta las propuestas
    pendientes para el badge del nav. Un estático no renderiza `base.html`, así
    que ese contador no lo lee nadie — y una sola pantalla del panel pide la
    página y detrás su CSS y su JS, repitiendo el mismo COUNT cada vez.

    Con sesión iniciada, que es cuando el middleware sí consulta: sin ella este
    test pasaría igual con el arreglo revertido.
    """
    consultas, r = _sesiones_durante(panel, "/static/css/base.css")

    assert r.status_code in (200, 404), "da igual si el archivo existe; importa que no consulte"
    assert consultas == 0, f"un estático abrió {consultas} sesión(es) contra la base"


def test_una_pagina_del_panel_si_alimenta_el_badge(panel):
    """El complemento: el guard de arriba no puede haber apagado el badge. Una
    página real sigue consultando — si esto llega a 0, el nav dejó de contar."""
    consultas, r = _sesiones_durante(panel, "/")

    assert r.status_code == 200
    assert consultas > 0, "la página dejó de alimentar el contador del nav"
