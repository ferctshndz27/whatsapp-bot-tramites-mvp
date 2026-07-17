from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlmodel import Session, SQLModel

import app.archive as archive
from app.config import settings
from app.models import Direction, Message, TeamMessage

ARCHIVE_DDL = [
    "CREATE TABLE wa_clients (phone TEXT PRIMARY KEY, name TEXT, created_at TIMESTAMP)",
    """CREATE TABLE wa_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER UNIQUE,
        phone TEXT REFERENCES wa_clients(phone), direction TEXT, body TEXT,
        wa_message_id TEXT, wa_status TEXT, media_path TEXT, sent_by TEXT,
        habilitado INTEGER DEFAULT 1, created_at TIMESTAMP)""",
    "CREATE TABLE team_users (username TEXT PRIMARY KEY)",
    """CREATE TABLE team_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER UNIQUE,
        author TEXT REFERENCES team_users(username), body TEXT,
        media_path TEXT, habilitado INTEGER DEFAULT 1, created_at TIMESTAMP)""",
]


@pytest.fixture()
def mirror(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        for ddl in ARCHIVE_DDL:
            conn.execute(text(ddl))
    monkeypatch.setattr(archive, "_engine", engine)
    monkeypatch.setattr(archive, "_ready", True)
    monkeypatch.setattr(settings, "mssql_host", "test")
    monkeypatch.setattr(settings, "mssql_password", "test")
    yield engine
    engine.dispose()


def _wa_message(**kw) -> Message:
    base = dict(
        id=1,
        phone="5215500004444",
        direction=Direction.IN,
        body="hola",
        created_at=datetime.now(timezone.utc),
    )
    base.update(kw)
    return Message(**base)


def test_wa_message_archived_with_linked_client(mirror):
    archive.archive_wa_message(_wa_message())

    with mirror.connect() as conn:
        client = conn.execute(text("SELECT phone FROM wa_clients")).first()
        msg = conn.execute(text("SELECT phone, direction, body FROM wa_messages")).first()
    assert client[0] == "5215500004444"
    assert tuple(msg) == ("5215500004444", "in", "hola")


def test_wa_status_synced_by_source_id(mirror):
    archive.archive_wa_message(_wa_message(id=7, direction=Direction.OUT))

    archive.sync_wa_message(
        _wa_message(id=7, direction=Direction.OUT, wa_message_id="wamid.X", wa_status="read")
    )

    with mirror.connect() as conn:
        row = conn.execute(
            text("SELECT wa_message_id, wa_status FROM wa_messages WHERE source_id = 7")
        ).first()
    assert tuple(row) == ("wamid.X", "read")


def test_wa_message_soft_delete_synced(mirror):
    archive.archive_wa_message(_wa_message(id=8, direction=Direction.OUT))

    archive.sync_wa_message(_wa_message(id=8, direction=Direction.OUT, habilitado=False))

    with mirror.connect() as conn:
        row = conn.execute(
            text("SELECT habilitado FROM wa_messages WHERE source_id = 8")
        ).first()
    assert row[0] == 0


def test_team_message_soft_delete_synced(mirror):
    archive.archive_team_message(
        TeamMessage(id=9, author="ana", body="oculto luego", created_at=datetime.now(timezone.utc))
    )

    archive.sync_team_message(
        TeamMessage(id=9, author="ana", body="oculto luego", habilitado=False)
    )

    with mirror.connect() as conn:
        row = conn.execute(
            text("SELECT habilitado FROM team_messages WHERE source_id = 9")
        ).first()
    assert row[0] == 0


def test_team_message_archived_with_linked_user(mirror):
    archive.archive_team_message(
        TeamMessage(id=3, author="ana", body="hola equipo", created_at=datetime.now(timezone.utc))
    )

    with mirror.connect() as conn:
        user = conn.execute(text("SELECT username FROM team_users")).first()
        msg = conn.execute(text("SELECT author, body FROM team_messages")).first()
    assert user[0] == "ana"
    assert tuple(msg) == ("ana", "hola equipo")


def test_listeners_mirror_session_inserts(mirror):
    app_engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(app_engine)

    with Session(app_engine) as s:
        s.add(Message(phone="5215500005555", direction=Direction.IN, body="vía listener"))
        s.add(TeamMessage(author="bob", body="listener de equipo"))
        s.commit()

    with mirror.connect() as conn:
        wa = conn.execute(text("SELECT body FROM wa_messages")).first()
        team = conn.execute(text("SELECT body FROM team_messages")).first()
    assert wa[0] == "vía listener"
    assert team[0] == "listener de equipo"


def test_disabled_mirror_is_noop(monkeypatch):
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "mssql_password", "")

    app_engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(app_engine)
    with Session(app_engine) as s:
        s.add(Message(phone="5215500006666", direction=Direction.IN, body="sin espejo"))
        s.commit()
