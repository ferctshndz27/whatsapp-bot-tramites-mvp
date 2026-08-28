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

import json
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

# Contexto amplio para el nodo agéntico (catálogo + esquemas de tools): sin
# num_ctx explícito, Ollama trunca EN SILENCIO lo que rebase su default (4096).
# La capa RAG ya no lo necesita: TopKLexical acota el prompt por construcción,
# y no fijarlo evita que Ollama recargue el runner al alternar con el
# clasificador (cada num_ctx distinto es un runner distinto).
OLLAMA_NUM_CTX = 8192
# Procesar ese prompt en la CPU de labs tarda más que OLLAMA_TIMEOUT (calibrado
# para el clasificador, de prompt corto). Piso propio de las capas de prompt
# largo; las llamadas siguientes se amortizan con el caché de prompts de Ollama.
OLLAMA_TIMEOUT_MIN = 45.0


def _timeout_prompt_largo() -> float:
    """OLLAMA_TIMEOUT manda solo si es MÁS holgado que el piso."""
    return max(OLLAMA_TIMEOUT_MIN, settings.ollama_timeout)

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

# La regla positiva va PRIMERO y con un ejemplo, porque qwen3:4b (sin think,
# format estricto) es literal: con "si la respuesta no está en el corpus →
# NO_SE" a secas, "¿dónde están sus oficinas?" devolvía NO_SE determinista
# TENIENDO enfrente el párrafo "no contamos con oficinas... atendemos en
# línea" — pedía una dirección, no la veía, y se rendía (verificado con la
# respuesta cruda en producción, 2026-08-28). Una respuesta negativa del
# corpus ES una respuesta.
_SYSTEM_TEMPLATE = """Eres el agente informativo de un bot de WhatsApp de una \
consultoría de trámites en México. Respondes dudas de clientes usando ÚNICAMENTE \
la información del corpus de abajo.

Reglas estrictas:
- Usa el corpus aunque la respuesta sea negativa: si preguntan por algo que el \
corpus dice que no tenemos o no hacemos, explícalo con amabilidad (ejemplo: \
preguntan dónde están las oficinas y el corpus dice que no hay oficinas porque \
atendemos en línea → responde eso, no {no_se}).
- Responde exactamente {no_se} y nada más SOLO cuando el corpus no diga nada \
relacionado con la duda.
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
    # Párrafos curados por el admin en /agente (tabla infodoc). Cumplen la
    # pregunta de la frontera: los escribe el admin BAJO la misma regla que el
    # archivo (solo información pública) y el router los valida al guardar
    # (sin dinero, sin CURP, sin teléfonos). Import y sesión locales: rag no
    # arrastra la base al importarse.
    from sqlmodel import Session, select

    import app.db as db
    from app.models import InfoDoc

    try:
        with Session(db.engine) as sesion:
            filas = sesion.exec(
                select(InfoDoc)
                .where(InfoDoc.habilitado == True)  # noqa: E712
                .order_by(InfoDoc.position, InfoDoc.id)
            ).all()
        docs.extend(f.texto.strip() for f in filas if f.texto.strip())
    except Exception as exc:
        logger.warning("Corpus en base ilegible, sigue el resto: %s", exc)
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


class TopKLexical:
    """Top-k por traslape léxico, para el Ollama de labs (CPU): el corpus
    completo (~4-5k tokens) tarda más de un minuto de prefill y la capa moría
    por timeout aun con 45s. Prefijos normalizados a 5 letras: "entregas" /
    "entregamos" y "físico"/"físicamente" cuentan como el mismo término, sin
    stemmer ni dependencias. Documentos sin traslape no viajan: si nada
    empata, el modelo recibe corpus vacío y contesta NO_SE — mejor que
    truncar o esperar en vano.

    ponytail: el traslape léxico pierde sinónimos puros ("horario" vs
    "abrimos"); cuando duela, EMBEDDINGS_URL activa el vectorial sin tocar
    código.
    """

    MAX_CHARS = 4000  # ≈1k tokens: prefill de segundos en la CPU de labs

    def candidatos(self, pregunta: str) -> list[str]:
        """Documentos con traslape > 0, mejor primero. Sin tope de tamaño:
        el tope (top-k y caracteres) lo aplica quien arma el contexto."""
        terminos = _terminos(pregunta)
        puntuados = sorted(
            ((len(terminos & _terminos(d)), d) for d in _docs()),
            key=lambda par: par[0],
            reverse=True,
        )
        return [d for puntos, d in puntuados if puntos > 0]

    def contexto(self, pregunta: str) -> str:
        return _acotar(self.candidatos(pregunta))


def _acotar(docs: list[str]) -> str:
    """Top-k + tope de caracteres, compartido por léxico e híbrido: la CPU
    de labs paga cada token de prefill, gane quien gane el ranking."""
    elegidos: list[str] = []
    total = 0
    for doc in docs[: settings.rag_top_k]:
        if total + len(doc) > TopKLexical.MAX_CHARS:
            break
        elegidos.append(doc)
        total += len(doc)
    return "\n".join(elegidos)


def _terminos(texto: str) -> set[str]:
    """Prefijos (5 letras) de las palabras con sustancia, normalizados."""
    return {p[:5] for p in re.findall(r"[a-z]+", nlu._norm(texto)) if len(p) > 3}


# Prefijos de tarea de nomic-embed-text (los documenta Nomic): el modelo rinde
# mejor sabiendo si embebe una pregunta o un documento. La clave del caché es
# el documento SIN prefijo: cambiar el prefijo no invalida lo ya embebido.
_PREFIJO_PREGUNTA = "search_query: "
_PREFIJO_DOC = "search_document: "


class TopKVectorial:
    """Top-k por similitud coseno con embeddings remotos (EMBEDDINGS_URL),
    con umbral (settings.rag_min_coseno): un documento con coseno bajo NO
    viaja aunque sea el top-1. Sin umbral este retriever mandaba SIEMPRE
    top-k, relevante o no, y el modelo opinaba con basura "parecida".

    ponytail: caché de vectores en memoria por documento — se re-embebe al
    reiniciar el proceso o al editar el catálogo. Persistir en SQLite cuando
    el corpus pase de cientos de documentos.
    """

    def __init__(self) -> None:
        self._cache: dict[str, list[float]] = {}

    def puntuados(self, pregunta: str) -> list[tuple[float, str]]:
        """[(coseno, doc)] de todo el corpus, mejor primero. Embebe solo lo
        que el caché no tiene, en UNA llamada batch."""
        docs = _docs()
        faltantes = [d for d in docs if d not in self._cache]
        if faltantes:
            vectores = _embed([_PREFIJO_DOC + d for d in faltantes])
            for doc, vec in zip(faltantes, vectores):
                self._cache[doc] = vec
        [vec_pregunta] = _embed([_PREFIJO_PREGUNTA + pregunta])
        return sorted(
            ((_coseno(vec_pregunta, self._cache[d]), d) for d in docs),
            key=lambda par: par[0],
            reverse=True,
        )

    def relevantes(self, pregunta: str) -> list[str]:
        """Solo los documentos que superan el umbral semántico."""
        return [d for c, d in self.puntuados(pregunta) if c >= settings.rag_min_coseno]

    def contexto(self, pregunta: str) -> str:
        return "\n".join(self.relevantes(pregunta)[: settings.rag_top_k])


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


class Hibrido:
    """Léxico ∪ vectorial fusionados con Reciprocal Rank Fusion.

    El léxico aporta lo literal ("acta", "curp"); el vectorial, los sinónimos
    puros que el prefijo de 5 letras jamás ve ("¿atienden en Guadalajara?" no
    traslapa con "atendemos en línea a todo México"). RRF suma 1/(K + rango)
    por cada lista donde el documento aparece: salir en ambas pesa más que ser
    top-1 de una sola, sin pesos que calibrar. Unión vacía = corpus vacío =
    NO_SE, la misma semántica del léxico.

    Si el endpoint de embeddings falla (labs caído, timeout), el turno lo
    resuelve el léxico solo: la capa se degrada, no se muere.
    """

    RRF_K = 60  # el estándar del paper; con ~70 documentos no hay qué afinar

    def __init__(self, lexico: TopKLexical, vectorial: TopKVectorial) -> None:
        self._lexico = lexico
        self._vectorial = vectorial

    def fusion(self, pregunta: str) -> list[str]:
        try:
            vectoriales = self._vectorial.relevantes(pregunta)
        except Exception as exc:
            logger.warning(
                "Embeddings fallaron; este turno recupera solo el léxico: %s", exc
            )
            return self._lexico.candidatos(pregunta)
        puntajes: dict[str, float] = {}
        for lista in (self._lexico.candidatos(pregunta), vectoriales):
            for rango, doc in enumerate(lista):
                puntajes[doc] = puntajes.get(doc, 0.0) + 1.0 / (self.RRF_K + rango + 1)
        return sorted(puntajes, key=lambda d: puntajes[d], reverse=True)

    def contexto(self, pregunta: str) -> str:
        return _acotar(self.fusion(pregunta))


_retriever_vectorial = TopKVectorial()
# El híbrido comparte la instancia vectorial: UN caché de vectores, no dos.
_retriever_hibrido = Hibrido(TopKLexical(), _retriever_vectorial)


def _retriever() -> Retriever:
    """Híbrido cuando hay léxico Y embeddings (el caso de producción);
    vectorial solo con EMBEDDINGS_URL; léxico acotado para el Ollama de labs
    (su CPU no aguanta el corpus completo); corpus entero solo con Claude,
    donde el prompt largo es barato (caché de prompts) y no hay prefill."""
    if settings.embeddings_url and settings.ollama_url:
        return _retriever_hibrido
    if settings.embeddings_url:
        return _retriever_vectorial
    if settings.ollama_url:
        return TopKLexical()
    return CorpusCompleto()


def explicar_retrieval(text: str) -> dict:
    """Desglose de la recuperación para el probador del Estudio: qué
    documentos viajarían y por qué vía (léxico, vectorial o ambas), con su
    coseno. Solo diagnóstico — el camino caliente no pasa por aquí — y nunca
    revienta: el probador también debe poder explicar un labs caído."""
    pregunta = text.strip()[:MAX_INPUT_CHARS]
    r = _retriever()
    info: dict = {"retriever": type(r).__name__, "docs": [], "error": None}
    try:
        if isinstance(r, Hibrido):
            info["umbral"] = settings.rag_min_coseno
            en_lexico = r._lexico.candidatos(pregunta)
            try:
                cosenos = {d: c for c, d in r._vectorial.puntuados(pregunta)}
            except Exception as exc:  # labs caído: se explica el modo degradado
                info["error"] = f"embeddings: {exc}"
                info["docs"] = [
                    {"texto": d[:90], "vias": ["léxico"], "coseno": None}
                    for d in en_lexico[: settings.rag_top_k]
                ]
                return info
            for doc in r.fusion(pregunta)[: settings.rag_top_k]:
                vias = ["léxico"] if doc in en_lexico else []
                if cosenos.get(doc, 0.0) >= settings.rag_min_coseno:
                    vias.append("vectorial")
                info["docs"].append(
                    {"texto": doc[:90], "vias": vias,
                     "coseno": round(cosenos.get(doc, 0.0), 3)}
                )
        elif isinstance(r, TopKVectorial):
            info["umbral"] = settings.rag_min_coseno
            info["docs"] = [
                {"texto": d[:90], "vias": ["vectorial"], "coseno": round(c, 3)}
                for c, d in r.puntuados(pregunta)[: settings.rag_top_k]
                if c >= settings.rag_min_coseno
            ]
        elif isinstance(r, TopKLexical):
            info["docs"] = [
                {"texto": d[:90], "vias": ["léxico"], "coseno": None}
                for d in r.candidatos(pregunta)[: settings.rag_top_k]
            ]
        else:  # CorpusCompleto: no hay ranking que explicar
            info["docs"] = [
                {"texto": "(corpus completo: viajan todos los documentos)",
                 "vias": ["completo"], "coseno": None}
            ]
    except Exception as exc:
        info["error"] = str(exc)
    return info


# ---------------------------------------------------------------------------
# Respuesta
# ---------------------------------------------------------------------------


# Motivos de descarte que cuentan como disparo de guardrail: hubo una
# respuesta candidata y la validación la tumbó. Un NO_SE no está aquí — el
# modelo declinó, no fue atajado. conversation._try_informative los registra
# en nluevent (capa "rag_descartado") para que dejen de ser invisibles.
GUARDRAIL_MOTIVOS = frozenset({"larga", "pii", "dinero", "meta"})

# Cadena de razonamiento de qwen3 fugada al content ("Okay, the user is
# asking… The corpus says: …"): meta-parloteo en inglés que JAMÁS debe llegar
# a un cliente. Una respuesta legítima de esta capa es español dirigido al
# cliente y no contiene estas frases.
_META_RE = re.compile(r"\b(the user|the corpus|let me|the question|okay,)", re.IGNORECASE)
# Bloque de razonamiento explícito, por si el servidor lo devolviera pese a
# think=False (versiones/plantillas de Ollama varían): se poda, no se castiga.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

# Salida forzada a JSON (structured outputs de Ollama, como el clasificador):
# la gramática restringe el decoding y el parloteo fuera del campo no puede
# existir. Lo que el modelo meta DENTRO del campo lo cazan _META_RE y el resto.
_FORMATO_RESPUESTA = {
    "type": "object",
    "properties": {"respuesta": {"type": "string"}},
    "required": ["respuesta"],
}


def answer(text: str) -> str | None:
    """Respuesta informativa validada, o None (= que siga el flujo normal)."""
    respuesta, _ = answer_explained(text)
    return respuesta


def answer_explained(text: str) -> tuple[str | None, str | None]:
    """(respuesta, motivo_descarte). El motivo solo viene cuando un guardrail
    tumbó una respuesta candidata (GUARDRAIL_MOTIVOS); apagado, sin respuesta
    del modelo o error de red es (None, None).

    Backend: el Ollama de labs si OLLAMA_URL está configurada (el negocio no
    paga API); Claude solo como alternativa si hay ANTHROPIC_API_KEY. La
    validación de la respuesta es la misma con cualquier modelo.

    Nunca lanza: cualquier falla (red, modelo, validación) es un None y el
    cliente recibe el aviso de siempre con su escalada a humano."""
    if not settings.rag_enabled or not (settings.ollama_url or settings.anthropic_api_key):
        return None, None
    try:
        pregunta = text.strip()[:MAX_INPUT_CHARS]
        corpus = _retriever().contexto(pregunta)
        system = _SYSTEM_TEMPLATE.format(no_se=NO_SE, corpus=corpus)
        if settings.ollama_url:
            message = nlu.ollama_chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": pregunta},
                ],
                # 90 tokens ≈ 60-70 palabras: margen sobre el tope de 45 sin
                # pagar la generación completa en CPU (~10 tokens/s — cada
                # token de más es un segundo que el cliente espera); el
                # recorte a oraciones limpia el final si corta a media frase.
                num_predict=90,
                format=_FORMATO_RESPUESTA,
                timeout=_timeout_prompt_largo(),
            )
            crudo = (message.get("content") or "").strip()
            try:
                respuesta = (json.loads(crudo).get("respuesta") or "").strip()
            except (ValueError, AttributeError):
                # Modelo desobediente con el esquema: que decida la validación.
                respuesta = crudo
        else:
            response = nlu._get_client().messages.create(
                model=settings.nlu_model,
                max_tokens=150,
                # Mismo patrón que nlu._deduce_llm: prefijo estable cacheado
                # (con CorpusCompleto el system no cambia entre preguntas).
                system=[
                    {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
                ],
                messages=[{"role": "user", "content": pregunta}],
            )
            if response.stop_reason == "refusal":
                return None, None
            respuesta = next(
                (b.text for b in response.content if b.type == "text"), ""
            ).strip()
        segura, motivo = _evaluar_respuesta(respuesta)
        return segura, motivo if motivo in GUARDRAIL_MOTIVOS else None
    except Exception as exc:
        logger.warning("Capa informativa falló, sigue el flujo normal: %s", exc)
        return None, None


def _respuesta_segura(respuesta: str) -> str | None:
    """Valida la respuesta EN CÓDIGO. El prompt pide; aquí se exige."""
    segura, _ = _evaluar_respuesta(respuesta)
    return segura


def _recortar_a_oraciones(respuesta: str) -> str | None:
    """Primeras oraciones completas que quepan en MAX_RESPONSE_WORDS, en
    orden. También limpia el final trunco cuando num_predict corta a media
    frase (el fragmento sin puntuación de cierre queda fuera)."""
    oraciones = re.findall(r"[^.!?]+[.!?]+", respuesta)
    recorte: list[str] = []
    palabras = 0
    for oracion in oraciones:
        n = len(oracion.split())
        if palabras + n > MAX_RESPONSE_WORDS:
            break
        recorte.append(oracion.strip())
        palabras += n
    return " ".join(recorte) if recorte else None


def _evaluar_respuesta(respuesta: str) -> tuple[str | None, str | None]:
    """(respuesta validada, motivo del descarte). El motivo distingue un
    guardrail que disparó ("larga"/"pii"/"dinero") de un simple "no sé"."""
    respuesta = _THINK_RE.sub("", respuesta).strip()
    if not respuesta or NO_SE in respuesta:
        return None, "sin_respuesta"
    # Razonamiento fugado = respuesta entera no confiable: se tumba, no se
    # intenta rescatar recortando (las "oraciones" serían el propio monólogo).
    if _META_RE.search(respuesta):
        return None, "meta"
    if len(respuesta.split()) > MAX_RESPONSE_WORDS:
        # qwen3:4b ignora el "máximo 30 palabras" del prompt y parlotea. La
        # verbosidad no es motivo para tirar una respuesta correcta: se
        # recorta a oraciones completas y los chequeos de seguridad corren
        # sobre lo que SÍ viaja. Sin una oración completa que quepa, se
        # descarta como siempre.
        recorte = _recortar_a_oraciones(respuesta)
        if recorte is None:
            return None, "larga"
        respuesta = recorte
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
