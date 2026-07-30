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
from app.config import settings
from app.db import get_session
from app.models import EventoPasarela

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


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request) -> dict:
    # El cuerpo CRUDO antes de cualquier json(): la firma es sobre los bytes.
    raw = await request.body()
    if not _verificar_firma(raw, request.headers.get("stripe-signature", ""),
                            settings.stripe_webhook_secret):
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
