from sqlmodel import select

from app.conversation import handle_incoming
from app.models import Conversation, Ticket, TicketStatus

PHONE = "5215500000000"


def test_greeting_gets_open_question_not_service_list(session):
    replies = handle_incoming(session, PHONE, "Hola")

    assert "ayudarte" in replies[0].lower()
    assert "1." not in replies[0]  # regla: no listar servicios en la primera respuesta


def test_direct_service_request_skips_menu_and_creates_ticket(session):
    # Regla: si el cliente llega directo con su trámite, saludar y generar el ticket
    replies = handle_incoming(session, PHONE, "Necesito un acta de divorcio, soy Juan Pérez")
    assert "Acta de divorcio" in replies[0]
    assert "confirmas" in replies[0].lower()
    assert "1." not in replies[0]  # sin listado

    replies = handle_incoming(session, PHONE, "sí")
    assert "folio" in replies[0].lower()

    ticket = session.exec(select(Ticket).where(Ticket.phone == PHONE)).first()
    assert ticket is not None
    assert ticket.service_name == "Acta de divorcio"
    assert ticket.status == TicketStatus.NUEVO


def test_services_keyword_shows_catalog_listing(session):
    handle_incoming(session, PHONE, "Hola")

    replies = handle_incoming(session, PHONE, "¿qué servicios tienen?")

    assert "1." in replies[0]  # única situación donde sí se lista

    conv = session.get(Conversation, PHONE)
    assert conv.state == "menu_categoria"


def test_identity_question_gets_fixed_reply(session):
    replies = handle_incoming(session, PHONE, "¿quién eres?")

    assert "agente" in replies[0].lower()


def test_annoyed_client_is_escalated_to_human(session):
    handle_incoming(session, PHONE, "Hola")

    replies = handle_incoming(session, PHONE, "estoy muy molesto con su servicio")

    assert "asesor" in replies[0].lower()
    ticket = session.exec(select(Ticket).where(Ticket.phone == PHONE)).first()
    assert ticket is not None and ticket.escalado is True

    conv = session.get(Conversation, PHONE)
    assert conv.state == "escalado"


def test_escalation_keyword_from_any_state(session):
    handle_incoming(session, PHONE, "Hola")

    replies = handle_incoming(session, PHONE, "quiero hablar con un asesor")
    assert "asesor" in replies[0].lower()

    ticket = session.exec(select(Ticket).where(Ticket.phone == PHONE)).first()
    assert ticket is not None
    assert ticket.escalado is True

    conv = session.get(Conversation, PHONE)
    assert conv.state == "escalado"

    # tras escalar (ticket sin asignar), el bot nunca deja sin respuesta
    replies = handle_incoming(session, PHONE, "sigo esperando")
    assert replies != []
    assert "asesor" in replies[0].lower()


def test_two_failed_attempts_auto_escalates(session):
    handle_incoming(session, PHONE, "Hola")

    replies = handle_incoming(session, PHONE, "xyzqq zzz")  # intento 1
    assert "asesor" in replies[0].lower()  # ofrece el contacto humano

    replies = handle_incoming(session, PHONE, "wwwk qqq")  # intento 2 -> escala
    conv = session.get(Conversation, PHONE)
    assert conv.state == "escalado"

    ticket = session.exec(select(Ticket).where(Ticket.phone == PHONE)).first()
    assert ticket is not None
    assert ticket.escalado is True


def test_media_message_is_not_processed(session):
    replies = handle_incoming(session, PHONE, "", is_media=True)
    assert "documentos" in replies[0].lower()

    conv = session.get(Conversation, PHONE)
    assert conv.state == "inicio"  # no cambia de estado por un adjunto


def test_menu_keyword_shows_listing_from_any_state(session):
    handle_incoming(session, PHONE, "Hola")

    replies = handle_incoming(session, PHONE, "menu")
    assert "1." in replies[0]

    conv = session.get(Conversation, PHONE)
    assert conv.state == "menu_categoria"


def test_numeric_flow_still_works_after_listing(session):
    handle_incoming(session, PHONE, "Hola")
    handle_incoming(session, PHONE, "servicios")  # muestra categorías

    replies = handle_incoming(session, PHONE, "1")  # Servicios básicos
    assert "Acta de nacimiento" in replies[0]

    replies = handle_incoming(session, PHONE, "0")  # volver
    assert "1." in replies[0]
    conv = session.get(Conversation, PHONE)
    assert conv.state == "menu_categoria"


def test_greeting_after_closed_ticket_is_cordial_short(session):
    handle_incoming(session, PHONE, "necesito acta de nacimiento")
    handle_incoming(session, PHONE, "sí")  # folio creado, estado vuelve a menu_categoria

    replies = handle_incoming(session, PHONE, "hola")

    assert "no logré" not in replies[0].lower()
    assert "ayud" in replies[0].lower()
    assert "1." not in replies[0]  # el saludo no lista servicios


def test_escalated_user_resumes_bot_with_menu_if_ticket_unclaimed(session):
    handle_incoming(session, PHONE, "Hola")
    handle_incoming(session, PHONE, "quiero hablar con un asesor")

    conv = session.get(Conversation, PHONE)
    assert conv.state == "escalado"

    replies = handle_incoming(session, PHONE, "menu")
    assert replies != []
    assert "1." in replies[0]

    conv = session.get(Conversation, PHONE)
    assert conv.state == "menu_categoria"


def test_escalated_user_stays_silent_if_ticket_already_claimed(session):
    handle_incoming(session, PHONE, "Hola")
    handle_incoming(session, PHONE, "quiero hablar con un asesor")

    ticket = session.exec(select(Ticket).where(Ticket.phone == PHONE)).first()
    ticket.assigned_to = "ana"
    session.add(ticket)
    session.commit()

    replies = handle_incoming(session, PHONE, "menu")
    assert replies == []

    conv = session.get(Conversation, PHONE)
    assert conv.state == "escalado"
