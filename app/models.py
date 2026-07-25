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
    # Si el ticket está escalado, controla si el bot le sigue respondiendo al
    # cliente. Tomar el ticket YA NO lo apaga por sí solo — debe apagarlo el
    # colaborador asignado explícitamente (ver /tickets/{id}/agent-toggle).
    agent_active: bool = True
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
    # Celular del colaborador (formato WhatsApp 521 + 10 dígitos), capturado
    # en el registro.
    phone: str | None = None


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


class LoginEvent(SQLModel, table=True):
    """Bitácora de intentos de acceso al panel: éxitos y fallos con motivo.

    Append-only (como AdminAudit): los logs del contenedor se pierden al
    recrearlo, esto no. La respuesta al usuario en /login sigue siendo
    genérica; el motivo detallado solo se ve en el panel admin."""

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True)
    success: bool = Field(index=True)
    # None en éxito; en fallo: "usuario no existe", "contraseña incorrecta",
    # "usuario deshabilitado", "bloqueado temporalmente"
    reason: str | None = None
    ip: str | None = None
    # "Ciudad, Región, País" — se completa best-effort (ver app.authlog)
    geo: str | None = None
    # Resumen legible del User-Agent, p.ej. "Chrome · Windows"
    device: str | None = None
    user_agent: str | None = None
    created_at: datetime = Field(default_factory=utcnow, index=True)
    habilitado: bool = True


class WebSession(SQLModel, table=True):
    """Sesión del panel por dispositivo, para saber quién está conectado,
    desde dónde y desde cuándo. Activa = ended_at nulo y last_seen reciente."""

    id: int | None = Field(default=None, primary_key=True)
    token: str = Field(unique=True, index=True)
    username: str = Field(index=True)
    ip: str | None = None
    geo: str | None = None
    device: str | None = None
    user_agent: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow, index=True)
    ended_at: datetime | None = None
    habilitado: bool = True


class IpGeoCache(SQLModel, table=True):
    """Cache local de geolocalización por IP. Solo vive en SQLite (no se
    espeja a MSSQL: es derivable y se reconstruye con nuevos lookups)."""

    ip: str = Field(primary_key=True)
    geo: str | None = None  # None = el lookup falló; se reintenta al expirar
    resolved_at: datetime = Field(default_factory=utcnow)


class CatalogCategory(SQLModel, table=True):
    """Categoría del catálogo de servicios (antes vivía en catalog.json)."""

    id: str = Field(primary_key=True)  # slug estable; los tickets guardan service_id/categoría
    name: str
    position: int = 0
    habilitado: bool = True


class CatalogService(SQLModel, table=True):
    """Servicio del catálogo. synonyms_json es una lista JSON de frases con
    que los clientes piden el servicio (usada por la deducción de intención)."""

    id: str = Field(primary_key=True)  # slug estable
    category_id: str = Field(index=True)
    name: str
    price_info: str
    synonyms_json: str = "[]"
    requirements: str = ""
    position: int = 0
    habilitado: bool = True


class CatalogAudit(SQLModel, table=True):
    """Auditoría campo a campo del catálogo: quién cambió qué, valor anterior
    y nuevo. Append-only, visible en el panel Catálogo y espejada a MSSQL."""

    id: int | None = Field(default=None, primary_key=True)
    admin: str = Field(index=True)
    entity: str  # "categoria" | "servicio"
    entity_id: str = Field(index=True)
    # "name" | "price_info" | "synonyms" | "requirements" | "habilitado" | "creado"
    field: str
    old_value: str | None = None
    new_value: str | None = None
    created_at: datetime = Field(default_factory=utcnow, index=True)
    habilitado: bool = True


class SynonymProposal(SQLModel, table=True):
    """Propuesta de sinónimo para el catálogo (minería nocturna con Claude o
    clustering de no-entendidos). Nada se aplica sin aprobación humana: el
    admin decide en el panel Agente WhatsApp y queda quién y cuándo."""

    id: int | None = Field(default=None, primary_key=True)
    service_id: str = Field(index=True)
    synonym: str
    origin: str = "claude"  # claude | claude-nocturno | cluster
    status: str = Field(default="pendiente", index=True)  # pendiente|aprobada|rechazada
    decided_by: str | None = None
    decided_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow, index=True)
    habilitado: bool = True


class Feedback(SQLModel, table=True):
    """Opinión del cliente al cerrar su solicitud (botones 👍/👎 enviados por
    WhatsApp al terminar el ticket). Una sola opinión por ticket."""

    id: int | None = Field(default=None, primary_key=True)
    ticket_id: int = Field(index=True)
    phone: str = Field(index=True)
    rating: str  # "up" | "down"
    created_at: datetime = Field(default_factory=utcnow, index=True)
    habilitado: bool = True


class NluEvent(SQLModel, table=True):
    """Bitácora de deducción del agente: qué entendió (o no) de cada texto
    libre, con qué capa, y si terminó en solicitud. Alimenta las métricas
    del panel Agente IA (/unmatched). Append-only."""

    id: int | None = Field(default=None, primary_key=True)
    phone: str = Field(index=True)
    text: str  # truncado a 200 caracteres
    # llm | nombre | sinonimo | keyword | fuzzy | categoria | multi |
    # sugerencia | ninguna
    capa: str = Field(index=True)
    # Uno o varios service_id separados por coma (multi/sugerencia).
    service_id: str | None = None
    confianza: float | None = None  # solo capa fuzzy (score 0-1)
    termino_en_ticket: bool = False
    created_at: datetime = Field(default_factory=utcnow, index=True)
    habilitado: bool = True


class Alert(SQLModel, table=True):
    """Alerta de salud del agente (Fase 4b). Una sola fila ACTIVA por `code`
    (regla): mientras la condición se sostiene se actualiza en su lugar, y al
    desaparecer se marca resuelta (active=False, resolved_at). Solo se muestra
    en el panel /unmatched (admin) — nunca se envía por WhatsApp.

    La evalúa el job nocturno (ver app.alerts.evaluate_alerts). Append-only en
    la práctica salvo el toggle de active/resolved_at."""

    id: int | None = Field(default=None, primary_key=True)
    code: str = Field(index=True)  # id de regla; a lo más una fila active por code
    level: str  # "warn" | "critical"
    title: str
    detail: str
    metric_value: float | None = None
    active: bool = True
    created_at: datetime = Field(default_factory=utcnow)
    resolved_at: datetime | None = None


class Invitation(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    token: str = Field(unique=True, index=True)
    phone: str | None = None
    created_by: str
    created_at: datetime = Field(default_factory=utcnow)
    used_at: datetime | None = None
    used_by: str | None = None
    habilitado: bool = True
