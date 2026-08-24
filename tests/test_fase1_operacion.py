"""Fase 1 del análisis de brechas: tiempos de operación, etiquetas y ficha CRM.

Qué protege este archivo (docs/ANALISIS-BRECHAS-ERP-CRM.md §5):

- `first_response_at` lo llena el PRIMER envío humano y nunca se sobreescribe;
  el bot no pasa por el inbox, así que ese es el único punto de llenado.
- `finish` y los cambios de status sellan `resolved_at` / `status_changed_at`.
- Etiquetar es idempotente (una sola fila por par folio-etiqueta), quitar es
  soft delete (el espejo MSSQL se entera vía after_update) y re-etiquetar
  reactiva la misma fila.
- La ficha del cliente normaliza el correo y el consentimiento guarda fecha y
  fuente (Meta exige poder probar cuándo y cómo — §2.5 del análisis).
- El export CSV lleva las etiquetas y las tablas nuevas están en el espejo.

Mismo esqueleto que test_authz.py: HTTP real con TestClient sobre app.main,
engine StaticPool compartido entre hilos, red apagada.
"""

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import create_engine, select

import app.db as db
import app.messaging as messaging
from app.config import settings
from app.db import get_session
from app.main import app
from app.models import (
    Client,
    Direction,
    Label,
    Message,
    Ticket,
    TicketLabel,
    TicketStatus,
    User,
)

CLAVE = "clave-de-prueba"
TELEFONO = "5215550000001"


_contador = iter(range(1_000_000))


def _sin_red(*args, **kwargs):
    # wa_message_id es UNIQUE: cada envío falso necesita su propio id.
    return f"wamid.prueba.{next(_contador)}"


@pytest.fixture(autouse=True)
def entorno(_isolated_db, monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(messaging, "send_text", _sin_red)
    with get_session() as s:
        s.add(
            User(
                username="admin",
                password_hash=bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt()).decode(),
                is_admin=True,
            )
        )
        s.add(Client(phone=TELEFONO, name="Cliente Prueba"))
        # Mensaje entrante reciente: sin él la ventana de 24h está cerrada y
        # /inbox/{phone}/send redirige antes de llegar al sello de tiempos.
        s.add(Message(phone=TELEFONO, direction=Direction.IN, body="hola, necesito un acta"))
        s.commit()
    yield


def _sesion() -> TestClient:
    cliente = TestClient(app, base_url="https://testserver")
    r = cliente.post(
        "/login", data={"username": "admin", "password": CLAVE}, follow_redirects=False
    )
    assert r.status_code == 303, "el login de la prueba falló"
    return cliente


def _crear_ticket(**extra) -> int:
    with get_session() as s:
        t = Ticket(
            phone=TELEFONO,
            client_name="Cliente Prueba",
            category="actas",
            service_id="acta-nacimiento",
            service_name="Acta de nacimiento",
            price_info="$150 (estimado)",
            **extra,
        )
        s.add(t)
        s.commit()
        return t.id


def _ticket(tid: int) -> Ticket:
    with get_session() as s:
        return s.get(Ticket, tid)


# ---------------------------------------------------------------------------
# Tiempos de operación
# ---------------------------------------------------------------------------


def test_primer_envio_humano_sella_first_response():
    tid = _crear_ticket()
    assert _ticket(tid).first_response_at is None

    cliente = _sesion()
    r = cliente.post(
        f"/inbox/{TELEFONO}/send", data={"body": "Hola, ya reviso su trámite"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    despues = _ticket(tid)
    assert despues.first_response_at is not None
    assert despues.first_response_by == "admin"
    # El envío también auto-toma el folio: el cambio de etapa queda sellado.
    assert despues.status == TicketStatus.TOMADO
    assert despues.status_changed_at is not None


def test_segundo_envio_no_sobreescribe_first_response():
    tid = _crear_ticket()
    cliente = _sesion()
    cliente.post(f"/inbox/{TELEFONO}/send", data={"body": "primera"}, follow_redirects=False)
    primera = _ticket(tid).first_response_at
    assert primera is not None

    cliente.post(f"/inbox/{TELEFONO}/send", data={"body": "segunda"}, follow_redirects=False)
    assert _ticket(tid).first_response_at == primera, (
        "la métrica mide la PRIMERA respuesta; un segundo envío no debe moverla"
    )


def test_finish_sella_resolved_y_status_changed():
    tid = _crear_ticket()
    cliente = _sesion()
    r = cliente.post(
        f"/tickets/{tid}/finish", data={"confirm": "si"}, follow_redirects=False
    )
    assert r.status_code == 303
    t = _ticket(tid)
    assert t.status == TicketStatus.CERRADO
    assert t.resolved_at is not None
    assert t.status_changed_at is not None


def test_cambio_de_status_sella_status_changed_y_cerrado_resuelve():
    # Solo el asignado puede cambiar el status (regla existente de la ruta).
    tid = _crear_ticket(assigned_to="admin")
    cliente = _sesion()

    cliente.post(f"/tickets/{tid}/status", data={"status": "en_proceso"}, follow_redirects=False)
    t = _ticket(tid)
    assert t.status_changed_at is not None
    assert t.resolved_at is None, "en_proceso no es un estado terminal"
    sello = t.status_changed_at

    # Repetir el mismo status no es un cambio: el sello no se mueve.
    cliente.post(f"/tickets/{tid}/status", data={"status": "en_proceso"}, follow_redirects=False)
    assert _ticket(tid).status_changed_at == sello

    cliente.post(f"/tickets/{tid}/status", data={"status": "cerrado"}, follow_redirects=False)
    assert _ticket(tid).resolved_at is not None


# ---------------------------------------------------------------------------
# Etiquetas
# ---------------------------------------------------------------------------


def test_etiquetar_es_idempotente_una_sola_fila():
    tid = _crear_ticket()
    cliente = _sesion()
    for _ in range(2):
        cliente.post(f"/tickets/{tid}/labels", data={"nombre": "urgente"}, follow_redirects=False)

    with get_session() as s:
        assert len(s.exec(select(Label).where(Label.nombre == "urgente")).all()) == 1
        vinculos = s.exec(select(TicketLabel).where(TicketLabel.ticket_id == tid)).all()
        assert len(vinculos) == 1 and vinculos[0].habilitado


def test_quitar_etiqueta_es_soft_delete_y_reetiquetar_reactiva():
    tid = _crear_ticket()
    cliente = _sesion()
    cliente.post(f"/tickets/{tid}/labels", data={"nombre": "urgente"}, follow_redirects=False)
    with get_session() as s:
        label_id = s.exec(select(Label).where(Label.nombre == "urgente")).one().id

    cliente.post(f"/tickets/{tid}/labels/{label_id}/remove", follow_redirects=False)
    with get_session() as s:
        vinculo = s.exec(select(TicketLabel).where(TicketLabel.ticket_id == tid)).one()
        assert not vinculo.habilitado, "quitar deshabilita, no borra (espejo MSSQL)"
        assert s.get(Ticket, tid) is not None, "quitar la etiqueta jamás toca el folio"

    cliente.post(f"/tickets/{tid}/labels", data={"nombre": "urgente"}, follow_redirects=False)
    with get_session() as s:
        vinculos = s.exec(select(TicketLabel).where(TicketLabel.ticket_id == tid)).all()
        assert len(vinculos) == 1 and vinculos[0].habilitado, "reactiva la misma fila"


def test_export_csv_lleva_columna_de_etiquetas():
    tid = _crear_ticket()
    cliente = _sesion()
    cliente.post(f"/tickets/{tid}/labels", data={"nombre": "curp"}, follow_redirects=False)

    r = cliente.get("/export/tickets.csv", follow_redirects=False)
    assert r.status_code == 200
    encabezado, fila = r.text.splitlines()[0], r.text.splitlines()[1]
    assert "etiquetas" in encabezado
    assert "curp" in fila


# ---------------------------------------------------------------------------
# Ficha CRM y consentimiento
# ---------------------------------------------------------------------------


def test_ficha_normaliza_email_a_minusculas():
    cliente = _sesion()
    r = cliente.post(
        f"/clients/{TELEFONO}/update",
        data={"email": "  Cliente@Ejemplo.COM ", "origen": "meta", "idioma": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with get_session() as s:
        c = s.get(Client, TELEFONO)
        assert c.email == "cliente@ejemplo.com"
        assert c.idioma == "es-MX", "idioma vacío regresa al default"


def test_consentimiento_guarda_fecha_y_fuente_y_opt_out_persiste():
    cliente = _sesion()
    cliente.post(
        f"/clients/{TELEFONO}/consentimiento",
        data={"accion": "opt_in", "fuente": "lo pidió en el chat"},
        follow_redirects=False,
    )
    with get_session() as s:
        c = s.get(Client, TELEFONO)
        assert c.opt_in_at is not None
        assert c.opt_in_source == "lo pidió en el chat"

    cliente.post(
        f"/clients/{TELEFONO}/consentimiento", data={"accion": "opt_out"},
        follow_redirects=False,
    )
    with get_session() as s:
        assert s.get(Client, TELEFONO).opt_out_at is not None

    # Un opt-in nuevo (consentimiento renovado) limpia el opt-out.
    cliente.post(
        f"/clients/{TELEFONO}/consentimiento",
        data={"accion": "opt_in", "fuente": "volvió a aceptar"},
        follow_redirects=False,
    )
    with get_session() as s:
        c = s.get(Client, TELEFONO)
        assert c.opt_out_at is None and c.opt_in_source == "volvió a aceptar"


# ---------------------------------------------------------------------------
# Espejo
# ---------------------------------------------------------------------------


def test_tablas_de_etiquetas_estan_en_el_espejo():
    """Protege la quinta place: sin esto una divergencia sería invisible."""
    from app.system import MIRROR_TABLES

    espejadas = {nombre for _, nombre in MIRROR_TABLES}
    assert {"labels", "ticket_labels"} <= espejadas
