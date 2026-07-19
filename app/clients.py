"""Ficha del cliente: historial y memoria compartida del equipo.

Todo lo que un colaborador necesita antes de responder: trámites anteriores,
notas internas de otros compañeros ("ya pagó anticipo", "prefiere llamadas")
y acceso directo al chat. La memoria deja de vivir en la cabeza de cada quien.
"""

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from app.auth import is_admin
from app.db import get_session
from app.models import Client, ClientNote, Ticket

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.globals["is_admin"] = is_admin


def _current_user(request: Request) -> str | None:
    return request.session.get("user")


@router.get("/clients/{phone}")
def client_profile(request: Request, phone: str):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    with get_session() as session:
        client = session.get(Client, phone)
        if client is None:
            return RedirectResponse("/inbox", status_code=303)
        tickets = session.exec(
            select(Ticket).where(Ticket.phone == phone).order_by(Ticket.created_at.desc())
        ).all()
        notes = session.exec(
            select(ClientNote)
            .where(ClientNote.phone == phone)
            .where(ClientNote.habilitado == True)  # noqa: E712
            .order_by(ClientNote.created_at.desc())
        ).all()

    return templates.TemplateResponse(
        "client.html",
        {
            "request": request,
            "user": user,
            "client": client,
            "tickets": tickets,
            "notes": notes,
        },
    )


@router.post("/clients/{phone}/notes")
def add_client_note(request: Request, phone: str, body: str = Form(...)):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    cleaned = body.strip()
    with get_session() as session:
        if session.get(Client, phone) is None:
            return RedirectResponse("/inbox", status_code=303)
        if cleaned:
            session.add(ClientNote(phone=phone, author=user, body=cleaned[:2000]))
            session.commit()
            logger.info("Nota de cliente agregada a %s por %s", phone, user)
    return RedirectResponse(f"/clients/{phone}", status_code=303)


@router.post("/clients/{phone}/notes/{note_id}/delete")
def delete_client_note(request: Request, phone: str, note_id: int):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    with get_session() as session:
        note = session.get(ClientNote, note_id)
        # Soft-delete: solo el autor o un admin pueden ocultar una nota.
        if note is not None and note.phone == phone and (note.author == user or is_admin(user)):
            note.habilitado = False
            session.add(note)
            session.commit()
    return RedirectResponse(f"/clients/{phone}", status_code=303)
