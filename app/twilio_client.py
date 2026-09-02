"""Adaptador de la segunda cuenta de WhatsApp (Twilio).

Hermano de app/whatsapp_client.py: misma superficie de funciones, otro
proveedor. Nadie lo llama directo — se llega por app/messaging.py, que decide
por cuál de las dos cuentas contestarle a cada cliente (ver el candado en
tests/test_messaging_port.py).

Tres diferencias con Meta que este módulo absorbe para que nadie más las vea:

1. TELÉFONOS. Twilio usa "whatsapp:+5215500000000"; la base guarda
   "525500000000". La conversión vive aquí y solo aquí.
2. FIRMA. Meta manda HMAC-SHA256 del cuerpo crudo; Twilio manda HMAC-SHA1 en
   base64 sobre la URL COMPLETA más los parámetros POST ordenados. Detrás de un
   proxy, la URL que ve la app no es la que firmó Twilio: por eso se reconstruye
   desde PUBLIC_BASE_URL y no desde request.url.
3. INTERACTIVOS. Twilio no acepta el JSON inline de listas y botones; exige
   plantillas de la Content API. Aquí se degradan a texto numerado, que la
   máquina de estados ya entiende porque el id de cada opción ES el texto que el
   cliente habría tecleado. Los nativos llegan en la Fase 4.
"""

import base64
import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from app.config import settings
from app.credenciales import credencial
from app.media_store import signed_media_path
from app.models import OFICINA_TLAXCALA_ID
from app.utils import mask_phone, normalize_phone

logger = logging.getLogger(__name__)

API_BASE = "https://api.twilio.com/2010-04-01"
REQUEST_TIMEOUT_SECONDS = 10
MAX_RETRIES = 3
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

PREFIJO_WHATSAPP = "whatsapp:"

# Límites de WhatsApp (no de Twilio): son los MISMOS que ya aplica
# whatsapp_client para Meta. Se repiten aquí porque el texto degradado se arma
# en este módulo, y truncar de más o de menos rompería el mensaje.
MAX_OPCIONES = 10
BODY_MAX = 1024

# MessageStatus de Twilio -> el vocabulario que ya guarda message.wa_status
# (el de Meta). Tabla EXPLÍCITA a propósito: adivinar el mapeo dejaría la
# bandeja mostrando estados de entrega que no significan lo que parecen.
#   accepted/queued/sending -> aún no sale del proveedor
#   sent                    -> entregado a WhatsApp
#   delivered/read          -> mismos nombres que Meta
#   failed/undelivered      -> Meta solo tiene "failed"
STATUS_MAP = {
    "accepted": "sent",
    "queued": "sent",
    "sending": "sent",
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "failed": "failed",
    "undelivered": "failed",
}


def enabled() -> bool:
    # ponytail: office_id fijo a Tlaxcala — el PR 4 lo resuelve desde el campo
    # `To` (§4.2) y lo baja por el mismo túnel que ya transporta `provider`
    # (webhook.handle_inbound -> _deliver -> messaging.send_*). La firma de
    # enabled() no cambia: es el candado de tests/test_messaging_port.py.
    return bool(
        credencial("TWILIO_ACCOUNT_SID", office_id=OFICINA_TLAXCALA_ID)
        and credencial("TWILIO_AUTH_TOKEN", office_id=OFICINA_TLAXCALA_ID)
    )


# ---------------------------------------------------------------------------
# Entrada: firma y parseo
# ---------------------------------------------------------------------------


def verify_signature(url: str, params: dict[str, str], signature_header: str) -> bool:
    """Valida X-Twilio-Signature.

    Algoritmo (documentación de Twilio): a la URL completa —protocolo, host,
    puerto y query string— se le concatenan los parámetros POST ordenados
    alfabéticamente, nombre y valor pegados sin separador; se aplica HMAC-SHA1
    con el AuthToken y el resultado va en base64.

    Sin TWILIO_AUTH_TOKEN devuelve False: aceptar sin verificar dejaría que
    cualquiera inyecte mensajes al bot y avance conversaciones ajenas.
    """
    token = credencial("TWILIO_AUTH_TOKEN", office_id=OFICINA_TLAXCALA_ID)
    if not token or not signature_header:
        return False

    firmado = url
    for clave in sorted(params):  # orden Unix, sensible a mayúsculas
        firmado += clave + params[clave]

    esperada = base64.b64encode(
        hmac.new(token.encode(), firmado.encode("utf-8"), hashlib.sha1).digest()
    ).decode()
    return hmac.compare_digest(esperada, signature_header)


def callback_url(path: str) -> str:
    """URL pública exacta que Twilio usó para llamarnos.

    Se arma desde PUBLIC_BASE_URL y no desde request.url porque detrás del
    proxy la app ve el host interno y http://; con eso la firma NUNCA
    coincidiría — y fallarían todas, no algunas.
    """
    return f"{settings.public_base_url}{path}"


def parse_inbound(form: dict[str, str]) -> dict | None:
    """Normaliza el form de Twilio al mismo shape que produce el de Meta.

    Devuelve None si no hay remitente aprovechable: sin teléfono no hay a quién
    atribuirle el mensaje.
    """
    phone = normalize_phone(form.get("From", ""))
    if not phone:
        return None

    try:
        num_media = int(form.get("NumMedia", "0"))
    except ValueError:
        num_media = 0

    # Una respuesta interactiva trae ADEMÁS el texto visible en Body; se prefiere
    # el id (ListId / ButtonPayload) porque es el mismo valor que el cliente
    # habría tecleado, que es lo que espera la máquina de estados.
    texto = form.get("ListId") or form.get("ButtonPayload") or form.get("Body", "")

    return {
        "phone": phone,
        "text": texto,
        "is_media": num_media > 0,
        "message_id": form.get("MessageSid") or None,
        # Twilio lo entrega directo; Meta lo esconde en contacts[].profile.name.
        "profile_name": (form.get("ProfileName") or "").strip() or None,
    }


def map_status(twilio_status: str) -> str | None:
    return STATUS_MAP.get((twilio_status or "").lower())


# ---------------------------------------------------------------------------
# Salida
# ---------------------------------------------------------------------------


def _to_whatsapp(phone: str) -> str:
    """525500000000 -> whatsapp:+525500000000 (el formato que exige Twilio)."""
    return f"{PREFIJO_WHATSAPP}+{phone.lstrip('+')}"


def _post_message(to_phone: str, data: dict) -> str | None:
    """POST a /Messages.json con reintentos. Devuelve el MessageSid o None."""
    sid = credencial("TWILIO_ACCOUNT_SID", office_id=OFICINA_TLAXCALA_ID)
    token = credencial("TWILIO_AUTH_TOKEN", office_id=OFICINA_TLAXCALA_ID)
    desde = credencial("TWILIO_WHATSAPP_FROM", office_id=OFICINA_TLAXCALA_ID)
    # La guarda comprueba los valores YA resueltos en vez de volver a llamar a
    # enabled(), que resolvía OTRA VEZ esas mismas dos llaves: 5 SELECT por
    # saliente en vez de 3, y —lo caro— validando una resolución distinta de la
    # que después se usa para firmar el POST. Es exactamente la partición entre
    # «lo que se valida» y «lo que se envía» que este PR declaró peligrosa y
    # quitó en stripe_client. El fail-closed no cambia: enabled() es
    # `bool(sid and token)`, así que `not sid or not token` es la misma guarda.
    if not sid or not token or not desde:
        return None

    url = f"{API_BASE}/Accounts/{quote(sid)}/Messages.json"
    auth = (sid, token)
    payload = {"From": desde, "To": _to_whatsapp(to_phone), **data}

    delay = 1.0
    for intento in range(1, MAX_RETRIES + 1):
        try:
            respuesta = httpx.post(url, data=payload, auth=auth, timeout=REQUEST_TIMEOUT_SECONDS)
            respuesta.raise_for_status()
            logger.info("Twilio enviado a %s (intento %s)", mask_phone(to_phone), intento)
            return respuesta.json().get("sid")
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status not in RETRYABLE_STATUS or intento == MAX_RETRIES:
                # El cuerpo trae el código de Twilio (p.ej. 63016 = fuera de la
                # ventana de 24 h); sin él no se puede diagnosticar.
                logger.error(
                    "Fallo al enviar por Twilio a %s (status=%s): %s",
                    mask_phone(to_phone),
                    status,
                    exc.response.text[:500],
                )
                return None
        except httpx.RequestError as exc:
            if intento == MAX_RETRIES:
                logger.error("Fallo de red de Twilio a %s: %s", mask_phone(to_phone), exc)
                return None
        time.sleep(delay)
        delay *= 2
    return None


def send_text(to_phone: str, body: str) -> str | None:
    return _post_message(to_phone, {"Body": body[:BODY_MAX]})


def _numerar(body: str, opciones: list[tuple[str, str]]) -> str:
    """Arma el texto degradado de una lista o de unos botones.

    El id de cada opción ES lo que el cliente habría tecleado ("1", "si"), así
    que mostrarlo como prefijo mantiene intacta la máquina de estados: el
    cliente responde con ese mismo valor y `handle_incoming` no distingue si
    vino de un toque o de un tecleo.
    """
    lineas = [f"{opcion_id}) {titulo}" for opcion_id, titulo in opciones[:MAX_OPCIONES]]
    return "\n".join([body, "", *lineas])[:BODY_MAX]


def send_list(to_phone: str, body: str, button: str, rows: list[tuple[str, str]]) -> str | None:
    # ponytail: degradado a texto plano. Las listas nativas de Twilio exigen
    # una plantilla de la Content API (twilio/list-picker); llegan en la Fase 4.
    # SIN _numerar: el body de todo menú ya trae sus opciones numeradas
    # (conversation.presentation solo arma listas de cuerpos con MENU_MARKER),
    # y re-anexar las filas duplicaba el menú completo en el chat. Los botones
    # (send_buttons) sí la necesitan: su body no lista opciones.
    return send_text(to_phone, body)


def send_buttons(
    to_phone: str, body: str, buttons: list[tuple[str, str]], content_sid: str | None = None
) -> str | None:
    """Botones nativos si hay plantilla de la Content API; si no, texto numerado.

    Twilio no manda botones con un POST suelto: exige una plantilla
    `twilio/quick-reply` creada de antemano y se envía por su ContentSid. Los
    títulos viven en la plantilla (no admiten variables); el cuerpo sí, y va
    como {{1}} para que el texto siga saliendo de este código.

    DENTRO de la ventana de 24 h WhatsApp no pide aprobación para esta
    plantilla, con un máximo de 3 botones — por eso sirve para la encuesta de
    cierre, que `dashboard._notify_finish` solo manda con la ventana abierta.

    Si el envío con plantilla falla (SID equivocado, plantilla borrada) se cae
    al texto numerado: vale más una encuesta fea que un cliente sin encuesta.
    """
    if content_sid:
        sid = _post_message(
            to_phone,
            {
                "ContentSid": content_sid,
                "ContentVariables": json.dumps({"1": body[:BODY_MAX]}, ensure_ascii=False),
            },
        )
        if sid:
            return sid
        logger.warning(
            "Botones de Twilio no enviados a %s con la plantilla %s; se manda como texto",
            mask_phone(to_phone),
            content_sid,
        )
    return send_text(to_phone, _numerar(body, buttons))


def send_media(
    to_phone: str,
    file_path: str,
    mime_type: str,
    caption: str = "",
    filename: str | None = None,
) -> str | None:
    """Manda un adjunto vía URL pública firmada.

    A diferencia de Meta —que recibe el binario y devuelve un media id— Twilio
    exige `MediaUrl`: una URL de la que ÉL descarga el archivo. Como MEDIA_DIR
    guarda documentos de clientes, no se publica abierto: se genera una URL
    firmada y de vida corta (ver app/media_store.signed_media_path).

    Devuelve None sin enviar si falta PUBLIC_BASE_URL o SESSION_SECRET. Preferir
    no entregar el archivo antes que publicarlo sin protección; el asesor ve que
    el mensaje quedó sin acuse y el motivo queda en el log.
    """
    ruta = signed_media_path(Path(file_path).name)
    if not ruta or not settings.public_base_url:
        logger.error(
            "Adjunto para %s NO enviado: falta PUBLIC_BASE_URL o SESSION_SECRET "
            "para firmar la URL que Twilio necesita. Archivo: %s",
            mask_phone(to_phone),
            filename or file_path,
        )
        return None

    data = {"MediaUrl": f"{settings.public_base_url}{ruta}"}
    if caption:
        data["Body"] = caption[:BODY_MAX]
    return _post_message(to_phone, data)
