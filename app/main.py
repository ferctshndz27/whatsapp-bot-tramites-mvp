import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.responses import PlainTextResponse
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
    crm,
    dashboard,
    eventos,
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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Reemplaza a `@app.on_event("startup")`, deprecado en FastAPI/Starlette.

    El cuerpo del arranque NO se movió aquí a propósito: sigue en `on_startup()`
    (abajo), que es el nombre que citan la skill de devops y el procedimiento de
    deploy. Este envoltorio solo dice cuándo llamarlo. Sin `try` alrededor: si el
    arranque falla, el proceso no debe atender tráfico — `pull-deploy.sh` cuenta
    con que `/health` no responda para disparar el rollback.
    """
    on_startup()
    # Lo único que sí vive aquí y no en on_startup(): el bus de avisos del CRM necesita el event
    # loop, y este envoltorio es lo primero que corre dentro de él (app/eventos.py).
    eventos.capturar_loop()
    yield
    eventos.soltar_loop()


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
async def _error_500(request, exc) -> PlainTextResponse:
    """El 500 sale de ServerErrorMiddleware, que envuelve a TODOS los middlewares
    —también a _security_headers—, así que era la única respuesta del panel sin
    CSP ni X-Frame-Options (auditoría 2026-09, INJ-6). Cuerpo fijo: nunca el
    traceback. Las cabeceras se ponen aquí porque nadie más las va a poner."""
    return PlainTextResponse("Error interno del servidor.", status_code=500, headers=SECURITY_HEADERS)


app = FastAPI(
    title="Bot de trámites — MVP",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    exception_handlers={500: _error_500},
)

# CSS/JS estáticos del panel (el CSS vive en static/css/*.css). No es un
# middleware: no altera el orden de los cuatro middlewares de abajo.
app.mount("/static", StaticFiles(directory="static"), name="static")

# El landing estático queda separado del ERP y se sirve desde la misma API
# principal para asegurar que la carpeta landing/ y sus recursos CSS/JS
# sean accesibles con una ruta pública estable.
app.mount("/landing", StaticFiles(directory="landing"), name="landing")

session_secret = settings.session_secret
if not session_secret:
    session_secret = secrets.token_urlsafe(32)
    logger.warning(
        "SESSION_SECRET no está definido en .env — usando un secreto aleatorio generado en "
        "memoria. Las sesiones del dashboard se invalidarán en cada reinicio del proceso. "
        "Define SESSION_SECRET para sesiones persistentes en producción."
    )

# Cabeceras de seguridad en toda respuesta. El panel es interno: noindex para
# que ningún buscador lo liste; CSP bloquea orígenes ajenos y el embebido en
# iframes (clickjacking). script-src SIN 'unsafe-inline': todo el JS vive en
# static/js y las plantillas solo le pasan datos (JSON inerte o data-*), así que
# un texto que se cuele al HTML —el nombre que un cliente escribe por WhatsApp—
# no puede ejecutarse. tests/test_separacion_front.py lo fija. style-src
# conserva 'unsafe-inline' solo por los colores que llegan como dato
# (style="--c: …") y por el SVG que JointJS arma con estilos en línea.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Robots-Tag": "noindex, nofollow",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'; "
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
    if request.url.path.startswith("/crm/api/"):
        # Conversaciones de clientes: ni el navegador ni el proxy del VPS deben guardarlas. Sin
        # ninguna directiva, la frescura queda al criterio heurístico de quien esté en medio, y
        # así es como un asesor acaba mirando un hilo viejo sin enterarse. `setdefault` respeta
        # el no-store que el stream de avisos ya manda por su cuenta.
        response.headers.setdefault("Cache-Control", "no-store")
    return response


async def _fetch_sin_html(request, call_next):
    """Un POST hecho por fetch no necesita la página a la que redirige, solo saber cuál es.

    Los POST del panel contestan 303 para que el formulario siga sirviendo sin JS. Pero quien los
    llama es `fetch` (static/js/crm.js::accionBandeja), que sigue el redirect y termina
    descargando /crm entero —32 KB medidos, en dos peticiones más— únicamente para leer el `?err=`
    de la URL final. Con `X-Requested-With: fetch` la respuesta se convierte en 204 y el destino
    viaja en `X-Destino`: el mismo contrato, sin el HTML.

    Va aquí y no en app/inbox.py a propósito: ahí habría que tocar los doce `RedirectResponse`
    del módulo dueño de la pausa del bot, la ventana de 24 h y el sellado de first_response_at, y
    reescribir las aserciones de seis archivos de pruebas. El 303 se queda intacto para todos:
    esto solo lo reescribe para quien pide explícitamente que no le manden la página.
    """
    response = await call_next(request)
    if response.status_code == 303 and request.headers.get("x-requested-with") == "fetch":
        return Response(status_code=204, headers={"X-Destino": response.headers.get("location", "")})
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
    #
    # ponytail: el COUNT corre en el event loop y NO se manda a threadpool, pese
    # a que una auditoría lo pidió (rechazado en septiembre de 2026,
    # docs/PLAN-CORRECCION-AUDITORIA.md). Ya está medido en el repo y en el otro
    # sentido: stripe_webhook.py documenta que el salto a threadpool cuesta más
    # que el SELECT que ahorraría. Además esto solo corre con sesión iniciada —
    # el webhook de WhatsApp, que es el camino con presión real, nunca lo toca.
    # Reabrir con una medición del loop bajo carga concurrente del panel, no
    # antes.
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
# OJO al agregar middlewares: GZipMiddleware rompe el stream de avisos del CRM
# (/crm/api/eventos) — comprime por bloques y los eventos se quedan en el búfer hasta llenarlo.
# Es un fallo mudo: el panel no da error, simplemente vuelve a enterarse tarde. Si algún día se
# comprime el panel, hay que excluir text/event-stream.
app.middleware("http")(_fetch_sin_html)  # dentro de _security_headers: su 204 también se sella
app.middleware("http")(_security_headers)

# ponytail: routers SIN `prefix` ni `tags`, cada archivo con sus rutas absolutas.
# Una auditoría pidió agregarlos y se rechazó (septiembre de 2026,
# docs/PLAN-CORRECCION-AUDITORIA.md): `prefix` cambia las URLs, y estas están
# cableadas en las plantillas Jinja y —lo caro— en los webhooks ya configurados
# del lado de Meta, Twilio y Stripe, donde un cambio no se despliega, se
# reconfigura a mano. El beneficio que se buscaba era ordenar /docs, que arriba
# quedó cerrado. Reabrir solo si algún día se publica el esquema.
app.include_router(webhook.router)
app.include_router(stripe_webhook.router)
app.include_router(dashboard.router)
app.include_router(cobranza_panel.router)
app.include_router(inbox.router)
app.include_router(crm.router)
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


def on_startup() -> None:
    """Todo lo que corre una vez, antes de atender el primer request.

    Sigue llamándose así —y sigue siendo síncrona— aunque ahora la invoque el
    `lifespan` de abajo: el nombre es carga documental (`devops-senior` lo cita
    siete veces, y `deploy/pull-deploy.sh` depende de que termine dentro del
    presupuesto de 60 s del health check). Lo que cambió es QUIÉN la llama, no
    qué hace ni en qué orden.
    """
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
async def health() -> dict:
    # `async` a propósito: un `def` lo mandaría al threadpool que comparte con el
    # webhook y las rutas del panel, y con el pool saturado por Ollama el cron del
    # deploy leería «caído» un servicio que solo está ocupado (auditoría, IA-4).
    return {"status": "ok"}
