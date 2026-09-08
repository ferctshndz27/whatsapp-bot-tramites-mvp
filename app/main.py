import logging
import secrets

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from sqlmodel import Session

import app.db as db
from app import (
    activity,
    agent_settings,
    agente_studio,
    archive,
    authlog,
    catalog_admin,
    clients,
    cobranza_panel,
    dashboard,
    inbox,
    kb,
    my,
    offices,
    replies,
    stripe_webhook,
    synonym_miner,
    system,
    team_chat,
    users,
    webhook,
)
from app.config import settings
from app.db import init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# La sesión del panel caduca a las 12 h (una jornada); tras eso hay que
# volver a iniciar sesión.
SESSION_MAX_AGE = 12 * 60 * 60

# Sin `/docs`, `/redoc` ni `/openapi.json`: los tres nacen abiertos en FastAPI y
# no hay guard de sesión que los cubra —los middlewares de abajo agregan
# cabeceras y contexto, no autenticación—, así que publicaban el mapa completo
# del ERP (98 rutas: /oficinas, /agente/config, /system, /clients/upsert...) a
# cualquiera que lo pidiera. Nadie los consume: ni una referencia en app/,
# templates/, tests/ ni deploy/. El esquema sigue disponible en local con
# `app.openapi()` desde una shell, que es donde hace falta.
#
# `openapi_url=None` por sí solo ya apaga los tres —las dos UIs se sirven DESDE
# el esquema, así que sin él FastAPI ni siquiera registra sus rutas—. Los otros
# dos van explícitos de todos modos: el día que alguien reactive el esquema para
# generar un cliente, no debe reabrir Swagger y ReDoc de paso sin notarlo.
app = FastAPI(
    title="Bot de trámites — MVP",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# CSS/JS estáticos del panel (el CSS vive en static/css/*.css). No es un
# middleware: no altera el orden de los cuatro middlewares de abajo.
app.mount("/static", StaticFiles(directory="static"), name="static")

session_secret = settings.session_secret
if not session_secret:
    session_secret = secrets.token_urlsafe(32)
    logger.warning(
        "SESSION_SECRET no está definido en .env — usando un secreto aleatorio generado en "
        "memoria. Las sesiones del dashboard se invalidarán en cada reinicio del proceso. "
        "Define SESSION_SECRET para sesiones persistentes en producción."
    )

# Cabeceras de seguridad en toda respuesta. El panel es interno: noindex para
# que ningún buscador lo liste; CSP permite estilos/scripts embebidos (el panel
# los usa) pero bloquea orígenes ajenos y el embebido en iframes (clickjacking).
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Robots-Tag": "noindex, nofollow",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    ),
    # HSTS: el navegador fuerza HTTPS por 1 año. Lo ignora en localhost/HTTP,
    # así que no estorba en desarrollo.
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}


async def _security_headers(request, call_next):
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


async def _nav_context(request, call_next):
    # Contador de propuestas pendientes para el badge del nav (solo lo ve el
    # admin en base.html). Best-effort: si falla, el nav no muestra badge y el
    # request sigue normal. Corre DENTRO de SessionMiddleware (usa la sesión).
    request.state.pending_proposals = 0
    # Los estáticos no renderizan base.html, así que su badge no lo lee nadie:
    # una navegación del panel pide la página y luego su CSS y su JS, y sin esta
    # línea cada uno de esos pedía también el COUNT. La comparación de prefijo
    # cuesta nada y es segura por construcción — lo que se monta en /static/ no
    # pasa por una plantilla.
    if request.url.path.startswith("/static/"):
        return await call_next(request)
    try:
        if request.session.get("user"):
            from sqlmodel import func, select

            from app.db import get_session
            from app.models import SynonymProposal

            with get_session() as s:
                request.state.pending_proposals = s.exec(
                    select(func.count())
                    .select_from(SynonymProposal)
                    .where(SynonymProposal.status == "pendiente")
                ).one()
    except Exception:  # noqa: BLE001 — el badge nunca debe tumbar una página
        pass
    return await call_next(request)


# Orden de registro (Starlette: el último agregado queda más afuera):
#  - session_activity: necesita request.session → dentro de SessionMiddleware.
#  - _security_headers: se registra al final para envolver todo y sellar la
#    respuesta final con las cabeceras.
app.middleware("http")(authlog.session_activity_middleware)
app.middleware("http")(_nav_context)  # dentro de SessionMiddleware, usa la sesión
app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret,
    https_only=settings.secure_cookies,  # flag Secure (HTTPS) — apagable en local
    same_site="lax",                     # mitiga CSRF cross-site
    max_age=SESSION_MAX_AGE,             # la sesión caduca (no vive para siempre)
)
app.middleware("http")(_security_headers)
app.include_router(webhook.router)
app.include_router(stripe_webhook.router)
app.include_router(dashboard.router)
app.include_router(cobranza_panel.router)
app.include_router(inbox.router)
app.include_router(team_chat.router)
app.include_router(activity.router)
app.include_router(users.router)
app.include_router(offices.router)
app.include_router(catalog_admin.router)
app.include_router(system.router)
app.include_router(synonym_miner.router)
app.include_router(my.router)
app.include_router(clients.router)
app.include_router(replies.router)
app.include_router(kb.router)
app.include_router(agente_studio.router)
app.include_router(authlog.router)


def _aplicar_eventos_pendientes() -> None:
    """Cola de eventos de Stripe que quedaron sin aplicar. También corre en el
    cron nocturno, pero el cron vive en el host y puede no estar dado de alta
    en el VPS: sin esta llamada un evento atorado esperaría indefinidamente."""
    try:
        from app.cobranza_jobs import eventos_pendientes

        resultado = eventos_pendientes()
        if resultado["pendientes"]:
            logger.info("Eventos de Stripe aplicados al arrancar: %s", resultado)
    except Exception:
        # Best-effort: la cobranza nunca puede impedir que el bot arranque.
        logger.exception("No se pudieron aplicar los eventos de Stripe pendientes")


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    # Overrides del Estudio del Agente (/agente): lo guardado en la base pisa
    # el .env ANTES de atender el primer mensaje.
    with Session(db.engine) as sesion:
        agent_settings.aplicar_overrides(sesion)
    dashboard.seed_dashboard_user(settings)
    archive.init()
    activity.backfill_closed_tickets()
    archive.backfill()
    _aplicar_eventos_pendientes()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
