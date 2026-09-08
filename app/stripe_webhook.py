"""Webhook de Stripe: POST /webhooks/stripe.

Archivo aparte del panel a propósito: su autenticación es HMAC, no cookie —
el mismo criterio por el que app/webhook.py (Meta) vive separado de
app/dashboard.py. Sin ningún guard de sesión.

El contrato de respuesta es dinero:
  - firma inválida → 403 SIN escribir nada (y los rechazos van al log, no a
    la tabla: si no, cualquiera desde internet la llena);
  - fila del evento COMMITEADA → 200. IntegrityError = replay → 200.
  - cualquier OTRA excepción al insertar (p.ej. database is locked) → 503
    para que Stripe reintente: confundir "duplicado" con "no pude escribir"
    pierde eventos de OXXO para siempre;
  - fallar APLICANDO sí devuelve 200: el payload ya es nuestro y lo
    reprocesa el barrido — el reintento de Stripe dura 3 días y se acaba.
"""

import hashlib
import hmac
import json
import logging
import time

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.exc import IntegrityError

from app import cobranza
from app.credenciales import credencial
from app.db import get_session
from sqlmodel import select

from app.models import EventoPasarela, OFICINA_TLAXCALA_ID, Office

logger = logging.getLogger(__name__)

router = APIRouter()

TOLERANCIA_SEGUNDOS = 300  # contra replay de un cuerpo viejo con firma vieja


def _verificar_firma(raw: bytes, header: str, secreto: str,
                     tolerancia: int = TOLERANCIA_SEGUNDOS) -> bool:
    """Fail-closed: sin secreto o sin cabecera, NADA pasa. Se comparan TODAS
    las firmas v1 (Stripe manda varias mientras rota el secreto) con
    compare_digest."""
    if not secreto or not header:
        return False
    t: str | None = None
    v1s: list[str] = []
    for parte in header.split(","):
        clave, _, valor = parte.strip().partition("=")
        if clave == "t":
            t = valor
        elif clave == "v1":
            v1s.append(valor)
    if not t or not v1s:
        return False
    try:
        marca = int(t)
    except ValueError:
        return False
    if abs(time.time() - marca) > tolerancia:
        return False
    firmado = f"{t}.".encode() + raw
    esperada = hmac.new(secreto.encode(), firmado, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(esperada, v1) for v1 in v1s)


@router.post("/webhooks/stripe/{slug}")
async def stripe_webhook_oficina(request: Request, slug: str) -> dict:
    """Endpoint por oficina — §4.3. Un slug desconocido es 403, no 404.

    El evento de Stripe NO trae identificador de oficina: con una cuenta por
    oficina hay un secreto por oficina, y la única forma de saber con cuál
    verificar es que la oficina venga en la URL. La alternativa —probar todos
    los secretos vivos— cuesta N verificaciones HMAC por evento y ensucia el
    log; configurar el endpoint en Stripe ya es parte del alta de la cuenta.

    403 y no 404 porque este endpoint es público: distinguir «esa oficina no
    existe» de «tu firma no cuadra» le enumera las oficinas a cualquiera. Con la
    misma respuesta para ambos, un slug inventado no dice nada.
    """
    office_id = _oficina_por_slug(slug)
    if office_id is None:
        logger.warning("Webhook de Stripe a un slug desconocido: %r", slug[:64])
        raise HTTPException(status_code=403, detail="Firma inválida")
    return await _procesar(request, office_id)


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request) -> dict:
    """Ruta LEGADA, amarrada a Tlaxcala.

    Se conserva a propósito: es el endpoint que hoy está configurado y
    funcionando en producción, y moverlo sería coordinar un cambio en el panel
    de Stripe con el deploy. Una oficina nueva usa la ruta con slug."""
    return await _procesar(request, OFICINA_TLAXCALA_ID)


def _oficina_por_slug(slug: str) -> int | None:
    if not slug:
        return None
    with get_session() as session:
        return session.exec(
            select(Office.id).where(
                Office.slug == slug,
                Office.habilitado == True,  # noqa: E712
            )
        ).first()


async def _procesar(request: Request, office_id: int) -> dict:
    # El cuerpo CRUDO antes de cualquier json(): la firma es sobre los bytes.
    raw = await request.body()
    # La llave SOLO autentica: la oficina del cobro sale del `Cobro` que el
    # evento referencia, no de con qué secreto se verificó (§4.3). Por eso
    # `eventopasarela` no lleva office_id y no hace falta que lo lleve.
    #
    # Este SELECT corre en el event loop: 184 µs una vez por evento. NO mandarlo
    # a threadpool — el salto cuesta 47 µs y lo subiría a 231 µs de pared para
    # ahorrar 137 de loop; es una pesimización medida.
    if not _verificar_firma(raw, request.headers.get("stripe-signature", ""),
                            credencial("STRIPE_WEBHOOK_SECRET", office_id=office_id)):
        logger.warning("Webhook de Stripe rechazado: firma inválida o ausente")
        raise HTTPException(status_code=403, detail="Firma inválida")

    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Cuerpo ilegible") from None
    evento_stripe_id = payload.get("id")
    tipo = payload.get("type")
    if not evento_stripe_id or not tipo:
        raise HTTPException(status_code=400, detail="Evento sin id o tipo")

    obj = (payload.get("data") or {}).get("object") or {}
    monto = obj.get("amount_total")
    if monto is None:
        monto = obj.get("amount_received") or obj.get("amount")

    # Transacción CORTÍSIMA: solo persistir. El INSERT contra el unique de
    # stripe_event_id ES la detección de duplicados (sin ventana de carrera).
    try:
        with get_session() as session:
            evento = EventoPasarela(
                stripe_event_id=evento_stripe_id,
                tipo=tipo,
                api_version=payload.get("api_version"),
                livemode=payload.get("livemode"),
                stripe_created=payload.get("created") or 0,
                objeto_id=obj.get("id"),
                monto_centavos=monto,
                moneda=obj.get("currency"),
                payload_json=raw.decode("utf-8", "replace")[:cobranza.PAYLOAD_MAX],
            )
            session.add(evento)
            session.commit()
            evento_id = evento.id
    except IntegrityError:
        return {"status": "duplicado"}  # replay de Stripe: ya lo tenemos
    except Exception:
        logger.exception("No se pudo persistir el evento %s", evento_stripe_id)
        raise HTTPException(status_code=503, detail="Reintentar") from None

    # Aplicación en su propia transacción: si revienta, procesado_at queda
    # NULL y el barrido lo reintenta. Aun así 200.
    try:
        with get_session() as session:
            fila = session.get(EventoPasarela, evento_id)
            cobranza.aplicar_evento(session, fila)
            session.commit()
    except Exception:
        logger.exception("Fallo aplicando el evento %s (id=%s); queda en cola",
                         evento_stripe_id, evento_id)
        try:
            with get_session() as session:
                fila = session.get(EventoPasarela, evento_id)
                if fila is not None:
                    fila.intentos_proceso += 1
                    session.add(fila)
                    session.commit()
        except Exception:  # pragma: no cover — contar el intento es best-effort
            logger.exception("No se pudo contar el intento fallido de %s", evento_stripe_id)

    return {"status": "ok"}
