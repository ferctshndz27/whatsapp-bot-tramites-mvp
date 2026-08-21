"""Configuración del agente editable en runtime — puente entre /agente y settings.

El .env sigue siendo el DEFAULT de cada clave; lo que el admin guarda aquí lo
sobreescribe en caliente (setattr sobre settings: hay UN worker de uvicorn,
así que el cambio aplica al siguiente request sin recrear el contenedor). El
cron nocturno corre en otro proceso y llama aplicar_overrides() al arrancar.

Fuente única de QUÉ es editable: CONFIG_EDITABLE. Los secretos (tokens, API
keys) y la infra (URLs) jamás entran aquí — esos siguen siendo del .env.
"""

import logging

from sqlmodel import Session, select

from app.config import settings
from app.models import AgentConfig, utcnow

logger = logging.getLogger(__name__)

_MODELO_MAX = 60


def _bool(raw: str) -> bool:
    if raw not in ("0", "1"):
        raise ValueError("usa 1 (encendido) o 0 (apagado)")
    return raw == "1"


def _umbral(raw: str) -> int:
    valor = int(raw)
    # Mismo clamp que settings.fuzzy_threshold: fuera de 70-95 el difuso
    # inventa o nunca dispara.
    if not 70 <= valor <= 95:
        raise ValueError("el umbral difuso va de 70 a 95")
    return valor


def _timeout(raw: str) -> float:
    valor = float(raw)
    if not 1 <= valor <= 60:
        raise ValueError("el timeout va de 1 a 60 segundos")
    return valor


def _modelo(raw: str) -> str:
    limpio = raw.strip()
    if not limpio or len(limpio) > _MODELO_MAX or any(c.isspace() for c in limpio):
        raise ValueError("nombre de modelo inválido")
    return limpio


# clave -> (parser/validador, etiqueta para el panel). El atributo en settings
# se llama IGUAL que la clave: una sola verdad para leer y escribir.
CONFIG_EDITABLE = {
    "rag_enabled": (_bool, "Capa informativa (RAG)"),
    "agent_tools_enabled": (_bool, "Nodo agéntico (tools)"),
    "fuzzy_threshold": (_umbral, "Umbral difuso"),
    "ollama_model": (_modelo, "Modelo de Ollama"),
    "ollama_timeout": (_timeout, "Timeout de Ollama (s)"),
}


def aplicar_overrides(session: Session) -> None:
    """Pisa settings con lo guardado en la base. Llamar al arrancar la app y
    al inicio del cron. Una clave corrupta se ignora con warning: un dato malo
    en la base no puede tumbar el arranque del bot."""
    for fila in session.exec(select(AgentConfig)).all():
        entrada = CONFIG_EDITABLE.get(fila.key)
        if entrada is None:
            continue  # clave vieja o desconocida: el .env manda
        try:
            setattr(settings, fila.key, entrada[0](fila.value))
        except (ValueError, TypeError) as exc:
            logger.warning("Config %s=%r ignorada: %s", fila.key, fila.value, exc)


def guardar(session: Session, key: str, raw: str, usuario: str):
    """Valida, persiste y aplica en caliente. Devuelve el valor normalizado.
    Lanza ValueError con mensaje legible si la clave o el valor no van."""
    entrada = CONFIG_EDITABLE.get(key)
    if entrada is None:
        raise ValueError(f"'{key}' no es configurable desde el panel")
    valor = entrada[0](raw)

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
    """{clave: {valor, etiqueta}} con lo VIGENTE en settings (env + overrides)."""
    return {
        key: {"valor": getattr(settings, key), "etiqueta": etiqueta}
        for key, (_, etiqueta) in CONFIG_EDITABLE.items()
    }
