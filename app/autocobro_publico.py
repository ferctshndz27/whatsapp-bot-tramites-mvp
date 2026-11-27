"""Páginas públicas del autocobro (las abre el CLIENTE, sin sesión).

F2: los términos. F3: la liga con token (`/autocobro/{token}`), que registra la tarjeta con Stripe
Elements + SetupIntent. El número de tarjeta y el CVV los captura el iframe de Stripe: nunca llegan aquí.

El alta se cierra en `/listo` leyendo el SetupIntent DIRECTO de Stripe (no se cree nada del navegador).
El respaldo por webhook `setup_intent.succeeded` (cliente que cierra la pestaña antes de volver) queda
para la fase del motor de cobro.
"""

import hashlib
import logging
import secrets

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlmodel import select

from app import autocobro, stripe_client
from app import autocobro_terminos as terminos
from app.authlog import client_ip
from app.autocobro import EstadoLiga
from app.config import settings
from app.db import get_session
from app.models import ConsentimientoCobro, PlanCobro
from app.templating import templates

logger = logging.getLogger(__name__)
router = APIRouter()

# La página de la liga es la única del sistema que carga recursos de un tercero, y solo de Stripe.
CSP_LIGA = (
    "default-src 'self'; img-src 'self' data: https://*.stripe.com; style-src 'self'; "
    "script-src 'self' https://js.stripe.com; frame-src https://js.stripe.com https://hooks.stripe.com; "
    "connect-src 'self' https://api.stripe.com; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)

# Texto para el cliente según por qué la liga no sirve. Nunca revela si el token existió.
_MENSAJES = {
    EstadoLiga.NO_EXISTE: ("🔎", "Esta liga no es válida", "Revisa que abriste la liga completa que te mandó tu asesor.", 404),
    EstadoLiga.VENCIDA: ("⏰", "Esta liga venció", "Las ligas duran 72 horas. Pide a tu asesor que te mande una nueva.", 410),
    EstadoLiga.USADA: ("✅", "Esta liga ya se usó", "Tu tarjeta ya quedó registrada. No tienes que hacer nada más.", 410),
    EstadoLiga.INACTIVA: ("↩️", "Esta liga ya no está activa", "Pide a tu asesor que te mande una nueva.", 410),
    EstadoLiga.DEMASIADAS: ("⛔", "Esta liga se abrió demasiadas veces", "Por seguridad la bloqueamos. Pide a tu asesor una nueva.", 429),
}


def _proteger(response: Response, *, con_stripe: bool = False) -> Response:
    """Cabeceras de una página con token: la URL es el secreto, así que no sale en Referer ni se cachea."""
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    if con_stripe:
        response.headers["Content-Security-Policy"] = CSP_LIGA
    return response


def _mensaje(request: Request, icono: str, titulo: str, mensaje: str, status: int, nota: str = ""):
    return _proteger(templates.TemplateResponse(
        request, "cobro_retorno.html",
        {"request": request, "icono": icono, "titulo": titulo, "mensaje": mensaje, "nota": nota},
        status_code=status,
    ))


def _de_estado(request: Request, estado: EstadoLiga):
    icono, titulo, mensaje, status = _MENSAJES[estado]
    return _mensaje(request, icono, titulo, mensaje, status)


def _stripe_listo() -> bool:
    return bool(settings.stripe_publishable_key and stripe_client.llave_secreta_configurada())


# ---------------------------------------------------------------------------
# Términos (F2)
# ---------------------------------------------------------------------------


def _pagina_terminos(request: Request, version: int):
    if version not in terminos.VERSIONES:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request, "legal_autocobro.html",
        {
            "request": request,
            "version": version,
            "vigente": version == terminos.VIGENTE,
            "huella": terminos.hash_texto(version),
            "secciones": terminos.secciones(version),
        },
    )


@router.get("/legal/autocobro")
def terminos_vigentes(request: Request):
    return _pagina_terminos(request, terminos.VIGENTE)


@router.get("/legal/autocobro/v{version}")
def terminos_por_version(request: Request, version: int):
    return _pagina_terminos(request, version)


# ---------------------------------------------------------------------------
# La liga (F3)
# ---------------------------------------------------------------------------


@router.get("/autocobro/{token}")
def ver_liga(request: Request, token: str):
    if not _stripe_listo():
        return _mensaje(request, "🛠️", "Por ahora no podemos registrar tu tarjeta",
                        "Avisa a tu asesor; en cuanto esté listo te manda la liga otra vez.", 503)
    with get_session() as session:
        estado, liga, plan = autocobro.abrir_liga(session, token)
        if estado is not EstadoLiga.OK:
            session.commit()  # la apertura de una liga bloqueada también cuenta
            return _de_estado(request, estado)
        vista = autocobro.resumen(session, plan)
        session.commit()
    return _proteger(templates.TemplateResponse(
        request, "autocobro.html",
        {
            "request": request, "token": token, "r": vista,
            "pk": settings.stripe_publishable_key,
            "version_terminos": terminos.VIGENTE,
        },
    ), con_stripe=True)


@router.post("/autocobro/{token}/preparar")
def preparar(
    request: Request, token: str,
    nombre: str = Form(""), email: str = Form(""), acepto: str = Form(""),
):
    """Crea el customer y el SetupIntent; devuelve el `client_secret` que Elements necesita."""
    if autocobro.limite_excedido(client_ip(request)):
        return _proteger(JSONResponse({"error": "Demasiados intentos. Espera unos minutos."}, status_code=429))
    if not _stripe_listo():
        return _proteger(JSONResponse({"error": "El registro no está disponible por ahora."}, status_code=503))
    if acepto != "1":
        return _proteger(JSONResponse({"error": "Marca la casilla para autorizar los cobros."}, status_code=400))
    nombre, email = nombre.strip(), email.strip()
    error = autocobro.validar_datos(nombre, email)
    if error:
        return _proteger(JSONResponse({"error": error}, status_code=400))

    with get_session() as session:
        estado, liga, plan = autocobro.liga_por_token(session, token)
        if estado is not EstadoLiga.OK:
            return _proteger(JSONResponse({"error": _MENSAJES[estado][1]}, status_code=410))
        liga_id, plan_id = liga.id, plan.id
        customer_id = plan.stripe_customer_id
        hash_mandato = autocobro.resumen(session, plan).hash_mandato

    # La red a Stripe, fuera de toda sesión abierta.
    if not customer_id:
        huella = hashlib.sha256(f"{nombre}|{email}".encode()).hexdigest()[:12]
        r = stripe_client.crear_customer(
            f"autocobro-cus-{plan_id}-{huella}", {"plan_id": plan_id}, nombre=nombre, email=email,
        )
        if not r.ok or not r.objeto or not r.objeto.get("id"):
            logger.error("Autocobro: no se pudo crear el customer del plan %s (%s)", plan_id, r.error_codigo)
            return _proteger(JSONResponse({"error": "No pudimos preparar el formulario. Intenta de nuevo."}, status_code=502))
        customer_id = r.objeto["id"]
        with get_session() as session:
            plan = session.get(PlanCobro, plan_id)
            plan.stripe_customer_id = plan.stripe_customer_id or customer_id
            customer_id = plan.stripe_customer_id
            session.add(plan)
            session.commit()

    r = stripe_client.crear_setup_intent(
        customer_id,
        {"liga_id": liga_id, "plan_id": plan_id, "mandato_hash": hash_mandato, "nombre": nombre, "email": email},
        clave_idempotencia=f"autocobro-si-{liga_id}-{secrets.token_hex(8)}",
    )
    secreto = (r.objeto or {}).get("client_secret") if r.ok else None
    if not secreto:
        logger.error("Autocobro: no se pudo crear el SetupIntent de la liga %s (%s)", liga_id, r.error_codigo)
        return _proteger(JSONResponse({"error": "No pudimos preparar el formulario. Intenta de nuevo."}, status_code=502))

    base = (settings.public_base_url or str(request.base_url)).rstrip("/")
    return _proteger(JSONResponse({"client_secret": secreto, "return_url": f"{base}/autocobro/{token}/listo"}))


@router.get("/autocobro/{token}/listo")
def listo(request: Request, token: str, setup_intent: str = ""):
    """Vuelta del formulario (con o sin 3D Secure): confirma con Stripe y cierra el alta."""
    exito = ("✅", "¡Listo, tu tarjeta quedó registrada!",
             "Tu asesor ya puede programar tus cobros según lo que autorizaste.",
             "Te avisaremos por WhatsApp antes de cada cargo. Ya puedes cerrar esta página.")

    with get_session() as session:
        estado, liga, plan = autocobro.liga_por_token(session, token)
        if estado is EstadoLiga.USADA:
            # Refrescar esta página no debe parecer un error: si la liga se usó con ESTE SetupIntent, es éxito.
            ya = session.exec(select(ConsentimientoCobro).where(
                ConsentimientoCobro.stripe_setup_intent_id == setup_intent)).first() if setup_intent else None
            return _mensaje(request, *exito[:3], 200, exito[3]) if ya else _de_estado(request, estado)
        if estado is not EstadoLiga.OK:
            return _de_estado(request, estado)
    if not setup_intent.startswith("seti_") or len(setup_intent) > 100:
        return _de_estado(request, EstadoLiga.NO_EXISTE)

    r = stripe_client.obtener_setup_intent(setup_intent)
    if not r.ok or not r.objeto:
        logger.error("Autocobro: no se pudo leer el SetupIntent %s (%s)", setup_intent, r.error_codigo)
        return _mensaje(request, "⚠️", "No pudimos confirmar tu tarjeta",
                        "Recarga esta página en un momento. Si sigue igual, avisa a tu asesor.", 502)
    si = r.objeto
    if si.get("status") == "processing":
        return _mensaje(request, "⏳", "Estamos confirmando tu tarjeta",
                        "Recarga esta página en un minuto.", 202)
    if si.get("status") != "succeeded":
        return _mensaje(request, "↩️", "No se pudo registrar la tarjeta",
                        "No se te cobró nada. Vuelve a abrir la liga que te mandó tu asesor para intentar de nuevo.", 200)

    try:
        with get_session() as session:
            estado, liga, plan = autocobro.liga_por_token(session, token)
            if estado is not EstadoLiga.OK:
                return _de_estado(request, estado)
            autocobro.completar_alta(
                session, liga, plan, si, ip=client_ip(request), user_agent=request.headers.get("user-agent"),
            )
            session.commit()
    except autocobro.AltaInvalida as exc:
        logger.warning("Autocobro: alta rechazada para %s: %s", setup_intent, exc)
        return _mensaje(request, "⚠️", "No pudimos registrar tu tarjeta",
                        "Avisa a tu asesor para que te mande una liga nueva.", 409)
    except Exception:
        logger.exception("Autocobro: falló el cierre del alta de %s", setup_intent)
        return _mensaje(request, "⚠️", "No pudimos registrar tu tarjeta",
                        "Recarga esta página en un momento. Si sigue igual, avisa a tu asesor.", 500)
    return _mensaje(request, *exito[:3], 200, exito[3])
