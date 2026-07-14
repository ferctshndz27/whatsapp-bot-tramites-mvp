import json
import logging
from datetime import timedelta

from sqlmodel import Session, select

from app.catalog import catalog
from app.models import Client, Conversation, Direction, Message, Ticket, TicketStatus, utcnow
from app.utils import mask_phone

logger = logging.getLogger(__name__)

ESCALATION_KEYWORDS = [
    "asesor",
    "humano",
    "persona",
    "queja",
    "reclamo",
    "hablar con alguien",
    "hablar con una persona",
]

YES_WORDS = {"si", "sí", "s", "yes", "confirmo"}
NO_WORDS = {"no", "n"}
MAX_FAILED_ATTEMPTS = 2
MAX_STORED_TEXT = 4000
MAX_NOTE_TEXT = 500
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX_MESSAGES = 20


def handle_incoming(
    session: Session,
    phone: str,
    text: str,
    is_media: bool = False,
    wa_message_id: str | None = None,
) -> list[str]:
    if wa_message_id and _already_processed(session, wa_message_id):
        logger.info("Mensaje duplicado ignorado (reintento de Meta): %s", wa_message_id)
        return []

    client = _get_or_create_client(session, phone)
    conv = _get_or_create_conversation(session, phone)
    _log_message(
        session, phone, Direction.IN, "[archivo adjunto]" if is_media else text, wa_message_id
    )

    if _is_rate_limited(session, phone):
        logger.warning("Rate limit alcanzado para %s — mensaje ignorado", mask_phone(phone))
        return []

    if is_media:
        replies = ["Por ahora no puedo recibir documentos en este chat. Un asesor te indicará cómo enviarlos."]
    elif conv.state == "escalado":
        replies = []
    else:
        normalized = text.strip().lower()
        if _is_escalation(normalized):
            replies = _escalate(session, client, conv, text)
        else:
            replies = _dispatch(session, client, conv, text, normalized)

    for reply in replies:
        _log_message(session, phone, Direction.OUT, reply)
    return replies


def _already_processed(session: Session, wa_message_id: str) -> bool:
    return (
        session.exec(
            select(Message)
            .where(Message.wa_message_id == wa_message_id)
            .where(Message.direction == Direction.IN)
        ).first()
        is not None
    )


def _is_rate_limited(session: Session, phone: str) -> bool:
    cutoff = utcnow() - timedelta(seconds=RATE_LIMIT_WINDOW_SECONDS)
    recent = session.exec(
        select(Message)
        .where(Message.phone == phone)
        .where(Message.direction == Direction.IN)
        .where(Message.created_at >= cutoff)
    ).all()
    return len(recent) > RATE_LIMIT_MAX_MESSAGES


def _dispatch(session: Session, client: Client, conv: Conversation, text: str, normalized: str) -> list[str]:
    handlers = {
        "inicio": _handle_inicio,
        "esperando_nombre": _handle_esperando_nombre,
        "menu_categoria": _handle_menu_categoria,
        "menu_servicio": _handle_menu_servicio,
        "confirmar": _handle_confirmar,
    }
    handler = handlers.get(conv.state, _handle_inicio)
    return handler(session, client, conv, text, normalized)


def _handle_inicio(session: Session, client: Client, conv: Conversation, text: str, normalized: str) -> list[str]:
    if client.name:
        _set_state(session, conv, "menu_categoria")
        return [f"Hola de nuevo, {client.name}. {catalog.format_category_menu()}"]
    _set_state(session, conv, "esperando_nombre")
    return ["¡Hola! Para comenzar, ¿cuál es tu nombre completo?"]


def _handle_esperando_nombre(session: Session, client: Client, conv: Conversation, text: str, normalized: str) -> list[str]:
    name = text.strip()
    if len(name) < 2:
        return _bump_or_escalate(session, client, conv, text, "No entendí tu nombre, ¿puedes escribirlo de nuevo?")
    client.name = name
    session.add(client)
    _set_state(session, conv, "menu_categoria")
    return [f"Gracias, {name}. {catalog.format_category_menu()}"]


def _handle_menu_categoria(session: Session, client: Client, conv: Conversation, text: str, normalized: str) -> list[str]:
    idx = _parse_int(normalized)
    if idx is not None:
        category = catalog.category_by_index(idx)
        if category is not None:
            _set_state(session, conv, "menu_servicio", {"category_id": category.id, "page": 0})
            return [catalog.format_service_menu(category.id)]

    matches = catalog.search_by_keyword(text)
    if len(matches) == 1:
        service = matches[0]
        _set_state(session, conv, "confirmar", {"pending_service_id": service.id})
        return [_confirm_message(service)]

    return _bump_or_escalate(
        session,
        client,
        conv,
        text,
        "No reconocí esa opción.\n" + catalog.format_category_menu(),
    )


def _handle_menu_servicio(session: Session, client: Client, conv: Conversation, text: str, normalized: str) -> list[str]:
    context = _get_context(conv)
    category_id = context.get("category_id")

    if normalized in {"mas", "más"}:
        page = context.get("page", 0) + 1
        menu = catalog.format_service_menu(category_id, page)
        if menu:
            _set_state(session, conv, "menu_servicio", {"category_id": category_id, "page": page})
            return [menu]
        return _bump_or_escalate(session, client, conv, text, "No hay más opciones en esa categoría.")

    idx = _parse_int(normalized)
    if idx is not None:
        service = catalog.service_by_index(category_id, idx)
        if service is not None:
            _set_state(session, conv, "confirmar", {"pending_service_id": service.id})
            return [_confirm_message(service)]

    category = catalog.category_by_id(category_id)
    reprompt = catalog.format_service_menu(category_id, context.get("page", 0)) if category else catalog.format_category_menu()
    return _bump_or_escalate(session, client, conv, text, "No reconocí esa opción.\n" + reprompt)


def _handle_confirmar(session: Session, client: Client, conv: Conversation, text: str, normalized: str) -> list[str]:
    context = _get_context(conv)
    service_id = context.get("pending_service_id")
    service = catalog.service_by_id(service_id) if service_id else None

    if normalized in YES_WORDS and service is not None:
        ticket = Ticket(
            phone=client.phone,
            client_name=client.name,
            category=service.category_name,
            service_id=service.id,
            service_name=service.name,
            price_info=service.price_info,
            status=TicketStatus.NUEVO,
        )
        session.add(ticket)
        session.commit()
        session.refresh(ticket)
        _set_state(session, conv, "menu_categoria")
        return [
            f"Listo, registré tu solicitud de '{service.name}' (folio #{ticket.id}). "
            "Un asesor te contactará en breve.\n\n" + catalog.format_category_menu()
        ]

    if normalized in NO_WORDS:
        _set_state(session, conv, "menu_categoria")
        return ["Sin problema. " + catalog.format_category_menu()]

    confirm_text = _confirm_message(service) if service else catalog.format_category_menu()
    return _bump_or_escalate(session, client, conv, text, "¿Puedes confirmar con 'sí' o 'no'?\n" + confirm_text)


def _confirm_message(service) -> str:
    return f"Confirmas '{service.name}' ({service.price_info})? Responde 'sí' o 'no'."


def _is_escalation(normalized: str) -> bool:
    return any(keyword in normalized for keyword in ESCALATION_KEYWORDS)


def _escalate(session: Session, client: Client, conv: Conversation, original_text: str) -> list[str]:
    open_ticket = session.exec(
        select(Ticket)
        .where(Ticket.phone == client.phone)
        .where(Ticket.status != TicketStatus.CERRADO)
        .order_by(Ticket.created_at.desc())
    ).first()

    if open_ticket is not None:
        open_ticket.escalado = True
        session.add(open_ticket)
    else:
        ticket = Ticket(
            phone=client.phone,
            client_name=client.name,
            category="(escalado)",
            service_id="escalado",
            service_name="Escalado a asesor",
            price_info="N/A",
            status=TicketStatus.NUEVO,
            escalado=True,
            notes=f"Cliente pidió hablar con un asesor. Último mensaje: {original_text[:MAX_NOTE_TEXT]}",
        )
        session.add(ticket)

    _set_state(session, conv, "escalado")
    return ["Un asesor te contactará en breve."]


def _bump_or_escalate(session: Session, client: Client, conv: Conversation, original_text: str, reprompt: str) -> list[str]:
    conv.failed_attempts += 1
    if conv.failed_attempts >= MAX_FAILED_ATTEMPTS:
        conv.failed_attempts = 0
        return _escalate(session, client, conv, original_text)
    session.add(conv)
    session.commit()
    return [reprompt]


def _set_state(session: Session, conv: Conversation, state: str, context: dict | None = None) -> None:
    conv.state = state
    conv.context_json = json.dumps(context or {})
    conv.failed_attempts = 0
    session.add(conv)
    session.commit()


def _get_context(conv: Conversation) -> dict:
    try:
        return json.loads(conv.context_json)
    except (json.JSONDecodeError, TypeError):
        return {}


def _parse_int(normalized: str) -> int | None:
    return int(normalized) if normalized.isdigit() else None


def _get_or_create_client(session: Session, phone: str) -> Client:
    client = session.get(Client, phone)
    if client is None:
        client = Client(phone=phone)
        session.add(client)
        session.commit()
        session.refresh(client)
    return client


def _get_or_create_conversation(session: Session, phone: str) -> Conversation:
    conv = session.get(Conversation, phone)
    if conv is None:
        conv = Conversation(phone=phone)
        session.add(conv)
        session.commit()
        session.refresh(conv)
    return conv


def _log_message(
    session: Session, phone: str, direction: Direction, body: str, wa_message_id: str | None = None
) -> None:
    session.add(
        Message(
            phone=phone, direction=direction, body=body[:MAX_STORED_TEXT], wa_message_id=wa_message_id
        )
    )
    session.commit()
