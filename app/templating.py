"""Objeto Jinja compartido — fuente única de configuración de plantillas.

Antes cada router creaba su propio Jinja2Templates y registraba is_admin (las
mismas dos líneas duplicadas en 13 módulos). Aquí vive UNO solo, con:
  - el global `is_admin` (lo usan las plantillas para las secciones de admin), y
  - el filtro `local`, que convierte los timestamps —guardados en UTC con
    utcnow()— a hora de Ciudad de México para mostrarlos en el panel.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

from app.auth import is_admin

# CDMX es UTC-6 todo el año (México eliminó el horario de verano en 2022). Es la
# misma zona que usan los cortes "hoy/semana/mes" de la gráfica de actividad.
BUSINESS_TZ = ZoneInfo("America/Mexico_City")


def local_dt(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Formatea un datetime en hora de CDMX. Los timestamps se guardan en UTC
    (utcnow(), sin tzinfo): se interpretan como UTC y se convierten a local.
    Devuelve '' si es None; idempotente si el valor ya trae zona."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BUSINESS_TZ).strftime(fmt)


templates = Jinja2Templates(directory="templates")
templates.env.globals["is_admin"] = is_admin
templates.env.filters["local"] = local_dt


if __name__ == "__main__":  # pragma: no cover
    # Check: 00:32 UTC del 25-jul debe verse como 18:32 del 24-jul en CDMX (-6 h).
    _utc = datetime(2026, 7, 25, 0, 32)
    assert local_dt(_utc) == "2026-07-24 18:32", local_dt(_utc)
    assert local_dt(None) == ""
    print("ok")
