"""Configuración del agente editable en runtime — puente entre /agente y settings.

El .env sigue siendo el DEFAULT de cada clave; lo que el admin guarda aquí lo
sobreescribe en caliente (setattr sobre settings: hay UN worker de uvicorn,
así que el cambio aplica al siguiente request sin recrear el contenedor). El
cron nocturno corre en otro proceso y llama aplicar_overrides() al arrancar.

Fuente única de QUÉ es editable: CONFIG_EDITABLE. Los secretos (tokens, API
keys) y la infra (URLs) jamás entran aquí — esos siguen siendo del .env.
"""

import logging
import re
from typing import NamedTuple

from sqlmodel import Session, select

from app.config import settings
from app.models import AgentConfig, utcnow

logger = logging.getLogger(__name__)

# Lo que Ollama acepta como nombre de modelo: familia, etiqueta y cuantización.
_MODELO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*")

class Perilla(NamedTuple):
    """Descriptor de una clave configurable: qué es, qué valores admite y cómo
    se pinta. El panel del Estudio se dibuja DESDE esto.

    Antes los rangos vivían dos veces —el validador aquí y el min/max del
    <input> en agente.html— y nada obligaba a que coincidieran: bajar un tope
    en Python dejaba el deslizador ofreciendo valores que el servidor
    rechazaba. Ahora agregar una perilla es una línea en este diccionario.

    En tipo "texto", `maximo` es la longitud máxima; en los numéricos, la cota
    superior del valor."""

    etiqueta: str
    tipo: str  # "bool" | "entero" | "decimal" | "texto"
    minimo: float | None = None
    maximo: float | None = None
    paso: float | None = None
    unidad: str = ""
    ayuda: str = ""


def _cota(n: float) -> float | int:
    """4.0 se lee "4" en un mensaje de error; 0.5 se queda como está."""
    return int(n) if float(n).is_integer() else n


def _convertir(perilla: Perilla, raw: str):
    """Valida y normaliza contra el descriptor. El mensaje de error se arma
    con las MISMAS cotas que publica el panel: no pueden contradecirse."""
    if perilla.tipo == "bool":
        if raw not in ("0", "1"):
            raise ValueError("usa 1 (encendido) o 0 (apagado)")
        return raw == "1"

    if perilla.tipo == "texto":
        limpio = raw.strip()
        # Allowlist, no lista negra: un nombre de modelo de Ollama es
        # "qwen3:4b", "llama3.1:8b-instruct-q4_0". Nada fuera de eso tiene por
        # qué llegar a la URL de la API ni a un atributo del panel.
        if not _MODELO_RE.fullmatch(limpio):
            raise ValueError(f"{perilla.etiqueta}: valor inválido")
        if perilla.maximo is not None and len(limpio) > perilla.maximo:
            raise ValueError(
                f"{perilla.etiqueta}: máximo {_cota(perilla.maximo)} caracteres"
            )
        return limpio

    valor = int(raw) if perilla.tipo == "entero" else float(raw)
    if perilla.minimo is not None and perilla.maximo is not None:
        if not perilla.minimo <= valor <= perilla.maximo:
            raise ValueError(
                f"{perilla.etiqueta}: va de {_cota(perilla.minimo)} a "
                f"{_cota(perilla.maximo)}{perilla.unidad}"
            )
    return valor


# clave -> descriptor. El atributo en settings se llama IGUAL que la clave:
# una sola verdad para leer, escribir y pintar.
CONFIG_EDITABLE = {
    "rag_enabled": Perilla("Capa informativa (RAG)", "bool"),
    "agent_tools_enabled": Perilla("Nodo agéntico (tools)", "bool"),
    "reparto_automatico": Perilla(
        "Reparto de folios",
        "bool",
        ayuda="Apagado, los folios nuevos nacen sin dueño y se toman a mano.",
    ),
    "fuzzy_threshold": Perilla(
        "Umbral difuso", "entero", 70, 95, 1,
        ayuda="Abajo de 70 el difuso inventa coincidencias; arriba de 95 casi "
              "nunca dispara.",
    ),
    "ollama_model": Perilla(
        "Modelo de Ollama", "texto", maximo=60,
        ayuda="El nombre tal como lo sirve Ollama, sin espacios (qwen3:4b).",
    ),
    "openrouter_model": Perilla(
        "Modelo en OpenRouter", "texto", maximo=80,
        ayuda="El id tal como lo lista openrouter.ai/models (autor/modelo, sin "
              "espacios). Solo aplica con OPENROUTER_API_KEY en el .env; elige "
              "uno sin razonamiento o el cliente paga esa latencia.",
    ),
    "ollama_timeout": Perilla(
        "Timeout de Ollama", "decimal", 1, 60, 0.5, unidad=" s",
        ayuda="Acota la generación; conectar a un servidor caído siempre corta "
              "a los 2 s.",
    ),
    "rag_min_coseno": Perilla(
        "Umbral semántico (RAG)", "decimal", 0.1, 0.9, 0.05,
        ayuda="Solo con embeddings configurados: documentos con coseno menor "
              "no viajan al modelo. Bajo deja pasar poco relacionado; alto "
              "enmudece el vectorial y responde el léxico solo.",
    ),
    # Perillas del nodo agéntico. Apagan herramientas PERMITIDAS; no existe
    # clave alguna que encienda una en deny —"cobrar" no tiene flag— porque
    # esa frontera es de código revisado, no de un clic en el panel.
    "tool_cotizar_enabled": Perilla("Herramienta: cotizar servicio", "bool"),
    "tool_folios_enabled": Perilla("Herramienta: consultar folios", "bool"),
    "agent_max_turnos": Perilla(
        "Turnos máximos del bucle", "entero", 1, 5, 1,
        ayuda="Menos de 1 apagaría el bucle sin apagar el nodo; más de 5 es "
              "latencia que el cliente de WhatsApp sí siente.",
    ),
    "agent_max_words": Perilla(
        "Palabras máximas de la respuesta", "entero", 30, 120, 5,
        ayuda="Abajo de 30 un resumen de folios legítimo se tumba solo; arriba "
              "de 120 deja de parecer un mensaje de WhatsApp.",
    ),
}


def aplicar_overrides(session: Session) -> None:
    """Pisa settings con lo guardado en la base. Llamar al arrancar la app y
    al inicio del cron. Una clave corrupta se ignora con warning: un dato malo
    en la base no puede tumbar el arranque del bot."""
    for fila in session.exec(select(AgentConfig)).all():
        perilla = CONFIG_EDITABLE.get(fila.key)
        if perilla is None:
            continue  # clave vieja o desconocida: el .env manda
        try:
            setattr(settings, fila.key, _convertir(perilla, fila.value))
        except (ValueError, TypeError) as exc:
            logger.warning("Config %s=%r ignorada: %s", fila.key, fila.value, exc)


def guardar(session: Session, key: str, raw: str, usuario: str):
    """Valida, persiste y aplica en caliente. Devuelve el valor normalizado.
    Lanza ValueError con mensaje legible si la clave o el valor no van."""
    perilla = CONFIG_EDITABLE.get(key)
    if perilla is None:
        raise ValueError(f"'{key}' no es configurable desde el panel")
    valor = _convertir(perilla, raw)

    fila = session.get(AgentConfig, key)
    if fila is None:
        fila = AgentConfig(key=key, value=str(int(valor) if isinstance(valor, bool) else valor))
    else:
        fila.value = str(int(valor) if isinstance(valor, bool) else valor)
    fila.updated_by = usuario
    fila.updated_at = utcnow()
    session.add(fila)
    session.commit()

    setattr(settings, key, valor)
    return valor


def estado_actual() -> dict:
    """Lo VIGENTE en settings (env + overrides) MÁS el descriptor de cada
    clave, para que el panel arme el control solo: {clave: {valor, etiqueta,
    tipo, minimo, maximo, paso, unidad, ayuda}}."""
    return {
        key: {"valor": getattr(settings, key), **perilla._asdict()}
        for key, perilla in CONFIG_EDITABLE.items()
    }
