import hashlib
import hmac
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Response
from sqlmodel import select

from app import whatsapp_client
from app.config import settings
from app.conversation import handle_incoming
from app.db import get_session
from app.models import Message

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/webhook")
def verify_webhook(request: Request) -> Response:
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge", "")

    if mode == "subscribe" and token == settings.verify_token:
        return Response(content=challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="Verificación fallida")


@router.post("/webhook")
async def receive_webhook(request: Request) -> dict:
    raw_body = await request.body()
    if not _valid_signature(raw_body, request.headers.get("x-hub-signature-256", "")):
        raise HTTPException(status_code=403, detail="Firma inválida")

    payload = await request.json()
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                try:
                    _process_message(message)
                except Exception:
                    logger.exception("Error procesando mensaje de WhatsApp id=%s", message.get("id"))
            for status in value.get("statuses", []):
                try:
                    _process_status(status)
                except Exception:
                    logger.exception("Error procesando estado de WhatsApp id=%s", status.get("id"))

    return {"status": "ok"}


def _valid_signature(raw_body: bytes, signature_header: str) -> bool:
    if not settings.app_secret or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(settings.app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    received = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, received)


def _process_message(message: dict) -> None:
    phone = message.get("from")
    if not phone:
        return

    msg_type = message.get("type")
    is_media = msg_type != "text"
    text = message.get("text", {}).get("body", "") if not is_media else ""

    with get_session() as session:
        replies = handle_incoming(
            session, phone, text, is_media=is_media, wa_message_id=message.get("id")
        )

    for reply in replies:
        wa_message_id = whatsapp_client.send_text(phone, reply)
        if wa_message_id:
            _tag_sent_message(phone, wa_message_id)


def _tag_sent_message(phone: str, wa_message_id: str) -> None:
    # ponytail: matches the oldest untagged OUT row for this phone (FIFO with the
    # order replies were logged/sent). Fine for single-request sends; revisit if
    # concurrent sends per phone become a thing.
    with get_session() as session:
        row = session.exec(
            select(Message)
            .where(Message.phone == phone)
            .where(Message.wa_message_id.is_(None))
            .order_by(Message.id)
        ).first()
        if row:
            row.wa_message_id = wa_message_id
            session.add(row)
            session.commit()


def _process_status(status: dict) -> None:
    wa_message_id = status.get("id")
    state = status.get("status")
    if not wa_message_id or not state:
        return

    timestamp = status.get("timestamp")
    status_at = (
        datetime.fromtimestamp(int(timestamp), tz=timezone.utc) if timestamp else datetime.now(timezone.utc)
    )

    with get_session() as session:
        row = session.exec(select(Message).where(Message.wa_message_id == wa_message_id)).first()
        if not row:
            return
        row.wa_status = state
        row.wa_status_at = status_at
        session.add(row)
        session.commit()
