import logging
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Form, Request, UploadFile, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse, PlainTextResponse, Response
from app.templating import templates
from sqlalchemy import func
from sqlmodel import select, or_, and_

from app.auth import is_admin, alcance
from app.db import get_session
from app.media_store import save_upload
from app.models import (
    TeamMessage, User, ChatRoom, ChatMember,
    MessageReaction, MessageReminder, utcnow
)

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_MESSAGES_SHOWN = 100
MAX_BODY_LENGTH = 4000
MAX_REMIND_MINUTES = 7 * 24 * 60
# Las que ofrece la burbuja. Texto libre dejaba guardar cualquier cadena, de
# cualquier largo, que luego se pinta a todos los de la sala.
REACCIONES = frozenset({"👍", "❤️"})
# Lo que ve el usuario cuando save_upload() rechaza un adjunto (team.js lo
# muestra tal cual).
ERRORES_ADJUNTO = {
    "tipo": "Ese tipo de archivo no se puede enviar. Usa imagen (JPG, PNG o WEBP), PDF, Word o Excel.",
    "peso": "El archivo es demasiado grande: hasta 5 MB en imágenes y 16 MB en documentos.",
}

def _current_user(request: Request) -> str | None:
    return request.session.get("user")

def _miembro_activo(session, room_id: int, user: str) -> ChatMember | None:
    """Su membresía, solo si está activa y la sala también. Es el candado de
    todo lo que lee o escribe en una sala: el `room_id` de la ruta lo pone el
    navegador, así que nunca basta con que la sala exista."""
    return session.exec(
        select(ChatMember)
        .join(ChatRoom, ChatRoom.id == ChatMember.room_id)
        .where(ChatMember.room_id == room_id, ChatMember.username == user,
               ChatMember.habilitado == True, ChatRoom.habilitado == True)  # noqa: E712
    ).first()

def _mensaje_visible(session, message_id: int, user: str) -> TeamMessage | None:
    """El mensaje, solo si sigue visible y `user` es miembro activo de su sala."""
    return session.exec(
        select(TeamMessage)
        .join(ChatMember, and_(ChatMember.room_id == TeamMessage.room_id,
                               ChatMember.username == user))
        .join(ChatRoom, ChatRoom.id == TeamMessage.room_id)
        .where(TeamMessage.id == message_id, TeamMessage.habilitado == True,
               ChatMember.habilitado == True, ChatRoom.habilitado == True,  # noqa: E712
               _posterior_al_borrado())
    ).first()

def _posterior_al_borrado():
    """Condición SQL (con TeamMessage y ChatMember en el join): el mensaje es
    posterior a cuando ese miembro eliminó el chat, o nunca lo eliminó. Lo de
    antes ya no es suyo de ver, como en WhatsApp."""
    return or_(ChatMember.borrado_at == None,  # noqa: E711
               TeamMessage.created_at > ChatMember.borrado_at)

def _hay_mensajes_despues(session, room_id: int, momento) -> bool:
    return session.exec(
        select(TeamMessage.id)
        .where(TeamMessage.room_id == room_id, TeamMessage.habilitado == True,  # noqa: E712
               TeamMessage.created_at > momento)
        .limit(1)
    ).first() is not None

def archivo_de_chat_visible(session, media_path: str, user: str) -> bool | None:
    """¿`user` puede abrir este adjunto del chat de equipo?

    None si el archivo no es del chat (adjunto de cliente de la bandeja: lo
    decide su propia regla en inbox.media_file). Si es del chat, solo lo abre
    quien ve el mensaje que lo trae: miembro activo, sala activa y sin haber
    eliminado el chat después.
    # ponytail: teammessage.media_path no tiene índice — un escaneo por
    # descarga. Indexar si el chat llega a decenas de miles de mensajes."""
    ids = session.exec(select(TeamMessage.id).where(TeamMessage.media_path == media_path)).all()
    if not ids:
        return None
    return any(_mensaje_visible(session, i, user) is not None for i in ids)

def _nombre_de_sala(session, room: ChatRoom, user: str) -> str:
    """Como en WhatsApp: el DM lleva el nombre del otro, y el DM consigo mismo
    el suyo con "(tú)" — ahí no hay otro integrante que nombrar."""
    if room.room_type != "direct":
        return room.name or "Grupo"
    otro = session.exec(
        select(ChatMember.username).where(ChatMember.room_id == room.id, ChatMember.username != user)
    ).first()
    return otro or f"{user} (tú)"


def _dm_existente(session, user: str, otro: str) -> int | None:
    """El DM que ya une a `user` con `otro` (consigo mismo si otro == user).

    Compara el conjunto EXACTO de integrantes: con solo buscar a `otro`, el DM
    consigo mismo casaba con cualquier DM suyo. Cuenta aunque la sala o una
    membresía estén desactivadas — ese se renueva, no se abre un segundo."""
    mis_dms = session.exec(
        select(ChatMember.room_id)
        .join(ChatRoom, ChatRoom.id == ChatMember.room_id)
        .where(ChatMember.username == user, ChatRoom.room_type == "direct")
    ).all()
    if not mis_dms:
        return None
    integrantes: dict[int, set[str]] = {}
    for room_id, username in session.exec(
        select(ChatMember.room_id, ChatMember.username).where(ChatMember.room_id.in_(mis_dms))
    ).all():
        integrantes.setdefault(room_id, set()).add(username)
    buscado = {user, otro}
    return next((rid for rid in sorted(integrantes) if integrantes[rid] == buscado), None)


def _renovar_dm(session, room_id: int) -> None:
    """Un DM desactivado (la sala, o la membresía de alguno) vuelve a la vista
    de los dos con su historial. Escritura ORM: el espejo MSSQL la ve."""
    room = session.get(ChatRoom, room_id)
    apagadas = session.exec(
        select(ChatMember).where(ChatMember.room_id == room_id, ChatMember.habilitado == False)  # noqa: E712
    ).all()
    if room.habilitado and not apagadas:
        return
    room.habilitado = True
    for m in apagadas:
        m.habilitado = True
    session.add(room)
    session.add_all(apagadas)
    session.commit()


@router.get("/team")
def team_chat(request: Request, err: str | None = None, room: int | None = None):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    vista = alcance(user)
    with get_session() as session:
        # Check if user has a general office room
        general_name = f"Oficina General #{vista.office_id}" if not vista.ve_todas else "Oficina General"
        general_room = session.exec(
            select(ChatRoom).where(
                ChatRoom.name == general_name,
                ChatRoom.room_type == "general",
                ChatRoom.habilitado == True
            )
        ).first()

        if not general_room:
            # Create a general room for the office
            general_room = ChatRoom(
                name=general_name,
                room_type="general",
            )
            session.add(general_room)
            session.commit()
            
            # Map old messages with no room_id to this general room
            old_msgs = session.exec(
                select(TeamMessage).where(TeamMessage.room_id == None, TeamMessage.office_id == vista.office_id)
            ).all()
            for msg in old_msgs:
                msg.room_id = general_room.id
                session.add(msg)
            session.commit()

        # Ensure user is member of general room
        if not session.exec(select(ChatMember).where(ChatMember.room_id == general_room.id, ChatMember.username == user)).first():
            session.add(ChatMember(room_id=general_room.id, username=user))
            session.commit()

        # Get all rooms the user is in
        my_memberships = session.exec(select(ChatMember).where(ChatMember.username == user, ChatMember.habilitado == True)).all()
        room_ids = [m.room_id for m in my_memberships]
        
        rooms_out = []
        if room_ids:
            rooms_db = session.exec(select(ChatRoom).where(ChatRoom.id.in_(room_ids), ChatRoom.habilitado == True)).all()
            # Chat eliminado «solo para mí» (borrado_at): no sale en su lista hasta
            # que alguien escriba de nuevo, o hasta que él lo pida por ?room=
            # (abrir otra vez el DM) — igual que en WhatsApp.
            borrados = {m.room_id: m.borrado_at for m in my_memberships if m.borrado_at}
            for r in rooms_db:
                if (r.id in borrados and r.id != room
                        and not _hay_mensajes_despues(session, r.id, borrados[r.id])):
                    continue
                rooms_out.append({"id": r.id, "display_name": _nombre_de_sala(session, r, user)})
            
        # Con quién puede abrir un DM o un grupo: la gente de su oficina, o la
        # de todas si es admin (§5), igual que en el resto del panel.
        users_query = select(User).where(User.habilitado == True, User.username != user)
        office_users = session.exec(vista.filtrar(users_query, User.office_id)).all()

    # `?room=` abre esa sala en vez de la general: a él redirige crear una sala
    # (o pedir un DM que ya existe) y ahí lo deja la auto-recarga de 20 s. Solo
    # se honra si es una de SUS salas — si no, cae a la general sin avisar.
    mis_salas = {r["id"] for r in rooms_out}
    sala_inicial = room if room in mis_salas else general_room.id

    return templates.TemplateResponse(
        "team_chat.html",
        {
            "request": request,
            "user": user,
            "rooms": rooms_out,
            "office_users": office_users,
            "error": err,
            "general_room_id": general_room.id,
            "sala_inicial": sala_inicial,
        },
    )

@router.get("/team/rooms/{room_id}/messages")
def team_room_messages(request: Request, room_id: int):
    user = _current_user(request)
    if not user:
        return Response(status_code=403)
        
    with get_session() as session:
        membership = _miembro_activo(session, room_id, user)
        room = session.get(ChatRoom, room_id)
        if not membership or not room or not room.habilitado:
            return HTMLResponse("<div class='alert'>No tienes acceso a esta sala.</div>")

        consulta = select(TeamMessage).where(TeamMessage.room_id == room_id, TeamMessage.habilitado == True)  # noqa: E712
        if membership.borrado_at:
            # Eliminó el chat: lo anterior ya no es suyo de ver, como en WhatsApp.
            consulta = consulta.where(TeamMessage.created_at > membership.borrado_at)
        recent = session.exec(
            consulta.order_by(TeamMessage.created_at.desc()).limit(MAX_MESSAGES_SHOWN)
        ).all()
        
        display_name = _nombre_de_sala(session, room, user)
        
        messages = list(reversed(recent))
        
        # Get reactions
        msg_ids = [m.id for m in messages]
        reactions = {}
        if msg_ids:
            rxns = session.exec(select(MessageReaction).where(MessageReaction.message_id.in_(msg_ids))).all()
            for rx in rxns:
                reactions.setdefault(rx.message_id, []).append(rx)

    return templates.TemplateResponse(
        "partials/team_messages.html",
        {
            "request": request,
            "user": user,
            "room": room,
            "room_display_name": display_name,
            "messages": messages,
            "reactions": reactions,
            "is_muted": membership.is_muted
        },
    )

@router.post("/team/rooms")
def create_room(request: Request, room_type: str = Form(...), name: str = Form(""), members: List[str] = Form([])):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    vista = alcance(user)
    with get_session() as session:
        # La lista del formulario ya viene acotada, pero `members` lo arma el
        # navegador: sin volver a acotarlo aquí, un POST hecho a mano mete a
        # alguien de otra oficina a la sala. Con uno solo que no sea elegible
        # no se crea nada — una sala a medias tampoco es la que se pidió.
        pedidos = {m for m in members if m != user}
        if pedidos:
            elegibles = select(User.username).where(
                User.username.in_(sorted(pedidos)), User.habilitado == True
            )
            fuera = pedidos - set(session.exec(vista.filtrar(elegibles, User.office_id)).all())
            if fuera:
                logger.warning(
                    "Sala de equipo rechazada: %s pidió integrantes fuera de su alcance: %s",
                    user, sorted(fuera),
                )
                return RedirectResponse("/team", status_code=303)

        is_dm = len(members) == 1 and not name.strip()
        actual_type = "direct" if is_dm else "group"
        
        if is_dm:
            # members[0] puede ser él mismo: el DM consigo mismo es válido.
            existing = _dm_existente(session, user, members[0])
            if existing:
                _renovar_dm(session, existing)
                # Lo lleva a la conversación que ya tiene, no a la general.
                return RedirectResponse(f"/team?room={existing}", status_code=303)
                    
        room = ChatRoom(room_type=actual_type, name=name.strip() if not is_dm else None)
        session.add(room)
        session.commit()
        session.refresh(room)
        
        session.add(ChatMember(room_id=room.id, username=user))
        for m in members:
            if m != user:
                session.add(ChatMember(room_id=room.id, username=m))
        session.commit()
        room_id = room.id

    return RedirectResponse(f"/team?room={room_id}", status_code=303)

@router.post("/team/rooms/{room_id}/send")
def team_send(request: Request, room_id: int, body: str = Form(...)):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    body = body.strip()
    if body:
        vista = alcance(user)
        with get_session() as session:
            if _miembro_activo(session, room_id, user):
                session.add(TeamMessage(room_id=room_id, author=user, body=body[:MAX_BODY_LENGTH], office_id=vista.office_id))
                session.commit()
    # team.js manda hx-request y recibe el hilo ya pintado (con el mismo candado).
    if "hx-request" in request.headers:
        return team_room_messages(request, room_id)
    return RedirectResponse(f"/team?room={room_id}", status_code=303)

@router.post("/team/rooms/{room_id}/send-media")
def team_send_media(request: Request, room_id: int, file: UploadFile, caption: str = Form("")):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    # La membresía va ANTES de guardar: al revés, cualquiera con sesión dejaba
    # archivos en disco apuntando a una sala ajena, aunque el mensaje no naciera.
    hx = "hx-request" in request.headers
    with get_session() as session:
        if _miembro_activo(session, room_id, user) is None:
            return team_room_messages(request, room_id) if hx else RedirectResponse("/team", status_code=303)

    web_path, _disk_path, error = save_upload(file)
    if error:
        if hx:
            # team.js lo enseña en un aviso; el adjunto sigue en la vista previa.
            return PlainTextResponse(ERRORES_ADJUNTO[error], status_code=422)
        return RedirectResponse(f"/team?err={error}", status_code=303)

    vista = alcance(user)
    with get_session() as session:
        session.add(
            TeamMessage(
                room_id=room_id,
                author=user,
                body=caption.strip()[:MAX_BODY_LENGTH] or f"[archivo: {file.filename}]",
                media_path=web_path,
                office_id=vista.office_id,
            )
        )
        session.commit()
    if hx:
        return team_room_messages(request, room_id)
    return RedirectResponse(f"/team?room={room_id}", status_code=303)

@router.post("/team/rooms/{room_id}/mute")
def team_mute_room(request: Request, room_id: int, mute: bool = Form(...)):
    user = _current_user(request)
    if not user: return Response(status_code=403)

    with get_session() as session:
        membership = _miembro_activo(session, room_id, user)
        if membership:
            membership.is_muted = mute
            session.add(membership)
            session.commit()
    if "hx-request" in request.headers:
        return team_room_messages(request, room_id)
    return RedirectResponse(f"/team?room={room_id}", status_code=303)

@router.post("/team/rooms/{room_id}/delete")
def team_delete_room(request: Request, room_id: int):
    """«Eliminar chat» como en WhatsApp: desaparece solo para él.

    Soft delete: no se borra nada — se marca `borrado_at` en SU membresía. Deja
    de ver lo anterior; los demás no se enteran; si alguien escribe otra vez,
    la sala vuelve a su lista solo con lo nuevo. La Oficina General no se
    elimina: es la sala de todos por oficina y se recrearía sola."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    with get_session() as session:
        membership = _miembro_activo(session, room_id, user)
        room = session.get(ChatRoom, room_id)
        if membership is not None and room.room_type != "general":
            membership.borrado_at = utcnow()
            session.add(membership)
            session.commit()
            logger.info("Chat de equipo #%s eliminado para %s", room_id, user)
    return RedirectResponse("/team", status_code=303)

@router.post("/team/messages/{message_id}/react")
def team_react(request: Request, message_id: int, reaction: str = Form(...)):
    user = _current_user(request)
    if not user: return Response(status_code=403)

    with get_session() as session:
        # Antes bastaba haber sido miembro alguna vez (sin mirar `habilitado`),
        # y un id inexistente tronaba con 500. Mismo 404 si no existe o no es
        # suyo, como en los recordatorios: no confirma cuál.
        msg = _mensaje_visible(session, message_id, user)
        if msg is None or reaction not in REACCIONES:
            return Response(status_code=404)
        room_id = msg.room_id
        # Toggle reaction
        existing = session.exec(select(MessageReaction).where(MessageReaction.message_id == message_id, MessageReaction.username == user, MessageReaction.reaction == reaction)).first()
        if existing:
            session.delete(existing)
        else:
            session.add(MessageReaction(message_id=message_id, username=user, reaction=reaction))
        session.commit()

    if "hx-request" in request.headers:
        return team_room_messages(request, room_id)
    return RedirectResponse(f"/team?room={room_id}", status_code=303)

@router.post("/team/messages/{message_id}/remind")
def team_remind(request: Request, message_id: int, remind_in_minutes: int = Form(...)):
    user = _current_user(request)
    if not user: return Response(status_code=403)

    # Un minuto negativo o cero vence al instante: era la forma de leer un
    # mensaje ajeno sin esperar. El tope es una semana.
    if not 1 <= remind_in_minutes <= MAX_REMIND_MINUTES:
        return JSONResponse({"status": "error"}, status_code=422)

    import datetime
    remind_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=remind_in_minutes)
    remind_at = remind_at.replace(tzinfo=None)

    with get_session() as session:
        # El recordatorio devuelve el texto del mensaje: sin esta comprobación,
        # cualquiera leía un DM de otra sala —de otra oficina incluso— con solo
        # adivinar el id. Mismo 404 si no existe o no es suyo: no confirma cuál.
        if _mensaje_visible(session, message_id, user) is None:
            return JSONResponse({"status": "error"}, status_code=404)
        session.add(MessageReminder(message_id=message_id, username=user, remind_at=remind_at, note=""))
        session.commit()
        
    return JSONResponse({"status": "ok"})

@router.get("/team/reminders/active")
def active_reminders(request: Request):
    user = _current_user(request)
    if not user: return Response(status_code=403)

    with get_session() as session:
        now = datetime.utcnow()
        # La membresía se vuelve a comprobar al entregar, no solo al crear: así
        # no sale nada de un recordatorio creado antes del candado, ni de una
        # sala de la que ya lo sacaron, ni de un mensaje que su autor ocultó.
        reminders = session.exec(
            select(MessageReminder, TeamMessage)
            .join(TeamMessage, MessageReminder.message_id == TeamMessage.id)
            .join(ChatMember, and_(ChatMember.room_id == TeamMessage.room_id,
                                   ChatMember.username == MessageReminder.username))
            .where(MessageReminder.username == user, MessageReminder.is_dismissed == False,
                   MessageReminder.remind_at <= now, ChatMember.habilitado == True,
                   TeamMessage.habilitado == True, _posterior_al_borrado())
        ).all()
        
        results = []
        for rem, msg in reminders:
            results.append({
                "id": rem.id,
                "body": msg.body,
                "author": msg.author,
                "room_id": msg.room_id
            })
            rem.is_dismissed = True
            session.add(rem)
        session.commit()
        
    return JSONResponse(results)

@router.post("/team/messages/{message_id}/delete")
def team_delete_message(request: Request, message_id: int):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    with get_session() as session:
        message = session.get(TeamMessage, message_id)
        if message is not None and message.author == user:
            message.habilitado = False
            session.add(message)
            session.commit()
            logger.info("Mensaje de equipo #%s ocultado por %s", message_id, user)
    
    if "hx-request" in request.headers and message:
        return team_room_messages(request, message.room_id)
    return RedirectResponse("/team", status_code=303)
