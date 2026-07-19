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

    # tras escalar (ticket sin asignar), el bot nunca deja sin respuesta
    replies = handle_incoming(session, PHONE, "sigo esperando")
    assert replies != []
    assert "asesor" in replies[0].lower()


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


def test_menu_keyword_resets_from_any_state(session):
    handle_incoming(session, PHONE, "Hola")
    handle_incoming(session, PHONE, "Juan Pérez")
    handle_incoming(session, PHONE, "1")  # entra al submenú de Servicios básicos

    replies = handle_incoming(session, PHONE, "menu")
    assert "1." in replies[0]  # de vuelta al menú de categorías

    conv = session.get(Conversation, PHONE)
    assert conv.state == "menu_categoria"


def test_zero_goes_back_to_category_menu_from_service_menu(session):
    handle_incoming(session, PHONE, "Hola")
    handle_incoming(session, PHONE, "Juan Pérez")
    handle_incoming(session, PHONE, "1")  # submenú de Servicios básicos

    replies = handle_incoming(session, PHONE, "0")
    assert "1." in replies[0]

    conv = session.get(Conversation, PHONE)
    assert conv.state == "menu_categoria"


def test_confused_user_no_longer_escalates_if_they_use_menu(session):
    # Reproduce el caso real: el cliente se confunde en el submenú y en vez de
    # escalar automáticamente por 2 respuestas inválidas, usa "menu" para salir.
    handle_incoming(session, PHONE, "Hola")
    handle_incoming(session, PHONE, "Leticia Diaz")
    handle_incoming(session, PHONE, "1")  # submenú de Servicios básicos

    replies = handle_incoming(session, PHONE, "menu")
    assert "asesor" not in replies[0].lower()

    conv = session.get(Conversation, PHONE)
    assert conv.state == "menu_categoria"


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


def test_greeting_after_closed_ticket_is_cordial_not_confused(session):
    handle_incoming(session, PHONE, "Hola")
    handle_incoming(session, PHONE, "Leticia Diaz")
    handle_incoming(session, PHONE, "1")
    handle_incoming(session, PHONE, "1")
    handle_incoming(session, PHONE, "sí")  # folio + despedida, estado vuelve a menu_categoria

    replies = handle_incoming(session, PHONE, "hola")
    assert "no reconocí" not in replies[0].lower()
    assert "leticia diaz" in replies[0].lower()
    assert "1." in replies[0]  # muestra el menú de categorías


def test_greeting_mid_submenu_is_cordial(session):
    handle_incoming(session, PHONE, "Hola")
    handle_incoming(session, PHONE, "Ana")
    handle_incoming(session, PHONE, "1")  # entra al submenú

    replies = handle_incoming(session, PHONE, "buenas tardes")
    assert "no reconocí" not in replies[0].lower()
    assert "1." in replies[0]

    conv = session.get(Conversation, PHONE)
    assert conv.state == "menu_categoria"


def test_first_invalid_option_offers_agent_contact(session):
    handle_incoming(session, PHONE, "Hola")
    handle_incoming(session, PHONE, "Juan Pérez")

    replies = handle_incoming(session, PHONE, "xyz no reconocido")  # 1er intento, no escala aún

    assert "asesor" in replies[0].lower()
    conv = session.get(Conversation, PHONE)
    assert conv.state == "menu_categoria"  # sigue en el menú, no escaló


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
