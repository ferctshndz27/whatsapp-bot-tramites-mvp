import hashlib
import hmac
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Response
from sqlmodel import select
from starlette.concurrency import run_in_threadpool

from app import conversation, messaging, twilio_client
from app.conversation import handle_incoming
from app.credenciales import credencial
from app.db import get_session
from app.models import OFICINA_TLAXCALA_ID, PROVIDER_META, PROVIDER_TWILIO, Client, Message
from app.utils import mask_phone, normalize_phone

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/webhook")
def verify_webhook(request: Request) -> Response:
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge", "")

    # ponytail: office_id fijo a Tlaxcala. El PR 4 lo resuelve desde
    # metadata.phone_number_id (§4.1) / el campo `To` (§4.2) y lo baja por el
    # mismo túnel que ya transporta `provider` (ver handle_inbound más abajo).
    #
    # A diferencia del SELECT del POST (ver _valid_signature), este NO toca el
    # event loop: verify_webhook es `def` y no `async def`, así que Starlette ya
    # lo corre en su threadpool. Y encima solo corre en el handshake de alta del
    # webhook, no una vez por mensaje.
    if mode == "subscribe" and token == credencial(
        "WHATSAPP_VERIFY_TOKEN", office_id=OFICINA_TLAXCALA_ID
    ):
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
                    # En threadpool: toda la cadena de abajo (NLU con red, envío
                    # HTTP a Meta/Twilio, SQLite) es síncrona, y uvicorn corre
                    # con UN solo worker — dentro del event loop bloquearía el
                    # proceso entero por hasta ~8 s por mensaje. El orden dentro
                    # del payload se conserva: se espera cada mensaje.
                    await run_in_threadpool(_process_message, message)
                except Exception:
                    logger.exception("Error procesando mensaje de WhatsApp id=%s", message.get("id"))
            for status in value.get("statuses", []):
                try:
                    await run_in_threadpool(_process_status, status)
                except Exception:
                    logger.exception("Error procesando estado de WhatsApp id=%s", status.get("id"))

    return {"status": "ok"}


def _valid_signature(raw_body: bytes, signature_header: str) -> bool:
    # ponytail: office_id fijo a Tlaxcala. El PR 4 lo resuelve desde
    # metadata.phone_number_id (§4.1); mientras tanto este SELECT corre DENTRO
    # del event loop, porque receive_webhook es `async def` y lo llama sin
    # threadpool. Techo: 184 µs, uno por petición entrante. NO mandarlo a
    # threadpool: el salto cuesta 47 µs y subiría el SELECT de 184 a 231 µs de
    # pared para ahorrar 137 de loop — la misma pesimización medida que ya
    # documenta stripe_webhook.py, y por el mismo motivo.
    secreto = credencial("WHATSAPP_APP_SECRET", office_id=OFICINA_TLAXCALA_ID)
    if not secreto or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secreto.encode(), raw_body, hashlib.sha256).hexdigest()
    received = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, received)


def _process_message(message: dict) -> None:
    """Entrada de Meta: se traduce a la forma común y se procesa."""
    # Se normaliza AQUÍ, en el borde: el formato crudo de cada proveedor no
    # entra a la base. Sin esto, el mismo humano por Meta y por Twilio serían
    # dos clientes distintos (la PK de `client` es el teléfono).
    phone = normalize_phone(message.get("from"))
    if not phone:
        return
    text, is_media = _extract_text(message)
    handle_inbound(phone, text, is_media, message.get("id"), PROVIDER_META)


def handle_inbound(
    phone: str, text: str, is_media: bool, message_id: str | None, provider: str
) -> None:
    """Tronco común a los dos proveedores.

    Todo lo que sigue —bloqueo del cliente, máquina de estados, presentación y
    entrega— es idéntico venga de donde venga: para cuando se llega aquí, cada
    adaptador ya tradujo su formato al canónico. `provider` viaja explícito
    hasta la entrega para que la respuesta salga POR EL MISMO NÚMERO que recibió
    el mensaje.
    """
    with get_session() as session:
        client = session.get(Client, phone)
        if client is not None and not client.habilitado:
            logger.info("Mensaje de %s ignorado: cliente bloqueado", mask_phone(phone))
            return

    with get_session() as session:
        replies = handle_incoming(
            session, phone, text, is_media=is_media, wa_message_id=message_id, provider=provider
        )
        outbound = conversation.presentation(session, phone, replies)

    for item in outbound:
        wa_message_id = _deliver(phone, item, provider)
        if wa_message_id:
            _tag_sent_message(phone, wa_message_id, provider)


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


def _deliver(phone: str, item, provider: str = PROVIDER_META) -> str | None:
    """Envía texto plano o interactivo; si el interactivo falla, cae al texto
    (el cliente nunca se queda sin respuesta por un error de formato)."""
    if isinstance(item, str):
        return messaging.send_text(phone, item, provider=provider)
    if "buttons" in item:
        wamid = messaging.send_buttons(phone, item["text"], item["buttons"], provider=provider)
    else:
        wamid = messaging.send_list(
            phone, item["text"], item.get("button", "Ver opciones"), item["list"], provider=provider
        )
    return wamid or messaging.send_text(phone, item["text"], provider=provider)


def _tag_sent_message(phone: str, wa_message_id: str, provider: str = PROVIDER_META) -> None:
    # Empareja con la fila OUT sin taggear más vieja de este teléfono (FIFO con
    # el orden en que se registraron y enviaron las respuestas).
    #
    # El filtro por `provider` NO es cosmético: con dos cuentas escribiéndole al
    # mismo cliente, sin él un wamid de Meta podía quedar pegado a un saliente
    # de Twilio y el estado de entrega se aplicaría al mensaje equivocado.
    # ponytail: sigue siendo FIFO, suficiente para envíos de una sola petición;
    # revisar si algún día hay envíos concurrentes al mismo teléfono.
    with get_session() as session:
        row = session.exec(
            select(Message)
            .where(Message.phone == phone)
            .where(Message.provider == provider)
            .where(Message.wa_message_id.is_(None))
            .order_by(Message.id)
        ).first()
        if row:
            row.wa_message_id = wa_message_id
            session.add(row)
            session.commit()


def _apply_status(
    wa_message_id: str, state: str, status_at: datetime, pricing: dict | None = None
) -> None:
    """Guarda el estado de entrega en la fila del mensaje. Común a los dos
    proveedores: para llegar aquí, cada uno ya tradujo su vocabulario.

    `pricing` es el veredicto de cobro de Meta y solo viaja en el status `sent`
    y en UNO de delivered/read — nunca en los tres. Por eso se fusiona en vez de
    asignarse: el status que llegue después sin `pricing` borraría lo ya
    guardado y el medidor contaría ese mensaje como «sin dato».
    """
    with get_session() as session:
        row = session.exec(select(Message).where(Message.wa_message_id == wa_message_id)).first()
        if not row:
            # No hay a quién pegarle el estado. Pasa cuando el emparejamiento
            # FIFO de wamid cruzó ids entre canales (ver _tag_sent_message) o
            # cuando el saliente no dejó fila. Se registra porque el medidor
            # cuenta estos huecos: un total que se lee completo sin estarlo es
            # peor que un total con su «sin dato» al lado.
            logger.info("Estado de WhatsApp sin mensaje local: %s", wa_message_id)
            return
        row.wa_status = state
        row.wa_status_at = status_at
        if pricing:
            row.pricing_category = pricing.get("category") or row.pricing_category
            row.pricing_type = pricing.get("type") or row.pricing_type
            if pricing.get("billable") is not None:
                row.pricing_billable = bool(pricing["billable"])
        session.add(row)
        session.commit()


def _process_status(status: dict) -> None:
    wa_message_id = status.get("id")
    state = status.get("status")
    if not wa_message_id or not state:
        return

    timestamp = status.get("timestamp")
    status_at = (
        datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
        if timestamp
        else datetime.now(timezone.utc)
    )
    # `pricing`: {billable, pricing_model, type, category}. Se copia entero y sin
    # interpretar — qué se cobra lo decide Meta, no este código.
    _apply_status(wa_message_id, state, status_at, status.get("pricing"))


# ---------------------------------------------------------------------------
# Segunda cuenta: Twilio
# ---------------------------------------------------------------------------


async def _form_verificado(request: Request, path: str) -> dict[str, str]:
    """Form del webhook de Twilio, ya con la firma validada.

    La firma se calcula sobre la URL PÚBLICA que Twilio llamó, reconstruida
    desde PUBLIC_BASE_URL: detrás del proxy `request.url` trae el host interno y
    http://, y con eso no coincidiría ninguna firma.

    Esto es además el cortafuegos entre proveedores: un payload de Meta enviado
    a esta ruta no trae X-Twilio-Signature válida y se rechaza entero, en vez de
    procesarse a medias.
    """
    form = {clave: str(valor) for clave, valor in (await request.form()).items()}
    firma = request.headers.get("x-twilio-signature", "")
    if not twilio_client.verify_signature(twilio_client.callback_url(path), form, firma):
        raise HTTPException(status_code=403, detail="Firma inválida")
    return form


@router.post("/webhook/twilio")
async def receive_twilio(request: Request) -> Response:
    form = await _form_verificado(request, "/webhook/twilio")

    datos = twilio_client.parse_inbound(form)
    if datos is not None:
        try:
            # Mismo motivo que en receive_webhook: cadena síncrona fuera del loop.
            await run_in_threadpool(
                handle_inbound,
                datos["phone"],
                datos["text"],
                datos["is_media"],
                datos["message_id"],
                PROVIDER_TWILIO,
            )
        except Exception:
            logger.exception("Error procesando mensaje de Twilio sid=%s", datos["message_id"])

    # 204 y no TwiML: la respuesta al cliente se manda por la API, igual que en
    # Meta. Devolver TwiML aquí duplicaría el mensaje.
    return Response(status_code=204)


@router.post("/webhook/twilio/status")
async def receive_twilio_status(request: Request) -> Response:
    form = await _form_verificado(request, "/webhook/twilio/status")

    sid = form.get("MessageSid")
    estado = twilio_client.map_status(form.get("MessageStatus", ""))
    if sid and estado:
        await run_in_threadpool(_apply_status, sid, estado, datetime.now(timezone.utc))
    elif sid:
        # Estado que no está en la tabla: se registra en vez de inventarle un
        # significado. La bandeja prefiere no mostrar nada a mostrar algo falso.
        logger.warning(
            "MessageStatus desconocido de Twilio (%s) para sid=%s",
            form.get("MessageStatus"),
            sid,
        )
    return Response(status_code=204)
