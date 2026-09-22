import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from app.templating import templates
from sqlmodel import select

from app import messaging, ventana
from app.audit import log_admin
from app.clients import conversacion_escribible
from app.auth import is_admin, alcance
from app.config import settings
from app import asignacion
from app.conversation import _latest_open_ticket
from app.db import get_session
from app.media_store import MEDIA_NAME_RE, save_upload, verify_media_token
from app.team_chat import archivo_de_chat_visible
from app.utils import mask_phone
from app.models import (
    PROVIDER_META,
    Client,
    Conversation,
    Direction,
    Message,
    Ticket,
    TicketActivity,
    TicketStatus,
    utcnow,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Reexportado: `app.my` lo lee de aquí. La definición vive en app/ventana.py.
WINDOW_HOURS = ventana.WINDOW_HOURS
QUICK_REPLIES = json.loads((Path(__file__).parent / "quick_replies.json").read_text())


def _current_user(request: Request) -> str | None:
    return request.session.get("user")


def _puede_escribir(session, phone: str, user: str) -> bool:
    """¿Este usuario puede actuar sobre la conversación de este número?

    La regla vive en `clients.conversacion_escribible`, junto al corte de
    lectura: oficina, y para quien no es admin, un folio vigente reclamable por
    él. El guard anterior solo miraba la oficina —y contestaba True sin fila
    Client—, así que un colaborador al que el CRM le negaba el chat podía
    mandar WhatsApp como el negocio, reactivar el bot y ocultar mensajes
    (auditoría 2026-09, AUTHZ-2)."""
    return conversacion_escribible(session, phone, user) is not None


def _time_ago(dt: datetime) -> str:
    seconds = (datetime.now(timezone.utc) - ventana.as_utc(dt)).total_seconds()
    if seconds < 60:
        return "ahora"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


# Filtros de conversación. Una sola definición porque los comparten la Bandeja
# y la fila de filtros de Solicitudes: duplicar los predicados haría divergir el
# número del chip respecto a la lista que ese mismo chip abre.
CONV_FILTERS = {
    "esperando": lambda i: i["waiting"],
    "caducadas": lambda i: not i["window_open"],
    "pausadas": lambda i: i["bot_paused"],
}


def conversation_counts(items: list[dict]) -> dict[str, int]:
    counts = {"todas": len(items)}
    counts.update({k: sum(1 for i in items if pred(i)) for k, pred in CONV_FILTERS.items()})
    return counts


def filter_conversations(items: list[dict], filtro: str | None) -> list[dict]:
    """Aplica un filtro de conversación. Un valor desconocido no filtra nada."""
    pred = CONV_FILTERS.get(filtro or "")
    return [i for i in items if pred(i)] if pred else items


def build_conversation_items(session, vista) -> list[dict]:
    # ponytail: carga todos los mensajes y agrupa en Python — sobra a este
    # volumen; pasar a una consulta agregada si el historial crece a decenas de miles.
    # Una auditoría pidió reescribirlo ya con ROW_NUMBER() OVER (PARTITION BY...)
    # y se rechazó (septiembre de 2026, docs/PLAN-CORRECCION-AUDITORIA.md): sin
    # una medición, y con el volumen de hoy, cambia una función legible por una
    # consulta que solo entiende quien la escribió. Cuando toque, el primer paso
    # NO es la función de ventana sino dejar de hidratar entidades ORM: aquí se
    # usan cuatro columnas de Message y se traen todas.
    
    # Acotamos primero los teléfonos que puede ver esta oficina
    query_conv = select(Conversation)
    if not vista.ve_todas:
        query_conv = query_conv.where(Conversation.office_id == vista.office_id)
    convs = {c.phone: c for c in session.exec(query_conv).all()}
    
    if not vista.ve_todas and not convs:
        return []

    # Se traen todos los mensajes habilitados (o solo de los teléfonos permitidos si está acotado)
    query_msg = select(Message).where(Message.habilitado.is_(True)).order_by(Message.created_at.desc())
    if not vista.ve_todas:
        query_msg = query_msg.where(Message.phone.in_(convs.keys()))
    messages = session.exec(query_msg).all()
    clients = {c.phone: c for c in session.exec(select(Client)).all()}
    tickets: dict[str, Ticket] = {}
    for t in session.exec(select(Ticket).order_by(Ticket.created_at.desc())).all():
        tickets.setdefault(t.phone, t)

    items: dict[str, dict] = {}
    last_in: dict[str, datetime] = {}
    for m in messages:
        if m.phone not in items:
            items[m.phone] = {
                "phone": m.phone,
                "name": (clients.get(m.phone).name if clients.get(m.phone) else None),
                "last_body": m.body,
                "last_direction": m.direction.value,
                "last_at": m.created_at,
                "ago": _time_ago(m.created_at),
            }
        if m.phone not in last_in and m.direction == Direction.IN:
            last_in[m.phone] = m.created_at

    result = []
    for phone, item in items.items():
        conv = convs.get(phone)
        ticket = tickets.get(phone)
        item["waiting"] = item["last_direction"] == "in"
        item["window_open"] = ventana.abierta(last_in.get(phone))
        item["bot_paused"] = bool(conv and conv.state == "escalado")
        item["assigned_to"] = ticket.assigned_to if ticket else None
        item["ticket_id"] = ticket.id if ticket else None
        # Cuenta por la que se atiende a este cliente. El asesor tiene que verlo:
        # la ventana de 24 h es por número, así que "hace 3 h" en un canal no
        # significa lo mismo que en el otro, y el remitente que ve el cliente
        # cambia según la cuenta.
        item["provider"] = conv.provider if conv else PROVIDER_META
        result.append(item)
    return result


# Bandeja apagada: Solicitudes («/») absorbió sus filtros y sus tarjetas.
# Se comenta solo el decorador — la función queda para poder revivir la vista
# con una línea si hiciera falta; /inbox responde 404 mientras tanto.
# @router.get("/inbox")
def inbox_list(request: Request, filter: str | None = None):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    vista = alcance(user)
    with get_session() as session:
        items = build_conversation_items(session, vista)

    counts = conversation_counts(items)
    items = filter_conversations(items, filter)

    return templates.TemplateResponse(
        request, "inbox_list.html",
        {
            "request": request,
            "user": user,
            "items": items,
            "counts": counts,
            "current_filter": filter,
        },
    )


# Un mensaje no lleva folio y no puede llevarlo: cuando el cliente escribe
# «quiero un acta» el folio todavía no existe, y esos son justo los mensajes que
# más importan. Se deriva por tiempo: cada mensaje cae con el alta más cercana,
# así que la frontera entre dos folios es el punto medio entre ellos. Eso pone
# del lado correcto las DOS mitades de un folio: lo que el cliente pidió (antes
# del alta) y lo que se le pidió a él (requisitos y encuesta, después).
#
# El margen agrupa los folios de un mismo turno. Un «quiero un acta y un rfc»
# levanta dos folios con segundos de diferencia; sin margen el segundo se
# quedaría con una ventana vacía.
# ponytail: heurística de tiempo; si no alcanza, la solución real es una columna
# Message.ticket_id llenada al confirmar el alta (y su espejo en MSSQL).
@router.get("/inbox/{phone}")
def inbox_chat(request: Request, phone: str, err: str | None = None):
    """La conversación vive en el CRM (fase B): esta ruta solo reenvía.

    Se conserva por los enlaces guardados y porque los POST de abajo (enviar,
    reactivar el bot, bloquear, ocultar) siguen redirigiendo aquí al terminar:
    el ?err= que traen (window | tipo | peso) viaja al CRM, que lo muestra en su
    compositor. `volver`, `folio` y `todo` ya no significan nada —el CRM tiene su
    propia navegación y muestra la conversación completa— y se descartan, con
    lo que un `volver` fabricado tampoco puede convertirse en un redirect abierto.
    Solo se reenvía un teléfono con forma de teléfono: lo demás cae al tablero.
    """
    if not _current_user(request):
        return RedirectResponse("/login", status_code=303)
    if not re.fullmatch(r"\d{8,15}", phone or ""):
        return RedirectResponse("/crm", status_code=303)
    destino = f"/crm?chat={phone}"
    if err in ("window", "tipo", "peso"):
        destino += f"&err={err}"
    return RedirectResponse(destino, status_code=303)


def _pause_bot(session, phone: str, user: str) -> None:
    """Primer mensaje humano en un chat: la conversación queda escalada y el
    agente IA se apaga solo — sin que el asesor tenga que ir al detalle del
    ticket. Regla: quien responde se queda el chat; si el ticket no tenía
    dueño se le asigna, para que nunca exista un chat con el agente apagado
    y sin asesor responsable. Cada cambio queda en TicketActivity.

    Desde la fase 3b el folio puede llegar aquí YA con dueño de reparto, y esa
    regla habría muerto en silencio: la guarda `assigned_to is None` no se
    cumpliría nunca, así que el trabajo de quien contesta se le acreditaría al
    dueño de reparto en la tabla de equipo. Por eso el dueño cuenta como
    provisional mientras el folio siga en NUEVO (`asignacion.provisional`):
    contestar sigue siendo la forma de quedarse un chat, y en cuanto alguien
    lo trabajó —NUEVO deja de serlo— el dueño ya no se mueve solo.
    """
    conv = session.get(Conversation, phone)
    if conv and conv.state != "escalado":
        conv.state = "escalado"
        session.add(conv)
    ticket = _latest_open_ticket(session, phone)
    if ticket is not None:
        if ticket.assigned_to is None or asignacion.provisional(ticket):
            anterior = ticket.assigned_to
            ticket.assigned_to = user
            ticket.status = TicketStatus.TOMADO
            ticket.status_changed_at = utcnow()
            # Si le quitó el folio a un dueño de reparto, el histórico lo dice:
            # un cambio de dueño sin nadie apareciendo haciéndolo es justo lo
            # que vuelve inauditable la tabla de equipo.
            accion = "tomado" if anterior in (None, user) else f"tomado-de:{anterior}"
            session.add(TicketActivity(ticket_id=ticket.id, username=user, action=accion))
        if ticket.agent_active:
            ticket.agent_active = False
            session.add(
                TicketActivity(ticket_id=ticket.id, username=user, action="agente:pausado-auto")
            )
        ticket.updated_at = utcnow()
        session.add(ticket)
    session.commit()


@router.post("/inbox/{phone}/send")
def inbox_send(
    request: Request,
    phone: str,
    body: str = Form(""),
    file: UploadFile | None = File(None),
):
    """Composer unificado (como WhatsApp): texto, archivo, o archivo con pie.

    La decisión la toma el servidor según lo que llegue, NO el navegador: así
    el composer sigue funcionando con JavaScript deshabilitado. Un input de
    archivo vacío llega como parte sin filename — eso cuenta como "sin
    adjunto"."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    body = body.strip()
    # Duck typing y NO isinstance(file, UploadFile): por HTTP real FastAPI
    # entrega el UploadFile de Starlette (fastapi.UploadFile es una subclase
    # suya, y el validador solo hace cast), así que el isinstance daba False
    # SIEMPRE y todos los adjuntos se descartaban en silencio. Con `filename`
    # se cubren los cuatro casos: sin parte, parte vacía (input sin elegir),
    # marcador File(None) al llamar la función directo, y archivo real.
    adjunto = file if getattr(file, "filename", "") else None
    if not body and adjunto is None:
        return RedirectResponse(f"/inbox/{phone}", status_code=303)

    with get_session() as session:
        if not _puede_escribir(session, phone, user):
            return RedirectResponse("/", status_code=303)
            
        if not ventana.abierta_para(session, phone):
            return RedirectResponse(f"/inbox/{phone}?err=window", status_code=303)

        media_path = None
        # El canal se resuelve UNA vez y sirve para enviar Y para sellar la
        # fila: resolverlo dos veces dejaría divergir el registro del envío.
        canal = messaging.canal_de(phone)
        if adjunto is None:
            wamid = messaging.send_text(phone, body, provider=canal)
            registro = body
        else:
            media_path, disk_path, error = save_upload(adjunto)
            if error:
                return RedirectResponse(f"/inbox/{phone}?err={error}", status_code=303)
            # El texto del composer viaja como pie del archivo, igual que en
            # WhatsApp; si no hay texto se registra el tipo y el nombre.
            wamid = messaging.send_media(
                phone, disk_path, adjunto.content_type, body, adjunto.filename, provider=canal
            )
            kind = "imagen" if (adjunto.content_type or "").startswith("image/") else "documento"
            registro = body or f"[{kind}: {adjunto.filename}]"

        session.add(
            Message(
                phone=phone,
                direction=Direction.OUT,
                body=registro,
                wa_message_id=wamid,
                media_path=media_path,
                sent_by=user,
                provider=canal,
            )
        )
        # Primera respuesta HUMANA (fase 1): sella los folios abiertos de este
        # teléfono que aún no la tienen. El bot nunca pasa por aquí — el inbox
        # es de personas — y por eso este es EL punto de llenado, no _deliver.
        # ponytail: SELECT+loop tiene una carrera de segundos si dos humanos
        # contestan el mismo teléfono a la vez (gana el último commit, con
        # timestamp casi idéntico). El UPDATE atómico la cerraría, pero
        # brincaría los listeners ORM del espejo MSSQL — peor trato. Subir a
        # UPDATE + archivado manual solo si la atribución exacta importa.
        # ponytail: se sellan TODOS los folios abiertos del teléfono (una
        # respuesta contesta la conversación, no un folio); un folio viejo
        # jamás atendido entra como outlier real — la mediana lo aguanta.
        ahora = utcnow()
        for t in session.exec(
            select(Ticket).where(
                Ticket.phone == phone,
                Ticket.status != TicketStatus.CERRADO,
                Ticket.first_response_at.is_(None),  # type: ignore[union-attr]
            )
        ).all():
            t.first_response_at = ahora
            t.first_response_by = user
            session.add(t)
        session.commit()
        _pause_bot(session, phone, user)

    if wamid is None:
        logger.warning("Mensaje a %s registrado pero sin confirmación de envío", mask_phone(phone))
    return RedirectResponse(f"/inbox/{phone}", status_code=303)


@router.post("/inbox/{phone}/messages/{message_id}/delete")
def inbox_delete_message(request: Request, phone: str, message_id: int):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    with get_session() as session:
        if not _puede_escribir(session, phone, user):
            return RedirectResponse("/", status_code=303)
            
        message = session.get(Message, message_id)
        # Solo se puede ocultar un mensaje que el propio asesor envió desde el
        # panel — nunca las palabras del cliente, que son evidencia de la
        # conversación real y deben permanecer intactas.
        if message is not None and message.phone == phone and message.sent_by == user:
            message.habilitado = False
            session.add(message)
            session.commit()
            logger.info("Mensaje #%s ocultado por %s", message_id, user)
    return RedirectResponse(f"/inbox/{phone}", status_code=303)


@router.post("/inbox/{phone}/block")
def inbox_toggle_block(request: Request, phone: str):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not is_admin(user):
        return RedirectResponse(f"/inbox/{phone}", status_code=303)

    with get_session() as session:
        if not _puede_escribir(session, phone, user):
            return RedirectResponse("/", status_code=303)
            
        client = session.get(Client, phone)
        if client is not None:
            client.habilitado = not client.habilitado
            session.add(client)
            session.commit()
            state = "desbloqueado" if client.habilitado else "bloqueado"
            log_admin(user, f"cliente:{state}", phone)
            logger.info("Cliente %s %s por %s", mask_phone(phone), state, user)
    return RedirectResponse(f"/inbox/{phone}", status_code=303)


@router.post("/inbox/{phone}/bot-on")
def inbox_bot_on(request: Request, phone: str):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    with get_session() as session:
        if not _puede_escribir(session, phone, user):
            return RedirectResponse("/", status_code=303)
            
        conv = session.get(Conversation, phone)
        if conv and conv.state == "escalado":
            conv.state = "inicio"
            session.add(conv)
        # Reactivar el bot también reenciende el agente del ticket abierto,
        # con su registro de quién lo hizo (espejo del apagado).
        ticket = _latest_open_ticket(session, phone)
        if ticket is not None and not ticket.agent_active:
            ticket.agent_active = True
            ticket.updated_at = utcnow()
            session.add(ticket)
            session.add(
                TicketActivity(ticket_id=ticket.id, username=user, action="agente:reanudado")
            )
        session.commit()
        logger.info("Bot reactivado para %s por %s", mask_phone(phone), user)
    return RedirectResponse(f"/inbox/{phone}", status_code=303)


@router.get("/media/publico/{name}")
def media_file_publico(request: Request, name: str, exp: str = "", sig: str = ""):
    """Adjunto servido SIN sesión, solo con firma vigente.

    Existe para Twilio, que no acepta subir el archivo y exige una URL de la que
    él lo descarga (ver app/media_store.signed_media_path). Es la única ruta del
    panel sin autenticación, así que la firma y la expiración son lo único que
    separa un acta de cliente de internet abierto: se responde 404 —sin
    distinguir entre firma mala, vencida o archivo inexistente— para no
    confirmarle a nadie qué archivos hay.
    """
    if not verify_media_token(name, exp, sig):
        raise HTTPException(status_code=404, detail="No encontrado")
    path = Path(settings.media_dir) / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No encontrado")
    return FileResponse(path)


@router.get("/media/{name}")
def media_file(request: Request, name: str):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not MEDIA_NAME_RE.match(name):
        # return RedirectResponse("/inbox", status_code=303)  # Bandeja apagada
        return RedirectResponse("/", status_code=303)
    path = Path(settings.media_dir) / name
    if not path.is_file():
        # return RedirectResponse("/inbox", status_code=303)  # Bandeja apagada
        return RedirectResponse("/", status_code=303)
    # Adjunto del chat de equipo: solo lo abre quien ve el mensaje que lo trae.
    # Misma respuesta que «no existe», para no confirmarle a nadie qué hay.
    with get_session() as session:
        if archivo_de_chat_visible(session, f"/media/{name}", user) is False:
            return RedirectResponse("/", status_code=303)
    return FileResponse(path)
