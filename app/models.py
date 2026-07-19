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
    habilitado: bool = True


class Conversation(SQLModel, table=True):
    phone: str = Field(primary_key=True)
    state: str = "inicio"
    context_json: str = "{}"
    failed_attempts: int = 0
    updated_at: datetime = Field(default_factory=utcnow)
    habilitado: bool = True


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
    habilitado: bool = True


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
    habilitado: bool = True


class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    password_hash: str
    is_admin: bool = False
    habilitado: bool = True
    # Disponibilidad visible al equipo: disponible | ocupado | ausente
    estado: str = "disponible"


class FollowUp(SQLModel, table=True):
    """Recordatorio de seguimiento sobre un ticket ("recontactar el lunes")."""

    id: int | None = Field(default=None, primary_key=True)
    ticket_id: int = Field(index=True)
    username: str = Field(index=True)
    note: str = ""
    due_at: datetime = Field(index=True)
    done_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    habilitado: bool = True


class ClientNote(SQLModel, table=True):
    """Nota interna sobre un cliente (visible a todo el equipo, no al cliente)."""

    id: int | None = Field(default=None, primary_key=True)
    phone: str = Field(index=True)
    author: str
    body: str
    created_at: datetime = Field(default_factory=utcnow)
    habilitado: bool = True


class SavedReply(SQLModel, table=True):
    """Respuesta guardada personal de un colaborador para la bandeja."""

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True)
    title: str
    body: str
    created_at: datetime = Field(default_factory=utcnow)
    habilitado: bool = True


class TeamMessage(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    author: str = Field(index=True)
    body: str
    media_path: str | None = None
    created_at: datetime = Field(default_factory=utcnow, index=True)
    habilitado: bool = True


class TicketActivity(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    ticket_id: int = Field(index=True)
    username: str = Field(index=True)
    # Acciones: "tomado", "soltado", "cerrado", "estado:<valor>",
    # "reasignado:<destino>", "nota"
    action: str
    created_at: datetime = Field(default_factory=utcnow, index=True)
    habilitado: bool = True


class AdminAudit(SQLModel, table=True):
    """Bitácora permanente de acciones administrativas (los logs del
    contenedor se pierden al recrearlo; esto no)."""

    id: int | None = Field(default=None, primary_key=True)
    admin: str = Field(index=True)
    # Acciones: "usuario:creado", "usuario:password", "usuario:rol",
    # "usuario:toggle", "invitacion:toggle", "ticket:toggle",
    # "cliente:toggle", "espejo:sync", "catalogo:<detalle>"
    action: str
    target: str | None = None
    created_at: datetime = Field(default_factory=utcnow, index=True)
    habilitado: bool = True


class Invitation(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    token: str = Field(unique=True, index=True)
    phone: str | None = None
    created_by: str
    created_at: datetime = Field(default_factory=utcnow)
    used_at: datetime | None = None
    used_by: str | None = None
    habilitado: bool = True
