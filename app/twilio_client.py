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
import re
import time
from urllib.parse import quote

import httpx

from app.config import settings
from app.credenciales import credencial
from app.media_store import publicar_efimero
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

    # Solo el primer archivo: WhatsApp manda uno por mensaje. Se guarda la URL de
    # la API de Twilio, que lo conserva hasta que alguien lo borre; el VPS no
    # descarga nada al recibir (app/adjuntos.py lo trae al abrirlo).
    adjunto = None
    if num_media > 0 and form.get("MediaUrl0"):
        adjunto = {
            "ref": form["MediaUrl0"],
            "mime": (form.get("MediaContentType0") or "").split(";")[0].strip(),
            "nombre": None,
        }

    return {
        "phone": phone,
        "text": texto,
        "is_media": num_media > 0,
        "adjunto": adjunto,
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


_LISTA_YA_NUMERADA = re.compile(r"^\s*1[.)]\s", re.MULTILINE)


def _numerar(body: str, opciones: list[tuple[str, str]]) -> str:
    """Arma el texto degradado de unos botones.

    Cada opción sale numerada por su posición («1) …», «2) …») y NO con su id interno: el cliente
    ve «1) 🔎 Revisión», no «revision) 🔎 Revisión» (operador, 2026-10-12). La máquina de estados
    entiende el número, igual que la palabra, en cada pregunta con botones.

    Regla general (operador, 2026-10-12): el cliente nunca ve el id interno («revision)», «si)»).
    - Ids que ya son dígitos (la encuesta: «1», «2»): salen tal cual.
    - Un cuerpo que ya trae su lista numerada («¿Confirmas estos 2 trámites? 1. … 2. …»): ahí los
      dígitos eligen servicios y el cuerpo ya dice «responde sí o no»; los botones salen como
      viñetas, sin segundo «1)». Es la misma razón por la que `send_list` no re-numera.
    - Cualquier otro: numeradas por posición, «1) …», «2) …». El bot entiende el número y el nombre.
    """
    ids_son_digitos = all(opcion_id.isdigit() for opcion_id, _ in opciones)
    visibles = opciones[:MAX_OPCIONES]
    if ids_son_digitos:
        lineas = [f"{opcion_id}) {titulo}" for opcion_id, titulo in visibles]
    elif _LISTA_YA_NUMERADA.search(body):
        lineas = [f"• {titulo}" for _, titulo in visibles]
    else:
        lineas = [f"{n}) {titulo}" for n, (_, titulo) in enumerate(visibles, start=1)]
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
    to_phone: str, data: bytes, mime_type: str, caption: str = "", filename: str | None = None
) -> tuple[str | None, str | None]:
    """Manda un adjunto EN MEMORIA vía URL pública efímera. Devuelve (sid, ref).

    Twilio exige `MediaUrl`: una URL de la que ÉL descarga el archivo. Sin disco
    (regla del operador, 2026-09-28), la URL la sirve la RAM unos minutos
    (media_store.publicar_efimero). La referencia «msg:<sid>» deja al CRM volver
    a abrirlo desde el recurso Media del mensaje en Twilio.

    Twilio no admite nombre ni pie en documentos por WhatsApp (su guía de
    medios): el pie se manda en el Body, que es lo único que llega.
    Devuelve (None, None) sin enviar si falta PUBLIC_BASE_URL.
    """
    if not settings.public_base_url:
        logger.error(
            "Adjunto para %s NO enviado: falta PUBLIC_BASE_URL para la URL que Twilio descarga",
            mask_phone(to_phone),
        )
        return None, None
    token = publicar_efimero(data, mime_type, filename)
    if token is None:
        return None, None
    payload = {"MediaUrl": f"{settings.public_base_url}/media/efimero/{token}"}
    if caption:
        payload["Body"] = caption[:BODY_MAX]
    sid = _post_message(to_phone, payload)
    return sid, (f"msg:{sid}" if sid else None)


def _es_url_de_twilio(url: str) -> bool:
    return url.startswith(API_BASE + "/")


def descargar_media(ref: str, max_bytes: int) -> tuple[bytes, str] | None:
    """(contenido, mime) de un archivo guardado en Twilio, o None.

    `ref` es la MediaUrl0 de un entrante o «msg:<MessageSid>» de un saliente
    (se busca el primer Media de ese mensaje). Solo se le habla a la API de
    Twilio —la URL entrante vino en un form con firma verificada, pero aun así
    se rechaza cualquier otro host para no mandar la credencial a donde no es—.
    Twilio exige HTTP Basic en todas las URLs de media; su respuesta puede
    redirigir al almacenamiento donde vive el archivo, y httpx no reenvía la
    autenticación a otro host.
    """
    sid = credencial("TWILIO_ACCOUNT_SID", office_id=OFICINA_TLAXCALA_ID)
    token = credencial("TWILIO_AUTH_TOKEN", office_id=OFICINA_TLAXCALA_ID)
    if not sid or not token:
        return None
    auth = (sid, token)
    try:
        if ref.startswith("msg:"):
            lista = httpx.get(
                f"{API_BASE}/Accounts/{quote(sid)}/Messages/{quote(ref[4:])}/Media.json",
                auth=auth, timeout=REQUEST_TIMEOUT_SECONDS,
            )
            lista.raise_for_status()
            medios = lista.json().get("media_list") or []
            if not medios or not medios[0].get("uri"):
                return None
            url = "https://api.twilio.com" + medios[0]["uri"].removesuffix(".json")
        else:
            url = ref
        if not _es_url_de_twilio(url):
            logger.error("Referencia de media de Twilio con host inesperado; no se descarga")
            return None
        with httpx.stream("GET", url, auth=auth, follow_redirects=True, timeout=30) as r:
            r.raise_for_status()
            mime = (r.headers.get("content-type") or "").split(";")[0].strip()
            partes, total = [], 0
            for trozo in r.iter_bytes():
                total += len(trozo)
                if total > max_bytes:
                    return None
                partes.append(trozo)
        return b"".join(partes), mime
    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
        logger.error("Fallo al descargar media de Twilio: %s", exc)
        return None
