"""CRUD de colaboradores — accesible solo para administradores.

Alta, reset de contraseña, cambio de rol y habilitar/deshabilitar
(soft-delete; nunca DELETE físico). Un admin no puede deshabilitarse ni
quitarse el rol a sí mismo, para no dejar el panel sin administrador.
"""

import logging

import bcrypt
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from app.audit import log_admin
from app.auth import is_admin
from app.dashboard import PASSWORD_MIN, _validate_registration
from app.db import get_session
from app.models import User

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.globals["is_admin"] = is_admin


def _admin_or_redirect(request: Request):
    """Devuelve el username admin, o una redirección si no hay sesión/rol."""
    user = request.session.get("user")
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    if not is_admin(user):
        return None, RedirectResponse("/", status_code=303)
    return user, None


def _render_list(request: Request, admin: str, error: str | None = None, ok: str | None = None):
    with get_session() as session:
        users = session.exec(select(User).order_by(User.username)).all()
    return templates.TemplateResponse(
        "users.html",
        {"request": request, "user": admin, "users": users, "error": error, "ok": ok},
    )


@router.get("/users")
def users_list(request: Request):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    return _render_list(request, admin)


@router.post("/users")
def users_create(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("colaborador"),
):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect

    username = username.strip().lower()
    error = _validate_registration(username, password)
    if error:
        return _render_list(request, admin, error=error)

    with get_session() as session:
        if session.exec(select(User).where(User.username == username)).first():
            return _render_list(request, admin, error="Ese usuario ya existe.")
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt())
        session.add(
            User(username=username, password_hash=hashed.decode(), is_admin=role == "admin")
        )
        session.commit()

    log_admin(admin, "usuario:creado", username)
    logger.info("Usuario %s creado por %s (rol %s)", username, admin, role)
    return _render_list(request, admin, ok=f"Usuario {username} creado.")


@router.post("/users/{username}/password")
def users_reset_password(request: Request, username: str, password: str = Form(...)):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect

    if len(password) < PASSWORD_MIN:
        return _render_list(
            request, admin,
            error=f"La contraseña debe tener al menos {PASSWORD_MIN} caracteres.",
        )

    with get_session() as session:
        target = session.exec(select(User).where(User.username == username)).first()
        if target is None:
            return _render_list(request, admin, error="Usuario no encontrado.")
        target.password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        session.add(target)
        session.commit()

    log_admin(admin, "usuario:password", username)
    logger.info("Contraseña de %s restablecida por %s", username, admin)
    return _render_list(request, admin, ok=f"Contraseña de {username} actualizada.")


@router.post("/users/{username}/role")
def users_toggle_role(request: Request, username: str):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    if username == admin:
        return _render_list(request, admin, error="No puedes cambiar tu propio rol.")

    with get_session() as session:
        target = session.exec(select(User).where(User.username == username)).first()
        if target is None:
            return _render_list(request, admin, error="Usuario no encontrado.")
        target.is_admin = not target.is_admin
        session.add(target)
        session.commit()
        new_role = "admin" if target.is_admin else "colaborador"

    log_admin(admin, f"usuario:rol:{new_role}", username)
    logger.info("Rol de %s cambiado a %s por %s", username, new_role, admin)
    return _render_list(request, admin, ok=f"{username} ahora es {new_role}.")


@router.post("/users/{username}/toggle")
def users_toggle_enabled(request: Request, username: str):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    if username == admin:
        # Evita que un admin se deje a sí mismo fuera del panel.
        return _render_list(request, admin, error="No puedes deshabilitarte a ti mismo.")

    with get_session() as session:
        target = session.exec(select(User).where(User.username == username)).first()
        if target is None:
            return _render_list(request, admin, error="Usuario no encontrado.")
        target.habilitado = not target.habilitado
        session.add(target)
        session.commit()
        state = "habilitado" if target.habilitado else "deshabilitado"

    log_admin(admin, f"usuario:{state}", username)
    logger.info("Colaborador %s %s por %s", username, state, admin)
    return _render_list(request, admin, ok=f"{username} {state}.")
