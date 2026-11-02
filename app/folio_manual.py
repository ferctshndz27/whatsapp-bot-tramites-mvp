"""Alta manual de un folio (ext-cse): el colaborador atendió al cliente por su
teléfono personal y deja el folio en el CRM.

Qué NO hace, a propósito:
- No habla con el cliente ni con el colaborador: el diálogo que junta estos datos
  (número, trámite, asignado) vive en `conversation.py`; aquí solo se valida y se
  escribe. Así cada regla se prueba sin simular WhatsApp.
- No usa `asignacion.elegir*`: el colaborador decide a quién le toca, no el
  reparto. Tampoco `asignacion.bitacora`, que escribe `asignado-auto` con actor
  «sistema» y aquí sería mentira: la bitácora dice quién lo dio de alta y a quién
  se lo pasó.
- No consulta `asignacion.puede_tomar`: esa regla decide quién se queda un chat
  al contestar; aquí el folio es nuevo y el dueño lo elige una persona.

Todo se valida ANTES de escribir: un rechazo no deja un cliente a medias.

ponytail: dos altas simultáneas del mismo cliente y trámite podrían pasar la
revisión de repetidos a la vez (no hay índice único; es la misma carrera que
tiene el alta del bot, `conversation._handle_confirmar`). Si deja de bastar, un
índice único parcial sobre (phone, service_id) de folios abiertos.
"""

import logging
from collections.abc import Sequence

from sqlmodel import Session, select

from app import horario
from app.auth import alcance
from app.catalog import catalog, requisitos_iniciales
from app.clients import MAX_NOMBRE
from app.conversation import (
    CASO_SERVICE_ID,
    MAX_FOLIOS_ABIERTOS,
    _folios_abiertos,
    _ligar_mensajes,
)
from app.models import PROVIDER_META, Client, Message, Ticket, TicketActivity, TicketStatus, User, utcnow
from app.utils import mask_phone, normalize_phone

logger = logging.getLogger(__name__)

ORIGEN_MANUAL = "manual"


class FolioManualError(Exception):
    """Rechazo con un `codigo` estable para que el diálogo lo traduzca al
    colaborador: creador · telefono · nombre · servicio · otra_oficina ·
    cliente_inhabilitado · asignado · repetido · tope. `folio_id` solo en
    `repetido`: el folio que ya existe."""

    def __init__(self, codigo: str, mensaje: str = "", folio_id: int | None = None):
        super().__init__(mensaje or codigo)
        self.codigo = codigo
        self.folio_id = folio_id


def _usuario_habilitado(session: Session, username: str | None) -> User | None:
    if not username:
        return None
    return session.exec(
        select(User).where(User.username == username).where(User.habilitado == True)  # noqa: E712
    ).first()


def crear_folio_manual(
    session: Session,
    creador: str,
    phone: str | None,
    nombre: str | None,
    service_id: str,
    asignado_a: str,
    mensajes: Sequence[Message] = (),
    notas: str = "",
    provider: str = PROVIDER_META,
) -> Ticket:
    """Crea el folio (y el cliente, si es nuevo) y lo devuelve ya con `id`.

    Si `asignado_a` es el propio creador el folio nace TOMADO (él lo atiende);
    si es otro, nace NUEVO con dueño, como un folio auto-asignado. Siempre con
    el agente IA apagado y `origen='manual'`. `provider` es la cuenta por la que
    el colaborador habló con el agente: el cliente no tiene conversación, así que
    el folio es lo único que dice por dónde invitarlo (`messaging.canal_de`).
    """
    # --- Validación: nada se escribe hasta que todo cuadra ------------------
    autor = _usuario_habilitado(session, creador)
    if autor is None:
        raise FolioManualError("creador", "el creador no existe o está deshabilitado")

    canonico = normalize_phone(phone)
    if not canonico:
        raise FolioManualError("telefono", "teléfono inservible")

    service = catalog.service_by_id(service_id)
    if service is None:
        raise FolioManualError("servicio", f"el trámite {service_id!r} no existe")

    # `alcance` abre su propia sesión: se resuelve antes de la primera escritura
    # (si no, en la suite su close() revertiría lo pendiente de esta sesión).
    vista = alcance(creador)
    cliente = session.get(Client, canonico)
    if cliente is not None:
        if not vista.alcanza(cliente.office_id):
            raise FolioManualError("otra_oficina", "el cliente es de otra oficina")
        if not cliente.habilitado:
            raise FolioManualError("cliente_inhabilitado", "el cliente está dado de baja")
        oficina = cliente.office_id
        nombre_folio = cliente.name or (nombre or "").strip()[:MAX_NOMBRE] or None
    else:
        nombre_folio = (nombre or "").strip()[:MAX_NOMBRE]
        if not nombre_folio:
            raise FolioManualError("nombre", "falta el nombre del cliente nuevo")
        oficina = vista.office_id

    dueno = _usuario_habilitado(session, asignado_a)
    if dueno is None or dueno.office_id != oficina:
        raise FolioManualError("asignado", "el asignado no existe, está deshabilitado o es de otra oficina")

    abiertos = _folios_abiertos(session, canonico)
    repetido = next((t for t in abiertos if t.service_id == service.id), None)
    if repetido is not None:
        raise FolioManualError("repetido", "ya hay un folio abierto de ese trámite", repetido.id)
    # Mismo criterio que el bot: los casos para análisis no ocupan cupo.
    if sum(1 for t in abiertos if t.service_id != CASO_SERVICE_ID) >= MAX_FOLIOS_ABIERTOS:
        raise FolioManualError("tope", "el cliente ya tiene el máximo de folios abiertos")

    # --- Escritura: un solo commit -----------------------------------------
    if cliente is None:
        session.add(Client(phone=canonico, name=nombre_folio, origen=ORIGEN_MANUAL, office_id=oficina))

    propio = dueno.username == autor.username
    ahora = utcnow()
    sello = horario.ahora().strftime("%Y-%m-%d %H:%M")
    ticket = Ticket(
        phone=canonico,
        client_name=nombre_folio,
        category=service.category_name,
        service_id=service.id,
        service_name=service.name,
        price_info=service.price_info,
        status=TicketStatus.TOMADO if propio else TicketStatus.NUEVO,
        status_changed_at=ahora if propio else None,
        assigned_to=dueno.username,
        agent_active=False,
        origen=ORIGEN_MANUAL,
        office_id=oficina,
        provider=provider,
        requirements_json=requisitos_iniciales(service.requirements),
        notes=f"[alta manual, {autor.username}, {sello}] {notas.strip()}" if notas.strip() else None,
    )
    session.add(ticket)
    session.commit()
    session.refresh(ticket)

    # Después del commit: la bitácora necesita el id del folio.
    session.add(TicketActivity(ticket_id=ticket.id, username=autor.username, action="alta-manual"))
    if not propio:
        session.add(TicketActivity(
            ticket_id=ticket.id, username=autor.username, action=f"reasignado:{dueno.username}",
        ))
    if mensajes:
        _ligar_mensajes(session, ticket, list(mensajes))
    session.commit()
    session.refresh(ticket)
    logger.info(
        "Folio #%s manual de %s para %s, asignado a %s",
        ticket.id, autor.username, mask_phone(canonico), dueno.username,
    )
    return ticket
