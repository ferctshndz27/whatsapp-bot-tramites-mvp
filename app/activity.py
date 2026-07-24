"""Histórico de actividad por colaborador.

Cada acción sobre solicitudes (tomar, cerrar, reasignar, notas) queda
registrada en TicketActivity. Aquí se calculan los despachos (solicitudes
cerradas) por día/semana/mes de cada colaborador; los admin ven al equipo
completo y pueden abrir el histórico de cualquiera.
"""

import logging
import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from app.auth import is_admin
from app.db import get_session
from app.models import Feedback, Ticket, TicketActivity, TicketStatus, User

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.globals["is_admin"] = is_admin

# Los cortes de "hoy/semana/mes" usan la zona horaria del negocio, no UTC —
# si no, un cierre a las 20:00 de México contaría para el día siguiente.
BUSINESS_TZ = ZoneInfo("America/Mexico_City")
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

ACTION_LABELS = {
    "tomado": "tomó la solicitud",
    "soltado": "soltó la solicitud",
    "cerrado": "cerró la solicitud ✅",
    "nota": "agregó una nota",
    "agente:pausado": "apagó el agente IA",
    "agente:pausado-auto": "apagó el agente IA (al responder por bandeja)",
    "agente:reanudado": "reanudó el agente IA",
}


def _label(action: str) -> str:
    if action in ACTION_LABELS:
        return ACTION_LABELS[action]
    if action.startswith("estado:"):
        return f"cambió el estado a {action.removeprefix('estado:').replace('_', ' ')}"
    if action.startswith("reasignado:"):
        return f"transfirió la solicitud a {action.removeprefix('reasignado:')}"
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
                        "label": f"{u} — {chart[i]['date'].strftime('%d/%m')}: "
                                 f"{chart[i]['by_user'].get(u, 0)}",
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
        if admin:
            usernames = [u.username for u in session.exec(select(User)).all()]
            fb = feedback_counts(session)
            team = [
                {
                    "username": name,
                    **counts.get(name, {"day": 0, "week": 0, "month": 0, "total": 0}),
                    "fb_up": fb.get(name, {}).get("up", 0),
                    "fb_down": fb.get(name, {}).get("down", 0),
                }
                for name in sorted(usernames)
            ]

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
            "chart": chart,
            "chart_max": max((d["total"] for d in chart), default=0) or 1,
            "chart_total": sum(d["total"] for d in chart),
            "chart_colors": chart_colors,
            "line_chart": build_line_chart(chart, chart_colors),
            "donut": build_donut(chart, chart_colors),
            "desde": chart_start.isoformat(),
            "hasta": chart_end.isoformat(),
        },
    )
