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


def _clasificar_fallo(exc: Exception) -> str:
    """Motivo corto y accionable para el panel: no es el texto del error, es
    qué tiene que ir a revisar el admin."""
    import httpx

    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        return "conexion"
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
    r"|\b(?:los |sus )?servicios\s+(?:de|del|para)\b"
    r"|\bme interesan?\s+(?:los |sus )?servicios\b"
    r"|\bquiero saber\s+(?:de|sobre)\b"
)


def es_vago(text: str) -> bool:
    """¿El mensaje nombra un TEMA en vez de pedir un trámite concreto?"""
    return _VAGO_RE.search(_norm(text)) is not None


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

    if settings.ollama_url:
        try:
            service, category = _deduce_ollama(cleaned[:MAX_INPUT_CHARS])
            if service is not None and vago:
                # Tema, no trámite: la categoría del servicio que eligió es la
                # respuesta honesta; si no la hay, decide la cascada barata.
                service, category = None, _categoria_de(service)
            if service is not None or category is not None:
                return service, category, "ollama", None
            # Un "no sé" no detiene la cadena (misma regla que la capa Claude):
            # las capas de abajo ven sinónimos aprendidos por el minero que el
            # prompt no lleva, y son gratis.
        except Exception as exc:
            logger.warning("Deducción con Ollama falló, sigue la cadena: %s", exc)

    if settings.anthropic_api_key:
        try:
            service, category = _deduce_llm(cleaned[:MAX_INPUT_CHARS])
            if service is not None and vago:
                # Tema, no trámite: la categoría del servicio que eligió es la
                # respuesta honesta; si no la hay, decide la cascada barata.
                service, category = None, _categoria_de(service)
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
    try:
        response = httpx.post(
            f"{settings.ollama_url}/api/chat",
            headers=headers,
            json=payload,
            # OLLAMA_TIMEOUT acota la fase de lectura (la generación, lo único
            # lento); conectar a un servidor caído corta a los 2s. Un float suelto
            # aplicaría el tope a CADA fase y un servidor lento podría sumarlas.
            # Las capas de prompt largo pasan su propio timeout: procesar miles de
            # tokens en la CPU de labs tarda más que el clasificador.
            timeout=httpx.Timeout(timeout or settings.ollama_timeout, connect=2.0),
        )
        response.raise_for_status()
        return response.json()["message"]
    except Exception as exc:
        _FALLO_OLLAMA.set(_clasificar_fallo(exc))
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
    solo servicio lo sigue resolviendo deduce()."""
    normalized = _norm(text)
    if len(normalized) < 3:
        return []
    found: list[Service] = []
    seen: set[str] = set()
    for service, words in _service_terms():
        if service.id not in seen and any(w and _contains_term(w, normalized) for w in words):
            seen.add(service.id)
            found.append(service)
    return found[:MULTI_MAX_SERVICES] if len(found) >= 2 else []


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
