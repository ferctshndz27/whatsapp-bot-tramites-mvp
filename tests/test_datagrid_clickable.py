"""Filas de los grids clickeables completas, como las tarjetas de conversación.

REGLA UX: en las tablas con detalle (folios, clientes, actividad, cobros) toda
la fila navega, no solo la celda del nombre. El mecanismo es un atributo
`data-href` en el `<tr>` + un script de delegación en base.html. Mejora
progresiva: el enlace interno de la fila queda intacto, así que sin JS todo
sigue funcionando.

Aquí se prueba lo que el servidor controla: que cada vista pinte `data-href`
con el MISMO destino que el enlace de la fila. El comportamiento del click es
del navegador y se validó a mano.
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
from app.models import (
    Client,
    Conversation,
    Direction,
    Message,
    Ticket,
    TicketActivity,
    TicketStatus,
    User,
)

PHONE = "525500000001"
CLAVE = "clave-de-prueba"
# rounds=4: con el costo por defecto (12) cada login del test costaría ~0.3 s.
HASH_CLAVE = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()


@pytest.fixture(autouse=True)
def _base_compartida(monkeypatch):
    """Engine con StaticPool para que el hilo del TestClient vea la misma base."""
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
    with Session(db.engine) as s:
        s.add(User(username="asesora", password_hash=HASH_CLAVE))
        s.add(Client(phone=PHONE, name="Ana Ruiz"))
        s.add(Conversation(phone=PHONE, state="menu_categoria"))
        s.add(Message(phone=PHONE, direction=Direction.IN, body="hola"))
        s.add(
            Ticket(
                phone=PHONE,
                client_name="Ana Ruiz",
                category="Actas",
                service_id="sb-acta",
                service_name="Acta de nacimiento",
                price_info="$120",
                status=TicketStatus.TOMADO,
                assigned_to="asesora",
            )
        )
        s.commit()
        s.add(TicketActivity(ticket_id=1, username="asesora", action="tomado"))
        s.commit()
    with TestClient(app, base_url="https://testserver") as cliente:
        r = cliente.post(
            "/login", data={"username": "asesora", "password": CLAVE}, follow_redirects=False
        )
        assert r.status_code == 303, "el login de la prueba dejó de funcionar"
        yield cliente


@pytest.mark.parametrize(
    "url,destino",
    [
        ("/", "/tickets/1?volver="),
        ("/clients", f"/clients/{PHONE}?volver="),
        (f"/clients/{PHONE}", "/tickets/1"),
        ("/my", "/tickets/1"),
        ("/activity", "/tickets/1"),
    ],
)
def test_las_filas_del_grid_llevan_su_destino(http, url, destino):
    html = http.get(url).text
    filas = re.findall(r'<tr data-href="([^"]+)"', html)
    assert any(f.startswith(destino) for f in filas), f"sin fila clickeable en {url}"


def test_el_data_href_coincide_con_el_enlace_de_la_fila(http):
    """Si el atributo y el enlace divergieran, click en la fila y click en el
    texto llevarían a lugares distintos."""
    html = http.get("/").text
    fila = re.search(r'<tr data-href="([^"]+)">.*?<a class="cell-person" href="([^"]+)"', html, re.S)
    assert fila and fila.group(1) == fila.group(2)


def test_el_script_de_delegacion_esta_en_todas_las_vistas(http):
    """Vive en base.html: si alguien lo mueve a una plantilla concreta, el
    resto de los grids queda con cursor de click que no hace nada."""
    for url in ("/", "/clients", "/my"):
        assert "tr[data-href]" in http.get(url).text, f"falta el script en {url}"
