"""Histórico de actividad por colaborador.

Cada acción sobre solicitudes (tomar, cerrar, reasignar, notas) queda
registrada en TicketActivity. Aquí se calculan los despachos (solicitudes
cerradas) por día/semana/mes de cada colaborador; los admin ven al equipo
completo y pueden abrir el histórico de cualquiera.
"""

import logging
import math
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from app.templating import BUSINESS_TZ, templates
from sqlalchemy import func
from sqlmodel import select

from app import medidor
from app.auth import is_admin
from app.db import get_session
from app.models import (
    Feedback,
    Label,
    Ticket,
    TicketActivity,
    TicketLabel,
    TicketStatus,
    User,
)

logger = logging.getLogger(__name__)

router = APIRouter()

RECENT_LIMIT = 50

# Gráfica de despachos: tope del rango para que la página no crezca sin
# control (92 días ≈ un trimestre; suficiente para la vista ejecutiva).
MAX_CHART_DAYS = 92
# Colores por colaborador (naranja/morado primero, como las líneas del diseño
# de referencia); el color nunca es el único indicador — la leyenda y el
# tooltip llevan nombre y número.
CHART_PALETTE = [
    "#f59e0b", "#8b5cf6", "#22d3ee", "#ec4899",
    "#10b981", "#2563eb", "#ef4444", "#64748b",
]

# --- Mensajería y costo ------------------------------------------------------
# Fuente primaria de Meta, consultada el 2026-08-24: desde el 1-oct-2026 se
# cobra por mensaje el saliente de servicio, y las plantillas de UTILIDAD
# respondidas dentro de la ventana de 24 h dejan de estar exentas. Hoy casi todo
# el saliente de este bot es servicio gratis dentro de la ventana.
FUENTE_COBRO_SERVICIO = (
    "developers.facebook.com/documentation/business-messaging/whatsapp/"
    "pricing/non-template-messages"
)
# La fecha vive aquí y NO se deriva de rate_card.json —que sí es la fuente única
# de las TARIFAS— por dos razones: el rate card se lee dentro de un try y puede
# quedar vacío, y es justo cuando falla que este aviso más se necesita; y la
# vigencia de una tarjeta solo dice cuándo cambian los precios, no dice que la
# utilidad pierde su exención en ventana, que es la otra mitad del hecho.
COBRO_SERVICIO_DESDE = date(2026, 10, 1)
# Cuándo se leyó la fuente, y hasta cuándo se comprometió Meta a publicar las
# tarifas de octubre. Viven aquí y no escritas a mano en la plantilla: un texto
# fechado que nadie recalcula sigue leyéndose como vigente para siempre.
FUENTE_CONSULTADA_EL = date(2026, 8, 24)
PLAZO_PUBLICACION_TARIFAS = date(2026, 9, 1)

# Series de la gráfica de mensajería. El color NUNCA es el único indicador: cada
# serie lleva además patrón de trazo (MENSAJERIA_TRAZOS), nombre en la leyenda y
# número en la tabla de abajo.
MENSAJERIA_SERIES = {
    "Salientes": "#2563eb",
    "Cobrables": "#d97706",
    "Sin dato de cobro": "#6d7280",
}
MENSAJERIA_TRAZOS = {"Salientes": "", "Cobrables": "8 4", "Sin dato de cobro": "2 4"}

ACTION_LABELS = {
    "tomado": "tomó la solicitud",
    "soltado": "soltó la solicitud",
    "cerrado": "cerró la solicitud ✅",
    "nota": "agregó una nota",
    "agente:pausado": "apagó el agente IA",
    "agente:pausado-auto": "apagó el agente IA (al responder por bandeja)",
    "agente:reanudado": "reanudó el agente IA",
    # Cobranza. Sin estas claves /activity mostraría el slug crudo.
    "cobro:generado": "generó un cobro",
    "cobro:enviado": "envió el link de pago",
    "cobro:voucher": "el cliente pidió su ficha de pago",
    "cobro:pagado": "registró el pago del cliente 💰",
    "cobro:fallido": "el pago fue rechazado",
    "cobro:vencido": "el cobro venció sin pagarse",
    "cobro:cancelado": "canceló el cobro",
    "cobro:cancelacion-pendiente": "pidió cancelar el cobro (Stripe no confirmó)",
    "cobro:confirmado-asesor": "confirmó el pago contra el recibo del cliente",
    "cobro:cierre-con-pago-pendiente": "cerró el folio con un cobro sin liquidar",
}


def _label(action: str) -> str:
    if action in ACTION_LABELS:
        return ACTION_LABELS[action]
    if action.startswith("estado:"):
        return f"cambió el estado a {action.removeprefix('estado:').replace('_', ' ')}"
    if action.startswith("reasignado:"):
        return f"transfirió la solicitud a {action.removeprefix('reasignado:')}"
    if action.startswith("tomado-de:"):
        return f"le tomó la solicitud a {action.removeprefix('tomado-de:')}"
    if action.startswith("asignado-auto:"):
        return f"el sistema le asignó la solicitud a {action.removeprefix('asignado-auto:')}"
    return action


def _period_starts_utc(now: datetime | None = None) -> dict[str, datetime]:
    """Inicios de hoy/semana/mes en horario de negocio, convertidos a UTC naive
    (como quedan los timestamps al leerse de SQLite)."""
    local_now = (now or datetime.now(BUSINESS_TZ)).astimezone(BUSINESS_TZ)
    day = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = day - timedelta(days=day.weekday())
    month = day.replace(day=1)
    to_utc = lambda dt: dt.astimezone(timezone.utc).replace(tzinfo=None)  # noqa: E731
    return {"day": to_utc(day), "week": to_utc(week), "month": to_utc(month)}


def _as_naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def dispatch_counts(session, starts: dict[str, datetime]) -> dict[str, dict[str, int]]:
    """{usuario: {day: n, week: n, month: n, total: n}} de solicitudes cerradas."""
    closures = session.exec(
        select(TicketActivity).where(TicketActivity.action == "cerrado")
    ).all()

    counts: dict[str, dict[str, int]] = {}
    for act in closures:
        row = counts.setdefault(act.username, {"day": 0, "week": 0, "month": 0, "total": 0})
        row["total"] += 1
        created = _as_naive_utc(act.created_at)
        if created >= starts["month"]:
            row["month"] += 1
        if created >= starts["week"]:
            row["week"] += 1
        if created >= starts["day"]:
            row["day"] += 1
    return counts


def _parse_date(value: str | None, fallback: date) -> date:
    try:
        return date.fromisoformat(value) if value else fallback
    except ValueError:
        return fallback


def daily_dispatches(
    session, start: date, end: date, username: str | None = None
) -> list[dict]:
    """Cierres por día (horario de negocio) entre start y end inclusive.

    Devuelve un item por CADA día del rango (con total 0 si no hubo), para
    que la gráfica no tenga huecos: [{"date", "total", "by_user": {user: n}}].
    """
    query = select(TicketActivity).where(TicketActivity.action == "cerrado")
    if username:
        query = query.where(TicketActivity.username == username)
    # ponytail: filtrado en Python igual que dispatch_counts — son cientos de
    # filas; pasar el rango al WHERE si algún día son cientos de miles.
    rows = session.exec(query).all()

    days: dict[date, dict[str, int]] = {
        start + timedelta(days=i): {} for i in range((end - start).days + 1)
    }
    for act in rows:
        local_day = (
            _as_naive_utc(act.created_at)
            .replace(tzinfo=timezone.utc)
            .astimezone(BUSINESS_TZ)
            .date()
        )
        bucket = days.get(local_day)
        if bucket is not None:
            bucket[act.username] = bucket.get(act.username, 0) + 1

    return [
        {"date": d, "total": sum(users.values()), "by_user": dict(sorted(users.items()))}
        for d, users in days.items()
    ]


def _mediana(valores: list[float]) -> float | None:
    if not valores:
        return None
    ordenados = sorted(valores)
    n = len(ordenados)
    if n % 2:
        return ordenados[n // 2]
    return (ordenados[n // 2 - 1] + ordenados[n // 2]) / 2


def primera_respuesta_diaria(session, start: date, end: date) -> list[dict]:
    """Mediana (minutos) de primera respuesta humana, por día de creación.

    Solo cuenta folios con first_response_at: los NULL previos a la migración
    de fase 1 quedan fuera a propósito (el template lo declara). La salida es
    compatible con build_line_chart — serie única «mediana»; un día sin datos
    va en 0 y su punto lo dice en el tooltip.
    """
    # ponytail: filtrado en Python igual que daily_dispatches — mismas
    # magnitudes, mismo upgrade path (WHERE por rango si crece).
    tickets = session.exec(
        select(Ticket).where(
            Ticket.first_response_at.is_not(None),  # type: ignore[union-attr]
            # Misma población que operacion_stats: la tarjeta y la gráfica del
            # tablero deben contar lo mismo o los números no cuadran entre sí.
            Ticket.habilitado == True,  # noqa: E712
        )
    ).all()
    por_dia: dict[date, list[float]] = {
        start + timedelta(days=i): [] for i in range((end - start).days + 1)
    }
    for t in tickets:
        local_day = (
            _as_naive_utc(t.created_at)
            .replace(tzinfo=timezone.utc)
            .astimezone(BUSINESS_TZ)
            .date()
        )
        if local_day in por_dia:
            delta = _as_naive_utc(t.first_response_at) - _as_naive_utc(t.created_at)
            por_dia[local_day].append(delta.total_seconds() / 60)
    out = []
    for d, valores in por_dia.items():
        m = _mediana(valores)
        minutos = int(round(m)) if m is not None else 0
        out.append(
            {"date": d, "total": minutos, "by_user": {"mediana": minutos}, "n": len(valores)}
        )
    return out


def operacion_stats(session) -> dict:
    """Resumen de operación (fase 1): medianas globales, embudo y etiquetas."""
    tickets = session.exec(
        select(Ticket).where(Ticket.habilitado == True)  # noqa: E712
    ).all()
    espera_min = [
        (_as_naive_utc(t.first_response_at) - _as_naive_utc(t.created_at)).total_seconds() / 60
        for t in tickets
        if t.first_response_at
    ]
    resolucion_h = [
        (_as_naive_utc(t.resolved_at) - _as_naive_utc(t.created_at)).total_seconds() / 3600
        for t in tickets
        if t.resolved_at
    ]
    conteo = Counter(t.status.value for t in tickets)
    top_etiquetas = session.exec(
        select(Label.nombre, func.count(TicketLabel.id))
        .join(TicketLabel, TicketLabel.label_id == Label.id)
        .where(
            TicketLabel.habilitado == True,  # noqa: E712
            Label.habilitado == True,  # noqa: E712
        )
        .group_by(Label.nombre)
        .order_by(func.count(TicketLabel.id).desc())
        .limit(10)
    ).all()
    med_espera = _mediana(espera_min)
    med_resolucion = _mediana(resolucion_h)
    return {
        "espera_mediana_min": int(round(med_espera)) if med_espera is not None else None,
        "espera_n": len(espera_min),
        "resolucion_mediana_h": round(med_resolucion, 1) if med_resolucion is not None else None,
        "resolucion_n": len(resolucion_h),
        "embudo": [(s.value, conteo.get(s.value, 0)) for s in TicketStatus],
        "embudo_max": max(conteo.values(), default=0) or 1,
        "top_etiquetas": top_etiquetas,
    }


def _usd(monto: float, moneda: str = "USD") -> str:
    """Formatea un costo ESTIMADO de mensajería.

    No usa `dinero.formato_monto` a propósito: aquel es dinero de Stripe
    —centavos enteros— y aquí la tarifa es una fracción de centavo por mensaje
    (0.0099 USD). Redondear a centavos haría que un costo real se leyera
    "$0.00 USD", exactamente la confusión que esta sección existe para evitar.
    """
    if 0 < monto < 0.01:
        return f"${monto:,.4f} {moneda}"
    return f"${monto:,.2f} {moneda}"


def _rango_utc(start: date, end: date) -> tuple[datetime, datetime]:
    """Los días de negocio [start, end] como límites UTC naive, medio abiertos.

    Los timestamps se guardan en UTC sin zona (utcnow()) y el filtro de la
    página habla en días de CDMX. `hasta` es el arranque del día SIGUIENTE
    porque el medidor filtra `created_at < hasta`.
    """
    desde = datetime.combine(start, time.min, tzinfo=BUSINESS_TZ)
    hasta = datetime.combine(end + timedelta(days=1), time.min, tzinfo=BUSINESS_TZ)
    return (
        desde.astimezone(timezone.utc).replace(tzinfo=None),
        hasta.astimezone(timezone.utc).replace(tzinfo=None),
    )


def carga_abierta(session) -> dict[str | None, int]:
    """Folios VIVOS por asesor. La clave `None` es el trabajo sin dueño.

    «Vivo» = no cerrado y no oculto. No mide esfuerzo ni antigüedad: cuenta
    cabezas de folio, que es lo que un tope de carga puede limitar.

    Existe porque hoy nadie puede ver dónde está parado el trabajo: el panel
    solo sabe contar los folios SIN asignar (dashboard.py), y en cuanto la
    fase 3b empiece a repartir automáticamente, «sin asignar» deja de ser el
    pendiente y pasa a ser solo el desbordamiento. Repartir sin poder ver el
    reparto es exactamente el punto ciego que esta función tapa.

    Un asesor sin folios vivos NO aparece en el resultado: quien la use para
    pintar una tabla debe partir de la lista de usuarios, no de estas llaves.
    """
    filas = session.exec(
        select(Ticket.assigned_to, func.count(Ticket.id))
        .where(
            Ticket.status != TicketStatus.CERRADO,
            Ticket.habilitado == True,  # noqa: E712
        )
        .group_by(Ticket.assigned_to)
    ).all()
    return {asesor: n for asesor, n in filas}


def _aviso_cobro_servicio(hoy: date) -> dict:
    """El aviso fechado del 1-oct-2026, con los días que faltan.

    El rate card solo lo ENRIQUECE: si la tarjeta que aplicará ese día ya
    contempla la categoría `service`, el aviso puede decir si Meta ya publicó su
    precio. Si el rate card no se pudo leer, el aviso sale igual y completo.
    """
    tarjeta = medidor.tarjeta_vigente(COBRO_SERVICIO_DESDE)
    tarifas = (tarjeta or {}).get("tarifas") or {}
    return {
        "desde": COBRO_SERVICIO_DESDE,
        "dias": (COBRO_SERVICIO_DESDE - hoy).days,
        "vigente": hoy >= COBRO_SERVICIO_DESDE,
        "fuente": FUENTE_COBRO_SERVICIO,
        "contempla_servicio": "service" in tarifas,
        # None con contempla_servicio=True = Meta aún no publica el precio.
        "tarifa_servicio": tarifas.get("service"),
        "consultada_el": FUENTE_CONSULTADA_EL,
        "plazo_tarifas": PLAZO_PUBLICACION_TARIFAS,
        # El plazo ya venció y la tarifa sigue sin capturarse: deja de ser una
        # promesa a futuro y pasa a ser un pendiente. Sin esto, el aviso seguía
        # diciendo «se compromete a publicarlas» meses después de la fecha.
        "plazo_vencido": hoy > PLAZO_PUBLICACION_TARIFAS,
    }


def mensajeria_stats(session, start: date, end: date, hoy: date | None = None) -> dict:
    """Cuánto saliente hubo en el rango y cuánto de eso cobra Meta.

    Los conteos y el costo salen enteros de `app.medidor` (que copia el veredicto
    de Meta y nunca lo adivina); aquí solo se arma la presentación y la geometría
    del SVG, reutilizando `build_line_chart`.
    """
    hoy = hoy or datetime.now(BUSINESS_TZ).date()
    desde, hasta = _rango_utc(start, end)
    resumen = medidor.resumen(session, desde, hasta)
    # Del MISMO recuento, no de una segunda consulta: llamar también a
    # serie_diaria() repetía el GROUP BY entero en cada carga de la página, y
    # dejaba abierta la puerta a que la gráfica y las tarjetas discrepen.
    serie = resumen["_dias"]

    # El eje va por día UTC y no por día de negocio: `medidor.serie_diaria`
    # agrupa con func.date(created_at), que en SQLite es UTC. Recorriendo los
    # días UTC que caben entre los límites, la suma de la gráfica es EXACTAMENTE
    # la de las tarjetas; con un eje local, el saliente de las 18:00–23:59 CDMX
    # del último día caería en un día UTC fuera del eje y la gráfica desmentiría
    # al total, que es la única falla que esta sección no se puede permitir.
    # ponytail: por eso el corte diario queda en UTC (hasta 6 h corrido respecto
    # al día de negocio del resto de la página). Se arregla pasándole un offset a
    # medidor.serie_diaria, no maquillándolo aquí.
    dia_ini = desde.date()
    dia_fin = (hasta - timedelta(seconds=1)).date()
    chart = []
    for i in range((dia_fin - dia_ini).days + 1):
        dia = dia_ini + timedelta(days=i)
        v = serie.get(dia.isoformat(), {})
        chart.append(
            {
                "date": dia,
                "total": v.get("salientes", 0),
                "by_user": {
                    "Salientes": v.get("salientes", 0),
                    "Cobrables": v.get("cobrables", 0),
                    "Sin dato de cobro": v.get("sin_dato", 0),
                },
                # Sin clave "n" a propósito: build_line_chart le agrega
                # "(sin datos)" al tooltip cuando n == 0, y aquí un día en cero
                # es una medición real (no salió nada), no un hueco de medición.
            }
        )

    return {
        **resumen,
        "por_categoria": [
            {
                "categoria": nombre,
                "total": v["total"],
                "cobrables": v["cobrables"],
                "gratis": v["total"] - v["cobrables"],
                # Una categoría cobrable SIN tarifa conocida no vale $0.00: no es
                # cotizable. Imprimir el cero contradecía a la nota de la misma celda.
                "costo": (
                    "no cotizable"
                    if v["cobrables"] and v["sin_tarifa"] >= v["cobrables"]
                    else _usd(v["costo_usd"], resumen["moneda"])
                ),
                "sin_tarifa": v["sin_tarifa"],
            }
            for nombre, v in resumen["por_categoria"].items()
        ],
        # $0.00 servía para tres cosas distintas: «no salió nada», «salió y fue
        # verificadamente gratis» y «salió y no sabemos nada». La tercera es hoy
        # el caso normal para cualquier periodo anterior al despliegue, y era la
        # que se leía como un cobro de cero.
        "costo_texto": (
            "sin salientes"
            if not resumen["salientes"]
            else "no estimable"
            if not resumen["estimable"]
            else _usd(resumen["costo_usd"], resumen["moneda"])
        ),
        "costo_estimable": resumen["estimable"] and bool(resumen["salientes"]),
        # El mismo recuento que la gráfica, como texto: un <svg role="img"> poda
        # sus <title> del árbol de accesibilidad.
        "dias": [{"dia": d, **v} for d, v in resumen["_dias"].items()],
        # costo_parcial, cobertura, sin_dato y rate_card_multiple ya llegan por el
        # **resumen de arriba; aquí solo el formato de presentación.
        "cobertura_texto": (
            f"{resumen['cobertura']:.0%}" if resumen["cobertura"] is not None else "—"
        ),
        "linea": build_line_chart(chart, MENSAJERIA_SERIES),
        "trazos": MENSAJERIA_TRAZOS,
        "aviso": _aviso_cobro_servicio(hoy),
    }


def _smooth_path(pts: list[tuple[float, float]]) -> str:
    """Curva suave (Catmull-Rom → Bézier) por los puntos de la serie."""
    if len(pts) == 1:
        x, y = pts[0]
        return f"M {x - 5},{y} L {x + 5},{y}"
    parts = [f"M {pts[0][0]},{pts[0][1]}"]
    for i in range(len(pts) - 1):
        p0 = pts[i - 1] if i > 0 else pts[i]
        p1, p2 = pts[i], pts[i + 1]
        p3 = pts[i + 2] if i + 2 < len(pts) else p2
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        parts.append(
            f"C {c1[0]:.1f},{c1[1]:.1f} {c2[0]:.1f},{c2[1]:.1f} {p2[0]:.1f},{p2[1]:.1f}"
        )
    return " ".join(parts)


def build_line_chart(chart: list[dict], colors: dict[str, str]) -> dict:
    """Geometría SVG de la gráfica de líneas (una serie por colaborador)."""
    n = len(chart)
    pad_l, pad_r, pad_t, pad_b = 34, 14, 14, 26
    width = max(620, 52 * n + pad_l + pad_r)
    height = 240
    ymax = max((d["by_user"].get(u, 0) for d in chart for u in colors), default=0) or 1

    def x(i: int) -> float:
        span = width - pad_l - pad_r
        return pad_l + (i * span / (n - 1) if n > 1 else span / 2)

    def y(v: int) -> float:
        return pad_t + (1 - v / ymax) * (height - pad_t - pad_b)

    series = []
    for u, color in colors.items():
        pts = [(round(x(i), 1), round(y(d["by_user"].get(u, 0)), 1)) for i, d in enumerate(chart)]
        series.append(
            {
                "user": u,
                "color": color,
                "path": _smooth_path(pts),
                "points": [
                    {
                        "x": px, "y": py,
                        # Un día con n=0 dibuja 0 pero el tooltip lo dice: un
                        # cero real y un "sin datos" no deben verse iguales.
                        "label": f"{u} — {chart[i]['date'].strftime('%d/%m')}: "
                                 f"{chart[i]['by_user'].get(u, 0)}"
                                 + (" (sin datos)" if chart[i].get("n") == 0 else ""),
                    }
                    for i, (px, py) in enumerate(pts)
                ],
            }
        )
    step = max(1, math.ceil(n / 12))  # no amontonar etiquetas en rangos largos
    x_labels = [
        (round(x(i), 1), d["date"].strftime("%d/%m"))
        for i, d in enumerate(chart)
        if i % step == 0 or i == n - 1
    ]
    ticks = sorted({0, ymax // 2, ymax}) if ymax > 1 else [0, 1]
    return {
        "width": width, "height": height, "pad_l": pad_l, "pad_r": pad_r,
        "series": series, "x_labels": x_labels,
        "y_ticks": [(round(y(v), 1), v) for v in ticks],
    }


def build_donut(chart: list[dict], colors: dict[str, str]) -> dict:
    """Dona de distribución por colaborador en el rango (como la referencia)."""
    totals: dict[str, int] = {}
    for d in chart:
        for u, count in d["by_user"].items():
            totals[u] = totals.get(u, 0) + count
    total = sum(totals.values())
    if total == 0:
        return {"total": 0, "segments": []}
    circumference = 2 * math.pi * 54  # r=54 en un viewBox de 140x140
    segments, offset = [], 0.0
    for u, color in colors.items():
        count = totals.get(u, 0)
        if count == 0:
            continue
        length = count / total * circumference
        segments.append(
            {
                "user": u, "color": color, "n": count,
                "pct": round(count / total * 100),
                # pequeño espacio entre segmentos, como en el diseño
                "dash": f"{max(length - 4, 1):.1f} {circumference:.1f}",
                "offset": f"{-offset:.1f}",
            }
        )
        offset += length
    return {"total": total, "segments": segments}


def feedback_counts(session) -> dict[str, dict[str, int]]:
    """{colaborador: {"up": n, "down": n}} — opiniones 👍/👎 de clientes sobre
    tickets atendidos por cada quien (botones del aviso de cierre)."""
    rows = session.exec(
        select(Feedback, Ticket).where(Feedback.ticket_id == Ticket.id)
    ).all()
    counts: dict[str, dict[str, int]] = {}
    for fb, ticket in rows:
        if not ticket.assigned_to:
            continue
        row = counts.setdefault(ticket.assigned_to, {"up": 0, "down": 0})
        if fb.rating in row:
            row[fb.rating] += 1
    return counts


def backfill_closed_tickets() -> None:
    """Puebla el histórico una sola vez con los cierres previos a esta función.

    Los tickets ya cerrados no tienen eventos; se les crea un 'cerrado'
    sintético con su updated_at para que las estadísticas no arranquen en cero.
    Idempotente: solo corre si la tabla de actividad está vacía.
    """
    with get_session() as session:
        if session.exec(select(TicketActivity)).first() is not None:
            return
        closed = session.exec(
            select(Ticket)
            .where(Ticket.status == TicketStatus.CERRADO)
            .where(Ticket.assigned_to.is_not(None))
        ).all()
        for ticket in closed:
            session.add(
                TicketActivity(
                    ticket_id=ticket.id,
                    username=ticket.assigned_to,
                    action="cerrado",
                    created_at=ticket.updated_at,
                )
            )
        if closed:
            session.commit()
            logger.info("Histórico de actividad poblado con %s cierres previos", len(closed))


@router.get("/activity")
def activity_view(
    request: Request,
    user: str | None = None,
    desde: str | None = None,
    hasta: str | None = None,
):
    me = request.session.get("user")
    if not me:
        return RedirectResponse("/login", status_code=303)

    admin = is_admin(me)
    viewing = user if (user and admin) else me

    # Rango de la gráfica: por defecto el día actual (horario de negocio).
    today_local = datetime.now(BUSINESS_TZ).date()
    chart_start = _parse_date(desde, today_local)
    chart_end = _parse_date(hasta, today_local)
    if chart_end < chart_start:
        chart_start, chart_end = chart_end, chart_start
    if (chart_end - chart_start).days + 1 > MAX_CHART_DAYS:
        chart_start = chart_end - timedelta(days=MAX_CHART_DAYS - 1)

    starts = _period_starts_utc()
    with get_session() as session:
        counts = dispatch_counts(session, starts)
        # Admin ve la gráfica del equipo completo (apilada por colaborador);
        # un colaborador solo la suya.
        chart = daily_dispatches(
            session, chart_start, chart_end, username=None if admin else viewing
        )
        recent = session.exec(
            select(TicketActivity)
            .where(TicketActivity.username == viewing)
            .order_by(TicketActivity.created_at.desc())
            .limit(RECENT_LIMIT)
        ).all()
        team = None
        operacion = None
        mensajeria = None
        pr_chart = None
        pr_dias_con_dato = 0
        # La carga del equipo es admin-only, igual que el resto de las métricas
        # de equipo; un asesor no ve la bandeja de los demás.
        sin_asignar = 0
        sin_quien = 0
        if admin:
            usernames = [u.username for u in session.exec(select(User)).all()]
            fb = feedback_counts(session)
            carga = carga_abierta(session)
            # Un asesor deshabilitado no puede entrar (dashboard rechaza su
            # login), así que sus folios vivos no los va a mover nadie. Contarlos
            # como «cubiertos» es exactamente el bug del espejo que se declaraba
            # al día: trabajo huérfano presentado como atendido.
            inhabilitados = {
                u.username for u in session.exec(select(User)).all() if not u.habilitado
            }
            sin_quien = sum(n for a, n in carga.items() if a in inhabilitados)
            team = [
                {
                    "username": name,
                    **counts.get(name, {"day": 0, "week": 0, "month": 0, "total": 0}),
                    "fb_up": fb.get(name, {}).get("up", 0),
                    "fb_down": fb.get(name, {}).get("down", 0),
                    # Lo único de esta tabla que mira el PRESENTE: las demás
                    # columnas cuentan cierres pasados. Sin esto no se puede ver
                    # quién está saturado ahora, que es lo que un tope limita.
                    "abiertos": carga.get(name, 0),
                    "inhabilitado": name in inhabilitados,
                }
                for name in sorted(usernames)
            ]
            sin_asignar = carga.get(None, 0)
            # Métricas de operación (fase 1) — vista de equipo, solo admin.
            operacion = operacion_stats(session)
            # Mensajería y costo: mismo rango que la gráfica de despachos, misma
            # puerta de admin que el resto de las métricas de equipo.
            mensajeria = mensajeria_stats(session, chart_start, chart_end)
            pr_diaria = primera_respuesta_diaria(session, chart_start, chart_end)
            pr_dias_con_dato = sum(1 for d in pr_diaria if d["n"])
            if pr_dias_con_dato:
                pr_chart = build_line_chart(pr_diaria, {"mediana": "#0ea5e9"})

    chart_users = sorted({u for d in chart for u in d["by_user"]})
    chart_colors = {
        u: CHART_PALETTE[i % len(CHART_PALETTE)] for i, u in enumerate(chart_users)
    }
    mine = counts.get(viewing, {"day": 0, "week": 0, "month": 0, "total": 0})
    return templates.TemplateResponse(
        "activity.html",
        {
            "request": request,
            "user": me,
            "viewing": viewing,
            "stats": mine,
            "recent": [
                {"ticket_id": a.ticket_id, "label": _label(a.action), "at": a.created_at}
                for a in recent
            ],
            "team": team,
            "sin_asignar": sin_asignar,
            "sin_quien": sin_quien,
            "chart": chart,
            "chart_max": max((d["total"] for d in chart), default=0) or 1,
            "chart_total": sum(d["total"] for d in chart),
            "chart_colors": chart_colors,
            "line_chart": build_line_chart(chart, chart_colors),
            "donut": build_donut(chart, chart_colors),
            "desde": chart_start.isoformat(),
            "hasta": chart_end.isoformat(),
            "operacion": operacion,
            "mensajeria": mensajeria,
            "pr_chart": pr_chart,
            "pr_dias_con_dato": pr_dias_con_dato,
        },
    )
