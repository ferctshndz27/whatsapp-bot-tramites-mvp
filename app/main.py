import logging
import secrets

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from app import (
    activity,
    archive,
    authlog,
    catalog_admin,
    clients,
    dashboard,
    inbox,
    kb,
    my,
    replies,
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

app = FastAPI(title="Bot de trámites — MVP")

session_secret = settings.session_secret
if not session_secret:
    session_secret = secrets.token_urlsafe(32)
    logger.warning(
        "SESSION_SECRET no está definido en .env — usando un secreto aleatorio generado en "
        "memoria. Las sesiones del dashboard se invalidarán en cada reinicio del proceso. "
        "Define SESSION_SECRET para sesiones persistentes en producción."
    )

# Registrado ANTES que SessionMiddleware a propósito: en Starlette el último
# middleware agregado queda más afuera, y este necesita request.session (es
# decir, correr por DENTRO de SessionMiddleware).
app.middleware("http")(authlog.session_activity_middleware)
app.add_middleware(SessionMiddleware, secret_key=session_secret)
app.include_router(webhook.router)
app.include_router(dashboard.router)
app.include_router(inbox.router)
app.include_router(team_chat.router)
app.include_router(activity.router)
app.include_router(users.router)
app.include_router(catalog_admin.router)
app.include_router(system.router)
app.include_router(synonym_miner.router)
app.include_router(my.router)
app.include_router(clients.router)
app.include_router(replies.router)
app.include_router(kb.router)
app.include_router(authlog.router)


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    dashboard.seed_dashboard_user(settings)
    archive.init()
    activity.backfill_closed_tickets()
    archive.backfill()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
