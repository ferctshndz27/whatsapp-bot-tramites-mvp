"""Navegación con contexto: el botón «volver» de los detalles.

REGLA DE NEGOCIO: la operadora vive en listas filtradas y paginadas (folios,
Bandeja). Entrar a un detalle y regresar NO debe tirarla a la página 1 sin
filtros — el enlace del detalle lleva `?volver=<url de la lista>` y el botón
de regreso lo usa.

REGLA DE SEGURIDAD: `volver` viaja en la URL, así que cualquiera puede
fabricarlo. Solo se aceptan rutas internas del panel (empiezan con «/» y no
con «//»): un `volver=https://evil.com` sería un open redirect servido por
nosotros mismos.
"""

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

import app.db as db
from app.config import settings
from app.main import app
from app.models import Conversation, Direction, Message, Ticket, TicketStatus, User
from app.utils import ruta_interna

PHONE = "525500000000"
CLAVE = "clave-de-prueba"
# rounds=4: con el costo por defecto (12) cada login del test costaría ~0.3 s.
HASH_CLAVE = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()


@pytest.fixture(autouse=True)
def _base_compartida(monkeypatch):
    """Engine con StaticPool para que el hilo del TestClient vea la misma base
    (mismo motivo documentado en tests/test_clients_admin.py)."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "geoip_enabled", False)
    yield


@pytest.fixture()
def http():
    """Cliente con sesión iniciada. base_url https: la cookie lleva Secure con
    la config de producción y sobre http el cliente la descartaría."""
    with Session(db.engine) as s:
        s.add(User(username="asesora", password_hash=HASH_CLAVE))
        s.add(
            Ticket(
                phone=PHONE,
                client_name="Ana Ruiz",
                category="Actas",
                service_id="sb-acta",
                service_name="Acta de nacimiento",
                price_info="$120",
                status=TicketStatus.NUEVO,
            )
        )
        s.add(Message(phone=PHONE, direction=Direction.IN, body="hola"))
        s.add(Conversation(phone=PHONE, state="menu_categoria"))
        s.commit()
    with TestClient(app, base_url="https://testserver") as cliente:
        r = cliente.post(
            "/login",
            data={"username": "asesora", "password": CLAVE},
            follow_redirects=False,
        )
        assert r.status_code == 303, "el login de la prueba dejó de funcionar"
        yield cliente


# ---------------------------------------------------------------------------
# ruta_interna: la validación que hace seguro aceptar `volver` desde la URL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "maliciosa",
    ["https://evil.com", "//evil.com", "/\\evil.com", "javascript:alert(1)", ""],
)
def test_ruta_interna_rechaza_destinos_externos(maliciosa):
    assert ruta_interna(maliciosa) == ""


@pytest.mark.parametrize("interna", ["/", "/?status=NUEVO&page=2", "/inbox?filter=esperando"])
def test_ruta_interna_acepta_rutas_del_panel(interna):
    assert ruta_interna(interna) == interna


# ---------------------------------------------------------------------------
# Folio: volver a la lista con filtros y página intactos
# ---------------------------------------------------------------------------


def test_el_volver_del_folio_conserva_filtros_y_pagina(http):
    r = http.get("/tickets/1?volver=/%3Fstatus%3DNUEVO%26page%3D2")

    assert r.status_code == 200
    assert 'href="/?status=NUEVO&amp;page=2"' in r.text


def test_un_volver_externo_en_el_folio_cae_a_la_lista(http):
    r = http.get("/tickets/1?volver=https://evil.com")

    assert r.status_code == 200
    assert "evil.com" not in r.text
    assert "Volver a solicitudes" in r.text


def test_la_lista_de_folios_propaga_su_url_en_los_enlaces_al_detalle(http):
    # El filtro de la lista usa el value del enum, en minúsculas.
    r = http.get("/?status=nuevo&page=1")

    assert r.status_code == 200
    # urlencode de "/?status=nuevo&page=1" — el enlace al detalle lo carga.
    # (el filtro urlencode de Jinja deja «/» sin codificar; no se asume.)
    assert "/tickets/1?volver=" in r.text
    assert "status%3Dnuevo%26page%3D1" in r.text


# ---------------------------------------------------------------------------
# Bandeja: volver al filtro activo
# ---------------------------------------------------------------------------


def test_el_volver_del_chat_conserva_el_filtro(http):
    r = http.get(f"/inbox/{PHONE}?volver=/inbox%3Ffilter%3Desperando")

    assert r.status_code == 200
    assert 'href="/inbox?filter=esperando"' in r.text


def test_un_volver_externo_en_el_chat_cae_a_la_bandeja(http):
    r = http.get(f"/inbox/{PHONE}?volver=//evil.com")

    assert r.status_code == 200
    assert "evil.com" not in r.text
    assert 'href="/inbox"' in r.text
