"""Adaptador del canal «web»: el WhatsApp Web de un colaborador (agente de ext-cse).

No hay nada que llamar. Las respuestas de este canal las pide la extensión a
`POST /crm/api/agente/entrante` y ella misma las escribe en el chat, con su pausa. Este módulo
existe para que `messaging` tenga a quién entregarle un envío del servidor (un recordatorio, un
aviso de cierre) a un cliente cuya conversación está en este canal: no sale por Meta ni por
Twilio hacia un número que nunca le escribió a esas cuentas, y devuelve None —«no se envió»—,
que es lo que ya entienden quienes llaman.
"""

import logging

logger = logging.getLogger(__name__)


def send_text(to_phone: str, body: str) -> str | None:
    logger.info("Canal web: envío del servidor omitido (lo escribe la extensión)")
    return None


def send_list(to_phone: str, body: str, button: str, rows: list) -> str | None:
    return send_text(to_phone, body)


def send_buttons(to_phone: str, body: str, buttons: list, content_sid: str | None = None) -> str | None:
    return send_text(to_phone, body)


def send_template(to_phone: str, template_name: str | None, content_sid: str | None = None,
                  variables: list | None = None) -> str | None:
    return send_text(to_phone, template_name or "")


def send_media(to_phone: str, data: bytes, mime_type: str, caption: str | None = None,
               filename: str | None = None) -> str | None:
    return send_text(to_phone, caption or "")
