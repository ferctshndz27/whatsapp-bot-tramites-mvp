import json
import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from app import adjuntos, agent_tools, asignacion, codigos, horario, nlu, rag
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
    OFICINA_TLAXCALA_ID,
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
    TicketActivity,
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
    # Frases, no palabras sueltas: «agente», «transferir» y «trasferir» solos
    # aparecen en preguntas legítimas («¿se hace con un agente aduanal?»). Las
    # frases casi no se dan en un trámite (FTD-SALUDO-MENU §C, opción A).
    "transferir agente",
    "trasferir agente",
    "transfiereme",
    "pasame con",
    "pasarme con",
    "comunicame con",
    "atencion humana",
    # «operador» va suelto a propósito: riesgo aceptado por el operador (2026-10-09)
    # («soy operador de tráiler» escalaría a un asesor).
    "operador",
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
    "buenos dias", "buena tarde", "buenas tardes", "buenos tardes", "buena noche",
    "buenas noches", "buenos noches", "buenas dias", "buenas dia", "hey", "que tal",
    "que onda", "que hay", "saludos", "como estas", "como esta", "como estan",
    "disculpa", "disculpe",
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
# saludo → saludo + menú principal de categorías (cambio del operador,
# 2026-10-08, D7: el cliente debe ver qué puede pedir), servicio directo → ticket.
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
    # "trámite" es la palabra del negocio y faltaba. Sin ella "quiero un
    # tramite" no se entendía, sumaba intento fallido y al tercero escalaba:
    # el 2026-09-10 levantó los folios #47 y #48 ("Escalado a asesor") por eso.
    # Van FRASES y no la palabra suelta a propósito: "como van mis tramites"
    # es una pregunta de estatus y la atiende _asks_status, no este atajo.
    "que tramites", "qué trámites", "que trámites", "qué tramites",
    "cuales tramites", "cuáles trámites", "cuales servicios", "cuáles servicios",
    "quiero un tramite", "quiero un trámite",
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
    # Desde la fase 3b un folio puede nacer YA con asesor, así que «en espera
    # de asignación» sería mentirle al cliente. El texto de NUEVO se elige
    # según haya dueño o no (ver _etiqueta_estado); nunca se le dice al
    # cliente QUIÉN lo lleva: eso es organización interna.
    TicketStatus.NUEVO: "recibido, en espera de asignación",
    TicketStatus.TOMADO: "en atención",
    TicketStatus.EN_PROCESO: "en proceso",
    TicketStatus.CERRADO: "completado ✅",
}
STATUS_NUEVO_ASIGNADO = "recibido, con asesor asignado"
ESCALADO_NOMBRE_CLIENTE = "Revisión con asesor"


def _etiqueta_estado(ticket: Ticket) -> str:
    if ticket.status == TicketStatus.NUEVO and ticket.assigned_to:
        return STATUS_NUEVO_ASIGNADO
    return STATUS_LABELS[ticket.status]
STATUS_LIST_LIMIT = 5

# ---- Estatus por folio (operador, 2026-10-12) ----
# Quien pregunta por su caso recibe el estatus DE ESE FOLIO. Si nombra uno («#74», «folio 74»,
# «caso 74») se contesta ese; si no nombra ninguno y tiene varios abiertos, se le pregunta cuál.
# Sin estado en la FSM: la pregunta es una lista cuyas filas llevan el folio como id («#74») y el
# número suelto se entiende solo si lo último que dijo el bot fue esa pregunta. Detrás de
# `contexto_folio_enabled`: apagada, «mi caso» y «#74» se atienden como antes.
ESTADOS_ESTATUS_LIBRE = frozenset({"inicio", "menu_categoria", "menu_servicio", "escalado"})
MAX_PALABRAS_ESTATUS = 12  # más largo ya es información, no una pregunta por el estatus
ESTATUS_FOLIO = "Folio #{folio} ({servicio}): {estado}."
FOLIO_NO_ENCONTRADO = (
    "No encuentro el folio #{folio} entre tus solicitudes. Escribe *estatus* para ver las que tienes."
)
ESTATUS_PREGUNTA_INICIO = "¿De cuál folio quieres saber?"
ESTATUS_PREGUNTA = ESTATUS_PREGUNTA_INICIO + " Responde con el número de folio, por ejemplo #{folio}:"
ESTATUS_TODOS_PIE = "O escribe *todos* para ver el estado de todos."
ESTATUS_FILA_TODOS = "📋 Todos mis folios"
ESTATUS_ID_TODOS = "todos"
MAX_FOLIOS_EN_PREGUNTA = 9  # + «Todos» = las 10 filas de Meta

# Selección múltiple pura en un menú: "1 y 2", "1,3", "2 4", "1y2".
_MULTI_PICK_RE = re.compile(r"\d+(?:(?:\s*(?:[,;+&]|y|e)\s*|\s+)\d+)+")

MAX_FAILED_ATTEMPTS = 2
MAX_STORED_TEXT = 4000
MAX_NOTE_TEXT = 500
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX_MESSAGES = 20

# ---- Folios repetidos (auditoría 2026-09, EXT-2) ----
# El alta creaba un folio por servicio confirmado sin mirar los ABIERTOS del
# mismo cliente: un número, 20 mensajes → 50 folios, cada uno pasando por el
# reparto a asesores y por el espejo, e inundando la bandeja. El rate limit no
# lo frena porque son mensajes legítimos en cantidad legítima.
# Dos reglas, antes de `session.add(ticket)`: un servicio que ya tiene folio
# abierto para ese teléfono no abre otro («ya tienes el folio #N»), y un cliente
# con MAX_FOLIOS_ABIERTOS trámites abiertos no abre más hasta que su asesor cierre alguno.
# Los casos para análisis NO cuentan para ese cupo ni tienen tope propio (operador, 2026-10-12:
# un gestor manda por el mismo chat los casos de varios trabajadores).
# El bot no cierra folios —eso es del asesor—, solo deja de multiplicarlos.
# El tope se cuenta y se aplica en la misma función pero no en una transacción
# serializable: dos confirmaciones del mismo teléfono que lleguen a la vez
# pueden colarse una de más (la carrera ya rastreada en
# tests/test_concurrency.py, xfail). Frena el abuso secuencial, que es el PoC;
# no es un candado bajo concurrencia.
# ponytail: constante, no knob del Estudio; se vuelve knob el día que alguien
# lo pida con un número en la mano (un cliente real con más de cinco trámites
# vivos a la vez es el asesor consolidando, no el bot).
MAX_FOLIOS_ABIERTOS = 5
FOLIO_REPETIDO = "Ya tienes el folio #{folio} de '{servicio}' abierto; tu asesor lo está atendiendo."
FOLIOS_TOPE = (
    "Tienes {n} solicitudes abiertas, que es el máximo a la vez. Cuando tu asesor "
    "cierre alguna podrás abrir más. Escribe *estatus* para ver cómo van."
)

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


def _cierre_atencion(dentro: str) -> str:
    """El cierre que corresponde a la hora, para un texto que PROMETE tiempo.

    Los textos de arriba prometen «en breve», que a las 2 de la mañana de un
    domingo es falso. Fuera de horario el aviso **sustituye** esa promesa en vez
    de acompañarla: «en breve le resolvemos. Ahorita estamos fuera de horario»
    se contradice a sí mismo dentro de la misma línea.

    El bot NO deja de trabajar fuera de horario —el folio se abre igual—; lo que
    cambia es lo que se promete. La queja real nunca fue que nadie contestara de
    madrugada, sino que nadie lo dijera.
    """
    return horario.aviso() or dentro


def _con_horario(texto: str) -> str:
    """Agrega el aviso a un texto que NO promete tiempo (uno propio del flujo).

    Aquí sí se acompaña en vez de sustituir: el texto trae información que se
    perdería. Dentro de horario no agrega nada.
    """
    aviso = horario.aviso()
    return f"{texto} {aviso}" if aviso else texto
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
    office_id: int = OFICINA_TLAXCALA_ID,
    adjunto: dict | None = None,
) -> list[str]:
    """`adjunto` es la referencia del archivo en el proveedor (app/adjuntos.py);
    solo se guarda la referencia, nunca el archivo.

    `office_id` viaja por el mismo túnel explícito que `provider` (§4).

    Lleva default —a diferencia de credenciales.credencial(), donde uno sería el
    fallback silencioso que ese módulo prohíbe— por dos razones que no aplican
    allá: aquí equivocarse escribe una fila en la oficina 1 en vez de firmar con
    el secreto de otra oficina, y hay 229 call sites, casi todos de la suite,
    para los que Tlaxcala es la respuesta correcta. El ÚNICO camino donde la
    oficina es ambigua es el webhook, y ahí se pasa siempre explícito: lo amarra
    tests/test_ruteo_oficina.py con un candado AST."""
    if wa_message_id and _already_processed(session, wa_message_id):
        logger.info("Mensaje duplicado ignorado (reintento del proveedor): %s", wa_message_id)
        return []

    client = _get_or_create_client(session, phone, office_id)
    # Se fija ANTES de registrar nada: todo lo que se escriba en este request
    # —el entrante y las respuestas— hereda de aquí el canal correcto.
    conv = _get_or_create_conversation(session, phone, provider, office_id)
    entrante = _log_message(
        session, phone, Direction.IN, adjuntos.cuerpo(adjunto, text) if is_media else text,
        wa_message_id, adjunto=adjunto if is_media else None,
    )
    # El rate limit se decide con el entrante YA registrado —es la base del
    # contador y de la idempotencia— y ANTES de derivar nada más: 60 entrantes
    # en un minuto dejaban 123 ClientNote además de las filas de message
    # (auditoría 2026-09, EXT-4).
    if _is_rate_limited(session, phone):
        logger.warning("Rate limit alcanzado para %s — mensaje ignorado", mask_phone(phone))
        return []
    # Una pregunta de folio o de caso que venció o cuya perilla se apagó en vuelo se descarta
    # ANTES de decidir nada: este mensaje se atiende desde un estado neutral.
    _caduca_aclaracion(session, conv)
    # Los datos del cliente (CURP, correo) van a su ficha DESPUÉS de decidir: nunca desde un texto
    # que se anexó a un folio ni mientras se aclara (casos-analisis §5.4).
    extraer = not is_media and conv.state not in ESTADOS_ACLARANDO

    if is_media:
        # El archivo ya quedó en el chat del CRM (solo su referencia). El bot
        # acusa recibo y nada más —revisarlo es del asesor—, y calla si alguien
        # apagó el agente en su folio, igual que con un mensaje de texto.
        if any(not t.agent_active for t in _folios_abiertos(session, phone)):
            replies = []
        elif adjuntos.soportado(adjunto) and conv.state in ESTADOS_ACLARANDO:
            # Pregunta pendiente: el archivo ya quedó en el chat; se liga al resolver.
            _set_state(session, conv, conv.state, _get_context(conv))
            replies = [FOLIO_LO_SUMO_ARCHIVO]
        elif adjuntos.soportado(adjunto):
            replies = _regla_del_folio_archivo(session, conv, entrante.id) or [adjuntos.ACUSE]
        else:
            replies = [adjuntos.NO_SOPORTADO]
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
    elif any(not t.agent_active for t in _folios_abiertos(session, phone)):
        # Un colaborador apagó el agente IA en su folio: silencio total en
        # CUALQUIER estado — antes solo aplicaba en "escalado" y el bot seguía
        # pidiendo requisitos con el agente apagado. Tomar el ticket
        # (asignarlo) sigue sin callar al bot por sí solo.
        # Basta CUALQUIER folio abierto, no el más reciente: con la asignación
        # por servicio la conversación trae folios de dueños distintos, y el
        # bot no puede hablarle al cliente encima de un asesor que lo atiende
        # solo porque el folio más nuevo es de otro.
        replies = []
    elif (respuesta := _contesta_la_pregunta_de_estatus(session, conv, text)) is not None:
        # «74» o «todos» contestando «¿de cuál folio quieres saber?»: solo si esa fue la última
        # cosa que le dijo el bot (ver la función). Va después del silencio por agente apagado.
        replies = [respuesta]
    elif conv.state == "escalado":
        # Su handler sale del dict de la FSM, pero se invoca AQUÍ y no desde
        # _dispatch: el cliente que espera a un asesor no pasa por el preludio
        # (menú por palabra, identidad, catálogo, saludo, despedida) ni por la
        # molestia o el pedido de humano de abajo — ya lo atiende un humano.
        normalized = text.strip().lower()
        # Dentro del escalado la regla del folio va después de «menu» y del estatus, que
        # `_regla_del_folio` deja pasar (diseño §5.4, parche 1).
        regla = _regla_del_folio(session, conv, text, normalized, entrante.id)
        if regla is None:
            regla = _regla_de_la_revision(session, conv, text, normalized, entrante.id)
        if regla is not None:
            replies, extraer = regla, False
        else:
            replies = _handler_de(conv.state)(session, client, conv, text, normalized)
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
        elif (regla := _regla_del_folio(session, conv, text, normalized, entrante.id)) is not None:
            replies, extraer = regla, False
        elif (regla := _regla_de_la_revision(session, conv, text, normalized, entrante.id)) is not None:
            replies, extraer = regla, False
        else:
            replies = _dispatch(session, client, conv, text, normalized)

    if extraer:
        _extract_client_data(session, phone, text)
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


def _folios_abiertos(session: Session, phone: str) -> list[Ticket]:
    """Los folios abiertos del cliente, del más nuevo al más viejo."""
    return list(
        session.exec(
            select(Ticket)
            .where(Ticket.phone == phone)
            .where(Ticket.status != TicketStatus.CERRADO)
            # Un ticket borrado suavemente desde el panel NO es un ticket abierto:
            # sin este filtro la escalada marcaba escalado=True en un folio
            # invisible (y ninguna solicitud aparecía), y un agent_active=False
            # borrado silenciaba al bot para siempre.
            .where(Ticket.habilitado == True)  # noqa: E712
            .order_by(Ticket.created_at.desc())
        ).all()
    )


def _latest_open_ticket(session: Session, phone: str) -> Ticket | None:
    abiertos = _folios_abiertos(session, phone)
    return abiertos[0] if abiertos else None


def _filtrar_folios_nuevos(session: Session, phone: str, services: list) -> tuple[list, list[str]]:
    """Qué servicios confirmados SÍ abren folio, y los avisos para los que no.

    Descarta el que ya tiene folio abierto para este teléfono (se le dice cuál)
    y recorta al cupo que deja MAX_FOLIOS_ABIERTOS. Un mismo servicio dos veces
    en la misma confirmación cuenta una. Auditoría 2026-09, EXT-2."""
    abiertos = _folios_abiertos(session, phone)
    abierto_por_servicio: dict[str, Ticket] = {}
    for t in abiertos:  # del más nuevo al más viejo: se queda el más reciente
        abierto_por_servicio.setdefault(t.service_id, t)
    avisos: list[str] = []
    nuevos: list = []
    vistos: set[str] = set()
    for service in services:
        if service.id in vistos:
            continue
        vistos.add(service.id)
        repetido = abierto_por_servicio.get(service.id)
        if repetido is not None:
            avisos.append(FOLIO_REPETIDO.format(folio=repetido.id, servicio=service.name))
        else:
            nuevos.append(service)
    # Los casos para análisis no cuentan para el cupo (operador, 2026-10-12: sin tope de tickets por
    # cliente): un gestor con muchos casos abiertos sigue pudiendo pedir un trámite. El tope de los
    # trámites normales sigue igual.
    ocupan_cupo = sum(1 for t in abiertos if t.service_id != CASO_SERVICE_ID)
    cupo = max(MAX_FOLIOS_ABIERTOS - ocupan_cupo, 0)
    if len(nuevos) > cupo:
        nuevos = nuevos[:cupo]
        avisos.append(FOLIOS_TOPE.format(n=ocupan_cupo + len(nuevos)))
    return nuevos, avisos


def _asks_status(text: str) -> bool:
    return _STATUS_INTENT_RE.search(nlu._norm(text)) is not None


def _nombre_para_el_cliente(ticket: Ticket) -> str:
    """El folio de la escalada se guarda como «Escalado a asesor» (así lo reconocen el CRM y el
    panel); al cliente se le nombra por lo que pidió."""
    return ESCALADO_NOMBRE_CLIENTE if ticket.service_id == "escalado" else ticket.service_name


def _status_reply(session: Session, phone: str) -> str:
    # El folio de la escalada SÍ se lista (operador, 2026-10-09): es lo que el cliente pidió
    # revisar y lo que un asesor ya lleva. Excluirlo le daba el estatus de folios viejos.
    tickets = session.exec(
        select(Ticket)
        .where(Ticket.phone == phone)
        .where(Ticket.habilitado == True)  # noqa: E712
        .order_by(Ticket.created_at.desc())
        .limit(STATUS_LIST_LIMIT)
    ).all()
    if not tickets:
        return NO_TICKETS_REPLY
    lines = "\n".join(
        f"• {_nombre_para_el_cliente(t)} (folio #{t.id}): {_etiqueta_estado(t)}" for t in tickets
    )
    return f"Estatus de tus solicitudes:\n{lines}"


# El seguimiento de «su caso» (se suma a `_STATUS_INTENT_RE`, que no cambia). Se busca sobre el texto
# normalizado. «estado» a secas NO entra a propósito: «Estado de cuenta» es un servicio del catálogo.
_CASO_INTENT_RE = re.compile(
    r"\bmi caso\b|\bel caso\b|\bmi folio\b|\bel folio\b|\bcaso para analisis\b|\bcaso\s*#?\s*\d|#\s*\d"
    r"|\bya (?:lo |la |me |nos )?(?:vio|vieron|reviso|revisaron|checo|checaron|atendio|atendieron"
    r"|contesto|contestaron|respondio|respondieron|resolvio|resolvieron)\b"
    r"|\bnovedad(?:es)?\b|\bnoticias\b"
)
# Pregunta por UN caso (singular): la que se contesta preguntando cuál. «estatus», «mis solicitudes»,
# «mis casos» piden todos y siguen recibiendo la lista.
_UN_CASO_RE = re.compile(r"\b(?:mi|el) (?:caso|folio)\b|\bcaso para analisis\b|\bya (?:lo|la|me|nos)\b")
_FOLIO_NOMBRADO_RE = re.compile(
    r"#\s*(\d{1,9})\b|\b(?:folio|caso|ticket)\s*(?:numero|num|no)?\.?\s*#?\s*(\d{1,9})\b"
)
_RESPUESTA_ESTATUS_RE = re.compile(r"^(?:#|folio |caso )?\s*(\d{1,9})$|^(todos|todas)$")


def _quiere_estatus(session: Session, conv: Conversation, text: str) -> bool:
    """La intención de estatus de siempre (`_asks_status`, en cualquier estado) o, con la perilla del
    folio encendida, el seguimiento de «su caso» de quien tiene folios abiertos. Solo en los estados
    que no esperan otra respuesta (D16) y solo si el mensaje es corto: uno largo es información, y la
    regla del folio le pregunta a dónde va. Sin folios abiertos «quiero que revisen mi caso» es
    pedir un caso, no preguntar por uno."""
    if _asks_status(text):
        return True
    if not settings.contexto_folio_enabled or conv.state not in ESTADOS_ESTATUS_LIBRE:
        return False
    n = nlu._norm(text)
    if len(n.split()) > MAX_PALABRAS_ESTATUS or _CASO_INTENT_RE.search(n) is None:
        return False
    return bool(_folios_abiertos(session, conv.phone))


def _folio_nombrado(text: str) -> int | None:
    m = _FOLIO_NOMBRADO_RE.search(nlu._norm(text))
    return int(next(g for g in m.groups() if g)) if m else None


def _estatus_de_folio(session: Session, phone: str, numero: int) -> str:
    """El estatus de UN folio del cliente. Uno ajeno, borrado o inexistente recibe la MISMA respuesta:
    distinguirlos confirmaría a quien teclea números qué folios existen."""
    ticket = session.get(Ticket, numero)
    if ticket is None or ticket.phone != phone or not ticket.habilitado:
        return FOLIO_NO_ENCONTRADO.format(folio=numero)
    return ESTATUS_FOLIO.format(
        folio=ticket.id, servicio=_nombre_para_el_cliente(ticket), estado=_etiqueta_estado(ticket),
    )


def _pregunta_estatus(folios: list[Ticket]) -> str:
    folios = folios[:MAX_FOLIOS_EN_PREGUNTA]
    lineas = [f"#{t.id} · {_nombre_para_el_cliente(t)}" for t in folios]
    return "\n".join([ESTATUS_PREGUNTA.format(folio=folios[0].id), *lineas, ESTATUS_TODOS_PIE])


def _filas_estatus(session: Session, phone: str) -> list[tuple[str, str]]:
    """Filas de la lista nativa de la pregunta «¿de cuál folio?». El id de cada fila es el folio
    («#74»): el toque llega como si el cliente lo hubiera tecleado."""
    folios = _folios_abiertos(session, phone)[:MAX_FOLIOS_EN_PREGUNTA]
    if len(folios) < 2:
        return []
    filas = [(f"#{t.id}", f"#{t.id} · {_nombre_para_el_cliente(t)}") for t in folios]
    filas.append((ESTATUS_ID_TODOS, ESTATUS_FILA_TODOS))
    return filas


def _responder_estatus(session: Session, conv: Conversation, text: str) -> str:
    """«¿Cómo va mi caso?»: el estatus del folio que nombra el cliente; si pregunta por UN caso sin
    nombrar ninguno y tiene varios abiertos, cuál; en lo demás, la lista de siempre."""
    if settings.contexto_folio_enabled:
        numero = _folio_nombrado(text)
        if numero is not None:
            return _estatus_de_folio(session, conv.phone, numero)
        if conv.state in ESTADOS_ESTATUS_LIBRE and _UN_CASO_RE.search(nlu._norm(text)):
            abiertos = _folios_abiertos(session, conv.phone)
            if len(abiertos) >= 2:
                return _pregunta_estatus(abiertos)
    return _status_reply(session, conv.phone)


def _contesta_la_pregunta_de_estatus(session: Session, conv: Conversation, text: str) -> str | None:
    """El número (o «todos») con que el cliente contesta «¿de cuál folio?». Solo vale si esa fue lo
    último que le dijo el bot: un número suelto sin esa pregunta no es un folio. Sin estado en la
    FSM, así que no hay nada que caduque ni que le robe una respuesta a otra pregunta."""
    if not settings.contexto_folio_enabled:
        return None
    m = _RESPUESTA_ESTATUS_RE.match(nlu._norm(text).strip("*_. "))
    if m is None:
        return None
    ultimo = session.exec(
        select(Message.body)
        .where(Message.phone == conv.phone)
        .where(Message.direction == Direction.OUT)
        .order_by(Message.id.desc())  # type: ignore[union-attr]
    ).first()
    if not ultimo or not ultimo.startswith(ESTATUS_PREGUNTA_INICIO):
        return None
    if m.group(2):
        return _status_reply(session, conv.phone)
    return _estatus_de_folio(session, conv.phone, int(m.group(1)))


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
    if not capturando and _quiere_estatus(session, conv, text):
        return [_responder_estatus(session, conv, text)]

    # Saludo o despedida: trato social, no un trámite. Al saludo se responde con
    # el saludo y el menú principal de categorías (D7, 2026-10-08: un cliente que
    # no ve las opciones no sabe qué puede pedir); a la despedida, con un cierre
    # cálido. Ninguno pasa por la deducción ni ensucia la bitácora.
    # Exclusiones: "esperando_nombre" (un saludo podría ser el nombre real) y
    # "pidiendo_requisitos" (un "ok" o un "hola" ahí es la respuesta al dato).
    # En "confirmar" solo aplica el saludo — "ok"/"listo" son intentos de
    # confirmar y los reatiende su handler con el re-prompt de sí/no.
    # Los estados de aclaración (casos-analisis) también: ahí un «hola» o un «gracias» no es trato
    # social, es lo que el cliente contesta a la pregunta pendiente.
    social = _social_kind(text)
    if social == "saludo" and conv.state not in {"esperando_nombre", "pidiendo_requisitos", *ESTADOS_ACLARANDO}:
        _set_state(session, conv, "menu_categoria")
        return [_greeting_for(client) + "\n\n" + catalog.format_category_menu()]
    if social == "despedida" and conv.state not in {
        "esperando_nombre", "confirmar", "pidiendo_requisitos", *ESTADOS_ACLARANDO
    }:
        return [FAREWELL_REPLY]

    # La FSM es `_HANDLERS`, debajo del último handler (un dict de módulo solo
    # puede nombrar funciones ya definidas).
    return _handler_de(conv.state)(session, client, conv, text, normalized)


def _handle_escalado(session: Session, client: Client, conv: Conversation, text: str, normalized: str) -> list[str]:
    """El cliente ya espera a un asesor: el bot no lo deja sin respuesta, pero
    tampoco lo trata como una conversación nueva.

    Orden: «menu» retoma el bot → «¿cómo va?» contesta el estatus → un pedido
    concreto de trámite se atiende → lo demás recibe la espera. `handle_incoming`
    lo invoca sin el preludio de `_dispatch` (ver su rama `escalado`)."""
    if normalized in MENU_KEYWORDS:
        _set_state(session, conv, "menu_categoria")
        return ["Claro, continuemos aquí. " + catalog.format_category_menu()]
    if _quiere_estatus(session, conv, text):
        return [_responder_estatus(session, conv, text)]
    # Aunque esté esperando a un asesor, un pedido concreto de trámite
    # se atiende (deducción, multi-servicio o mini-menú de parecidos):
    # el "espera" enlatado es solo para mensajes sin petición clara.
    replies = _try_service_request(session, conv, text)
    if replies:
        return replies
    if _latest_open_ticket(session, client.phone) is None:
        # Promesa sin registro: el ticket de la escalada ya se
        # cerró (o nunca existió) y el cliente sigue escribiendo.
        # Sin esto, "un asesor te contactará" no avisa a NADIE —
        # se re-crea la solicitud para que el panel la muestre.
        return _escalate(session, client, conv, text, reply=ESCALATED_WAIT_REPLY)
    # Ruta de MAYOR frecuencia de todo el flujo: cada mensaje de
    # seguimiento de un cliente ya escalado cae aquí. Sin el
    # aviso, es donde más veces se prometería atención inmediata
    # a las 3 de la mañana.
    return [_con_horario(ESCALATED_WAIT_REPLY)]


# ---- Regla del folio (casos-analisis, diseño §5.1 y §5.4) ----
#
# El cliente con un folio abierto escribe información («ya tengo la CURP, también revisé semanas
# cotizadas…») o manda un archivo. Antes, el bot troceaba ese texto en trámites nuevos; ahora
# pregunta «¿es para tu folio #N?» y, si sí, lo ANEXA a ese folio sin deducir nada. Detrás de la
# perilla `contexto_folio_enabled` (apagada): apagada, nada de esto corre.

# Tope del texto que se anexa a un folio. MAX_NOTE_TEXT (500) sigue valiendo para las respuestas a
# requisitos: un caso real (2026-09-24) pasaba de 900 caracteres.
MAX_NOTE_TEXT_CASO = 2000
# Dónde corre la regla (tabla cerrada de §5.1; el estado manda, no el umbral). Fuera, a propósito:
# `confirmar`, `eligiendo_sugerencia` y `esperando_nombre` esperan una respuesta, y la regla la
# secuestraría (D16).
ESTADOS_REGLA_FOLIO = frozenset(
    {"inicio", "menu_categoria", "menu_servicio", "escalado", "pidiendo_requisitos"}
)
ESTADOS_ACLARANDO = frozenset({"aclarando_folio", "aclarando_caso"})
# Folios en la lista nativa: 8 + «Es algo nuevo» + «Otro folio» = las 10 filas de Meta.
MAX_FOLIOS_EN_LISTA = 8
# Títulos de botón ≤ 20 (whatsapp_client.BUTTON_TITLE_MAX recorta EN SILENCIO). Los ids son lo que
# el cliente teclea en Twilio, que degrada los botones a texto («si) ✅ Sí, de ese folio»).
FOLIO_BOTONES = [("si", "✅ Sí, de ese folio"), ("nuevo", "🆕 Es algo nuevo")]
FOLIO_FILA_NUEVO = "🆕 Es algo nuevo"
FOLIO_FILA_OTRO = "📋 Otro folio"
FOLIO_PREGUNTA_UNO = "¿Esta información es para tu folio #{folio} ({servicio})?"
FOLIO_PREGUNTA_VARIOS = "¿Esta información es para alguno de tus folios? Responde con el número:"
FOLIO_ARCHIVO = "Recibí tu archivo 📎."
FOLIO_LO_SUMO = "Lo sumo 👍"
FOLIO_LO_SUMO_ARCHIVO = "Lo sumo 📎"
FOLIO_OTRO = "Escribe el número de tu folio, por ejemplo #{folio}."
FOLIO_ANEXADO = "Listo, lo agregué a tu folio #{folio} ({servicio}). Tu asesor lo revisará."
FOLIO_SOLO_ARCHIVOS_NUEVO = "Recibí tu archivo 📎. ¿Qué trámite necesitas?\n\n"
FOLIO_NOMBRE_ESCALADO = "Tu atención con un asesor"
# Respuestas que resuelven la pregunta: por IGUALDAD con el texto normalizado, nunca por prefijo.
# «Sí, y también anexo lo del IMSS» no resuelve: decidir por la primera palabra es decidir por el
# cliente.
NUEVO_WORDS = frozenset({"nuevo", "es algo nuevo", "algo nuevo"})
OTRO_FOLIO_WORDS = frozenset({"otro", "otro folio"})
_FOLIO_TECLEADO_RE = re.compile(r"^#\s*(\d+)$")
_NUMERO_FOLIO_RE = re.compile(r"^#?\s*(\d+)$")


def _nombre_folio(ticket: Ticket) -> str:
    return FOLIO_NOMBRE_ESCALADO if ticket.service_id == "escalado" else ticket.service_name


def _pregunta_folio(folios: list[Ticket]) -> str:
    if len(folios) == 1:
        return FOLIO_PREGUNTA_UNO.format(folio=folios[0].id, servicio=_nombre_folio(folios[0]))
    lineas = [f"{i}. #{t.id} · {_nombre_folio(t)}" for i, t in enumerate(folios, start=1)]
    lineas.append(f"{len(folios) + 1}. {FOLIO_FILA_NUEVO}")
    lineas.append(f"{len(folios) + 2}. {FOLIO_FILA_OTRO}")
    return FOLIO_PREGUNTA_VARIOS + "\n" + "\n".join(lineas)


def _filas_folio(session: Session, conv: Conversation) -> list[tuple[str, str]]:
    """Filas de la lista nativa de `aclarando_folio` (solo con 2+ folios; con uno, botones)."""
    folios = _folios_de_contexto(session, conv)
    if len(folios) < 2:
        return []
    filas = [(str(i), f"#{t.id} · {_nombre_folio(t)}") for i, t in enumerate(folios, start=1)]
    filas.append((str(len(folios) + 1), FOLIO_FILA_NUEVO))
    filas.append((str(len(folios) + 2), FOLIO_FILA_OTRO))
    return filas


def _folios_de_contexto(session: Session, conv: Conversation) -> list[Ticket]:
    """Los folios que se ofrecieron, en el mismo orden, salvo los que ya se cerraron."""
    abiertos = {t.id: t for t in _folios_abiertos(session, conv.phone)}
    return [abiertos[i] for i in _get_context(conv).get("folios", []) if i in abiertos]


def _texto_es_el_dato_pedido(session: Session, conv: Conversation, text: str) -> bool:
    """En `pidiendo_requisitos` la regla se estrecha: «mi CURP es AAAA…» es la respuesta al
    requisito, no información para el folio. Salvo que sea largo: una CURP dentro de un caso de
    900 caracteres no es «la CURP que pedí»."""
    cola = _get_context(conv).get("cola") or []
    if not cola:
        return False
    ticket_id, idx = cola[0]
    largo = len(nlu._norm(text).split()) >= settings.contexto_min_palabras
    return codigos.trae_el_dato(_requisito_de(session, ticket_id, idx), text) and not largo


def _regla_del_folio(
    session: Session, conv: Conversation, text: str, normalized: str, desde_id: int,
) -> list[str] | None:
    """La pregunta «¿es para tu folio?» si toca; None para seguir por el camino de hoy.

    Corre después de lo que tiene intención inequívoca y barata —«menu», el estatus, el saludo,
    el enojo y el pedido de humano (esos dos los decide `handle_incoming` antes)— y antes de la
    deducción de trámites."""
    if not settings.contexto_folio_enabled or conv.state not in ESTADOS_REGLA_FOLIO:
        return None
    if normalized in MENU_KEYWORDS or _quiere_estatus(session, conv, text) or _social_kind(text) == "saludo":
        return None
    if not nlu.es_informativo(text, settings.contexto_min_palabras):
        return None
    if conv.state == "pidiendo_requisitos" and _texto_es_el_dato_pedido(session, conv, text):
        return None
    folios = _folios_abiertos(session, conv.phone)
    if not folios:
        return None
    return _preguntar_folio(session, conv, folios, text[:MAX_NOTE_TEXT_CASO], desde_id)


def _regla_del_folio_archivo(session: Session, conv: Conversation, desde_id: int) -> list[str] | None:
    """Un archivo soportado con folios abiertos: la misma pregunta, precedida del acuse."""
    if not settings.contexto_folio_enabled or conv.state not in ESTADOS_REGLA_FOLIO:
        return None
    folios = _folios_abiertos(session, conv.phone)
    if not folios:
        return None
    replies = _preguntar_folio(session, conv, folios, "", desde_id)
    return [f"{FOLIO_ARCHIVO} {replies[0]}"]


def _preguntar_folio(
    session: Session, conv: Conversation, folios: list[Ticket], texto: str, desde_id: int,
) -> list[str]:
    folios = folios[:MAX_FOLIOS_EN_LISTA]
    contexto = {
        "texto": texto,
        "desde_id": desde_id,
        "folios": [t.id for t in folios],
        "estado_previo": conv.state,
        "contexto_previo": _get_context(conv),
        # El de ANTES de la pregunta: al volver, la caducidad de la captura se mide sobre la
        # antigüedad real, no sobre el updated_at que renueva _set_state.
        "updated_previo": conv.updated_at.isoformat(),
    }
    _set_state(session, conv, "aclarando_folio", contexto)
    return [_pregunta_folio(folios)]


def _handle_aclarando_folio(
    session: Session, client: Client, conv: Conversation, text: str, normalized: str,
) -> list[str]:
    contexto = _get_context(conv)
    folios = _folios_de_contexto(session, conv)
    n = nlu._norm(text)
    if not folios:
        # Se cerraron todos mientras el cliente contestaba: no hay a qué anexar.
        return _es_algo_nuevo(session, client, conv, contexto)
    abiertos = {t.id: t for t in _folios_abiertos(session, conv.phone)}

    if contexto.get("esperando_numero"):
        m = _NUMERO_FOLIO_RE.match(n)
        if m and int(m.group(1)) in abiertos:
            return _anexar_y_volver(session, conv, contexto, abiertos[int(m.group(1))])
        if m:  # un número ajeno o de un folio cerrado: se vuelve a pedir
            return _bump_or_escalate(session, client, conv, text, FOLIO_OTRO.format(folio=folios[0].id))
        return _sumar_o_repreguntar(session, client, conv, contexto, text, FOLIO_OTRO.format(folio=folios[0].id))

    eleccion: Ticket | str | None = None
    if len(folios) == 1:
        # «1) Sí, de ese folio / 2) Es algo nuevo» en Twilio: el dígito vale lo que el botón.
        if n == "1" or n in {nlu._norm(w) for w in YES_WORDS}:
            eleccion = folios[0]
        elif n == "2" or n in {nlu._norm(w) for w in NO_WORDS} or n in NUEVO_WORDS:
            eleccion = "nuevo"
    else:
        indice = _parse_int(n)
        tecleado = _FOLIO_TECLEADO_RE.match(n)
        if indice is not None and 1 <= indice <= len(folios):
            eleccion = folios[indice - 1]
        elif indice == len(folios) + 1 or n in NUEVO_WORDS or n in {nlu._norm(w) for w in NO_WORDS}:
            eleccion = "nuevo"
        elif indice == len(folios) + 2 or n in OTRO_FOLIO_WORDS:
            eleccion = "otro"
        elif tecleado and int(tecleado.group(1)) in abiertos:
            eleccion = abiertos[int(tecleado.group(1))]
    if eleccion == "nuevo":
        return _es_algo_nuevo(session, client, conv, contexto)
    if eleccion == "otro":
        _set_state(session, conv, "aclarando_folio", {**contexto, "esperando_numero": True})
        return [FOLIO_OTRO.format(folio=folios[0].id)]
    if isinstance(eleccion, Ticket):
        return _anexar_y_volver(session, conv, contexto, eleccion)
    return _sumar_o_repreguntar(session, client, conv, contexto, text, _pregunta_folio(folios))


def _sumar_o_repreguntar(
    session: Session, client: Client, conv: Conversation, contexto: dict, text: str, reprompt: str,
) -> list[str]:
    """Mientras la pregunta está pendiente nada queda en silencio: la información nueva se suma
    («Lo sumo 👍»); lo que no es ni información ni respuesta («ok», «¿y cuánto tarda?») recibe la
    pregunta otra vez, como `confirmar`, y no se suma ni se liga."""
    if nlu.es_informativo(text, settings.contexto_min_palabras):
        previo = contexto.get("texto") or ""
        contexto["texto"] = (previo + "\n\n" + text if previo else text)[:MAX_NOTE_TEXT_CASO]
        _set_state(session, conv, conv.state, contexto)
        return [FOLIO_LO_SUMO]
    return _bump_or_escalate(session, client, conv, text, reprompt)


def _anexar_a_folio(
    session: Session, ticket: Ticket, contexto: dict, mensajes: list[Message],
    capa: str = "folio", en_notas: bool = True,
) -> int:
    """Anexa el texto acumulado a las notas del folio y le liga `mensajes` (los de
    `_mensajes_a_ligar`). Devuelve cuántos archivos se ligaron. Nada se deduce del texto.
    `en_notas=False` cuando el texto ya va en las notas de un folio recién creado (el caso)."""
    texto = contexto.get("texto") or ""
    if texto:
        if en_notas:
            sello = horario.ahora().strftime("%Y-%m-%d %H:%M")
            ticket.notes = (ticket.notes or "") + f"\n\n[cliente, {sello}] " + texto[:MAX_NOTE_TEXT_CASO]
            session.add(ticket)
        session.add(TicketActivity(
            ticket_id=ticket.id, username=asignacion.ACTOR, action=f"info-cliente:{len(texto)}c",
        ))
    archivos = _ligar_mensajes(session, ticket, mensajes)
    # Un evento por mensaje y SIN texto: el texto vive en el folio; el largo, en la actividad.
    session.add(NluEvent(
        phone=ticket.phone, text="", capa=capa, service_id=None,
        office_id=_oficina_de(session, ticket.phone),
    ))
    session.commit()
    return archivos


def _mensajes_a_ligar(session: Session, phone: str, desde_id: int, hasta_id: int) -> list[Message]:
    """Los mensajes que el cliente sumó mientras se aclaraba, por pertenencia: del teléfono,
    entrantes, habilitados, sin folio, con id entre `desde_id` y `hasta_id`, y que cumplan el mismo
    predicado con que se sumaron —archivo o informativo—. Un «ok» o un número en medio no entran.

    Solo lee, y se llama ANTES de la primera escritura del turno: `es_informativo` lee el catálogo,
    que abre su propia sesión (`catalog._refresh`), y con escrituras pendientes en la misma
    conexión su cierre las desharía (la trampa de `asignacion._por_producto`)."""
    candidatos = session.exec(
        select(Message)
        .where(Message.phone == phone)
        .where(Message.direction == Direction.IN)
        .where(Message.habilitado == True)  # noqa: E712
        .where(Message.ticket_id == None)  # noqa: E711
        .where(Message.id >= desde_id)
        .where(Message.id <= hasta_id)
    ).all()
    return [
        m for m in candidatos
        if m.media_ref is not None or nlu.es_informativo(m.body, settings.contexto_min_palabras)
    ]


def _ligar_mensajes(session: Session, ticket: Ticket, mensajes: list[Message]) -> int:
    """Liga `mensajes` al folio con el ORM (el espejo MSSQL escucha `after_update` de Message en
    el mapper; un UPDATE de Core no lo dispararía). Devuelve cuántos archivos. No hace commit."""
    archivos = 0
    for mensaje in mensajes:
        mensaje.ticket_id = ticket.id
        session.add(mensaje)
        archivos += mensaje.media_ref is not None
    if archivos:
        session.add(TicketActivity(
            ticket_id=ticket.id, username=asignacion.ACTOR, action=f"archivo-cliente:{archivos}",
        ))
    return archivos


def _volver_al_estado_previo(session: Session, conv: Conversation, contexto: dict) -> str:
    estado = contexto.get("estado_previo") or "menu_categoria"
    _set_state(session, conv, estado, contexto.get("contexto_previo") or {})
    previo = contexto.get("updated_previo")
    if previo:
        conv.updated_at = datetime.fromisoformat(previo)
        session.add(conv)
        session.commit()
    return estado


def _por_ligar(session: Session, phone: str, contexto: dict) -> list[Message]:
    """`_mensajes_a_ligar` desde `desde_id` hasta el mensaje que resuelve la pregunta."""
    hasta = session.exec(
        select(Message.id)
        .where(Message.phone == phone)
        .where(Message.direction == Direction.IN)
        .order_by(Message.id.desc())  # type: ignore[union-attr]
    ).first()
    desde = contexto.get("desde_id")
    if desde is None or hasta is None:
        return []
    return _mensajes_a_ligar(session, phone, desde, hasta)


def _naive_utc(momento: datetime) -> datetime:
    """SQLite devuelve datetimes naive y utcnow() es aware: se comparan ambos como naive-UTC."""
    if momento.tzinfo is not None:
        return momento.astimezone(timezone.utc).replace(tzinfo=None)
    return momento


def _archivos_previos_al_caso(session: Session, phone: str, desde_id: int | None) -> list[Message]:
    """La racha de archivos que el cliente mandó justo ANTES del mensaje que abrió la pregunta del
    caso (`desde_id`), para ligarla al folio cuando el caso se abre (operador, 2026-10-12: que los
    archivos lleguen a la ficha). `_por_ligar` solo mira de `desde_id` en adelante, y el gestor que
    manda las fotos del trabajador y DESPUÉS cuenta el caso las dejaba sin folio.

    Se retrocede desde ese mensaje entre los entrantes visibles del teléfono y se corta en el primero
    que NO sea un archivo sin folio —un texto del cliente es otra conversación; un archivo con folio
    ya es de otra solicitud— o que esté a más de `VENTANA_ARCHIVOS_PREVIOS` del mensaje del caso. Se
    queda con los `MAX_ARCHIVOS_PREVIOS` más cercanos. Sin preguntar: ligar de
    más cuesta una burbuja mal etiquetada en el chat; no ligar, que la foto no salga en ningún folio.

    Solo lee, y se llama ANTES de la primera escritura del turno (igual que `_por_ligar`)."""
    ancla = session.get(Message, desde_id) if desde_id is not None else None
    if ancla is None:
        return []
    mas_viejo_permitido = _naive_utc(ancla.created_at) - VENTANA_ARCHIVOS_PREVIOS
    anteriores = session.exec(
        select(Message)
        .where(Message.phone == phone)
        .where(Message.direction == Direction.IN)
        .where(Message.habilitado == True)  # noqa: E712
        .where(Message.id < ancla.id)
        .order_by(Message.id.desc())  # type: ignore[union-attr]
        .limit(MAX_ARCHIVOS_PREVIOS)
    ).all()
    racha: list[Message] = []
    for m in anteriores:
        if m.media_ref is None or m.ticket_id is not None or _naive_utc(m.created_at) < mas_viejo_permitido:
            break
        racha.append(m)
    return racha


def _anexar_y_volver(session: Session, conv: Conversation, contexto: dict, ticket: Ticket) -> list[str]:
    _anexar_a_folio(session, ticket, contexto, _por_ligar(session, conv.phone, contexto))
    replies = [_con_horario(FOLIO_ANEXADO.format(folio=ticket.id, servicio=_nombre_folio(ticket)))]
    estado = _volver_al_estado_previo(session, conv, contexto)
    if estado == "pidiendo_requisitos":
        if _captura_caducada(conv):
            _set_state(session, conv, "menu_categoria")
        else:
            pregunta, _ = _siguiente_pregunta(session, _get_context(conv).get("cola") or [])
            if pregunta:
                replies.append(pregunta)
    return replies


def _es_algo_nuevo(session: Session, client: Client, conv: Conversation, contexto: dict) -> list[str]:
    """«Es algo nuevo»: el camino de hoy, pero llamando al handler de `estado_previo` y no a
    `_dispatch` —sus atajos son por subcadena, y un caso de 900 caracteres con la palabra
    «servicios» dentro respondería con el catálogo—."""
    texto = contexto.get("texto") or ""
    # Regla de la revisión sobre el texto acumulado, en el mismo turno y heredando `desde_id`
    # (§5.1). Solo archivos con la revisión encendida: los botones, con texto vacío.
    if settings.revision_enabled and (not texto or nlu.pide_revision(texto, settings.contexto_min_palabras)):
        return _preguntar_caso(session, conv, {**contexto, "texto": texto})
    estado = _volver_al_estado_previo(session, conv, contexto)
    if not texto:
        _set_state(session, conv, "menu_categoria")
        return [FOLIO_SOLO_ARCHIVOS_NUEVO + catalog.format_category_menu()]
    # No se anexó: lo que traiga (una CURP, un correo) va a la ficha como hoy.
    _extract_client_data(session, conv.phone, texto)
    return _handler_de(estado)(session, client, conv, texto, texto.strip().lower())


def _caduca_aclaracion(session: Session, conv: Conversation) -> None:
    """TTL vencido, o la perilla que abrió la pregunta se apagó en vuelo: el contexto se descarta
    (los mensajes sin folio se quedan así; el asesor los ve en el chat) y la conversación va a un
    estado neutral. Solo `pidiendo_requisitos` se restaura —nunca un estado con efectos de alta—;
    si esa captura también venció, cae como hoy."""
    if conv.state not in ESTADOS_ACLARANDO:
        return
    perilla = settings.contexto_folio_enabled if conv.state == "aclarando_folio" else settings.revision_enabled
    if perilla and not _captura_caducada(conv):
        return
    contexto = _get_context(conv)
    if contexto.get("estado_previo") == "pidiendo_requisitos":
        _volver_al_estado_previo(session, conv, contexto)
    else:
        _set_state(session, conv, "menu_categoria")



# ---- Regla de la revisión y caso para análisis (casos-analisis, diseño §5.2 y §5.3) ----
#
# El cliente sin folio abierto que pide que alguien revise su caso («tengo una situación con mi
# pensión, ¿qué se puede hacer?») o que mete varios trámites en un texto largo recibe una lista:
# «Revisión de un asesor» primero, cada servicio que nombró, y «Otro trámite». Antes se le
# troceaba en «¿Confirmas estos 3 trámites?». Detrás de `revision_enabled` (apagada).

CASO_SERVICE_ID = "analisis"
CASO_NOMBRE = "Caso para análisis"
CASO_NOTA = "Caso para análisis. Mensaje del cliente:\n"
CASO_NOTA_ARCHIVOS = "Caso para análisis. Archivos del cliente."
# Los archivos que el cliente manda ANTES de contar su caso se ligan al abrirlo (operador,
# 2026-10-12): la racha inmediata anterior al mensaje que lo disparó, de los últimos 30 minutos y de
# a lo más 25 archivos —los más cercanos—. Se liga sin preguntar: ver `_archivos_previos_al_caso`.
VENTANA_ARCHIVOS_PREVIOS = timedelta(minutes=30)
MAX_ARCHIVOS_PREVIOS = 25
# Revisión + 7 servicios + «Otro trámite» = 9 filas (≤ 10 de Meta).
MAX_SERVICIOS_EN_CASO = 7
# Dónde corre: la tabla de §5.2. `pidiendo_requisitos` no, a propósito: ahí el cliente tiene un
# folio abierto (el que pide el dato), y con folio la que pregunta es la regla del folio.
ESTADOS_REGLA_REVISION = frozenset({"inicio", "menu_categoria", "menu_servicio", "escalado"})
# Copy de §5.2 y D18: claro y corto, sin precio ni cotización (D11).
CASO_PREGUNTA_LISTA = (
    "Puedo pedir a un asesor que revise tu caso completo, o abrir ahora uno de estos trámites. "
    "¿Qué prefieres? Responde con el número:"
)
CASO_PREGUNTA_BOTONES = "¿Quieres que un asesor revise tu caso, o buscas un trámite en concreto?"
CASO_BOTONES = [("revision", "🔎 Revisión"), ("tramite", "📋 Un trámite")]
CASO_FILA_REVISION = "🔎 Revisión de un asesor"
CASO_FILA_OTRO = "📋 Otro trámite"
CASO_RECIBIDO = "Gracias, recibí tu caso. Tu folio es el #{folio}. 🙏 Un asesor lo revisará y te contactará."
CASO_OTRO_TRAMITE = "Claro. "
# Por igualdad con el texto normalizado, como las respuestas del folio.
REVISION_WORDS = frozenset({"revision", "revision de un asesor", "revisar"})
OTRO_TRAMITE_WORDS = frozenset({"tramite", "un tramite", "otro tramite", "otro"})


def _pregunta_caso(servicios: list[str]) -> str:
    nombres = [s.name for s in (catalog.service_by_id(i) for i in servicios) if s is not None]
    if not nombres:
        return CASO_PREGUNTA_BOTONES
    lineas = [f"1. {CASO_FILA_REVISION}"]
    lineas += [f"{i}. {nombre}" for i, nombre in enumerate(nombres, start=2)]
    lineas.append(f"{len(nombres) + 2}. {CASO_FILA_OTRO}")
    return CASO_PREGUNTA_LISTA + "\n" + "\n".join(lineas)


def _servicios_del_caso(conv: Conversation) -> list:
    ids = _get_context(conv).get("servicios", [])
    return [s for s in (catalog.service_by_id(i) for i in ids) if s is not None]


def _filas_caso(conv: Conversation) -> list[tuple[str, str]]:
    """Filas de la lista nativa de `aclarando_caso` (solo con servicios; sin ellos, botones). Un
    nombre de más de 24 caracteres lo pasa entero a la descripción `whatsapp_client`."""
    servicios = _servicios_del_caso(conv)
    if not servicios:
        return []
    filas = [("1", CASO_FILA_REVISION)]
    filas += [(str(i), s.name) for i, s in enumerate(servicios, start=2)]
    filas.append((str(len(servicios) + 2), CASO_FILA_OTRO))
    return filas


def _regla_de_la_revision(
    session: Session, conv: Conversation, text: str, normalized: str, desde_id: int,
) -> list[str] | None:
    """La lista «¿revisión de un asesor o un trámite?» si toca; None para seguir por el camino de
    hoy. Solo sin folios abiertos: con folio, la que pregunta es la regla del folio (y con esa
    perilla apagada, el fallo original sigue: es la fila OFF/ON de la matriz)."""
    if not settings.revision_enabled or conv.state not in ESTADOS_REGLA_REVISION:
        return None
    if normalized in MENU_KEYWORDS or _quiere_estatus(session, conv, text) or _social_kind(text) == "saludo":
        return None
    if not nlu.pide_revision(text, settings.contexto_min_palabras):
        return None
    if _folios_abiertos(session, conv.phone):
        return None
    return _preguntar_caso(session, conv, {
        "texto": text[:MAX_NOTE_TEXT_CASO],
        "desde_id": desde_id,
        "estado_previo": conv.state,
        "contexto_previo": _get_context(conv),
        "updated_previo": conv.updated_at.isoformat(),
    })


def _preguntar_caso(session: Session, conv: Conversation, contexto: dict) -> list[str]:
    """Pasa a `aclarando_caso`. `contexto` trae texto, `desde_id` y el estado previo; si viene de
    `aclarando_folio` («Es algo nuevo») los hereda tal cual."""
    texto = contexto.get("texto") or ""
    servicios = [s.id for s in nlu.servicios_nombrados(texto)][:MAX_SERVICIOS_EN_CASO] if texto else []
    nuevo = {
        "texto": texto,
        "servicios": servicios,
        "desde_id": contexto.get("desde_id"),
        "estado_previo": contexto.get("estado_previo"),
        "contexto_previo": contexto.get("contexto_previo") or {},
        "updated_previo": contexto.get("updated_previo"),
    }
    _set_state(session, conv, "aclarando_caso", nuevo)
    return [_pregunta_caso(servicios)]


def _handle_aclarando_caso(
    session: Session, client: Client, conv: Conversation, text: str, normalized: str,
) -> list[str]:
    contexto = _get_context(conv)
    servicios = _servicios_del_caso(conv)
    n = nlu._norm(text)
    eleccion = None
    if servicios:
        indice = _parse_int(n)
        if indice == 1 or n in REVISION_WORDS:
            eleccion = "revision"
        elif indice is not None and 2 <= indice <= len(servicios) + 1:
            eleccion = servicios[indice - 2]
        elif indice == len(servicios) + 2 or n in OTRO_TRAMITE_WORDS:
            eleccion = "otro"
        else:
            # El cliente escribe el nombre en vez del número (C, operador 2026-10-10).
            eleccion = next((s for s in servicios if nlu._norm(s.name) == n), None)
    elif n == "1" or n in REVISION_WORDS:
        eleccion = "revision"
    elif n == "2" or n in OTRO_TRAMITE_WORDS:
        eleccion = "otro"

    if eleccion == "revision":
        return _abrir_caso(session, client, conv, contexto)
    texto = contexto.get("texto") or ""
    if eleccion == "otro":
        # Los mensajes quedan sin folio; lo que traiga el texto va a la ficha, como hoy.
        if texto:
            _extract_client_data(session, conv.phone, texto)
        _set_state(session, conv, "menu_categoria")
        return [CASO_OTRO_TRAMITE + catalog.format_category_menu()]
    if eleccion is not None:
        if texto:
            _extract_client_data(session, conv.phone, texto)
        replies = _confirmar(session, conv, [eleccion], texto, detectados=_detectar_codigos(texto))
        pendiente = _get_context(conv)
        pendiente["desde_id"] = contexto.get("desde_id")
        _set_state(session, conv, "confirmar", pendiente)
        return replies
    return _sumar_o_repreguntar(session, client, conv, contexto, text, _pregunta_caso(contexto.get("servicios", [])))


def _abrir_caso(session: Session, client: Client, conv: Conversation, contexto: dict) -> list[str]:
    """Abre el caso para análisis (§5.3): cada «Revisión» confirmada es un folio nuevo.

    No anexa a un caso que ya esté abierto (el diseño §5.3 lo hacía, «un caso por teléfono»;
    el operador lo corrigió el 2026-10-12, caso #74): aquí solo se llega por «Es algo nuevo»
    —con folios abiertos la regla de la revisión no dispara— o sea, después de que el cliente
    dijo que NO era del folio abierto. Juntarlo era darle el sí a lo que acababa de negar. Lo que
    sí es del mismo folio se anexa antes, en la pregunta del folio (`_anexar_y_volver`).

    Sin producto (D2): `asignacion.elegir` SIN `service_ids`, como la escalada; el asesor puede
    salir `None` y el folio queda en el pool de la oficina. Sin consultar `MAX_FOLIOS_ABIERTOS`:
    un caso no es un trámite que el cliente multiplica por catálogo, sino una revisión que él
    confirmó. La carrera de dos webhooks simultáneos es la misma clase que la del alta normal y
    queda fuera, como el candado (§11)."""
    # Los de antes del texto del caso (la racha de archivos) y los de desde él: ids disjuntos, así
    # que ninguno se liga dos veces. Antes de escribir nada, como `_por_ligar`.
    mensajes = _archivos_previos_al_caso(session, conv.phone, contexto.get("desde_id")) + _por_ligar(
        session, conv.phone, contexto,
    )
    texto = contexto.get("texto") or ""
    asesor, motivo = asignacion.elegir(session, client.phone)
    ticket = Ticket(
        phone=client.phone,
        client_name=client.name,
        category="(análisis)",
        service_id=CASO_SERVICE_ID,
        service_name=CASO_NOMBRE,
        price_info="N/A",
        status=TicketStatus.NUEVO,
        escalado=True,
        notes=(CASO_NOTA + texto[:MAX_NOTE_TEXT_CASO]) if texto else CASO_NOTA_ARCHIVOS,
        provider=conv.provider,
        office_id=client.office_id,
        assigned_to=asesor,
    )
    session.add(ticket)
    session.commit()
    session.refresh(ticket)
    asignacion.bitacora(session, ticket, motivo)  # no escribe nada si `asesor` es None
    _anexar_a_folio(session, ticket, contexto, mensajes, capa=CASO_SERVICE_ID, en_notas=False)
    _set_state(session, conv, "escalado")
    return [_con_horario(CASO_RECIBIDO.format(folio=ticket.id))]


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
    # Si el recorte no quitó NADA, el servicio no se nombró en el texto: lo
    # dedujo un modelo. Entonces los "parecidos" que salgan del resto no son
    # otro trámite, son las hermanas del mismo — y anunciarlas como un segundo
    # pedido es lo que el 2026-09-11 convirtió "alta medica" en dos folios.
    if resto.strip() == nlu._norm(text).strip():
        return []
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
    empate = nlu.empate_por_tramo(text, services)
    if empate:
        # Empate de un mismo tramo («vigencia de derechos»): no son dos pedidos, es uno que el
        # texto no alcanza a decir cuál. Se pregunta cuál y lo claro del mismo mensaje se anota
        # (A1, operador 2026-10-10). Antes el bot pedía confirmar los dos.
        claros = [s for s in services if s.id not in {e.id for e in empate}]
        if not claros:
            _set_state(session, conv, "eligiendo_sugerencia", {"suggestion_ids": [s.id for s in empate]})
            opciones = "\n".join(f"{i}. {s.name}" for i, s in enumerate(empate, 1))
            carita = "" if prefijo else " 😊"  # el saludo del prefijo ya trae la suya
            return [
                f"{prefijo}Tenemos varios trámites parecidos{carita} ¿Cuál necesitas? "
                f"Responde con el número:\n{opciones}"
            ]
        return _preguntar_cual(session, conv, empate, claros, text, prefijo)
    pendientes = _pendientes_por_aclarar(text, services)
    if not pendientes:
        return _confirmar(session, conv, services, text, prefijo)
    return _preguntar_cual(session, conv, pendientes, services, text, prefijo)


def _preguntar_cual(
    session: Session, conv: Conversation, pendientes: list, resueltos: list, text: str, prefijo: str
) -> list[str]:
    """Anota lo ya resuelto y pregunta cuál de `pendientes` era el otro trámite del mensaje."""
    # Los códigos se detectan (y clasifican con Ollama) AQUÍ, en el turno que
    # ya pagó la deducción: el turno de la elección solo trae "1" y ahí ya no
    # habría de dónde sacarlos. Viajan en el contexto hasta el _confirmar real.
    context: dict = {
        "suggestion_ids": [s.id for s in pendientes],
        "resueltos_ids": [s.id for s in resueltos],
    }
    detectados = _detectar_codigos(text)
    if detectados:
        context["codigos"] = detectados
    _set_state(session, conv, "eligiendo_sugerencia", context)
    lines = "\n".join(f"{i}. {s.name}" for i, s in enumerate(pendientes, 1))
    ya = ", ".join(f"'{s.name}'" for s in resueltos)
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

    # Twilio numera Sí/No como «1) Sí / 2) No» cuando hay UN servicio. Con varios, los dígitos
    # ya eligen servicios de la lista (B1) y no se tocan.
    if len(services) == 1 and normalized in {"1", "2"}:
        normalized = "si" if normalized == "1" else "no"

    if normalized in YES_WORDS and services:
        # Un servicio con folio abierto no abre otro, y hay un tope de abiertos
        # por cliente (EXT-2). Si no queda ninguno por abrir, se le dice qué
        # folios ya tiene y se vuelve al menú: nada pasa por el reparto ni por
        # el espejo. El folio existente lo cierra el asesor, no el bot.
        services, avisos_folios = _filtrar_folios_nuevos(session, client.phone, services)
        if not services:
            _set_state(session, conv, "menu_categoria")
            return ["\n".join(avisos_folios) + " ¿Otro trámite? Escribe *menu*."]
        # Códigos que venían en el mensaje con que pidió el trámite: se copian
        # al folio antes de preguntar nada (ver _requisitos_con_codigos).
        detectados = context.get("codigos") or {}
        # Viene de la lista del caso (§5.2): lo que el cliente fue sumando es del folio que nace.
        # Se calcula antes de escribir (ver `_mensajes_a_ligar`).
        por_ligar = _por_ligar(session, client.phone, context) if len(services) == 1 else []
        tickets = []
        prellenado = False
        # A quién le toca cada uno: se decide POR SERVICIO (decisión del
        # operador, 2026-09-24). Cada folio va a quien atiende ese trámite —lo
        # declara cada colaborador en /catalog—, prefiriendo al titular de la
        # cartera; el mismo asesor se queda varios solo si los atiende, así que
        # un cliente no se parte sin necesidad. Solo lee: nada se escribe
        # hasta crear los folios.
        decisiones = asignacion.elegir_por_servicio(
            session, client.phone, [s.id for s in services]
        )
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
                office_id=client.office_id,
                # Nace en NUEVO CON dueño: TOMADO significaría «alguien lo
                # tiene», falso para un folio que nadie ha mirado, y
                # corrompería el embudo de operacion_stats.
                assigned_to=decisiones[service.id].asesor,
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
        # La bitácora va después del commit porque necesita el id del folio.
        for ticket in tickets:
            asignacion.bitacora(session, ticket, decisiones[ticket.service_id].motivo)
        if por_ligar:
            _ligar_mensajes(session, tickets[0], por_ligar)
        session.commit()

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
                f"(folio #{tickets[0].id}). {_cierre_atencion(TICKET_ALTA_CIERRE)}"
            )
        else:
            lines = "\n".join(f"• '{t.service_name}' — folio #{t.id}" for t in tickets)
            multi = _cierre_atencion(TICKET_ALTA_CIERRE_MULTI)
            cierre = multi if cola else (multi + " ¿Otro trámite? Escribe *menu*.")
            alta = f"¡Listo! ✅ Registré tus {len(tickets)} solicitudes:\n{lines}\n{cierre}"

        if prellenado:
            # Decirlo evita que el cliente crea que se ignoró lo que escribió y
            # lo vuelva a mandar (que es justo lo que pasaba antes).
            alta += " " + REQ_YA_CAPTURADO
        if avisos_folios:
            # Lo que SÍ se abrió va primero; lo que ya existía o no cupo, después,
            # para que el cliente no crea que se ignoró lo que pidió.
            alta += "\n" + "\n".join(avisos_folios)

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

    # Corrección del alcance antes de insistir con el sí/no: "no solo alta
    # medica basica" sobre una lista de dos NO es una respuesta inválida, es el
    # cliente quitando el trámite que no pidió. El 2026-09-11 el agente la
    # trató como error, repitió la pregunta y el cliente acabó diciendo "no" a
    # todo: 24 minutos de conversación y cero folios.
    #
    # Se resuelve con el matcher LITERAL, que es gratis y exacto: si el cliente
    # nombra un subconjunto de lo que ya está en la lista, eso basta. No se le
    # vuelve a preguntar al modelo lo que el cliente ya dijo con todas sus
    # letras — y así un "sí" que llegue después sigue costando cero.
    if services:
        mencionados = nlu._exact_matches(text)[0]
        pedidos = {s.id for s in services}
        fuera = set(mencionados) - pedidos
        if fuera and set(mencionados) & pedidos:
            # Nombra cosas de la lista Y cosas que no están: no está recortando la
            # lista, está haciendo un pedido nuevo («quiero acta y curp» sobre
            # CURP + NSS). Se atiende igual que desde el menú. Antes se tomaba como
            # recorte: quedaba solo la CURP y el acta se perdía sin aviso (prueba
            # del operador, 2026-10-10, folio #70).
            _set_state(session, conv, "menu_categoria")
            return _handle_menu_categoria(session, client, conv, text, normalized)
        # Nombró parte de lo que está en la lista: quiere QUITAR el resto.
        nombrados = [s for s in services if s.id in mencionados]
        if not nombrados and len(mencionados) == 1:
            # Nombró otro trámite del catálogo: se equivocó de opción y está
            # corrigiendo. Solo con UNO: "mejor el básico, no el semiestable"
            # nombra dos y ahí no se puede saber cuál quiere sin adivinar.
            nombrados = list(mencionados.values())
        if not nombrados and len(services) > 1:
            # Eligió por número sobre la lista numerada (B1, operador
            # 2026-10-10). Un número fuera de la lista no elige nada y cae a
            # la repregunta de abajo, con la lista completa.
            idx = _parse_int(normalized)
            picks = [idx] if idx is not None else _parse_indices(normalized)
            nombrados = _unique_services(
                services[i - 1] for i in picks if 1 <= i <= len(services)
            )
        pedidos = {s.id for s in services}
        if nombrados and {s.id for s in nombrados} == pedidos and len(services) > 1:
            # Eligió todos por número: es un sí a la lista, que se vuelve a
            # mostrar tal cual para el sí final.
            return [_confirm_multi_message(services)]
        if nombrados and {s.id for s in nombrados} != pedidos:
            _set_state(
                session, conv, "confirmar",
                _pendiente([s.id for s in nombrados], text, context.get("codigos")),
            )
            quedan = (
                _confirm_multi_message(nombrados) if len(nombrados) > 1
                else _confirm_message(nombrados[0], context.get("codigos"))
            )
            return [f"Va, lo dejamos así 👍\n\n{quedan}"]

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
        return ["¡Gracias! 🙌 Ya guardé tus datos. " + _cierre_atencion(TICKET_ALTA_CIERRE)]
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
        return ["¡Gracias! 🙌 Ya guardé tus datos. " + _cierre_atencion(TICKET_ALTA_CIERRE)]
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
    if conv.state == "aclarando_folio" and FOLIO_PREGUNTA_UNO.split("{")[0] in last:
        # Solo la pregunta lleva botones: «Lo sumo 👍» y los re-prompts de «Otro folio» son texto.
        # `in` y no `startswith`: la del archivo va precedida del acuse «Recibí tu archivo 📎.».
        out[-1] = {"text": last, "buttons": list(FOLIO_BOTONES)}
        return out
    if conv.state == "aclarando_caso" and last == CASO_PREGUNTA_BOTONES:
        out[-1] = {"text": last, "buttons": list(CASO_BOTONES)}
        return out
    if conv.state == "aclarando_caso" and MENU_MARKER in last:
        rows = _filas_caso(conv)
        if rows:
            out[-1] = {"text": last, "list": rows, "button": LIST_BUTTON_LABEL}
        return out
    if conv.state == "aclarando_folio" and MENU_MARKER in last:
        rows = _filas_folio(session, conv)
        if rows:
            out[-1] = {"text": last, "list": rows, "button": LIST_BUTTON_LABEL}
        return out
    if last.startswith(ESTATUS_PREGUNTA_INICIO):
        # «¿De cuál folio quieres saber?»: la lista lleva el folio como id («#74»). Va antes del
        # menú genérico: su texto también dice «Responde con el número».
        rows = _filas_estatus(session, phone)
        if rows:
            out[-1] = {"text": last, "list": rows, "button": LIST_BUTTON_LABEL}
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
            # Enmascarado: la bitácora se espeja a MSSQL y alimenta la minería;
            # ninguna de las dos necesita la CURP (auditoría 2026-09, IA-7).
            phone=phone, text=codigos.enmascarar(text)[:200], capa=capa,
            service_id=service_id, confianza=confianza,
            office_id=_oficina_de(session, phone),
        )
    )


def _log_fallo_ollama(session: Session, phone: str, text: str) -> None:
    """Deja constancia de que el servidor de Ollama no contestó.

    Fila EXTRA (models.CAPAS_EXTRA) con el motivo en service_id: el mensaje ya
    lo contó la capa que sí respondió. Antes de esto un Ollama caído solo
    dejaba un logger.warning y desde el panel era indistinguible de un bot que
    de pronto "dejó de entender". Se llama en los dos puntos donde una capa
    pudo haberlo usado (clasificador y capa informativa) y el motivo se
    consume al leerlo, así que un mismo fallo no genera dos filas."""
    motivo = nlu.limpiar_fallo_ollama()
    if motivo is not None:
        _log_nlu(session, phone, text, "ollama_error", motivo)


def _deduce_and_log(session: Session, phone: str, text: str):
    """Corre la cascada de deducción UNA sola vez por mensaje y deja
    constancia en nluevent (capa que entendió, o "ninguna"). Devuelve
    (multi, service, category, options) para que cada handler arme su UX."""
    # Arranca limpio. El ContextVar se copia por request, pero el cron procesa
    # muchos mensajes en el MISMO contexto: sin esto, un fallo de Ollama de
    # hace dos mensajes se registraba —y ahora también degradaría las capas de
    # prompt largo— como si fuera de este.
    nlu.reiniciar_turno()
    multi = nlu.deduce_all(text)
    if multi:
        _log_nlu(session, phone, text, "multi", ",".join(s.id for s in multi))
        return multi, None, None, []
    service, category, capa, confianza = nlu.deduce_explained(text)
    # Aquí y no antes: deduce_all es puramente literal y nunca toca Ollama.
    _log_fallo_ollama(session, phone, text)
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
    # Ollama ya falló en ESTE mensaje: esta capa habla con el mismo servidor y
    # con un piso de timeout de 45 s (prompt largo). Reintentar solo alarga la
    # espera del cliente —medido el 2026-09-10: 70 s para acabar escalando—.
    if nlu.backend_degradado() and not settings.anthropic_api_key:
        return None
    # El corpus curado que ve el modelo es el de la oficina del cliente (§6):
    # requisitos y precios varían por estado, y contestarle a alguien de
    # Tlaxcala con los requisitos de otra ciudad es peor que no contestarle.
    respuesta, motivo = rag.answer_explained(text, _oficina_de(session, phone))
    # La capa informativa tiene su propio timeout (prompt largo): puede caerse
    # aunque el clasificador haya pasado, así que se revisa otra vez.
    _log_fallo_ollama(session, phone, text)
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
    # Mismo cortocircuito que la capa informativa: el nodo agéntico habla con
    # el mismo Ollama, con prompt largo y hasta AGENT_MAX_TURNOS llamadas.
    if nlu.backend_degradado() and not settings.anthropic_api_key:
        return None
    respuesta = agent_tools.responder(session, phone, text)
    # También aquí: el nodo agéntico habla con el mismo servidor y su nodo del
    # Estudio ya muestra avisos. Sin esta línea, "el agéntico no pudo hablar
    # con Ollama" era el único fallo que seguía siendo invisible.
    _log_fallo_ollama(session, phone, text)
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
    if not picks and options:
        # El nombre vale igual que el número (operador, 2026-10-10, «C»): «acta de matrimonio» o
        # «la de matrimonio» sobre la lista de actas. Antes solo se entendían números y, con
        # trámites ya anotados, la elección se tiraba en silencio.
        picks = [options.index(s) + 1 for s in _elegidos_por_nombre(options, normalized)]
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
        # Piso duro: si el cliente no eligió, lo que YA se había entendido NO se tira. Pero lo que
        # pidió y no se entendió tampoco se tira EN SILENCIO: primero se vuelve a preguntar, y a la
        # segunda se confirma lo anotado diciéndole que el otro quedó fuera.
        anotados = ", ".join(f"'{s.name}'" for s in resueltos)
        if options and not context.get("reintento"):
            _set_state(session, conv, "eligiendo_sugerencia", {**context, "reintento": True})
            lines = "\n".join(f"{i}. {s.name}" for i, s in enumerate(options, 1))
            return [
                f"No identifiqué cuál de estos necesitas 🙏 Ya tengo anotado {anotados}.\n\n"
                f"Responde con el número o el nombre:\n{lines}"
            ]
        aviso = (
            "No identifiqué cuál de las opciones querías, así que por ahora la dejo fuera; "
            "si la necesitas, escríbemela después.\n\n"
            if options else ""
        )
        return _confirmar(session, conv, resueltos, text, aviso, detectados=detectados)
    # Cualquier otra cosa se procesa como texto libre normal.
    return _handle_menu_categoria(session, client, conv, text, normalized)


def _elegidos_por_nombre(options: list, normalized: str) -> list:
    """Las opciones del mini-menú que el cliente nombró con texto en vez de número.

    Primero el nombre o un sinónimo completos («acta de matrimonio»). Si no, la palabra que
    distingue a una opción de las demás («matrimonio», «la de matrimonio»): una palabra que
    comparten varias («acta») no elige nada, porque no dice cuál."""
    completos = [
        s for s in options
        if any(t and nlu._contains_term(nlu._norm(t), normalized) for t in [s.name, *s.synonyms])
    ]
    if completos:
        return completos
    palabras = {s.id: {w for w in re.findall(r"[a-z0-9]+", nlu._norm(s.name)) if len(w) >= 4} for s in options}
    del_texto = set(re.findall(r"[a-z0-9]+", normalized))
    elegidos = []
    for s in options:
        de_otras = set().union(*(palabras[o.id] for o in options if o.id != s.id))
        if (palabras[s.id] - de_otras) & del_texto:
            elegidos.append(s)
    return elegidos


Handler = Callable[[Session, Client, Conversation, str, str], list[str]]

# ponytail: la FSM vive en este dict y no en un submódulo propio. Una
# auditoría pidió extraerla (septiembre de 2026,
# docs/PLAN-CORRECCION-AUDITORIA.md) describiendo el archivo como un bloque
# indivisible; en la práctica ya está desacoplada aquí —un estado es una
# entrada y una función— y el archivo son 63 funciones sueltas, no una
# madeja. Reabrir cuando un estado necesite datos que no quepan en la firma
# `(session, client, conv, text, normalized)`, que es la señal real de que
# la máquina pide su propio módulo.
# Vive en el módulo, no dentro de _dispatch (PR1 de «casos para análisis»,
# 2026-10-09): `escalado` también es una entrada, pero handle_incoming la
# invoca sin el preludio de _dispatch, así que los dos leen este mismo dict.
_HANDLERS: dict[str, Handler] = {
    "inicio": _handle_inicio,
    "esperando_nombre": _handle_esperando_nombre,
    "menu_categoria": _handle_menu_categoria,
    "menu_servicio": _handle_menu_servicio,
    "confirmar": _handle_confirmar,
    "eligiendo_sugerencia": _handle_sugerencia,
    "pidiendo_requisitos": _handle_requisitos,
    "escalado": _handle_escalado,
    "aclarando_folio": _handle_aclarando_folio,
    "aclarando_caso": _handle_aclarando_caso,
}


def _handler_de(state: str) -> Handler:
    """El handler del estado; uno que la FSM no conoce (una fila vieja, un
    estado retirado) se atiende como `inicio`, nunca con silencio o un error."""
    return _HANDLERS.get(state, _handle_inicio)


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
    # Numerados para que el cliente pueda quedarse con algunos respondiendo el
    # número (B1, operador 2026-10-10): WhatsApp no tiene casillas múltiples.
    lines = "\n".join(f"{i}. {s.name} ({s.price_info})" for i, s in enumerate(services, 1))
    return (
        f"¿Confirmas estos {len(services)} trámites?\n{lines}\n"
        "Responde sí o no, o el número de los que sí quieres (por ejemplo: 1, o 1 y 2)."
    )


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

    ticket = None
    if open_ticket is not None:
        open_ticket.escalado = True
        session.add(open_ticket)
    else:
        asesor, motivo_asignacion = asignacion.elegir(session, client.phone)
        ticket = Ticket(
            phone=client.phone,
            client_name=client.name,
            category="(escalado)",
            service_id="escalado",
            service_name="Escalado a asesor",
            price_info="N/A",
            status=TicketStatus.NUEVO,
            office_id=client.office_id,
            assigned_to=asesor,
            escalado=True,
            notes=f"Cliente pidió hablar con un asesor. Último mensaje: {original_text[:MAX_NOTE_TEXT]}",
            provider=conv.provider,
        )
        session.add(ticket)

    _set_state(session, conv, "escalado")
    if ticket is not None:
        # _set_state ya commiteó, así que el folio tiene id.
        session.refresh(ticket)
        asignacion.bitacora(session, ticket, motivo_asignacion)
        session.commit()
    if reply:
        # Un `reply` propio trae información que se perdería al sustituirlo.
        return [_con_horario(reply)]
    return [_cierre_atencion("Un asesor te contactará en breve.")]


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


def _get_or_create_client(
    session: Session, phone: str, office_id: int = OFICINA_TLAXCALA_ID
) -> Client:
    client = session.get(Client, phone)
    if client is None:
        client = Client(phone=phone, last_activity_at=utcnow(), office_id=office_id)
        session.add(client)
        session.commit()
        session.refresh(client)
    else:
        # Se sella en cada entrante; el commit lo hace el flujo que ya corre.
        client.last_activity_at = utcnow()
        session.add(client)
    return client


def _get_or_create_conversation(
    session: Session, phone: str, provider: str = PROVIDER_META,
    office_id: int = OFICINA_TLAXCALA_ID,
) -> Conversation:
    conv = session.get(Conversation, phone)
    if conv is None:
        conv = Conversation(phone=phone, provider=provider, office_id=office_id)
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


def _oficina_de(session: Session, phone: str) -> int:
    """La oficina del cliente. `session.get` pega al identity map: si el flujo ya
    cargó la fila —siempre, en la cadena del entrante— no hay SELECT."""
    client = session.get(Client, phone)
    return client.office_id if client is not None else OFICINA_TLAXCALA_ID


def _log_message(
    session: Session,
    phone: str,
    direction: Direction,
    body: str,
    wa_message_id: str | None = None,
    provider: str | None = None,
    adjunto: dict | None = None,
) -> Message:
    if provider is None:
        # Se hereda de la conversación, que handle_incoming ya dejó apuntando al
        # canal de ESTE mensaje. Así los salientes quedan marcados con el canal
        # correcto sin pasar el proveedor por cada punto de registro.
        conv = session.get(Conversation, phone)
        provider = conv.provider if conv else PROVIDER_META
    mensaje = Message(
        phone=phone,
        direction=direction,
        body=body[:MAX_STORED_TEXT],
        wa_message_id=wa_message_id,
        provider=provider,
        # Sin office_id A PROPÓSITO: `message` no es una de las 15 tablas de
        # §2.3. El mensaje pertenece a la conversación, que sí la lleva, así
        # que la oficina es derivable y duplicarla abriría la puerta a que
        # las dos discrepen. OJO: SQLModel ACEPTA Y DESCARTA en silencio un
        # kwarg que no es columna, así que ponerlo aquí no daría error —
        # sería un no-op mudo. Lo amarra test_ruteo_oficina.py.
    )
    # Solo la referencia del archivo en el proveedor: el archivo no toca el VPS.
    adjuntos.sellar(mensaje, adjunto, provider)
    session.add(mensaje)
    session.commit()
    return mensaje
