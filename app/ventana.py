"""Ventana de servicio de WhatsApp: la única definición de «¿puedo escribirle?».

Meta abre una ventana de 24 h cada vez que el CLIENTE escribe. Dentro de ella el
negocio puede mandar mensajes libres; fuera, solo pasa una plantilla aprobada.
La ventana es **del número, no del folio**: dos folios del mismo cliente
comparten una sola ventana, y contestar por el otro canal no la reabre (cada
cuenta —Meta y Twilio— tiene su propio número, ver `app/messaging.py`).

La fuente de verdad es `Message`: el último entrante de ese teléfono. NO hay una
columna `last_inbound_at` a propósito — sería una copia desnormalizada de algo
que la tabla ya contesta, y una copia que se puede desincronizar es exactamente
el bug que costó el arreglo del espejo (61ea7c2).

Este módulo existe porque la misma pregunta se hacía en cinco lugares —bandeja,
envío manual, avisos de cierre y de soltado, y los dos envíos de cobranza— cada
uno con su propio SELECT copiado. Un cambio en el criterio tenía que acertarle a
los cinco.

OJO — el 1-oct-2026 cambia la economía, no la mecánica: los mensajes de servicio
(los libres dentro de esta ventana) y las plantillas de utilidad respondidas
dentro de ella **pasan a cobrarse por mensaje**. La ventana seguirá diciendo qué
se PUEDE mandar; a partir de esa fecha ya no dice que sea gratis. Quién mide el
costo es `app/medidor.py`, que no adivina: copia lo que Meta reporta.
"""

from datetime import datetime, timedelta, timezone

from sqlmodel import select

from app.models import Direction, Message

WINDOW_HOURS = 24


def as_utc(dt: datetime) -> datetime:
    """SQLite devuelve los datetime sin tzinfo; compararlos con un `now()`
    consciente de zona revienta. Se asume UTC, que es como se guardan."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def abierta(last_in_at: datetime | None) -> bool:
    """¿Sigue abierta la ventana, dado el último entrante? Sin entrante nunca
    hubo ventana: a un cliente dado de alta a mano no se le puede escribir
    libre."""
    if last_in_at is None:
        return False
    return datetime.now(timezone.utc) - as_utc(last_in_at) < timedelta(hours=WINDOW_HOURS)


def horas_restantes(last_in_at: datetime | None) -> float:
    """Horas que le quedan a la ventana. 0.0 si ya cerró o nunca existió."""
    if last_in_at is None:
        return 0.0
    transcurridas = (datetime.now(timezone.utc) - as_utc(last_in_at)).total_seconds() / 3600
    return max(0.0, WINDOW_HOURS - transcurridas)


def restante_texto(last_in_at: datetime | None) -> str:
    """«3 h 12 min» / «41 min» / «cerrada». Para que el asesor sepa cuánto le
    queda ANTES de escribir un mensaje largo que ya no va a salir."""
    horas = horas_restantes(last_in_at)
    if horas <= 0:
        return "cerrada"
    h, m = divmod(int(horas * 60), 60)
    if h and m:
        return f"{h} h {m} min"
    if h:
        return f"{h} h"
    return f"{m} min"


def ultimo_entrante(session, phone: str) -> datetime | None:
    """Momento del último mensaje ENTRANTE de ese teléfono.

    # ponytail: no discrimina por canal. Un cliente que escribiera por las DOS
    # cuentas tendría una sola ventana calculada aquí, cuando en realidad Meta
    # lleva una por número de negocio. Hoy no muerde porque siempre se contesta
    # por el canal del último entrante (`messaging.canal_de`), que es justo el
    # que abrió esta ventana. Si algún día se contesta por el otro canal, hay
    # que pasarle `provider` a esta consulta.
    """
    fila = session.exec(
        select(Message)
        .where(Message.phone == phone, Message.direction == Direction.IN)
        .order_by(Message.created_at.desc())  # type: ignore[union-attr]
    ).first()
    return fila.created_at if fila else None


def abierta_para(session, phone: str) -> bool:
    """Atajo para quien solo quiere saber si puede escribirle a `phone`."""
    return abierta(ultimo_entrante(session, phone))
