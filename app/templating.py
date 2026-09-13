"""Objeto Jinja compartido — fuente única de configuración de plantillas.

Antes cada router creaba su propio Jinja2Templates y registraba is_admin (las
mismas dos líneas duplicadas en 13 módulos). Aquí vive UNO solo, con:
  - el global `is_admin` (lo usan las plantillas para las secciones de admin), y
  - el filtro `local`, que convierte los timestamps —guardados en UTC con
    utcnow()— a hora de Ciudad de México para mostrarlos en el panel.
"""

import hashlib
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

from app.auth import is_admin, is_admin_global
from app.cobranza import antiguedad
from app.dinero import formato_monto
from app.models import ZONA_HORARIA_DEFAULT

# CDMX es UTC-6 todo el año (México eliminó el horario de verano en 2022). Es la
# misma zona que usan los cortes "hoy/semana/mes" de la gráfica de actividad.
BUSINESS_TZ = ZoneInfo(ZONA_HORARIA_DEFAULT)


def local_dt(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Formatea un datetime en hora de CDMX. Los timestamps se guardan en UTC
    (utcnow(), sin tzinfo): se interpretan como UTC y se convierten a local.
    Devuelve '' si es None; idempotente si el valor ya trae zona."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BUSINESS_TZ).strftime(fmt)


STATIC_DIR = Path("static")


@lru_cache(maxsize=256)
def _huella(ruta: str, mtime_ns: int, tamano: int) -> str:
    # mtime y tamaño entran a la llave solo para invalidar la caché: la huella
    # sale del CONTENIDO, así un deploy que no cambió el archivo no lo invalida.
    return hashlib.sha256(Path(ruta).read_bytes()).hexdigest()[:12]


def static_url(ruta: str) -> str:
    """URL de un estático con la huella de su contenido (`?v=…`).

    Sin huella, tras un deploy el navegador podía seguir usando el JS de antes
    con el HTML nuevo: con el JS fuera de las plantillas, eso es un botón sin
    listener hasta que alguien limpie la caché. Si el archivo no existe se
    devuelve la ruta sin huella y el 404 queda a la vista."""
    archivo = STATIC_DIR / ruta
    try:
        st = archivo.stat()
    except OSError:
        return f"/static/{ruta}"
    return f"/static/{ruta}?v={_huella(str(archivo), st.st_mtime_ns, st.st_size)}"


templates = Jinja2Templates(directory="templates")
templates.env.globals["static"] = static_url
templates.env.globals["is_admin"] = is_admin
templates.env.globals["is_admin_global"] = is_admin_global
templates.env.globals["formato_monto"] = formato_monto
# Global y NO variable de contexto: Jinja2Templates usa Undefined permisivo, así
# que una clave olvidada en una de las dos rutas que comparten la tabla de
# cobros dejaría la celda vacía SIN error — y la vista mutilada sería la del
# colaborador, que es justo quien menos lo reporta. Como global, las dos la
# tienen por construcción.
templates.env.globals["antiguedad"] = antiguedad
templates.env.filters["local"] = local_dt


if __name__ == "__main__":  # pragma: no cover
    # Check: 00:32 UTC del 25-jul debe verse como 18:32 del 24-jul en CDMX (-6 h).
    _utc = datetime(2026, 7, 25, 0, 32)
    assert local_dt(_utc) == "2026-07-24 18:32", local_dt(_utc)
    assert local_dt(None) == ""
    print("ok")
