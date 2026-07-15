from datetime import datetime, timezone
from enum import Enum

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Direction(str, Enum):
    IN = "in"
    OUT = "out"


class TicketStatus(str, Enum):
    NUEVO = "nuevo"
    TOMADO = "tomado"
    EN_PROCESO = "en_proceso"
    CERRADO = "cerrado"


class Client(SQLModel, table=True):
    phone: str = Field(primary_key=True)
    name: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Conversation(SQLModel, table=True):
    phone: str = Field(primary_key=True)
    state: str = "inicio"
    context_json: str = "{}"
    failed_attempts: int = 0
    updated_at: datetime = Field(default_factory=utcnow)


class Message(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    phone: str = Field(index=True)
    direction: Direction
    body: str
    created_at: datetime = Field(default_factory=utcnow)
    wa_message_id: str | None = Field(default=None, index=True)
    wa_status: str | None = None
    wa_status_at: datetime | None = None
    media_path: str | None = None
    sent_by: str | None = None


class Ticket(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    phone: str = Field(index=True)
    client_name: str | None = None
    category: str
    service_id: str
    service_name: str
    price_info: str
    status: TicketStatus = TicketStatus.NUEVO
    escalado: bool = False
    notes: str | None = None
    assigned_to: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    password_hash: str


class TeamMessage(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    author: str = Field(index=True)
    body: str
    media_path: str | None = None
    created_at: datetime = Field(default_factory=utcnow, index=True)
