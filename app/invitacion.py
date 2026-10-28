"""Invitación desde el número A (ext-cse, B8): el primer contacto con el cliente del folio manual.

El cliente habló con el número PERSONAL del colaborador, no con A: para el bot no existe ventana
suya y el primer mensaje de A es «iniciado por el negocio». Dos caminos:

  - Ventana abierta (el cliente sí ha escrito a A en las últimas 24 h): texto plano, gratis.
    La ventana ES el permiso; no se le exige opt-in por encima.
  - Sin ventana: solo una PLANTILLA aprobada sale (Twilio Content API / plantilla de Meta). Y solo
    si el cliente tiene `opt_in_at` registrado — el diálogo lo registra al crear el folio
    (ALTA_CONSENT), y el operador puede registrarlo después desde la ficha. Sin opt-in, el barrido
    no manda nada y vuelve a intentarlo en su siguiente pasada, mientras dure la vigencia del folio:
    así un opt-in tardío no deja el folio sin invitación.

Degradación (v3 §7): si la plantilla no está aprobada o el envío falla, el folio sigue existiendo,
el colaborador sigue por su número y la ficha muestra «sin ventana en A»; el rastro queda en la
bitácora del folio (`aviso-cliente*`). La aprobación de Meta se configura en producción
(`Plantilla.aprobada_meta` y su ContentSid); localmente no se puede probar.
"""

from datetime import timedelta

from sqlmodel import Session, select

from app import messaging
from app.conversation import _log_message
from app.models import Client, Direction, Message, Plantilla, Ticket, TicketActivity, utcnow

CODIGO_PLANTILLA = "invitacion_folio"


def _cliente(session: Session, phone: str) -> Client | None:
    return session.get(Client, phone)


def consentido(client: Client | None) -> bool:
    """El opt-in se registra ANTES de enviar (v3 §4.5); sin él no sale plantilla."""
    return bool(client and client.opt_in_at)


def ventana_abierta(session: Session, phone: str, horas: int | float = 24) -> bool:
    """Un entrante de ESTE número en el lapso abre la ventana de 24 h."""
    limite = utcnow() - timedelta(hours=horas)
    return bool(
        session.exec(
            select(Message.id)
            .where(Message.phone == phone)
            .where(Message.direction == Direction.IN)  # type: ignore[arg-type]
            .where(Message.created_at >= limite)
            .limit(1)
        ).first()
    )


def plantilla_de_invitacion(session: Session) -> Plantilla | None:
    """La plantilla aprobada, o None: no existe, está dada de baja o Meta no la aprobó.

    `aprobada_twilio` no se exige aquí: el SID de Twilio y el nombre de Meta viven en la misma fila
    y el envío usa el del proveedor en turno; si falta el del suyo, el envío devuelve None y la
    bitácora lo marca. Exigir ambas aprobaciones haría que un cambio de proveedor bloqueara el
    camino ya aprobado."""
    p = session.get(Plantilla, CODIGO_PLANTILLA)
    if p is None or not p.habilitado or not p.aprobada_meta:
        return None
    return p


def _texto_plain(cliente: Client, creador: str, ticket: Ticket) -> str:
    quien = f"Hola, {cliente.name.split()[0]}. " if cliente.name else "Hola. "
    return (
        f"{quien}{creador} registró tu folio #{ticket.id} ({ticket.service_name}). "
        "Responde este mensaje para que te atendamos."
    )


def _variables_plantilla(cliente: Client, creador: str, ticket: Ticket) -> list[str]:
    """Variables {{1}}…{{4}} en el orden del texto aprobado (v3 §4.3)."""
    nombre = cliente.name.split()[0] if cliente and cliente.name else "cliente"
    return [nombre, creador, str(ticket.id), ticket.service_name or "trámite"]


def preparar(
    session: Session, ticket: Ticket, creador: str
) -> tuple[str, str | None, Plantilla | None, list[str]]:
    """Decide el camino y devuelve (estado, cuerpo, plantilla, variables).

    estados posibles: texto · plantilla · sin-consentir · sin-plantilla. `fallo` no se decide
    aquí: lo decide quien llama con el resultado del envío. SIN red: solo lee.
    """
    cliente = _cliente(session, ticket.phone)
    if ventana_abierta(session, ticket.phone):
        return "texto", _texto_plain(cliente, creador, ticket), None, None
    if not consentido(cliente):
        return "sin-consentir", None, None, None
    p = plantilla_de_invitacion(session)
    if p is None:
        return "sin-plantilla", None, None, None
    variables = _variables_plantilla(cliente, creador, ticket)
    return "plantilla", None, p, variables


def enviar(
    ticket: Ticket, estado: str, cuerpo: str | None, plantilla: Plantilla | None,
    variables: list[str], provider: str | None = None,
) -> str | None:
    """El envío en sí (llamar SIN sesión abierta)."""
    if estado == "texto":
        return messaging.send_text(ticket.phone, cuerpo or "", provider=provider)
    if estado == "plantilla" and plantilla is not None:
        return messaging.send_template(
            ticket.phone, plantilla.meta_template_name, plantilla.twilio_content_sid,
            variables, provider=provider,
        )
    return None


def registrar(
    session: Session, ticket: Ticket, estado: str, cuerpo: str | None, wamid: str | None
) -> None:
    """El saliente queda registrado (con su wamid) para que el webhook de estado
    actualice su `wa_status`: «el `Message` queda con su estado de entrega»."""
    if wamid:
        _log_message(session, ticket.phone, Direction.OUT, cuerpo or "", wa_message_id=wamid)
