"""Deducción de intención: ¿qué servicio del catálogo quiere el cliente?

Con ANTHROPIC_API_KEY definida usa Claude (salida estructurada, validada
contra el catálogo — nunca se confía en un id inventado). Sin API key, o si
la llamada falla/tarda, cae al matching por palabras clave de siempre: el
bot jamás se queda sin responder por culpa de un servicio externo.
"""

import json
import logging
import unicodedata

from rapidfuzz import fuzz

from app.catalog import Category, Service, catalog
from app.config import settings

logger = logging.getLogger(__name__)

_client = None

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
- Nunca inventes ids que no estén en el catálogo."""


def deduce(text: str) -> tuple[Service | None, Category | None]:
    """Devuelve (servicio, categoría) deducidos del texto; (None, None) si no se pudo."""
    cleaned = text.strip()
    if len(cleaned) < 3:
        return None, None

    if settings.anthropic_api_key:
        try:
            return _deduce_llm(cleaned[:MAX_INPUT_CHARS])
        except Exception as exc:
            logger.warning("Deducción con Claude falló, usando keywords: %s", exc)

    return _deduce_keywords(cleaned)


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


def _deduce_llm(text: str) -> tuple[Service | None, Category | None]:
    listing = "\n".join(
        f"- {s.id}: {s.name} — {c.name}" for c in catalog.categories for s in c.services
    )
    categories = "\n".join(f"- {c.id}: {c.name}" for c in catalog.categories)
    system = _SYSTEM_TEMPLATE.format(listing=listing, categories=categories)

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

    data = json.loads(next(b.text for b in response.content if b.type == "text"))
    # Validación contra el catálogo: un id alucinado se descarta en silencio.
    service = catalog.service_by_id(data["service_id"]) if data.get("service_id") else None
    category = catalog.category_by_id(data["category_id"]) if data.get("category_id") else None
    if service:
        return service, None
    return None, category


FUZZY_THRESHOLD = 85
FUZZY_MIN_CHARS = 4
# Tope de servicios por mensaje ("quiero acta, curp, rfc…"): más de esto huele
# a texto sin sentido y es mejor que lo vea un humano.
MULTI_MAX_SERVICES = 5


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
        if service.id not in seen and any(w and w in normalized for w in words):
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


def _deduce_keywords(text: str) -> tuple[Service | None, Category | None]:
    normalized = _norm(text)
    terms = _service_terms()

    # Capa 1: nombre o sinónimo mencionado dentro de la frase ("necesito un
    # acta de divorcio", "ocupo mi clave única") — el caso más común.
    matches = {s.id: s for s, words in terms if any(w and w in normalized for w in words)}
    if not matches:
        # Capa 2: frase corta contenida en el nombre ("divorcio") — matching clásico.
        matches = {s.id: s for s in catalog.search_by_keyword(text)}
    if not matches and len(normalized) >= FUZZY_MIN_CHARS:
        # Capa 3: difuso — tolera faltas de ortografía ("nesesito mi kurp").
        # Solo gana si hay UN servicio claramente arriba del umbral.
        scored = sorted(
            ((max(fuzz.partial_ratio(w, normalized) for w in words), s) for s, words in terms),
            key=lambda pair: pair[0],
            reverse=True,
        )
        if scored and scored[0][0] >= FUZZY_THRESHOLD:
            top_score = scored[0][0]
            leaders = [s for score, s in scored if score >= top_score - 2]
            if len(leaders) == 1:
                matches = {leaders[0].id: leaders[0]}

    if len(matches) == 1:
        return next(iter(matches.values())), None

    for c in catalog.categories:
        if _norm(c.name) in normalized:
            return None, c
    return None, None
