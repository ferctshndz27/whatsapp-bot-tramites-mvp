"""Medidor de mensajería: cuánto saliente hubo, cuánto de eso cobra Meta.

Dos principios, y de ellos sale todo lo demás:

1. **No se adivina lo que Meta ya dijo.** El veredicto de cobro de cada mensaje
   llega en el webhook de estado (`pricing.category` / `pricing.type`) y se
   copia tal cual a `Message`. Aquí solo se cuenta. Por eso el 1-oct-2026 —el
   día que los mensajes de servicio y las plantillas de utilidad dentro de la
   ventana dejan de ser gratis— este archivo no necesita un solo cambio: Meta
   empezará a mandar `type='regular'` donde hoy manda `free_customer_service`,
   y el conteo de cobrables sube solo.

2. **Un total que se lee completo sin estarlo es peor que no tenerlo.** Hay
   salientes que nunca reciben veredicto: los de Twilio (ese canal no reporta
   nada de esto) y los que perdieron el emparejamiento de wamid. Por eso todo
   resumen trae `sin_dato` al lado del total. Sin ese denominador el reporte no
   se puede desmentir, que es la misma falla que tenía `/system` cuando llamaba
   «al día» a un espejo inflado.

El dinero es **estimado**: los conteos salen de Meta, la tarifa sale de
`rate_card.json`, y ese archivo declara su propia confianza. Cuando la tarifa
falta —o la categoría es nueva— el resumen lo dice en vez de contar 0.
"""

import json
import logging
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from sqlmodel import func, select

from app.models import Direction, Message

logger = logging.getLogger(__name__)

_RUTA_RATE_CARD = Path(__file__).parent / "rate_card.json"

# Veredicto de Meta que significa «esto se cobra». El resto de valores
# (free_customer_service, free_entry_point) son gratis.
#
# El conteo mira SOLO `pricing_type` y nunca `pricing_billable`, a propósito: la
# documentación de Meta dice que `billable` se va a deprecar y recomienda
# decidir con `type` + `category`. La columna se guarda igual —y se espeja— para
# poder contrastar el día que las dos señales no coincidan, pero es forense: no
# entra en ningún total.
TIPO_COBRABLE = "regular"


def _cargar_rate_card() -> dict:
    try:
        return json.loads(_RUTA_RATE_CARD.read_text())
    except Exception:
        # Sin tarifas el medidor sigue sirviendo: los CONTEOS son el dato duro y
        # no dependen de esto. Solo se queda sin la columna de pesos.
        logger.exception("Rate card ilegible (%s); el medidor reporta sin costo", _RUTA_RATE_CARD)
        return {}


RATE_CARD = _cargar_rate_card()


def tarjeta_vigente(cuando: date | None = None) -> dict | None:
    """La tarjeta que aplicaba en esa fecha.

    Se elige por fecha y no «la última» porque Meta cobra con el rate card
    vigente el día de la ENTREGA: corregir una tarifa de hoy no debe reescribir
    lo que costó el trimestre pasado.
    """
    tarjetas = RATE_CARD.get("tarjetas") or []
    if not tarjetas:
        return None
    cuando = cuando or datetime.now().date()
    candidatas = [
        t
        for t in tarjetas
        if t.get("vigencia_desde") and date.fromisoformat(t["vigencia_desde"]) <= cuando
    ]
    if not candidatas:
        return None
    return max(candidatas, key=lambda t: t["vigencia_desde"])


def tarifa(categoria: str | None, cuando: date | None = None) -> float | None:
    """USD por mensaje entregado de esa categoría. `None` = no hay dato, que NO
    es lo mismo que gratis."""
    if not categoria:
        return None
    tarjeta = tarjeta_vigente(cuando)
    if not tarjeta:
        return None
    return tarjeta.get("tarifas", {}).get(categoria)


def _sin_veredicto(pricing_type: str | None) -> bool:
    """¿Meta todavía no dijo nada del cobro de este mensaje?

    UNA sola definición, compartida por el resumen y por la serie diaria. El
    veredicto es el TIPO: la categoría sola no dice si se cobra (una fila con
    `category='service'` y `type=NULL` no es «gratis», es «no sabemos»). Cuando
    esto vivía duplicado, las dos funciones discrepaban justo en esas filas —la
    gráfica las contaba como sin dato y la tabla como gratis— y el panel se
    desmentía a sí mismo, que es exactamente lo que esta sección existe para
    no hacer.
    """
    return pricing_type is None


def _conteos(session, desde: datetime, hasta: datetime) -> list[tuple]:
    """(día, categoría, tipo, cuántos) de los SALIENTES del periodo.

    Agrupado POR DÍA aunque el resumen sea del periodo entero, por dos razones:
    la tarifa se aplica con el rate card vigente el día de la ENTREGA (un
    reporte que cruce el 1-oct-2026 cotizaría octubre con la tarjeta de 2025 si
    se usara una sola fecha), y así el total y la gráfica salen de la MISMA
    consulta y no pueden discrepar.

    Un solo round-trip agrupado en vez de traerse los mensajes: a fin de mes son
    decenas de miles de filas que no hacen falta en memoria.

    Cuenta también los mensajes ocultos (`habilitado = 0`): Meta los cobró
    aunque el panel ya no los muestre.
    """
    return session.exec(
        select(
            func.date(Message.created_at),
            Message.pricing_category,
            Message.pricing_type,
            func.count(),
        )
        .where(
            Message.direction == Direction.OUT,
            Message.created_at >= desde,
            Message.created_at < hasta,
        )
        .group_by(func.date(Message.created_at), Message.pricing_category, Message.pricing_type)
    ).all()


def _como_fecha(dia) -> date | None:
    try:
        return date.fromisoformat(str(dia))
    except (TypeError, ValueError):
        return None


def resumen(session, desde: datetime, hasta: datetime) -> dict:
    """Qué salió y qué de eso se cobra, entre `desde` (incluido) y `hasta`.

    Devuelve conteos exactos y un costo estimado, siempre acompañado de su
    procedencia, de cuántos mensajes se quedaron sin veredicto y de qué tan
    completo está el total.
    """
    filas = _conteos(session, desde, hasta)

    por_categoria: dict[str, dict] = defaultdict(
        lambda: {"total": 0, "cobrables": 0, "costo_usd": 0.0, "sin_tarifa": 0}
    )
    dias: dict[str, dict] = defaultdict(lambda: {"salientes": 0, "cobrables": 0, "sin_dato": 0})
    salientes = con_veredicto = cobrables = 0
    costo_usd = 0.0
    categorias_sin_tarifa: set[str] = set()
    tarjetas_usadas: dict[str, dict] = {}

    for dia, categoria, tipo, cuantos in filas:
        salientes += cuantos
        del_dia = dias[str(dia)]
        del_dia["salientes"] += cuantos

        if _sin_veredicto(tipo):
            del_dia["sin_dato"] += cuantos
            continue

        con_veredicto += cuantos
        etiqueta = categoria or "sin categoría"
        entrada = por_categoria[etiqueta]
        entrada["total"] += cuantos
        if tipo != TIPO_COBRABLE:
            continue

        cobrables += cuantos
        del_dia["cobrables"] += cuantos
        entrada["cobrables"] += cuantos

        # La tarifa del día de la entrega, no la del inicio del periodo.
        fecha = _como_fecha(dia) or desde.date()
        tarjeta = tarjeta_vigente(fecha)
        if tarjeta:
            tarjetas_usadas[tarjeta.get("vigencia_desde", "")] = tarjeta
        precio = tarifa(categoria, fecha)
        if precio is None:
            # Cobrable con tarifa desconocida. Sumarlo como 0 haría que el total
            # pareciera barato justo cuando aparece una categoría nueva.
            entrada["sin_tarifa"] += cuantos
            categorias_sin_tarifa.add(etiqueta)
            continue
        entrada["costo_usd"] += precio * cuantos
        costo_usd += precio * cuantos

    sin_dato = salientes - con_veredicto
    tarjeta_inicial = tarjeta_vigente(desde.date())
    if tarjeta_inicial:
        tarjetas_usadas.setdefault(tarjeta_inicial.get("vigencia_desde", ""), tarjeta_inicial)

    return {
        "desde": desde,
        "hasta": hasta,
        "salientes": salientes,
        "con_veredicto": con_veredicto,
        # El denominador honesto: Twilio nunca reporta precio, y un acuse cuyo
        # wamid no empareja deja su mensaje mudo.
        "sin_dato": sin_dato,
        "cobrables": cobrables,
        "gratis": con_veredicto - cobrables,
        "por_categoria": dict(sorted(por_categoria.items())),
        "costo_usd": costo_usd,
        # DOS motivos distintos por los que el total puede quedarse corto, y hay
        # que poder distinguirlos: falta la TARIFA de algo que sí se cobra, o
        # falta el VEREDICTO de Meta sobre el mensaje entero. El segundo es hoy
        # el caso normal —todo lo anterior al despliegue tiene pricing NULL— y
        # antes no encendía ninguna bandera: el panel enseñaba un total corto
        # sin una sola advertencia.
        "costo_incompleto": bool(categorias_sin_tarifa),
        "categorias_sin_tarifa": sorted(categorias_sin_tarifa),
        "costo_parcial": salientes > 0 and sin_dato > 0,
        "cobertura": (con_veredicto / salientes) if salientes else None,
        # `estimable` separa «costó cero» de «no se puede saber»: sin un solo
        # veredicto, un $0.00 en pantalla sería una afirmación falsa.
        "estimable": con_veredicto > 0,
        "rate_card": tarjeta_inicial,
        "rate_cards": [tarjetas_usadas[k] for k in sorted(tarjetas_usadas) if k],
        "rate_card_multiple": len([k for k in tarjetas_usadas if k]) > 1,
        "moneda": RATE_CARD.get("moneda", "USD"),
        "_dias": dict(sorted(dias.items())),
    }


def serie_diaria(session, desde: datetime, hasta: datetime) -> list[dict]:
    """Por día: salientes, cobrables y sin dato. Para la gráfica de /activity.

    Sale del MISMO recuento que `resumen()` —no de una consulta paralela— para
    que la suma de la gráfica cuadre con las tarjetas por construcción y no por
    coincidencia. La plantilla afirma eso por escrito.
    """
    return [{"dia": d, **v} for d, v in resumen(session, desde, hasta)["_dias"].items()]
