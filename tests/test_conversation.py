from sqlmodel import select

from app.conversation import handle_incoming
from app.models import Conversation, Ticket, TicketStatus

PHONE = "5215500000000"


def test_happy_path_creates_ticket(session):
    replies = handle_incoming(session, PHONE, "Hola")
    assert "nombre" in replies[0].lower()

    replies = handle_incoming(session, PHONE, "Juan Pérez")
    assert "1." in replies[0]  # menú de categorías

    replies = handle_incoming(session, PHONE, "1")  # Servicios básicos
    assert "Acta de nacimiento" in replies[0]

    replies = handle_incoming(session, PHONE, "1")  # Acta de nacimiento
    assert "confirmas" in replies[0].lower()

    replies = handle_incoming(session, PHONE, "sí")
    assert "folio" in replies[0].lower()

    ticket = session.exec(select(Ticket).where(Ticket.phone == PHONE)).first()
    assert ticket is not None
    assert ticket.service_name == "Acta de nacimiento"
    assert ticket.status == TicketStatus.NUEVO

    conv = session.get(Conversation, PHONE)
    assert conv.state == "menu_categoria"


def test_keyword_free_text_jumps_to_confirm(session):
    handle_incoming(session, PHONE, "Hola")
    handle_incoming(session, PHONE, "Ana López")

    replies = handle_incoming(session, PHONE, "acta de divorcio")
    assert "confirmas" in replies[0].lower()
    assert "Acta de divorcio" in replies[0]


def test_escalation_keyword_from_any_state(session):
    handle_incoming(session, PHONE, "Hola")

    replies = handle_incoming(session, PHONE, "quiero hablar con un asesor")
    assert "asesor" in replies[0].lower()

    ticket = session.exec(select(Ticket).where(Ticket.phone == PHONE)).first()
    assert ticket is not None
    assert ticket.escalado is True

    conv = session.get(Conversation, PHONE)
    assert conv.state == "escalado"

    # tras escalar, el bot ya no responde automáticamente
    replies = handle_incoming(session, PHONE, "sigo esperando")
    assert replies == []


def test_two_failed_attempts_auto_escalates(session):
    handle_incoming(session, PHONE, "Hola")
    handle_incoming(session, PHONE, "Juan Pérez")  # ahora en menu_categoria

    handle_incoming(session, PHONE, "xyz no reconocido")  # intento 1
    replies = handle_incoming(session, PHONE, "otro texto raro")  # intento 2 -> escala

    assert "asesor" in replies[0].lower()
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
