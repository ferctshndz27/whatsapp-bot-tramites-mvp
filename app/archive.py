"""Archivo espejo en MSSQL de todas las conversaciones.

SQLite sigue siendo la base operativa del bot; este módulo replica cada
mensaje (WhatsApp entrante/saliente y chat de equipo) a SQL Server para
archivo/BI. Escritura best-effort: si MSSQL no responde, el bot sigue
funcionando y solo se registra el fallo en el log.

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
        created_at DATETIME2     NOT NULL,
        CONSTRAINT fk_team_messages_user
            FOREIGN KEY (author) REFERENCES team_users(username)
    )
    """,
]


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


def _upsert_client(conn, phone: str, name: str | None = None) -> None:
    row = conn.execute(
        text("SELECT name FROM wa_clients WHERE phone = :p"), {"p": phone}
    ).first()
    if row is None:
        conn.execute(
            text("INSERT INTO wa_clients (phone, name, created_at) VALUES (:p, :n, :c)"),
            {"p": phone, "n": name, "c": datetime.utcnow()},
        )
    elif name and row[0] != name:
        conn.execute(
            text("UPDATE wa_clients SET name = :n WHERE phone = :p"), {"n": name, "p": phone}
        )


def archive_wa_message(m) -> None:
    with _get_engine().begin() as conn:
        _upsert_client(conn, m.phone)
        conn.execute(
            text(
                "INSERT INTO wa_messages (source_id, phone, direction, body, wa_message_id,"
                " wa_status, media_path, sent_by, created_at)"
                " VALUES (:sid, :p, :d, :b, :wid, :ws, :mp, :sb, :c)"
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
                "c": _naive_utc(m.created_at),
            },
        )


def sync_wa_message(m) -> None:
    """Sincroniza wamid/estado de entrega cuando el registro local se actualiza."""
    with _get_engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE wa_messages SET wa_message_id = :wid, wa_status = :ws"
                " WHERE source_id = :sid"
            ),
            {"wid": m.wa_message_id, "ws": m.wa_status, "sid": m.id},
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
                "INSERT INTO team_messages (source_id, author, body, media_path, created_at)"
                " VALUES (:sid, :a, :b, :mp, :c)"
            ),
            {
                "sid": m.id,
                "a": m.author,
                "b": m.body,
                "mp": m.media_path,
                "c": _naive_utc(m.created_at),
            },
        )


def sync_client_name(c) -> None:
    with _get_engine().begin() as conn:
        _upsert_client(conn, c.phone, c.name)


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


def _register_listeners() -> None:
    # ponytail: la réplica corre dentro del flush de SQLModel — con este
    # volumen es inofensivo; mover a una cola si algún día hay miles de
    # mensajes por minuto.
    from app.models import Client, Message, TeamMessage

    @event.listens_for(Message, "after_insert")
    def _wa_insert(mapper, connection, target):
        _safe(archive_wa_message, target)

    @event.listens_for(Message, "after_update")
    def _wa_update(mapper, connection, target):
        _safe(sync_wa_message, target)

    @event.listens_for(TeamMessage, "after_insert")
    def _team_insert(mapper, connection, target):
        _safe(archive_team_message, target)

    @event.listens_for(Client, "after_insert")
    @event.listens_for(Client, "after_update")
    def _client_upsert(mapper, connection, target):
        _safe(sync_client_name, target)


_register_listeners()
