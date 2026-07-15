import logging
import secrets

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from app import dashboard, inbox, team_chat, webhook
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

app.add_middleware(SessionMiddleware, secret_key=session_secret)
app.include_router(webhook.router)
app.include_router(dashboard.router)
app.include_router(inbox.router)
app.include_router(team_chat.router)


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    dashboard.seed_dashboard_user(settings)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
