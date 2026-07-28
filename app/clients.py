"""Ficha del cliente: historial y memoria compartida del equipo.

Todo lo que un colaborador necesita antes de responder: trámites anteriores,
notas internas de otros compañeros ("ya pagó anticipo", "prefiere llamadas")
y acceso directo al chat. La memoria deja de vivir en la cabeza de cada quien.
"""

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from app.templating import templates
from sqlmodel import or_, select

from app.audit import log_admin
from app.auth import is_admin
from app.db import get_session
from app.models import Client, ClientNote, Ticket
from app.utils import normalize_phone

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_NOMBRE = 200
# Tope de la lista: a este volumen sobra y evita renderizar miles de filas.
# ponytail: paginar si el directorio pasa de unos miles de clientes.
LIMITE_LISTA = 300


def _current_user(request: Request) -> str | None:
    return request.session.get("user")


@router.get("/clients")
def clients_list(request: Request, q: str = "", alta: str = "", error: str = ""):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    termino = q.strip()
    with get_session() as session:
        consulta = select(Client).where(Client.habilitado == True)  # noqa: E712
        if termino:
            # Se busca por nombre y por teléfono. El teléfono se normaliza para
            # que buscar "+52 155 0000 0000" encuentre al cliente guardado como
            # "525500000000": si no, el asesor concluye que no existe y lo da de
            # alta otra vez.
            patron = f"%{termino}%"
            canonico = normalize_phone(termino)
            condiciones = [Client.name.ilike(patron), Client.phone.like(patron)]
            if canonico:
                condiciones.append(Client.phone == canonico)
            consulta = consulta.where(or_(*condiciones))
        clientes = session.exec(
            consulta.order_by(Client.created_at.desc()).limit(LIMITE_LISTA)
        ).all()

    return templates.TemplateResponse(
        "clients_list.html",
        {
            "request": request,
            "user": user,
            "clients": clientes,
            "q": termino,
            "alta": alta,
            "error": error,
            "truncada": len(clientes) == LIMITE_LISTA,
        },
    )


@router.post("/clients")
def clients_upsert(request: Request, phone: str = Form(...), name: str = Form("")):
    """Alta manual, o actualización del nombre si el teléfono ya existe.

    Deliberadamente NO toca `habilitado`, `created_at` ni la conversación: dar
    de alta a alguien que ya existe actualiza su nombre y nada más. En
    particular no sirve para desbloquear a un cliente bloqueado ni para
    reiniciar el flujo del bot de alguien que va a media conversación.
    """
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    canonico = normalize_phone(phone)
    nombre = name.strip()[:MAX_NOMBRE]
    if not canonico:
        return RedirectResponse(
            "/clients?error=Teléfono inválido: usa 10 dígitos, o 52 + 10 dígitos.",
            status_code=303,
        )
    if not nombre:
        return RedirectResponse("/clients?error=El nombre es obligatorio.", status_code=303)

    with get_session() as session:
        cliente = session.get(Client, canonico)
        if cliente is None:
            session.add(Client(phone=canonico, name=nombre))
            session.commit()
            accion, resultado = "cliente:alta", "alta"
        elif cliente.name == nombre:
            accion, resultado = None, "sin-cambio"
        else:
            cliente.name = nombre
            session.add(cliente)
            session.commit()
            accion, resultado = "cliente:nombre", "actualizado"

    # Auditado porque a este cliente se le van a cobrar tarjetas: quién le puso
    # ese nombre y cuándo es dato que hace falta el día de una aclaración.
    if accion:
        log_admin(user, accion, f"{canonico} · {nombre}")
        logger.info("Cliente %s (%s) por %s", canonico, resultado, user)
    return RedirectResponse(f"/clients?alta={resultado}&q={canonico}", status_code=303)


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
