import logging

from fastapi import APIRouter, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from app.templating import templates
from sqlmodel import select

from app.auth import is_admin, alcance
from app.db import get_session
from app.media_store import save_upload
from app.models import TeamMessage, User

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_MESSAGES_SHOWN = 200
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
        # ponytail: últimos N y reversa en memoria; paginar si el canal crece mucho.
        query = select(TeamMessage).where(TeamMessage.habilitado.is_(True))
        if not vista.ve_todas:
            query = query.where(TeamMessage.office_id == vista.office_id)
        
        recent = session.exec(
            query.order_by(TeamMessage.created_at.desc())
            .limit(MAX_MESSAGES_SHOWN)
        ).all()

    return templates.TemplateResponse(
        "team_chat.html",
        {
            "request": request,
            "user": user,
            "messages": list(reversed(recent)),
            "error": err,
        },
    )


@router.post("/team/send")
def team_send(request: Request, body: str = Form(...)):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    body = body.strip()
    if body:
        vista = alcance(user)
        with get_session() as session:
            session.add(TeamMessage(author=user, body=body[:MAX_BODY_LENGTH], office_id=vista.office_id))
            session.commit()
    return RedirectResponse("/team", status_code=303)


@router.post("/team/send-media")
def team_send_media(request: Request, file: UploadFile, caption: str = Form("")):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    web_path, _disk_path, error = save_upload(file)
    if error:
        return RedirectResponse(f"/team?err={error}", status_code=303)

    vista = alcance(user)
    with get_session() as session:
        session.add(
            TeamMessage(
                author=user,
                body=caption.strip()[:MAX_BODY_LENGTH] or f"[archivo: {file.filename}]",
                media_path=web_path,
                office_id=vista.office_id,
            )
        )
        session.commit()
    return RedirectResponse("/team", status_code=303)


@router.post("/team/{message_id}/delete")
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
    return RedirectResponse("/team", status_code=303)
