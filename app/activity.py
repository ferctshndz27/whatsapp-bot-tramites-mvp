"""Histórico de actividad por colaborador.

Cada acción sobre solicitudes (tomar, cerrar, reasignar, notas) queda
registrada en TicketActivity. Aquí se calculan los despachos (solicitudes
cerradas) por día/semana/mes de cada colaborador; los admin ven al equipo
completo y pueden abrir el histórico de cualquiera.
"""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from app.auth import is_admin
from app.db import get_session
from app.models import Ticket, TicketActivity, TicketStatus, User

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.globals["is_admin"] = is_admin

# Los cortes de "hoy/semana/mes" usan la zona horaria del negocio, no UTC —
# si no, un cierre a las 20:00 de México contaría para el día siguiente.
BUSINESS_TZ = ZoneInfo("America/Mexico_City")
RECENT_LIMIT = 50

ACTION_LABELS = {
    "tomado": "tomó la solicitud",
    "soltado": "soltó la solicitud",
    "cerrado": "cerró la solicitud ✅",
    "nota": "agregó una nota",
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
def activity_view(request: Request, user: str | None = None):
    me = request.session.get("user")
    if not me:
        return RedirectResponse("/login", status_code=303)

    admin = is_admin(me)
    viewing = user if (user and admin) else me

    starts = _period_starts_utc()
    with get_session() as session:
        counts = dispatch_counts(session, starts)
        recent = session.exec(
            select(TicketActivity)
            .where(TicketActivity.username == viewing)
            .order_by(TicketActivity.created_at.desc())
            .limit(RECENT_LIMIT)
        ).all()
        team = None
        if admin:
            usernames = [u.username for u in session.exec(select(User)).all()]
            team = [
                {"username": name, **counts.get(name, {"day": 0, "week": 0, "month": 0, "total": 0})}
                for name in sorted(usernames)
            ]

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
        },
    )
