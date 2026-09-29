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
from app.auth import alcance, is_admin, is_admin_global
from app.config import CREDENCIALES, settings
from app import credenciales
from app.credenciales import CredencialIlegible, credencial
from app.db import get_session
from app.models import (
    CrmColumna,
    CrmEtapa,
    CrmUbicacion,
    AgentConfig,
    InfoDoc,
    AdminAudit,
    Alert,
    CatalogAsesor,
    CatalogAudit,
    CatalogCategory,
    CatalogService,
    Client,
    ClientNote,
    ClienteAsesor,
    Cobro,
    Conversation,
    EventoPasarela,
    Feedback,
    FollowUp,
    IntentoCobro,
    Invitation,
    Label,
    LoginEvent,
    Message,
    NluEvent,
    OFICINA_TLAXCALA_ID,
    PaymentMethod,
    PlanCobro,
    Plantilla,
    SavedReply,
    SynonymProposal,
    TeamMessage,
    ChatRoom,
    ChatMember,
    MessageReaction,
    MessageReminder,
    Ticket,
    TicketActivity,
    TicketLabel,
    User,
    WebSession,
)
from app.utils import celda_csv

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
    (CatalogAsesor, "catalog_asesores"),
    (NluEvent, "nlu_events"),
    (AgentConfig, "agent_config"),
    (InfoDoc, "info_docs"),
    (Feedback, "feedback"),
    (SynonymProposal, "synonym_proposals"),
    (Alert, "alerts"),
    (Plantilla, "message_templates"),
    (ChatRoom, "chat_rooms"),
    (ChatMember, "chat_members"),
    (MessageReaction, "message_reactions"),
    (MessageReminder, "message_reminders"),
    # Estas tres ya se espejaban (DDL + archive_* + listener + backfill) pero
    # faltaban aquí, así que /system las daba por buenas sin contarlas: una
    # divergencia en las notas del cliente era invisible. Ver los CINCO lugares
    # que hay que tocar al agregar una tabla espejada (CLAUDE.md).
    (ClientNote, "client_notes"),
    (FollowUp, "followups"),
    (SavedReply, "saved_replies"),
    # Cobranza: las cinco tablas del módulo de Stripe, espejadas completas.
    (Cobro, "cobros"),
    (IntentoCobro, "intentos_cobro"),
    (EventoPasarela, "eventos_pasarela"),
    (PlanCobro, "planes_cobro"),
    (PaymentMethod, "payment_methods"),
    # Etiquetas (fase 1).
    (Label, "labels"),
    (TicketLabel, "ticket_labels"),
    # Embudo global del CRM (/crm): la plantilla del tablero de cada quien.
    (CrmEtapa, "crm_etapas"),
    # El tablero personal (app/tablero.py): columnas y ubicaciones de cada quien.
    (CrmColumna, "crm_columnas"),
    (CrmUbicacion, "crm_ubicaciones"),
    # La cartera de clientes de cada colaborador (app/cartera.py).
    (ClienteAsesor, "cliente_asesores"),
]


def _admin_or_redirect(request: Request):
    user = request.session.get("user")
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    # Solo el admin GLOBAL: es de todas las oficinas (auth.is_admin_global).
    if not is_admin_global(user):
        return None, RedirectResponse("/", status_code=303)
    return user, None


def _cualquier_admin_or_redirect(request: Request):
    """Global o de oficina: para lo que YA acota por oficina (bitácora, accesos
    y reportes CSV). Quien la use responde por acotar lo que pinta."""
    user = request.session.get("user")
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    if not is_admin(user):
        return None, RedirectResponse("/", status_code=303)
    return user, None


def usuarios_de_su_alcance(admin: str) -> set[str] | None:
    """Los usuarios cuyas acciones y accesos puede ver este admin; `None` =
    todos (admin global). Para tablas que solo tienen un USERNAME, sin oficina
    (AdminAudit, LoginEvent, WebSession): lo que hizo alguien que no es de su
    oficina —un admin global, el sistema, un usuario inexistente que intentó
    entrar— no se le puede atribuir, y queda para el global."""
    vista = alcance(admin)
    if vista.ve_todas:
        return None
    with get_session() as session:
        return set(session.exec(
            select(User.username).where(User.office_id == vista.office_id)
        ).all())


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
            # Igualdad estricta: un espejo con filas DE MÁS también es
            # divergencia (teléfonos crudos duplicados, p.ej.) — el ">=" de
            # antes lo escondía y /system decía "al día" con 7 vs 14.
            "ok": remote[name] is not None and remote[name] == local[name],
        }
        for name in local
    ]



# Las dos llaves se resuelven POR SEPARADO aunque compartan el `and`: una está
# marcada compartida y la otra es por oficina, y con filas reales (PR 5) pueden
# venir de fuentes distintas. /system muestra SOLO el booleano: nunca el valor,
# nunca la pista, nunca la longitud.
#
# ponytail: office_id fijo a Tlaxcala. Con varias oficinas un booleano GLOBAL
# deja de significar algo (una configurada y otra no); el corte natural es una
# fila por oficina, y es del PR 5.
def _whatsapp_configurado() -> bool:
    # Las dos claves LITERALES, no una tupla recorrida: tests/
    # test_credenciales_inventario.py audita por nombre y una clave en variable
    # la deja fuera del censo.
    try:
        return bool(
            credencial("WHATSAPP_TOKEN", office_id=OFICINA_TLAXCALA_ID)
            and credencial("WHATSAPP_PHONE_NUMBER_ID", office_id=OFICINA_TLAXCALA_ID)
        )
    except CredencialIlegible:
        # Ilegible NO es «configurada»: el bot no puede enviar nada con ella.
        return False


def _credenciales_ilegibles() -> list[str]:
    """Las llaves con fila viva que no se pueden descifrar. Solo el NOMBRE.

    Usa `estado()` —el mismo resolvedor que pinta /oficinas— en vez de repetir
    aquí el try/except: el cuarto estado se decide en UN lugar, y así esta
    página y la de detalle nunca se contradicen.
    """
    return [
        clave for clave in CREDENCIALES
        if credenciales.estado(clave, office_id=OFICINA_TLAXCALA_ID)[0] == credenciales.ILEGIBLE
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
        # ponytail: office_id fijo a Tlaxcala. Con varias oficinas un booleano
        # GLOBAL deja de significar algo (una configurada y otra no); el corte
        # natural es una fila por oficina en /system, y es del PR 3/5.
        # Las dos llaves se resuelven POR SEPARADO aunque compartan el `and`:
        # una está marcada compartida y la otra es por oficina, y con filas
        # reales (PR 5) pueden venir de fuentes distintas.
        # Sin `session=`: `info` se arma aquí y la única sesión de la vista la
        # abre mirror_status() por dentro, después.
        # /system sigue mostrando SOLO el booleano: nunca el valor, nunca la
        # pista, nunca la longitud.
        #
        # El CUARTO estado, que el PR 3 estrena: con una fila viva y una
        # CREDENTIALS_KEY cambiada, estas lecturas levantan CredencialIlegible y
        # esta página —la que el operador abre PRECISAMENTE para diagnosticar
        # eso— contestaba 500 en vez de decírselo. Ahora lo dice. El detalle por
        # llave vive en /oficinas/{id}; aquí basta con la alarma.
        "whatsapp_configured": _whatsapp_configurado(),
        "credenciales_ilegibles": _credenciales_ilegibles(),
    }
    return templates.TemplateResponse(
        request, "system.html",
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
    admin, redirect = _cualquier_admin_or_redirect(request)
    if redirect:
        return redirect
    quienes = usuarios_de_su_alcance(admin)
    with get_session() as session:
        consulta = select(AdminAudit)
        if quienes is not None:
            consulta = consulta.where(AdminAudit.admin.in_(sorted(quienes)))
        entries = session.exec(
            consulta.order_by(AdminAudit.created_at.desc()).limit(min(limit, 1000))
        ).all()
    return templates.TemplateResponse(
        request, "audit.html", {"request": request, "user": admin, "entries": entries}
    )


# Inyección de fórmulas: la regla vive en utils.celda_csv, compartida con el
# CSV en streaming de cobranza — dos copias de la lista de caracteres es la
# forma en que una de las dos se queda sin el tabulador (auditoría 2026-09,
# INJ-1/INJ-3). El alias conserva el nombre que ya usan los exportes de aquí.
_celda_segura = celda_csv


def _csv_response(filename: str, header: list[str], rows) -> Response:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows([_celda_segura(c) for c in fila] for fila in rows)
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
    admin, redirect = _cualquier_admin_or_redirect(request)
    if redirect:
        return redirect
    vista = alcance(admin)
    with get_session() as session:
        tickets = session.exec(
            vista.filtrar(select(Ticket), Ticket.office_id).order_by(Ticket.created_at)
        ).all()
        # Etiquetas por folio en un solo viaje (no una consulta por fila).
        pares = session.exec(
            vista.filtrar(
                select(TicketLabel.ticket_id, Label.nombre)
                .join(Label, Label.id == TicketLabel.label_id)
                .join(Ticket, Ticket.id == TicketLabel.ticket_id),
                Ticket.office_id,
            )
            .where(TicketLabel.habilitado == True, Label.habilitado == True)  # noqa: E712
        ).all()
    etiquetas: dict[int, list[str]] = {}
    for tid, nombre in pares:
        etiquetas.setdefault(tid, []).append(nombre)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return _csv_response(
        f"solicitudes-{stamp}.csv",
        ["id", "telefono", "cliente", "categoria", "servicio", "precio", "estado",
         "escalado", "asignado_a", "etiquetas", "habilitado", "creado", "actualizado",
         "primera_respuesta", "resuelto"],
        (
            [t.id, t.phone, t.client_name or "", t.category, t.service_name, t.price_info,
             t.status.value, int(t.escalado), t.assigned_to or "",
             "|".join(sorted(etiquetas.get(t.id or 0, []))), int(t.habilitado),
             t.created_at.isoformat(sep=" ", timespec="seconds"),
             t.updated_at.isoformat(sep=" ", timespec="seconds"),
             t.first_response_at.isoformat(sep=" ", timespec="seconds")
             if t.first_response_at else "",
             t.resolved_at.isoformat(sep=" ", timespec="seconds")
             if t.resolved_at else ""]
            for t in tickets
        ),
    )


@router.get("/export/activity.csv")
def export_activity(request: Request):
    admin, redirect = _cualquier_admin_or_redirect(request)
    if redirect:
        return redirect
    vista = alcance(admin)
    with get_session() as session:
        # La oficina del FOLIO, igual que el tablero de /activity.
        acts = session.exec(
            vista.filtrar(
                select(TicketActivity).join(Ticket, Ticket.id == TicketActivity.ticket_id),
                Ticket.office_id,
            ).order_by(TicketActivity.created_at)
        ).all()
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
