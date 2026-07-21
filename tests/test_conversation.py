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


def test_escalated_user_keeps_getting_bot_replies_after_being_claimed(session):
    # Regla: tomar el ticket NO apaga el bot por sí solo — solo lo hace el
    # colaborador de forma explícita (agent_active=False, ver test siguiente).
    handle_incoming(session, PHONE, "Hola")
    handle_incoming(session, PHONE, "quiero hablar con un asesor")

    ticket = session.exec(select(Ticket).where(Ticket.phone == PHONE)).first()
    ticket.assigned_to = "ana"
    session.add(ticket)
    session.commit()

    replies = handle_incoming(session, PHONE, "cualquier cosa")
    assert replies != []
    assert "asesor" in replies[0].lower()


def test_escalated_user_stays_silent_only_after_agent_explicitly_paused(session):
    handle_incoming(session, PHONE, "Hola")
    handle_incoming(session, PHONE, "quiero hablar con un asesor")

    ticket = session.exec(select(Ticket).where(Ticket.phone == PHONE)).first()
    ticket.assigned_to = "ana"
    ticket.agent_active = False
    session.add(ticket)
    session.commit()

    replies = handle_incoming(session, PHONE, "menu")
    assert replies == []


def test_status_query_lists_open_tickets_with_folio(session):
    handle_incoming(session, PHONE, "Necesito un acta de divorcio, soy Juan Pérez")
    handle_incoming(session, PHONE, "si")

    replies = handle_incoming(session, PHONE, "cual es el estatus de mi tramite")

    assert "Acta de divorcio" in replies[0]
    assert "folio #" in replies[0]


def test_status_query_understands_casual_phrasings(session):
    """Frases reales de clientes (captura 2026-07-21): "como va?" y
    "mi solicitud como va?" deben responder estatus, no escalar por error."""
    handle_incoming(session, PHONE, "Necesito un acta de divorcio, soy Juan Pérez")
    handle_incoming(session, PHONE, "si")

    for phrasing in ["como va?", "mi solicitud como va?", "¿Cómo va mi trámite?",
                     "que pasó con mi acta", "ya quedó?"]:
        replies = handle_incoming(session, PHONE, phrasing)
        assert "Acta de divorcio" in replies[0], phrasing
        assert "folio #" in replies[0], phrasing

    conv = session.get(Conversation, PHONE)
    assert conv.state != "escalado"  # preguntar estatus jamás escala


def test_ambiguous_keyword_shows_matching_services(session):
    """Captura 2026-07-21: "quiero un acta" debe listar las actas disponibles,
    no rendirse con "no logré identificar el trámite"."""
    handle_incoming(session, PHONE, "hola")

    replies = handle_incoming(session, PHONE, "quiero un acta")
    assert "parecidos" in replies[0]
    for name in ["Acta de nacimiento", "Acta de matrimonio",
                 "Acta de defunción", "Acta de divorcio"]:
        assert name in replies[0]

    replies = handle_incoming(session, PHONE, "2")
    assert "Acta de matrimonio" in replies[0]
    assert "confirmas" in replies[0].lower()

    handle_incoming(session, PHONE, "sí")
    ticket = session.exec(select(Ticket).where(Ticket.phone == PHONE)).first()
    assert ticket.service_name == "Acta de matrimonio"


def test_ambiguous_keyword_works_as_first_message(session):
    replies = handle_incoming(session, PHONE, "quiero un acta")

    assert "parecidos" in replies[0]


def test_suggestion_menu_accepts_multi_pick(session):
    handle_incoming(session, PHONE, "quiero un acta")

    replies = handle_incoming(session, PHONE, "1 y 4")

    assert "2 trámites" in replies[0]
    assert "Acta de nacimiento" in replies[0]
    assert "Acta de divorcio" in replies[0]


def test_escalated_client_asking_for_service_gets_attended(session):
    """Captura 2026-07-21 (2): estando escalado, "quiero un acta" recibía dos
    veces el 'espera' enlatado — debe ofrecer el mini-menú y registrar."""
    handle_incoming(session, PHONE, "quiero hablar con un asesor")
    conv = session.get(Conversation, PHONE)
    assert conv.state == "escalado"

    replies = handle_incoming(session, PHONE, "hola quiero una acta")
    assert "parecidos" in replies[0]
    assert "Acta de nacimiento" in replies[0]

    handle_incoming(session, PHONE, "1")
    replies = handle_incoming(session, PHONE, "sí")
    assert "folio" in replies[0].lower()


def test_escalated_client_with_direct_service_goes_to_confirm(session):
    handle_incoming(session, PHONE, "quiero hablar con un asesor")

    replies = handle_incoming(session, PHONE, "necesito un acta de divorcio")

    assert "Acta de divorcio" in replies[0]
    assert "confirmas" in replies[0].lower()


def test_escalated_client_with_vague_message_still_gets_wait_reply(session):
    handle_incoming(session, PHONE, "quiero hablar con un asesor")

    replies = handle_incoming(session, PHONE, "gracias, aquí espero")

    assert "asesor te contactará" in replies[0].lower()


def test_gibberish_still_gets_not_recognized_hint(session):
    handle_incoming(session, PHONE, "hola")

    replies = handle_incoming(session, PHONE, "xyzk qwrt")

    assert "no logré identificar" in replies[0].lower()


def test_starting_a_new_procedure_is_not_mistaken_for_status(session):
    replies = handle_incoming(session, PHONE, "quiero iniciar mi tramite")

    assert "no tienes ninguna solicitud" not in replies[0].lower()


def test_status_query_with_no_tickets_redirects_to_services(session):
    replies = handle_incoming(session, PHONE, "cual es el estatus de mi solicitud")

    assert "no tienes ninguna solicitud" in replies[0].lower()


def test_status_query_works_while_escalated_and_agent_active(session):
    handle_incoming(session, PHONE, "Necesito un acta de divorcio, soy Juan Pérez")
    handle_incoming(session, PHONE, "si")
    handle_incoming(session, PHONE, "quiero hablar con un asesor")

    replies = handle_incoming(session, PHONE, "estatus")

    assert "Acta de divorcio" in replies[0]


def test_status_query_blocked_when_agent_explicitly_paused(session):
    handle_incoming(session, PHONE, "Necesito un acta de divorcio, soy Juan Pérez")
    handle_incoming(session, PHONE, "si")
    handle_incoming(session, PHONE, "quiero hablar con un asesor")

    ticket = session.exec(select(Ticket).where(Ticket.phone == PHONE)).first()
    ticket.assigned_to = "ana"
    ticket.agent_active = False
    session.add(ticket)
    session.commit()

    replies = handle_incoming(session, PHONE, "estatus")
    assert replies == []

    conv = session.get(Conversation, PHONE)
    assert conv.state == "escalado"
