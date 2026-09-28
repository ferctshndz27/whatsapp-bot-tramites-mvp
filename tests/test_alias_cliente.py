"""Alias del cliente (Client.alias) y edición del nombre desde la ficha.

Decisión del operador (2026-09-28): cada cliente tiene un alias que sirve de identificador
visual para los colaboradores y se pinta como etiqueta debajo del nombre en las vistas de
trabajo diario (directorio, ficha, CRM, Solicitudes, detalle del folio y Mi día). El nombre
también se edita en la ficha, con la misma regla que el alta con teléfono repetido: se
propaga a los folios y queda auditado.

El alias es interno: vive solo en Client (no se copia al folio) y el bot nunca lo lee.
"""

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

import app.db as db
from app.clients import MAX_ALIAS
from app.config import settings
from app.conversation import handle_incoming
from app.main import app
from app.models import AdminAudit, Client, Ticket, TicketStatus, User

CLAVE = "clave-de-prueba"
HASH = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()

CLI = "525500000041"
OTRO = "525500000042"
ALIAS = "contador Pérez"


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "geoip_enabled", False)
    with Session(engine) as s:
        s.add(User(username="jefa", password_hash=HASH, is_admin=True, office_id=1))
        s.add(User(username="ana", password_hash=HASH, is_admin=False, office_id=1))
        s.add(Client(phone=CLI, name="Cliente Uno", office_id=1, alias=ALIAS))
        s.add(Client(phone=OTRO, name="Cliente Dos", office_id=1))
        s.add(Ticket(phone=CLI, client_name="Cliente Uno", category="c", service_id="s",
                     service_name="Acta", price_info="$1", status=TicketStatus.TOMADO,
                     assigned_to="ana", office_id=1))
        s.commit()
    yield


def _sesion(username: str) -> TestClient:
    cliente = TestClient(app, base_url="https://testserver")
    r = cliente.post("/login", data={"username": username, "password": CLAVE},
                     follow_redirects=False)
    assert r.status_code == 303, f"el login de {username} falló"
    return cliente


def _cliente(phone: str = CLI) -> Client:
    with Session(db.engine) as s:
        return s.get(Client, phone)


def _ficha(username: str, phone: str = CLI, **campos):
    datos = {"email": "", "origen": "", "idioma": "es-MX", "notas_internas": "", **campos}
    return _sesion(username).post(f"/clients/{phone}/update", data=datos, follow_redirects=False)


# --- La ficha guarda el alias ------------------------------------------------


def test_la_ficha_guarda_el_alias_en_una_linea_y_con_tope():
    r = _ficha("ana", alias="  la   señora\n de las actas  ")
    assert r.status_code == 303
    assert _cliente().alias == "la señora de las actas"
    _ficha("ana", alias="x" * (MAX_ALIAS + 20))
    assert _cliente().alias == "x" * MAX_ALIAS


def test_el_alias_vacio_lo_quita():
    _ficha("ana", alias="   ")
    assert _cliente().alias is None


def test_una_peticion_sin_los_campos_nuevos_no_toca_ni_alias_ni_nombre():
    """None ≠ «borrar»: una petición que no manda el campo no lo pisa."""
    _ficha("ana", email="uno@ejemplo.com")
    c = _cliente()
    assert (c.alias, c.name, c.email) == (ALIAS, "Cliente Uno", "uno@ejemplo.com")


# --- La ficha edita el nombre con la regla del alta ---------------------------


def test_el_nombre_de_la_ficha_se_propaga_a_los_folios_y_se_audita():
    _ficha("ana", name="  Cliente Uno Corregido ")
    assert _cliente().name == "Cliente Uno Corregido"
    with Session(db.engine) as s:
        folio = s.exec(select(Ticket).where(Ticket.phone == CLI)).one()
        assert folio.client_name == "Cliente Uno Corregido"
        acciones = [(a.admin, a.action, a.target) for a in s.exec(select(AdminAudit)).all()]
    assert ("ana", "cliente:nombre", f"{CLI} · Cliente Uno Corregido") in acciones


def test_el_nombre_vacio_en_la_ficha_se_ignora():
    _ficha("ana", name="   ")
    assert _cliente().name == "Cliente Uno"


def test_el_mismo_nombre_no_deja_fila_de_cambio_de_nombre():
    _ficha("ana", name="Cliente Uno")
    with Session(db.engine) as s:
        acciones = [a.action for a in s.exec(select(AdminAudit)).all()]
    assert "cliente:nombre" not in acciones


# --- La etiqueta en las vistas ------------------------------------------------


@pytest.mark.parametrize(
    "usuario,ruta",
    [
        ("jefa", "/clients"),
        ("jefa", f"/clients/{CLI}"),
        ("jefa", "/"),  # Solicitudes
        ("jefa", "/tickets/1"),
        ("ana", "/my"),
    ],
)
def test_la_etiqueta_del_alias_sale_en_las_vistas_de_trabajo(usuario, ruta):
    html = _sesion(usuario).get(ruta).text
    assert f'<span class="alias-tag">' in html
    assert ALIAS in html


def test_sin_alias_no_se_pinta_etiqueta():
    with Session(db.engine) as s:
        c = s.get(Client, CLI)
        c.alias = None
        s.add(c)
        s.commit()
    html = _sesion("jefa").get(f"/clients/{CLI}").text
    assert '<span class="alias-tag">' not in html


def test_el_alias_se_escapa():
    _ficha("ana", alias='<img src=x onerror="alert(1)">')
    html = _sesion("jefa").get("/clients").text
    assert "<img src=x" not in html
    assert "&lt;img src=x" in html


def test_el_crm_trae_el_alias_y_lo_encuentra_al_buscar():
    s = _sesion("jefa")
    contactos = s.get("/crm/api/tablero").json()["contactos"]
    assert next(c for c in contactos if c["phone"] == CLI)["alias"] == ALIAS
    assert next(c for c in contactos if c["phone"] == OTRO)["alias"] is None
    encontrados = s.get("/crm/api/tablero", params={"q": "perez"}).json()["contactos"]
    assert [c["phone"] for c in encontrados] == [CLI]


def test_el_directorio_encuentra_por_alias():
    html = _sesion("jefa").get("/clients", params={"q": "contador"}).text
    assert CLI in html
    assert OTRO not in html


# --- Interno: el bot nunca lo usa ---------------------------------------------


def test_el_bot_nunca_le_dice_el_alias_al_cliente():
    with Session(db.engine) as s:
        respuestas = handle_incoming(s, CLI, "hola") + handle_incoming(s, CLI, "menu")
    assert all(ALIAS not in r for r in respuestas)
