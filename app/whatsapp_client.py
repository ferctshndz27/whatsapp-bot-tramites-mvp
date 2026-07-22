import logging
import time

import httpx

from app.config import settings
from app.utils import mask_phone

logger = logging.getLogger(__name__)

GRAPH_API_VERSION = "v20.0"
MAX_RETRIES = 3
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
REQUEST_TIMEOUT_SECONDS = 10


def send_text(to_phone: str, body: str) -> str | None:
    if not settings.whatsapp_token or not settings.whatsapp_phone_number_id:
        return None

    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{settings.whatsapp_phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": body},
    }
    headers = {"Authorization": f"Bearer {settings.whatsapp_token}"}

    delay = 1.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = httpx.post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            logger.info("WhatsApp enviado a %s (intento %s)", mask_phone(to_phone), attempt)
            return response.json().get("messages", [{}])[0].get("id")
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status not in RETRYABLE_STATUS or attempt == MAX_RETRIES:
                # El cuerpo trae el código de error de Meta (p.ej. 131047 =
                # fuera de la ventana de 24h) — sin él no se puede diagnosticar.
                logger.error(
                    "Fallo al enviar WhatsApp a %s (status=%s): %s",
                    mask_phone(to_phone), status, exc.response.text[:500],
                )
                return None
        except httpx.RequestError as exc:
            if attempt == MAX_RETRIES:
                logger.error("Fallo de red al enviar WhatsApp a %s: %s", mask_phone(to_phone), exc)
                return None
        time.sleep(delay)
        delay *= 2
    return None


def send_media(
    to_phone: str, file_path: str, mime_type: str, caption: str = "", filename: str | None = None
) -> str | None:
    """Sube un archivo a Meta y lo envía como imagen o documento. Devuelve el wamid."""
    if not settings.whatsapp_token or not settings.whatsapp_phone_number_id:
        return None

    base = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{settings.whatsapp_phone_number_id}"
    headers = {"Authorization": f"Bearer {settings.whatsapp_token}"}

    # ponytail: sin reintentos para media (el asesor ve el error y reintenta a mano);
    # añadir backoff como en send_text si los fallos de red se vuelven frecuentes.
    try:
        with open(file_path, "rb") as fh:
            upload = httpx.post(
                f"{base}/media",
                data={"messaging_product": "whatsapp"},
                files={"file": (filename or "archivo", fh, mime_type)},
                headers=headers,
                timeout=30,
            )
        upload.raise_for_status()
        media_id = upload.json()["id"]

        kind = "image" if mime_type.startswith("image/") else "document"
        media_obj: dict = {"id": media_id}
        if caption:
            media_obj["caption"] = caption
        if kind == "document" and filename:
            media_obj["filename"] = filename

        response = httpx.post(
            f"{base}/messages",
            json={"messaging_product": "whatsapp", "to": to_phone, "type": kind, kind: media_obj},
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        logger.info("Media %s enviado a %s", kind, mask_phone(to_phone))
        return response.json().get("messages", [{}])[0].get("id")
    except (httpx.HTTPError, OSError, KeyError) as exc:
        logger.error("Fallo al enviar media a %s: %s", mask_phone(to_phone), exc)
        return None
