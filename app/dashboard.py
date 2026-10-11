import hmac
import logging
import secrets
import threading
import time

import bcrypt
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError
from app.templating import templates
from sqlalchemy import or_, update
from sqlmodel import select

from app import archive, authlog, codigos, messaging, ventana
from app.utils import mask_phone, ruta_interna
from app.audit import log_admin
from app.auth import alcance, is_admin
from app.config import settings
from app.credenciales import credencial
from app import asignacion
from app.clients import alias_por_telefono, folio_de
from app.db import get_session
from app.dinero import formato_monto, parse_monto
from app.models import (
    Conversation,
    Direction,
    Invitation,
    Label,
    Message,
    OFICINA_TLAXCALA_ID,
    Office,
    SERVICIOS_SIN_CATALOGO,
    Ticket,
    TicketActivity,
    TicketLabel,
    TicketStatus,
    User,
    utcnow,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# ---- Lockout del login (auditoría 2026-09, AUTH-2) ----
# Tres llaves y no una. El contador viejo era solo por usuario: cinco claves
# malas a «jefa» —desde cualquier parte— la dejaban sin entrar cinco minutos
# con su clave correcta (DoS de cuenta), y como el límite ignoraba la IP,
# un mismo origen probaba 28 usuarios seguidos sin frenarse (spraying).
#   (par)     usuario+IP: fuerza bruta a UNA cuenta desde UN origen. Es el
#             límite que muerde primero y no le sirve a un tercero para
#             bloquear a nadie: tendría que estar en su misma IP.
#   (ip)      cualquier usuario desde una IP: spraying.
#   (usuario) respaldo alto contra un ataque repartido en muchas IPs; que a un
#             tercero le cueste 50 fallos, no 5, y quede en la bitácora.
LOGIN_MAX_ATTEMPTS = 5          # por (usuario, IP)
LOGIN_MAX_POR_IP = 20
LOGIN_MAX_POR_USUARIO = 50
LOGIN_WINDOW_SECONDS = 300
# La llave se trunca: 200 POST anónimos con usernames de 500 KB retenían
# 100 MB (AUTH-7). Mismo tope que la bitácora (authlog.USERNAME_MAX_LEN).
_LLAVE_MAX = 64
# Pasado esto, al registrar un fallo se barren las llaves sin intentos vivos.
_PURGA_A_PARTIR_DE = 2000
# ponytail: contador en memoria por proceso — se reinicia al reiniciar el
# servidor y no se comparte entre instancias. Pasar a DB/Redis si se corre
# con más de un worker o se necesita persistencia entre reinicios.
_login_attempts: dict[tuple[str, str], list[float]] = {}
# Chequear y registrar eran dos pasos sin candado: 30 intentos concurrentes
# contra un usuario pasaban 28 por bcrypt (límite nominal 5). Ahora es UN paso
# bajo este lock y el intento se reserva ANTES de evaluar la clave.
_candado_login = threading.Lock()


def _llaves(username: str, ip: str | None) -> tuple[tuple[str, str], tuple[str, str], tuple[str, str]]:
    u, i = (username or "")[:_LLAVE_MAX], ip or ""
    return ("par", f"{u}|{i}"), ("ip", i), ("usuario", u)


def _vivos(llave: tuple[str, str], ahora: float) -> list[float]:
    vivos = [t for t in _login_attempts.get(llave, []) if ahora - t < LOGIN_WINDOW_SECONDS]
    if vivos:
        _login_attempts[llave] = vivos
    else:
        _login_attempts.pop(llave, None)
    return vivos


def _is_login_locked(username: str, ip: str | None = None) -> bool:
    """¿Alguna de las tres llaves ya está en su tope? Solo lectura."""
    with _candado_login:
        ahora = time.time()
        par, por_ip, por_usuario = _llaves(username, ip)
        return (
            len(_vivos(par, ahora)) >= LOGIN_MAX_ATTEMPTS
            or len(_vivos(por_ip, ahora)) >= LOGIN_MAX_POR_IP
            or len(_vivos(por_usuario, ahora)) >= LOGIN_MAX_POR_USUARIO
        )


def _intento_permitido(username: str, ip: str | None) -> float | None:
    """Reserva un intento y devuelve su marca de tiempo, o None si no procede.

    ATÓMICO: chequeo y registro bajo el mismo lock, así N intentos concurrentes
    dejan pasar exactamente el cupo y ni uno más llega a bcrypt. La reserva
    cuenta como fallo desde ya; si la clave resulta correcta, `_login_ok` la
    retira de las tres llaves —un acierto no es un fallo en ninguna—. La
    primera versión la dejaba en la llave de la IP, y veinte logins CORRECTOS
    desde una misma IP cerraban el panel (lo cazó la suite, no producción)."""
    with _candado_login:
        ahora = time.time()
        par, por_ip, por_usuario = _llaves(username, ip)
        if (
            len(_vivos(par, ahora)) >= LOGIN_MAX_ATTEMPTS
            or len(_vivos(por_ip, ahora)) >= LOGIN_MAX_POR_IP
            or len(_vivos(por_usuario, ahora)) >= LOGIN_MAX_POR_USUARIO
        ):
            return None
        for llave in (par, por_ip, por_usuario):
            _login_attempts.setdefault(llave, []).append(ahora)
        if len(_login_attempts) > _PURGA_A_PARTIR_DE:
            for llave in list(_login_attempts):
                _vivos(llave, ahora)
        return ahora


def _record_failed_login(username: str, ip: str | None = None) -> None:
    """Cuenta un fallo sin preguntar (el registro y el código de invitación no
    evalúan bcrypt, así que no reservan: registran al fallar)."""
    with _candado_login:
        ahora = time.time()
        for llave in _llaves(username, ip):
            _login_attempts.setdefault(llave, []).append(ahora)
        if len(_login_attempts) > _PURGA_A_PARTIR_DE:
            for llave in list(_login_attempts):
                _vivos(llave, ahora)


def _login_ok(username: str, ip: str | None, marca: float) -> None:
    """Clave correcta: se retira la reserva de la IP y se limpian las llaves del
    usuario. Los fallos PREVIOS desde esa IP siguen contando: un acierto no
    borra diecinueve intentos fallidos de spraying, solo deja de sumar uno."""
    with _candado_login:
        par, por_ip, por_usuario = _llaves(username, ip)
        _login_attempts.pop(par, None)
        _login_attempts.pop(por_usuario, None)
        de_ip = _login_attempts.get(por_ip)
        if de_ip and marca in de_ip:
            de_ip.remove(marca)
            if not de_ip:
                _login_attempts.pop(por_ip, None)


# Enumeración por tiempo (AUTH-3): bcrypt corría solo si el usuario existía y
# estaba habilitado (219 ms) y no en los otros dos casos (5 ms), así que el
# tiempo de respuesta decía qué nombres existen y cuáles están activos. Con
# el usuario ausente o deshabilitado se evalúa la clave contra este hash
# señuelo, del mismo costo que los reales, y el resultado se ignora.
_HASH_SENUELO = bcrypt.hashpw(secrets.token_bytes(16), bcrypt.gensalt())


def _log_user(username: str) -> str:
    """Username para una línea de log: acotado y con repr, para que un CRLF
    tecleado en el formulario no invente renglones en la bitácora (INJ-4)."""
    return repr((username or "")[:_LLAVE_MAX])


def seed_dashboard_user(settings) -> None:
    if not settings.dashboard_username or not settings.dashboard_password:
        return
    with get_session() as session:
        existing = session.exec(
            select(User).where(User.username == settings.dashboard_username)
        ).first()
        if existing is None:
            hashed = bcrypt.hashpw(settings.dashboard_password.encode(), bcrypt.gensalt())
            session.add(
                User(
                    username=settings.dashboard_username,
                    password_hash=hashed.decode(),
                    is_admin=True,
                )
            )
            session.commit()
        elif not existing.is_admin:
            # El usuario semilla ya existía de antes de agregar el rol de
            # administrador — se promueve para no dejar el panel sin admin.
            existing.is_admin = True
            session.add(existing)
            session.commit()


def _current_user(request: Request) -> str | None:
    return request.session.get("user")


@router.get("/login")
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"request": request, "error": None})


@router.post("/login")
def login_submit(request: Request, username: str = Form(""), password: str = Form("")):
    ip = authlog.client_ip(request)
    marca = _intento_permitido(username, ip)
    if marca is None:
        logger.warning("Login bloqueado por intentos fallidos: %s", _log_user(username))
        authlog.record_login(request, username, success=False, reason=authlog.REASON_LOCKED)
        return templates.TemplateResponse(
            request, "login.html",
            {"request": request, "error": "Demasiados intentos fallidos. Intenta de nuevo en unos minutos."},
        )

    with get_session() as session:
        user = session.exec(select(User).where(User.username == username)).first()

    if user and user.habilitado:
        correcta = bcrypt.checkpw(password.encode(), user.password_hash.encode())
    else:
        # Mismo costo que un usuario real, para que el tiempo no diga si existe.
        bcrypt.checkpw(password.encode(), _HASH_SENUELO)
        correcta = False

    if correcta:
        _login_ok(username, ip, marca)
        logger.info("Login exitoso: %s", _log_user(username))
        authlog.record_login(request, username, success=True)
        request.session["user"] = username
        request.session["sid"] = authlog.open_session(request, username)
        return RedirectResponse("/", status_code=303)

    # El motivo detallado va solo a la bitácora admin; la respuesta al
    # navegador sigue siendo genérica (no revela si el usuario existe).
    if user is None:
        reason = authlog.REASON_NO_USER
    elif not user.habilitado:
        reason = authlog.REASON_DISABLED
    else:
        reason = authlog.REASON_BAD_PASSWORD
    authlog.record_login(request, username, success=False, reason=reason)
    # El intento ya quedó reservado en _intento_permitido; aquí no se cuenta otra vez.
    logger.warning("Login fallido: %s", _log_user(username))
    return templates.TemplateResponse(
        request, "login.html", {"request": request, "error": "Usuario o contraseña incorrectos"}
    )


@router.get("/logout")
def logout(request: Request):
    authlog.close_session(request)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


USERNAME_MIN, USERNAME_MAX = 3, 30
PASSWORD_MIN = 8


def _validate_registration(username: str, password: str) -> str | None:
    if not (USERNAME_MIN <= len(username) <= USERNAME_MAX) or not username.replace("_", "").isalnum():
        return f"El usuario debe tener {USERNAME_MIN}-{USERNAME_MAX} caracteres alfanuméricos (se permite _)."
    if len(password) < PASSWORD_MIN:
        return f"La contraseña debe tener al menos {PASSWORD_MIN} caracteres."
    return None


def _find_valid_invitation(session, code: str) -> Invitation | None:
    if not code:
        return None
    return session.exec(
        select(Invitation).where(
            Invitation.token == code,
            Invitation.used_at.is_(None),
            Invitation.habilitado == True,  # noqa: E712 — SQLAlchemy requiere `== True`, no `is True`
        )
    ).first()


def _code_grants_registration(session, code: str) -> tuple[bool, Invitation | None]:
    """Un código de registro es válido si coincide con el código maestro
    (REGISTRATION_CODE) o con una invitación individual sin usar."""
    if settings.registration_code and hmac.compare_digest(code, settings.registration_code):
        return True, None
    invitation = _find_valid_invitation(session, code)
    return invitation is not None, invitation


@router.get("/register")
def register_form(request: Request):
    return templates.TemplateResponse(request, "register.html", {"request": request, "error": None})


@router.post("/register")
def register_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    code: str = Form(""),
    phone: str = Form(""),
):
    username = username.strip().lower()
    # Reusa el lockout del login con la llave («__register__», IP): frena la
    # fuerza bruta del código de invitación desde un origen sin que ese origen
    # pueda dejar sin registro a todo el mundo, como hacía la llave global.
    ip = authlog.client_ip(request)
    if _is_login_locked("__register__", ip):
        return templates.TemplateResponse(
            request, "register.html",
            {"request": request, "error": "Demasiados intentos. Espera unos minutos."},
        )

    error = _validate_registration(username, password)
    if error:
        _record_failed_login("__register__", ip)
        logger.warning("Registro rechazado para %s: %s", _log_user(username), error)
        return templates.TemplateResponse(request, "register.html", {"request": request, "error": error})

    normalized_phone = _normalize_mx_phone(phone)
    if normalized_phone is None:
        _record_failed_login("__register__", ip)
        logger.warning("Registro rechazado para %s: celular inválido", _log_user(username))
        return templates.TemplateResponse(
            request, "register.html",
            {"request": request, "error": "Celular inválido: escribe los 10 dígitos (ej. 3312345678)."},
        )

    with get_session() as session:
        code_ok, invitation = _code_grants_registration(session, code)
        if not code_ok:
            _record_failed_login("__register__", ip)
            logger.warning("Registro rechazado para %s: código inválido", _log_user(username))
            return templates.TemplateResponse(
                request, "register.html",
                {
                    "request": request,
                    "error": "Código de invitación incorrecto, ya utilizado, o el registro está deshabilitado.",
                },
            )

        if session.exec(select(User).where(User.username == username)).first():
            return templates.TemplateResponse(
                request, "register.html", {"request": request, "error": "Ese usuario ya existe."}
            )

        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt())
        # La oficina viene de la invitación, que la fijó quien invitó. Con el
        # código MAESTRO (REGISTRATION_CODE) no hay invitación y no hay a quién
        # preguntarle: cae en la principal, que es lo correcto mientras ese
        # código siga siendo uno global del despliegue. Si algún día hace falta
        # un código maestro por oficina, el lugar es config, no aquí.
        office_id = invitation.office_id if invitation is not None else OFICINA_TLAXCALA_ID
        if invitation is not None:
            # «Un solo uso» de verdad (AUTH-4): la invitación se consume con un
            # UPDATE condicionado a que siga sin usar, y el usuario se crea SOLO
            # si ese UPDATE tocó una fila. Antes se marcaba usada DESPUÉS de
            # crear el usuario y sin candado: cinco registros concurrentes con
            # el mismo código creaban tres cuentas. El histórico de invitaciones
            # muestra código + teléfono + quién lo usó.
            consumida = session.exec(
                update(Invitation)
                .where(
                    Invitation.id == invitation.id,
                    Invitation.used_at.is_(None),
                    Invitation.habilitado == True,  # noqa: E712
                )
                .values(used_at=utcnow(), used_by=username, phone=normalized_phone)
            )
            if consumida.rowcount != 1:
                session.rollback()
                _record_failed_login("__register__", ip)
                logger.warning("Registro rechazado para %s: invitación ya consumida", _log_user(username))
                return templates.TemplateResponse(
                    request, "register.html",
                    {
                        "request": request,
                        "error": "Código de invitación incorrecto, ya utilizado, o el registro está deshabilitado.",
                    },
                )
        session.add(User(username=username, password_hash=hashed.decode(),
                         phone=normalized_phone, office_id=office_id))
        session.commit()
        if invitation is not None:
            # El UPDATE de Core no dispara el listener del espejo (archive.py):
            # se le manda la fila ya consumida por el mismo camino seguro.
            session.refresh(invitation)
            archive.espejar(archive.archive_invitation, invitation)

    logger.info("Usuario registrado: %s", _log_user(username))
    request.session["user"] = username
    return RedirectResponse("/", status_code=303)


# El código de invitación se comparte manualmente (botón "Copiar código"):
# el envío por WhatsApp se eliminó porque Meta solo entrega mensajes libres
# a números con ventana de 24h abierta y fallaba en silencio con números nuevos.


def _normalize_mx_phone(raw: str) -> str | None:
    """Convierte lo tecleado al formato de WhatsApp para celulares mexicanos
    (521 + 10 dígitos). Acepta 10 dígitos, 52+10 o 521+10; None si no cuadra."""
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 10:
        return "521" + digits
    if len(digits) == 12 and digits.startswith("52"):
        return "521" + digits[2:]
    if len(digits) == 13 and digits.startswith("521"):
        return digits
    return None


@router.get("/invitations")
def invitations_list(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not is_admin(user):
        return RedirectResponse("/", status_code=303)

    vista = alcance(user)
    with get_session() as session:
        invitations = _invitaciones_visibles(session, vista)
        oficinas, nombres = _oficinas_para_invitar(session, vista)

    return templates.TemplateResponse(
        request, "invitations.html",
        {"request": request, "user": user, "invitations": invitations,
         "oficinas": oficinas, "nombres_oficina": nombres},
    )


@router.post("/invitations/{invitation_id}/toggle")
def invitations_toggle(request: Request, invitation_id: int):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not is_admin(user):
        return RedirectResponse("/", status_code=303)

    vista = alcance(user)
    with get_session() as session:
        invitation = session.get(Invitation, invitation_id)
        # Solo tiene sentido cancelar/reactivar invitaciones sin usar — una ya
        # canjeada no debe poder revocarse desde aquí (el usuario ya existe).
        # Y solo las de su oficina: el admin de oficina no revoca las ajenas.
        if (invitation is not None and invitation.used_at is None
                and vista.alcanza(invitation.office_id)):
            invitation.habilitado = not invitation.habilitado
            session.add(invitation)
            session.commit()
            state = "reactivada" if invitation.habilitado else "cancelada"
            # El id, nunca el token (AUTH-6): la bitácora la lee cualquier admin de
            # oficina y se espeja a MSSQL; un código vivo ahí era una cuenta regalada.
            log_admin(user, f"invitacion:{state}", f"#{invitation.id}")
            logger.info("Invitación #%s %s por %s", invitation_id, state, user)
    return RedirectResponse("/invitations", status_code=303)


def _invitaciones_visibles(session, vista):
    return session.exec(
        vista.filtrar(select(Invitation), Invitation.office_id)
        .order_by(Invitation.created_at.desc())
    ).all()


def _oficinas_para_invitar(session, vista):
    """(oficinas habilitadas a las que puede invitar, {id: nombre}).

    El admin de oficina solo invita a la suya: un código para otra oficina
    sería meter gente donde él no administra.

    Solo las habilitadas: invitar a alguien a una oficina dada de baja lo dejaría
    sin ver nada al entrar —su ruteo está cortado— y el síntoma no apunta a la
    causa. El mapa de nombres es para pintar los códigos ya emitidos, incluidos
    los de una oficina que se dio de baja después.
    """
    oficinas = session.exec(
        vista.filtrar(select(Office), Office.id)
        .where(Office.habilitado == True).order_by(Office.id)  # noqa: E712
    ).all()
    nombres = {o.id: o.nombre for o in session.exec(select(Office)).all()}
    return oficinas, nombres


@router.post("/invitations")
def invitations_create(request: Request, office_id: int = Form(OFICINA_TLAXCALA_ID)):
    """Genera un código de un solo uso PARA UNA OFICINA.

    La oficina se elige aquí y no en el registro a propósito: quien acepta la
    invitación no sabe —ni le toca decidir— de qué sede es. Sin esto, todo
    invitado nacía en la oficina 1 por el DEFAULT y había que moverlo después,
    con el rescate de folios que eso arrastra (users.mover_de_oficina).
    """
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not is_admin(user):
        return RedirectResponse("/", status_code=303)

    token = secrets.token_urlsafe(9)
    vista = alcance(user)
    with get_session() as session:
        oficina = session.get(Office, office_id)
        # Otra oficina, para el admin de oficina, responde como una que no
        # existe: el formulario no se la ofrece, pero el POST lo arma cualquiera.
        if oficina is None or not oficina.habilitado or not vista.alcanza(office_id):
            invitations = _invitaciones_visibles(session, vista)
            oficinas, nombres = _oficinas_para_invitar(session, vista)
            return templates.TemplateResponse(
                request, "invitations.html",
                {"request": request, "user": user, "invitations": invitations,
                 "oficinas": oficinas, "nombres_oficina": nombres,
                 "error": "Esa oficina no existe o está dada de baja."},
            )
        session.add(Invitation(token=token, phone=None, created_by=user, office_id=office_id))
        session.commit()
        invitations = _invitaciones_visibles(session, vista)
        oficinas, nombres = _oficinas_para_invitar(session, vista)

    logger.info("Invitación generada por %s para la oficina %s", user, office_id)
    return templates.TemplateResponse(
        request, "invitations.html",
        {
            "request": request,
            "user": user,
            "invitations": invitations,
            "oficinas": oficinas,
            "nombres_oficina": nombres,
            "new_token": token,
        },
    )


PER_PAGE = 15


@router.get("/")
def ticket_list(
    request: Request,
    status: str | None = None,
    assigned: str | None = None,
    conv: str | None = None,
    etiqueta: str | None = None,
    page: int = 1,
):
    """Solicitudes y conversaciones en una sola vista.

    Los filtros por estado del folio (`status`, `assigned`) listan folios en la
    tabla; los de conversación (`conv`) listan conversaciones por teléfono, con
    las mismas tarjetas de la Bandeja. Son mutuamente excluyentes: un chip de
    conversación no acota folios, cambia lo que se lista."""
    from app.inbox import (
        CONV_FILTERS,
        build_conversation_items,
        conversation_counts,
        filter_conversations,
    )

    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    # La oficina se decide UNA vez (auth.alcance) y se aplica por consulta: un
    # filtro global también taparía al bot, al cron y al espejo, que no tienen
    # sesión de usuario que consultar.
    vista = alcance(user)
    with get_session() as session:
        all_tickets = session.exec(
            vista.filtrar(
                select(Ticket).where(Ticket.habilitado == True),  # noqa: E712
                Ticket.office_id,
            ).order_by(Ticket.created_at.desc())
        ).all()
        # ponytail: recorre todos los mensajes en cada carga de «/», que además
        # se auto-refresca cada 30 s; pasar a una consulta agregada si el
        # historial crece a decenas de miles.
        conversaciones = build_conversation_items(session, vista)
        nombres_etiquetas = session.exec(
            select(Label.nombre).where(Label.habilitado == True).order_by(Label.nombre)  # noqa: E712
        ).all()
        etiqueta = (etiqueta or "").strip() or None
        ids_con_etiqueta = None
        if etiqueta:
            ids_con_etiqueta = set(
                session.exec(
                    select(TicketLabel.ticket_id)
                    .join(Label, Label.id == TicketLabel.label_id)
                    .where(
                        Label.nombre == etiqueta,
                        TicketLabel.habilitado == True,  # noqa: E712
                        Label.habilitado == True,  # noqa: E712
                    )
                ).all()
            )

    # Un valor desconocido en ?conv= equivale a «todas»: la vista nunca queda en
    # un estado sin lista ni chip activo.
    conv = conv if conv in CONV_FILTERS else None
    conv_counts = conversation_counts(conversaciones)
    conversaciones = filter_conversations(conversaciones, conv)

    # Un folio cerrado solo se lista en su chip, «cerrado» (regla del operador,
    # 2026-09-16): en «todas», «sin asignar», «escalados» o una etiqueta sola se
    # revolvía con lo que todavía hay que atender. Cerrar no apaga `escalado`,
    # así que sin esto «escalados» también los seguía arrastrando.
    abiertos = [t for t in all_tickets if t.status != TicketStatus.CERRADO]

    stats = {
        "total": len(all_tickets),
        "nuevos": sum(1 for t in all_tickets if t.status == TicketStatus.NUEVO),
        "activos": sum(1 for t in all_tickets if t.status in (TicketStatus.TOMADO, TicketStatus.EN_PROCESO)),
        "escalados": sum(1 for t in abiertos if t.escalado),
    }
    # Conteo de cada chip de folio, nunca sobre el filtro activo: el número dice
    # cuántos hay, no cuántos quedan dentro del filtro — si se contaran sobre lo
    # ya filtrado, todos los chips menos el activo dirían 0.
    ticket_counts = {s.value: 0 for s in TicketStatus}
    for t in all_tickets:
        ticket_counts[t.status.value] += 1
    ticket_counts["todas"] = len(abiertos)
    ticket_counts["sin_asignar"] = sum(1 for t in abiertos if t.assigned_to is None)
    # La bandera escalado cruza estados (un folio escalado puede estar nuevo,
    # tomado o en proceso): la tarjeta de arriba ya la contaba, pero sin chip
    # no había forma de LISTAR esos folios.
    ticket_counts["escalados"] = stats["escalados"]

    tickets = all_tickets if status == TicketStatus.CERRADO.value else abiertos
    if status == "escalados":
        tickets = [t for t in tickets if t.escalado]
    elif status:
        tickets = [t for t in tickets if t.status.value == status]
    if assigned == "none":
        tickets = [t for t in tickets if t.assigned_to is None]
    if ids_con_etiqueta is not None:
        tickets = [t for t in tickets if t.id in ids_con_etiqueta]
    if not status and not assigned:
        # «todas» abre con las nuevas sin dueño (regla del operador, 2026-09-16):
        # por fecha, una nueva que nadie tomó quedaba debajo de las ya tomadas,
        # a veces en otra página. Cuentan las dos condiciones: la nueva que
        # repartió el bot ya tiene dueño, y la tomada que perdió al suyo en
        # users.mover_de_oficina no es nueva. sorted() es estable, así que cada
        # grupo conserva la más reciente primero; va antes de paginar para que
        # encabecen la página 1.
        tickets = sorted(
            tickets,
            key=lambda t: 0 if t.status == TicketStatus.NUEVO and t.assigned_to is None else 1,
        )

    # ponytail: paginación en memoria — el filtrado ya trae todo para las
    # stats; pasar a OFFSET/LIMIT en SQL si los folios crecen a decenas de miles.
    pages = max(1, -(-len(tickets) // PER_PAGE))
    page = max(1, min(page, pages))
    tickets = tickets[(page - 1) * PER_PAGE : page * PER_PAGE]
    # Ventana de 5 números centrada en la actual, como la referencia (1…5).
    inicio_ventana = max(1, min(page - 2, pages - 4))
    page_numbers = list(range(inicio_ventana, min(pages, inicio_ventana + 4) + 1))
    # Solo los de la página: el alias vive en Client, no en el folio.
    with get_session() as session:
        alias = alias_por_telefono(session, (t.phone for t in tickets))

    return templates.TemplateResponse(
        request, "tickets_list.html",
        {
            "request": request,
            "user": user,
            "tickets": tickets,
            "alias": alias,
            "stats": stats,
            "statuses": [s.value for s in TicketStatus],
            "current_filter": status,
            "assigned_filter": assigned,
            "etiqueta_filter": etiqueta,
            "nombres_etiquetas": nombres_etiquetas,
            "ticket_counts": ticket_counts,
            "conversaciones": conversaciones,
            "conv_counts": conv_counts,
            "conv_filter": conv,
            "page": page,
            "pages": pages,
            "page_numbers": page_numbers,
        },
    )


def cotizacion_estimada(price_info: str, requisitos: list[dict]) -> dict | None:
    """Cuántas claves mandó el cliente para este folio y a cuánto suman al
    precio del catálogo.

    Se DERIVA de lo capturado en el folio: no hay columna "cantidad" que
    mantener sincronizada con el espejo MSSQL. Devuelve None cuando es una sola
    (la vista de siempre); `total` es None cuando el precio del catálogo no es
    un número ("A cotizar" — 7 de los 58 servicios).

    Es un ESTIMADO para que el asesor no cuente los códigos a mano. El monto
    que se cobra lo sigue tecleando él: el sistema no fija precios finales.
    """
    capturado = " ".join(r["valor"] for r in requisitos if r["valor"])
    cuantos = codigos.cuantos_tramites(
        [r["req"] for r in requisitos], codigos.detectar(capturado)
    )
    if cuantos < 2:
        return None
    try:
        unitario = parse_monto(price_info)
    except ValueError:
        return {"cuantos": cuantos, "total": None}
    return {"cuantos": cuantos, "total": formato_monto(unitario * cuantos)}


@router.get("/tickets/{ticket_id}")
def ticket_detail(
    request: Request, ticket_id: int, err: str = "", ok: str = "", volver: str = ""
):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    vista = alcance(user)
    with get_session() as session:
        ticket = session.get(Ticket, ticket_id)
        if ticket is None:
            return RedirectResponse("/", status_code=303)
        # Referencia directa: el id viene de la URL, así que acotar el LISTADO
        # no basta. Sin esto, teclear el id de un folio de otra oficina lo abre
        # entero —teléfono del cliente, historial, notas—. Misma respuesta que
        # «no existe»: distinguirlas confirmaría qué ids están ocupados.
        if not vista.alcanza(ticket.office_id):
            logger.warning("%s intentó abrir el folio %s de otra oficina", user, ticket_id)
            return RedirectResponse("/", status_code=303)
        # A quién se puede transferir: gente de la oficina DEL FOLIO (no la de
        # quien mira: un admin ve todas). Mismo criterio que reassign_ticket.
        teammates = [
            (u.username, u.estado)
            for u in session.exec(
                select(User)
                .where(User.habilitado == True, User.office_id == ticket.office_id)  # noqa: E712
                .order_by(User.username)
            ).all()
            if u.username != user
        ]
        # «Si le contestas, pasa a ti» solo es verdad si contestar se la queda: tiene que
        # ser el folio con el que quien mira ve la conversación —con un folio propio ahí,
        # contestar consolida el suyo— y tiene que poder tomarlo (cartera y asignación por
        # servicio, 2026-09-25). Misma regla que inbox._pause_bot.
        al_contestar = folio_de(session, ticket.phone, user)
        contestar_la_toma = (
            al_contestar is not None
            and al_contestar.id == ticket.id
            and asignacion.puede_tomar(session, ticket, user)
        )
        from app.models import FollowUp

        followups = session.exec(
            select(FollowUp)
            .where(FollowUp.ticket_id == ticket_id)
            .where(FollowUp.done_at.is_(None))
            .where(FollowUp.habilitado == True)  # noqa: E712
            .order_by(FollowUp.due_at)
        ).all()
        etiquetas = session.exec(
            select(Label)
            .join(TicketLabel, TicketLabel.label_id == Label.id)
            .where(
                TicketLabel.ticket_id == ticket_id,
                TicketLabel.habilitado == True,  # noqa: E712
                Label.habilitado == True,  # noqa: E712
            )
            .order_by(Label.nombre)
        ).all()
        alias_cliente = alias_por_telefono(session, [ticket.phone]).get(ticket.phone)
        veredicto = _veredicto_actual(session, ticket) if ticket.service_id == "analisis" else None
        # Para el datalist del formulario «+ etiqueta».
        todas_las_etiquetas = [
            l.nombre
            for l in session.exec(
                select(Label).where(Label.habilitado == True).order_by(Label.nombre)  # noqa: E712
            ).all()
        ]

    from app.catalog import catalog, requisitos_del_ticket

    service = catalog.service_by_id(ticket.service_id)
    requisitos = requisitos_del_ticket(ticket.requirements_json)
    return templates.TemplateResponse(
        request, "ticket_detail.html",
        {
            "request": request,
            "user": user,
            "ticket": ticket,
            "alias_cliente": alias_cliente,
            "statuses": [s.value for s in TicketStatus],
            # Dueño de reparto que nadie ha tocado todavía: la vista lo dice de
            # otra forma, porque «la está atendiendo X» sería falso.
            "asignacion_provisional": asignacion.provisional(ticket),
            "contestar_la_toma": contestar_la_toma,
            "teammates": teammates,
            "followups": followups,
            "requirements": service.requirements if service else "",
            # Lo que el agente pidió por WhatsApp en ESTE folio (congelado al
            # darlo de alta), con lo que el cliente contestó y lo que falta.
            "requisitos": requisitos,
            "etiquetas": etiquetas,
            "todas_las_etiquetas": todas_las_etiquetas,
            # Cuántas claves mandó el cliente y cuánto suman al precio del
            # catálogo. Es un ESTIMADO para que el asesor no cuente a mano; el
            # monto que se cobra lo sigue tecleando él.
            "cotizacion": cotizacion_estimada(ticket.price_info, requisitos),
            # Solo en el folio «analisis»: el último veredicto del equipo, o "" si nadie lo ha dado.
            "es_caso_analisis": ticket.service_id == "analisis",
            "veredicto": veredicto,
            "veredictos": VEREDICTOS,
            "err": err,
            "ok": ok,
            # Ruta de la lista de origen (filtros y página incluidos) para el
            # botón «volver»; validada, un valor externo se descarta.
            "volver": ruta_interna(volver),
            **_contexto_cobranza(ticket, user),
        },
    )


def _contexto_cobranza(ticket: Ticket, user: str) -> dict:
    """Lo que el parcial de cobro necesita del folio."""
    from app import cobranza
    from app.dinero import OXXO_MAX_CENTAVOS, monto_editable, parse_monto
    from app.models import Cobro

    with get_session() as session:
        cobros = list(session.exec(
            select(Cobro)
            .where(Cobro.ticket_id == ticket.id)
            .order_by(Cobro.created_at.desc())  # type: ignore[union-attr]
        ).all())
        # Cobros vivos del MISMO TELÉFONO en otros folios: con OXXO el folio
        # se cierra y el bot fabrica uno nuevo, así que sin esto el asesor no
        # vería el instrumento que todavía es cobrable y se toparía con el
        # candado sin entender por qué.
        vivos = [
            c for c in session.exec(
                select(Cobro)
                .where(Cobro.phone == ticket.phone)
                .where(Cobro.llave_cobro_vivo.is_not(None))  # type: ignore[union-attr]
            ).all()
            if c.ticket_id != ticket.id
        ]

    # La línea base del monto es lo que el bot le COTIZÓ al cliente por
    # WhatsApp, no el catálogo vivo: ese es el número que el cliente leyó.
    cotizado = None
    if ticket.price_info:
        try:
            cotizado = parse_monto(ticket.price_info)
        except ValueError:
            cotizado = None  # "A cotizar" y cualquier texto ambiguo

    return {
        "cobros": cobros,
        "cobros_vivos_del_telefono": vivos,
        "puede_cobrar": cobranza.puede_cobrar(user, ticket, None),
        # ponytail: office_id fijo a Tlaxcala. En el PR 5 la oficina sale del
        # ticket, no de una constante.
        # Sin `session=`: contraintuitivo pero verificado — aquí NO hay sesión
        # abierta, la de :561 cerró en :578.
        "stripe_configurado": bool(
            credencial("STRIPE_SECRET_KEY", office_id=OFICINA_TLAXCALA_ID)
        ),
        # Único por RENDER: dos envíos del mismo formulario (F5, doble clic)
        # chocan contra ux_cobro_generacion en vez de crear dos cobros.
        "token_form": secrets.token_hex(16),
        "monto_cotizado_centavos": cotizado,
        "monto_sugerido": monto_editable(cotizado) if cotizado else "",
        "oxxo_max_centavos": OXXO_MAX_CENTAVOS,
    }


@router.post("/tickets/{ticket_id}/claim")
def claim_ticket(request: Request, ticket_id: int):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    vista = alcance(user)
    with get_session() as session:
        # Claim atómico: solo asigna si nadie la tiene — dos asesores no
        # pueden tomar la misma solicitud aunque hagan clic a la vez. La
        # oficina va DENTRO del WHERE: sin ella, teclear el id de un folio sin
        # dueño de otra oficina lo tomaba, y ser dueño abre todo lo demás.
        result = session.exec(
            vista.filtrar(update(Ticket), Ticket.office_id)
            .where(
                Ticket.id == ticket_id,
                Ticket.assigned_to.is_(None),
                Ticket.status != TicketStatus.CERRADO,
            )
            # agent_active=True: quien reclama un ticket arranca con el agente
            # activo por default; lo apaga explícitamente si lo decide (abajo).
            .values(assigned_to=user, status=TicketStatus.TOMADO, agent_active=True, updated_at=utcnow())
        )
        session.commit()

    if result.rowcount:
        with get_session() as session:
            _log_activity(session, ticket_id, user, "tomado")
        logger.info("Ticket #%s tomado por %s", ticket_id, user)
    else:
        logger.info("Ticket #%s ya estaba tomado (intento de %s)", ticket_id, user)
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


# Aviso de cortesía al soltar un ticket escalado: el cliente no debe sentir
# que lo dejaron colgado ni enterarse del movimiento interno de asesores.
RELEASE_NOTICE_SERVICE = (
    "Seguimos trabajando para ti 🙌 Tu solicitud de '{service}' (folio #{folio}) "
    "se está revisando más a fondo. Un asesor te contactará pronto; "
    "si necesitas algo más mientras tanto, escribe *menu*."
)
RELEASE_NOTICE_GENERIC = (
    "Seguimos trabajando para ti 🙌 Tu caso se está revisando más a fondo. "
    "Un asesor te contactará pronto; si necesitas algo más mientras tanto, escribe *menu*."
)


@router.post("/tickets/{ticket_id}/release")
def release_ticket(request: Request, ticket_id: int):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    # Un admin puede soltar el folio de cualquiera. Sin esto, los folios vivos
    # de un colaborador deshabilitado quedan congelados PARA SIEMPRE: él ya no
    # puede entrar a soltarlos, y `claim` solo toma folios sin dueño. La única
    # salida era editar SQLite a mano. La fase 3a hace visible ese trabajo
    # huérfano en /activity, así que tiene que existir la forma de rescatarlo.
    admin = is_admin(user)
    condicion = [Ticket.id == ticket_id]
    if not admin:
        condicion.append(Ticket.assigned_to == user)

    vista = alcance(user)
    with get_session() as session:
        anterior = session.get(Ticket, ticket_id)
        dueno_anterior = anterior.assigned_to if anterior else None
        result = session.exec(
            vista.filtrar(update(Ticket), Ticket.office_id)
            .where(*condicion, Ticket.assigned_to.is_not(None))
            # agent_active=True: al soltar, el bot vuelve a cubrir al cliente —
            # nunca debe quedar un chat sin asesor Y con el agente apagado.
            .values(
                assigned_to=None,
                status=TicketStatus.NUEVO,
                agent_active=True,
                updated_at=utcnow(),
            )
        )
        session.commit()

    if result.rowcount:
        with get_session() as session:
            # Quién soltó a quién: sin esto el histórico mostraría un folio que
            # cambió de dueño sin que nadie apareciera haciéndolo.
            accion = (
                "soltado"
                if dueno_anterior == user
                else f"soltado-por-admin:{dueno_anterior or '—'}"
            )
            _log_activity(session, ticket_id, user, accion)
        logger.info("Ticket #%s soltado por %s", ticket_id, user)
        _notify_release(ticket_id)
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


# Aviso de cierre: el cliente sabe que su trámite quedó listo y la puerta
# queda abierta — nunca un cierre silencioso.
FINISH_NOTICE_SERVICE = (
    "✅ Tu solicitud de '{service}' (folio #{folio}) quedó terminada. "
    "¡Gracias por tu confianza! Si necesitas otro trámite, aquí estamos."
)
FINISH_NOTICE_GENERIC = (
    "✅ Tu solicitud quedó atendida. ¡Gracias por tu confianza! "
    "Si necesitas otro trámite, aquí estamos."
)
# Con un cobro sin liquidar, "quedó terminada, ¡gracias por tu confianza!" es
# literalmente el argumento del cliente para no pagar la ficha.
FINISH_NOTICE_CON_COBRO = (
    "✅ Tu trámite quedó listo. Recuerda que tu pago pendiente de "
    "{monto}{vence} sigue activo."
)


def _cobros_vivos_del_folio(session, ticket: Ticket) -> list:
    """Cobros con instrumento todavía cobrable de ESTE trámite. El criterio es
    el MISMO del candado (`ux_cobro_vivo`): (teléfono, servicio) — no solo el
    teléfono, porque el cobro de la CURP no tiene por qué bloquear el cierre
    del acta ni colarse en su aviso; y no solo el folio, porque con OXXO el
    folio se cierra y el bot fabrica otro para el mismo servicio."""
    from app.models import Cobro

    return list(session.exec(
        select(Cobro)
        .where(Cobro.phone == ticket.phone)
        .where(Cobro.llave_cobro_vivo.is_not(None))  # type: ignore[union-attr]
        .where(or_(Cobro.ticket_id == ticket.id,
                   Cobro.service_id == ticket.service_id))
    ).all())


@router.post("/tickets/{ticket_id}/finish")
def finish_ticket(request: Request, ticket_id: int, confirm: str = Form("")):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    vista = alcance(user)
    with get_session() as session:
        ticket = session.get(Ticket, ticket_id)
        # Puede terminar el asignado o un admin; un ticket cerrado no se re-cierra.
        allowed = (
            ticket is not None
            and vista.alcanza(ticket.office_id)
            and ticket.status != TicketStatus.CERRADO
            and (ticket.assigned_to == user or is_admin(user))
        )
        if not allowed:
            if ticket is not None:
                logger.warning("Terminar rechazado en #%s por %s", ticket_id, user)
            return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)

        # Cerrar con un cobro vivo a veces es legítimo (el cliente paga
        # después), así que NO se bloquea: se exige confirmación explícita.
        vivos = bool(_cobros_vivos_del_folio(session, ticket))
        if vivos and not confirm:
            return RedirectResponse(
                f"/tickets/{ticket_id}?err=cobro-vivo-al-cerrar", status_code=303
            )

        ticket.status = TicketStatus.CERRADO
        ticket.agent_active = True  # ciclo terminado: el bot vuelve a cubrir el chat
        ticket.updated_at = utcnow()
        ticket.status_changed_at = ticket.updated_at
        ticket.resolved_at = ticket.updated_at
        session.add(ticket)
        # La conversación regresa al flujo normal — sin esto el cliente seguiría
        # recibiendo "un asesor te contactará" con el ticket ya cerrado.
        conv = session.get(Conversation, ticket.phone)
        if conv is not None and conv.state == "escalado":
            conv.state = "inicio"
            session.add(conv)
        session.commit()

    with get_session() as session:
        _log_activity(session, ticket_id, user, "cerrado")
        if vivos:
            _log_activity(session, ticket_id, user, "cobro:cierre-con-pago-pendiente")
    logger.info("Ticket #%s terminado por %s", ticket_id, user)
    _notify_finish(ticket_id)
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


def _notify_finish(ticket_id: int) -> None:
    """Aviso de cierre al cliente — best-effort, solo dentro de la ventana de 24h."""
    with get_session() as session:
        ticket = session.get(Ticket, ticket_id)
        if ticket is None:
            return
        phone = ticket.phone
        if not ventana.abierta_para(session, phone):
            logger.info("Sin aviso de cierre a %s: ventana de 24h cerrada", mask_phone(phone))
            return
        cobro_vivo = next(iter(_cobros_vivos_del_folio(session, ticket)), None)
        if cobro_vivo is not None:
            # Con dinero pendiente el texto cambia: felicitar por el cierre sin
            # mencionar el pago es regalarle al cliente el argumento para no
            # pagar la ficha.
            from app.dinero import formato_monto

            vence = ""
            if cobro_vivo.voucher_expira_at:
                vence = f", que vence el {cobro_vivo.voucher_expira_at:%d/%m/%Y}"
            body = FINISH_NOTICE_CON_COBRO.format(
                monto=formato_monto(cobro_vivo.monto_centavos, cobro_vivo.moneda),
                vence=vence,
            )
        elif ticket.service_id in SERVICIOS_SIN_CATALOGO:
            body = FINISH_NOTICE_GENERIC
        else:
            body = FINISH_NOTICE_SERVICE.format(service=ticket.service_name, folio=ticket.id)
        # El ContentSid se resuelve AQUÍ, antes del primer session.add: leerlo
        # con `session=` dispara el autoflush de lo que esté pendiente, y con una
        # escritura en vuelo eso (a) toma el candado de ESCRITOR de SQLite y lo
        # sostiene durante los reintentos de red a Meta —hasta ~33 s de
        # "database is locked" para el webhook y el cron— y (b) bajo el pool de
        # la suite deja esa fila a merced del ROLLBACK de la sesión anidada que
        # abre messaging.send_buttons, y el aviso de cierre se pierde sin ruido.
        # Con la sesión limpia el flush es un no-op y `session=` sigue siendo
        # gratis: cero conexiones nuevas. No mover esta lectura hacia abajo: el
        # orden ES el arreglo, y lo fija test_el_aviso_y_la_encuesta_se_guardan.
        content_sid = credencial(
            "TWILIO_CONTENT_SID_FEEDBACK",
            office_id=OFICINA_TLAXCALA_ID,
            session=session,
        ) or None

        wamid = messaging.send_text(phone, body)
        session.add(
            Message(phone=phone, direction=Direction.OUT, body=body, wa_message_id=wamid)
        )
        # Encuesta del cliente: botones 👍/👎 en Meta y el mismo texto con las
        # opciones numeradas en Twilio, que no tiene botones nativos. Las
        # respuestas ("1"/"2", o el emoji) las resuelve
        # conversation.handle_incoming contra el último folio cerrado.
        from app.conversation import FEEDBACK_OPCIONES, FEEDBACK_PROMPT

        fb_wamid = messaging.send_buttons(
            phone,
            FEEDBACK_PROMPT,
            FEEDBACK_OPCIONES,
            content_sid=content_sid,
        )
        opciones = " / ".join(f"{tecla} {titulo}" for tecla, titulo in FEEDBACK_OPCIONES)
        session.add(
            Message(
                phone=phone, direction=Direction.OUT,
                body=f"{FEEDBACK_PROMPT} ({opciones})", wa_message_id=fb_wamid,
            )
        )
        session.commit()
    if wamid is None:
        logger.warning("Aviso de cierre a %s sin confirmación de envío", mask_phone(phone))


def _notify_release(ticket_id: int) -> None:
    """Mensaje best-effort al cliente cuando su ticket escalado queda sin
    asesor, solo dentro de la ventana de 24h de WhatsApp."""
    with get_session() as session:
        ticket = session.get(Ticket, ticket_id)
        if ticket is None or not ticket.escalado:
            return
        phone = ticket.phone
        if not ventana.abierta_para(session, phone):
            logger.info("Sin aviso de soltado a %s: ventana de 24h cerrada", mask_phone(phone))
            return
        if ticket.service_id in SERVICIOS_SIN_CATALOGO:
            body = RELEASE_NOTICE_GENERIC
        else:
            body = RELEASE_NOTICE_SERVICE.format(service=ticket.service_name, folio=ticket.id)
        wamid = messaging.send_text(phone, body)
        session.add(
            Message(phone=phone, direction=Direction.OUT, body=body, wa_message_id=wamid)
        )
        session.commit()
    if wamid is None:
        logger.warning("Aviso de soltado a %s sin confirmación de envío", mask_phone(phone))


@router.post("/tickets/{ticket_id}/delete")
def delete_ticket(request: Request, ticket_id: int):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not is_admin(user):
        return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)

    vista = alcance(user)
    with get_session() as session:
        ticket = session.get(Ticket, ticket_id)
        if ticket is not None and vista.alcanza(ticket.office_id):
            ticket.habilitado = not ticket.habilitado
            ticket.updated_at = utcnow()
            session.add(ticket)
            session.commit()
            state = "restaurado" if ticket.habilitado else "eliminado"
            _log_activity(session, ticket_id, user, state)
            log_admin(user, f"ticket:{state}", str(ticket_id))
            logger.info("Ticket #%s %s por %s", ticket_id, state, user)
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


@router.post("/tickets/{ticket_id}/agent-toggle")
def toggle_ticket_agent(request: Request, ticket_id: int):
    """Apaga o reanuda el agente IA para este ticket. Solo puede hacerlo quien
    lo tiene asignado — es la acción explícita que exige la regla de negocio:
    escalar un ticket NUNCA calla al bot por sí solo, tiene que apagarlo el
    colaborador. Cada cambio queda en TicketActivity (quién y cuándo)."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    vista = alcance(user)
    with get_session() as session:
        ticket = session.get(Ticket, ticket_id)
        if ticket is not None and vista.alcanza(ticket.office_id) and ticket.assigned_to == user:
            ticket.agent_active = not ticket.agent_active
            ticket.updated_at = utcnow()
            session.add(ticket)
            session.commit()
            action = "agente:reanudado" if ticket.agent_active else "agente:pausado"
            _log_activity(session, ticket_id, user, action)
            logger.info("Ticket #%s: %s (por %s)", ticket_id, action, user)
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


# Veredicto del equipo sobre un «caso para análisis» (casos-analisis §5.3 y §9): ¿era de verdad un
# caso, o un trámite normal que la regla de la revisión desvió? Es el KPI de la regla: se cuenta
# en TicketActivity (`veredicto:caso` / `veredicto:tramite-normal`), el último manda, y no cambia
# nada del folio.
VEREDICTOS = {"caso": "Sí, era un caso", "tramite-normal": "No, era un trámite normal"}


def _veredicto_actual(session, ticket: Ticket) -> str:
    ultima = session.exec(
        select(TicketActivity)
        .where(TicketActivity.ticket_id == ticket.id)
        .where(TicketActivity.action.startswith("veredicto:"))  # type: ignore[attr-defined]
        .where(TicketActivity.habilitado == True)  # noqa: E712
        .order_by(TicketActivity.id.desc())  # type: ignore[union-attr]
    ).first()
    return ultima.action.split(":", 1)[1] if ultima else ""


@router.post("/tickets/{ticket_id}/veredicto")
def ticket_veredicto(request: Request, ticket_id: int, valor: str = Form("")):
    """Cualquiera del alcance del folio lo da (es juicio del equipo, no del dueño). Solo en el
    folio «analisis» y con un valor de VEREDICTOS; lo demás se ignora en silencio, como los otros
    POST del folio."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    vista = alcance(user)
    with get_session() as session:
        ticket = session.get(Ticket, ticket_id)
        if (ticket is not None and vista.alcanza(ticket.office_id)
                and ticket.service_id == "analisis" and valor in VEREDICTOS):
            _log_activity(session, ticket_id, user, f"veredicto:{valor}")
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


def _log_activity(session, ticket_id: int, username: str, action: str) -> None:
    session.add(TicketActivity(ticket_id=ticket_id, username=username, action=action))
    session.commit()


def _append_note(ticket: Ticket, author: str, text: str) -> None:
    stamp = utcnow().strftime("%Y-%m-%d %H:%M")
    line = f"— {author} ({stamp}): {text.strip()}"
    ticket.notes = f"{ticket.notes}\n{line}" if ticket.notes else line


@router.post("/tickets/{ticket_id}/reassign")
def reassign_ticket(
    request: Request, ticket_id: int, to_user: str = Form(""), note: str = Form("")
):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    vista = alcance(user)
    with get_session() as session:
        ticket = session.get(Ticket, ticket_id)
        target = session.exec(select(User).where(User.username == to_user)).first()
        # El destino tiene que ser de la oficina DEL FOLIO: pasárselo a alguien
        # de otra lo deja con un dueño que no puede abrirlo.
        if (ticket is not None and target is not None
                and vista.alcanza(ticket.office_id)
                and target.office_id == ticket.office_id
                and ticket.assigned_to == user):
            ticket.assigned_to = to_user
            # Transferir ES trabajo humano: saca el folio de NUEVO para que
            # deje de contar como «dueño provisional» (asignacion.provisional).
            # Sin esto un folio recién repartido y transferido a propósito se lo
            # quedaba el primer compañero que le contestara al cliente, y el
            # traspaso deliberado se deshacía con un solo mensaje.
            if ticket.status == TicketStatus.NUEVO:
                ticket.status = TicketStatus.TOMADO
                ticket.status_changed_at = utcnow()
            if note.strip():
                _append_note(ticket, user, note)
            ticket.updated_at = utcnow()
            session.add(ticket)
            session.commit()
            _log_activity(session, ticket_id, user, f"reasignado:{to_user}")
            logger.info("Ticket #%s reasignado de %s a %s", ticket_id, user, to_user)
        elif ticket is not None:
            logger.warning(
                "Reasignación rechazada en #%s: %s no es el asignado o destino inválido",
                ticket_id, user,
            )
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


@router.post("/tickets/{ticket_id}/notes")
def add_ticket_note(request: Request, ticket_id: int, note: str = Form("")):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    vista = alcance(user)
    with get_session() as session:
        ticket = session.get(Ticket, ticket_id)
        if (ticket is not None and vista.alcanza(ticket.office_id)
                and ticket.assigned_to == user and note.strip()):
            _append_note(ticket, user, note)
            ticket.updated_at = utcnow()
            session.add(ticket)
            session.commit()
            _log_activity(session, ticket_id, user, "nota")
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


@router.post("/tickets/{ticket_id}/labels")
def add_ticket_label(request: Request, ticket_id: int, nombre: str = Form("")):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    # Minúsculas: "Urgente" y "urgente" serían dos etiquetas distintas (SQLite
    # colaciona BINARY) y partirían el conteo del top en /activity.
    limpio = nombre.strip().lower()[:80]
    if not limpio:
        return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)
    vista = alcance(user)
    with get_session() as session:
        ticket = session.get(Ticket, ticket_id)
        if ticket is None or not vista.alcanza(ticket.office_id):
            return RedirectResponse("/", status_code=303)
        try:
            label = session.exec(select(Label).where(Label.nombre == limpio)).first()
            if label is None:
                label = Label(nombre=limpio)
                session.add(label)
                session.commit()
                session.refresh(label)
            # A lo más existe UNA fila por par (folio, etiqueta): quitar
            # deshabilita y re-etiquetar reactiva — nunca hay duplicado.
            vinculo = session.exec(
                select(TicketLabel).where(
                    TicketLabel.ticket_id == ticket_id,
                    TicketLabel.label_id == label.id,
                )
            ).first()
            if vinculo is None:
                session.add(
                    TicketLabel(ticket_id=ticket_id, label_id=label.id, created_by=user)
                )
            elif not vinculo.habilitado:
                vinculo.habilitado = True
                vinculo.created_by = user
                session.add(vinculo)
            session.commit()
        except IntegrityError:
            # Carrera check-then-insert: otro POST simultáneo ya creó la
            # etiqueta o el vínculo. El estado final es el deseado; sin 500.
            session.rollback()
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


@router.post("/tickets/{ticket_id}/labels/{label_id}/remove")
def remove_ticket_label(request: Request, ticket_id: int, label_id: int):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    vista = alcance(user)
    with get_session() as session:
        ticket = session.get(Ticket, ticket_id)
        if ticket is None or not vista.alcanza(ticket.office_id):
            return RedirectResponse("/", status_code=303)
        vinculo = session.exec(
            select(TicketLabel).where(
                TicketLabel.ticket_id == ticket_id,
                TicketLabel.label_id == label_id,
                TicketLabel.habilitado == True,  # noqa: E712
            )
        ).first()
        if vinculo:
            vinculo.habilitado = False
            session.add(vinculo)
            session.commit()
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


@router.post("/tickets/{ticket_id}/status")
def update_ticket_status(request: Request, ticket_id: int, status: str = Form("")):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    vista = alcance(user)
    with get_session() as session:
        ticket = session.get(Ticket, ticket_id)
        if ticket is not None and vista.alcanza(ticket.office_id) and ticket.assigned_to == user:
            logger.info(
                "Ticket #%s: %s -> %s (por %s)", ticket_id, ticket.status.value, status, user
            )
            new_status = TicketStatus(status)
            changed = ticket.status != new_status
            ticket.status = new_status
            ticket.updated_at = utcnow()
            if changed:
                ticket.status_changed_at = ticket.updated_at
                if new_status == TicketStatus.CERRADO:
                    ticket.resolved_at = ticket.updated_at
            session.add(ticket)
            session.commit()
            if changed:
                action = "cerrado" if new_status == TicketStatus.CERRADO else f"estado:{new_status.value}"
                _log_activity(session, ticket_id, user, action)
        elif ticket is not None:
            logger.warning(
                "Cambio de estado rechazado en #%s: %s no es el asignado (%s)",
                ticket_id, user, ticket.assigned_to,
            )

    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)
