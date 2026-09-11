"""Mi día: panel personal del colaborador.

Sus tickets activos, los chats que esperan su respuesta (con las horas que
quedan de la ventana de 24h de WhatsApp), sus seguimientos pendientes y su
estado de disponibilidad. Es la pantalla de trabajo diaria — la lista global
de solicitudes queda para supervisión.
"""

import logging
from datetime import timedelta, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from app.templating import templates
from sqlmodel import select

from app import cobranza_panel
from app.auth import is_admin, alcance
from app.db import get_session
from app.inbox import build_conversation_items
from app.ventana import WINDOW_HOURS, horas_restantes
from app.models import FollowUp, Ticket, TicketStatus, User, utcnow

logger = logging.getLogger(__name__)

router = APIRouter()

ESTADOS = ("disponible", "ocupado", "ausente")
EXPIRING_HOURS = 6  # ventana de 24h con menos de esto se marca urgente


def _current_user(request: Request) -> str | None:
    return request.session.get("user")


def _hours_left(last_at) -> float:
    # `last_at` aquí ES el último entrante: la lista viene filtrada a los chats
    # que esperan respuesta (last_direction == "in").
    return horas_restantes(last_at)


@router.get("/my")
def my_day(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    vista = alcance(user)
    now = utcnow()
    with get_session() as session:
        # Acotado aunque sean SUYOS: un folio de otra oficina con su nombre
        # (repartido antes de que asignacion respetara la oficina) no se puede
        # abrir, y listarlo aquí sería mandarlo a una puerta cerrada.
        tickets = session.exec(
            vista.filtrar(select(Ticket), Ticket.office_id)
            .where(Ticket.assigned_to == user)
            .where(Ticket.status != TicketStatus.CERRADO)
            .where(Ticket.habilitado == True)  # noqa: E712
            .order_by(Ticket.updated_at.desc())
        ).all()

        waiting = [
            {**item, "hours_left": _hours_left(item["last_at"])}
            for item in build_conversation_items(session, vista)
            if item["waiting"] and item["assigned_to"] == user
        ]
        waiting.sort(key=lambda i: i["hours_left"])

        followups = session.exec(
            select(FollowUp)
            .where(FollowUp.username == user)
            .where(FollowUp.done_at.is_(None))
            .where(FollowUp.habilitado == True)  # noqa: E712
            .order_by(FollowUp.due_at)
        ).all()
        me = session.exec(select(User).where(User.username == user)).first()

    # Fuera del `with`: abre su propia sesión y no tiene por qué alargar esta.
    cobros_vivos, cobros_en_la_calle = cobranza_panel.cartera_de(user)

    naive_now = now.replace(tzinfo=None)
    return templates.TemplateResponse(
        "my.html",
        {
            "request": request,
            "user": user,
            "tickets": tickets,
            "waiting": waiting,
            "expiring": [w for w in waiting if w["hours_left"] <= EXPIRING_HOURS],
            "due_now": [f for f in followups if f.due_at <= naive_now],
            "upcoming": [f for f in followups if f.due_at > naive_now][:10],
            "estado": me.estado if me else "disponible",
            "estados": ESTADOS,
            "cobros_vivos": cobros_vivos,
            "cobros_en_la_calle": cobros_en_la_calle,
        },
    )


@router.post("/tickets/{ticket_id}/followup")
def create_followup(
    request: Request, ticket_id: int, days: int = Form(...), note: str = Form("")
):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    days = max(0, min(days, 60))  # hoy mismo hasta 2 meses
    vista = alcance(user)
    with get_session() as session:
        ticket = session.get(Ticket, ticket_id)
        # Un folio de otra oficina responde igual que uno que no existe.
        if ticket is None or not vista.alcanza(ticket.office_id):
            return RedirectResponse("/", status_code=303)
        session.add(
            FollowUp(
                ticket_id=ticket_id,
                username=user,
                note=note.strip()[:500],
                due_at=(utcnow() + timedelta(days=days)).replace(tzinfo=None),
            )
        )
        session.commit()
    logger.info("Seguimiento en ticket #%s para %s (+%s días)", ticket_id, user, days)
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


@router.post("/my/followups/{followup_id}/done")
def complete_followup(request: Request, followup_id: int):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    with get_session() as session:
        followup = session.get(FollowUp, followup_id)
        if followup is not None and followup.username == user and followup.done_at is None:
            followup.done_at = utcnow().replace(tzinfo=None)
            session.add(followup)
            session.commit()
    return RedirectResponse("/my", status_code=303)


@router.post("/my/estado")
def set_estado(request: Request, estado: str = Form(...)):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if estado not in ESTADOS:
        return RedirectResponse("/my", status_code=303)

    with get_session() as session:
        me = session.exec(select(User).where(User.username == user)).first()
        if me is not None:
            me.estado = estado
            session.add(me)
            session.commit()
    # Regresar a donde estaba (el selector vive en el sidebar de todas las páginas)
    referer = request.headers.get("referer", "/my")
    return RedirectResponse(referer, status_code=303)
