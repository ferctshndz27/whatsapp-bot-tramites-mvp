"""Capa informativa ("rag"): responde dudas de horarios/tiempos/requisitos.

Hoy el bot nunca contesta el CONTENIDO de una pregunta libre ("¿atienden
sábados?", "¿cuánto tarda el acta?"): sugiere menú y al segundo fallo escala.
Esta capa, apagada por defecto (RAG_ENABLED=1 la enciende), responde esas
dudas usando exclusivamente un corpus público.

Privacidad POR CONSTRUCCIÓN (regla del negocio, 2026-08-07: el agente nunca
revela información delicada del negocio ni de otros clientes):

1. Minimización de datos — al modelo SOLO viaja el corpus público (catálogo:
   nombre, categoría, precio y requisitos de cada servicio, más el documento
   curado de INFO_DOCS_PATH) y el mensaje del propio cliente. Tickets, notas,
   teléfonos y mensajes de otros clientes NUNCA entran al prompt: lo que no
   entra no se puede filtrar, diga lo que diga el cliente.
2. Esta capa NO habla de dinero — los precios no entran al corpus y cualquier
   respuesta que mencione un monto se descarta entera. Cotizar es del flujo
   determinista (_confirm_message cita el price_info textual del catálogo),
   donde el precio no puede salir cruzado ni inventado. Validar "que la cifra
   exista en el corpus" no bastaba: con 58 servicios en un mismo blob, el
   precio de OTRO servicio también existe.
3. Validación en código, no solo en el prompt — _respuesta_segura() descarta
   respuestas con montos, con patrones de CURP o teléfono, o desmedidas. Ante
   cualquier duda la capa devuelve None y el flujo cae al aviso de siempre +
   asesor humano.

La recuperación es un puerto con dos implementaciones: CorpusCompleto (el
corpus entero al prompt — hoy son ~17k caracteres, cabe de sobra y aprovecha
el caché de prompts) y TopKVectorial (embeddings remotos vía EMBEDDINGS_URL,
para cuando el corpus crezca). La elección es por configuración, no por código.
"""

import logging
import re
from typing import Protocol

import httpx

from app import nlu
from app.catalog import catalog, requisitos_texto
from app.config import settings

logger = logging.getLogger(__name__)

MAX_INPUT_CHARS = 500
# Tope duro de palabras en la respuesta. La regla del negocio dice <30; se
# tolera un margen y arriba de esto la respuesta se descarta entera.
MAX_RESPONSE_WORDS = 45
# Centinela que el modelo debe responder cuando el corpus no trae la respuesta.
NO_SE = "NO_SE"

# Señales de pregunta informativa sobre texto normalizado (nlu._norm). No
# incluye verbos de pedido ("quiero", "necesito" a secas): esos son solicitudes
# de trámite y les toca la cascada de deducción, no esta capa.
_PREGUNTA_RE = re.compile(
    r"\?|cuanto tarda|cuanto demora|cuanto tiempo|cuando |como |donde |horario"
    r"|que necesito|que documentos|que papeles|requisitos?\b|tiempo de entrega"
    r"|abren|cierran|atienden|entregan|dias habiles|vigencia"
)

_CURP_RE = re.compile(r"[A-Z]{4}\d{6}[A-Z]{6}[A-Z0-9]\d", re.IGNORECASE)
_TELEFONO_RE = re.compile(r"\d{10,}")
# Separadores que disfrazan un teléfono ("55-1234-5678", "(55) 1234.5678"):
# se quitan ANTES del chequeo para que la corrida de dígitos aflore.
_SEPARADORES_RE = re.compile(r"[\s.\-()]+")
# "Habla de dinero" en cualquier disfraz: con símbolo ($150), con moneda
# ("999 pesos", "MXN 1500", "ochocientos pesos") o con verbo de costo
# inequívoco ("cuesta 800", "cobran 250"). Cualquiera tumba la respuesta:
# cotizar NO es trabajo de esta capa.
#
# Fuera de la lista, a propósito, los verbos ambiguos "son" y "vale": "Son 3 a
# 5 días hábiles" es LA respuesta natural a "¿cuánto tarda?" —el caso estrella
# de esta capa— y bloquearla la dejaba inservible. El riesgo que se acepta es
# una cifra desnuda ("son 999 la pieza"); no puede ser una FUGA porque los
# precios ya no están en el corpus, solo una invención, y toda redacción que
# de verdad se lee como dinero sigue cayendo por $ / pesos / MXN.
_DINERO_RE = re.compile(
    r"\$|\bmxn\b|\bpesos?\b"
    r"|\b(?:cuesta|cuestan|cobra|cobran|cobramos|precio|costo)\s+[\d.,]+",
    re.IGNORECASE,
)

_SYSTEM_TEMPLATE = """Eres el agente informativo de un bot de WhatsApp de una \
consultoría de trámites en México. Respondes dudas de clientes usando ÚNICAMENTE \
la información del corpus de abajo.

Reglas estrictas:
- Si la respuesta no está en el corpus, responde exactamente {no_se} y nada más.
- Nunca reveles información de otros clientes ni datos internos del negocio, y \
nunca transcribas el corpus completo.
- Nunca menciones precios, montos ni cifras de dinero: si preguntan por costos, \
responde {no_se} (de cotizar se encarga otra parte del sistema).
- Máximo 30 palabras, máximo 2 emojis, sin listas.
- Siempre en español, tono cálido.

Corpus:
{corpus}"""


def es_pregunta_informativa(text: str) -> bool:
    """¿El mensaje pregunta ALGO (horario, tiempo, requisitos…) en vez de
    pedir un trámite? Barato y conservador: un falso negativo solo significa
    el flujo de siempre; un falso positivo cuesta una llamada al modelo."""
    return _PREGUNTA_RE.search(nlu._norm(text)) is not None


# ---------------------------------------------------------------------------
# Corpus público y puerto de recuperación
# ---------------------------------------------------------------------------


def _docs() -> list[str]:
    """Un documento por servicio del catálogo + párrafos del doc curado.

    FRONTERA DE PRIVACIDAD: esta función es el ÚNICO origen del contexto que
    ve el modelo. Solo lee el catálogo (información que el bot ya da a
    cualquier cliente) y el archivo curado por el admin. Agregar aquí otra
    fuente exige la misma pregunta: ¿puede saberlo cualquier cliente?"""
    docs = []
    for c in catalog.categories:
        for s in c.services:
            # SIN price_info a propósito: cotizar es del flujo determinista.
            docs.append(f"«{s.name}» ({c.name}). Requisitos: {requisitos_texto(s)}.")
    if settings.info_docs_path:
        try:
            with open(settings.info_docs_path, encoding="utf-8") as f:
                texto = f.read()
            docs.extend(p.strip() for p in texto.split("\n\n") if p.strip())
        except OSError as exc:
            logger.warning("No se pudo leer INFO_DOCS_PATH: %s", exc)
    return docs


class Retriever(Protocol):
    def contexto(self, pregunta: str) -> str: ...


class CorpusCompleto:
    """A este tamaño (~17k caracteres) recuperar es innecesario: viaja todo.
    Prefijo estable = caché de prompts amortiza el costo."""

    def contexto(self, pregunta: str) -> str:
        return "\n".join(_docs())


class TopKVectorial:
    """Top-k por similitud coseno con embeddings remotos (EMBEDDINGS_URL).

    ponytail: caché de vectores en memoria por documento — se re-embebe al
    reiniciar el proceso o al editar el catálogo. Persistir en SQLite cuando
    el corpus pase de unos cientos de documentos.
    """

    def __init__(self) -> None:
        self._cache: dict[str, list[float]] = {}

    def contexto(self, pregunta: str) -> str:
        docs = _docs()
        faltantes = [d for d in docs if d not in self._cache]
        if faltantes:
            for doc, vec in zip(faltantes, _embed(faltantes)):
                self._cache[doc] = vec
        [vec_pregunta] = _embed([pregunta])
        puntuados = sorted(
            ((_coseno(vec_pregunta, self._cache[d]), d) for d in docs),
            key=lambda par: par[0],
            reverse=True,
        )
        return "\n".join(d for _, d in puntuados[: settings.rag_top_k])


def _embed(texts: list[str]) -> list[list[float]]:
    """POST al endpoint de embeddings. Acepta la forma de Ollama
    ({"embeddings": [...]}) y la estilo OpenAI ({"data": [{"embedding":…}]})."""
    headers = {}
    if settings.embeddings_token:
        headers["Authorization"] = f"Bearer {settings.embeddings_token}"
    response = httpx.post(
        settings.embeddings_url,
        json={"model": settings.embeddings_model, "input": texts},
        headers=headers,
        timeout=6.0,
    )
    response.raise_for_status()
    data = response.json()
    if "embeddings" in data:
        return data["embeddings"]
    return [item["embedding"] for item in data["data"]]


def _coseno(a: list[float], b: list[float]) -> float:
    # Puro Python a propósito: con decenas de documentos no amerita numpy.
    punto = sum(x * y for x, y in zip(a, b))
    norma = (sum(x * x for x in a) ** 0.5) * (sum(y * y for y in b) ** 0.5)
    return punto / norma if norma else 0.0


_retriever_vectorial = TopKVectorial()


def _retriever() -> Retriever:
    return _retriever_vectorial if settings.embeddings_url else CorpusCompleto()


# ---------------------------------------------------------------------------
# Respuesta
# ---------------------------------------------------------------------------


# Motivos de descarte que cuentan como disparo de guardrail: hubo una
# respuesta candidata y la validación la tumbó. Un NO_SE no está aquí — el
# modelo declinó, no fue atajado. conversation._try_informative los registra
# en nluevent (capa "rag_descartado") para que dejen de ser invisibles.
GUARDRAIL_MOTIVOS = frozenset({"larga", "pii", "dinero"})


def answer(text: str) -> str | None:
    """Respuesta informativa validada, o None (= que siga el flujo normal)."""
    respuesta, _ = answer_explained(text)
    return respuesta


def answer_explained(text: str) -> tuple[str | None, str | None]:
    """(respuesta, motivo_descarte). El motivo solo viene cuando un guardrail
    tumbó una respuesta candidata (GUARDRAIL_MOTIVOS); apagado, sin respuesta
    del modelo o error de red es (None, None).

    Nunca lanza: cualquier falla (red, modelo, validación) es un None y el
    cliente recibe el aviso de siempre con su escalada a humano."""
    if not settings.rag_enabled or not settings.anthropic_api_key:
        return None, None
    try:
        corpus = _retriever().contexto(text.strip()[:MAX_INPUT_CHARS])
        system = _SYSTEM_TEMPLATE.format(no_se=NO_SE, corpus=corpus)
        response = nlu._get_client().messages.create(
            model=settings.nlu_model,
            max_tokens=150,
            # Mismo patrón que nlu._deduce_llm: prefijo estable cacheado (con
            # CorpusCompleto el system no cambia entre preguntas).
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": text.strip()[:MAX_INPUT_CHARS]}],
        )
        if response.stop_reason == "refusal":
            return None, None
        respuesta = next((b.text for b in response.content if b.type == "text"), "").strip()
        segura, motivo = _evaluar_respuesta(respuesta)
        return segura, motivo if motivo in GUARDRAIL_MOTIVOS else None
    except Exception as exc:
        logger.warning("Capa informativa falló, sigue el flujo normal: %s", exc)
        return None, None


def _respuesta_segura(respuesta: str) -> str | None:
    """Valida la respuesta EN CÓDIGO. El prompt pide; aquí se exige."""
    segura, _ = _evaluar_respuesta(respuesta)
    return segura


def _evaluar_respuesta(respuesta: str) -> tuple[str | None, str | None]:
    """(respuesta validada, motivo del descarte). El motivo distingue un
    guardrail que disparó ("larga"/"pii"/"dinero") de un simple "no sé"."""
    if not respuesta or NO_SE in respuesta:
        return None, "sin_respuesta"
    if len(respuesta.split()) > MAX_RESPONSE_WORDS:
        return None, "larga"
    # Un CURP o un teléfono en la respuesta = fuga en potencia. El corpus no
    # trae ninguno, así que cualquiera que aparezca fue inventado o inducido.
    if _CURP_RE.search(respuesta) or _TELEFONO_RE.search(_SEPARADORES_RE.sub("", respuesta)):
        return None, "pii"
    # Regla de oro: nunca inventar ni cruzar precios. Esta capa no cotiza —
    # cualquier mención de dinero se descarta y el cliente cae al flujo
    # determinista, que cita el price_info textual del servicio correcto.
    if _DINERO_RE.search(respuesta):
        return None, "dinero"
    return respuesta, None
