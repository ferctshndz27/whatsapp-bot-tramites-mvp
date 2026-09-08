"""De qué oficina es un mensaje entrante — docs/SPEC-MULTI-OFICINA.md §4.

Autenticar y resolver son dos cosas distintas, y confundirlas es el error que
este diseño evita: la FIRMA dice si el mensaje es legítimo (un solo `app_secret`
de Meta, un solo `auth_token` de Twilio, ambos compartidos por todas las
oficinas), y la OFICINA sale del contenido, después de que la firma pasó.

FAIL-CLOSED: un identificador que no mapea a ninguna oficina devuelve None y el
mensaje se descarta con un registro. Caer a Tlaxcala por omisión metería los
mensajes de un número nuevo en su bandeja sin que nadie se entere.

POR QUÉ ESTO NO ES UNA CONSULTA POR VALOR: Fernet mete un IV aleatorio en cada
cifrado, así que dos cifrados del mismo texto no se parecen y un
`WHERE valor_cif = ?` no puede casar nunca. Se compara contra el valor
RESUELTO de cada oficina, que además es lo que hace el ruteo correcto el día
del despliegue: con `officecredential` vacía —el estado de producción hoy— la
oficina 1 hereda del `.env` y casa, así que el ruteo funciona ANTES de sembrar
una sola fila. La siembra del paso 5 de §7 es una optimización, no un requisito
previo; sin esta propiedad, desplegar el ruteo antes de correr la siembra
descartaría TODO el tráfico.
"""

from __future__ import annotations

import logging

from sqlmodel import select

import app.db as db
from app.credenciales import CredencialIlegible, credencial
from app.models import Office
from app.utils import normalize_phone

logger = logging.getLogger(__name__)


def resolver_oficina_meta(phone_number_id: str) -> int | None:
    """La oficina dueña de ese `phone_number_id` de Meta, o None (§4.1)."""
    if not phone_number_id:
        return None
    # La clave va LITERAL dentro de la llamada a credencial(), no como parámetro
    # de _buscar: tests/test_credenciales_inventario.py audita por análisis
    # estático qué credencial lee cada módulo, y una clave en variable lo deja
    # fuera del censo. El precio es esta lambda; la alternativa era un candado
    # ciego a los dos puntos de lectura más críticos del proyecto.
    return _buscar(
        lambda oficina, ses: credencial(
            "WHATSAPP_PHONE_NUMBER_ID", office_id=oficina, session=ses
        ),
        phone_number_id, comparar=str.__eq__, clave="WHATSAPP_PHONE_NUMBER_ID",
    )


def resolver_oficina_twilio(to: str) -> int | None:
    """La oficina dueña de ese `To` de Twilio, o None (§4.2).

    Compara NORMALIZADO: Twilio entrega `whatsapp:+5215500000000` en el form y
    el `.env` guarda ese mismo formato, pero una oficina dada de alta desde el
    panel puede traer el número tecleado de otra manera. Comparar en crudo
    convertiría un espacio de más en un descarte silencioso de todo el tráfico
    de esa oficina — el modo de fallo más caro de §4.
    """
    if not to:
        return None
    return _buscar(
        lambda oficina, ses: credencial(
            "TWILIO_WHATSAPP_FROM", office_id=oficina, session=ses
        ),
        to, comparar=_mismo_numero, clave="TWILIO_WHATSAPP_FROM",
    )


def _mismo_numero(a: str, b: str) -> bool:
    normalizado = normalize_phone(a)
    return bool(normalizado) and normalizado == normalize_phone(b)


def _buscar(leer, valor: str, *, comparar, clave: str) -> int | None:
    """`leer` resuelve la credencial de una oficina; `clave` es solo para el log.

    El bucle corre DENTRO del `with` y le presta su sesión a `leer`: esto está
    en el camino de CADA mensaje entrante, y sin la sesión compartida
    `credencial()` abría y cerraba la suya por oficina — 1+N conexiones por
    webhook. Hoy N=1 y el ahorro es una conexión; el número crece con cada
    oficina que se dé de alta desde el panel, y el costo de prestarla es cero.
    """
    with db.get_session() as session:
        oficinas = session.exec(
            select(Office.id).where(Office.habilitado == True).order_by(Office.id)  # noqa: E712
        ).all()

        for office_id in oficinas:
            try:
                propio = leer(office_id, session)
            except CredencialIlegible:
                # Se sigue con las demás en vez de abortar: una CREDENTIALS_KEY
                # que no descifra la fila de UNA oficina no puede dejar sin
                # servicio a las otras. La que no se pudo leer no rutea
                # —fail-closed— y /system ya la pinta como ilegible.
                logger.warning(
                    "No se pudo leer %s de la oficina %s al rutear; esa oficina no resuelve",
                    clave, office_id,
                )
                continue
            if propio and comparar(propio, valor):
                return office_id
    return None
