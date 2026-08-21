import json
import logging
import re
from datetime import timedelta, timezone

from sqlmodel import Session, select

from app import agent_tools, codigos, nlu, rag
from app.catalog import (
    PAGE_SIZE,
    catalog,
    parse_requirements,
    requisitos_del_ticket,
    requisitos_iniciales,
)
from app.config import settings
from app.dinero import formato_monto, parse_monto
from app.models import (
    CAPAS_EXTRA,
    PROVIDER_META,
    Client,
    ClientNote,
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

# ---- Trato social: saludos y despedidas ----
# Fuente única: de estos conjuntos salen tanto la respuesta del agente como el
# filtro de la bitácora nluevent. Un "hola buen día" no es un trámite: no debe
# contar como "no entendido" ni aparecer en los grupos del panel.
# Se comparan sobre el texto normalizado (minúsculas, sin acentos ni emojis —
# ver nlu._norm), así "¿Qué tal?" == "que tal".
GREETING_PHRASES = {
    "hola", "holaa", "holaaa", "holi", "ola", "buenas", "buen dia",
    "buenos dias", "buena tarde", "buenas tardes", "buena noche",
    "buenas noches", "hey", "que tal", "que onda", "que hay", "saludos",
    "como estas", "como esta", "como estan", "disculpa", "disculpe",
}
FAREWELL_PHRASES = {
    "gracias", "muchas gracias", "mil gracias", "gracias por todo",
    "gracias por su atencion", "muy amable", "muy amables", "adios",
    "hasta luego", "hasta pronto", "hasta manana", "nos vemos", "bye", "bay",
    "chao", "chau", "que tengas buen dia", "que tenga buen dia",
    "excelente dia", "buen fin de semana", "estamos en contacto",
    "que estes bien", "quedo atento", "quedo atenta",
    # Cierres de cortesía: no piden nada, cierran el turno.
    "ok", "okay", "oki", "va", "vale", "sale", "listo", "perfecto",
    "de acuerdo", "esta bien", "entendido", "enterado", "correcto",
}
# Frases largas primero: "que tengas buen dia" (despedida) debe recortarse
# antes que "buen dia" (saludo).
_SOCIAL_PHRASES = sorted(
    [(p, "saludo") for p in GREETING_PHRASES] + [(p, "despedida") for p in FAREWELL_PHRASES],
    key=lambda pair: -len(pair[0]),
)
# Relleno que puede acompañar a un saludo sin convertirlo en petición ("muchas
# gracias señor", "hola, buen día"). Ninguna palabra de servicio vive aquí: si
# algo distinto sobrevive al recorte, el mensaje sí pide algo.
_SOCIAL_FILLER = {
    "si", "no", "y", "e", "o", "a", "de", "del", "la", "el", "lo", "los",
    "las", "un", "una", "me", "te", "le", "les", "se", "su", "muy", "mucho",
    "muchas", "muchos", "pues", "ps", "por", "favor", "porfa", "porfavor",
    "senor", "senora", "senorita", "amigo", "amiga", "estimado", "estimada",
    "todo", "todos", "ya", "ah", "eh", "oye", "jaja", "jeje", "dia", "dias",
    "tarde", "tardes", "noche", "noches", "bien", "tenga", "tengas", "que",
}
_SOCIAL_WORD_RE = re.compile(r"[a-z0-9]+")
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
FAREWELL_REPLY = (
    "¡Gracias a ti! 😊 Que tengas excelente día. "
    "Aquí estaré si necesitas otro trámite."
)
# Parte estable del aviso: por ella se reconocen en la base los "no entendidos"
# YA enviados (app.synonym_miner). Vive aparte para que retocar el saludo de
# cortesía no borre el historial de 30 días del panel.
NOT_RECOGNIZED_MARKER = "¿Me dices qué trámite o servicio necesitas?"
NOT_RECOGNIZED_HINT = (
    f"Estoy para ayudarte 😊 {NOT_RECOGNIZED_MARKER} "
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

# ---- Captura de requisitos (estado "pidiendo_requisitos") ----
# Al registrar el folio, si el servicio tiene requisitos documentados en el
# catálogo el agente los pide uno por uno y guarda la respuesta EN EL TICKET,
# para que el asesor vea al tomarlo qué datos ya tiene y cuáles debe pedir.
TICKET_ALTA_CIERRE = "Su solicitud está siendo procesada, en breve le resolvemos."
TICKET_ALTA_CIERRE_MULTI = "Sus solicitudes están siendo procesadas, en breve le resolvemos."
REQ_PREGUNTA = (
    "Para avanzar con '{servicio}' (folio #{folio}) necesito *{requisito}*. "
    "¿Me lo compartes? Si no lo tienes a la mano, escribe *omitir*."
)
REQ_CIERRE = "¡Gracias! 🙌 Ya guardé tus datos. " + TICKET_ALTA_CIERRE
# Cuando el cliente mandó los códigos en el mismo mensaje del trámite, el
# agente NO se los vuelve a pedir — pero sí avisa que los tomó.
REQ_YA_CAPTURADO = "📎 Ya tomé los datos que me compartiste."
# La respuesta a un requisito que pide una clave (CURP/RFC/NSS) y no trae nada
# con esa forma ("." , "ya te lo mandé") se repregunta UNA vez: guardarla tal
# cual dejaba el folio con basura donde debía ir la clave.
REQ_REINTENTO = (
    "No reconocí *{requisito}* en tu respuesta 🤔 ¿Me la compartes completa? "
    "Si no la tienes a la mano, escribe *omitir*."
)
# El cliente puede saltarse un dato: se marca como faltante y lo pide el asesor.
OMIT_WORDS = {
    "omitir", "omitelo", "saltar", "despues", "luego", "no lo tengo",
    "no la tengo", "no lo se", "no la se", "no se", "no tengo",
}
# Tolerancia de cola en la omisión: "no lo tengo aun" / "no la tengo todavia"
# cuentan como omitir, pero "despues de buscar la encontre: ABC123" (cola larga:
# ahí viene el dato) no.
OMIT_TAIL_CHARS = 10
# Tope de preguntas encadenadas (multi-servicio): el rate limit son 20 mensajes
# entrantes por minuto y nadie contesta un interrogatorio.
MAX_COLA = 8
# Un requisito absurdamente largo pegado en el catálogo no debe reventar el
# envío (Meta rechaza cuerpos >4096): la pregunta se trunca, el asesor ve el
# texto íntegro en el folio.
REQ_MAX_CHARS = 300
# La captura caduca: si el cliente respondió días después, ese mensaje ya no es
# "el dato que le pedí" — se atiende como mensaje nuevo y lo faltante queda
# para el asesor.
CAPTURE_TTL_HOURS = 6
# Durante la captura la respuesta es texto libre y palabras como "persona" o
# "queja" aparecen dentro de datos legítimos ("persona fisica", "la queja 4412
# que levante"). El escape a humano sigue vivo, pero exige intención explícita
# (palabra completa), y la molestia solo dispara con términos inequívocos.
CAPTURE_ESCALATION_RE = re.compile(r"\basesora?\b|\bhumano\b|\bhablar con\b")
CAPTURE_ANNOYANCE = [
    "molesto", "molesta", "enojado", "enojada", "pesimo", "pésimo",
    "mal servicio", "no sirve", "harto", "harta", "indignado", "indignada",
]
_OMIT_PUNCT_RE = re.compile(r"[^\w\s]")


def handle_incoming(
    session: Session,
    phone: str,
    text: str,
    is_media: bool = False,
    wa_message_id: str | None = None,
    provider: str = PROVIDER_META,
) -> list[str]:
    if wa_message_id and _already_processed(session, wa_message_id):
        logger.info("Mensaje duplicado ignorado (reintento del proveedor): %s", wa_message_id)
        return []

    client = _get_or_create_client(session, phone)
    # Se fija ANTES de registrar nada: todo lo que se escriba en este request
    # —el entrante y las respuestas— hereda de aquí el canal correcto.
    conv = _get_or_create_conversation(session, phone, provider)
    _log_message(
        session, phone, Direction.IN, "[archivo adjunto]" if is_media else text, wa_message_id
    )
    if not is_media:
        _extract_client_data(session, phone, text)

    if _is_rate_limited(session, phone):
        logger.warning("Rate limit alcanzado para %s — mensaje ignorado", mask_phone(phone))
        return []

    if is_media:
        replies = ["Por ahora no puedo recibir documentos en este chat. Un asesor te indicará cómo enviarlos."]
    elif (fb := FEEDBACK_RE.match(text.strip())):
        # Botón 👍/👎 del aviso de cierre con el id viejo: se registra y se
        # agradece, en cualquier estado (no pasa por la máquina de estados ni
        # por nluevent).
        replies = _record_feedback(session, phone, int(fb.group(1)), fb.group(2))
    elif (rating := _lee_rating(text)) is not None and (
        pendiente := _feedback_pendiente(session, phone)
    ) is not None:
        # Respuesta a la encuesta: el toque del botón manda "1"/"2" y el cliente
        # de Twilio teclea eso mismo (o el emoji). El folio se resuelve por el
        # último cerrado, así que la opción no tiene que cargar el id.
        replies = _record_feedback(session, phone, pendiente, rating)
    elif (apagado := _latest_open_ticket(session, phone)) is not None and not apagado.agent_active:
        # El colaborador asignado apagó el agente IA para este folio: silencio
        # total en CUALQUIER estado — antes solo aplicaba en "escalado" y el
        # bot seguía pidiendo requisitos con el agente apagado. Tomar el
        # ticket (asignarlo) sigue sin callar al bot por sí solo.
        replies = []
    elif conv.state == "escalado":
        normalized = text.strip().lower()
        if normalized in MENU_KEYWORDS:
            _set_state(session, conv, "menu_categoria")
            replies = ["Claro, continuemos aquí. " + catalog.format_category_menu()]
        elif _asks_status(text):
            replies = [_status_reply(session, phone)]
        else:
            # Aunque esté esperando a un asesor, un pedido concreto de trámite
            # se atiende (deducción, multi-servicio o mini-menú de parecidos):
            # el "espera" enlatado es solo para mensajes sin petición clara.
            replies = _try_service_request(session, conv, text)
            if not replies:
                if _latest_open_ticket(session, phone) is None:
                    # Promesa sin registro: el ticket de la escalada ya se
                    # cerró (o nunca existió) y el cliente sigue escribiendo.
                    # Sin esto, "un asesor te contactará" no avisa a NADIE —
                    # se re-crea la solicitud para que el panel la muestre.
                    replies = _escalate(session, client, conv, text, reply=ESCALATED_WAIT_REPLY)
                else:
                    replies = [ESCALATED_WAIT_REPLY]
    else:
        normalized = text.strip().lower()
        # Captura caducada (CAPTURE_TTL_HOURS sin respuesta): este mensaje ya
        # no es el dato pedido — se atiende como nuevo. Lo capturado ya vive
        # en el folio; lo que falte lo pide el asesor.
        if conv.state == "pidiendo_requisitos" and _captura_caducada(conv):
            _set_state(session, conv, "menu_categoria")
        capturando = conv.state == "pidiendo_requisitos"
        molesto = _mentions_any(
            normalized, CAPTURE_ANNOYANCE if capturando else ANNOYANCE_KEYWORDS
        )
        pide_humano = (
            CAPTURE_ESCALATION_RE.search(normalized) is not None
            if capturando
            else _is_escalation(normalized)
        )
        if molesto:
            # Cliente molesto: disculpa breve y directo con un humano.
            replies = _escalate(session, client, conv, text, reply=ANNOYANCE_REPLY)
        elif pide_humano:
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
        # Un ticket borrado suavemente desde el panel NO es un ticket abierto:
        # sin este filtro la escalada marcaba escalado=True en un folio
        # invisible (y ninguna solicitud aparecía), y un agent_active=False
        # borrado silenciaba al bot para siempre.
        .where(Ticket.habilitado == True)  # noqa: E712
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

    # Los atajos que hacen match por SUBSTRING se apagan mientras se capturan
    # requisitos: la respuesta del cliente es texto libre y palabras como
    # "lista", "precios" o "folio" aparecen dentro de un dato legítimo
    # ("mi folio de INE es…"). "menu" (match exacto) y "asesor" siguen vivos.
    capturando = conv.state == "pidiendo_requisitos"

    # Pidieron el catálogo explícitamente: única situación donde sí se lista.
    if not capturando and _mentions_any(normalized, SERVICES_KEYWORDS):
        _set_state(session, conv, "menu_categoria")
        return [catalog.format_category_menu()]

    # "¿Cómo va mi trámite?" — funciona desde cualquier estado, sin tocarlo.
    if not capturando and _asks_status(text):
        return [_status_reply(session, client.phone)]

    # Saludo o despedida: trato social, no un trámite. Al saludo se responde
    # con pregunta abierta (sin listar servicios) y a la despedida con un
    # cierre cálido; ninguno pasa por la deducción ni ensucia la bitácora.
    # Exclusiones: "esperando_nombre" (un saludo podría ser el nombre real) y
    # "pidiendo_requisitos" (un "ok" o un "hola" ahí es la respuesta al dato).
    # En "confirmar" solo aplica el saludo — "ok"/"listo" son intentos de
    # confirmar y los reatiende su handler con el re-prompt de sí/no.
    social = _social_kind(text)
    if social == "saludo" and conv.state not in {"esperando_nombre", "pidiendo_requisitos"}:
        _set_state(session, conv, "menu_categoria")
        return [_greeting_for(client)]
    if social == "despedida" and conv.state not in {
        "esperando_nombre", "confirmar", "pidiendo_requisitos"
    }:
        return [FAREWELL_REPLY]

    handlers = {
        "inicio": _handle_inicio,
        "esperando_nombre": _handle_esperando_nombre,
        "menu_categoria": _handle_menu_categoria,
        "menu_servicio": _handle_menu_servicio,
        "confirmar": _handle_confirmar,
        "eligiendo_sugerencia": _handle_sugerencia,
        "pidiendo_requisitos": _handle_requisitos,
    }
    handler = handlers.get(conv.state, _handle_inicio)
    return handler(session, client, conv, text, normalized)


def _greeting_for(client: Client) -> str:
    return (
        f"¡Hola de nuevo, {client.name}! ¿En qué te ayudo hoy?"
        if client.name
        else GREETING_REPLY
    )


def _social_kind(text: str) -> str | None:
    """"saludo" | "despedida" | None (el mensaje pide algo).

    Recorta del texto normalizado las frases sociales conocidas; si lo que
    queda es puro relleno ("hola buen día" → nada), el mensaje es trato social
    y NO un intento de pedir un trámite. "hola quiero mi CURP" deja "quiero mi
    curp" en pie, así que sigue su camino normal a la deducción."""
    rest = " ".join(_SOCIAL_WORD_RE.findall(nlu._norm(text)))
    if not rest:
        return None
    kinds: set[str] = set()
    for phrase, kind in _SOCIAL_PHRASES:
        pattern = rf"\b{re.escape(phrase)}\b"
        if re.search(pattern, rest):
            rest = re.sub(pattern, " ", rest)
            kinds.add(kind)
    if not kinds or any(w not in _SOCIAL_FILLER for w in rest.split()):
        return None
    # "gracias, buen día" es una despedida aunque traiga un saludo dentro.
    return "despedida" if "despedida" in kinds else "saludo"


def _pendiente(service_ids: list[str], text: str, detectados: dict | None = None) -> dict:
    """Contexto del estado "confirmar". Guarda los códigos que venían en el
    MISMO mensaje que pidió el trámite ("acta de nacimiento CURP1 CURP2"): el
    folio todavía no existe —falta que el cliente confirme— y sin esto el dato
    se perdería y el agente lo volvería a pedir dos mensajes después.

    La clasificación se resuelve AQUÍ, en el turno que ya paga la deducción con
    Ollama, y queda guardada: el turno del "sí" reusa este dict en vez de
    volver a detectar. Así la cantidad que se le cotizó al cliente y la que
    acaba en el folio son la misma, y el "sí" sigue costando cero."""
    context: dict = {"pending_service_ids": service_ids}
    if detectados is None:
        detectados = _detectar_codigos(text)
    if detectados:
        context["codigos"] = detectados
    return context


def _detectar_codigos(text: str) -> dict:
    """Códigos del cliente en el texto, ya clasificados por tipo. Vive aparte
    de _pendiente porque cuando el agente tiene que preguntar por un trámite
    ambiguo, los códigos hay que detectarlos en ESE turno y arrastrarlos al
    siguiente: el mensaje que los trae es el original, no el "1" con que el
    cliente elige después."""
    detectados = codigos.detectar(text)
    sueltos = detectados.get(codigos.SUELTO)
    if sueltos:
        # Solo cuando los patrones NO alcanzaron: el "por si tiene dudas". Si
        # Ollama no contesta devuelve {} y todo sigue igual — el folio se crea
        # de todos modos y el colaborador valida a mano.
        detectados = codigos.aplicar_clasificacion(
            detectados, nlu.clasificar_codigos(sueltos)
        )
    return detectados


def _confirmar(
    session: Session, conv: Conversation, services: list, text: str, prefijo: str = "",
    detectados: dict | None = None,
) -> list[str]:
    """Deja la conversación esperando el sí/no y arma el mensaje de
    confirmación. Fuente única: los códigos se detectan y clasifican una sola
    vez, y de ahí salen tanto la cotización que ve el cliente como lo que se
    copia al folio cuando confirme. `detectados` los trae ya resueltos de un
    turno anterior (ver _confirmar_o_aclarar); None = detectarlos de `text`."""
    context = _pendiente([s.id for s in services], text, detectados)
    _set_state(session, conv, "confirmar", context)
    if len(services) > 1:
        return [prefijo + _confirm_multi_message(services)]
    return [prefijo + _confirm_message(services[0], context.get("codigos"))]


def _pendientes_por_aclarar(text: str, services: list) -> list:
    """Trámites que el cliente pidió en el MISMO mensaje y quedaron sin
    resolver ("quiero un acta y un rfc": el rfc se resuelve, el acta no dice
    cuál de las cuatro). Se buscan sobre el texto SIN las menciones ya
    resueltas: suggestions() matchea por palabra compartida, así que sin ese
    recorte "acta de nacimiento" arrastraría a sus tres hermanas y el agente
    preguntaría de más en un pedido que ya estaba completo."""
    resueltos = {s.id for s in services}
    resto = nlu.texto_sin_menciones(text, services)
    return [s for s in nlu.suggestions(resto) if s.id not in resueltos]


def _confirmar_o_aclarar(
    session: Session, conv: Conversation, services: list, text: str, prefijo: str = ""
) -> list[str]:
    """Como _confirmar, pero si en el mismo mensaje quedó un trámite sin
    identificar, primero pregunta cuál era — cargando los ya resueltos para
    confirmarlos JUNTOS después. Antes ese pedazo se perdía en silencio: el
    cliente pedía dos trámites, el agente entendía uno y levantaba un solo
    folio sin avisar que había ignorado el otro.

    OJO: esto se llama SOLO desde los handlers que acaban de deducir texto
    libre, nunca desde _confirmar mismo. Si viviera dentro de _confirmar, el
    turno en que el cliente elige del mini-menú volvería a detectar ambigüedad
    y el agente preguntaría en círculos."""
    pendientes = _pendientes_por_aclarar(text, services)
    if not pendientes:
        return _confirmar(session, conv, services, text, prefijo)
    # Los códigos se detectan (y clasifican con Ollama) AQUÍ, en el turno que
    # ya pagó la deducción: el turno de la elección solo trae "1" y ahí ya no
    # habría de dónde sacarlos. Viajan en el contexto hasta el _confirmar real.
    context: dict = {
        "suggestion_ids": [s.id for s in pendientes],
        "resueltos_ids": [s.id for s in services],
    }
    detectados = _detectar_codigos(text)
    if detectados:
        context["codigos"] = detectados
    _set_state(session, conv, "eligiendo_sugerencia", context)
    lines = "\n".join(f"{i}. {s.name}" for i, s in enumerate(pendientes, 1))
    ya = ", ".join(f"'{s.name}'" for s in services)
    return [
        f"{prefijo}Voy anotando {ya} ✅\n\n"
        "También me pediste otro trámite y tenemos varios parecidos 😊 "
        f"¿Cuál necesitas? Responde con el número:\n{lines}"
    ]


def _handle_inicio(session: Session, client: Client, conv: Conversation, text: str, normalized: str) -> list[str]:
    # Los saludos y despedidas ya los atajó _dispatch (también en este estado).

    # Varios servicios en un mismo mensaje ("quiero acta de nacimiento y mi
    # curp"): se confirman juntos y se crea un ticket por cada uno.
    multi, service, category, options = _deduce_and_log(session, client.phone, text)
    if multi:
        return _confirmar_o_aclarar(session, conv, multi, text, "¡Hola! 😊 ")

    # Regla: si el primer mensaje ya trae el servicio ("necesito mi CURP, soy
    # Juan…"), no se listan servicios: saludo breve y directo a confirmar.
    if service is not None:
        return _confirmar_o_aclarar(session, conv, [service], text, "¡Hola! 😊 ")
    if category is not None:
        _set_state(session, conv, "menu_servicio", {"category_id": category.id, "page": 0})
        return [catalog.format_service_menu(category.id)]

    # "quiero un acta": una palabra que coincide con varios servicios no debe
    # terminar en "no te entendí" — se ofrece el mini-menú de parecidos.
    if options:
        return _suggest_reply(session, conv, options, text)

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
        return _confirmar_o_aclarar(session, conv, multi, text)
    if service is not None:
        return _confirmar_o_aclarar(session, conv, [service], text)
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
            return _confirmar(session, conv, [service], text)

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
            return _confirmar(session, conv, picked, text, note)

    # Texto libre dentro del submenú: intentar deducir igual que en el menú
    # principal ("mejor quiero una CURP" debe funcionar desde cualquier lado).
    multi, service, deduced_category, options = _deduce_and_log(session, client.phone, text)
    if multi:
        return _confirmar_o_aclarar(session, conv, multi, text)
    if service is not None:
        return _confirmar_o_aclarar(session, conv, [service], text)
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
        # Códigos que venían en el mensaje con que pidió el trámite: se copian
        # al folio antes de preguntar nada (ver _requisitos_con_codigos).
        detectados = context.get("codigos") or {}
        tickets = []
        prellenado = False
        for service in services:
            requisitos_json = requisitos_iniciales(service.requirements)
            con_codigos = _requisitos_con_codigos(requisitos_json, detectados)
            prellenado = prellenado or con_codigos != requisitos_json
            ticket = Ticket(
                phone=client.phone,
                client_name=client.name,
                category=service.category_name,
                service_id=service.id,
                service_name=service.name,
                price_info=service.price_info,
                status=TicketStatus.NUEVO,
                # Los requisitos se congelan aquí: editar el catálogo después no
                # reescribe lo que se le pidió a este cliente.
                requirements_json=con_codigos,
                provider=conv.provider,
            )
            session.add(ticket)
            tickets.append(ticket)
        _mark_nlu_ticket(session, client.phone)
        session.commit()
        for ticket in tickets:
            session.refresh(ticket)

        # Cola plana de preguntas: [[folio, índice del requisito], …] en orden
        # de folio. Vacía = ningún servicio tiene requisitos pendientes → flujo
        # de siempre. Lo que ya llegó en el mensaje del cliente no se pregunta.
        cola = [
            [t.id, i]
            for t in tickets
            for i, requisito in enumerate(requisitos_del_ticket(t.requirements_json))
            if not requisito["valor"]
        ][:MAX_COLA]

        if len(tickets) == 1:
            alta = (
                f"¡Listo! ✅ Registré tu solicitud de '{tickets[0].service_name}' "
                f"(folio #{tickets[0].id}). {TICKET_ALTA_CIERRE}"
            )
        else:
            lines = "\n".join(f"• '{t.service_name}' — folio #{t.id}" for t in tickets)
            cierre = TICKET_ALTA_CIERRE_MULTI if cola else (
                TICKET_ALTA_CIERRE_MULTI + " ¿Otro trámite? Escribe *menu*."
            )
            alta = f"¡Listo! ✅ Registré tus {len(tickets)} solicitudes:\n{lines}\n{cierre}"

        if prellenado:
            # Decirlo evita que el cliente crea que se ignoró lo que escribió y
            # lo vuelva a mandar (que es justo lo que pasaba antes).
            alta += " " + REQ_YA_CAPTURADO

        pregunta, cola = _siguiente_pregunta(session, cola)
        if pregunta is None:
            _set_state(session, conv, "menu_categoria")
            return [alta]
        # Un solo mensaje (alta + primera pregunta): presentation() solo
        # transforma el último, y así el pareo IN/OUT sigue siendo 1:1.
        _marcar_pedido(session, cola)
        _set_state(session, conv, "pidiendo_requisitos", {"cola": cola})
        return [f"{alta}\n\n{pregunta}"]

    if normalized in NO_WORDS:
        _set_state(session, conv, "menu_categoria")
        return ["Sin problema. " + catalog.format_category_menu()]

    if len(services) > 1:
        confirm_text = _confirm_multi_message(services)
    elif services:
        # Misma cotización que la primera vez: el contexto sobrevive a
        # _bump_or_escalate, que solo incrementa failed_attempts.
        confirm_text = _confirm_message(services[0], context.get("codigos"))
    else:
        confirm_text = catalog.format_category_menu()
    return _bump_or_escalate(session, client, conv, text, "¿Puedes confirmar con 'sí' o 'no'?\n" + confirm_text)


def _requisitos_con_codigos(requisitos_json: str | None, detectados: dict) -> str | None:
    """Los requisitos del folio con los códigos que el cliente ya escribió.

    Devuelve el JSON tal cual si no hay nada que copiar, para que el llamador
    sepa por comparación si hubo prellenado."""
    if not requisitos_json or not detectados:
        return requisitos_json
    originales = requisitos_del_ticket(requisitos_json)
    llenos = codigos.prellenar(originales, detectados)
    if llenos == originales:
        return requisitos_json
    return json.dumps(llenos, ensure_ascii=False)


def _requisito_de(session: Session, ticket_id: int, idx: int) -> str:
    """Texto del requisito que está contestando el cliente ("clave curp")."""
    ticket = session.get(Ticket, ticket_id)
    requisitos = requisitos_del_ticket(ticket.requirements_json) if ticket else []
    return requisitos[idx]["req"] if 0 <= idx < len(requisitos) else ""


def _siguiente_pregunta(session: Session, cola: list) -> tuple[str | None, list]:
    """Primera pregunta pendiente de la cola y la cola desde ese punto.

    Salta folios eliminados por el asesor (soft delete, habilitado=False) o ya
    cerrados, e índices fuera del JSON: a media captura la base puede haber
    cambiado y el cliente no debe quedarse contestando a un folio muerto.

    Salta también los requisitos que YA tienen valor —el cliente los mandó en el
    mismo mensaje del trámite, o el asesor los capturó a mano desde el panel—:
    preguntar por un dato que ya se tiene es justo lo que molestaba al cliente."""
    while cola:
        ticket_id, idx = cola[0]
        ticket = session.get(Ticket, ticket_id)
        vivo = (
            ticket is not None
            and ticket.habilitado
            and ticket.status != TicketStatus.CERRADO
        )
        requisitos = requisitos_del_ticket(ticket.requirements_json) if vivo else []
        if 0 <= idx < len(requisitos) and not requisitos[idx]["valor"]:
            return (
                REQ_PREGUNTA.format(
                    servicio=ticket.service_name,
                    folio=ticket.id,
                    requisito=requisitos[idx]["req"][:REQ_MAX_CHARS],
                ),
                cola,
            )
        cola = cola[1:]
    return None, cola


def _editar_requisito(session: Session, ticket_id: int, idx: int, cambios: dict) -> None:
    """Actualiza un requisito EN EL FOLIO, de inmediato: si la conversación se
    corta a media captura, lo que ya llegó no se pierde. `pedido` se marca al
    emitir la pregunta; `valor` al recibir la respuesta."""
    ticket = session.get(Ticket, ticket_id)
    if ticket is None:
        return
    requisitos = requisitos_del_ticket(ticket.requirements_json)
    if not 0 <= idx < len(requisitos):
        return
    requisitos[idx].update(cambios)
    ticket.requirements_json = json.dumps(requisitos, ensure_ascii=False)
    ticket.updated_at = utcnow()
    session.add(ticket)
    session.commit()


def _es_omision(text: str) -> bool:
    """"omitir", "No lo tengo.", "no la tengo todavia" → el cliente no tiene el
    dato. Se quita puntuación y se tolera una cola corta ("aun", "todavia");
    una cola larga suele traer el dato ("despues de buscar la encontre: X")."""
    limpio = " ".join(_OMIT_PUNCT_RE.sub(" ", nlu._norm(text)).split())
    if limpio in OMIT_WORDS:
        return True
    return any(
        limpio.startswith(w + " ") and len(limpio) <= len(w) + 1 + OMIT_TAIL_CHARS
        for w in OMIT_WORDS
    )


def _handle_requisitos(session: Session, client: Client, conv: Conversation, text: str, normalized: str) -> list[str]:
    """Captura los requisitos del trámite recién registrado.

    Aquí NO existe "no te entendí": cualquier texto es la respuesta al dato
    pedido, así que nunca se pasa por _bump_or_escalate (escalar dejaría el
    contexto vacío y mataría la captura). Si el cliente no lo tiene, *omitir*
    lo marca como faltante para que el asesor lo pida.

    ÚNICA excepción (2026-08-08): si el dato pedido es una clave con formato
    —CURP, RFC, NSS— y la respuesta no trae nada con esa forma, se repregunta
    UNA vez. Sin eso el folio se quedaba con un "." donde iba la CURP. A la
    segunda se guarda tal cual: el agente no discute con el cliente, y
    *omitir* sigue saliendo antes de llegar aquí."""
    contexto = _get_context(conv)
    cola_original = contexto.get("cola") or []
    pregunta_vigente, cola = _siguiente_pregunta(session, cola_original)
    if pregunta_vigente is None:
        _set_state(session, conv, "menu_categoria")
        return [REQ_CIERRE]
    if cola != cola_original:
        # La pregunta que el cliente contestó ya no existe (el asesor eliminó o
        # cerró ese folio a media captura): su respuesta no es de nadie, así
        # que no se guarda en el folio equivocado — se re-pregunta el frente.
        _marcar_pedido(session, cola)
        _set_state(session, conv, "pidiendo_requisitos", {"cola": cola})
        return [pregunta_vigente]

    ticket_id, idx = cola[0]
    omite = _es_omision(text)
    requisito = _requisito_de(session, ticket_id, idx)
    if not omite and not contexto.get("reintento") and not codigos.trae_el_dato(requisito, text):
        # Se pidió una clave (CURP/RFC/NSS) y la respuesta no trae nada con esa
        # forma: un "." o un "ya te lo mandé". Se repregunta UNA vez —a la
        # siguiente se guarda tal cual, que el asesor lo lea y decida.
        _set_state(session, conv, "pidiendo_requisitos", {"cola": cola, "reintento": True})
        return [REQ_REINTENTO.format(requisito=requisito[:REQ_MAX_CHARS])]

    valor = None if omite else text.strip()[:MAX_NOTE_TEXT]
    _editar_requisito(session, ticket_id, idx, {"valor": valor, "pedido": True})

    pregunta, resto = _siguiente_pregunta(session, cola[1:])
    if pregunta is None:
        _set_state(session, conv, "menu_categoria")
        return [REQ_CIERRE]
    _marcar_pedido(session, resto)
    _set_state(session, conv, "pidiendo_requisitos", {"cola": resto})
    return [pregunta]


def _marcar_pedido(session: Session, cola: list) -> None:
    """Deja constancia en el folio de que la pregunta del frente se emitió:
    el panel distingue "sin respuesta" (se preguntó) de "sin preguntar"."""
    if cola:
        _editar_requisito(session, cola[0][0], cola[0][1], {"pedido": True})


# Datos estructurados que el cliente suelta en el chat (Fase 2): se anexan a
# su ficha como nota para que el colaborador no los vuelva a pedir. Los
# patrones viven en app/codigos.py — aquí solo se decide qué se anota.
# Solo los tipos con formato oficial: las cadenas "con forma de clave" sin
# clasificar (codigos.SUELTO) sirven para llenar un requisito concreto, pero
# como nota suelta en la ficha serían ruido.
TIPOS_EN_FICHA = ("CURP", "RFC", "Email")


def _extract_client_data(session: Session, phone: str, text: str) -> None:
    detectados = codigos.detectar(text)
    found = [
        (tipo, valor) for tipo in TIPOS_EN_FICHA for valor in detectados.get(tipo, [])
    ]
    # El tope es de NOTAS por mensaje, no de datos: lo que se copia al folio
    # sale de `detectados`, sin recortar (tres CURP entran completas).
    for tipo, valor in found[:3]:
        exists = session.exec(
            select(ClientNote)
            .where(ClientNote.phone == phone)
            .where(ClientNote.body.contains(valor))  # type: ignore[attr-defined]
        ).first()
        if exists is None:
            session.add(
                ClientNote(
                    phone=phone, author="agente-ia",
                    body=f"📎 {tipo} detectado en el chat: {valor}",
                )
            )


# Encuesta del aviso de cierre (la emite dashboard._notify_finish).
#
# Los ids de las opciones son "1" y "2" —tecleables— porque Twilio no tiene
# botones nativos y degrada el mensaje a texto poniendo el id delante de cada
# opción: con los ids viejos el cliente leía "fb:25:up) 👍 Sí, excelente", un
# identificador interno que encima no podía responder. Con ids tecleables el
# toque (Meta) y el tecleo (Twilio) entran por el mismo camino.
FEEDBACK_PROMPT = "¿Te sirvió nuestro servicio?"
FEEDBACK_OPCIONES = [("1", "👍 Sí, excelente"), ("2", "👎 Puede mejorar")]

# Compatibilidad: los botones ya enviados con el id viejo siguen llegando desde
# los chats donde el cliente todavía no ha tocado nada.
FEEDBACK_RE = re.compile(r"^fb:(\d+):(up|down)$")
FEEDBACK_THANKS = {
    "up": "¡Gracias por tu opinión! 🙌 Nos alegra haberte ayudado.",
    "down": "Gracias por decírnoslo 🙏 Vamos a revisar tu caso para mejorar.",
}


def _lee_rating(text: str) -> str | None:
    """Traduce la respuesta a la encuesta: el número, el emoji o un sí/no."""
    t = text.strip().lower()
    if t.startswith("👍") or t in {"1", "si", "sí"}:
        return "up"
    if t.startswith("👎") or t in {"2", "no"}:
        return "down"
    return None


def _feedback_pendiente(session: Session, phone: str) -> int | None:
    """Folio cerrado que está esperando la calificación, si lo hay.

    Solo cuenta cuando el ÚLTIMO mensaje que le mandamos al cliente fue la
    encuesta: si después hubo cualquier otro envío, un "1" suyo pertenece a esa
    conversación (un menú, por ejemplo) y no a la calificación.
    """
    ultimo_out = session.exec(
        select(Message)
        .where(Message.phone == phone, Message.direction == Direction.OUT)
        .order_by(Message.created_at.desc(), Message.id.desc())
    ).first()
    if ultimo_out is None or not ultimo_out.body.startswith(FEEDBACK_PROMPT):
        return None
    ticket = session.exec(
        select(Ticket)
        .where(Ticket.phone == phone, Ticket.status == TicketStatus.CERRADO)
        .order_by(Ticket.updated_at.desc(), Ticket.id.desc())
    ).first()
    if ticket is None:
        return None
    ya_calificado = session.exec(
        select(Feedback).where(Feedback.ticket_id == ticket.id)
    ).first()
    return None if ya_calificado else ticket.id


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
            # Meta no permite auto-abrir la lista: el desplegable siempre llega
            # cerrado. Por eso las opciones numeradas van VISIBLES en el cuerpo
            # (auto-despliegue de facto) y "Ver opciones" queda para elegir con
            # un toque — o el cliente teclea el número, como siempre.
            out[-1] = {"text": last, "list": rows, "button": LIST_BUTTON_LABEL}
    return out


def _log_nlu(
    session: Session, phone: str, text: str, capa: str,
    service_id: str | None = None, confianza: float | None = None,
) -> None:
    # Saludos, despedidas y cortesías no son un intento de pedir un trámite:
    # no se registran en la bitácora para no inflar los "no entendidos"
    # (mismas frases que atiende _dispatch — ver _social_kind).
    if len(nlu._norm(text)) < 3 or _social_kind(text) is not None:
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
        # La capa real, no el literal "categoria": un acierto de categoría de
        # Ollama/Claude debe contar para SU capa en la dona del panel (la capa
        # de keywords ya se reporta a sí misma como "categoria").
        _log_nlu(session, phone, text, capa, category.id)
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
        # Las filas extra (rag/rag_descartado/tools) no cuentan como mensaje:
        # marcar la conversión ahí la volvería invisible para el KPI de
        # tickets, que solo suma sobre filas de mensaje real.
        .where(NluEvent.capa.not_in(CAPAS_EXTRA))
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
        return _confirmar_o_aclarar(session, conv, multi, text)
    if service is not None:
        return _confirmar_o_aclarar(session, conv, [service], text)
    if category is not None:
        _set_state(session, conv, "menu_servicio", {"category_id": category.id, "page": 0})
        return [catalog.format_service_menu(category.id)]
    if options:
        return _suggest_reply(session, conv, options, text)
    return None


def _suggest_reply(session: Session, conv: Conversation, options: list, text: str) -> list[str]:
    if len(options) == 1:
        return _confirmar(session, conv, [options[0]], text)
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
    if _asks_info(text) and not options:
        # Pedir informes es interés, no un intento fallido: no suma al
        # contador de reintentos ni suena a regaño. Va ANTES de la capa
        # informativa: un "¿cuánto cuesta?" genérico no tiene respuesta única
        # en el corpus y el embudo cálido lo resuelve mejor.
        _set_state(session, conv, "menu_categoria")
        return [INFO_REPLY]
    # Capa informativa (RAG_ENABLED): responde la duda antes de ofrecer el
    # mini-menú — "¿cuánto tarda el acta?" pregunta por tiempos, no pide un
    # menú de actas. Si no hay respuesta con evidencia, el flujo de siempre.
    respuesta_rag = _try_informative(session, client.phone, text)
    if respuesta_rag is not None:
        return [respuesta_rag]
    if options:
        return _suggest_reply(session, conv, options, text)
    # Último recurso antes de reintentar/escalar: el nodo agéntico con
    # herramientas de solo lectura (AGENT_TOOLS_ENABLED). Atiende pedidos
    # compuestos o fuera de guion que nada de lo anterior resolvió.
    respuesta_tools = _try_agent_tools(session, client.phone, text)
    if respuesta_tools is not None:
        return [respuesta_tools]
    return _bump_or_escalate(session, client, conv, text, NOT_RECOGNIZED_HINT)


def _try_informative(session: Session, phone: str, text: str) -> str | None:
    """Capa "rag" (app/rag.py) con su bitácora. Deja una fila nluevent
    capa="rag" ADEMÁS de la que ya dejó _deduce_and_log ("ninguna" o
    "sugerencia"): el minado de sinónimos sigue viendo lo mismo, y las
    métricas por mensaje (alerts._no_entendidos_stats, nlu_metrics) EXCLUYEN
    las filas extra (models.CAPAS_EXTRA) para no inflar su denominador."""
    if not settings.rag_enabled or not rag.es_pregunta_informativa(text):
        return None
    respuesta, motivo = rag.answer_explained(text)
    if respuesta is None:
        if motivo in rag.GUARDRAIL_MOTIVOS:
            # El guardrail tumbó una respuesta candidata: fila propia con el
            # motivo (en service_id) para que el disparo se vea en el panel —
            # antes un descarte en producción era invisible.
            _log_nlu(session, phone, text, "rag_descartado", motivo)
        return None
    _log_nlu(session, phone, text, "rag")
    return respuesta


def _try_agent_tools(session: Session, phone: str, text: str) -> str | None:
    """Nodo agéntico (app/agent_tools.py) con su bitácora: fila capa="tools"
    EXTRA del mismo mensaje, excluida de los denominadores igual que "rag"."""
    respuesta = agent_tools.responder(session, phone, text)
    if respuesta is None:
        return None
    _log_nlu(session, phone, text, "tools")
    return respuesta


def _handle_sugerencia(session: Session, client: Client, conv: Conversation, text: str, normalized: str) -> list[str]:
    context = _get_context(conv)
    options = [
        s for s in (catalog.service_by_id(i) for i in context.get("suggestion_ids", []))
        if s is not None
    ]
    # Trámites que ya se habían entendido en el mensaje original y esperan a
    # que el cliente aclare el que faltaba (ver _confirmar_o_aclarar), con los
    # códigos que venían en ESE mensaje y que este turno ya no trae.
    resueltos = _unique_services(
        s for s in (catalog.service_by_id(i) for i in context.get("resueltos_ids", []))
        if s is not None
    )
    detectados = context.get("codigos")
    idx = _parse_int(normalized)
    picks = [idx] if idx is not None else _parse_indices(normalized)
    if picks and options:
        chosen = _unique_services(options[i - 1] for i in picks if 1 <= i <= len(options))
        if chosen:
            # Lo ELEGIDO va primero: es la respuesta directa a la pregunta que
            # acabamos de hacer, y si el tope recorta, eso es lo último que
            # debe caerse. Al revés, un cliente con el tope lleno de trámites
            # ya resueltos veía cómo su elección desaparecía sin aviso — el
            # mismo bug silencioso que este flujo vino a arreglar.
            juntos = _unique_services(chosen + resueltos)[: nlu.MULTI_MAX_SERVICES]
            return _confirmar(session, conv, juntos, text, detectados=detectados)
    if resueltos:
        # Piso duro: si el cliente no eligió, lo que YA se había entendido NO
        # se tira. Sin esto el flujo nuevo sería PEOR que el viejo, donde ese
        # trámite se confirmaba de inmediato.
        # ponytail: aquí no se reintenta deducir su texto libre ("el de
        # nacimiento"); si hace falta, deducir y sumar a `resueltos` antes de
        # confirmar.
        return _confirmar(session, conv, resueltos, text, detectados=detectados)
    # Cualquier otra cosa se procesa como texto libre normal.
    return _handle_menu_categoria(session, client, conv, text, normalized)


def _confirm_message(service, detectados: dict | None = None) -> str:
    # El precio sale SOLO del catálogo — el agente nunca inventa precios.
    cuantos = codigos.cuantos_tramites(
        parse_requirements(service.requirements), detectados or {}
    )
    if cuantos == 1:
        return f"¿Confirmas '{service.name}' ({service.price_info})? Responde sí o no."
    # Una sola solicitud que cubre los N códigos —no N folios— pero cotizada
    # por cantidad: el cliente ve de dónde sale el número antes de confirmar.
    return (
        f"¿Confirmas '{service.name}' para los {cuantos} datos que me compartiste "
        f"({service.price_info} c/u)? {_estimado(service, cuantos)} Responde sí o no."
    )


def _estimado(service, cuantos: int) -> str:
    """Precio del catálogo × cantidad. Es un ESTIMADO y se dice: el agente NO
    fija el precio final de un trámite — eso lo confirma el asesor, que además
    es quien teclea el monto del cobro. Un precio no numérico del catálogo
    ("A cotizar") no se multiplica: no hay número que multiplicar."""
    try:
        unitario = parse_monto(service.price_info)
    except ValueError:
        return "El asesor te confirma el total."
    return f"Estimado: {formato_monto(unitario * cuantos)}, sujeto a confirmación del asesor."


def _confirm_multi_message(services) -> str:
    # Con varios servicios distintos NO se reparten los códigos: adivinar cuál
    # de los trámites lleva cuál clave sería inventar. Uno de cada uno, y el
    # asesor ajusta si el cliente quería más.
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
            provider=conv.provider,
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
    # Último movimiento real de la conversación: de aquí se mide la caducidad
    # de la captura de requisitos (antes updated_at se quedaba en la fecha de
    # creación para siempre).
    conv.updated_at = utcnow()
    session.add(conv)
    session.commit()


def _captura_caducada(conv: Conversation) -> bool:
    """True si el último movimiento fue hace más de CAPTURE_TTL_HOURS.

    SQLite devuelve datetimes naive y utcnow() es aware: se normalizan ambos a
    naive-UTC antes de comparar (mismo criterio que app.alerts)."""
    updated = conv.updated_at
    if updated.tzinfo is not None:
        updated = updated.astimezone(timezone.utc).replace(tzinfo=None)
    limite = (
        (utcnow() - timedelta(hours=CAPTURE_TTL_HOURS))
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )
    return updated < limite


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


def _get_or_create_conversation(
    session: Session, phone: str, provider: str = PROVIDER_META
) -> Conversation:
    conv = session.get(Conversation, phone)
    if conv is None:
        conv = Conversation(phone=phone, provider=provider)
        session.add(conv)
        session.commit()
        session.refresh(conv)
    elif conv.provider != provider:
        # El cliente escribió al otro número: a partir de ahora se le contesta
        # por ahí. Una persona = una conversación, así que el avance del trámite
        # continúa donde estaba — cambiar de canal no reinicia nada.
        conv.provider = provider
        session.add(conv)
        session.commit()
        session.refresh(conv)
    return conv


def _log_message(
    session: Session,
    phone: str,
    direction: Direction,
    body: str,
    wa_message_id: str | None = None,
    provider: str | None = None,
) -> None:
    if provider is None:
        # Se hereda de la conversación, que handle_incoming ya dejó apuntando al
        # canal de ESTE mensaje. Así los salientes quedan marcados con el canal
        # correcto sin pasar el proveedor por cada punto de registro.
        conv = session.get(Conversation, phone)
        provider = conv.provider if conv else PROVIDER_META
    session.add(
        Message(
            phone=phone,
            direction=direction,
            body=body[:MAX_STORED_TEXT],
            wa_message_id=wa_message_id,
            provider=provider,
        )
    )
    session.commit()
