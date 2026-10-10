"""El folio de la escalada también aparece en el estatus que ve el cliente.

Reporte del operador (2026-10-09): un cliente pidió que un asesor revisara su trámite; como no tenía
folio abierto, la escalada le abrió uno nuevo («Escalado a asesor») y se asignó. Al preguntar
«¿cómo va?» el bot le contestó con sus folios VIEJOS, porque `_status_reply` excluía el de la
escalada por «interno». Para el cliente no es interno: es justo lo que pidió y lo que un asesor ya
tiene. Si estas pruebas se ponen rojas, el cliente vuelve a recibir el estatus de algo que ya no es
lo que le importa, y el asesor recibe la queja.

Decisión del operador («opción A»): el folio de la escalada se lista siempre, con un nombre que el
cliente entienda —«Revisión con asesor»—, no el nombre interno que ve el CRM.
"""

from datetime import timedelta

from sqlmodel import select

from app import conversation
from app.conversation import _status_reply, handle_incoming
from app.models import Conversation, Ticket, TicketStatus, utcnow

PHONE = "5215522220000"


def _folio(session, **kw) -> Ticket:
    base = dict(
        phone=PHONE,
        category="c",
        service_id="sb-curp",
        service_name="CURP",
        price_info="$1",
        status=TicketStatus.NUEVO,
    )
    base.update(kw)
    t = Ticket(**base)
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


def _folio_de_escalada(session) -> Ticket:
    tickets = session.exec(
        select(Ticket).where(Ticket.phone == PHONE).where(Ticket.service_id == "escalado")
    ).all()
    assert len(tickets) == 1, "la preparación no abrió exactamente un folio de escalada"
    return tickets[0]


def test_el_caso_reportado_el_estatus_incluye_el_folio_de_la_escalada(session):
    """El caso del reporte por el camino real: un folio viejo ya cerrado, el cliente pide un
    asesor (se abre el folio de escalada y se asigna) y luego pregunta cómo va."""
    _folio(
        session,
        status=TicketStatus.CERRADO,
        created_at=utcnow() - timedelta(days=30),
    )
    handle_incoming(session, PHONE, "quiero hablar con un asesor")
    escalada = _folio_de_escalada(session)
    escalada.assigned_to = "ana"
    session.add(escalada)
    session.commit()

    replies = handle_incoming(session, PHONE, "¿cómo va mi trámite?")

    assert len(replies) == 1
    texto = replies[0]
    assert f"Revisión con asesor (folio #{escalada.id}): recibido, con asesor asignado" in texto, (
        f"el estatus no muestra el folio que el cliente tiene abierto: {texto!r}"
    )
    assert texto.index("Revisión con asesor") < texto.index("CURP"), (
        "el folio abierto de la escalada debe ir antes que el viejo (el más nuevo primero)"
    )
    session.expire_all()
    assert session.get(Conversation, PHONE).state == "escalado", "preguntar el estatus no saca de la espera"


def test_sin_otros_folios_el_estatus_ya_no_dice_que_no_hay_solicitudes(session):
    """Antes, el cliente cuyo único folio era la escalada recibía «Aún no tienes ninguna solicitud»
    mientras un asesor ya lo atendía: le negaba lo que acababa de pedir."""
    handle_incoming(session, PHONE, "quiero hablar con un asesor")
    escalada = _folio_de_escalada(session)

    texto = _status_reply(session, PHONE)

    assert texto != conversation.NO_TICKETS_REPLY
    assert f"Revisión con asesor (folio #{escalada.id})" in texto


def test_el_cliente_no_ve_el_nombre_interno_de_la_escalada(session):
    """«Escalado a asesor» es la etiqueta del CRM. Al cliente se le dice qué pidió."""
    _folio(session, service_id="escalado", service_name="Escalado a asesor", category="(escalado)")

    texto = _status_reply(session, PHONE)

    assert "Escalado a asesor" not in texto
    assert "Revisión con asesor" in texto


def test_el_folio_de_la_escalada_guarda_su_nombre_interno(session):
    """Mostrar otro nombre al cliente no puede cambiar lo que se guarda: el CRM y el panel
    reconocen la escalada por su folio y su nombre."""
    handle_incoming(session, PHONE, "quiero hablar con un asesor")
    _status_reply(session, PHONE)

    session.expire_all()
    assert _folio_de_escalada(session).service_name == "Escalado a asesor"


def test_una_escalada_cerrada_se_lista_con_su_estado(session):
    """Opción A: se lista abierta o cerrada, como cualquier folio, con su estado real."""
    _folio(
        session,
        service_id="escalado",
        service_name="Escalado a asesor",
        category="(escalado)",
        status=TicketStatus.CERRADO,
    )

    texto = _status_reply(session, PHONE)

    assert "Revisión con asesor" in texto
    assert conversation.STATUS_LABELS[TicketStatus.CERRADO] in texto


def test_un_folio_normal_se_sigue_nombrando_por_su_tramite(session):
    _folio(session)

    assert "• CURP (folio #" in _status_reply(session, PHONE)
