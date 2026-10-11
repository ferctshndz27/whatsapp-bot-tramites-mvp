"""Deducción de intención: ¿qué servicio del catálogo quiere el cliente?

Cadena de capas, de la más barata a la más cara: coincidencia textual exacta
(nombre o sinónimo del catálogo — instantánea y gratis) → Ollama local (con
OLLAMA_URL; modelo abierto, costo cero pero ~5s en CPU) → Claude (con
ANTHROPIC_API_KEY) → difuso y categoría. Los LLM solo ven lo que el matching
exacto no resolvió, y su respuesta se valida contra el catálogo: nunca se
confía en un id inventado. Si una capa falla o tarda, contesta la siguiente:
el bot jamás se queda sin responder por culpa de un servicio externo.
"""

import json
import logging
import re
import threading
import time
import unicodedata
from contextvars import ContextVar

from rapidfuzz import fuzz

from app.catalog import Category, Service, catalog
from app.config import settings

logger = logging.getLogger(__name__)

_client = None

# Última falla de transporte de la capa Ollama EN ESTE mensaje. ContextVar y no
# global: los endpoints síncronos de FastAPI corren en un pool de hilos y una
# global mezclaría la falla de un cliente con la de otro.
#
# Existe porque hasta ahora un Ollama caído solo dejaba un logger.warning: en
# el panel se veía como "el bot dejó de entender" y no había forma de
# distinguir un servidor muerto de un modelo que declinó. conversation lo
# registra en nluevent (capa "ollama_error") y el Estudio lo pinta como aviso.
_FALLO_OLLAMA: ContextVar[str | None] = ContextVar("fallo_ollama", default=None)


def fallo_ollama() -> str | None:
    """El motivo de la última falla de Ollama en este mensaje, o None."""
    return _FALLO_OLLAMA.get()


def limpiar_fallo_ollama() -> str | None:
    """Devuelve el motivo y lo consume: quien registra la fila lo apaga para
    que un mismo fallo no se cuente dos veces en el mismo mensaje."""
    motivo = _FALLO_OLLAMA.get()
    if motivo is not None:
        _FALLO_OLLAMA.set(None)
    return motivo


# Bandera aparte de _FALLO_OLLAMA porque aquella se CONSUME al escribir la fila
# de nluevent, y después ya nadie puede saber que el servidor sigue caído. Esta
# dura todo el mensaje y la consultan las capas de prompt largo antes de marcar
# el mismo número muerto: el 2026-09-10 un Ollama en timeout dejó a un cliente
# esperando 70 s —los 12 s del clasificador MÁS los 45 s de piso de la capa
# informativa— para terminar en "un asesor te contactará".
_BACKEND_CAIDO: ContextVar[bool] = ContextVar("backend_caido", default=False)


def backend_degradado() -> bool:
    """¿Ollama ya falló en este mensaje? Entonces no vale reintentar con un
    prompt más largo y un timeout más holgado: el cliente paga la espera."""
    return _BACKEND_CAIDO.get()


def reiniciar_turno() -> None:
    """Arranca limpio el estado por mensaje (motivo del fallo + degradación) y
    abre el presupuesto de IA del mensaje.

    El cron procesa muchos mensajes en el MISMO contexto: sin esto, un fallo
    de hace dos mensajes se registraría —y degradaría las capas— como si
    fuera de este."""
    _FALLO_OLLAMA.set(None)
    _BACKEND_CAIDO.set(False)
    iniciar_presupuesto()


# ---- Cupo global, presupuesto por mensaje y pausa (auditoría 2026-09, IA-4 / EXT-3) ----
# `_BACKEND_CAIDO` es por mensaje; no había nada ENTRE mensajes ni alrededor de
# Ollama: sin semáforo ni cola, con OLLAMA_TIMEOUT hasta 60 s y el webhook, el
# panel y /health compartiendo un threadpool de 40 hilos en un solo worker, N
# números con preguntas raras degradaban a todos los clientes y congelaban el
# panel. Una pregunta no entendida con RAG + agente sumaba plazos
# [12, 45, 45, 45, 45] = 192 s.
# Tres piezas, todas en el ÚNICO embudo (`ollama_chat`), así cubren al
# clasificador, los códigos, la capa informativa, el agente y el minero:
#   cupo        — a lo más OLLAMA_CONCURRENCIA generaciones a la vez; quien no
#                 alcanza cupo en un segundo cae a las capas baratas.
#   presupuesto — un deadline por mensaje: cada llamada recibe como timeout lo
#                 que quede, y sin resto ya no se llama. IA-8 queda cubierto de
#                 paso: turnos × timeout del Estudio no puede rebasar esto.
#   pausa       — tras K timeouts seguidos, Ollama se da por caído P segundos
#                 para TODOS los mensajes, en vez de que cada uno lo descubra.
# ponytail: constantes salvo el cupo, que es OLLAMA_CONCURRENCIA en .env desde
# que el modelo corre en el VPS (2 vCPU) y no en labs; se lee al importar.
OLLAMA_CONCURRENCIA = settings.ollama_concurrencia
OLLAMA_ESPERA_CUPO_SEGUNDOS = 1.0
PRESUPUESTO_IA_SEGUNDOS = 25.0
OLLAMA_TIMEOUTS_PARA_PAUSAR = 3
OLLAMA_PAUSA_SEGUNDOS = 60.0
_CUPO_OLLAMA = threading.BoundedSemaphore(OLLAMA_CONCURRENCIA)
_DEADLINE: ContextVar[float | None] = ContextVar("deadline_ia", default=None)
_candado_pausa = threading.Lock()
_timeouts_seguidos = 0
_pausado_hasta = 0.0


class OllamaSinCupo(Exception):
    """OLLAMA_CONCURRENCIA generaciones en curso y ninguna terminó en un segundo."""


class OllamaSinPresupuesto(Exception):
    """El mensaje ya gastó su presupuesto de IA: no se llama."""


class OllamaEnPausa(Exception):
    """K timeouts seguidos: Ollama se da por caído hasta que pase la pausa."""


def iniciar_presupuesto(segundos: float = PRESUPUESTO_IA_SEGUNDOS) -> None:
    _DEADLINE.set(time.monotonic() + segundos)


def presupuesto_restante() -> float | None:
    """Segundos que le quedan al mensaje, o None si nadie abrió presupuesto
    (el minero nocturno, por ejemplo: ahí manda su propio timeout)."""
    deadline = _DEADLINE.get()
    return None if deadline is None else deadline - time.monotonic()


def _en_pausa() -> bool:
    with _candado_pausa:
        return time.monotonic() < _pausado_hasta


def _registrar_resultado(exc: Exception | None) -> None:
    """Solo los timeouts cuentan para la pausa: un «sin cupo» o un «sin
    presupuesto» no dicen nada de la salud del servidor."""
    global _timeouts_seguidos, _pausado_hasta
    import httpx

    with _candado_pausa:
        if exc is None:
            _timeouts_seguidos = 0
        elif isinstance(exc, httpx.TimeoutException):
            _timeouts_seguidos += 1
            if _timeouts_seguidos >= OLLAMA_TIMEOUTS_PARA_PAUSAR:
                _pausado_hasta = time.monotonic() + OLLAMA_PAUSA_SEGUNDOS
                _timeouts_seguidos = 0
                logger.warning("Ollama en pausa %s s tras %s timeouts seguidos",
                               OLLAMA_PAUSA_SEGUNDOS, OLLAMA_TIMEOUTS_PARA_PAUSAR)


def _clasificar_fallo(exc: Exception) -> str:
    """Motivo corto y accionable para el panel: no es el texto del error, es
    qué tiene que ir a revisar el admin."""
    import httpx

    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        return "conexion"
    if isinstance(exc, OllamaSinCupo):
        return "sin_cupo"
    if isinstance(exc, OllamaSinPresupuesto):
        return "sin_presupuesto"
    if isinstance(exc, OllamaEnPausa):
        return "en_pausa"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http_{exc.response.status_code}"
    return type(exc).__name__.lower()[:60]

MAX_INPUT_CHARS = 500

_SCHEMA = {
    "type": "object",
    "properties": {
        "service_id": {"type": ["string", "null"]},
        "category_id": {"type": ["string", "null"]},
    },
    "required": ["service_id", "category_id"],
    "additionalProperties": False,
}

_SYSTEM_TEMPLATE = """Eres el clasificador de intención de un bot de WhatsApp de una \
consultoría de trámites en México. Recibes el mensaje de un cliente y debes deducir qué \
servicio del catálogo necesita.

Catálogo (id: nombre — categoría):
{listing}

Categorías (id: nombre):
{categories}

Reglas:
- Si el mensaje apunta claramente a UN servicio, responde su service_id (category_id null).
- Si solo se distingue el tipo/categoría, responde category_id (service_id null).
- Si el mensaje es ambiguo, un saludo, o no se relaciona con el catálogo, ambos null.
- Nunca inventes ids que no estén en el catálogo.

Un mensaje que solo nombra un TEMA ("algo de", "información de", "los servicios \
de") NO apunta a un servicio: es categoría. Elegir uno a ciegas le cotiza al \
cliente un trámite que no pidió. Ejemplos:
- "algo de infonavit" → category_id de INFONAVIT, service_id null.
- "quiero información de afore" → category_id de AFORE, service_id null.
- "me interesan los servicios del imss" → category_id del IMSS, service_id null."""


# Marcadores de mensaje VAGO: el cliente nombra un tema, no un trámite ("algo
# de infonavit", "quiero información de afore"). Medido contra el Ollama real
# (scripts/eval_nlu.py, 2026-08-19): qwen3:4b ignora la regla de categoría del
# prompt y elige un servicio concreto a ciegas — le cotiza al cliente un
# trámite que no pidió, que es justo la regla de oro del producto. El prompt
# pide; aquí se exige: en un texto vago se descarta el service_id del LLM y
# decide la cascada barata, que ya resuelve categoría por nombre y mini-menú
# por parecidos. Ojo: esto solo alcanza a lo que el matching literal NO
# resolvió (va antes), así que "quiero información de mi acta de nacimiento"
# sigue resolviendo por nombre exacto.
_VAGO_RE = re.compile(
    r"\balgo (?:de|sobre|para)\b"
    r"|\b(?:informacion|info|informes|datos)\s+(?:de|sobre|acerca)\b"
    r"|\bque (?:tienen|manejan|ofrecen|hacen)\b"
    # "que tramites hay?" es una pregunta por el catálogo, no un pedido: el
    # 2026-09-11 el modelo la resolvió como la categoría "Otros servicios" y le
    # contestó al cliente con buró de crédito y pensiones.
    r"|\b(?:que|cuales|cuantos)\s+(?:tramites?|servicios?)\b"
    r"|\b(?:los |sus )?servicios\s+(?:de|del|para)\b"
    r"|\bme interesan?\s+(?:los |sus )?servicios\b"
    r"|\bquiero saber\s+(?:de|sobre)\b"
)


def es_vago(text: str) -> bool:
    """¿El mensaje nombra un TEMA en vez de pedir un trámite concreto?"""
    return _VAGO_RE.search(_norm(text)) is not None


# Palabras que no dicen QUÉ trámite es. Se descuentan antes de medir si el
# cliente nombró un servicio a medias: "quiero alta medica" pide lo mismo que
# "alta medica".
_RELLENO_PEDIDO = frozenset({
    "quiero", "queria", "necesito", "necesitaba", "ocupo", "ocupa", "ocupaba",
    "me", "mi", "mis", "el", "la", "lo", "los", "las", "un", "una", "unos",
    "unas", "de", "del", "para", "por", "favor", "porfa", "porfavor", "con",
    "sacar", "saca", "tramitar", "hacer", "solicitar", "pedir", "urgente",
    "ayuda", "ayudame", "info", "sobre", "que", "como", "donde", "cuanto",
})


def familia_ambigua(text: str, service: Service) -> bool:
    """¿El cliente nombró un trámite A MEDIAS, y varios lo completan?

    Hermano de es_vago() y con el mismo destino —descartar el service_id del
    LLM— pero por otra causa. Aquí el mensaje no es un tema ("algo de
    pensiones"): es un nombre incompleto. "alta medica" cabe dentro de 'Alta
    médica básica' y de 'Alta médica semiestable', y no dice cuál. El
    2026-09-11 el modelo eligió 'básica' a ciegas y el cliente acabó
    confirmando dos trámites, uno de los cuales nunca pidió.

    OJO con la frontera: esto NO es "hay varios parecidos". Un empate por
    sinónimo compartido —"necesito el papel ese"— sí es trabajo del LLM, que
    lee el contexto y por eso corre antes que el difuso. La condición es más
    estrecha: cada palabra con contenido del mensaje es palabra del nombre del
    servicio, y al nombre le sobran palabras que el cliente no dijo. Eso es un
    nombre truncado, no una descripción ambigua.

    Con el service_id descartado decide la cascada barata, que para este mismo
    texto ya ofrece el mini-menú de las cinco altas — la respuesta honesta.

    La elección del modelo NO entra en la condición. Medido el 2026-09-23 con
    qwen3:4b en el VPS: «necesito una constancia» (diez constancias lo
    completan) y el modelo contestó 'Semanas cotizadas', que ni lleva la
    palabra; el veto de entonces solo miraba si la elección estaba dentro de la
    familia y una de fuera pasaba entera. La ambigüedad la define el mensaje."""
    pedidas = {
        w for w in _norm(text).split()
        if len(w) >= 3 and w not in _RELLENO_PEDIDO
    }
    if not pedidas:
        return False
    completan = [
        s for s, _terms in _service_terms()
        if pedidas < set(_norm(s.name).split())  # subconjunto ESTRICTO
    ]
    return len(completan) >= 2


def deduce(text: str) -> tuple[Service | None, Category | None]:
    """Devuelve (servicio, categoría) deducidos del texto; (None, None) si no se pudo."""
    service, category, _, _ = deduce_explained(text)
    return service, category


def deduce_explained(text: str) -> tuple[Service | None, Category | None, str, float | None]:
    """Como deduce(), más la capa que decidió ("ollama", "llm", "nombre",
    "sinonimo", "keyword", "fuzzy", "categoria", "ninguna") y la confianza
    (solo fuzzy, 0-1). Alimenta la bitácora nluevent del panel Agente IA."""
    cleaned = text.strip()
    if len(cleaned) < 3:
        return None, None, "ninguna", None

    # Atajo determinista: si el cliente nombró el trámite tal cual (nombre
    # oficial o sinónimo del catálogo), no hay nada que deducir. Va ANTES de
    # los LLM porque es exacto, gratis e instantáneo: una clasificación local
    # en CPU cuesta ~5s, y cobrárselos a "quiero mi acta de nacimiento" sería
    # pagar por adivinar lo que ya está dicho. Un empate NO se resuelve aquí
    # (es ambigüedad real) y sigue a los LLM, que sí leen el contexto.
    matches, capa = _exact_matches(cleaned)
    if len(matches) == 1:
        service = next(iter(matches.values()))
        return service, None, _capa_exacta(service, cleaned, capa), None

    vago = es_vago(cleaned)

    # Lo que sale hacia un modelo va sin CURP/RFC/NSS/correo/teléfono: el
    # trámite se deduce de "quiero mi acta", no de la clave de al lado
    # (auditoría 2026-09, IA-6). Se enmascara ANTES de cortar: una CURP partida
    # por el tope ya no tiene forma de CURP. Import local: codigos importa nlu.
    from app.codigos import enmascarar

    para_modelo = enmascarar(cleaned)[:MAX_INPUT_CHARS]

    if settings.ollama_url:
        try:
            service, category = _deduce_ollama(para_modelo)
            if service is not None and vago:
                # Tema, no trámite: la categoría del servicio que eligió es la
                # respuesta honesta; si no la hay, decide la cascada barata.
                service, category = None, _categoria_de(service)
            elif service is not None and familia_ambigua(cleaned, service):
                # Nombre incompleto: ni servicio ni categoría. La categoría
                # tampoco sirve aquí —mandaría al submenú de los 8 servicios
                # del IMSS cuando las candidatas son 5 altas— y la cascada
                # barata ya sabe ofrecer exactamente esas cinco.
                service = None
            if service is not None or category is not None:
                return service, category, "ollama", None
            # Un "no sé" no detiene la cadena (misma regla que la capa Claude):
            # las capas de abajo ven sinónimos aprendidos por el minero que el
            # prompt no lleva, y son gratis.
        except Exception as exc:
            logger.warning("Deducción con Ollama falló, sigue la cadena: %s", exc)

    if settings.anthropic_api_key:
        try:
            service, category = _deduce_llm(para_modelo)
            if service is not None and vago:
                # Tema, no trámite: la categoría del servicio que eligió es la
                # respuesta honesta; si no la hay, decide la cascada barata.
                service, category = None, _categoria_de(service)
            elif service is not None and familia_ambigua(cleaned, service):
                # Mismo veto que en la capa de arriba: un nombre incompleto no
                # lo desambigua el modelo, lo desambigua el cliente.
                service = None
            if service is not None or category is not None:
                return service, category, "llm", None
            # Ambos null ya NO es veredicto final: las capas literales/fuzzy
            # ven sinónimos aprendidos por el minero que el prompt no lleva,
            # y son gratis. Antes el mensaje moría aquí en "ninguna".
        except Exception as exc:
            logger.warning("Deducción con Claude falló, usando keywords: %s", exc)

    return _deduce_keywords_explained(cleaned)


# ---- Clasificación de códigos sueltos (Ollama) ----
# Solo se llama cuando los patrones de app/codigos.py NO alcanzaron: una CURP
# mal tecleada, una clave copiada de otro documento. Es el "por si tiene
# dudas". Si Ollama no está configurado, tarda o alucina, la función devuelve
# {} y el flujo sigue igual que sin ella: el folio SE CREA y el colaborador
# valida a mano. Nunca bloquea el alta de un trámite.
CODIGOS_MAX = 10
_TIPOS_CODIGO = ("CURP", "RFC", "NSS")
_CODIGOS_SCHEMA = {
    "type": "object",
    "properties": {
        "codigos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "codigo": {"type": "string"},
                    "tipo": {"type": ["string", "null"]},
                },
                "required": ["codigo", "tipo"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["codigos"],
    "additionalProperties": False,
}
_CODIGOS_SYSTEM = """Eres un validador de claves oficiales mexicanas. Recibes una lista \
de cadenas que un cliente escribió en WhatsApp y dices de qué tipo es cada una.

Tipos posibles:
- CURP: 18 caracteres, 4 letras + 6 dígitos de fecha + H o M + 5 letras + 2 de verificación.
- RFC: 12 (moral) o 13 (física) caracteres, 3-4 letras + 6 dígitos de fecha + 3 de homoclave.
- NSS: 11 dígitos del IMSS.

Reglas:
- Una clave con un dedazo o un carácter de más o de menos SIGUE siendo de su tipo: \
responde el tipo que el cliente claramente quiso escribir.
- Si no se parece a ninguno de los tres, responde tipo null. No adivines.
- Devuelve exactamente una entrada por cada cadena recibida, con el `codigo` idéntico \
al recibido."""


def clasificar_codigos(codigos: list[str]) -> dict[str, str]:
    """{código: "CURP"|"RFC"|"NSS"} para las cadenas que el modelo local sí
    reconoce. Devuelve {} si Ollama no está configurado o si falla: quien
    llama debe seguir adelante sin esto."""
    if not settings.ollama_url or not codigos:
        return {}
    try:
        return _clasificar_codigos_ollama(codigos[:CODIGOS_MAX])
    except Exception as exc:
        logger.warning("Clasificación de códigos con Ollama falló, se dejan sin tipo: %s", exc)
        return {}


def _clasificar_codigos_ollama(codigos: list[str]) -> dict[str, str]:
    message = ollama_chat(
        [
            {"role": "system", "content": _CODIGOS_SYSTEM},
            {"role": "user", "content": "\n".join(codigos)},
        ],
        format=_CODIGOS_SCHEMA,
        num_predict=256,
    )
    data = json.loads(message["content"])
    # Igual que _validated con los ids del catálogo: un código que el modelo se
    # inventó (o devolvió cambiado) no entra.
    pedidos = set(codigos)
    return {
        item["codigo"]: item["tipo"]
        for item in data.get("codigos", [])
        if isinstance(item, dict)
        and item.get("codigo") in pedidos
        and item.get("tipo") in _TIPOS_CODIGO
    }


def _get_client():
    global _client
    if _client is None:
        import anthropic

        # ponytail: timeout corto y 1 reintento — si Claude no responde rápido,
        # el fallback por keywords contesta; un webhook de WhatsApp no puede esperar.
        _client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key, timeout=8.0, max_retries=1
        )
    return _client


def catalog_listing() -> str:
    """Listado "- id: nombre — categoría" del catálogo. Fuente única: lo usan
    el prompt clasificador de aquí y el system del nodo agéntico."""
    return "\n".join(
        f"- {s.id}: {s.name} — {c.name}" for c in catalog.categories for s in c.services
    )


def _catalog_system_prompt() -> str:
    categories = "\n".join(f"- {c.id}: {c.name}" for c in catalog.categories)
    return _SYSTEM_TEMPLATE.format(listing=catalog_listing(), categories=categories)


def _categoria_de(service: Service) -> Category | None:
    """La categoría que contiene al servicio (None si el catálogo cambió)."""
    return next((c for c in catalog.categories if service in c.services), None)


def _validated(data: dict) -> tuple[Service | None, Category | None]:
    """Validación contra el catálogo: un id alucinado se descarta en silencio."""
    service = catalog.service_by_id(data["service_id"]) if data.get("service_id") else None
    category = catalog.category_by_id(data["category_id"]) if data.get("category_id") else None
    if service:
        return service, None
    return None, category


def _deduce_llm(text: str) -> tuple[Service | None, Category | None]:
    system = _catalog_system_prompt()

    response = _get_client().messages.create(
        model=settings.nlu_model,
        max_tokens=256,
        # Prefijo estable primero (catálogo) para aprovechar el caché de prompts;
        # el mensaje volátil del cliente va al final.
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        output_config={"format": {"type": "json_schema", "schema": _SCHEMA}, "effort": "low"},
        messages=[{"role": "user", "content": text}],
    )
    if response.stop_reason == "refusal":
        return None, None

    return _validated(json.loads(next(b.text for b in response.content if b.type == "text")))


def ollama_chat(
    messages: list[dict],
    *,
    format: dict | None = None,
    tools: list[dict] | None = None,
    num_predict: int = 128,
    num_ctx: int | None = None,
    timeout: float | None = None,
) -> dict:
    """El `message` de /api/chat de Ollama (labs, detrás de nginx). Fuente
    única de la llamada: la usan el clasificador, el validador de códigos, la
    capa informativa y el nodo agéntico. "think" apagado: nada de esto
    necesita cadena de razonamiento y el cliente de WhatsApp sí siente cada
    segundo de latencia."""
    import httpx

    headers = {}
    if settings.ollama_token:
        headers["Authorization"] = f"Bearer {settings.ollama_token}"
    payload: dict = {
        "model": settings.ollama_model,
        "messages": messages,
        "stream": False,
        "think": False,
        "options": {"temperature": 0, "num_predict": num_predict},
    }
    if num_ctx is not None:
        # Sin esto, un prompt más grande que el contexto default del servidor
        # (4096) se TRUNCA en silencio: el modelo contesta sin haber visto el
        # principio del prompt. Lo fijan las capas de prompt largo (RAG, tools).
        payload["options"]["num_ctx"] = num_ctx
    if format is not None:
        # Ollama fuerza la salida al esquema (structured outputs), igual
        # que output_config en la API de Claude.
        payload["format"] = format
    if tools is not None:
        payload["tools"] = tools
    # El try envuelve la llamada porque este es el ÚNICO punto por donde pasan
    # todas las capas que usan Ollama (clasificador, códigos, RAG, nodo
    # agéntico): registrar la falla aquí la hace visible para todas sin
    # repetir el except en cada una. Se re-lanza sin tocar: cada capa sigue
    # decidiendo por su cuenta cómo degradar.
    # Desalojo del prefijo (2026-09-23): con OLLAMA_NUM_PARALLEL=1 hay UNA ranura
    # de caché en el servidor. Cualquier llamada que no sea la del clasificador
    # —otro system (minero, códigos, RAG, agente) u otro num_ctx, que además
    # levanta otro runner— la ocupa, y el siguiente cliente paga los ~1 500
    # tokens del catálogo desde cero (13:19 UTC del 2026-09-23: 12 s, 500,
    # fuzzy contestó). Se decide aquí porque este es el único embudo.
    del_clasificador = _es_llamada_del_clasificador(messages, num_ctx)
    llego_al_servidor = False
    try:
        if _en_pausa():
            raise OllamaEnPausa("Ollama en pausa tras timeouts seguidos")
        # OLLAMA_TIMEOUT acota la fase de lectura (la generación, lo único
        # lento); conectar a un servidor caído corta a los 2s. Un float suelto
        # aplicaría el tope a CADA fase y un servidor lento podría sumarlas.
        # Las capas de prompt largo pasan su propio timeout: procesar miles de
        # tokens en la CPU de labs tarda más que el clasificador. Y ninguno
        # rebasa lo que le queda al mensaje.
        tope = timeout or settings.ollama_timeout
        restante = presupuesto_restante()
        if restante is not None:
            if restante < 1.0:
                raise OllamaSinPresupuesto("el mensaje agotó su presupuesto de IA")
            tope = min(tope, restante)
        if not _CUPO_OLLAMA.acquire(timeout=OLLAMA_ESPERA_CUPO_SEGUNDOS):
            raise OllamaSinCupo("sin cupo para otra generación")
        try:
            llego_al_servidor = True
            response = httpx.post(
                f"{settings.ollama_url}/api/chat",
                headers=headers,
                json=payload,
                timeout=httpx.Timeout(tope, connect=2.0),
            )
        finally:
            _CUPO_OLLAMA.release()
        response.raise_for_status()
        _registrar_resultado(None)
        if not del_clasificador:
            prefijo_desalojado("otro_prompt")
        return response.json()["message"]
    except Exception as exc:
        _FALLO_OLLAMA.set(_clasificar_fallo(exc))
        # Se marca aquí, en el embudo, por la misma razón que el motivo: las
        # cuatro capas que hablan con Ollama pasan por este punto, y basta que
        # UNA falle para que reintentar con las otras solo alargue la espera.
        _BACKEND_CAIDO.set(True)
        _registrar_resultado(exc)
        # Un ConnectError no llegó a nadie: la ranura sigue como estaba.
        if (llego_al_servidor and not _EN_PRECALENTADO.get()
                and not isinstance(exc, httpx.ConnectError)):
            if not del_clasificador:
                # Aunque fallara, el servidor pudo evaluar parte del otro prompt
                # en la ranura: se da por desalojado.
                prefijo_desalojado("otro_prompt")
            elif isinstance(exc, httpx.TimeoutException):
                # El clasificador cortó por timeout: prefijo frío por una causa
                # que no vimos. Sin esto cada cliente paga 12 s, aborta, y el
                # siguiente vuelve a empezar: nunca termina de calentarse solo.
                prefijo_desalojado("timeout")
        raise


def _deduce_ollama(text: str) -> tuple[Service | None, Category | None]:
    """Clasificación con el modelo abierto local. Mismo prompt, mismo esquema
    y misma validación que la capa Claude."""
    message = ollama_chat(
        [
            {"role": "system", "content": _catalog_system_prompt()},
            {"role": "user", "content": text},
        ],
        format=_SCHEMA,
    )
    return _validated(json.loads(message["content"]))


# ---- Precalentado del prefijo del catálogo (2026-09-23) ----
# El system prompt del clasificador lleva el catálogo entero y es idéntico en
# cada mensaje: Ollama reutiliza su caché de prefijo mientras nadie lo desaloje.
# Pero se evalúa en frío tras cada arranque del bot y tras cada cambio del
# catálogo: >60 s con 4b en las 2 vCPU del VPS (~10 tok/s de prompt eval), 22,8 s
# medidos con 1.7b. Con OLLAMA_TIMEOUT=12 eso significa que el primer cliente
# después de cada deploy se queda sin modelo. Aquí se paga antes de que llegue:
# una llamada con el mismo prefijo y un token de salida, en un hilo demonio, al
# arrancar y cuando `catalog` avisa que cambió. Va por `ollama_chat` como todo
# lo demás (cupo y pausa incluidos); lo que NO lleva es presupuesto por
# mensaje, porque no hay mensaje: su tope es el suyo.
# 1 491 tokens de prefijo a ~10 tok/s de prompt eval son ~150 s (medido el
# 2026-09-23: un recalentado parcial tardó 51 s; el minero, con 8k de
# contexto, 3 min). Nadie espera por esto, así que el tope es holgado.
PRECALENTADO_TIMEOUT_SEGUNDOS = 300.0
# Interruptor de módulo, no de .env: existe para que la suite no abra hilos ni
# sockets (conftest lo apaga), no para que un admin lo toque.
PRECALENTADO_ACTIVO = True
_precalentado_lock = threading.Lock()
# Lo que ya está caliente: (hash del system prompt, generación de la caché).
# La generación sube con cada desalojo; así un desalojo que llega mientras un
# precalentado está en vuelo no se pierde (el hilo guardaría como caliente un
# prefijo que el minero acaba de tirar, y la repetición se saltaría por hash).
_prefijo_calentado: tuple[str, int] | None = None
_generacion_cache = 0
_precalentando = False
_precalentar_de_nuevo = False
_hilo_precalentado: threading.Thread | None = None
# La llamada del propio precalentado no cuenta como desalojo: si corta por
# timeout se anota y no se reprograma en bucle contra un servidor lento.
_EN_PRECALENTADO: ContextVar[bool] = ContextVar("en_precalentado", default=False)


def _firma_prefijo() -> str:
    import hashlib

    return hashlib.sha256(_catalog_system_prompt().encode("utf-8")).hexdigest()


def _es_llamada_del_clasificador(messages: list[dict], num_ctx: int | None) -> bool:
    """La única llamada que deja la ranura como el clasificador la necesita:
    su mismo system, byte a byte, y sin num_ctx (otro tamaño es otro runner)."""
    return (
        num_ctx is None
        and bool(messages)
        and messages[0].get("role") == "system"
        and messages[0].get("content") == _catalog_system_prompt()
    )


def precalentar_prefijo() -> bool:
    """Síncrono. Manda el prefijo del clasificador con un mensaje mínimo para
    que el servidor lo evalúe y lo deje en caché. True si lo mandó y contestó;
    False si no hay Ollama, si ese mismo prefijo ya se calentó, o si falló
    (queda en el log: nunca lanza, nadie espera por esto)."""
    global _prefijo_calentado
    if not settings.ollama_url:
        return False
    # La firma se calcula FUERA del candado: leer el catálogo puede detectar
    # un cambio y avisar, y el aviso pide este mismo candado (interbloqueo
    # visto en la suite el 2026-09-23).
    firma = _firma_prefijo()
    with _precalentado_lock:
        clave = (firma, _generacion_cache)
        if clave == _prefijo_calentado:
            return False
    token = _EN_PRECALENTADO.set(True)
    try:
        # Sin presupuesto por mensaje: el hilo nace sin deadline, y si alguien
        # lo llamara dentro de un request, tampoco debe heredar el suyo.
        _DEADLINE.set(None)
        ollama_chat(
            [
                {"role": "system", "content": _catalog_system_prompt()},
                {"role": "user", "content": "hola"},
            ],
            num_predict=1,
            timeout=PRECALENTADO_TIMEOUT_SEGUNDOS,
        )
    except Exception as exc:
        logger.warning("Precalentado del prefijo del catálogo falló: %s", exc)
        return False
    finally:
        _EN_PRECALENTADO.reset(token)
    with _precalentado_lock:
        _prefijo_calentado = clave
    logger.info("Prefijo del catálogo precalentado (%s)", settings.ollama_model)
    return True


def prefijo_desalojado(motivo: str) -> bool:
    """La caché del servidor ya no tiene el prefijo (otro prompt pasó por la
    única ranura, o el clasificador cortó por timeout): sube la generación,
    para que el mismo catálogo cuente como frío, y recalienta en segundo plano."""
    global _generacion_cache
    with _precalentado_lock:
        _generacion_cache += 1
    return precalentar_en_segundo_plano(motivo)


def esperar_precalentado(timeout: float) -> bool:
    """Bloquea hasta que el hilo en vuelo termine (True) o venza el plazo
    (False). Para procesos que salen —el job nocturno—: un hilo demonio muere
    con el proceso y dejaría el prefijo frío hasta el primer cliente."""
    hilo = _hilo_precalentado
    if hilo is None or not hilo.is_alive():
        return True
    hilo.join(timeout)
    return not hilo.is_alive()


def _precalentar_hasta_quedar_al_dia() -> None:
    global _precalentando, _precalentar_de_nuevo
    while True:
        try:
            precalentar_prefijo()
        except Exception:  # pragma: no cover - precalentar_prefijo ya no lanza
            logger.exception("Precalentado del prefijo reventó")
        with _precalentado_lock:
            if not _precalentar_de_nuevo:
                _precalentando = False
                return
            _precalentar_de_nuevo = False


def precalentar_en_segundo_plano(motivo: str) -> bool:
    """Lanza el precalentado en un hilo demonio. A lo más uno en vuelo: si el
    catálogo vuelve a cambiar mientras corre, se anota y repite UNA vez al
    terminar (dos ediciones seguidas de la parrilla no son dos hilos peleando
    por las 2 vCPU). True si arrancó un hilo o anotó la repetición."""
    global _precalentando, _precalentar_de_nuevo, _hilo_precalentado
    if not PRECALENTADO_ACTIVO or not settings.ollama_url:
        return False
    with _precalentado_lock:
        if _precalentando:
            _precalentar_de_nuevo = True
            return True
        _precalentando = True
    logger.info("Precalentando el prefijo del catálogo (%s)", motivo)
    _hilo_precalentado = threading.Thread(
        target=_precalentar_hasta_quedar_al_dia,
        name=f"precalentado-{motivo}",
        daemon=True,
    )
    _hilo_precalentado.start()
    return True


# El catálogo avisa cuando su contenido cambió (parrilla, Estudio, sinónimo
# aprobado): el prefijo viejo ya no sirve. Lambda a propósito: resuelve el
# nombre en este módulo al llamar, así un monkeypatch sí lo alcanza.
catalog.al_cambiar(lambda: precalentar_en_segundo_plano("catalogo"))


# FUZZY_THRESHOLD ahora es configurable (settings.fuzzy_threshold, .env).
FUZZY_MIN_CHARS = 4
# Tope de servicios por mensaje ("quiero acta, curp, rfc…"): más de esto huele
# a texto sin sentido y es mejor que lo vea un humano.
MULTI_MAX_SERVICES = 5
SUGGESTIONS_MAX = 6


# Palabras funcionales que NO discriminan un trámite. El filtro hace falta
# porque los NOMBRES del catálogo las contienen ("Amparo PARA liberar pensión",
# "Alta PARA retiro por desempleo"): sin esto, "necesito mi acta PARA la
# escuela" —una frase de lo más normal— sugiere trámites de pensión y el agente
# pregunta por algo que el cliente nunca pidió. Solo entran preposiciones,
# conjunciones, artículos y verbos de intención: nada que pueda ser, por sí
# solo, el nombre de un servicio.
PALABRAS_VACIAS = {
    "para", "pero", "porque", "como", "sobre", "hasta", "entre", "desde",
    "cuando", "donde", "mientras", "aunque", "segun", "sino",
    "este", "esta", "esto", "esos", "esas", "unos", "unas", "ellos", "ellas",
    "mismo", "misma", "otro", "otra", "otros", "otras",
    "quiero", "quisiera", "necesito", "ocupo", "tengo", "puedo", "podria",
    "saber", "favor", "porfa", "gracias", "hola", "buenas", "tardes", "dias",
    "noches", "urgente", "ayuda", "ayudame",
}


def suggestions(text: str) -> list[Service]:
    """Servicios que comparten alguna palabra clave con el texto ("quiero un
    acta" → las cuatro actas). Es la red de seguridad cuando deduce() no
    encontró UN servicio claro: en vez de rendirse, el bot ofrece un mini-menú
    de los parecidos. Palabras de 4+ letras con frontera izquierda ("acta"
    matchea "acta"/"actas", no "contacta")."""
    normalized = _norm(text)
    if len(normalized) < FUZZY_MIN_CHARS:
        return []
    found: list[Service] = []
    for service, terms in _service_terms():
        words = {
            w for term in terms for w in term.split()
            if len(w) >= 4 and w not in PALABRAS_VACIAS
        }
        if any(re.search(rf"\b{re.escape(w)}", normalized) for w in words):
            found.append(service)
    return found[:SUGGESTIONS_MAX]


def texto_sin_menciones(text: str, services: list[Service]) -> str:
    """El texto normalizado, sin las menciones que YA resolvieron esos servicios.

    Sirve para preguntarle a lo que SOBRA del mensaje: en "quiero un acta y un
    rfc" el rfc se resuelve solo, y quitarlo deja "quiero un acta y un", que sí
    dispara suggestions() con las cuatro actas. Sin este recorte no se puede
    distinguir un pedido incompleto de uno completo: suggestions() matchea por
    palabra compartida, así que "quiero mi acta de nacimiento" —que no dejó
    nada pendiente— también devolvería las otras tres actas.
    """
    ids = {s.id for s in services}
    resto = _norm(text)
    for service, terms in _service_terms():
        if service.id not in ids:
            continue
        # De más largo a más corto: quitar "acta de nacimiento" antes que un
        # sinónimo "acta" evita dejar suelto un "de nacimiento" que después
        # vuelva a matchear a sus hermanas.
        for term in sorted((t for t in terms if t), key=len, reverse=True):
            resto = re.sub(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", " ", resto)
    return resto


def deduce_all(text: str) -> list[Service]:
    """Todos los servicios mencionados textualmente en la frase ("necesito
    acta de nacimiento y mi curp"). Solo usa la capa literal (nombre o
    sinónimo contenido en el texto): es barata, determinista y no llama a
    Claude. Devuelve [] salvo que haya 2+ menciones claras — el caso de un
    solo servicio lo sigue resolviendo deduce().

    La mención más larga gana por tramo (D1 reabierta, PR9): un servicio cuenta
    solo si alguna de sus menciones no cae dentro de una mención MÁS LARGA de
    otro servicio. Sin esto, «Acta de matrimonio» contaba también el acta de
    nacimiento (su sinónimo «acta») y el cliente recibía dos trámites que no
    pidió. Dos menciones del mismo tramo y el mismo largo («vigencia de
    derechos», de dos servicios) cuentan las dos: esa ambigüedad es real."""
    normalized = _norm(text)
    if len(normalized) < 3:
        return []
    found = servicios_nombrados(text)
    return found[:MULTI_MAX_SERVICES] if len(found) >= 2 else []


def servicios_nombrados(text: str) -> list[Service]:
    """Todos los servicios que el texto nombra con su nombre o un sinónimo, en el orden del catálogo,
    con la regla de la mención más larga (PR9) y SIN el filtro `≥ 2` ni el tope de `deduce_all`.

    Es el contador que distingue 0, 1 y n (diseño §5.2): `deduce_all` devuelve `[]` tanto para 0
    como para 1, así que una compuerta «0 o ≥ 2» montada sobre él nunca cierra. Solo capa literal:
    gratis, determinista, sin red."""
    normalized = _norm(text)
    if len(normalized) < 3:
        return []
    sueltos = {s.id for s, _, _ in _menciones_sueltas(normalized)}
    found: list[Service] = []
    seen: set[str] = set()
    for service, _ in _service_terms():  # el orden del catálogo, como antes
        if service.id in sueltos and service.id not in seen:
            seen.add(service.id)
            found.append(service)
    return found


def _menciones_sueltas(normalized: str) -> list[tuple[Service, int, int]]:
    """Cada mención de un nombre o sinónimo en la frase, con su tramo, salvo las que caen dentro de
    una mención MÁS LARGA de otro servicio (la regla de PR9). Dos menciones del mismo tramo y largo
    sobreviven las dos: ahí el texto no dice cuál."""
    menciones: list[tuple[Service, int, int]] = []
    for service, words in _service_terms():
        for w in {w for w in words if w}:
            for m in re.finditer(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", normalized):
                menciones.append((service, m.start(), m.end()))

    def _tapada(s: Service, ini: int, fin: int) -> bool:
        return any(
            o.id != s.id and o_ini <= ini and fin <= o_fin and (o_fin - o_ini) > (fin - ini)
            for o, o_ini, o_fin in menciones
        )

    return [(s, ini, fin) for s, ini, fin in menciones if not _tapada(s, ini, fin)]


def empate_por_tramo(text: str, services: list[Service]) -> list[Service]:
    """De los servicios que dio `deduce_all`, los que el cliente nombró con UNA misma mención que
    comparten («vigencia de derechos», nombre de uno y sinónimo de otro) y con ninguna otra propia.
    Esos no son dos pedidos: es uno que el texto no alcanza a decir cuál, y se le pregunta al
    cliente (operador, 2026-10-10, «A1»). [] si no hay empate.

    ponytail: atiende solo el primer empate de la frase; con dos empates distintos en un mensaje, el
    segundo se confirma como hoy. Si aparece en producción, preguntar uno tras otro."""
    if len(services) < 2:
        return []
    pedidos = {s.id for s in services}
    sueltas = [(s, ini, fin) for s, ini, fin in _menciones_sueltas(_norm(text)) if s.id in pedidos]
    por_tramo: dict[tuple[int, int], list[Service]] = {}
    for s, ini, fin in sueltas:
        grupo = por_tramo.setdefault((ini, fin), [])
        if all(o.id != s.id for o in grupo):
            grupo.append(s)
    compartidos = {tramo for tramo, grupo in por_tramo.items() if len(grupo) >= 2}
    claros = {s.id for s, ini, fin in sueltas if (ini, fin) not in compartidos}
    for tramo in sorted(compartidos):
        empate = [s for s in services if s.id in {o.id for o in por_tramo[tramo]} and s.id not in claros]
        if len(empate) >= 2:
            return empate
    return []


def _norm(text: str) -> str:
    """Minúsculas y sin acentos: 'CURP certificada' == 'curp sertificada'-friendly."""
    stripped = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return stripped.lower().strip()


def _service_terms() -> list[tuple[Service, list[str]]]:
    return [
        (s, [_norm(s.name), *(_norm(a) for a in s.synonyms if a.strip())])
        for c in catalog.categories
        for s in c.services
    ]


def _contains_term(term: str, normalized: str) -> bool:
    """Coincidencia por palabra completa, nunca por subcadena. El catálogo de
    producción trae el sinónimo "Ur", que como subcadena vive dentro de
    "urge", "seguro" y "buro": contaminaba la capa literal con dobles matches
    y el agente contestaba "no entendí" a frases perfectamente claras."""
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", normalized) is not None


def _deduce_keywords(text: str) -> tuple[Service | None, Category | None]:
    service, category, _, _ = _deduce_keywords_explained(text)
    return service, category


def _exact_matches(text: str) -> tuple[dict[str, Service], str]:
    """Candidatos por coincidencia textual exacta, y la capa que los produjo.

    Puede devolver varios: un empate es ambigüedad real y NO se resuelve aquí.
    Es la única parte de la cascada barata en la que se confía lo bastante
    como para saltarse los LLM, así que no incluye el difuso.

    Capa "literal": nombre o sinónimo mencionado como palabra completa dentro
    de la frase ("necesito un acta de divorcio") — el caso más común. Si varios
    servicios matchean, gana el del término más largo: la mención completa
    "correccion de rfc" pesa más que el sinónimo suelto "rfc" de otro servicio.
    Capa "keyword": la frase corta entera contenida en un nombre ("divorcio").
    """
    normalized = _norm(text)
    con_termino = {}
    for s, words in _service_terms():
        largo = max((len(w) for w in words if w and _contains_term(w, normalized)), default=0)
        if largo:
            con_termino[s.id] = (s, largo)
    if con_termino:
        tope = max(largo for _, largo in con_termino.values())
        return {sid: s for sid, (s, largo) in con_termino.items() if largo == tope}, "literal"
    return {s.id: s for s in catalog.search_by_keyword(text)}, "keyword"


def _capa_exacta(service: Service, text: str, capa: str) -> str:
    """Nombre de capa para la bitácora. Distingue si acertó el nombre oficial
    o un sinónimo aprendido: el panel mide si el catálogo de sinónimos rinde."""
    if capa != "literal":
        return capa
    return "nombre" if _contains_term(_norm(service.name), _norm(text)) else "sinonimo"


def _deduce_keywords_explained(
    text: str,
) -> tuple[Service | None, Category | None, str, float | None]:
    normalized = _norm(text)
    matches, capa = _exact_matches(text)
    confianza: float | None = None

    if not matches and len(normalized) >= FUZZY_MIN_CHARS:
        # Capa difusa — tolera faltas de ortografía ("nesesito mi kurp"). Va
        # hasta el final, después de los LLM: es la más propensa a falsos
        # positivos, y aquí un falso positivo le cotiza el trámite equivocado
        # a un cliente real. Solo gana si hay UN servicio arriba del umbral.
        scored = sorted(
            (
                # Términos de 1-2 letras fuera del difuso: partial_ratio("ur",
                # "seguro") da 100 y un sinónimo corto ganaría cualquier frase.
                (max((fuzz.partial_ratio(w, normalized) for w in words if len(w) >= 3), default=0), s)
                for s, words in _service_terms()
            ),
            key=lambda pair: pair[0],
            reverse=True,
        )
        if scored and scored[0][0] >= settings.fuzzy_threshold:
            top_score = scored[0][0]
            leaders = [s for score, s in scored if score >= top_score - 2]
            if len(leaders) == 1:
                matches = {leaders[0].id: leaders[0]}
                capa, confianza = "fuzzy", round(top_score / 100, 2)

    if len(matches) == 1:
        service = next(iter(matches.values()))
        return service, None, _capa_exacta(service, text, capa), confianza

    for c in catalog.categories:
        if _norm(c.name) in normalized:
            return None, c, "categoria", None
    return None, None, "ninguna", None
