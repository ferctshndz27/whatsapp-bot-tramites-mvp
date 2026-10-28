"""Barrido del alta manual (ext-cse, B5): cierra el lote cuando el colaborador deja de reenviar.

El colaborador reenvía material y se calla; nada lo dispara en el webhook (no llega ningún mensaje),
así que una tarea de fondo revisa cada ~10 s qué lotes llevan `alta_manual_espera_s` en silencio y
le hace UNA pregunta: el número del cliente. El lote pasa de `alta_manual_lote` a
`alta_manual_numero`; B6 atiende lo que conteste.

Tres fases por lote, y la red NUNCA con una sesión abierta (`busy_timeout` son 5 s):
  1. reclamar: UPDATE condicionado al `updated_at` que se leyó. Si el colaborador reenvió algo en el
     intervalo, `updated_at` cambió, el UPDATE no toca nada y el lote sigue su reloj. Eso también
     es lo que impide la doble pregunta si dos barridos coinciden.
  2. enviar, sin sesión.
  3. registrar: el saliente con su wamid y la pregunta marcada «enviada».

El UPDATE de la fase 1 es Core a propósito (la excepción documentada de backend-senior: aquí el
fallo es preguntar dos veces, y un SELECT+UPDATE ORM no es atómico entre sesiones). Cuesta que el
espejo MSSQL no vea ese paso intermedio; lo alcanza la fase 3, que sí escribe por ORM.

Qué pasa si algo falla a medias:
  - Se cae el proceso o el envío devuelve None tras reclamar: la pregunta queda `pendiente` y
    pasada la misma espera se reclama de nuevo, hasta MAX_INTENTOS. «Un reinicio retoma».
  - Se cae entre enviar y registrar: se repite la pregunta. Es el lado barato del error (dos
    preguntas iguales) frente al caro (ninguna y el colaborador esperando).

ponytail: asyncio en el mismo proceso, válido mientras uvicorn corra UN worker (Dockerfile:9). Con
N workers cada uno barrería; el reclamo condicionado evita la doble pregunta, pero el trabajo se
repetiría N veces. Si algún día hay más de un worker, esto pasa a un job único (cron/`app.jobs`).
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, update
from sqlmodel import Session, select
from starlette.concurrency import run_in_threadpool

from app import db, invitacion, messaging
from app.config import settings
from app.conversation import ALTA_LOTE, ALTA_NUMERO, CAPTURE_TTL_HOURS, _log_message
from app.models import Conversation, Direction, Message, Ticket, TicketActivity, User, utcnow
from app.utils import mask_phone, normalize_phone

logger = logging.getLogger(__name__)

# Cada cuánto despierta el bucle. La pregunta sale entre `espera` y `espera + INTERVALO_S` segundos
# después del último reenvío: con 10 s el colaborador no nota la diferencia y la base casi no se toca.
INTERVALO_S = 10
# Reclamos de una misma pregunta (el primero y los reintentos tras un envío fallido).
MAX_INTENTOS = 3

PENDIENTE = "pendiente"
ENVIADA = "enviada"


def _plural(n: int, uno: str, varios: str) -> str:
    return f"{n} {uno if n == 1 else varios}"


def pregunta_numero(mensajes: int, archivos: int) -> str:
    """La pregunta única del agente. No promete nada del folio: solo pide el número."""
    recibido = _plural(mensajes, "mensaje", "mensajes")
    if archivos:
        recibido += f" ({_plural(archivos, 'archivo', 'archivos')})"
    return (
        f"Recibí {recibido}. ¿Cuál es el número de WhatsApp del cliente? "
        "Escríbelo con lada (10 dígitos). Si no quieres abrir un folio, escribe «cancelar»."
    )


def _naive_utc(valor: datetime) -> datetime:
    """SQLite devuelve datetimes naive y `utcnow()` es aware: se comparan ambos como naive-UTC."""
    if valor.tzinfo is not None:
        return valor.astimezone(timezone.utc).replace(tzinfo=None)
    return valor


def _contexto(crudo: str) -> dict:
    try:
        contexto = json.loads(crudo)
    except (json.JSONDecodeError, TypeError):
        return {}
    return contexto if isinstance(contexto, dict) else {}


def _candidatos(session: Session, ahora: datetime) -> list[dict]:
    """Lotes con la espera cumplida y preguntas reclamadas que nunca se confirmaron.

    Los dos casos esperan lo mismo: una pregunta `pendiente` más joven que la espera puede estar
    todavía en vuelo, y reclamarla de nuevo la mandaría dos veces."""
    limite = _naive_utc(ahora) - timedelta(seconds=settings.alta_manual_espera_s)
    candidatos = []
    filas = session.exec(
        select(Conversation).where(Conversation.state.in_([ALTA_LOTE, ALTA_NUMERO]))  # type: ignore[attr-defined]
    ).all()
    for conv in filas:
        contexto = _contexto(conv.context_json)
        if conv.state == ALTA_NUMERO and (
            contexto.get("pregunta") != PENDIENTE or contexto.get("intentos", 0) >= MAX_INTENTOS
        ):
            continue
        if _naive_utc(conv.updated_at) > limite:
            continue
        if _naive_utc(conv.updated_at) < _naive_utc(ahora) - timedelta(hours=CAPTURE_TTL_HOURS):
            continue  # lote caducado: lo descarta el siguiente mensaje, no una pregunta de hace un día
        ids = [i for i in contexto.get("mensajes", []) if isinstance(i, int)]
        archivos = 0
        if ids:
            archivos = session.exec(
                select(func.count())
                .select_from(Message)
                .where(Message.id.in_(ids))
                .where(Message.media_ref != None)  # type: ignore[operator]  # noqa: E711
            ).one()
        candidatos.append({
            "phone": conv.phone,
            "state": conv.state,
            "updated_at": conv.updated_at,
            "contexto": contexto,
            "provider": conv.provider,
            "texto": pregunta_numero(int(contexto.get("total", len(ids))), archivos),
        })
    return candidatos


def _reclamar(session: Session, cand: dict, ahora: datetime) -> bool:
    """Fase 1. True solo si ESTE barrido ganó el lote."""
    contexto = {
        **cand["contexto"], "pregunta": PENDIENTE, "intentos": int(cand["contexto"].get("intentos", 0)) + 1,
    }
    resultado = session.execute(
        update(Conversation)
        .where(Conversation.phone == cand["phone"])  # type: ignore[arg-type]
        .where(Conversation.state == cand["state"])  # type: ignore[arg-type]
        .where(Conversation.updated_at == cand["updated_at"])  # type: ignore[arg-type]
        .values(state=ALTA_NUMERO, context_json=json.dumps(contexto), updated_at=ahora)
    )
    session.commit()
    return resultado.rowcount == 1  # type: ignore[attr-defined]


def _registrar(session: Session, cand: dict, wamid: str) -> None:
    """Fase 3. El saliente se registra siempre (ya salió); la marca solo si el colaborador no movió
    la conversación entre tanto (un «cancelar» en ese intervalo manda)."""
    _log_message(session, cand["phone"], Direction.OUT, cand["texto"], wa_message_id=wamid)
    conv = session.get(Conversation, cand["phone"])
    if conv is not None and conv.state == ALTA_NUMERO:
        contexto = _contexto(conv.context_json)
        contexto["pregunta"] = ENVIADA
        conv.context_json = json.dumps(contexto)
        session.add(conv)
        session.commit()


def barrer_una_vez(ahora: datetime | None = None) -> int:
    """Una pasada. Devuelve cuántas preguntas salieron. Síncrona: corre en el threadpool."""
    if not settings.alta_manual_enabled:
        return 0
    ahora = ahora or utcnow()
    with db.get_session() as session:
        candidatos = _candidatos(session, ahora)
    enviadas = 0
    for cand in candidatos:
        with db.get_session() as session:
            if not _reclamar(session, cand, ahora):
                continue
        # Sin sesión abierta: el envío son hasta ~10 s de red.
        try:
            wamid = messaging.send_text(cand["phone"], cand["texto"], provider=cand["provider"])
        except Exception:  # los clientes salientes no lanzan, pero esto no puede matar el barrido
            logger.exception("Pregunta de alta manual a %s: el envío lanzó", mask_phone(cand["phone"]))
            wamid = None
        if not wamid:
            logger.warning("Pregunta de alta manual a %s sin enviar; se reintenta", mask_phone(cand["phone"]))
            continue
        with db.get_session() as session:
            _registrar(session, cand, wamid)
        enviadas += 1
    return enviadas


# --- Aviso al asignado (B7) ---------------------------------------------------------------------
# El folio que un colaborador deja a OTRO nace en el CRM, pero el dueño no se entera hasta que lo
# abra. El aviso sale por este mismo barrido y la bitácora del folio (`TicketActivity`) es su buzón:
# no hay columna ni tabla nueva, sobrevive a un reinicio y no repite. Marcas:
#   aviso-alta                  salió (y no se repite)
#   aviso-alta-fallo            el envío devolvió None; se reintenta hasta MAX_INTENTOS, con pausa
#   aviso-alta-sin-celular      el asignado no tiene celular registrado; no hay a dónde mandarlo
# Límite conocido: WhatsApp solo admite texto libre dentro de las 24 h desde el último mensaje de
# la persona. Si el asignado no ha escrito al bot en ese lapso el envío falla (Meta 131047), se agotan
# los reintentos y queda el rastro en la bitácora; el folio sigue visible en el CRM. Avisar fuera de
# ventana pide una plantilla aprobada (otra decisión, otro PR).
AVISO_VIGENCIA_H = 24
AVISO_PAUSA_S = 60
_AVISO_LISTO = frozenset({"aviso-alta", "aviso-alta-sin-celular"})


def _texto_aviso(creador: str, ticket: Ticket) -> str:
    cliente = ticket.client_name or "un cliente"
    return (
        f"{creador} te asignó el folio #{ticket.id} ({ticket.service_name}) de {cliente}. "
        "Ábrelo en el CRM para atenderlo."
    )


def _avisos_pendientes(session: Session, ahora: datetime) -> list[dict]:
    desde = _naive_utc(ahora) - timedelta(hours=AVISO_VIGENCIA_H)
    altas = session.exec(
        select(TicketActivity).where(TicketActivity.action == "alta-manual")
        .where(TicketActivity.created_at >= desde)
    ).all()
    pendientes = []
    for alta in altas:
        ticket = session.get(Ticket, alta.ticket_id)
        if ticket is None or not ticket.assigned_to or ticket.assigned_to == alta.username:
            continue  # el folio es del propio creador: nadie a quien avisar
        marcas = session.exec(
            select(TicketActivity).where(TicketActivity.ticket_id == ticket.id)
            .where(TicketActivity.action.in_([*_AVISO_LISTO, "aviso-alta-fallo"]))  # type: ignore[attr-defined]
        ).all()
        if any(m.action in _AVISO_LISTO for m in marcas):
            continue
        fallos = [m for m in marcas if m.action == "aviso-alta-fallo"]
        if len(fallos) >= MAX_INTENTOS:
            continue
        if fallos and _naive_utc(max(m.created_at for m in fallos)) > _naive_utc(ahora) - timedelta(seconds=AVISO_PAUSA_S):
            continue
        dueno = session.exec(select(User).where(User.username == ticket.assigned_to)).first()
        pendientes.append({
            "ticket_id": ticket.id, "creador": alta.username,
            "phone": normalize_phone(dueno.phone) if dueno else "",
            "texto": _texto_aviso(alta.username, ticket),
        })
    return pendientes


def avisar_una_vez(ahora: datetime | None = None) -> int:
    """Una pasada del aviso al asignado. Devuelve cuántos salieron. La red va sin sesión abierta."""
    if not settings.alta_manual_enabled:
        return 0
    ahora = ahora or utcnow()
    with db.get_session() as session:
        pendientes = _avisos_pendientes(session, ahora)
    enviados = 0
    for aviso in pendientes:
        wamid = None
        if aviso["phone"]:
            try:
                wamid = messaging.send_text(aviso["phone"], aviso["texto"])
            except Exception:
                logger.exception("Aviso de alta manual a %s: el envío lanzó", mask_phone(aviso["phone"]))
        with db.get_session() as session:
            if not aviso["phone"]:
                accion = "aviso-alta-sin-celular"
            elif wamid:
                accion = "aviso-alta"
                _log_message(session, aviso["phone"], Direction.OUT, aviso["texto"], wa_message_id=wamid)
            else:
                accion = "aviso-alta-fallo"
                logger.warning("Aviso de alta manual a %s sin enviar", mask_phone(aviso["phone"]))
            session.add(TicketActivity(ticket_id=aviso["ticket_id"], username=aviso["creador"], action=accion))
            session.commit()
        enviados += accion == "aviso-alta"
    return enviados


# --- Invitación desde el número A (B8, app/invitacion.py) ----------------------------------------
# El cliente habló con el número personal del colaborador: para A no tiene ventana y su primer
# mensaje es «iniciado por el negocio». Sale por este mismo barrido (un folio recién creado no
# puede esperar a su dueño), y el mismo rastro de B7 la hace idempotente:
#   aviso-cliente            salió (texto en ventana o plantilla); no se repite
#   aviso-cliente-fallo      el envío devolvió None o la plantilla no está aprobada; se reintenta
#                            con la pausa y el tope de B7 — si el operador aprueba la plantilla,
#                            sale en la siguiente pasada
# Sin opt-in no se manda NADA y no se marca: el barrido vuelve a intentarlo en su siguiente pasada
# (dentro de la vigencia del folio), así un opt-in tardío no deja el folio sin invitación.
_AVISO_CLIENTE_HECHO = "aviso-cliente"
_AVISO_CLIENTE_FALLO = "aviso-cliente-fallo"


def _invitaciones_pendientes(session: Session, ahora: datetime) -> list[dict]:
    desde = _naive_utc(ahora) - timedelta(hours=AVISO_VIGENCIA_H)
    altas = session.exec(
        select(TicketActivity).where(TicketActivity.action == "alta-manual")
        .where(TicketActivity.created_at >= desde)
    ).all()
    pendientes = []
    for alta in altas:
        ticket = session.get(Ticket, alta.ticket_id)
        if ticket is None:
            continue
        marcas = session.exec(
            select(TicketActivity).where(TicketActivity.ticket_id == ticket.id)
            .where(TicketActivity.action.in_([_AVISO_CLIENTE_HECHO, _AVISO_CLIENTE_FALLO]))  # type: ignore[attr-defined]
        ).all()
        if any(m.action == _AVISO_CLIENTE_HECHO for m in marcas):
            continue
        fallos = [m for m in marcas if m.action == _AVISO_CLIENTE_FALLO]
        if len(fallos) >= MAX_INTENTOS:
            continue
        if fallos and _naive_utc(max(m.created_at for m in fallos)) > _naive_utc(ahora) - timedelta(seconds=AVISO_PAUSA_S):
            continue
        pendientes.append({"ticket_id": ticket.id, "creador": alta.username, "ticket": ticket})
    return pendientes


def invitar_una_vez(ahora: datetime | None = None) -> int:
    """Una pasada de invitaciones desde A. Devuelve cuántas salieron. La red va sin sesión abierta."""
    if not settings.alta_manual_enabled:
        return 0
    ahora = ahora or utcnow()
    with db.get_session() as session:
        pendientes = _invitaciones_pendientes(session, ahora)
    salidas = 0
    for cand in pendientes:
        with db.get_session() as session:
            estado, cuerpo, plantilla, variables = invitacion.preparar(session, cand["ticket"], cand["creador"])
        wamid = None
        if estado in ("texto", "plantilla"):
            wamid = invitacion.enviar(cand["ticket"], estado, cuerpo, plantilla, variables)
        with db.get_session() as session:
            session.get(Ticket, cand["ticket_id"])  # refrescar tras el envío
            if estado == "sin-consentir":
                continue  # no se marca: el opt-in tardío la desbloquea en la siguiente pasada
            accion = _AVISO_CLIENTE_HECHO if wamid else _AVISO_CLIENTE_FALLO
            if not wamid:
                logger.warning("Invitación de A para el folio %s sin enviar (%s)", cand["ticket_id"], estado)
            invitacion.registrar(session, cand["ticket"], estado, cuerpo, wamid)
            session.add(
                TicketActivity(ticket_id=cand["ticket_id"], username=cand["creador"], action=accion)
            )
            session.commit()
        salidas += bool(wamid)
    return salidas


def _pasada() -> None:
    barrer_una_vez()
    avisar_una_vez()
    invitar_una_vez()


async def bucle(intervalo: float = INTERVALO_S) -> None:
    """Tarea de fondo del `lifespan`. Una pasada que revienta se registra y el bucle sigue: un
    barrido muerto es un colaborador esperando una pregunta que no llega, y nadie lo vería."""
    while True:
        try:
            await run_in_threadpool(_pasada)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Barrido del alta manual falló")
        await asyncio.sleep(intervalo)
