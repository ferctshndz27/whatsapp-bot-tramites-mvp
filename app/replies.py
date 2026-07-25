"""Respuestas guardadas personales de cada colaborador.

Como las respuestas rápidas de WhatsApp Business: cada quien guarda las
suyas desde el panel y aparecen en el composer de la bandeja, junto a las
respuestas rápidas globales del negocio.
"""

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from app.templating import templates
from sqlmodel import select

from app.auth import is_admin
from app.db import get_session
from app.models import SavedReply

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_REPLIES = 30


def _current_user(request: Request) -> str | None:
    return request.session.get("user")


def personal_replies(session, username: str) -> list[SavedReply]:
    return session.exec(
        select(SavedReply)
        .where(SavedReply.username == username)
        .where(SavedReply.habilitado == True)  # noqa: E712
        .order_by(SavedReply.title)
    ).all()


def _render(request: Request, user: str, error: str | None = None):
    with get_session() as session:
        replies = personal_replies(session, user)
    return templates.TemplateResponse(
        "replies.html",
        {"request": request, "user": user, "replies": replies, "error": error},
    )


@router.get("/replies")
def replies_list(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return _render(request, user)


@router.post("/replies")
def replies_create(request: Request, title: str = Form(...), body: str = Form(...)):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    title, body = title.strip()[:100], body.strip()[:1000]
    if not title or not body:
        return _render(request, user, error="Título y texto son obligatorios.")

    with get_session() as session:
        if len(personal_replies(session, user)) >= MAX_REPLIES:
            return _render(
                request, user, error=f"Máximo {MAX_REPLIES} respuestas guardadas — elimina alguna."
            )
        session.add(SavedReply(username=user, title=title, body=body))
        session.commit()
    logger.info("Respuesta guardada '%s' creada por %s", title, user)
    return RedirectResponse("/replies", status_code=303)


@router.post("/replies/{reply_id}/delete")
def replies_delete(request: Request, reply_id: int):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    with get_session() as session:
        reply = session.get(SavedReply, reply_id)
        if reply is not None and reply.username == user:
            reply.habilitado = False
            session.add(reply)
            session.commit()
    return RedirectResponse("/replies", status_code=303)
