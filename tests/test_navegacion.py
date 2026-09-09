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

import re

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

import app.db as db
from app.config import settings
from app.main import app
from app.models import Client, Conversation, Direction, Message, Ticket, TicketStatus, User
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
        s.add(Client(phone=PHONE, name="Ana Ruiz"))
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
# Chat: el regreso lleva a Solicitudes, que ya trae los filtros de conversación
# ---------------------------------------------------------------------------


def _sidebar(html: str) -> str:
    return re.search(r'<nav aria-label="Principal">.*?</nav>', html, re.S).group(0)


def test_la_bandeja_ya_no_es_una_entrada_del_menu(http):
    """Solicitudes absorbió los filtros de conversación, así que la Bandeja
    dejó de ser un destino del sidebar."""
    assert ">Bandeja<" not in _sidebar(http.get("/").text)


@pytest.mark.parametrize("url,activo", [
    ("/", "/"),
    ("/?conv=pausadas", "/"),
    (f"/inbox/{PHONE}", "/"),
    ("/clients", "/clients"),
    # «Mi día» y «Mi cartera» comparten prefijo: con un `startswith('/my')` se
    # marcaban las DOS a la vez, que es peor que no marcar ninguna — el menú
    # deja de decirte dónde estás parado.
    ("/my", "/my"),
    ("/my/cobros", "/my/cobros"),
])
def test_el_sidebar_siempre_marca_un_item_activo(http, url, activo):
    """Quitar la Bandeja no puede dejar el chat sin nada resaltado en el menú:
    el chat se alcanza desde Solicitudes, así que ese es su ítem."""
    activos = re.findall(r'<a href="([^"]+)"[^>]*aria-current="page"', _sidebar(http.get(url).text))
    assert activos == [activo]


def _boton_volver(html: str) -> str:
    """Destino del botón «Volver» del chat (el primero de la página)."""
    return re.search(r'<a class="btn-nav" href="([^"]+)">← Volver', html).group(1)


def test_el_volver_del_chat_conserva_el_filtro_de_solicitudes(http):
    r = http.get(f"/inbox/{PHONE}?volver=/%3Fconv%3Dpausadas")

    assert r.status_code == 200
    assert _boton_volver(r.text) == "/?conv=pausadas"


def test_el_volver_del_chat_descarta_un_origen_en_la_bandeja(http):
    """El botón dice «Solicitudes», así que no puede llevar a la Bandeja."""
    r = http.get(f"/inbox/{PHONE}?volver=/inbox%3Ffilter%3Desperando")

    assert r.status_code == 200
    assert _boton_volver(r.text) == "/"
    assert "Volver a Solicitudes" in r.text


def test_un_volver_externo_en_el_chat_cae_a_solicitudes(http):
    r = http.get(f"/inbox/{PHONE}?volver=//evil.com")

    assert r.status_code == 200
    assert "evil.com" not in r.text
    assert _boton_volver(r.text) == "/"


# ---------------------------------------------------------------------------
# Ficha del cliente: regreso al folio o chat de origen, y sin enlace duplicado
# ---------------------------------------------------------------------------


def test_el_volver_de_la_ficha_regresa_a_la_solicitud(http):
    r = http.get(f"/clients/{PHONE}?volver=/tickets/1")

    assert r.status_code == 200
    assert 'href="/tickets/1"' in r.text
    assert "Volver a la solicitud" in r.text


def test_un_volver_externo_en_la_ficha_cae_a_clientes(http):
    r = http.get(f"/clients/{PHONE}?volver=https://evil.com")

    assert r.status_code == 200
    assert "evil.com" not in r.text
    assert "Volver a clientes" in r.text


def test_la_ficha_se_enlaza_una_sola_vez_desde_el_folio(http):
    # REGRESIÓN: el botón .btn-nav convivió con el enlace de texto viejo y el
    # folio mostraba «Ficha del cliente» dos veces.
    r = http.get("/tickets/1")

    assert r.text.count("Ficha del cliente") == 1


# ---------------------------------------------------------------------------
# Cobranza: _volver preserva el origen a través de las acciones POST
# ---------------------------------------------------------------------------


def test_volver_de_cobranza_preserva_el_origen_interno():
    # REGLA: entrar al folio desde /cobros pagina 2, ejecutar una acción de
    # cobro y regresar debe devolverte a /cobros pagina 2 — el redirect del
    # POST reenvía `volver` como query para que el botón de regreso sobreviva.
    from app.cobranza_panel import _volver

    r = _volver(5, volver="/cobros?pagina=2", ok="cobro")
    assert r.headers["location"] == "/tickets/5?ok=cobro&volver=%2Fcobros%3Fpagina%3D2"


def test_volver_de_cobranza_descarta_un_origen_externo():
    from app.cobranza_panel import _volver

    assert _volver(5, volver="https://evil.com").headers["location"] == "/tickets/5"


def test_volver_de_cobranza_sin_folio_cae_a_la_lista():
    from app.cobranza_panel import _volver

    assert _volver(None).headers["location"] == "/cobros"
