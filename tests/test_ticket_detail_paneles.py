"""El detalle de folio se arma con un panel (.card) por sección.

Las secciones de acción viven DENTRO del `if` de propiedad del ticket, así que
un `<div>` que abra en una rama y cierre en otra deja HTML roto justo en los
estados que uno no está mirando. Estas pruebas renderizan los cuatro estados y
verifican que cada uno cierre lo que abre.
"""

from html.parser import HTMLParser

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine
from sqlmodel.pool import StaticPool

import app.db as db
from app.config import settings
from app.main import app
from app.models import Ticket, TicketStatus, User

CLAVE = "clave-de-prueba"
HASH_CLAVE = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()
PHONE = "525599990000"

# Sin cierre en HTML: no cuentan para el balance.
VACIOS = {"br", "hr", "img", "input", "link", "meta", "source", "col", "area", "base"}


class _Balance(HTMLParser):
    """Lleva la pila de etiquetas abiertas y anota lo que no cuadra."""

    def __init__(self):
        super().__init__()
        self.pila: list[str] = []
        self.errores: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in VACIOS:
            self.pila.append(tag)

    def handle_endtag(self, tag):
        if tag in VACIOS:
            return
        if not self.pila:
            self.errores.append(f"</{tag}> sin apertura")
        elif self.pila[-1] != tag:
            self.errores.append(f"</{tag}> cierra a <{self.pila[-1]}>")
            self.pila.pop()
        else:
            self.pila.pop()


def _revisar(html: str) -> list[str]:
    parser = _Balance()
    parser.feed(html)
    return parser.errores + [f"<{t}> sin cerrar" for t in parser.pila]


@pytest.fixture(autouse=True)
def _base_compartida(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "geoip_enabled", False)
    yield


def _folio(**kwargs) -> int:
    with Session(db.engine) as s:
        s.add(User(username="asesora", password_hash=HASH_CLAVE))
        s.add(User(username="colega", password_hash=HASH_CLAVE))
        ticket = Ticket(
            phone=PHONE,
            client_name="Ana Ruiz",
            category="Actas",
            service_id="sb-acta",
            service_name="Acta de nacimiento",
            price_info="$120",
            **{"status": TicketStatus.NUEVO, **kwargs},
        )
        s.add(ticket)
        s.commit()
        s.refresh(ticket)
        return ticket.id


def _abrir(ticket_id: int) -> str:
    with TestClient(app, base_url="https://testserver") as cliente:
        r = cliente.post(
            "/login", data={"username": "asesora", "password": CLAVE}, follow_redirects=False
        )
        assert r.status_code == 303, "el login de la prueba dejó de funcionar"
        r = cliente.get(f"/tickets/{ticket_id}")
        assert r.status_code == 200
        return r.text


@pytest.mark.parametrize(
    "estado, kwargs",
    [
        ("sin asignar", {}),
        ("mío", {"assigned_to": "asesora", "status": TicketStatus.TOMADO}),
        ("de alguien más", {"assigned_to": "colega", "status": TicketStatus.TOMADO}),
        ("eliminado", {"habilitado": False}),
    ],
)
def test_el_html_cierra_lo_que_abre_en_cada_estado(estado, kwargs):
    errores = _revisar(_abrir(_folio(**kwargs)))
    assert not errores, f"folio {estado}: {errores}"


def test_cada_seccion_del_asignado_es_su_propio_panel():
    html = _abrir(_folio(assigned_to="asesora", status=TicketStatus.TOMADO))
    # Un h2 fuera de un panel es una sección que se quedó suelta al editar.
    for titulo in ("Gestión", "Agregar nota", "Seguimiento", "Transferir a otro colaborador"):
        assert f'<div class="card">\n  <h2>{titulo}</h2>' in html, titulo


def test_sin_asignar_la_accion_de_tomar_tambien_va_en_panel():
    html = _abrir(_folio())
    assert '<div class="card">\n  <h2>Gestión</h2>' in html
    assert "Tomar esta solicitud" in html
