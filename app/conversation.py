import json
import logging
import re
from datetime import timedelta

from sqlmodel import Session, select

from app import nlu
from app.catalog import PAGE_SIZE, catalog
from app.models import (
    Client,
    Conversation,
    Direction,
    Feedback,
    Message,
    NluEvent,
    Ticket,
    TicketStatus,
    utcnow,
)
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
# Intención de estatus: se busca sobre el texto normalizado (minúsculas, sin
# acentos — ver nlu._norm), así "¿Cómo va?" == "como va". Palabras directas
# (estatus, avance, folio…) o giros comunes ("como va", "que paso con", "ya
# quedo"). A propósito NO incluye "mi solicitud/mi tramite" a secas: "quiero
# iniciar mi tramite de curp" debe ir a deducción de servicio, no a estatus.
_STATUS_INTENT_RE = re.compile(
    r"estatus|status|avance|seguimiento|folio"
    r"|como van?\b|en que va\b|en que quedo|que paso con|que ha pasado"
    r"|ya quedo|ya esta list|ya salio"
    r"|mis solicitudes|mis tramites|mis pedidos|mis casos"
)

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
IDENTITY_REPLY = (
    "Soy el agente virtual de Consultoría y Soluciones Empresariales, "
    "ayudo a nuestros clientes con sus trámites 🤖 ¿Qué necesitas?"
)
ANNOYANCE_REPLY = "Lamento mucho la molestia 🙏 Te comunico con un asesor ahora mismo."
NOT_RECOGNIZED_HINT = (
    "Quiero ayudarte bien 😊 ¿Me dices qué trámite o servicio necesitas? "
    "Escribe *servicios* para ver la lista o *asesor* para hablar con una persona."
)
# Pedidos de informes/cotización sin trámite concreto — se responden con
# calidez, nunca con el aviso de "no te entendí" (comparar sin acentos).
INFO_KEYWORDS = [
    "informes", "informacion", "cotiza", "presupuesto",
    "precio", "costo", "cuanto cuesta", "cuanto sale",
]
INFO_REPLY = (
    "¡Con gusto te damos informes! 😊 Cuéntame qué trámite necesitas, "
    "o escribe *servicios* para ver la lista completa."
)
ESCALATED_WAIT_REPLY = (
    "Recibimos tu mensaje. Un asesor te contactará muy pronto — gracias por tu paciencia.\n"
    "Si mientras tanto quieres usar el menú automático, escribe *menu*."
)
NO_TICKETS_REPLY = "Aún no tienes ninguna solicitud registrada. Escribe *servicios* para ver la lista."
STATUS_LABELS = {
    TicketStatus.NUEVO: "recibido, en espera de asignación",
    TicketStatus.TOMADO: "en atención",
    TicketStatus.EN_PROCESO: "en proceso",
    TicketStatus.CERRADO: "completado ✅",
}
STATUS_LIST_LIMIT = 5
# Selección múltiple pura en un menú: "1 y 2", "1,3", "2 4", "1y2".
_MULTI_PICK_RE = re.compile(r"\d+(?:(?:\s*(?:[,;+&]|y|e)\s*|\s+)\d+)+")

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
    elif (fb := FEEDBACK_RE.match(text.strip())):
        # Botón 👍/👎 del aviso de cierre: se registra y se agradece, en
        # cualquier estado (no pasa por la máquina de estados ni por nluevent).
        replies = _record_feedback(session, phone, int(fb.group(1)), fb.group(2))
    elif conv.state == "escalado":
        normalized = text.strip().lower()
        ticket = _latest_open_ticket(session, phone)
        if ticket is not None and not ticket.agent_active:
            # El colaborador asignado apagó el agente explícitamente para este
            # ticket: silencio total, ni siquiera responde el estatus. Tomar
            # el ticket (asignarlo) YA NO basta para callar al bot por sí solo.
            replies = []
        elif normalized in MENU_KEYWORDS:
            _set_state(session, conv, "menu_categoria")
            replies = ["Claro, continuemos aquí. " + catalog.format_category_menu()]
        elif _asks_status(text):
            replies = [_status_reply(session, phone)]
        else:
            # Aunque esté esperando a un asesor, un pedido concreto de trámite
            # se atiende (deducción, multi-servicio o mini-menú de parecidos):
            # el "espera" enlatado es solo para mensajes sin petición clara.
            replies = _try_service_request(session, conv, text) or [ESCALATED_WAIT_REPLY]
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


def _latest_open_ticket(session: Session, phone: str) -> Ticket | None:
    return session.exec(
        select(Ticket)
        .where(Ticket.phone == phone)
        .where(Ticket.status != TicketStatus.CERRADO)
        .order_by(Ticket.created_at.desc())
    ).first()


def _asks_status(text: str) -> bool:
    return _STATUS_INTENT_RE.search(nlu._norm(text)) is not None


def _status_reply(session: Session, phone: str) -> str:
    tickets = session.exec(
        select(Ticket)
        .where(Ticket.phone == phone)
        .where(Ticket.habilitado == True)  # noqa: E712
        .where(Ticket.service_id != "escalado")  # ese ticket es interno, no un trámite real
        .order_by(Ticket.created_at.desc())
        .limit(STATUS_LIST_LIMIT)
    ).all()
    if not tickets:
        return NO_TICKETS_REPLY
    lines = "\n".join(f"• {t.service_name} (folio #{t.id}): {STATUS_LABELS[t.status]}" for t in tickets)
    return f"Estatus de tus solicitudes:\n{lines}"


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

    # "¿Cómo va mi trámite?" — funciona desde cualquier estado, sin tocarlo.
    if _asks_status(text):
        return [_status_reply(session, client.phone)]

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
        "eligiendo_sugerencia": _handle_sugerencia,
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

    # Varios servicios en un mismo mensaje ("quiero acta de nacimiento y mi
    # curp"): se confirman juntos y se crea un ticket por cada uno.
    multi, service, category, options = _deduce_and_log(session, client.phone, text)
    if multi:
        _set_state(session, conv, "confirmar", {"pending_service_ids": [s.id for s in multi]})
        return ["¡Hola! 😊 " + _confirm_multi_message(multi)]

    # Regla: si el primer mensaje ya trae el servicio ("necesito mi CURP, soy
    # Juan…"), no se listan servicios: saludo breve y directo a confirmar.
    if service is not None:
        _set_state(session, conv, "confirmar", {"pending_service_ids": [service.id]})
        return ["¡Hola! 😊 " + _confirm_message(service)]
    if category is not None:
        _set_state(session, conv, "menu_servicio", {"category_id": category.id, "page": 0})
        return [catalog.format_service_menu(category.id)]

    # "quiero un acta": una palabra que coincide con varios servicios no debe
    # terminar en "no te entendí" — se ofrece el mini-menú de parecidos.
    if options:
        return _suggest_reply(session, conv, options)

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

    # "1 y 2" con categorías: se atienden una por una (sin cola — el cliente
    # vuelve con *menu* al terminar la primera).
    indices = _parse_indices(normalized)
    if indices:
        picked = [c for c in (catalog.category_by_index(i) for i in indices) if c is not None]
        if picked:
            first = picked[0]
            _set_state(session, conv, "menu_servicio", {"category_id": first.id, "page": 0})
            menu = catalog.format_service_menu(first.id)
            if len(picked) > 1:
                menu = (
                    f"Vamos una por una 😊 Empecemos con {first.name}; al terminar, "
                    "escribe *menu* para la siguiente.\n" + menu
                )
            return [menu]

    multi, service, category, options = _deduce_and_log(session, client.phone, text)
    if multi:
        _set_state(session, conv, "confirmar", {"pending_service_ids": [s.id for s in multi]})
        return [_confirm_multi_message(multi)]
    if service is not None:
        _set_state(session, conv, "confirmar", {"pending_service_ids": [service.id]})
        return [_confirm_message(service)]
    if category is not None:
        _set_state(session, conv, "menu_servicio", {"category_id": category.id, "page": 0})
        return [catalog.format_service_menu(category.id)]

    return _not_understood(session, client, conv, text, options=options)


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
            _set_state(session, conv, "confirmar", {"pending_service_ids": [service.id]})
            return [_confirm_message(service)]

    # "1 y 2": varios servicios del mismo menú en un solo mensaje.
    indices = _parse_indices(normalized)
    if indices:
        picked = _unique_services(
            catalog.service_by_index(category_id, i) for i in indices
        )
        if picked:
            missing = [
                str(i) for i in indices if catalog.service_by_index(category_id, i) is None
            ]
            note = f"No encontré la opción {', '.join(missing)}. " if missing else ""
            _set_state(session, conv, "confirmar", {"pending_service_ids": [s.id for s in picked]})
            msg = _confirm_multi_message(picked) if len(picked) > 1 else _confirm_message(picked[0])
            return [note + msg]

    # Texto libre dentro del submenú: intentar deducir igual que en el menú
    # principal ("mejor quiero una CURP" debe funcionar desde cualquier lado).
    multi, service, deduced_category, options = _deduce_and_log(session, client.phone, text)
    if multi:
        _set_state(session, conv, "confirmar", {"pending_service_ids": [s.id for s in multi]})
        return [_confirm_multi_message(multi)]
    if service is not None:
        _set_state(session, conv, "confirmar", {"pending_service_ids": [service.id]})
        return [_confirm_message(service)]
    if deduced_category is not None and deduced_category.id != category_id:
        _set_state(session, conv, "menu_servicio", {"category_id": deduced_category.id, "page": 0})
        return [catalog.format_service_menu(deduced_category.id)]

    return _not_understood(session, client, conv, text, options=options)


def _handle_confirmar(session: Session, client: Client, conv: Conversation, text: str, normalized: str) -> list[str]:
    context = _get_context(conv)
    ids = context.get("pending_service_ids") or (
        # Compatibilidad con confirmaciones en vuelo del formato anterior.
        [context["pending_service_id"]] if context.get("pending_service_id") else []
    )
    services = [s for s in (catalog.service_by_id(i) for i in ids) if s is not None]

    if normalized in YES_WORDS and services:
        tickets = []
        for service in services:
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
            tickets.append(ticket)
        _mark_nlu_ticket(session, client.phone)
        session.commit()
        for ticket in tickets:
            session.refresh(ticket)
        _set_state(session, conv, "menu_categoria")
        if len(tickets) == 1:
            return [
                f"¡Listo! ✅ Registré tu solicitud de '{tickets[0].service_name}' "
                f"(folio #{tickets[0].id}). "
                "Un asesor te contactará pronto, gracias por tu confianza."
            ]
        lines = "\n".join(f"• '{t.service_name}' — folio #{t.id}" for t in tickets)
        return [
            f"¡Listo! ✅ Registré tus {len(tickets)} solicitudes:\n{lines}\n"
            "Un asesor te contactará pronto. ¿Otro trámite? Escribe *menu*."
        ]

    if normalized in NO_WORDS:
        _set_state(session, conv, "menu_categoria")
        return ["Sin problema. " + catalog.format_category_menu()]

    if len(services) > 1:
        confirm_text = _confirm_multi_message(services)
    elif services:
        confirm_text = _confirm_message(services[0])
    else:
        confirm_text = catalog.format_category_menu()
    return _bump_or_escalate(session, client, conv, text, "¿Puedes confirmar con 'sí' o 'no'?\n" + confirm_text)


# Botones 👍/👎 del aviso de cierre (ids emitidos por dashboard._notify_finish).
FEEDBACK_RE = re.compile(r"^fb:(\d+):(up|down)$")
FEEDBACK_THANKS = {
    "up": "¡Gracias por tu opinión! 🙌 Nos alegra haberte ayudado.",
    "down": "Gracias por decírnoslo 🙏 Vamos a revisar tu caso para mejorar.",
}


def _record_feedback(session: Session, phone: str, ticket_id: int, rating: str) -> list[str]:
    ticket = session.get(Ticket, ticket_id)
    if ticket is None or ticket.phone != phone:
        # Botón viejo o de otro chat: se agradece sin registrar nada.
        return [FEEDBACK_THANKS[rating]]
    existing = session.exec(
        select(Feedback).where(Feedback.ticket_id == ticket_id)
    ).first()
    if existing is None:
        session.add(Feedback(ticket_id=ticket_id, phone=phone, rating=rating))
    return [FEEDBACK_THANKS[rating]]


# ---- Capa de presentación WhatsApp (listas y botones nativos) ----
# La máquina de estados sigue hablando en texto plano (lo que se guarda y
# espeja); aquí solo se decide CÓMO se ve la última respuesta en WhatsApp.

MENU_MARKER = "esponde con el número"  # cubre "Responde/responde con el número"
LIST_BUTTON_LABEL = "Ver opciones"
CONFIRM_BUTTONS = [("si", "✅ Sí, confirmar"), ("no", "❌ No")]


def _menu_rows(conv: Conversation) -> list[tuple[str, str]]:
    """Filas (id, título) del menú según el estado; [] = quedarse en texto.
    El id es el número que el cliente habría tecleado."""
    context = _get_context(conv)
    if conv.state == "menu_categoria":
        return [(str(i), c.name) for i, c in enumerate(catalog.categories, start=1)][:10]
    if conv.state == "menu_servicio":
        category = catalog.category_by_id(context.get("category_id", ""))
        if category is None:
            return []
        page = context.get("page", 0)
        start = page * PAGE_SIZE
        chunk = category.services[start : start + PAGE_SIZE]
        if not chunk or len(chunk) > 8:
            return []  # no debería pasar con PAGE_SIZE=8; fallback defensivo a texto
        rows = [(str(start + i), s.name) for i, s in enumerate(chunk, start=1)]
        if start + PAGE_SIZE < len(category.services):
            # "mas" es lo que el cliente habría tecleado: pagina en nativo.
            rows.append(("mas", "➡ Ver más opciones"))
        rows.append(("0", "⬅ Volver al menú"))
        return rows
    if conv.state == "eligiendo_sugerencia":
        options = [
            s for s in (catalog.service_by_id(i) for i in context.get("suggestion_ids", []))
            if s is not None
        ]
        return [(str(i), s.name) for i, s in enumerate(options, start=1)][:10]
    return []


def presentation(session: Session, phone: str, replies: list[str]) -> list:
    """Convierte la última respuesta en mensaje interactivo según el estado:
    confirmación → botones Sí/No; menús → lista nativa. Devuelve strings
    (texto normal) y/o dicts {"text", "buttons"} | {"text", "list", "button"}."""
    if not replies:
        return list(replies)
    conv = session.get(Conversation, phone)
    if conv is None:
        return list(replies)

    out: list = list(replies)
    last = out[-1]
    if conv.state == "confirmar":
        out[-1] = {"text": last, "buttons": list(CONFIRM_BUTTONS)}
        return out
    if MENU_MARKER in last:
        rows = _menu_rows(conv)
        if rows:
            header = last.split("\n")[0]
            out[-1] = {
                "text": f"{header}\nElige una opción del menú 👇",
                "list": rows,
                "button": LIST_BUTTON_LABEL,
            }
    return out


# Mensajes de cortesía que no son un intento de pedir un trámite: no se
# registran en la bitácora del agente para no inflar los "no entendidos".
COURTESY_WORDS = {
    "gracias", "muchas gracias", "si gracias", "ok", "okay", "va", "vale",
    "listo", "perfecto", "de acuerdo", "buen dia", "buenas", "adios",
}


def _log_nlu(
    session: Session, phone: str, text: str, capa: str,
    service_id: str | None = None, confianza: float | None = None,
) -> None:
    normalized = nlu._norm(text)
    if len(normalized) < 3 or normalized in COURTESY_WORDS:
        return
    session.add(
        NluEvent(
            phone=phone, text=text[:200], capa=capa,
            service_id=service_id, confianza=confianza,
        )
    )


def _deduce_and_log(session: Session, phone: str, text: str):
    """Corre la cascada de deducción UNA sola vez por mensaje y deja
    constancia en nluevent (capa que entendió, o "ninguna"). Devuelve
    (multi, service, category, options) para que cada handler arme su UX."""
    multi = nlu.deduce_all(text)
    if multi:
        _log_nlu(session, phone, text, "multi", ",".join(s.id for s in multi))
        return multi, None, None, []
    service, category, capa, confianza = nlu.deduce_explained(text)
    if service is not None:
        _log_nlu(session, phone, text, capa, service.id, confianza)
        return [], service, None, []
    if category is not None:
        _log_nlu(session, phone, text, "categoria", category.id)
        return [], None, category, []
    options = nlu.suggestions(text)
    if options:
        _log_nlu(session, phone, text, "sugerencia", ",".join(s.id for s in options))
        return [], None, None, options
    _log_nlu(session, phone, text, "ninguna")
    return [], None, None, []


def _mark_nlu_ticket(session: Session, phone: str) -> None:
    """Marca la última deducción del cliente como convertida en solicitud.

    ponytail: sin ventana de tiempo — a este volumen, la última no convertida
    del mismo teléfono es la correcta; afinar con timestamp si algún día hay
    ambigüedad real."""
    event = session.exec(
        select(NluEvent)
        .where(NluEvent.phone == phone)
        .where(NluEvent.termino_en_ticket == False)  # noqa: E712
        .order_by(NluEvent.id.desc())
    ).first()
    if event is not None:
        event.termino_en_ticket = True
        session.add(event)


def _try_service_request(session: Session, conv: Conversation, text: str) -> list[str] | None:
    """Resuelve el texto como pedido de servicio: varios de golpe, uno claro,
    una categoría o parecidos por palabra clave. None si no pide nada."""
    multi, service, category, options = _deduce_and_log(session, conv.phone, text)
    if multi:
        _set_state(session, conv, "confirmar", {"pending_service_ids": [s.id for s in multi]})
        return [_confirm_multi_message(multi)]
    if service is not None:
        _set_state(session, conv, "confirmar", {"pending_service_ids": [service.id]})
        return [_confirm_message(service)]
    if category is not None:
        _set_state(session, conv, "menu_servicio", {"category_id": category.id, "page": 0})
        return [catalog.format_service_menu(category.id)]
    if options:
        return _suggest_reply(session, conv, options)
    return None


def _suggest_reply(session: Session, conv: Conversation, options: list) -> list[str]:
    if len(options) == 1:
        _set_state(session, conv, "confirmar", {"pending_service_ids": [options[0].id]})
        return [_confirm_message(options[0])]
    _set_state(
        session, conv, "eligiendo_sugerencia", {"suggestion_ids": [s.id for s in options]}
    )
    lines = "\n".join(f"{i}. {s.name}" for i, s in enumerate(options, 1))
    return [
        "Tenemos varios trámites parecidos 😊 ¿Cuál necesitas? Responde con el número:\n" + lines
    ]


def _asks_info(text: str) -> bool:
    folded = nlu._norm(text)
    return any(keyword in folded for keyword in INFO_KEYWORDS)


def _not_understood(
    session: Session, client: Client, conv: Conversation, text: str,
    options: list | None = None,
) -> list[str]:
    # options: sugerencias ya calculadas (y registradas) por _deduce_and_log;
    # None = calcularlas aquí (para llamadas fuera de la cascada instrumentada).
    if options is None:
        options = nlu.suggestions(text)
    if options:
        return _suggest_reply(session, conv, options)
    if _asks_info(text):
        # Pedir informes es interés, no un intento fallido: no suma al
        # contador de reintentos ni suena a regaño.
        _set_state(session, conv, "menu_categoria")
        return [INFO_REPLY]
    return _bump_or_escalate(session, client, conv, text, NOT_RECOGNIZED_HINT)


def _handle_sugerencia(session: Session, client: Client, conv: Conversation, text: str, normalized: str) -> list[str]:
    context = _get_context(conv)
    options = [
        s for s in (catalog.service_by_id(i) for i in context.get("suggestion_ids", []))
        if s is not None
    ]
    idx = _parse_int(normalized)
    picks = [idx] if idx is not None else _parse_indices(normalized)
    if picks and options:
        chosen = _unique_services(options[i - 1] for i in picks if 1 <= i <= len(options))
        if chosen:
            _set_state(session, conv, "confirmar", {"pending_service_ids": [s.id for s in chosen]})
            if len(chosen) > 1:
                return [_confirm_multi_message(chosen)]
            return [_confirm_message(chosen[0])]
    # Cualquier otra cosa se procesa como texto libre normal.
    return _handle_menu_categoria(session, client, conv, text, normalized)


def _confirm_message(service) -> str:
    # El precio sale SOLO del catálogo — el agente nunca inventa precios.
    return f"¿Confirmas '{service.name}' ({service.price_info})? Responde sí o no."


def _confirm_multi_message(services) -> str:
    lines = "\n".join(f"• {s.name} ({s.price_info})" for s in services)
    return f"¿Confirmas estos {len(services)} trámites?\n{lines}\nResponde sí o no."


def _parse_indices(normalized: str) -> list[int]:
    """"1 y 2", "1,3", "2 4" → [1, 2]. Solo aplica si el texto completo es una
    selección múltiple (números y separadores); en otro caso devuelve []."""
    if not _MULTI_PICK_RE.fullmatch(normalized.strip()):
        return []
    indices: list[int] = []
    for n in re.findall(r"\d+", normalized):
        value = int(n)
        if value not in indices:
            indices.append(value)
    return indices[: nlu.MULTI_MAX_SERVICES]


def _unique_services(candidates) -> list:
    """Filtra None y deduplica por id preservando el orden."""
    picked: list = []
    seen: set[str] = set()
    for service in candidates:
        if service is not None and service.id not in seen:
            seen.add(service.id)
            picked.append(service)
    return picked


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
    open_ticket = _latest_open_ticket(session, client.phone)

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
