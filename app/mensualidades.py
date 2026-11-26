"""Calendario de cobranza de mensualidades: reglas puras (sin base ni red).

El estatus de una mensualidad NO se guarda: se deriva de `vence_el`, `pagado_at` y el día de hoy
en hora de México. Así cambia a las 00:00 sin cron y sin un `update()` masivo que se saltaría el
espejo MSSQL (ver el plan, «Decisión principal»).
"""

import calendar
from datetime import date, datetime

from app.models import ClienteMensualidad, Mensualidad
from app.templating import BUSINESS_TZ

PAGADO = "pagado"
VENCIDO = "vencido"
POR_VENCER = "por_vencer"
PENDIENTE = "pendiente"
ESTATUS = (VENCIDO, POR_VENCER, PENDIENTE, PAGADO)
# Texto de cada estatus: el color nunca va solo (el panel promete un segundo canal).
ETIQUETAS = {
    PAGADO: "Pagado",
    VENCIDO: "Vencido",
    POR_VENCER: "Por vencer",
    PENDIENTE: "Pendiente",
}

MESES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)  # fmt: skip


def hoy_mx(ahora: datetime | None = None) -> date:
    """El día de hoy en la zona del negocio; `ahora` se inyecta en las pruebas."""
    return (ahora or datetime.now(BUSINESS_TZ)).astimezone(BUSINESS_TZ).date()


def vence_el(periodo: str, dia_pago: int) -> date:
    """Fecha de vencimiento de un periodo AAAA-MM: el día de pago, o el último del mes si no existe
    (día 31 en febrero → 28 o 29)."""
    anio, mes = (int(x) for x in periodo.split("-"))
    return date(anio, mes, min(dia_pago, calendar.monthrange(anio, mes)[1]))


def estado_de(m: Mensualidad, hoy: date, aviso: int) -> str:
    """pagado | vencido | por_vencer | pendiente.

    Vence hoy NO es vencido. Por vencer: faltan `aviso` días o menos (incluido el de hoy)."""
    if m.pagado_at is not None:
        return PAGADO
    if m.vence_el < hoy:
        return VENCIDO
    if (m.vence_el - hoy).days <= aviso:
        return POR_VENCER
    return PENDIENTE


def faltantes(c: ClienteMensualidad) -> list[str]:
    """Qué le falta a un cliente para generar mensualidades («Por completar»)."""
    falta = []
    if not c.whatsapp:
        falta.append("whatsapp")
    if c.importe_centavos is None:
        falta.append("importe")
    if c.dia_pago is None:
        falta.append("dia_pago")
    if c.primer_pago is None:
        falta.append("primer_pago")
    return falta


def parse_mes(valor: str | None, hoy: date) -> date:
    """Primer día del mes pedido en `?mes=AAAA-MM`; algo ilegible cae al mes de hoy (la URL la
    teclea cualquiera, no debe tumbar la página)."""
    try:
        anio, mes = (int(x) for x in (valor or "").split("-"))
        return date(anio, mes, 1)
    except ValueError:
        return hoy.replace(day=1)


def mes_vecino(primero: date, delta: int) -> date:
    n = primero.year * 12 + (primero.month - 1) + delta
    return date(n // 12, n % 12 + 1, 1)


def semanas_del_mes(primero: date) -> list[list[date | None]]:
    """Cuadrícula lunes→domingo; los huecos de otro mes van como None."""
    return [
        [date(primero.year, primero.month, n) if n else None for n in sem]
        for sem in calendar.Calendar(firstweekday=0).monthdayscalendar(primero.year, primero.month)
    ]
