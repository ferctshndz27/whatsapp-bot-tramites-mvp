"""La escritura de la bandeja respeta el folio, igual que la lectura del CRM
(auditoría 2026-09, AUTHZ-2).

El CRM le niega a un colaborador la conversación de un folio que no atiende
(`crm._conversacion_visible`), pero `/inbox/{phone}/send`, `/bot-on` y
`/messages/{id}/delete` solo comprobaban la OFICINA. PoC del informe: `ana`,
sin folios de ese cliente, tenía el chat en 404 y aun así mandaba un WhatsApp
firmado por el negocio, reactivaba su bot y ocultaba mensajes. Y sin fila
`Client` el guard contestaba True.

La regla que se prueba es UNA (`clients.conversacion_escribible`): oficina, y
para quien no es admin, que el folio vigente sea reclamable por él —suyo, sin
dueño, o con dueño provisional (`NUEVO`, de reparto)—, porque «quien responde
se queda el chat» (`inbox._pause_bot`). Un folio `TOMADO` por otro: no. Sin
folio: no. Sin ficha: no.

Cada negativo trae su control positivo en la misma prueba: sin él, un guard
que bloqueara a TODO el mundo dejaría el archivo en verde.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlmodel import select

import app.db as db
from app import whatsapp_client
from app.clients import folio_vigente, folio_vigente_de
from app.inbox import inbox_bot_on, inbox_delete_message, inbox_send
from app.models import Client, Conversation, Direction, Message, Ticket, TicketActivity, TicketStatus, User

CLIENTE = "5215511112222"


def _request(user: str) -> SimpleNamespace:
    return SimpleNamespace(session={"user": user}, url=SimpleNamespace(path=f"/inbox/{CLIENTE}"))


def _folio(session, *, assigned_to, status, habilitado=True, hace_horas=0) -> Ticket:
    t = Ticket(
        phone=CLIENTE, category="Servicios básicos", service_id="curp", service_name="CURP",
        price_info="$80", assigned_to=assigned_to, status=status, habilitado=habilitado,
        created_at=datetime.now(timezone.utc) - timedelta(hours=hace_horas),
    )
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


@pytest.fixture()
def escenario(session, monkeypatch):
    """Cliente con ventana abierta (un entrante reciente) y conversación escalada,
    para que el ÚNICO motivo de rechazo posible sea el permiso. Lo enviado se
    captura: la prueba mide si salió un WhatsApp, no si se intentó."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    enviados: list[str] = []
    monkeypatch.setattr(whatsapp_client, "send_text", lambda phone, body: enviados.append(body) or "wamid.X")
    session.add(Client(phone=CLIENTE, name="Cliente"))
    session.add(Conversation(phone=CLIENTE, state="escalado"))
    session.add(Message(phone=CLIENTE, direction=Direction.IN, body="hola",
                        created_at=datetime.now(timezone.utc) - timedelta(minutes=5)))
    session.add(User(username="jefa", password_hash="x", is_admin=True))
    session.add(User(username="ana", password_hash="x"))
    session.add(User(username="bob", password_hash="x"))
    session.commit()
    return enviados


def _salientes(session) -> list[Message]:
    return list(session.exec(select(Message).where(Message.phone == CLIENTE, Message.direction == Direction.OUT)).all())


def test_el_colaborador_que_no_atiende_el_folio_no_manda_ni_reactiva_ni_borra(escenario, session):
    """El PoC del informe, cerrado. `ana` intenta las tres escrituras sobre el
    cliente cuyo folio está TOMADO por `bob`: ninguna deja rastro. Luego `bob`
    hace las tres (control positivo) y el folio sigue siendo suyo."""
    folio = _folio(session, assigned_to="bob", status=TicketStatus.TOMADO)
    folio.agent_active = False
    session.add(folio)
    session.commit()

    r = inbox_send(_request("ana"), CLIENTE, body="hola, te apoyo")
    assert r.headers["location"] == "/" and escenario == [] and _salientes(session) == [], (
        "ana mandó un WhatsApp firmado por el negocio a un cliente que no atiende"
    )
    r = inbox_bot_on(_request("ana"), CLIENTE)
    session.refresh(folio)
    assert r.headers["location"] == "/" and folio.agent_active is False, "ana reactivó el bot ajeno"
    assert session.exec(select(TicketActivity).where(TicketActivity.username == "ana")).first() is None

    # control positivo: bob, el dueño
    inbox_send(_request("bob"), CLIENTE, body="aquí sigo")
    assert escenario == ["aquí sigo"]
    (saliente,) = _salientes(session)
    inbox_delete_message(_request("ana"), CLIENTE, saliente.id)
    session.refresh(saliente)
    assert saliente.habilitado is True, "ana ocultó un mensaje de una conversación ajena"
    inbox_delete_message(_request("bob"), CLIENTE, saliente.id)
    session.refresh(saliente)
    assert saliente.habilitado is False
    session.refresh(folio)
    assert folio.assigned_to == "bob"


def test_quien_responde_se_queda_el_folio_sin_dueno_o_provisional(escenario, session):
    """La regla de producto que el guard tiene que respetar: un folio sin dueño,
    o con dueño de reparto todavía en NUEVO, lo reclama quien contesta
    (`inbox._pause_bot`, `asignacion.provisional`). Si esto se pone rojo, el
    guard nuevo mató «quien responde se queda el chat»."""
    sin_dueno = _folio(session, assigned_to=None, status=TicketStatus.NUEVO)
    inbox_send(_request("ana"), CLIENTE, body="yo lo tomo")
    session.refresh(sin_dueno)
    assert escenario == ["yo lo tomo"] and sin_dueno.assigned_to == "ana"
    assert sin_dueno.status == TicketStatus.TOMADO

    # Otro folio, más nuevo, de reparto para bob y todavía en NUEVO: ana también puede.
    provisional = _folio(session, assigned_to="bob", status=TicketStatus.NUEVO)
    assert folio_vigente_de(session, CLIENTE).id == provisional.id, "el vigente debía ser el nuevo"
    inbox_send(_request("ana"), CLIENTE, body="lo trabajo yo")
    session.refresh(provisional)
    assert provisional.assigned_to == "ana", "el folio de reparto no cambió de manos al contestar"
    accion = {a.action for a in session.exec(select(TicketActivity).where(TicketActivity.ticket_id == provisional.id))}
    assert "tomado-de:bob" in accion, "el histórico no dice a quién se le quitó"


def test_sin_folio_o_sin_ficha_el_colaborador_no_escribe_y_el_admin_si(escenario, session):
    """Los dos bordes del informe: cliente sin folio, y teléfono sin fila
    Client (el guard viejo contestaba True). El admin sigue igual que antes en
    el primero; en el segundo tampoco puede: sin ficha no hay a quién."""
    r = inbox_send(_request("ana"), CLIENTE, body="hola")
    assert r.headers["location"] == "/" and escenario == [], "sin folio, ana escribió"
    inbox_send(_request("jefa"), CLIENTE, body="hola, soy la jefa")
    assert escenario == ["hola, soy la jefa"]

    sin_ficha = "5215599990000"
    session.add(Conversation(phone=sin_ficha, state="escalado"))
    session.add(Message(phone=sin_ficha, direction=Direction.IN, body="?",
                        created_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    session.commit()
    r = inbox_send(_request("ana"), sin_ficha, body="hola")
    assert r.headers["location"] == "/" and escenario == ["hola, soy la jefa"], "sin ficha, ana escribió"
    r = inbox_send(_request("jefa"), sin_ficha, body="hola")
    assert r.headers["location"] == "/" and escenario == ["hola, soy la jefa"], "sin ficha, la admin escribió"


def test_un_folio_cerrado_o_deshabilitado_no_le_da_acceso_a_nadie_mas(escenario, session):
    """El vigente es el ABIERTO más reciente. Que ana tenga un folio CERRADO
    viejo no le da la conversación si el abierto es de bob; y un folio
    deshabilitado (borrado suave) no existe para la regla."""
    _folio(session, assigned_to="ana", status=TicketStatus.CERRADO, hace_horas=48)
    _folio(session, assigned_to="ana", status=TicketStatus.TOMADO, habilitado=False, hace_horas=1)
    abierto_de_bob = _folio(session, assigned_to="bob", status=TicketStatus.TOMADO, hace_horas=24)
    assert folio_vigente_de(session, CLIENTE).id == abierto_de_bob.id

    r = inbox_send(_request("ana"), CLIENTE, body="hola")
    assert r.headers["location"] == "/" and escenario == []

    # Si el ÚNICO folio es el cerrado de ana, la conversación sigue siendo suya
    # (el CRM también se la muestra en «Folios cerrados»).
    session.delete(abierto_de_bob)
    session.commit()
    inbox_send(_request("ana"), CLIENTE, body="¿algo más?")
    assert escenario == ["¿algo más?"]


def test_dos_folios_con_el_mismo_created_at_los_desempata_el_id_no_el_orden_fisico(escenario, session):
    """Hallazgo de la refutación (confirmado ejecutando): con `created_at`
    idéntico —la ventana que abre la carrera de confirmaciones— `ORDER BY
    created_at DESC` sin segundo criterio dejaba a SQLite elegir por orden
    físico de fila, y desde AUTHZ-2 esa elección decide quién puede escribirle
    al cliente. El de `bob` se inserta primero; sin desempate salía «vigente»
    aunque el de `ana` sea el más nuevo (id mayor)."""
    mismo_instante = datetime(2026, 9, 21, 12, 0, 0)
    de_bob = Ticket(phone=CLIENTE, category="c", service_id="a", service_name="A", price_info="",
                    status=TicketStatus.TOMADO, assigned_to="bob", created_at=mismo_instante)
    de_ana = Ticket(phone=CLIENTE, category="c", service_id="b", service_name="B", price_info="",
                    status=TicketStatus.TOMADO, assigned_to="ana", created_at=mismo_instante)
    session.add(de_bob)
    session.commit()
    session.add(de_ana)
    session.commit()
    assert de_ana.id > de_bob.id

    assert folio_vigente_de(session, CLIENTE).id == de_ana.id, "el empate lo decidió el orden físico"
    inbox_send(_request("ana"), CLIENTE, body="hola")
    assert escenario == ["hola"], "ana atiende el folio más nuevo y no pudo escribir"


def test_el_crm_y_la_bandeja_eligen_el_mismo_folio_vigente():
    """La función es una y es pura: abierto más reciente; si no hay abierto, el
    más reciente. La lista llega del más nuevo al más viejo, como la consulta."""
    def t(i, status):
        return Ticket(id=i, phone=CLIENTE, category="c", service_id="s", service_name="s", price_info="", status=status)

    assert folio_vigente([]) is None
    solo_cerrados = [t(3, TicketStatus.CERRADO), t(2, TicketStatus.CERRADO)]
    assert folio_vigente(solo_cerrados).id == 3, "sin abiertos gana el más reciente"
    mezcla = [t(5, TicketStatus.CERRADO), t(4, TicketStatus.TOMADO), t(3, TicketStatus.NUEVO)]
    assert folio_vigente(mezcla).id == 4, "el abierto más reciente le gana a un cerrado más nuevo"
    dos_abiertos = [t(9, TicketStatus.NUEVO), t(8, TicketStatus.TOMADO)]
    assert folio_vigente(dos_abiertos).id == 9
