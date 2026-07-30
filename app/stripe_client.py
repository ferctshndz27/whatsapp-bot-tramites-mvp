"""Único módulo que habla con api.stripe.com.

httpx directo, sin el SDK de stripe: una dependencia menos que auditar y el
cuerpo form-urlencoded con corchetes es trivial de construir a mano. Copia el
patrón de reintentos de app/whatsapp_client.py.

Con dinero, "sin credenciales", "rechazado" y "no sé" son TRES cosas
distintas: RespuestaStripe.indeterminado=True significa "la petición pudo
haber llegado" (timeout, 5xx) y el que llama NUNCA debe tratarlo como un
rechazo — ese cobro va al barrido de huérfanos, no a un reintento a ciegas.

No conoce SQLModel ni FastAPI.
"""

import logging
import time
from dataclasses import dataclass

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

API_BASE = "https://api.stripe.com"
MAX_RETRIES = 3
REQUEST_TIMEOUT_SECONDS = 10
# 409 = idempotency_key_in_use: "la petición original sigue en vuelo".
# Reintentable por definición — con la misma clave, Stripe devuelve el
# objeto original, jamás crea un segundo.
RETRYABLE_STATUS = {409, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class RespuestaStripe:
    ok: bool
    http_status: int | None = None
    request_id: str | None = None  # req_…: lo primero que pide soporte de Stripe
    objeto: dict | None = None
    error_codigo: str | None = None
    error_mensaje: str | None = None
    cuerpo_recortado: str | None = None
    # True = no sabemos si Stripe procesó la petición (red caída, 5xx, 409
    # agotado). El objeto PUDO haberse creado: solo el barrido de huérfanos
    # puede resolverlo, nunca un segundo POST sin la misma clave.
    indeterminado: bool = False


def _request(
    metodo: str,
    path: str,
    data: dict | None = None,
    params: dict | None = None,
    clave_idempotencia: str | None = None,
) -> RespuestaStripe:
    if not settings.stripe_secret_key:
        # No-op silencioso del cliente (patrón whatsapp_client); la UI es la
        # que avisa "Stripe no configurado" — un cobro fantasma es peor.
        return RespuestaStripe(
            ok=False, error_codigo="sin_credenciales",
            error_mensaje="STRIPE_SECRET_KEY sin definir",
        )

    headers = {}
    if clave_idempotencia:
        headers["Idempotency-Key"] = clave_idempotencia
    if settings.stripe_api_version:
        headers["Stripe-Version"] = settings.stripe_api_version

    delay = 1.0
    ultima: RespuestaStripe | None = None
    for intento in range(1, MAX_RETRIES + 1):
        try:
            r = httpx.request(
                metodo, f"{API_BASE}{path}",
                data=data, params=params, headers=headers,
                auth=(settings.stripe_secret_key, ""),
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.RequestError as exc:
            ultima = RespuestaStripe(
                ok=False, error_codigo="error_red",
                error_mensaje=str(exc)[:500], indeterminado=True,
            )
            if intento == MAX_RETRIES:
                logger.error("Stripe sin respuesta (%s %s): %s", metodo, path, exc)
                return ultima
        else:
            request_id = r.headers.get("Request-Id")
            if r.status_code < 300:
                try:
                    objeto = r.json()
                except ValueError:
                    return RespuestaStripe(
                        ok=False, http_status=r.status_code, request_id=request_id,
                        error_codigo="respuesta_ilegible",
                        cuerpo_recortado=r.text[:500], indeterminado=True,
                    )
                return RespuestaStripe(
                    ok=True, http_status=r.status_code,
                    request_id=request_id, objeto=objeto,
                )
            try:
                err = r.json().get("error", {})
            except ValueError:
                err = {}
            ultima = RespuestaStripe(
                ok=False, http_status=r.status_code, request_id=request_id,
                error_codigo=err.get("code") or err.get("type"),
                error_mensaje=(err.get("message") or "")[:500],
                cuerpo_recortado=r.text[:500],
                # 5xx: Stripe pudo haber procesado antes de fallar. 409: la
                # original sigue en vuelo. Ambos son "no sé", no "rechazado".
                indeterminado=r.status_code >= 500 or r.status_code == 409,
            )
            if r.status_code not in RETRYABLE_STATUS or intento == MAX_RETRIES:
                if not ultima.ok:
                    logger.error(
                        "Stripe rechazó %s %s (status=%s req=%s): %s",
                        metodo, path, r.status_code, request_id, r.text[:500],
                    )
                return ultima
        time.sleep(delay)
        delay *= 2
    return ultima  # pragma: no cover — el bucle siempre retorna antes


def _metadata_plana(prefijo: str, metadata: dict) -> dict:
    return {f"{prefijo}[{k}]": str(v) for k, v in metadata.items()}


def crear_checkout_session(
    clave_idempotencia: str,
    monto_centavos: int,
    moneda: str,
    concepto: str,
    metadata: dict,
    metodos: list[str],
    success_url: str,
    cancel_url: str,
    msi: bool = False,
    oxxo_dias: int | None = None,
    expires_at: int | None = None,
) -> RespuestaStripe:
    """POST /v1/checkout/sessions. `metodos` acepta card/oxxo/spei.

    La metadata viaja DOS veces: a nivel sesión y en payment_intent_data —
    el PaymentIntent es el único objeto que Stripe sabe buscar por metadata,
    y esa búsqueda es la llave de recuperación cuando la clave de
    idempotencia ya caducó (>24 h).
    """
    data: dict = {
        "mode": "payment",
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": moneda,
        "line_items[0][price_data][unit_amount]": str(monto_centavos),
        "line_items[0][price_data][product_data][name]": concepto[:250],
        "success_url": success_url,
        "cancel_url": cancel_url,
    }
    i = 0
    for metodo in metodos:
        if metodo == "spei":
            # SPEI en Stripe MX no es un payment_method_type plano: es
            # customer_balance con transferencia bancaria mx, y exige Customer.
            data[f"payment_method_types[{i}]"] = "customer_balance"
            data["payment_method_options[customer_balance][funding_type]"] = "bank_transfer"
            data["payment_method_options[customer_balance][bank_transfer][type]"] = "mx_bank_transfer"
            data["customer_creation"] = "always"
        else:
            data[f"payment_method_types[{i}]"] = metodo
        i += 1
    if "oxxo" in metodos and oxxo_dias:
        data["payment_method_options[oxxo][expires_after_days]"] = str(oxxo_dias)
    if msi:
        data["payment_method_options[card][installments][enabled]"] = "true"
    if expires_at:
        data["expires_at"] = str(expires_at)
    data.update(_metadata_plana("metadata", metadata))
    data.update(_metadata_plana("payment_intent_data[metadata]", metadata))
    return _request("POST", "/v1/checkout/sessions", data=data,
                    clave_idempotencia=clave_idempotencia)


def expirar_sesion(session_id: str, clave_idempotencia: str | None = None) -> RespuestaStripe:
    return _request("POST", f"/v1/checkout/sessions/{session_id}/expire",
                    clave_idempotencia=clave_idempotencia)


def cancelar_payment_intent(pi_id: str, clave_idempotencia: str | None = None) -> RespuestaStripe:
    """Esto —y no /expire— es lo que mata un voucher OXXO en requires_action."""
    return _request("POST", f"/v1/payment_intents/{pi_id}/cancel",
                    clave_idempotencia=clave_idempotencia)


def obtener_session(session_id: str) -> RespuestaStripe:
    return _request("GET", f"/v1/checkout/sessions/{session_id}")


def obtener_payment_intent(pi_id: str) -> RespuestaStripe:
    return _request("GET", f"/v1/payment_intents/{pi_id}")


def obtener_balance_transaction(txn_id: str) -> RespuestaStripe:
    """comision y neto se LEEN de aquí, jamás se calculan con una tarifa."""
    return _request("GET", f"/v1/balance_transactions/{txn_id}")


def buscar_sesiones_por_ventana(desde_epoch: int, hasta_epoch: int) -> RespuestaStripe:
    """Checkout Sessions NO soporta filtro por metadata: se trae la ventana de
    `created` completa y la metadata se filtra del lado nuestro."""
    return _request(
        "GET", "/v1/checkout/sessions",
        params={"created[gte]": desde_epoch, "created[lte]": hasta_epoch, "limit": 100},
    )


def buscar_payment_intents_por_metadata(campo: str, valor: str) -> RespuestaStripe:
    """Search API (eventualmente consistente, ~1 min): respaldo de la ventana."""
    consulta = f"metadata['{campo}']:'{valor}'"
    return _request("GET", "/v1/payment_intents/search", params={"query": consulta})
