"""Horario de atención humana: la única definición de «¿hay alguien?».

El bot NO deja de trabajar fuera de horario. Sigue recibiendo, deduciendo el
trámite y abriendo el folio igual que siempre. Lo único que cambia es lo que se
le PROMETE al cliente: «un asesor te contactará en breve» a las 2 de la mañana
de un domingo es falso, y la queja no es que nadie conteste de madrugada —eso
cualquiera lo entiende— sino que nadie lo diga.

La política vive en `horario.json` como DATO, no aquí como constante, por lo
mismo que el rate card: cambia por temporada y el cambio debe quedar con autor y
fecha en git. La zona horaria se toma de `templating.BUSINESS_TZ`, que ya es la
definición única del negocio — no se declara una segunda.

Si el archivo no se puede leer, `abierto()` devuelve True: sin horario válido la
suposición segura es que sí hay alguien. Prometer atención que llega tarde es
mucho menos grave que decirle a un cliente que estamos cerrados cuando no lo
estamos.
"""

import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from app.templating import BUSINESS_TZ

logger = logging.getLogger(__name__)

_RUTA = Path(__file__).parent / "horario.json"

# Cuántos días hacia adelante se busca la próxima apertura antes de rendirse.
# Con un negocio cerrado toda la semana esto evita un bucle infinito; 14 cubre
# de sobra un puente largo.
_DIAS_MAX_BUSQUEDA = 14

DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")


def _cargar() -> dict:
    try:
        return json.loads(_RUTA.read_text())
    except Exception:
        logger.exception("Horario ilegible (%s); se asume siempre abierto", _RUTA)
        return {}


HORARIO = _cargar()


def _rangos(dia: date) -> list[tuple[time, time]] | None:
    """Rangos de atención de ese día. `None` = no hay política que aplicar."""
    if not HORARIO:
        return None
    excepciones = HORARIO.get("excepciones") or {}
    crudos = excepciones.get(dia.isoformat())
    if crudos is None:
        semana = HORARIO.get("semana") or {}
        crudos = semana.get(str(dia.weekday()))
    if crudos is None:
        return None
    salida = []
    invalidos = 0
    for par in crudos:
        try:
            abre, cierra = (time.fromisoformat(x) for x in par)
        except (TypeError, ValueError):
            logger.warning("Rango de horario inválido para %s: %r", dia, par)
            invalidos += 1
            continue
        if abre >= cierra:
            # Un rango que cruza la medianoche (["22:00","02:00"]) o al revés
            # nunca se cumple en `abre <= hora < cierra`, así que dejaría el día
            # cerrado en silencio — pero `proxima_apertura` SÍ devolvería esa
            # hora de apertura, y el bot terminaría prometiendo una hora en la
            # que él mismo dice que no hay nadie. Se descarta con ruido.
            logger.warning(
                "Rango de horario imposible para %s (abre >= cierra): %r. "
                "Para cruzar la medianoche, pártelo en dos rangos.",
                dia,
                par,
            )
            invalidos += 1
            continue
        salida.append((abre, cierra))
    if not salida and invalidos:
        # Todos los rangos del día se descartaron por estar mal escritos. Eso NO
        # es «cerrado a propósito» —una lista vacía en el JSON sí lo es— y no
        # puede degradar al lado contrario que un archivo ilegible: un error de
        # dedo dejaría al bot diciéndole a los clientes que no abrimos en un día
        # laboral entero.
        return None
    return salida


def ahora() -> datetime:
    """La hora del NEGOCIO. Un solo lugar donde preguntarla."""
    return datetime.now(BUSINESS_TZ)


def _local(cuando: datetime | None) -> datetime:
    """A hora de negocio. Un naive se interpreta como UTC.

    Esa convención NO es arbitraria: es la del resto del repo
    (`templating.local_dt`), y es la que devuelve SQLite. Interpretarlo aquí
    como hora local mientras todos los demás lo leen como UTC son seis horas de
    diferencia que invierten el veredicto en ambos sentidos — y la fase 3b va a
    querer preguntar «¿este folio entró en horario?» sobre `Ticket.created_at`,
    que es exactamente un naive UTC.
    """
    cuando = cuando or ahora()
    if cuando.tzinfo is None:
        cuando = cuando.replace(tzinfo=timezone.utc)
    return cuando.astimezone(BUSINESS_TZ)


def abierto(cuando: datetime | None = None) -> bool:
    """¿Hay atención humana en ese momento? Sin horario legible: True."""
    cuando = _local(cuando)
    rangos = _rangos(cuando.date())
    if rangos is None:
        return True
    hora = cuando.time()
    # [abre, cierra): a las 18:00 en punto ya se cerró.
    return any(abre <= hora < cierra for abre, cierra in rangos)


def proxima_apertura(cuando: datetime | None = None) -> datetime | None:
    """Cuándo vuelve a haber alguien. `None` si no se pudo determinar."""
    cuando = _local(cuando)

    for salto in range(_DIAS_MAX_BUSQUEDA):
        dia = (cuando + timedelta(days=salto)).date()
        rangos = _rangos(dia)
        if not rangos:
            continue
        for abre, _cierra in sorted(rangos):
            candidato = datetime.combine(dia, abre, tzinfo=BUSINESS_TZ)
            if candidato > cuando:
                return candidato
    return None


def _cuando_en_palabras(apertura: datetime, cuando: datetime) -> str:
    hora = apertura.strftime("%H:%M")
    dias = (apertura.date() - cuando.date()).days
    if dias == 0:
        return f"hoy a las {hora}"
    if dias == 1:
        return f"mañana a las {hora}"
    if dias < 7:
        return f"el {DIAS[apertura.weekday()]} a las {hora}"
    return f"el {apertura.strftime('%d/%m')} a las {hora}"


def aviso(cuando: datetime | None = None) -> str | None:
    """Qué decirle al cliente si escribe fuera de horario. `None` si hay alguien.

    Se redacta con la próxima apertura para que el cliente sepa a qué atenerse.
    Si no se pudo calcular, se dice lo único cierto —que ahorita no hay nadie—
    en vez de inventar una hora.
    """
    cuando = _local(cuando)
    if abierto(cuando):
        return None
    apertura = proxima_apertura(cuando)
    if apertura is None:
        return "Ahorita estamos fuera de horario: la solicitud ya quedó registrada y la atiende un asesor en cuanto abramos."
    return (
        "Ahorita estamos fuera de horario: la solicitud ya quedó registrada y la "
        f"atiende un asesor {_cuando_en_palabras(apertura, cuando)}."
    )
