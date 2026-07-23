"""Archivo espejo en MSSQL de toda la data operativa.

SQLite sigue siendo la base operativa del bot; este módulo replica a SQL
Server cada entidad: clientes, mensajes de WhatsApp, chat de equipo,
colaboradores (contraseña como hash bcrypt — nunca en claro), invitaciones,
solicitudes, actividad y estado de conversaciones.

Escritura best-effort: si MSSQL no responde, el bot sigue funcionando y solo
se registra el fallo en el log. Al arrancar corre un backfill idempotente que
sincroniza cualquier fila que falte (histórico previo al espejo o cambios
hechos mientras MSSQL estaba caído).

Se activa definiendo MSSQL_HOST y MSSQL_SA_PASSWORD (ver .env).
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import create_engine, event, text

from app.config import settings

logger = logging.getLogger(__name__)

_engine = None
_ready = False

_SCHEMA_DDL = [
    """
    IF OBJECT_ID('wa_clients', 'U') IS NULL
    CREATE TABLE wa_clients (
        phone      NVARCHAR(32)  NOT NULL PRIMARY KEY,
        name       NVARCHAR(200) NULL,
        habilitado BIT           NOT NULL DEFAULT 1,
        created_at DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME()
    )
    """,
    """
    IF OBJECT_ID('wa_messages', 'U') IS NULL
    CREATE TABLE wa_messages (
        id            BIGINT IDENTITY(1,1) PRIMARY KEY,
        source_id     BIGINT        NOT NULL UNIQUE,
        phone         NVARCHAR(32)  NOT NULL,
        direction     NVARCHAR(3)   NOT NULL,
        body          NVARCHAR(MAX) NOT NULL,
        wa_message_id NVARCHAR(128) NULL,
        wa_status     NVARCHAR(32)  NULL,
        media_path    NVARCHAR(300) NULL,
        sent_by       NVARCHAR(64)  NULL,
        habilitado    BIT           NOT NULL DEFAULT 1,
        created_at    DATETIME2     NOT NULL,
        CONSTRAINT fk_wa_messages_client
            FOREIGN KEY (phone) REFERENCES wa_clients(phone)
    )
    """,
    """
    IF OBJECT_ID('team_users', 'U') IS NULL
    CREATE TABLE team_users (
        username NVARCHAR(64) NOT NULL PRIMARY KEY
    )
    """,
    """
    IF OBJECT_ID('team_messages', 'U') IS NULL
    CREATE TABLE team_messages (
        id         BIGINT IDENTITY(1,1) PRIMARY KEY,
        source_id  BIGINT        NOT NULL UNIQUE,
        author     NVARCHAR(64)  NOT NULL,
        body       NVARCHAR(MAX) NOT NULL,
        media_path NVARCHAR(300) NULL,
        habilitado BIT           NOT NULL DEFAULT 1,
        created_at DATETIME2     NOT NULL,
        CONSTRAINT fk_team_messages_user
            FOREIGN KEY (author) REFERENCES team_users(username)
    )
    """,
    # Colaboradores. La contraseña se guarda tal cual está en SQLite: un hash
    # bcrypt irreversible, nunca la contraseña en texto plano.
    """
    IF OBJECT_ID('app_users', 'U') IS NULL
    CREATE TABLE app_users (
        source_id     BIGINT        NOT NULL PRIMARY KEY,
        username      NVARCHAR(64)  NOT NULL UNIQUE,
        password_hash NVARCHAR(200) NOT NULL,
        is_admin      BIT           NOT NULL DEFAULT 0,
        habilitado    BIT           NOT NULL DEFAULT 1,
        estado        NVARCHAR(20)  NOT NULL DEFAULT 'disponible',
        phone         NVARCHAR(20)  NULL
    )
    """,
    """
    IF OBJECT_ID('followups', 'U') IS NULL
    CREATE TABLE followups (
        source_id  BIGINT        NOT NULL PRIMARY KEY,
        ticket_id  BIGINT        NOT NULL,
        username   NVARCHAR(64)  NOT NULL,
        note       NVARCHAR(500) NOT NULL,
        due_at     DATETIME2     NOT NULL,
        done_at    DATETIME2     NULL,
        habilitado BIT           NOT NULL DEFAULT 1,
        created_at DATETIME2     NOT NULL
    )
    """,
    """
    IF OBJECT_ID('client_notes', 'U') IS NULL
    CREATE TABLE client_notes (
        source_id  BIGINT        NOT NULL PRIMARY KEY,
        phone      NVARCHAR(32)  NOT NULL,
        author     NVARCHAR(64)  NOT NULL,
        body       NVARCHAR(MAX) NOT NULL,
        habilitado BIT           NOT NULL DEFAULT 1,
        created_at DATETIME2     NOT NULL
    )
    """,
    """
    IF OBJECT_ID('saved_replies', 'U') IS NULL
    CREATE TABLE saved_replies (
        source_id  BIGINT        NOT NULL PRIMARY KEY,
        username   NVARCHAR(64)  NOT NULL,
        title      NVARCHAR(100) NOT NULL,
        body      NVARCHAR(MAX) NOT NULL,
        habilitado BIT           NOT NULL DEFAULT 1,
        created_at DATETIME2     NOT NULL
    )
    """,
    """
    IF OBJECT_ID('invitations', 'U') IS NULL
    CREATE TABLE invitations (
        source_id  BIGINT       NOT NULL PRIMARY KEY,
        token      NVARCHAR(64) NOT NULL,
        phone      NVARCHAR(32) NULL,
        created_by NVARCHAR(64) NOT NULL,
        created_at DATETIME2    NOT NULL,
        used_at    DATETIME2    NULL,
        used_by    NVARCHAR(64) NULL,
        habilitado BIT          NOT NULL DEFAULT 1
    )
    """,
    # Sin FKs en las tablas de solicitud/actividad a propósito: es un archivo
    # de solo lectura y las FKs complicarían el orden del backfill sin aportar
    # integridad que SQLite (la base operativa) no garantice ya.
    """
    IF OBJECT_ID('tickets', 'U') IS NULL
    CREATE TABLE tickets (
        source_id    BIGINT        NOT NULL PRIMARY KEY,
        phone        NVARCHAR(32)  NOT NULL,
        client_name  NVARCHAR(200) NULL,
        category     NVARCHAR(100) NOT NULL,
        service_id   NVARCHAR(64)  NOT NULL,
        service_name NVARCHAR(200) NOT NULL,
        price_info   NVARCHAR(200) NOT NULL,
        status       NVARCHAR(20)  NOT NULL,
        escalado     BIT           NOT NULL DEFAULT 0,
        notes        NVARCHAR(MAX) NULL,
        assigned_to  NVARCHAR(64)  NULL,
        habilitado   BIT           NOT NULL DEFAULT 1,
        created_at   DATETIME2     NOT NULL,
        updated_at   DATETIME2     NOT NULL
    )
    """,
    """
    IF OBJECT_ID('ticket_activity', 'U') IS NULL
    CREATE TABLE ticket_activity (
        source_id  BIGINT        NOT NULL PRIMARY KEY,
        ticket_id  BIGINT        NOT NULL,
        username   NVARCHAR(64)  NOT NULL,
        action     NVARCHAR(100) NOT NULL,
        habilitado BIT           NOT NULL DEFAULT 1,
        created_at DATETIME2     NOT NULL
    )
    """,
    """
    IF OBJECT_ID('admin_audit', 'U') IS NULL
    CREATE TABLE admin_audit (
        source_id  BIGINT        NOT NULL PRIMARY KEY,
        admin      NVARCHAR(64)  NOT NULL,
        action     NVARCHAR(100) NOT NULL,
        target     NVARCHAR(200) NULL,
        habilitado BIT           NOT NULL DEFAULT 1,
        created_at DATETIME2     NOT NULL
    )
    """,
    """
    IF OBJECT_ID('login_events', 'U') IS NULL
    CREATE TABLE login_events (
        source_id  BIGINT        NOT NULL PRIMARY KEY,
        username   NVARCHAR(64)  NOT NULL,
        success    BIT           NOT NULL,
        reason     NVARCHAR(100) NULL,
        ip         NVARCHAR(64)  NULL,
        geo        NVARCHAR(200) NULL,
        device     NVARCHAR(100) NULL,
        user_agent NVARCHAR(300) NULL,
        habilitado BIT           NOT NULL DEFAULT 1,
        created_at DATETIME2     NOT NULL
    )
    """,
    """
    IF OBJECT_ID('web_sessions', 'U') IS NULL
    CREATE TABLE web_sessions (
        source_id    BIGINT        NOT NULL PRIMARY KEY,
        token        NVARCHAR(64)  NOT NULL,
        username     NVARCHAR(64)  NOT NULL,
        ip           NVARCHAR(64)  NULL,
        geo          NVARCHAR(200) NULL,
        device       NVARCHAR(100) NULL,
        user_agent   NVARCHAR(300) NULL,
        created_at   DATETIME2     NOT NULL,
        last_seen_at DATETIME2     NOT NULL,
        ended_at     DATETIME2     NULL,
        habilitado   BIT           NOT NULL DEFAULT 1
    )
    """,
    """
    IF OBJECT_ID('catalog_categories', 'U') IS NULL
    CREATE TABLE catalog_categories (
        id         NVARCHAR(64)  NOT NULL PRIMARY KEY,
        name       NVARCHAR(200) NOT NULL,
        position   INT           NOT NULL DEFAULT 0,
        habilitado BIT           NOT NULL DEFAULT 1
    )
    """,
    """
    IF OBJECT_ID('catalog_services', 'U') IS NULL
    CREATE TABLE catalog_services (
        id            NVARCHAR(64)  NOT NULL PRIMARY KEY,
        category_id   NVARCHAR(64)  NOT NULL,
        name          NVARCHAR(200) NOT NULL,
        price_info    NVARCHAR(200) NOT NULL,
        synonyms_json NVARCHAR(MAX) NOT NULL,
        requirements  NVARCHAR(MAX) NOT NULL,
        position      INT           NOT NULL DEFAULT 0,
        habilitado    BIT           NOT NULL DEFAULT 1
    )
    """,
    """
    IF OBJECT_ID('catalog_audit', 'U') IS NULL
    CREATE TABLE catalog_audit (
        source_id  BIGINT        NOT NULL PRIMARY KEY,
        admin      NVARCHAR(64)  NOT NULL,
        entity     NVARCHAR(20)  NOT NULL,
        entity_id  NVARCHAR(64)  NOT NULL,
        field      NVARCHAR(30)  NOT NULL,
        old_value  NVARCHAR(MAX) NULL,
        new_value  NVARCHAR(MAX) NULL,
        habilitado BIT           NOT NULL DEFAULT 1,
        created_at DATETIME2     NOT NULL
    )
    """,
    """
    IF OBJECT_ID('conversations', 'U') IS NULL
    CREATE TABLE conversations (
        phone           NVARCHAR(32)  NOT NULL PRIMARY KEY,
        state           NVARCHAR(50)  NOT NULL,
        context_json    NVARCHAR(MAX) NOT NULL,
        failed_attempts INT           NOT NULL,
        habilitado      BIT           NOT NULL DEFAULT 1,
        updated_at      DATETIME2     NOT NULL
    )
    """,
]

# ponytail: igual que db.py, sin alembic — columnas agregadas después del
# lanzamiento se suman a mano vía ALTER TABLE si no existen.
_COLUMNS_ADDED_AFTER_LAUNCH = {
    "wa_messages": {"habilitado": "BIT NOT NULL DEFAULT 1"},
    "team_messages": {"habilitado": "BIT NOT NULL DEFAULT 1"},
    "wa_clients": {"habilitado": "BIT NOT NULL DEFAULT 1"},
    "app_users": {
        "habilitado": "BIT NOT NULL DEFAULT 1",
        "estado": "NVARCHAR(20) NOT NULL DEFAULT 'disponible'",
        "phone": "NVARCHAR(20) NULL",
    },
    "invitations": {"habilitado": "BIT NOT NULL DEFAULT 1"},
    "tickets": {
        "habilitado": "BIT NOT NULL DEFAULT 1",
        "agent_active": "BIT NOT NULL DEFAULT 1",
    },
    "ticket_activity": {"habilitado": "BIT NOT NULL DEFAULT 1"},
    "conversations": {"habilitado": "BIT NOT NULL DEFAULT 1"},
}


def enabled() -> bool:
    return bool(settings.mssql_host and settings.mssql_password)


def _get_engine():
    global _engine
    if _engine is None and enabled():
        url = (
            f"mssql+pymssql://{settings.mssql_user}:{settings.mssql_password}"
            f"@{settings.mssql_host}:{settings.mssql_port}/{settings.mssql_db}"
        )
        _engine = create_engine(url, pool_pre_ping=True)
    return _engine


def init() -> None:
    """Crea la base y las tablas si no existen. Llamar al arrancar la app."""
    global _ready
    if not enabled():
        logger.info("Archivo MSSQL deshabilitado (MSSQL_HOST/MSSQL_SA_PASSWORD sin definir)")
        return
    try:
        master_url = (
            f"mssql+pymssql://{settings.mssql_user}:{settings.mssql_password}"
            f"@{settings.mssql_host}:{settings.mssql_port}/master"
        )
        master = create_engine(master_url, isolation_level="AUTOCOMMIT")
        with master.connect() as conn:
            conn.execute(
                text(
                    f"IF DB_ID('{settings.mssql_db}') IS NULL "
                    f"CREATE DATABASE [{settings.mssql_db}]"
                )
            )
        master.dispose()

        engine = _get_engine()
        with engine.begin() as conn:
            for ddl in _SCHEMA_DDL:
                conn.execute(text(ddl))
            for table, columns in _COLUMNS_ADDED_AFTER_LAUNCH.items():
                for name, col_type in columns.items():
                    conn.execute(
                        text(
                            f"IF COL_LENGTH('{table}', '{name}') IS NULL "
                            f"ALTER TABLE {table} ADD {name} {col_type}"
                        )
                    )
        _ready = True
        logger.info("Archivo MSSQL listo (base %s en %s)", settings.mssql_db, settings.mssql_host)
    except Exception:
        # ponytail: si MSSQL no está listo al arrancar, se reintenta el
        # esquema en la primera escritura (ver _safe).
        logger.exception("No se pudo inicializar el archivo MSSQL; se reintentará al escribir")


def _naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _upsert_client(
    conn, phone: str, name: str | None = None, habilitado: bool | None = None
) -> None:
    row = conn.execute(
        text("SELECT name, habilitado FROM wa_clients WHERE phone = :p"), {"p": phone}
    ).first()
    if row is None:
        conn.execute(
            text(
                "INSERT INTO wa_clients (phone, name, habilitado, created_at)"
                " VALUES (:p, :n, :hab, :c)"
            ),
            {"p": phone, "n": name, "hab": habilitado if habilitado is not None else True,
             "c": datetime.utcnow()},
        )
        return
    updates = {}
    if name and row[0] != name:
        updates["name"] = name
    if habilitado is not None and bool(row[1]) != habilitado:
        updates["habilitado"] = habilitado
    if updates:
        set_clause = ", ".join(f"{c} = :{c}" for c in updates)
        conn.execute(
            text(f"UPDATE wa_clients SET {set_clause} WHERE phone = :p"), {**updates, "p": phone}
        )


def archive_wa_message(m) -> None:
    with _get_engine().begin() as conn:
        _upsert_client(conn, m.phone)
        conn.execute(
            text(
                "INSERT INTO wa_messages (source_id, phone, direction, body, wa_message_id,"
                " wa_status, media_path, sent_by, habilitado, created_at)"
                " VALUES (:sid, :p, :d, :b, :wid, :ws, :mp, :sb, :hab, :c)"
            ),
            {
                "sid": m.id,
                "p": m.phone,
                "d": m.direction.value,
                "b": m.body,
                "wid": m.wa_message_id,
                "ws": m.wa_status,
                "mp": m.media_path,
                "sb": m.sent_by,
                "hab": m.habilitado,
                "c": _naive_utc(m.created_at),
            },
        )


def sync_wa_message(m) -> None:
    """Sincroniza wamid/estado de entrega/habilitado cuando el registro local se actualiza."""
    with _get_engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE wa_messages SET wa_message_id = :wid, wa_status = :ws, habilitado = :hab"
                " WHERE source_id = :sid"
            ),
            {"wid": m.wa_message_id, "ws": m.wa_status, "hab": m.habilitado, "sid": m.id},
        )


def archive_team_message(m) -> None:
    with _get_engine().begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM team_users WHERE username = :u"), {"u": m.author}
        ).first()
        if exists is None:
            conn.execute(text("INSERT INTO team_users (username) VALUES (:u)"), {"u": m.author})
        conn.execute(
            text(
                "INSERT INTO team_messages (source_id, author, body, media_path, habilitado, created_at)"
                " VALUES (:sid, :a, :b, :mp, :hab, :c)"
            ),
            {
                "sid": m.id,
                "a": m.author,
                "b": m.body,
                "mp": m.media_path,
                "hab": m.habilitado,
                "c": _naive_utc(m.created_at),
            },
        )


def sync_team_message(m) -> None:
    """Sincroniza el campo habilitado (soft-delete) de un mensaje de equipo."""
    with _get_engine().begin() as conn:
        conn.execute(
            text("UPDATE team_messages SET habilitado = :hab WHERE source_id = :sid"),
            {"hab": m.habilitado, "sid": m.id},
        )


def sync_client_name(c) -> None:
    with _get_engine().begin() as conn:
        _upsert_client(conn, c.phone, c.name, c.habilitado)


def _upsert(table: str, key_col: str, key_val, columns: dict) -> None:
    """UPSERT genérico por clave primaria: intenta UPDATE y, si no afectó
    filas, hace INSERT. Idempotente — sirve igual para insert, update y
    backfill (reprocesar una fila ya espejada no la duplica)."""
    with _get_engine().begin() as conn:
        params = {**columns, "_key": key_val}
        set_clause = ", ".join(f"{c} = :{c}" for c in columns)
        result = conn.execute(
            text(f"UPDATE {table} SET {set_clause} WHERE {key_col} = :_key"), params
        )
        if result.rowcount == 0:
            cols = ", ".join([key_col, *columns])
            vals = ", ".join([":_key", *(f":{c}" for c in columns)])
            conn.execute(text(f"INSERT INTO {table} ({cols}) VALUES ({vals})"), params)


def archive_user(u) -> None:
    _upsert(
        "app_users", "source_id", u.id,
        {
            "username": u.username,
            "password_hash": u.password_hash,
            "is_admin": u.is_admin,
            "habilitado": u.habilitado,
            "estado": u.estado,
            "phone": u.phone,
        },
    )


def archive_followup(f) -> None:
    _upsert(
        "followups", "source_id", f.id,
        {
            "ticket_id": f.ticket_id,
            "username": f.username,
            "note": f.note,
            "due_at": _naive_utc(f.due_at),
            "done_at": _naive_utc(f.done_at) if f.done_at else None,
            "habilitado": f.habilitado,
            "created_at": _naive_utc(f.created_at),
        },
    )


def archive_client_note(n) -> None:
    _upsert(
        "client_notes", "source_id", n.id,
        {
            "phone": n.phone,
            "author": n.author,
            "body": n.body,
            "habilitado": n.habilitado,
            "created_at": _naive_utc(n.created_at),
        },
    )


def archive_saved_reply(r) -> None:
    _upsert(
        "saved_replies", "source_id", r.id,
        {
            "username": r.username,
            "title": r.title,
            "body": r.body,
            "habilitado": r.habilitado,
            "created_at": _naive_utc(r.created_at),
        },
    )


def archive_invitation(i) -> None:
    _upsert(
        "invitations", "source_id", i.id,
        {
            "token": i.token,
            "phone": i.phone,
            "created_by": i.created_by,
            "created_at": _naive_utc(i.created_at),
            "used_at": _naive_utc(i.used_at) if i.used_at else None,
            "used_by": i.used_by,
            "habilitado": i.habilitado,
        },
    )


def archive_ticket(t) -> None:
    _upsert(
        "tickets", "source_id", t.id,
        {
            "phone": t.phone,
            "client_name": t.client_name,
            "category": t.category,
            "service_id": t.service_id,
            "service_name": t.service_name,
            "price_info": t.price_info,
            "status": t.status.value,
            "escalado": t.escalado,
            "notes": t.notes,
            "assigned_to": t.assigned_to,
            "created_at": _naive_utc(t.created_at),
            "updated_at": _naive_utc(t.updated_at),
            "habilitado": t.habilitado,
            "agent_active": t.agent_active,
        },
    )


def archive_activity(a) -> None:
    _upsert(
        "ticket_activity", "source_id", a.id,
        {
            "ticket_id": a.ticket_id,
            "username": a.username,
            "action": a.action,
            "created_at": _naive_utc(a.created_at),
            "habilitado": a.habilitado,
        },
    )


def archive_admin_audit(a) -> None:
    _upsert(
        "admin_audit", "source_id", a.id,
        {
            "admin": a.admin,
            "action": a.action,
            "target": a.target,
            "habilitado": a.habilitado,
            "created_at": _naive_utc(a.created_at),
        },
    )


def archive_login_event(e) -> None:
    _upsert(
        "login_events", "source_id", e.id,
        {
            "username": e.username,
            "success": e.success,
            "reason": e.reason,
            "ip": e.ip,
            "geo": e.geo,
            "device": e.device,
            "user_agent": e.user_agent,
            "habilitado": e.habilitado,
            "created_at": _naive_utc(e.created_at),
        },
    )


def archive_web_session(w) -> None:
    _upsert(
        "web_sessions", "source_id", w.id,
        {
            "token": w.token,
            "username": w.username,
            "ip": w.ip,
            "geo": w.geo,
            "device": w.device,
            "user_agent": w.user_agent,
            "created_at": _naive_utc(w.created_at),
            "last_seen_at": _naive_utc(w.last_seen_at),
            "ended_at": _naive_utc(w.ended_at) if w.ended_at else None,
            "habilitado": w.habilitado,
        },
    )


def archive_catalog_category(c) -> None:
    _upsert(
        "catalog_categories", "id", c.id,
        {"name": c.name, "position": c.position, "habilitado": c.habilitado},
    )


def archive_catalog_service(s) -> None:
    _upsert(
        "catalog_services", "id", s.id,
        {
            "category_id": s.category_id,
            "name": s.name,
            "price_info": s.price_info,
            "synonyms_json": s.synonyms_json,
            "requirements": s.requirements,
            "position": s.position,
            "habilitado": s.habilitado,
        },
    )


def archive_catalog_audit(a) -> None:
    _upsert(
        "catalog_audit", "source_id", a.id,
        {
            "admin": a.admin,
            "entity": a.entity,
            "entity_id": a.entity_id,
            "field": a.field,
            "old_value": a.old_value,
            "new_value": a.new_value,
            "habilitado": a.habilitado,
            "created_at": _naive_utc(a.created_at),
        },
    )


def archive_conversation(c) -> None:
    _upsert(
        "conversations", "phone", c.phone,
        {
            "state": c.state,
            "context_json": c.context_json,
            "failed_attempts": c.failed_attempts,
            "updated_at": _naive_utc(c.updated_at),
            "habilitado": c.habilitado,
        },
    )


def _safe(fn, target) -> None:
    if not enabled():
        return
    global _ready
    try:
        if not _ready:
            init()
        fn(target)
    except Exception as exc:
        logger.warning("No se pudo archivar en MSSQL (%s): %s", fn.__name__, exc)


def backfill() -> None:
    """Sincroniza a MSSQL toda la data que ya existe en SQLite. Idempotente
    (gracias al upsert), así que corre seguro en cada arranque: cubre el
    histórico previo al espejo y cualquier cambio hecho mientras MSSQL
    estuvo caído. Best-effort — no tumba el arranque si MSSQL no responde."""
    if not enabled():
        return
    from app.db import get_session
    from app.models import (
        AdminAudit,
        CatalogAudit,
        CatalogCategory,
        CatalogService,
        Client,
        ClientNote,
        Conversation,
        FollowUp,
        Invitation,
        LoginEvent,
        SavedReply,
        Ticket,
        TicketActivity,
        User,
        WebSession,
    )
    from sqlmodel import select

    # Los mensajes (WhatsApp y chat de equipo) no se backfillean aquí: sus
    # tablas usan UNIQUE(source_id) e INSERT directo, así que reinsertarlos
    # rompería la constraint. Se espejan por listener en cada escritura nueva.
    try:
        if not _ready:
            init()
        with get_session() as session:
            for c in session.exec(select(Client)).all():
                sync_client_name(c)
            for u in session.exec(select(User)).all():
                archive_user(u)
            for t in session.exec(select(Ticket)).all():
                archive_ticket(t)
            for a in session.exec(select(TicketActivity)).all():
                archive_activity(a)
            for i in session.exec(select(Invitation)).all():
                archive_invitation(i)
            for conv in session.exec(select(Conversation)).all():
                archive_conversation(conv)
            for aa in session.exec(select(AdminAudit)).all():
                archive_admin_audit(aa)
            for f in session.exec(select(FollowUp)).all():
                archive_followup(f)
            for n in session.exec(select(ClientNote)).all():
                archive_client_note(n)
            for r in session.exec(select(SavedReply)).all():
                archive_saved_reply(r)
            for e in session.exec(select(LoginEvent)).all():
                archive_login_event(e)
            for w in session.exec(select(WebSession)).all():
                archive_web_session(w)
            for cc in session.exec(select(CatalogCategory)).all():
                archive_catalog_category(cc)
            for cs in session.exec(select(CatalogService)).all():
                archive_catalog_service(cs)
            for ca in session.exec(select(CatalogAudit)).all():
                archive_catalog_audit(ca)
        logger.info("Backfill de MSSQL completado")
    except Exception as exc:
        logger.warning("Backfill de MSSQL incompleto: %s", exc)


def _register_listeners() -> None:
    # ponytail: la réplica corre dentro del flush de SQLModel — con este
    # volumen es inofensivo; mover a una cola si algún día hay miles de
    # mensajes por minuto.
    from app.models import (
        AdminAudit,
        CatalogAudit,
        CatalogCategory,
        CatalogService,
        Client,
        ClientNote,
        Conversation,
        FollowUp,
        Invitation,
        LoginEvent,
        Message,
        SavedReply,
        TeamMessage,
        Ticket,
        TicketActivity,
        User,
        WebSession,
    )

    @event.listens_for(Message, "after_insert")
    def _wa_insert(mapper, connection, target):
        _safe(archive_wa_message, target)

    @event.listens_for(Message, "after_update")
    def _wa_update(mapper, connection, target):
        _safe(sync_wa_message, target)

    @event.listens_for(TeamMessage, "after_insert")
    def _team_insert(mapper, connection, target):
        _safe(archive_team_message, target)

    @event.listens_for(TeamMessage, "after_update")
    def _team_update(mapper, connection, target):
        _safe(sync_team_message, target)

    @event.listens_for(Client, "after_insert")
    @event.listens_for(Client, "after_update")
    def _client_upsert(mapper, connection, target):
        _safe(sync_client_name, target)

    @event.listens_for(User, "after_insert")
    @event.listens_for(User, "after_update")
    def _user_upsert(mapper, connection, target):
        _safe(archive_user, target)

    @event.listens_for(Invitation, "after_insert")
    @event.listens_for(Invitation, "after_update")
    def _invitation_upsert(mapper, connection, target):
        _safe(archive_invitation, target)

    @event.listens_for(Ticket, "after_insert")
    @event.listens_for(Ticket, "after_update")
    def _ticket_upsert(mapper, connection, target):
        _safe(archive_ticket, target)

    @event.listens_for(TicketActivity, "after_insert")
    @event.listens_for(TicketActivity, "after_update")
    def _activity_upsert(mapper, connection, target):
        _safe(archive_activity, target)

    @event.listens_for(Conversation, "after_insert")
    @event.listens_for(Conversation, "after_update")
    def _conversation_upsert(mapper, connection, target):
        _safe(archive_conversation, target)

    @event.listens_for(AdminAudit, "after_insert")
    @event.listens_for(AdminAudit, "after_update")
    def _admin_audit_upsert(mapper, connection, target):
        _safe(archive_admin_audit, target)

    @event.listens_for(FollowUp, "after_insert")
    @event.listens_for(FollowUp, "after_update")
    def _followup_upsert(mapper, connection, target):
        _safe(archive_followup, target)

    @event.listens_for(ClientNote, "after_insert")
    @event.listens_for(ClientNote, "after_update")
    def _client_note_upsert(mapper, connection, target):
        _safe(archive_client_note, target)

    @event.listens_for(SavedReply, "after_insert")
    @event.listens_for(SavedReply, "after_update")
    def _saved_reply_upsert(mapper, connection, target):
        _safe(archive_saved_reply, target)

    @event.listens_for(LoginEvent, "after_insert")
    @event.listens_for(LoginEvent, "after_update")
    def _login_event_upsert(mapper, connection, target):
        _safe(archive_login_event, target)

    @event.listens_for(WebSession, "after_insert")
    @event.listens_for(WebSession, "after_update")
    def _web_session_upsert(mapper, connection, target):
        _safe(archive_web_session, target)

    @event.listens_for(CatalogCategory, "after_insert")
    @event.listens_for(CatalogCategory, "after_update")
    def _catalog_category_upsert(mapper, connection, target):
        _safe(archive_catalog_category, target)

    @event.listens_for(CatalogService, "after_insert")
    @event.listens_for(CatalogService, "after_update")
    def _catalog_service_upsert(mapper, connection, target):
        _safe(archive_catalog_service, target)

    @event.listens_for(CatalogAudit, "after_insert")
    @event.listens_for(CatalogAudit, "after_update")
    def _catalog_audit_upsert(mapper, connection, target):
        _safe(archive_catalog_audit, target)


_register_listeners()
