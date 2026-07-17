import hmac
import logging
import secrets
import time

import bcrypt
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import update
from sqlmodel import select

from app import whatsapp_client
from app.auth import is_admin
from app.config import settings
from app.db import get_session
from app.models import Invitation, Message, Ticket, TicketStatus, User, utcnow

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.globals["is_admin"] = is_admin

LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 300
# ponytail: contador en memoria por proceso — se reinicia al reiniciar el
# servidor y no se comparte entre instancias. Pasar a DB/Redis si se corre
# con más de un worker o se necesita persistencia entre reinicios.
_login_attempts: dict[str, list[float]] = {}


def _is_login_locked(username: str) -> bool:
    now = time.time()
    attempts = [t for t in _login_attempts.get(username, []) if now - t < LOGIN_WINDOW_SECONDS]
    _login_attempts[username] = attempts
    return len(attempts) >= LOGIN_MAX_ATTEMPTS


def _record_failed_login(username: str) -> None:
    _login_attempts.setdefault(username, []).append(time.time())


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
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    if _is_login_locked(username):
        logger.warning("Login bloqueado por intentos fallidos: %s", username)
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Demasiados intentos fallidos. Intenta de nuevo en unos minutos."},
        )

    with get_session() as session:
        user = session.exec(select(User).where(User.username == username)).first()

    if user and bcrypt.checkpw(password.encode(), user.password_hash.encode()):
        _login_attempts.pop(username, None)
        logger.info("Login exitoso: %s", username)
        request.session["user"] = username
        return RedirectResponse("/", status_code=303)

    _record_failed_login(username)
    logger.warning("Login fallido: %s", username)
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "Usuario o contraseña incorrectos"}
    )


@router.get("/logout")
def logout(request: Request):
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
        select(Invitation).where(Invitation.token == code, Invitation.used_at.is_(None))
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
    return templates.TemplateResponse("register.html", {"request": request, "error": None})


@router.post("/register")
def register_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    code: str = Form(...),
):
    username = username.strip().lower()
    # ponytail: reusa el lockout del login con una llave global para frenar
    # fuerza bruta del código de invitación; pasar a llave por-IP si hace falta.
    if _is_login_locked("__register__"):
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Demasiados intentos. Espera unos minutos."},
        )

    error = _validate_registration(username, password)
    if error:
        _record_failed_login("__register__")
        logger.warning("Registro rechazado para %s: %s", username, error)
        return templates.TemplateResponse("register.html", {"request": request, "error": error})

    with get_session() as session:
        code_ok, invitation = _code_grants_registration(session, code)
        if not code_ok:
            _record_failed_login("__register__")
            logger.warning("Registro rechazado para %s: código inválido", username)
            return templates.TemplateResponse(
                "register.html",
                {
                    "request": request,
                    "error": "Código de invitación incorrecto, ya utilizado, o el registro está deshabilitado.",
                },
            )

        if session.exec(select(User).where(User.username == username)).first():
            return templates.TemplateResponse(
                "register.html", {"request": request, "error": "Ese usuario ya existe."}
            )

        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt())
        session.add(User(username=username, password_hash=hashed.decode()))
        if invitation is not None:
            invitation.used_at = utcnow()
            invitation.used_by = username
            session.add(invitation)
        session.commit()

    logger.info("Usuario registrado: %s", username)
    request.session["user"] = username
    return RedirectResponse("/", status_code=303)


def _invitation_message(token: str, request: Request) -> str:
    base = str(request.base_url).rstrip("/")
    return (
        "Te invitamos a colaborar en el panel de trámites.\n"
        f"Regístrate en {base}/register con el código de invitación: {token}"
    )


@router.get("/invitations")
def invitations_list(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not is_admin(user):
        return RedirectResponse("/", status_code=303)

    with get_session() as session:
        invitations = session.exec(select(Invitation).order_by(Invitation.created_at.desc())).all()

    return templates.TemplateResponse(
        "invitations.html", {"request": request, "user": user, "invitations": invitations}
    )


@router.post("/invitations")
def invitations_create(request: Request, phone: str = Form("")):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not is_admin(user):
        return RedirectResponse("/", status_code=303)

    phone = phone.strip()
    token = secrets.token_urlsafe(9)
    sent = False

    with get_session() as session:
        session.add(Invitation(token=token, phone=phone or None, created_by=user))
        session.commit()

        if phone:
            sent = whatsapp_client.send_text(phone, _invitation_message(token, request)) is not None
            if sent:
                logger.info("Invitación enviada por WhatsApp a %s (por %s)", phone, user)
            else:
                logger.warning("No se pudo enviar la invitación por WhatsApp a %s", phone)

        invitations = session.exec(select(Invitation).order_by(Invitation.created_at.desc())).all()

    return templates.TemplateResponse(
        "invitations.html",
        {
            "request": request,
            "user": user,
            "invitations": invitations,
            "new_token": token,
            "new_phone": phone,
            "sent": sent,
        },
    )


@router.get("/")
def ticket_list(request: Request, status: str | None = None, assigned: str | None = None):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    with get_session() as session:
        all_tickets = session.exec(select(Ticket).order_by(Ticket.created_at.desc())).all()

    stats = {
        "total": len(all_tickets),
        "nuevos": sum(1 for t in all_tickets if t.status == TicketStatus.NUEVO),
        "activos": sum(1 for t in all_tickets if t.status in (TicketStatus.TOMADO, TicketStatus.EN_PROCESO)),
        "escalados": sum(1 for t in all_tickets if t.escalado),
    }
    tickets = all_tickets
    if status:
        tickets = [t for t in tickets if t.status.value == status]
    if assigned == "none":
        tickets = [t for t in tickets if t.assigned_to is None]

    return templates.TemplateResponse(
        "tickets_list.html",
        {
            "request": request,
            "user": user,
            "tickets": tickets,
            "stats": stats,
            "statuses": [s.value for s in TicketStatus],
            "current_filter": status,
            "assigned_filter": assigned,
        },
    )


@router.get("/tickets/{ticket_id}")
def ticket_detail(request: Request, ticket_id: int):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    with get_session() as session:
        ticket = session.get(Ticket, ticket_id)
        if ticket is None:
            return RedirectResponse("/", status_code=303)
        messages = session.exec(
            select(Message).where(Message.phone == ticket.phone).order_by(Message.created_at)
        ).all()
        usernames = [u.username for u in session.exec(select(User)).all()]

    return templates.TemplateResponse(
        "ticket_detail.html",
        {
            "request": request,
            "user": user,
            "ticket": ticket,
            "messages": messages,
            "statuses": [s.value for s in TicketStatus],
            "usernames": [u for u in usernames if u != user],
        },
    )


@router.post("/tickets/{ticket_id}/claim")
def claim_ticket(request: Request, ticket_id: int):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    with get_session() as session:
        # Claim atómico: solo asigna si nadie la tiene — dos asesores no
        # pueden tomar la misma solicitud aunque hagan clic a la vez.
        result = session.exec(
            update(Ticket)
            .where(
                Ticket.id == ticket_id,
                Ticket.assigned_to.is_(None),
                Ticket.status != TicketStatus.CERRADO,
            )
            .values(assigned_to=user, status=TicketStatus.TOMADO, updated_at=utcnow())
        )
        session.commit()

    if result.rowcount:
        logger.info("Ticket #%s tomado por %s", ticket_id, user)
    else:
        logger.info("Ticket #%s ya estaba tomado (intento de %s)", ticket_id, user)
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


@router.post("/tickets/{ticket_id}/release")
def release_ticket(request: Request, ticket_id: int):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    with get_session() as session:
        result = session.exec(
            update(Ticket)
            .where(Ticket.id == ticket_id, Ticket.assigned_to == user)
            .values(assigned_to=None, status=TicketStatus.NUEVO, updated_at=utcnow())
        )
        session.commit()

    if result.rowcount:
        logger.info("Ticket #%s soltado por %s", ticket_id, user)
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


def _append_note(ticket: Ticket, author: str, text: str) -> None:
    stamp = utcnow().strftime("%Y-%m-%d %H:%M")
    line = f"— {author} ({stamp}): {text.strip()}"
    ticket.notes = f"{ticket.notes}\n{line}" if ticket.notes else line


@router.post("/tickets/{ticket_id}/reassign")
def reassign_ticket(
    request: Request, ticket_id: int, to_user: str = Form(...), note: str = Form("")
):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    with get_session() as session:
        ticket = session.get(Ticket, ticket_id)
        target = session.exec(select(User).where(User.username == to_user)).first()
        if ticket is not None and target is not None and ticket.assigned_to == user:
            ticket.assigned_to = to_user
            if note.strip():
                _append_note(ticket, user, note)
            ticket.updated_at = utcnow()
            session.add(ticket)
            session.commit()
            logger.info("Ticket #%s reasignado de %s a %s", ticket_id, user, to_user)
        elif ticket is not None:
            logger.warning(
                "Reasignación rechazada en #%s: %s no es el asignado o destino inválido",
                ticket_id, user,
            )
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


@router.post("/tickets/{ticket_id}/notes")
def add_ticket_note(request: Request, ticket_id: int, note: str = Form(...)):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    with get_session() as session:
        ticket = session.get(Ticket, ticket_id)
        if ticket is not None and ticket.assigned_to == user and note.strip():
            _append_note(ticket, user, note)
            ticket.updated_at = utcnow()
            session.add(ticket)
            session.commit()
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


@router.post("/tickets/{ticket_id}/status")
def update_ticket_status(request: Request, ticket_id: int, status: str = Form(...)):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    with get_session() as session:
        ticket = session.get(Ticket, ticket_id)
        if ticket is not None and ticket.assigned_to == user:
            logger.info(
                "Ticket #%s: %s -> %s (por %s)", ticket_id, ticket.status.value, status, user
            )
            ticket.status = TicketStatus(status)
            ticket.updated_at = utcnow()
            session.add(ticket)
            session.commit()
        elif ticket is not None:
            logger.warning(
                "Cambio de estado rechazado en #%s: %s no es el asignado (%s)",
                ticket_id, user, ticket.assigned_to,
            )

    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)
