import json
import logging
from datetime import timedelta

from sqlmodel import Session, select

from app import nlu
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
MENU_KEYWORDS = {"menu", "menú", "inicio", "reiniciar"}
GREETING_WORDS = {
    "hola", "holaa", "holaaa", "buenas", "buen dia", "buen día",
    "buenos dias", "buenos días", "buenas tardes", "buenas noches",
    "hey", "que tal", "qué tal", "saludos",
}

# Reglas del agente (definidas por el negocio, 2026-07-19): siempre español,
# mensajes cortos (<30 palabras, máx. 2 emojis, sin listas salvo que pidan el
# catálogo), nunca inventar precios (solo los del catálogo), molestia → humano,
# saludo → pregunta abierta sin listar servicios, servicio directo → ticket.
ANNOYANCE_KEYWORDS = [
    "molesto", "molesta", "enojado", "enojada", "furioso", "furiosa",
    "queja", "quejarme", "pesimo", "pésimo", "mal servicio", "muy mal",
    "no sirve", "tardan mucho", "indignado", "indignada", "harto", "harta",
]
IDENTITY_KEYWORDS = [
    "quien eres", "quién eres", "eres un bot", "eres un robot",
    "eres humano", "con quien hablo", "con quién hablo",
]
SERVICES_KEYWORDS = [
    "servicios", "catalogo", "catálogo", "listado", "lista",
    "precios", "que hacen", "qué hacen", "que ofrecen", "qué ofrecen",
]
GREETING_REPLY = "¡Hola! 😊 ¿Cómo puedo ayudarte hoy?"
ASK_GOAL_REPLY = (
    "Cuéntame, ¿qué trámite o servicio necesitas? "
    "Escribe *servicios* si quieres ver la lista."
)
IDENTITY_REPLY = "Soy un agente colaborador que ayuda a los colaboradores 🤖 ¿Qué trámite necesitas?"
ANNOYANCE_REPLY = "Lamento mucho la molestia 🙏 Te comunico con un asesor ahora mismo."
NOT_RECOGNIZED_HINT = (
    "No logré identificar el trámite 🤔 Cuéntame qué necesitas, "
    "escribe *servicios* para ver la lista o *asesor* para hablar con una persona."
)
ESCALATED_WAIT_REPLY = (
    "Recibimos tu mensaje. Un asesor te contactará muy pronto — gracias por tu paciencia.\n"
    "Si mientras tanto quieres usar el menú automático, escribe *menu*."
)
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
        normalized = text.strip().lower()
        if _escalated_ticket_unclaimed(session, phone):
            if normalized in MENU_KEYWORDS:
                _set_state(session, conv, "menu_categoria")
                replies = ["Claro, continuemos aquí. " + catalog.format_category_menu()]
            else:
                # Nadie ha tomado el ticket aún: el bot nunca deja al cliente
                # sin respuesta mientras espera.
                replies = [ESCALATED_WAIT_REPLY]
        else:
            # Un asesor ya está atendiendo este chat desde el panel: el bot
            # guarda silencio para no interrumpir la conversación humana.
            replies = []
    else:
        normalized = text.strip().lower()
        if _mentions_any(normalized, ANNOYANCE_KEYWORDS):
            # Cliente molesto: disculpa breve y directo con un humano.
            replies = _escalate(session, client, conv, text, reply=ANNOYANCE_REPLY)
        elif _is_escalation(normalized):
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


def _escalated_ticket_unclaimed(session: Session, phone: str) -> bool:
    ticket = session.exec(
        select(Ticket)
        .where(Ticket.phone == phone)
        .where(Ticket.status != TicketStatus.CERRADO)
        .order_by(Ticket.created_at.desc())
    ).first()
    return ticket is None or ticket.assigned_to is None


def _dispatch(session: Session, client: Client, conv: Conversation, text: str, normalized: str) -> list[str]:
    if normalized in MENU_KEYWORDS:
        _set_state(session, conv, "menu_categoria")
        return [catalog.format_category_menu()]

    # "¿Quién eres?" → identidad fija, sin cambiar de estado.
    if _mentions_any(normalized, IDENTITY_KEYWORDS):
        return [IDENTITY_REPLY]

    # Pidieron el catálogo explícitamente: única situación donde sí se lista.
    if _mentions_any(normalized, SERVICES_KEYWORDS):
        _set_state(session, conv, "menu_categoria")
        return [catalog.format_category_menu()]

    # Un saludo en medio del flujo se responde con pregunta abierta, sin
    # listar servicios. Se excluye "inicio" (lo maneja su handler) y
    # "esperando_nombre" (para no confundir un saludo con el nombre real).
    if normalized in GREETING_WORDS and conv.state not in {"inicio", "esperando_nombre"}:
        _set_state(session, conv, "menu_categoria")
        saludo = (
            f"¡Hola de nuevo, {client.name}! ¿En qué te ayudo hoy?"
            if client.name
            else GREETING_REPLY
        )
        return [saludo]

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
    saludo = (
        f"¡Hola de nuevo, {client.name}! ¿En qué te ayudo hoy?"
        if client.name
        else GREETING_REPLY
    )
    if normalized in GREETING_WORDS:
        # Regla: al saludo se responde con pregunta abierta, sin listar servicios.
        _set_state(session, conv, "menu_categoria")
        return [saludo]

    # Regla: si el primer mensaje ya trae el servicio ("necesito mi CURP, soy
    # Juan…"), no se listan servicios: saludo breve y directo a confirmar.
    service, category = nlu.deduce(text)
    if service is not None:
        _set_state(session, conv, "confirmar", {"pending_service_id": service.id})
        return ["¡Hola! 😊 " + _confirm_message(service)]
    if category is not None:
        _set_state(session, conv, "menu_servicio", {"category_id": category.id, "page": 0})
        return [catalog.format_service_menu(category.id)]

    _set_state(session, conv, "menu_categoria")
    prefix = f"¡Hola, {client.name}! " if client.name else "¡Hola! 😊 "
    return [prefix + ASK_GOAL_REPLY]


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

    service, category = nlu.deduce(text)
    if service is not None:
        _set_state(session, conv, "confirmar", {"pending_service_id": service.id})
        return [_confirm_message(service)]
    if category is not None:
        _set_state(session, conv, "menu_servicio", {"category_id": category.id, "page": 0})
        return [catalog.format_service_menu(category.id)]

    return _bump_or_escalate(session, client, conv, text, NOT_RECOGNIZED_HINT)


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
    if idx == 0:
        _set_state(session, conv, "menu_categoria")
        return [catalog.format_category_menu()]
    if idx is not None:
        service = catalog.service_by_index(category_id, idx)
        if service is not None:
            _set_state(session, conv, "confirmar", {"pending_service_id": service.id})
            return [_confirm_message(service)]

    # Texto libre dentro del submenú: intentar deducir igual que en el menú
    # principal ("mejor quiero una CURP" debe funcionar desde cualquier lado).
    service, deduced_category = nlu.deduce(text)
    if service is not None:
        _set_state(session, conv, "confirmar", {"pending_service_id": service.id})
        return [_confirm_message(service)]
    if deduced_category is not None and deduced_category.id != category_id:
        _set_state(session, conv, "menu_servicio", {"category_id": deduced_category.id, "page": 0})
        return [catalog.format_service_menu(deduced_category.id)]

    return _bump_or_escalate(session, client, conv, text, NOT_RECOGNIZED_HINT)


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
            f"¡Listo! ✅ Registré tu solicitud de '{service.name}' (folio #{ticket.id}). "
            "Un asesor te contactará pronto, gracias por tu confianza."
        ]

    if normalized in NO_WORDS:
        _set_state(session, conv, "menu_categoria")
        return ["Sin problema. " + catalog.format_category_menu()]

    confirm_text = _confirm_message(service) if service else catalog.format_category_menu()
    return _bump_or_escalate(session, client, conv, text, "¿Puedes confirmar con 'sí' o 'no'?\n" + confirm_text)


def _confirm_message(service) -> str:
    # El precio sale SOLO del catálogo — el agente nunca inventa precios.
    return f"¿Confirmas '{service.name}' ({service.price_info})? Responde sí o no."


def _is_escalation(normalized: str) -> bool:
    return any(keyword in normalized for keyword in ESCALATION_KEYWORDS)


def _mentions_any(normalized: str, keywords: list[str]) -> bool:
    return any(keyword in normalized for keyword in keywords)


def _escalate(
    session: Session,
    client: Client,
    conv: Conversation,
    original_text: str,
    reply: str | None = None,
) -> list[str]:
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
    return [reply or "Un asesor te contactará en breve."]


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
