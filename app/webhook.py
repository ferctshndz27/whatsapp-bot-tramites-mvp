import hashlib
import hmac
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Response
from sqlmodel import select

from app import conversation, whatsapp_client
from app.config import settings
from app.conversation import handle_incoming
from app.db import get_session
from app.models import Client, Message
from app.utils import normalize_phone

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
    # Se normaliza AQUÍ, en el borde: el formato crudo de cada proveedor no
    # entra a la base. Sin esto, el mismo humano por Meta y por Twilio serían
    # dos clientes distintos (la PK de `client` es el teléfono).
    phone = normalize_phone(message.get("from"))
    if not phone:
        return

    with get_session() as session:
        client = session.get(Client, phone)
        if client is not None and not client.habilitado:
            logger.info("Mensaje de %s ignorado: cliente bloqueado", phone)
            return

    text, is_media = _extract_text(message)

    with get_session() as session:
        replies = handle_incoming(
            session, phone, text, is_media=is_media, wa_message_id=message.get("id")
        )
        outbound = conversation.presentation(session, phone, replies)

    for item in outbound:
        wa_message_id = _deliver(phone, item)
        if wa_message_id:
            _tag_sent_message(phone, wa_message_id)


def _extract_text(message: dict) -> tuple[str, bool]:
    """(texto, is_media). Una respuesta interactiva (lista/botón) llega como
    su id — el mismo texto que el cliente habría tecleado ("1", "si")."""
    msg_type = message.get("type")
    if msg_type == "text":
        return message.get("text", {}).get("body", ""), False
    if msg_type == "interactive":
        inner = message.get("interactive", {})
        reply = inner.get("list_reply") or inner.get("button_reply") or {}
        return reply.get("id", ""), False
    return "", True


def _deliver(phone: str, item) -> str | None:
    """Envía texto plano o interactivo; si el interactivo falla, cae al texto
    (el cliente nunca se queda sin respuesta por un error de formato)."""
    if isinstance(item, str):
        return whatsapp_client.send_text(phone, item)
    if "buttons" in item:
        wamid = whatsapp_client.send_buttons(phone, item["text"], item["buttons"])
    else:
        wamid = whatsapp_client.send_list(
            phone, item["text"], item.get("button", "Ver opciones"), item["list"]
        )
    return wamid or whatsapp_client.send_text(phone, item["text"])


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
