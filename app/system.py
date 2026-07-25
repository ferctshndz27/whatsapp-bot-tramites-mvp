"""Panel de sistema para administradores: salud del espejo MSSQL,
sincronización manual, bitácora de auditoría y exportes CSV.

Convierte en un clic lo que hoy son sesiones de SSH: comparar filas
SQLite vs MSSQL, disparar el backfill sin reiniciar el contenedor y
bajar reportes para Excel.
"""

import csv
import io
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from app.templating import templates
from sqlalchemy import func, text
from sqlmodel import select

from app import archive
from app.audit import log_admin
from app.auth import is_admin
from app.config import settings
from app.db import get_session
from app.models import (
    AdminAudit,
    CatalogAudit,
    CatalogCategory,
    CatalogService,
    Client,
    Conversation,
    Feedback,
    Invitation,
    LoginEvent,
    Message,
    NluEvent,
    SynonymProposal,
    TeamMessage,
    Ticket,
    TicketActivity,
    User,
    WebSession,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_STARTED_AT = datetime.now(timezone.utc)

# Modelo SQLite -> tabla espejo en MSSQL
MIRROR_TABLES = [
    (Client, "wa_clients"),
    (Message, "wa_messages"),
    (TeamMessage, "team_messages"),
    (User, "app_users"),
    (Ticket, "tickets"),
    (TicketActivity, "ticket_activity"),
    (Invitation, "invitations"),
    (Conversation, "conversations"),
    (AdminAudit, "admin_audit"),
    (LoginEvent, "login_events"),
    (WebSession, "web_sessions"),
    (CatalogCategory, "catalog_categories"),
    (CatalogService, "catalog_services"),
    (CatalogAudit, "catalog_audit"),
    (NluEvent, "nlu_events"),
    (Feedback, "feedback"),
    (SynonymProposal, "synonym_proposals"),
]


def _admin_or_redirect(request: Request):
    user = request.session.get("user")
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    if not is_admin(user):
        return None, RedirectResponse("/", status_code=303)
    return user, None


def mirror_status() -> list[dict]:
    """Conteo de filas por tabla en SQLite vs MSSQL, con bandera de mismatch."""
    with get_session() as session:
        local = {
            mssql_name: session.exec(select(func.count()).select_from(model)).one()
            for model, mssql_name in MIRROR_TABLES
        }

    remote: dict[str, int | None] = {name: None for name in local}
    if archive.enabled():
        try:
            with archive._get_engine().connect() as conn:
                for name in remote:
                    remote[name] = conn.execute(
                        text(f"SELECT COUNT(*) FROM {name}")  # nombres fijos de MIRROR_TABLES
                    ).scalar()
        except Exception as exc:
            logger.warning("No se pudo consultar MSSQL para el estado del espejo: %s", exc)

    return [
        {
            "table": name,
            "sqlite": local[name],
            "mssql": remote[name],
            "ok": remote[name] is not None and remote[name] >= local[name],
        }
        for name in local
    ]


def _dir_size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    if path.is_file():
        return path.stat().st_size / 1_048_576
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1_048_576


def _deploy_info() -> str:
    # pull-deploy.sh escribe "<sha> <fecha>" en data/deploy-info (junto a la DB).
    info = Path(settings.db_path).parent / "deploy-info"
    try:
        return info.read_text().strip()
    except OSError:
        return "n/d (aún sin registro del deploy)"


def _backup_info() -> dict:
    """Antigüedad del último respaldo de la base. El backup automático está
    pendiente (infra del VPS); esto lo hace OBSERVABLE: si algún día se escribe
    un dump en data/backups/, aquí se ve su antigüedad y si está viejo."""
    backups = Path(settings.db_path).parent / "backups"
    latest = None
    if backups.is_dir():
        files = [f for f in backups.iterdir() if f.is_file()]
        latest = max(files, key=lambda f: f.stat().st_mtime, default=None)
    if latest is None:
        return {"exists": False, "age_hours": None, "stale": True}
    age_h = (datetime.now(timezone.utc).timestamp() - latest.stat().st_mtime) / 3600
    return {"exists": True, "age_hours": round(age_h, 1), "stale": age_h > 48}


def _disk_free() -> dict:
    usage = shutil.disk_usage(Path(settings.db_path).parent)
    free_pct = round(usage.free / usage.total * 100)
    return {
        "free_pct": free_pct,
        "free_gb": round(usage.free / 1_073_741_824, 1),
        "low": free_pct < 15,
    }


@router.get("/system")
def system_view(request: Request):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect

    db_file = Path(settings.db_path)
    uptime_h = (datetime.now(timezone.utc) - _STARTED_AT).total_seconds() / 3600
    info = {
        "uptime_hours": round(uptime_h, 1),
        "deploy": _deploy_info(),
        "db_mb": round(_dir_size_mb(db_file), 2),
        "wal_mb": round(_dir_size_mb(db_file.with_name(db_file.name + "-wal")), 2),
        "media_mb": round(_dir_size_mb(Path(settings.media_dir)), 2),
        "backup": _backup_info(),
        "disk": _disk_free(),
        "mssql_enabled": archive.enabled(),
        "whatsapp_configured": bool(settings.whatsapp_token and settings.whatsapp_phone_number_id),
    }
    return templates.TemplateResponse(
        "system.html",
        {
            "request": request,
            "user": admin,
            "info": info,
            "mirror": mirror_status(),
            "synced": request.query_params.get("synced"),
        },
    )


@router.post("/system/sync")
def system_sync(request: Request):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    archive.backfill()
    log_admin(admin, "espejo:sync")
    logger.info("Backfill manual del espejo MSSQL disparado por %s", admin)
    return RedirectResponse("/system?synced=1", status_code=303)


@router.get("/audit")
def audit_view(request: Request, limit: int = 200):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    with get_session() as session:
        entries = session.exec(
            select(AdminAudit).order_by(AdminAudit.created_at.desc()).limit(min(limit, 1000))
        ).all()
    return templates.TemplateResponse(
        "audit.html", {"request": request, "user": admin, "entries": entries}
    )


def _csv_response(filename: str, header: list[str], rows) -> Response:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    # BOM UTF-8: sin él, Excel (es-MX) muestra mal los acentos.
    # ponytail: CSV completo en memoria — a este volumen sobra; pasar a
    # StreamingResponse con generador si algún día son cientos de miles de filas.
    payload = "﻿" + buffer.getvalue()
    return Response(
        content=payload,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/tickets.csv")
def export_tickets(request: Request):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    with get_session() as session:
        tickets = session.exec(select(Ticket).order_by(Ticket.created_at)).all()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return _csv_response(
        f"solicitudes-{stamp}.csv",
        ["id", "telefono", "cliente", "categoria", "servicio", "precio", "estado",
         "escalado", "asignado_a", "habilitado", "creado", "actualizado"],
        (
            [t.id, t.phone, t.client_name or "", t.category, t.service_name, t.price_info,
             t.status.value, int(t.escalado), t.assigned_to or "", int(t.habilitado),
             t.created_at.isoformat(sep=" ", timespec="seconds"),
             t.updated_at.isoformat(sep=" ", timespec="seconds")]
            for t in tickets
        ),
    )


@router.get("/export/activity.csv")
def export_activity(request: Request):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    with get_session() as session:
        acts = session.exec(select(TicketActivity).order_by(TicketActivity.created_at)).all()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return _csv_response(
        f"actividad-{stamp}.csv",
        ["id", "ticket_id", "colaborador", "accion", "fecha"],
        (
            [a.id, a.ticket_id, a.username, a.action,
             a.created_at.isoformat(sep=" ", timespec="seconds")]
            for a in acts
        ),
    )
