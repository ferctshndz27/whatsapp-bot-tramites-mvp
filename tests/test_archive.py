from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlmodel import Session, SQLModel

import app.archive as archive
from app.config import settings
from app.models import (
    Direction,
    Invitation,
    Message,
    TeamMessage,
    Ticket,
    TicketActivity,
    TicketStatus,
    User,
)

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
    """CREATE TABLE app_users (
        source_id INTEGER PRIMARY KEY, username TEXT UNIQUE,
        password_hash TEXT, is_admin INTEGER DEFAULT 0)""",
    """CREATE TABLE invitations (
        source_id INTEGER PRIMARY KEY, token TEXT, phone TEXT, created_by TEXT,
        created_at TIMESTAMP, used_at TIMESTAMP, used_by TEXT)""",
    """CREATE TABLE tickets (
        source_id INTEGER PRIMARY KEY, phone TEXT, client_name TEXT, category TEXT,
        service_id TEXT, service_name TEXT, price_info TEXT, status TEXT,
        escalado INTEGER DEFAULT 0, notes TEXT, assigned_to TEXT,
        created_at TIMESTAMP, updated_at TIMESTAMP)""",
    """CREATE TABLE ticket_activity (
        source_id INTEGER PRIMARY KEY, ticket_id INTEGER, username TEXT,
        action TEXT, created_at TIMESTAMP)""",
    """CREATE TABLE conversations (
        phone TEXT PRIMARY KEY, state TEXT, context_json TEXT,
        failed_attempts INTEGER, updated_at TIMESTAMP)""",
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


def test_user_archived_with_bcrypt_hash_not_plaintext(mirror):
    archive.archive_user(
        User(id=1, username="ana", password_hash="$2b$12$abc.hashbcrypt", is_admin=True)
    )

    with mirror.connect() as conn:
        row = conn.execute(
            text("SELECT username, password_hash, is_admin FROM app_users")
        ).first()
    assert row[0] == "ana"
    assert row[1].startswith("$2b$")  # es un hash bcrypt, nunca la contraseña
    assert row[2] == 1


def test_user_upsert_updates_admin_flag(mirror):
    archive.archive_user(User(id=2, username="bob", password_hash="h", is_admin=False))
    archive.archive_user(User(id=2, username="bob", password_hash="h", is_admin=True))

    with mirror.connect() as conn:
        rows = conn.execute(text("SELECT is_admin FROM app_users WHERE source_id = 2")).all()
    assert len(rows) == 1  # upsert, no duplica
    assert rows[0][0] == 1


def test_invitation_archived_and_marked_used(mirror):
    archive.archive_invitation(
        Invitation(id=1, token="tok1", created_by="ana", created_at=datetime.now(timezone.utc))
    )
    archive.archive_invitation(
        Invitation(
            id=1, token="tok1", created_by="ana",
            created_at=datetime.now(timezone.utc),
            used_at=datetime.now(timezone.utc), used_by="carlos",
        )
    )

    with mirror.connect() as conn:
        row = conn.execute(
            text("SELECT token, used_by FROM invitations WHERE source_id = 1")
        ).first()
    assert row[0] == "tok1"
    assert row[1] == "carlos"


def test_ticket_archived_and_status_updated(mirror):
    def _ticket(**kw):
        base = dict(
            id=5, phone="521550000", category="Servicios básicos", service_id="curp",
            service_name="CURP", price_info="$80", status=TicketStatus.NUEVO,
            created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
        )
        base.update(kw)
        return Ticket(**base)

    archive.archive_ticket(_ticket())
    archive.archive_ticket(_ticket(status=TicketStatus.CERRADO, assigned_to="ana"))

    with mirror.connect() as conn:
        rows = conn.execute(
            text("SELECT status, assigned_to FROM tickets WHERE source_id = 5")
        ).all()
    assert len(rows) == 1
    assert tuple(rows[0]) == ("cerrado", "ana")


def test_activity_archived(mirror):
    archive.archive_activity(
        TicketActivity(id=1, ticket_id=5, username="ana", action="cerrado",
                       created_at=datetime.now(timezone.utc))
    )

    with mirror.connect() as conn:
        row = conn.execute(
            text("SELECT ticket_id, username, action FROM ticket_activity")
        ).first()
    assert tuple(row) == (5, "ana", "cerrado")


def test_conversation_archived_by_phone(mirror):
    from app.models import Conversation

    archive.archive_conversation(
        Conversation(phone="521999", state="menu_categoria", context_json="{}",
                     failed_attempts=1, updated_at=datetime.now(timezone.utc))
    )
    archive.archive_conversation(
        Conversation(phone="521999", state="escalado", context_json="{}",
                     failed_attempts=0, updated_at=datetime.now(timezone.utc))
    )

    with mirror.connect() as conn:
        rows = conn.execute(text("SELECT state FROM conversations WHERE phone = '521999'")).all()
    assert len(rows) == 1  # keyed por phone, no duplica
    assert rows[0][0] == "escalado"


def test_listeners_mirror_new_entities(mirror):
    app_engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(app_engine)

    with Session(app_engine) as s:
        s.add(User(username="jefa", password_hash="$2b$12$h", is_admin=True))
        s.add(Ticket(phone="521111", category="c", service_id="s", service_name="n",
                     price_info="$1"))
        s.commit()

    with mirror.connect() as conn:
        user = conn.execute(text("SELECT username FROM app_users")).first()
        ticket = conn.execute(text("SELECT phone FROM tickets")).first()
    assert user[0] == "jefa"
    assert ticket[0] == "521111"
