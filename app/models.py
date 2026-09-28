from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import text
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Direction(str, Enum):
    IN = "in"
    OUT = "out"


# Proveedor del canal de WhatsApp. Son DOS cuentas independientes, cada una con
# su propio número, que comparten este CRM; no hay failover entre ellas.
#
# Cadena simple y no Enum a propósito: el valor viaja en el DEFAULT de un ALTER
# TABLE (app/db.py) y en el espejo MSSQL, y SQLModel guarda los Enum por NOMBRE
# ("META"), lo que obligaría a escribir 'META' en el SQL y Provider.META.value en
# cada comparación. Para dos valores no compensa.
PROVIDER_META = "meta"
PROVIDER_TWILIO = "twilio"
PROVIDERS = (PROVIDER_META, PROVIDER_TWILIO)

# Multi-oficina (docs/SPEC-MULTI-OFICINA.md). La 1 es Tlaxcala: la única oficina
# que ha operado, y por eso es el DEFAULT de office_id en las 15 tablas de §2.3.
# La fila la siembra db.init_db(); nunca se borra (baja = habilitado=0).
OFICINA_TLAXCALA_ID = 1
# Fuente única de la zona horaria del negocio: origen de templating.BUSINESS_TZ
# (hoy un valor GLOBAL fijo, no por oficina — Office ya no tiene columna propia).
# templating importa de aquí y no al revés: este módulo no importa nada de
# app.*, así que no hay ciclo.
ZONA_HORARIA_DEFAULT = "America/Mexico_City"


def _office_id_field():
    """Columna office_id de las 15 tablas de §2.3 — UNA definición, 15 usos.

    Es una función y no una constante compartida por gusto de aislamiento: un
    solo FieldInfo entre 15 modelos también funciona (pydantic lo copia por
    clase), pero un Field por llamada deja el helper libre de suposiciones
    sobre las internas de SQLModel. Mismo truco que Conversation.provider
    (abajo): `default=` cubre el INSERT desde Python,
    `server_default` cubre el DDL de create_all() y _OFFICE_COLUMN en app/db.py
    cubre la base migrada — los tres caminos convergen en
    `INTEGER NOT NULL DEFAULT 1`. Sin foreign_key= ni index=True a propósito:
    el repo no declara FKs (PRAGMA foreign_keys apagado, app/db.py:19-27) y
    _add_missing_columns solo hace ADD COLUMN, así que una FK o un índice
    existirían en bases nuevas y nunca en el VPS sin que nadie lo note.
    """
    return Field(
        default=OFICINA_TLAXCALA_ID,
        sa_column_kwargs={"server_default": text(str(OFICINA_TLAXCALA_ID))},
    )


class TicketStatus(str, Enum):
    NUEVO = "nuevo"
    TOMADO = "tomado"
    EN_PROCESO = "en_proceso"
    CERRADO = "cerrado"


class Client(SQLModel, table=True):
    phone: str = Field(
        sa_column_kwargs={"primary_key": True},
        title="phone",
    )
    name: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    habilitado: bool = True
    office_id: int = _office_id_field()
    # Ficha CRM (fase 1 del análisis de brechas, docs/ANALISIS-BRECHAS-ERP-CRM.md).
    # Se normaliza a minúsculas al guardar (ver clients.update_client).
    email: str | None = None
    # Canal de adquisición: meta | twilio | referido | manual
    origen: str | None = None
    # server_default: mismo patrón que provider — una base nueva (create_all)
    # debe nacer con el mismo DEFAULT que deja la migración en la base vieja.
    idioma: str = Field(
        default="es-MX", sa_column_kwargs={"server_default": text("'es-MX'")}
    )
    # Consentimiento para mensajes iniciados por el negocio. Meta exige poder
    # probar cuándo y cómo se dio (un mensaje entrante NO es opt-in de
    # marketing). opt_out_at presente = excluido de cualquier audiencia, siempre.
    opt_in_at: datetime | None = None
    opt_in_source: str | None = None
    opt_out_at: datetime | None = None
    # ponytail: campos personalizados como JSON en vez de tablas de definición
    # tipo Chatwoot; subir a tablas reales si algún día hay que validar o
    # filtrar por campo.
    atributos_json: str | None = None
    last_activity_at: datetime | None = None
    # Resumen fijado del cliente; el historial granular vive en ClientNote.
    notas_internas: str | None = None
    # Etiqueta interna para que el equipo reconozca al cliente de un vistazo
    # («la señora de las actas», «contador Pérez»). Solo se pinta en el panel,
    # debajo del nombre; el bot nunca la lee ni se la manda al cliente. Uno por
    # cliente, no por colaborador: es un identificador compartido. NULL = sin alias.
    alias: str | None = None
    # Embudo comercial del CRM (/crm, app/crm.py). NULL = «en la primera etapa»
    # (la de menor `ord`): así el histórico entra al tablero sin un UPDATE
    # masivo, que además el espejo MSSQL no vería. Quién y cuándo la movió, para
    # medir tiempo en etapa (fase 5 del análisis de brechas).
    etapa_crm_id: int | None = None
    etapa_crm_at: datetime | None = None
    etapa_crm_por: str | None = None


class Conversation(SQLModel, table=True):
    phone: str = Field(primary_key=True)
    state: str = "inicio"
    context_json: str = "{}"
    failed_attempts: int = 0
    updated_at: datetime = Field(default_factory=utcnow)
    habilitado: bool = True
    office_id: int = _office_id_field()
    # Canal del ÚLTIMO mensaje entrante. Decide por dónde se responde: la
    # ventana de 24 h es por número, así que contestar por el otro canal deja
    # al negocio fuera de ventana con un remitente que el cliente no reconoce.
    # Una persona = una conversación, sin importar a cuál de los dos números
    # escribió (opción A del plan): nadie quiere dos trámites en paralelo.
    # server_default: una base NUEVA (create_all) nace con el mismo DEFAULT que
    # deja la migración en la base vieja (ALTER TABLE ... DEFAULT 'meta'). Sin
    # esto los dos caminos divergen y aparece un bug que solo se ve en uno.
    # text(): una cadena pelona la vuelve a citar SQLAlchemy y el DEFAULT
    # quedaría con las comillas DENTRO del valor ("'meta'"), que nunca
    # coincidiría con PROVIDER_META.
    provider: str = Field(
        default=PROVIDER_META,
        sa_column_kwargs={"server_default": text(f"'{PROVIDER_META}'")},
    )


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
    # Canal por el que entró o salió este mensaje. Sin esto no se puede
    # interpretar `wa_status` (los estados de Twilio y los de Meta no son el
    # mismo vocabulario) ni acotar el emparejamiento FIFO de wa_message_id,
    # que hoy cruza ids entre canales (ver webhook._tag_sent_message).
    # server_default: una base NUEVA (create_all) nace con el mismo DEFAULT que
    # deja la migración en la base vieja (ALTER TABLE ... DEFAULT 'meta'). Sin
    # esto los dos caminos divergen y aparece un bug que solo se ve en uno.
    # text(): una cadena pelona la vuelve a citar SQLAlchemy y el DEFAULT
    # quedaría con las comillas DENTRO del valor ("'meta'"), que nunca
    # coincidiría con PROVIDER_META.
    provider: str = Field(
        default=PROVIDER_META,
        sa_column_kwargs={"server_default": text(f"'{PROVIDER_META}'")},
    )
    # Lo que Meta dice que cobró por este mensaje. NO se calcula aquí: se copia
    # tal cual del webhook de estado (`pricing` viene en el status `sent` y en
    # UNO de delivered/read, nunca en los dos). NULL = todavía no llegó, o el
    # canal no lo reporta —Twilio no manda nada de esto—, y ese NULL es el
    # denominador honesto del medidor: sin él, «no cobrado» y «no sabemos» se
    # confunden y el reporte sale bajo.
    #
    # Copiar en vez de deducir es lo que hace que el 1-oct-2026 no requiera
    # tocar código: ese día los mensajes de servicio y las plantillas de
    # utilidad dentro de la ventana dejan de ser gratis, y Meta empezará a
    # mandar type='regular' donde hoy manda 'free_customer_service'.
    # utility | marketing | authentication | authentication-international
    # | marketing_lite | referral_conversion | service
    pricing_category: str | None = None
    # regular (se cobra) | free_customer_service | free_entry_point
    pricing_type: str | None = None
    # Meta lo va a deprecar; el veredicto vive en pricing_type. Se guarda
    # mientras lo mande para poder contrastar.
    pricing_billable: bool | None = None


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
    # Requisitos del servicio congelados al crear el folio, con lo que el
    # cliente contestó por WhatsApp: [{"req": "clave curp", "valor": "..."},
    # {"req": "nss", "valor": null}]. valor null = el cliente no lo dio (lo
    # pide el asesor). Columna nula = trámite sin requisitos documentados.
    requirements_json: str | None = None
    assigned_to: str | None = None
    # Si el ticket está escalado, controla si el bot le sigue respondiendo al
    # cliente. Tomar el ticket YA NO lo apaga por sí solo — debe apagarlo el
    # colaborador asignado explícitamente (ver /tickets/{id}/agent-toggle).
    agent_active: bool = True
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    habilitado: bool = True
    office_id: int = _office_id_field()
    # Número por el que llegó la solicitud. Es lo que permite reportar carga y
    # conversión por cuenta cuando las dos cuentas comparten este CRM.
    # server_default: una base NUEVA (create_all) nace con el mismo DEFAULT que
    # deja la migración en la base vieja (ALTER TABLE ... DEFAULT 'meta'). Sin
    # esto los dos caminos divergen y aparece un bug que solo se ve en uno.
    # text(): una cadena pelona la vuelve a citar SQLAlchemy y el DEFAULT
    # quedaría con las comillas DENTRO del valor ("'meta'"), que nunca
    # coincidiría con PROVIDER_META.
    provider: str = Field(
        default=PROVIDER_META,
        sa_column_kwargs={"server_default": text(f"'{PROVIDER_META}'")},
    )
    # Métricas de operación (fase 1). Nullable a propósito: los folios previos
    # no tienen el dato y un NULL honesto vale más que una métrica inventada —
    # los reportes los excluyen.
    # Primer mensaje saliente de un HUMANO en este folio; el bot nunca lo llena
    # (es justo lo que se quiere medir).
    first_response_at: datetime | None = None
    first_response_by: str | None = None
    # Cuándo pasó a CERRADO.
    resolved_at: datetime | None = None
    # Último cambio de status; permite medir tiempo en etapa.
    status_changed_at: datetime | None = None


class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    password_hash: str
    is_admin: bool = False
    # Admin DE OFICINA: con is_admin=True administra solo su oficina (alcance()
    # le quita `ve_todas`) y no entra a lo global (oficinas y llaves, /system,
    # catálogo, sinónimos, Estudio). Revierte lo que SPEC-MULTI-OFICINA §5
    # decidió, a pedido del operador (2026-09-11). Se guarda como bandera
    # NEGATIVA a propósito: DEFAULT 0 deja a todo admin existente como global,
    # sin reescribir una sola fila (un UPDATE crudo no llegaría al espejo).
    solo_su_oficina: bool = Field(default=False, sa_column_kwargs={"server_default": text("0")})
    habilitado: bool = True
    office_id: int = _office_id_field()
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


class Label(SQLModel, table=True):
    """Etiqueta transversal para clasificar folios (fase 1)."""

    id: int | None = Field(default=None, primary_key=True)
    nombre: str = Field(unique=True, index=True)
    color: str = "#dcf24c"
    descripcion: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    habilitado: bool = True
    office_id: int = _office_id_field()


# Etapas con las que nace el embudo (db._seed_crm_etapas). Nombres y colores del
# plan del CRM; después las edita el admin global desde /crm.
ETAPAS_CRM_DEFAULT = (
    ("Nuevo Lead", "#1E88E5"),
    ("En Negociación", "#FFB300"),
    ("Seguimiento", "#8E24AA"),
    ("Cerrado ✅", "#25D366"),
)


class CrmEtapa(SQLModel, table=True):
    """Etapa comercial del embudo del CRM (/crm): una columna del tablero.

    GLOBAL a propósito —sin office_id—: un solo embudo para todas las oficinas,
    igual que el catálogo (SPEC multi-oficina §11.3), para que los reportes por
    etapa sean comparables. La crea, renombra y da de baja el admin global;
    mover clientes entre etapas es de cualquiera con acceso al cliente. Baja =
    habilitado=0 (soft delete, como todo en este repo): así el espejo se entera
    por after_update. Tabla física `crmetapa` (SQLModel sin __tablename__, como
    ticketlabel); en MSSQL es `crm_etapas`."""

    id: int | None = Field(default=None, primary_key=True)
    nombre: str = Field(unique=True, index=True)
    color: str = "#25D366"
    ord: int = 0
    habilitado: bool = True
    created_at: datetime = Field(default_factory=utcnow)


class TicketLabel(SQLModel, table=True):
    """Puente folio-etiqueta. Quitar una etiqueta la deshabilita (soft delete,
    como todo en este repo) — así el espejo MSSQL se entera vía after_update y
    no hace falta un listener de DELETE. Re-etiquetar reactiva la fila (hay
    índice único parcial ux_ticketlabel_viva en db.init_db)."""

    id: int | None = Field(default=None, primary_key=True)
    ticket_id: int = Field(index=True)
    label_id: int = Field(index=True)
    created_at: datetime = Field(default_factory=utcnow)
    created_by: str | None = None
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
    office_id: int = _office_id_field()


class ChatRoom(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str | None = None
    # "direct" o "group"
    room_type: str = "direct"
    created_at: datetime = Field(default_factory=utcnow)
    habilitado: bool = True


class ChatMember(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    room_id: int = Field(index=True)
    username: str = Field(index=True)
    is_muted: bool = False
    joined_at: datetime = Field(default_factory=utcnow)
    habilitado: bool = True
    # «Eliminar chat» solo para él (como WhatsApp): desde este momento deja de
    # ver lo anterior. None = nunca lo eliminó. Soft delete: no se borra nada.
    borrado_at: datetime | None = None


class TeamMessage(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    room_id: int | None = Field(default=None, index=True)
    author: str = Field(index=True)
    body: str
    media_path: str | None = None
    created_at: datetime = Field(default_factory=utcnow, index=True)
    habilitado: bool = True
    office_id: int = _office_id_field()


class MessageReaction(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    message_id: int = Field(index=True)
    username: str = Field(index=True)
    reaction: str
    created_at: datetime = Field(default_factory=utcnow)


class MessageReminder(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    message_id: int = Field(index=True)
    username: str = Field(index=True)
    remind_at: datetime
    note: str | None = None
    is_dismissed: bool = False
    created_at: datetime = Field(default_factory=utcnow)


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
    office_id: int = _office_id_field()


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
    office_id: int = _office_id_field()


class CatalogAudit(SQLModel, table=True):
    """Auditoría campo a campo del catálogo: quién cambió qué, valor anterior
    y nuevo. Append-only, visible en el panel Catálogo y espejada a MSSQL."""

    id: int | None = Field(default=None, primary_key=True)
    admin: str = Field(index=True)
    entity: str  # "categoria" | "servicio"
    entity_id: str = Field(index=True)
    # "name" | "price_info" | "synonyms" | "requirements" | "habilitado" | "creado"
    # | "asesores"
    field: str
    old_value: str | None = None
    new_value: str | None = None
    created_at: datetime = Field(default_factory=utcnow, index=True)
    habilitado: bool = True


class CatalogAsesor(SQLModel, table=True):
    """Quién atiende qué producto: puente servicio-colaborador.

    Lo declara el propio colaborador desde /catalog, y lo consume la asignación
    (`asignacion.elegir`) para mandarle a cada quien los folios de lo que atiende.
    Mismo patrón que TicketLabel: dejar de atender deshabilita la fila (soft
    delete) —así el espejo MSSQL se entera vía after_update sin un listener de
    DELETE— y volver a atenderlo la reactiva; el índice único parcial
    ux_catalogasesor_vivo (db.init_db) impide dos filas vivas iguales.

    Sin office_id A PROPÓSITO, como TicketLabel: el catálogo es global
    (SPEC-MULTI-OFICINA §11.3) y la oficina es la del usuario — duplicarla aquí
    abriría la puerta a que las dos discrepen.
    """

    id: int | None = Field(default=None, primary_key=True)
    service_id: str = Field(index=True)
    username: str = Field(index=True)
    created_at: datetime = Field(default_factory=utcnow)
    created_by: str | None = None
    habilitado: bool = True


class ClienteAsesor(SQLModel, table=True):
    """La cartera: qué colaboradores consideran suyo a un cliente (app/cartera.py).

    Muchos a muchos porque un cliente puede ser compartido. Gemela de
    CatalogAsesor: quitarse de la cartera apaga la fila (soft delete, el espejo
    se entera por after_update) y volver a agregarse la reactiva; el índice
    único parcial ux_clienteasesor_vivo (db.init_db) impide dos filas vivas del
    mismo par.

    Da preferencia de asignación (si el titular atiende el servicio) y deja VER
    la conversación; nunca da permiso de escribirle al cliente, que sigue siendo
    del folio (decisión del operador, 2026-09-25). Sin office_id a propósito: la
    oficina es la del cliente, y duplicarla aquí abriría la puerta a que las dos
    discrepen.
    """

    id: int | None = Field(default=None, primary_key=True)
    phone: str = Field(index=True)
    username: str = Field(index=True)
    created_at: datetime = Field(default_factory=utcnow)
    created_by: str | None = None
    habilitado: bool = True


class SynonymProposal(SQLModel, table=True):
    """Propuesta de sinónimo para el catálogo (minería nocturna con Claude o
    clustering de no-entendidos). Nada se aplica sin aprobación humana: el
    admin decide en el panel Agente WhatsApp y queda quién y cuándo."""

    id: int | None = Field(default=None, primary_key=True)
    service_id: str = Field(index=True)
    synonym: str
    # claude | mineria-nocturna | cluster (filas viejas traen "claude-nocturno")
    origin: str = "claude"
    status: str = Field(default="pendiente", index=True)  # pendiente|aprobada|rechazada
    decided_by: str | None = None
    decided_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow, index=True)
    habilitado: bool = True
    office_id: int = _office_id_field()


class Feedback(SQLModel, table=True):
    """Opinión del cliente al cerrar su solicitud (botones 👍/👎 enviados por
    WhatsApp al terminar el ticket). Una sola opinión por ticket."""

    id: int | None = Field(default=None, primary_key=True)
    ticket_id: int = Field(index=True)
    phone: str = Field(index=True)
    rating: str  # "up" | "down"
    created_at: datetime = Field(default_factory=utcnow, index=True)
    habilitado: bool = True


# Capas de nluevent que son fila EXTRA de un mensaje ya contado por
# _deduce_and_log ("ninguna"/"sugerencia"): se miden en la dona del panel,
# pero NO entran al denominador de mensajes (alerts._no_entendidos_stats,
# synonym_miner.nlu_metrics) — contarlas diluiría el % de no entendidos.
# "tool"/"tool_error" son una fila POR LLAMADA de herramienta del nodo agéntico
# (varias por mensaje), no por mensaje: entran aquí para que no inflen ningún
# denominador — igual que rag_descartado. El nombre de la herramienta viaja en
# service_id, como el motivo del guardrail en las filas rag_descartado.
# "ollama_error" es una falla de TRANSPORTE (timeout, conexión, 5xx) que
# conversation registra ADEMÁS de la capa que sí acabó respondiendo: el
# mensaje ya está contado, y contarlo otra vez diluiría el % de no entendidos
# justo cuando el servidor está caído — que es cuando la alerta debe sonar.
CAPAS_EXTRA = frozenset(
    {"rag", "rag_descartado", "tools", "tool", "tool_error", "ollama_error"}
)


class NluEvent(SQLModel, table=True):
    """Bitácora de deducción del agente: qué entendió (o no) de cada texto
    libre, con qué capa, y si terminó en solicitud. Alimenta las métricas
    del panel Agente IA (/unmatched). Append-only."""

    id: int | None = Field(default=None, primary_key=True)
    phone: str = Field(index=True)
    text: str  # truncado a 200 caracteres
    # ollama | llm | nombre | sinonimo | keyword | fuzzy | categoria | multi |
    # sugerencia | ninguna | rag (capa informativa de app/rag.py) |
    # rag_descartado (guardrail de la capa informativa tumbó la respuesta;
    # el motivo viaja en service_id) | tools (nodo agéntico de
    # app/agent_tools.py). Las tres últimas conviven con la fila
    # "ninguna"/"sugerencia" del mismo mensaje — ver CAPAS_EXTRA.
    capa: str = Field(index=True)
    # Uno o varios service_id separados por coma (multi/sugerencia). En la
    # capa "rag_descartado" lleva el motivo del guardrail (larga|pii|dinero).
    service_id: str | None = None
    confianza: float | None = None  # solo capa fuzzy (score 0-1)
    termino_en_ticket: bool = False
    created_at: datetime = Field(default_factory=utcnow, index=True)
    habilitado: bool = True
    office_id: int = _office_id_field()


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


class Plantilla(SQLModel, table=True):
    """Plantilla aprobada por Meta, con su identificador en CADA proveedor.

    Fuera de la ventana de 24 h solo pasa una plantilla aprobada. Eso la vuelve
    obligatoria para tres cosas: listas y botones en el canal de Twilio (que no
    acepta el JSON inline de Meta, exige Content Templates), los avisos de
    cobranza ("tu cargo de $X se hace el día D", siempre fuera de ventana), y el
    reenganche de un cliente que lleva horas sin escribir.

    Vive como DATO y no regada en el código porque la MISMA plantilla necesita
    estar aprobada en los dos proveedores, cada uno con su identificador propio
    (`name` en Meta, `ContentSid` en Twilio). Si esos dos ids estuvieran
    hardcodeados en módulos distintos, el día que alguien actualice uno solo los
    dos números divergen en silencio.

    Una plantilla NO se usa en un canal donde no está aprobada: el envío la
    salta y lo registra, en vez de fallar contra la API del proveedor.
    """

    codigo: str = Field(primary_key=True)  # slug estable: "cobro_proximo"
    # utility | marketing | authentication. UTILITY aprueba más rápido y cuesta
    # menos; es la correcta para avisos de trámite y de cobranza.
    categoria: str = "utility"
    # Texto aprobado, con {{1}}, {{2}}… Sirve de preview en el panel; el envío
    # real lo arma el proveedor a partir de su propio identificador.
    texto_referencia: str = ""
    num_variables: int = 0
    meta_template_name: str | None = None
    twilio_content_sid: str | None = None
    aprobada_meta: bool = False
    aprobada_twilio: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    habilitado: bool = True
    office_id: int = _office_id_field()


class Invitation(SQLModel, table=True):
    """Código de un solo uso para que alguien se registre eligiendo su clave.

    Lleva `office_id` porque la oficina se decide al INVITAR, no al registrarse:
    quien acepta la invitación no sabe —ni debe decidir— de qué sede es. Sin
    esto, todo invitado nacía en la oficina 1 por el DEFAULT y había que moverlo
    a mano después, con el rescate de folios que eso implica (users.py)."""

    id: int | None = Field(default=None, primary_key=True)
    token: str = Field(unique=True, index=True)
    phone: str | None = None
    office_id: int = _office_id_field()
    created_by: str
    created_at: datetime = Field(default_factory=utcnow)
    used_at: datetime | None = None
    used_by: str | None = None
    habilitado: bool = True


# ---------------------------------------------------------------------------
# Cobranza con Stripe. Ver docs/DISENO-COBRANZA-STRIPE.md — el diseño explica
# por qué cada campo existe; aquí solo van las restricciones que el código no
# puede mostrar por sí solo.
#
# El dinero SIEMPRE es un entero de centavos. Nunca float, nunca Decimal: un
# float de punto flotante no puede representar $0.10 exacto y en una pasarela
# eso termina en centavos perdidos que no cuadran contra el estado de cuenta.
# ---------------------------------------------------------------------------


class Cobro(SQLModel, table=True):
    """Documento de cobro. `estado` es un read model: siempre se puede
    reconstruir haciendo replay de EventoPasarela ordenado por
    (stripe_created, id). Nunca se decide un estado sin un evento que lo
    respalde."""

    id: int | None = Field(default=None, primary_key=True)
    # Nullable a propósito: el cobro sobrevive al cierre del folio. Con OXXO el
    # cliente paga días después, cuando _latest_open_ticket ya no ve ese ticket.
    ticket_id: int | None = Field(default=None, index=True)
    phone: str = Field(index=True)
    client_name: str | None = None
    service_id: str | None = None
    concepto: str = ""

    # Minúsculas: es lo que exige la API de Stripe. El sufijo "MXN" lo pone la vista.
    moneda: str = "mxn"
    monto_centavos: int = 0
    monto_cotizado_centavos: int | None = None
    monto_catalogo_centavos: int | None = None
    monto_origen: str = "manual"  # cotizado | override | manual
    # Lo COBRADO, que puede diferir de lo pedido. Nunca pisa monto_centavos.
    monto_pagado_centavos: int | None = None
    moneda_pagada: str | None = None
    monto_reembolsado_centavos: int = 0
    # Leídos de la balance transaction de Stripe, jamás calculados por nosotros.
    comision_centavos: int | None = None
    neto_centavos: int | None = None
    moneda_liquidacion: str | None = None
    balance_transaction_id: str | None = None
    # El depósito bancario de Stripe agrupa muchos cobros: sin este id no hay
    # forma de cuadrar el estado de cuenta contra folios individuales.
    payout_id: str | None = Field(default=None, index=True)

    metodos_ofrecidos: str = "card"  # CSV: card,oxxo,spei
    metodo_usado: str | None = None
    msi_meses_ofrecidos: str | None = None  # CSV de plazos: "3,6,9,12"
    msi_meses_usado: int | None = None  # cambia la comisión que absorbe el negocio

    estado: str = Field(default="borrador", index=True)
    estado_motivo: str | None = None

    # Candado anti-doble-cobro. Va por (teléfono, servicio) y NO por folio: con
    # OXXO el folio ya se cerró y el bot fabricó otro, así que un candado por
    # ticket_id no vería nada y se generaría un segundo link cobrable.
    # Solo pasa a NULL cuando Stripe confirma que el instrumento murió.
    llave_cobro_vivo: str | None = None
    # Mata el F5 y el doble clic en "Generar cobro". Nunca se libera.
    llave_generacion: str | None = None
    # Segundo factor al resolver el cobro desde un evento: protege contra ids
    # reciclados si alguna vez se restaura un respaldo.
    token_verificacion: str = Field(unique=True, index=True)

    stripe_checkout_session_id: str | None = None
    # Su presencia significa que hay un instrumento cobrable emitido, aunque la
    # sesión de Checkout ya haya expirado: expirar la sesión NO mata el voucher.
    stripe_payment_intent_id: str | None = None
    stripe_charge_id: str | None = Field(default=None, index=True)
    stripe_customer_id: str | None = None  # requisito de SPEI y semilla del recurrente

    url_pago: str | None = None
    url_expira_at: datetime | None = None
    voucher_url: str | None = None
    # El reloj que de verdad importa en OXXO/SPEI: la sesión expira mucho antes
    # que el voucher, y vencer por la sesión mata cobros todavía pagables.
    voucher_expira_at: datetime | None = None
    # Cola del barrido: el aplicador de eventos no hace red, así que marca aquí
    # "me falta traer el detalle del PaymentIntent".
    detalle_pendiente_at: datetime | None = None

    # Se toma del evento o de la respuesta, no del prefijo de la llave en
    # memoria: así un evento de prueba nunca se aplica a un cobro real.
    livemode: bool | None = None
    discrepancia: bool = False
    disputado: bool = False
    ultimo_evento_id: str | None = None
    ultimo_evento_created: int | None = None  # epoch de Stripe; desempata el rango

    # Confirmación adelantada del asesor contra el recibo del cliente. NO es
    # "pagado": el rango 4 sigue siendo exclusivo de Stripe. Esta bandera
    # desbloquea el trabajo y deja el rastro de quién se adelantó.
    confirmado_por_asesor: bool = False
    confirmado_por: str | None = None
    confirmado_at: datetime | None = None
    # Lo único que amarra el comprobante del cliente a ESTE cobro.
    referencia_recibo: str | None = None

    plan_id: int | None = Field(default=None, index=True)  # bisagra del recurrente
    origen: str = "panel"  # panel | plan
    # Dueño permanente del cobro aunque el folio se reasigne después.
    creado_por: str = Field(default="", index=True)
    cancelado_por: str | None = None

    enviado_at: datetime | None = None
    pagado_at: datetime | None = None
    cancelado_at: datetime | None = None
    # El aviso al cliente sale FUERA del webhook y es reintentable: si Meta
    # falla, no se puede perder la confirmación del pago por eso.
    aviso_pago_enviado_at: datetime | None = None
    reconciliado_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow, index=True)
    updated_at: datetime = Field(default_factory=utcnow)
    # Soft delete. NO entra en el WHERE del índice del candado: borrar un cobro
    # del panel no debe liberar un voucher que el cliente todavía puede pagar.
    habilitado: bool = True
    office_id: int = _office_id_field()


class IntentoCobro(SQLModel, table=True):
    """Append-only: una fila por CADA llamada a la pasarela, escrita ANTES del
    POST. Es el write-ahead log que contesta "el proceso murió y no sé si
    Stripe alcanzó a crear el cobro"."""

    id: int | None = Field(default=None, primary_key=True)
    cobro_id: int = Field(index=True)
    numero: int = 1  # 1, 2, 3… dentro del mismo cobro
    # "cobro:{cobro_id}:{numero}" — se manda a Stripe como Idempotency-Key.
    clave_idempotencia: str = Field(unique=True, index=True)
    operacion: str = "crear_checkout"  # crear_checkout|expirar_sesion|cancelar_pi|consultar
    metodos_solicitados: str = "card"
    msi_meses: str | None = None
    monto_centavos: int = 0  # copia congelada: el cobro pudo cambiar después
    moneda: str = "mxn"
    estado: str = Field(default="enviando", index=True)  # enviando|exitoso|rechazado|error_red|abandonado
    stripe_object: str | None = None  # checkout.session | payment_intent
    stripe_id: str | None = Field(default=None, index=True)
    # Lo primero que pide el soporte de Stripe cuando algo sale mal.
    stripe_request_id: str | None = None
    http_status: int | None = None
    respuesta_json: str | None = None  # recortado a 8 KB antes de guardar
    error_codigo: str | None = None
    error_mensaje: str | None = None
    reintentos: int = 0
    origen: str = "panel"  # panel | cron | webhook
    creado_por: str = ""
    # Liga el dinero con la conversación: permite probar la entrega del link
    # mirando Message.wa_status.
    wa_message_id: int | None = None
    livemode: bool | None = None
    solicitado_at: datetime = Field(default_factory=utcnow)  # ANTES del POST
    # NULL con estado "enviando" significa literalmente "no sé qué pasó".
    respondido_at: datetime | None = None
    cerrado_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow, index=True)
    habilitado: bool = True


class EventoPasarela(SQLModel, table=True):
    """Append-only y única fuente de verdad local. Guarda TODO evento con firma
    válida, incluidos los que no sabemos aplicar: un evento huérfano nunca se
    descarta, se deja en la cola del barrido."""

    id: int | None = Field(default=None, primary_key=True)
    # evt_… de Stripe, o sintético para las transiciones que no vienen de él:
    # "panel:cancel:{cobro_id}:{iso}", "cron:recon:…", "panel:consulta:…".
    # Que TODA transición sea un evento es lo que hace fiel al replay.
    stripe_event_id: str = Field(unique=True, index=True)
    tipo: str = Field(index=True)
    api_version: str | None = None
    livemode: bool | None = None
    # El reloj del orden es el de Stripe, no el nuestro: los eventos llegan
    # desordenados y el nuestro puede estar desfasado.
    stripe_created: int = Field(default=0, index=True)
    recibido_at: datetime = Field(default_factory=utcnow, index=True)
    objeto_id: str | None = Field(default=None, index=True)  # cs_ | pi_ | ch_
    monto_centavos: int | None = None  # para conciliar sin abrir el JSON
    moneda: str | None = None
    cobro_id: int | None = Field(default=None, index=True)  # NULL = huérfano
    intento_id: int | None = Field(default=None, index=True)
    payload_json: str = ""  # crudo, recortado a 32 KB
    procesado_at: datetime | None = Field(default=None, index=True)  # NULL = pendiente
    intentos_proceso: int = 0
    resultado: str | None = Field(default=None, index=True)  # aplicado|duplicado|ignorado|huerfano|error
    detalle: str | None = None
    habilitado: bool = True


class PlanCobro(SQLModel, table=True):
    """Cobro recurrente. INERTE en la primera entrega: la tabla y su espejo
    existen para no volver a pagar los cinco lugares del espejo MSSQL cuando
    llegue el recurrente, pero nada la escribe todavía.

    La suscripción NO cobra: GENERA cobros, cada uno con su
    llave_generacion='plan:{id}:{periodo}' que garantiza exactamente-una-vez."""

    id: int | None = Field(default=None, primary_key=True)
    phone: str = Field(index=True)
    ticket_id: int | None = Field(default=None, index=True)
    service_id: str | None = None
    concepto: str = ""
    moneda: str = "mxn"
    monto_centavos: int = 0
    periodicidad: str = "mensual"  # mensual | quincenal | anual
    # 1-28 y nunca 29-31: febrero no tiene 30 y el cobro se saltaría un mes.
    dia_cobro: int | None = None
    paymentmethod_id: int | None = None
    proxima_ejecucion_at: datetime | None = Field(default=None, index=True)
    ultima_ejecucion_at: datetime | None = None
    ultimo_periodo_generado: str | None = None  # "2026-08": memoria explícita
    cobros_generados: int = 0
    intentos_fallidos_seguidos: int = 0  # dunning
    estado: str = Field(default="activo", index=True)  # activo | pausado | cancelado
    fin_at: datetime | None = None
    stripe_customer_id: str | None = None
    stripe_subscription_id: str | None = None
    creado_por: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    habilitado: bool = True


class PaymentMethod(SQLModel, table=True):
    """Método de pago guardado para cobrar sin el cliente presente. INERTE en
    la primera entrega.

    Nunca guarda el número de tarjeta ni el CVC: solo el identificador pm_ de
    Stripe y los cuatro últimos dígitos. Guardar el PAN nos metería en alcance
    PCI completo, que es justo lo que el diseño evita."""

    id: int | None = Field(default=None, primary_key=True)
    phone: str = Field(index=True)
    stripe_customer_id: str = Field(index=True)
    stripe_payment_method_id: str = Field(unique=True, index=True)
    tipo: str = "card"
    marca: str | None = None
    ultimos4: str | None = None
    exp_mes: int | None = None
    exp_anio: int | None = None
    funding: str | None = None  # credit | debit; determina si aplica MSI
    predeterminado: bool = False
    # Consentimiento explícito para cobrar sin presencia del cliente: sin esto
    # un cargo recurrente es indefendible ante una disputa.
    autorizado_por: str | None = None
    autorizado_at: datetime | None = None
    estado: str = "activo"  # activo | vencido | revocado
    created_at: datetime = Field(default_factory=utcnow)
    habilitado: bool = True


class AgentConfig(SQLModel, table=True):
    """Configuración del agente editable desde /agente (Estudio del Agente).

    Solo claves de la allowlist de app.agent_settings; el .env es el default
    de cada una y los SECRETOS (tokens, API keys, URLs de infra) jamás viven
    aquí. Una fila por clave, con quién la cambió — el detalle queda además
    en AdminAudit."""

    key: str = Field(primary_key=True)
    value: str
    updated_by: str | None = None
    updated_at: datetime = Field(default_factory=utcnow)
    habilitado: bool = True
    office_id: int = _office_id_field()


class InfoDoc(SQLModel, table=True):
    """Párrafo del corpus informativo de la capa RAG (app/rag.py), editable
    desde /agente. Cada fila es un documento del corpus — mismo contrato que
    los párrafos de INFO_DOCS_PATH (que sigue funcionando como fuente
    adicional). Regla dura heredada: solo información pública, sin precios ni
    datos de clientes (el router lo valida al guardar)."""

    id: int | None = Field(default=None, primary_key=True)
    texto: str
    position: int = Field(default=0, index=True)
    habilitado: bool = True
    office_id: int = _office_id_field()
    updated_by: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Office(SQLModel, table=True):
    """Oficina (sede) del ERP — docs/SPEC-MULTI-OFICINA.md §2.1.

    La fila id=1 es Tlaxcala y la siembra db.init_db(): TODO office_id del
    histórico apunta a ella. Nunca se borra; baja = habilitado=0. En el PR 1
    nadie la lee todavía (credencial() es PR 2, el panel /oficinas es PR 3)."""

    id: int | None = Field(default=None, primary_key=True)
    nombre: str = Field(unique=True, index=True)
    slug: str = Field(unique=True, index=True)  # URL del webhook de Stripe (PR 5)
    ciudad: str
    habilitado: bool = True
    created_at: datetime = Field(default_factory=utcnow)


class OfficeCredential(SQLModel, table=True):
    """Llave de proveedor por oficina — docs/SPEC-MULTI-OFICINA.md §2.2.

    Tabla física `officecredential` (SQLModel sin __tablename__, como
    ticketlabel/catalogcategory). NO se espeja a MSSQL a propósito (§2.2, §9):
    no entra a _SCHEMA_DDL ni a MIRROR_TABLES. valor_cif = Fernet(valor):
    bytes -> BLOB. La LEE credenciales.credencial() desde el PR 2 —un SELECT por
    lectura de credencial, y ahí mismo viven cifrar/descifrar—; la ESCRIBE el
    panel del PR 3, así que hoy en producción sigue vacía y todo hereda del
    .env. Unicidad VIVA (office_id, provider, clave) WHERE
    habilitado=1: índice parcial ux_officecredential_viva creado en
    db.init_db(), porque SQLModel no sabe declararlo.

    `clave` es el nombre de la variable de entorno en MAYÚSCULAS
    (WHATSAPP_TOKEN, STRIPE_SECRET_KEY, ...): la forma que el operador ya
    conoce del .env. El atributo de `settings` al que hereda NO se deriva con
    clave.lower() —WHATSAPP_VERIFY_TOKEN es settings.verify_token— así que el
    mapa vive en config.CREDENCIALES y lo resuelve credenciales.credencial()."""

    id: int | None = Field(default=None, primary_key=True)
    office_id: int  # -> office.id; sin FK declarada, igual que TicketLabel.ticket_id
    provider: str  # meta | twilio | stripe
    clave: str
    valor_cif: bytes  # sqlmodel 0.0.22 lo mapea a LargeBinary (BLOB) sin sa_column
    pista: str = ""  # últimos 4 caracteres, lo único que verá el panel (PR 3)
    es_secreto: bool = True  # False solo para identificadores (phone_number_id, whatsapp_from)
    habilitado: bool = True  # baja = apagar, nunca borrar: conserva quién y cuándo
    # NOT NULL a propósito (spec §2.2), a diferencia de AgentConfig/InfoDoc: aquí
    # no hay .env que sea «la fuente»; toda fila la pone un humano o una siembra
    # que se nombra a sí misma ('migracion').
    updated_by: str
    updated_at: datetime = Field(default_factory=utcnow)
