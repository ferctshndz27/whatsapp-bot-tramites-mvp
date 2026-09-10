import logging
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Form, Request, UploadFile, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse, Response
from app.templating import templates
from sqlalchemy import func
from sqlmodel import select, or_, and_

from app.auth import is_admin, alcance
from app.db import get_session
from app.media_store import save_upload
from app.models import (
    TeamMessage, User, ChatRoom, ChatMember,
    MessageReaction, MessageReminder
)

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_MESSAGES_SHOWN = 100
MAX_BODY_LENGTH = 4000

def _current_user(request: Request) -> str | None:
    return request.session.get("user")

@router.get("/team")
def team_chat(request: Request, err: str | None = None):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    vista = alcance(user)
    with get_session() as session:
        # Check if user has a general office room
        general_room = session.exec(
            select(ChatRoom).where(
                ChatRoom.office_id == vista.office_id,
                ChatRoom.room_type == "general",
                ChatRoom.habilitado == True
            )
        ).first()

        if not general_room:
            # Create a general room for the office
            general_room = ChatRoom(
                name="Oficina General",
                room_type="general",
                office_id=vista.office_id
            )
            session.add(general_room)
            session.commit()
            
            # Map old messages with no room_id to this general room
            old_msgs = session.exec(
                select(TeamMessage).where(TeamMessage.room_id == None, TeamMessage.office_id == vista.office_id)
            ).all()
            for msg in old_msgs:
                msg.room_id = general_room.id
                session.add(msg)
            session.commit()

        # Ensure user is member of general room
        if not session.exec(select(ChatMember).where(ChatMember.room_id == general_room.id, ChatMember.username == user)).first():
            session.add(ChatMember(room_id=general_room.id, username=user))
            session.commit()

        # Get all rooms the user is in
        my_memberships = session.exec(select(ChatMember).where(ChatMember.username == user, ChatMember.habilitado == True)).all()
        room_ids = [m.room_id for m in my_memberships]
        
        rooms = []
        if room_ids:
            rooms = session.exec(select(ChatRoom).where(ChatRoom.id.in_(room_ids), ChatRoom.habilitado == True)).all()
            
        # Get users in the same office for DM/Group creation
        users_query = select(User).where(User.habilitado == True, User.username != user)
        if not vista.ve_todas:
            # Note: User model does not have office_id directly, but typically they are filtered or we assume they belong to the office.
            # We will list all enabled users, but ideally this would be office scoped.
            pass
        office_users = session.exec(users_query).all()

    return templates.TemplateResponse(
        "team_chat.html",
        {
            "request": request,
            "user": user,
            "rooms": rooms,
            "office_users": office_users,
            "error": err,
            "general_room_id": general_room.id
        },
    )

@router.get("/team/rooms/{room_id}/messages")
def team_room_messages(request: Request, room_id: int):
    user = _current_user(request)
    if not user:
        return Response(status_code=403)
        
    with get_session() as session:
        # Check membership
        membership = session.exec(select(ChatMember).where(ChatMember.room_id == room_id, ChatMember.username == user, ChatMember.habilitado == True)).first()
        room = session.get(ChatRoom, room_id)
        if not membership or not room or not room.habilitado:
            return HTMLResponse("<div class='alert'>No tienes acceso a esta sala.</div>")

        recent = session.exec(
            select(TeamMessage)
            .where(TeamMessage.room_id == room_id, TeamMessage.habilitado == True)
            .order_by(TeamMessage.created_at.desc())
            .limit(MAX_MESSAGES_SHOWN)
        ).all()
        
        messages = list(reversed(recent))
        
        # Get reactions
        msg_ids = [m.id for m in messages]
        reactions = {}
        if msg_ids:
            rxns = session.exec(select(MessageReaction).where(MessageReaction.message_id.in_(msg_ids))).all()
            for rx in rxns:
                reactions.setdefault(rx.message_id, []).append(rx)

    return templates.TemplateResponse(
        "partials/team_messages.html",
        {
            "request": request,
            "user": user,
            "room": room,
            "messages": messages,
            "reactions": reactions,
            "is_muted": membership.is_muted
        },
    )

@router.post("/team/rooms")
def create_room(request: Request, room_type: str = Form(...), name: str = Form(""), members: List[str] = Form([])):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    vista = alcance(user)
    with get_session() as session:
        room = ChatRoom(room_type=room_type, name=name, office_id=vista.office_id)
        session.add(room)
        session.commit()
        session.refresh(room)
        
        # Add creator
        session.add(ChatMember(room_id=room.id, username=user))
        # Add other members
        for m in members:
            if m != user:
                session.add(ChatMember(room_id=room.id, username=m))
        session.commit()

    return RedirectResponse(f"/team", status_code=303)

@router.post("/team/rooms/{room_id}/send")
def team_send(request: Request, room_id: int, body: str = Form(...)):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    body = body.strip()
    if body:
        vista = alcance(user)
        with get_session() as session:
            # Check membership
            if session.exec(select(ChatMember).where(ChatMember.room_id == room_id, ChatMember.username == user, ChatMember.habilitado == True)).first():
                session.add(TeamMessage(room_id=room_id, author=user, body=body[:MAX_BODY_LENGTH], office_id=vista.office_id))
                session.commit()
    # If HTMX request, we can just return the updated message list or 200 OK to trigger reload
    if "hx-request" in request.headers:
        return team_room_messages(request, room_id)
    return RedirectResponse("/team", status_code=303)

@router.post("/team/rooms/{room_id}/send-media")
def team_send_media(request: Request, room_id: int, file: UploadFile, caption: str = Form("")):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    web_path, _disk_path, error = save_upload(file)
    if error:
        return RedirectResponse(f"/team?err={error}", status_code=303)

    vista = alcance(user)
    with get_session() as session:
        if session.exec(select(ChatMember).where(ChatMember.room_id == room_id, ChatMember.username == user, ChatMember.habilitado == True)).first():
            session.add(
                TeamMessage(
                    room_id=room_id,
                    author=user,
                    body=caption.strip()[:MAX_BODY_LENGTH] or f"[archivo: {file.filename}]",
                    media_path=web_path,
                    office_id=vista.office_id,
                )
            )
            session.commit()
    return RedirectResponse("/team", status_code=303)

@router.post("/team/rooms/{room_id}/mute")
def team_mute_room(request: Request, room_id: int, mute: bool = Form(...)):
    user = _current_user(request)
    if not user: return Response(status_code=403)

    with get_session() as session:
        membership = session.exec(select(ChatMember).where(ChatMember.room_id == room_id, ChatMember.username == user)).first()
        if membership:
            membership.is_muted = mute
            session.add(membership)
            session.commit()
    if "hx-request" in request.headers:
        return team_room_messages(request, room_id)
    return RedirectResponse("/team", status_code=303)

@router.post("/team/messages/{message_id}/react")
def team_react(request: Request, message_id: int, reaction: str = Form(...)):
    user = _current_user(request)
    if not user: return Response(status_code=403)

    with get_session() as session:
        msg = session.get(TeamMessage, message_id)
        if msg:
            # Check if user has access to room
            if session.exec(select(ChatMember).where(ChatMember.room_id == msg.room_id, ChatMember.username == user)).first():
                # Toggle reaction
                existing = session.exec(select(MessageReaction).where(MessageReaction.message_id == message_id, MessageReaction.username == user, MessageReaction.reaction == reaction)).first()
                if existing:
                    session.delete(existing)
                else:
                    session.add(MessageReaction(message_id=message_id, username=user, reaction=reaction))
                session.commit()
    
    if "hx-request" in request.headers:
        return team_room_messages(request, msg.room_id)
    return RedirectResponse("/team", status_code=303)

@router.post("/team/messages/{message_id}/remind")
def team_remind(request: Request, message_id: int, remind_in_minutes: int = Form(...)):
    user = _current_user(request)
    if not user: return Response(status_code=403)
    
    import datetime
    remind_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=remind_in_minutes)
    remind_at = remind_at.replace(tzinfo=None)

    with get_session() as session:
        session.add(MessageReminder(message_id=message_id, username=user, remind_at=remind_at, note=""))
        session.commit()
        
    return JSONResponse({"status": "ok"})

@router.get("/team/reminders/active")
def active_reminders(request: Request):
    user = _current_user(request)
    if not user: return Response(status_code=403)

    with get_session() as session:
        now = datetime.utcnow()
        reminders = session.exec(
            select(MessageReminder, TeamMessage)
            .join(TeamMessage, MessageReminder.message_id == TeamMessage.id)
            .where(MessageReminder.username == user, MessageReminder.is_dismissed == False, MessageReminder.remind_at <= now)
        ).all()
        
        results = []
        for rem, msg in reminders:
            results.append({
                "id": rem.id,
                "body": msg.body,
                "author": msg.author,
                "room_id": msg.room_id
            })
            rem.is_dismissed = True
            session.add(rem)
        session.commit()
        
    return JSONResponse(results)

@router.post("/team/messages/{message_id}/delete")
def team_delete_message(request: Request, message_id: int):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    with get_session() as session:
        message = session.get(TeamMessage, message_id)
        if message is not None and message.author == user:
            message.habilitado = False
            session.add(message)
            session.commit()
            logger.info("Mensaje de equipo #%s ocultado por %s", message_id, user)
    
    if "hx-request" in request.headers and message:
        return team_room_messages(request, message.room_id)
    return RedirectResponse("/team", status_code=303)
