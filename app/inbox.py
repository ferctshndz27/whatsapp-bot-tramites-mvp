import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from app import whatsapp_client
from app.auth import is_admin
from app.config import settings
from app.conversation import _log_message
from app.db import get_session
from app.media_store import MEDIA_NAME_RE, save_upload
from app.models import Client, Conversation, Direction, Message, Ticket

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.globals["is_admin"] = is_admin

WINDOW_HOURS = 24
QUICK_REPLIES = json.loads((Path(__file__).parent / "quick_replies.json").read_text())


def _current_user(request: Request) -> str | None:
    return request.session.get("user")


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _time_ago(dt: datetime) -> str:
    seconds = (datetime.now(timezone.utc) - _as_utc(dt)).total_seconds()
    if seconds < 60:
        return "ahora"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _window_open(last_in_at: datetime | None) -> bool:
    if last_in_at is None:
        return False
    return datetime.now(timezone.utc) - _as_utc(last_in_at) < timedelta(hours=WINDOW_HOURS)


def build_conversation_items(session) -> list[dict]:
    # ponytail: carga todos los mensajes y agrupa en Python — sobra a este
    # volumen; pasar a una consulta agregada si el historial crece a decenas de miles.
    messages = session.exec(
        select(Message).where(Message.habilitado.is_(True)).order_by(Message.created_at.desc())
    ).all()
    clients = {c.phone: c for c in session.exec(select(Client)).all()}
    convs = {c.phone: c for c in session.exec(select(Conversation)).all()}
    tickets: dict[str, Ticket] = {}
    for t in session.exec(select(Ticket).order_by(Ticket.created_at.desc())).all():
        tickets.setdefault(t.phone, t)

    items: dict[str, dict] = {}
    last_in: dict[str, datetime] = {}
    for m in messages:
        if m.phone not in items:
            items[m.phone] = {
                "phone": m.phone,
                "name": (clients.get(m.phone).name if clients.get(m.phone) else None),
                "last_body": m.body,
                "last_direction": m.direction.value,
                "last_at": m.created_at,
                "ago": _time_ago(m.created_at),
            }
        if m.phone not in last_in and m.direction == Direction.IN:
            last_in[m.phone] = m.created_at

    result = []
    for phone, item in items.items():
        conv = convs.get(phone)
        ticket = tickets.get(phone)
        item["waiting"] = item["last_direction"] == "in"
        item["window_open"] = _window_open(last_in.get(phone))
        item["bot_paused"] = bool(conv and conv.state == "escalado")
        item["assigned_to"] = ticket.assigned_to if ticket else None
        item["ticket_id"] = ticket.id if ticket else None
        result.append(item)
    return result


@router.get("/inbox")
def inbox_list(request: Request, filter: str | None = None):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    with get_session() as session:
        items = build_conversation_items(session)

    counts = {
        "todas": len(items),
        "esperando": sum(1 for i in items if i["waiting"]),
        "caducadas": sum(1 for i in items if not i["window_open"]),
        "pausadas": sum(1 for i in items if i["bot_paused"]),
    }
    if filter == "esperando":
        items = [i for i in items if i["waiting"]]
    elif filter == "caducadas":
        items = [i for i in items if not i["window_open"]]
    elif filter == "pausadas":
        items = [i for i in items if i["bot_paused"]]

    return templates.TemplateResponse(
        "inbox_list.html",
        {
            "request": request,
            "user": user,
            "items": items,
            "counts": counts,
            "current_filter": filter,
        },
    )


@router.get("/inbox/{phone}")
def inbox_chat(request: Request, phone: str, err: str | None = None):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    with get_session() as session:
        messages = session.exec(
            select(Message)
            .where(Message.phone == phone)
            .where(Message.habilitado.is_(True))
            .order_by(Message.created_at)
        ).all()
        if not messages:
            return RedirectResponse("/inbox", status_code=303)
        client = session.get(Client, phone)
        conv = session.get(Conversation, phone)
        ticket = session.exec(
            select(Ticket).where(Ticket.phone == phone).order_by(Ticket.created_at.desc())
        ).first()

    last_in = next(
        (m.created_at for m in reversed(messages) if m.direction == Direction.IN), None
    )
    return templates.TemplateResponse(
        "inbox_chat.html",
        {
            "request": request,
            "user": user,
            "phone": phone,
            "client": client,
            "messages": messages,
            "ticket": ticket,
            "bot_paused": bool(conv and conv.state == "escalado"),
            "window_open": _window_open(last_in),
            "quick_replies": QUICK_REPLIES,
            "error": err,
        },
    )


def _pause_bot(session, phone: str) -> None:
    conv = session.get(Conversation, phone)
    if conv and conv.state != "escalado":
        conv.state = "escalado"
        session.add(conv)
        session.commit()


@router.post("/inbox/{phone}/send")
def inbox_send(request: Request, phone: str, body: str = Form(...)):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    body = body.strip()
    if not body:
        return RedirectResponse(f"/inbox/{phone}", status_code=303)

    with get_session() as session:
        last_in = session.exec(
            select(Message)
            .where(Message.phone == phone, Message.direction == Direction.IN)
            .order_by(Message.created_at.desc())
        ).first()
        if not _window_open(last_in.created_at if last_in else None):
            return RedirectResponse(f"/inbox/{phone}?err=window", status_code=303)

        wamid = whatsapp_client.send_text(phone, body)
        session.add(
            Message(
                phone=phone,
                direction=Direction.OUT,
                body=body,
                wa_message_id=wamid,
                sent_by=user,
            )
        )
        session.commit()
        _pause_bot(session, phone)

    if wamid is None:
        logger.warning("Mensaje a %s registrado pero sin confirmación de envío", phone)
    return RedirectResponse(f"/inbox/{phone}", status_code=303)


@router.post("/inbox/{phone}/send-media")
def inbox_send_media(request: Request, phone: str, file: UploadFile, caption: str = Form("")):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    with get_session() as session:
        last_in = session.exec(
            select(Message)
            .where(Message.phone == phone, Message.direction == Direction.IN)
            .order_by(Message.created_at.desc())
        ).first()
        if not _window_open(last_in.created_at if last_in else None):
            return RedirectResponse(f"/inbox/{phone}?err=window", status_code=303)

        web_path, disk_path, error = save_upload(file)
        if error:
            return RedirectResponse(f"/inbox/{phone}?err={error}", status_code=303)

        wamid = whatsapp_client.send_media(
            phone, disk_path, file.content_type, caption, file.filename
        )
        kind = "imagen" if file.content_type.startswith("image/") else "documento"
        session.add(
            Message(
                phone=phone,
                direction=Direction.OUT,
                body=caption or f"[{kind}: {file.filename}]",
                wa_message_id=wamid,
                media_path=web_path,
                sent_by=user,
            )
        )
        session.commit()
        _pause_bot(session, phone)

    return RedirectResponse(f"/inbox/{phone}", status_code=303)


@router.post("/inbox/{phone}/messages/{message_id}/delete")
def inbox_delete_message(request: Request, phone: str, message_id: int):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    with get_session() as session:
        message = session.get(Message, message_id)
        # Solo se puede ocultar un mensaje que el propio asesor envió desde el
        # panel — nunca las palabras del cliente, que son evidencia de la
        # conversación real y deben permanecer intactas.
        if message is not None and message.phone == phone and message.sent_by == user:
            message.habilitado = False
            session.add(message)
            session.commit()
            logger.info("Mensaje #%s ocultado por %s", message_id, user)
    return RedirectResponse(f"/inbox/{phone}", status_code=303)


@router.post("/inbox/{phone}/block")
def inbox_toggle_block(request: Request, phone: str):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not is_admin(user):
        return RedirectResponse(f"/inbox/{phone}", status_code=303)

    with get_session() as session:
        client = session.get(Client, phone)
        if client is not None:
            client.habilitado = not client.habilitado
            session.add(client)
            session.commit()
            logger.info(
                "Cliente %s %s por %s", phone,
                "desbloqueado" if client.habilitado else "bloqueado", user,
            )
    return RedirectResponse(f"/inbox/{phone}", status_code=303)


@router.post("/inbox/{phone}/bot-on")
def inbox_bot_on(request: Request, phone: str):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    with get_session() as session:
        conv = session.get(Conversation, phone)
        if conv and conv.state == "escalado":
            conv.state = "inicio"
            session.add(conv)
            session.commit()
            logger.info("Bot reactivado para %s por %s", phone, user)
    return RedirectResponse(f"/inbox/{phone}", status_code=303)


@router.get("/media/{name}")
def media_file(request: Request, name: str):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not MEDIA_NAME_RE.match(name):
        return RedirectResponse("/inbox", status_code=303)
    path = Path(settings.media_dir) / name
    if not path.is_file():
        return RedirectResponse("/inbox", status_code=303)
    return FileResponse(path)
