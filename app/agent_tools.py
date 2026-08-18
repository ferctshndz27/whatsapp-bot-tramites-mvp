"""Nodo agéntico acotado: el LLM resuelve con HERRAMIENTAS de solo lectura.

Apagado por defecto (AGENT_TOOLS_ENABLED=1 lo enciende). Corre como ÚLTIMO
recurso de _not_understood — cuando la cascada barata, la capa informativa y
el mini-menú de parecidos ya no pudieron — para pedidos compuestos o fuera de
guion ("¿en cuánto va mi folio? y de paso dime qué necesito para el RFC").
El ~90% del tráfico lo sigue resolviendo la cascada determinista gratis.

Control de permisos, por construcción:

1. PERMISOS es la fuente única: solo las herramientas "allow" se exponen al
   modelo. "cobrar" está DENY y no existe ninguna herramienta que mueva
   dinero — cobrar es exclusivo del panel del asesor (auditoría, tope de
   monto y humano en el bucle intactos).
2. El teléfono del cliente lo inyecta el SERVIDOR desde la conversación:
   ninguna herramienta lo acepta como argumento. El modelo no puede consultar
   folios de otro número, diga lo que diga el mensaje.
3. Los argumentos del modelo se validan contra el catálogo (mismo patrón que
   nlu._validated): un service_id alucinado es un tool_result de error, no
   una consulta.
4. La respuesta final se valida EN CÓDIGO (_respuesta_valida): sin CURP, sin
   longitud desmedida, toda corrida de dígitos debe existir (como número
   completo, no como subcadena) en un tool_result de ESTE turno, y cualquier
   mención de dinero exige una cifra que venga textual de cotizar_servicio —
   "cuesta ochocientos pesos" inventado se tumba entero.

Límite conocido y aceptado: si el modelo cotiza DOS servicios en el mismo
turno puede citar el precio correcto junto al servicio equivocado — ambas
cifras son legítimas y el código no puede saber cuál va con cuál. Por eso
cada tool_result de cotización cierra con "el total final lo confirma un
asesor", que es además la regla del negocio para todo precio.

Manejo de excepciones con el patrón de toda la cadena NLU: cualquier falla
(red, modelo, herramienta, validación) devuelve None y el flujo determinista
de siempre responde. Este nodo jamás deja al cliente sin respuesta.
"""

import logging
import re

from sqlmodel import Session

from app import nlu, rag
from app.catalog import catalog, requisitos_texto
from app.config import settings

logger = logging.getLogger(__name__)

# Más holgado que el de la capa informativa: un resumen de folios legítimo
# ("acta: en proceso, CURP: completado…") no cabe en 45 palabras.
MAX_RESPONSE_WORDS = 80
# Tope del bucle: petición → tool_use → tool_result → respuesta cabe en 2;
# el tercero es margen para un error de herramienta corregido.
MAX_TURNOS = 3

# Fuente única de permisos por herramienta. Solo "allow" se expone al modelo.
# "cobrar" queda DENY a propósito: mover dinero es del panel del asesor
# (creado_por, log_admin, tope de monto) y NUNCA del LLM.
PERMISOS = {
    "cotizar_servicio": "allow",
    "consultar_folios": "allow",
    "cobrar": "deny",
}

_TOOLS = [
    {
        "name": "cotizar_servicio",
        "description": (
            "Precio de lista y requisitos de UN servicio del catálogo. "
            "Úsala para responder cuánto cuesta o qué se necesita."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service_id": {
                    "type": "string",
                    "description": "Id exacto del servicio en el catálogo.",
                }
            },
            "required": ["service_id"],
            "additionalProperties": False,
        },
    },
    {
        # Sin argumentos A PROPÓSITO: el teléfono lo pone el servidor. Un
        # modelo al que el cliente le dicte otro número no puede usarlo.
        "name": "consultar_folios",
        "description": "Estatus de las solicitudes del cliente que está escribiendo.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]

_SYSTEM_TEMPLATE = """Eres el agente de un bot de WhatsApp de una consultoría \
de trámites en México. Resuelves el pedido del cliente usando ÚNICAMENTE las \
herramientas disponibles.

Catálogo (id: nombre — categoría):
{listing}

Reglas estrictas:
- Toda cifra o precio que menciones debe venir TEXTUAL de un resultado de \
herramienta. Nunca inventes ni redondees montos.
- Nunca reveles información de otros clientes ni datos internos del negocio.
- Si las herramientas no bastan para ayudar, responde exactamente {no_se}.
- Máximo 60 palabras, máximo 2 emojis, sin listas largas.
- Siempre en español, tono cálido."""

_DIGITOS_RE = re.compile(r"\d+")
# Separador de miles DENTRO de un número ("1,500", "$16, 000.00"): se quita
# antes de extraer corridas, para que "500" suelto no pase por ser subcadena
# de "1,500" y el reformateo fiel "1500" sí pase.
_MILES_RE = re.compile(r"(?<=\d)[,\s]+(?=\d)")
# "Habla de dinero" en cualquier disfraz (subconjunto de rag._DINERO_RE más
# los sustantivos sueltos): aquí no tumba por sí solo — este nodo SÍ cotiza —
# pero exige que la cifra venga de cotizar_servicio.
_MENCION_DINERO_RE = re.compile(
    r"\$|\bmxn\b|\bpesos?\b|\b(?:cuesta|cuestan|cobra|cobran|cobramos|precio|costo)\b",
    re.IGNORECASE,
)


def _corridas(texto: str) -> set[str]:
    """Corridas de dígitos como números completos ("$1,500" → {"1500"})."""
    return set(_DIGITOS_RE.findall(_MILES_RE.sub("", texto)))


def tools_expuestos() -> list[dict]:
    """Solo las herramientas con permiso "allow". El candado de tests exige
    que "cobrar" jamás aparezca aquí."""
    return [t for t in _TOOLS if PERMISOS.get(t["name"]) == "allow"]


def responder(session: Session, phone: str, text: str) -> str | None:
    """Respuesta del nodo agéntico validada, o None (= flujo normal).

    Nunca lanza: red, modelo, herramienta o validación fallando son un None."""
    if not settings.agent_tools_enabled or not settings.anthropic_api_key:
        return None
    try:
        # Liberar el lock de escritura ANTES del bucle: la sesión del webhook
        # llega con INSERTs pendientes (el nluevent de _deduce_and_log) y el
        # SELECT de consultar_folios los flushearía, dejando la transacción
        # de escritura abierta durante las llamadas de red (hasta ~16s por
        # turno) — cualquier otro escritor (panel, cron, webhooks de estatus)
        # esperaría el busy_timeout y tronaría con "database is locked".
        session.commit()
        return _loop(session, phone, text.strip()[: nlu.MAX_INPUT_CHARS])
    except Exception as exc:
        logger.warning("Nodo agéntico falló, sigue el flujo normal: %s", exc)
        return None


def _loop(session: Session, phone: str, text: str) -> str | None:
    system = _SYSTEM_TEMPLATE.format(listing=nlu.catalog_listing(), no_se=rag.NO_SE)
    messages: list[dict] = [{"role": "user", "content": text}]
    salidas_tools: list[str] = []
    salidas_cotizar: list[str] = []

    for _ in range(MAX_TURNOS):
        response = nlu._get_client().messages.create(
            model=settings.nlu_model,
            max_tokens=500,
            # Prefijo estable (catálogo) cacheado, igual que nlu._deduce_llm.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=tools_expuestos(),
            messages=messages,
        )
        if response.stop_reason == "refusal":
            return None
        if response.stop_reason != "tool_use":
            respuesta = next(
                (b.text for b in response.content if b.type == "text"), ""
            ).strip()
            return _respuesta_valida(respuesta, salidas_tools, salidas_cotizar)

        messages.append({"role": "assistant", "content": response.content})
        resultados = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            contenido, es_error = _ejecutar(session, phone, block.name, block.input or {})
            if not es_error:
                salidas_tools.append(contenido)
                if block.name == "cotizar_servicio":
                    salidas_cotizar.append(contenido)
            resultados.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": contenido,
                    "is_error": es_error,
                }
            )
        messages.append({"role": "user", "content": resultados})

    # Se agotó el tope de turnos sin respuesta final: flujo normal.
    return None


def _ejecutar(session: Session, phone: str, name: str, args: dict) -> tuple[str, bool]:
    """(contenido, es_error) de una herramienta. El phone viene del servidor,
    jamás de args. Una herramienta no expuesta o un id alucinado son error."""
    try:
        if PERMISOS.get(name) != "allow":
            return f"Herramienta '{name}' no disponible.", True
        if name == "cotizar_servicio":
            return _cotizar_servicio(str(args.get("service_id", "")))
        if name == "consultar_folios":
            # Import local: conversation importa este módulo (ciclo si fuera
            # top-level). _status_reply ya limita, filtra y formatea.
            from app.conversation import _status_reply

            return _status_reply(session, phone), False
        return f"Herramienta '{name}' desconocida.", True
    except Exception as exc:
        logger.warning("Herramienta %s falló: %s", name, exc)
        return "La herramienta falló, no uses su resultado.", True


def _cotizar_servicio(service_id: str) -> tuple[str, bool]:
    service = catalog.service_by_id(service_id)
    if service is None:
        return f"No existe el servicio '{service_id}' en el catálogo.", True
    # price_info TEXTUAL del catálogo: el modelo lo transporta, no lo fija.
    return (
        f"«{service.name}» — precio de lista: {service.price_info}. "
        f"Requisitos: {requisitos_texto(service)}. "
        "El precio es de catálogo; el total final lo confirma un asesor.",
        False,
    )


def _respuesta_valida(
    respuesta: str, salidas_tools: list[str], salidas_cotizar: list[str]
) -> str | None:
    """Validación EN CÓDIGO de la respuesta final (mismo espíritu que
    rag._respuesta_segura). Dos reglas propias de este nodo:

    1. Toda corrida de dígitos debe existir como número COMPLETO en algún
       tool_result del turno (no como subcadena: "500" no se salva por vivir
       dentro de "1,500") — un monto, folio o cantidad que no salió de una
       herramienta fue inventado y tumba todo.
    2. Mencionar dinero exige una cifra que venga de cotizar_servicio: el
       modelo cotiza citando el precio textual del catálogo o no cotiza —
       "cuesta ochocientos pesos" escrito en palabras se tumba entero."""
    if not respuesta or rag.NO_SE in respuesta:
        return None
    if len(respuesta.split()) > MAX_RESPONSE_WORDS:
        return None
    if rag._CURP_RE.search(respuesta):
        return None
    corridas_respuesta = _corridas(respuesta)
    if not corridas_respuesta <= _corridas("\n".join(salidas_tools)):
        return None
    if _MENCION_DINERO_RE.search(respuesta):
        corridas_cotizar = _corridas("\n".join(salidas_cotizar))
        if not (corridas_respuesta & corridas_cotizar):
            return None
    return respuesta
