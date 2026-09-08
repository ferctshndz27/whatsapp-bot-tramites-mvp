"""CRUD de colaboradores — accesible solo para administradores.

Alta, reset de contraseña, cambio de rol y habilitar/deshabilitar
(soft-delete; nunca DELETE físico). Un admin no puede deshabilitarse ni
quitarse el rol a sí mismo, para no dejar el panel sin administrador.
"""

import logging

import bcrypt
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from app.templating import templates
from sqlmodel import select

from app.audit import log_admin
from app.auth import is_admin
from app.dashboard import PASSWORD_MIN, _validate_registration
from app.db import get_session
from app.models import OFICINA_TLAXCALA_ID, Office, User

logger = logging.getLogger(__name__)

router = APIRouter()


def mover_de_oficina(session, username: str, office_id: int, *, admin: str) -> tuple[bool, str]:
    """Cambia la oficina de un colaborador y SUELTA los folios que deja atrás.

    Devuelve (cambió, aviso). No hace commit: lo hace quien la llama.

    POR QUÉ SUELTA LOS FOLIOS: un folio vive en la oficina donde nació, y desde
    el PR 5 su detalle rechaza a quien no es de esa oficina. Sin soltarlos, el
    folio sigue diciendo «asignado a Ana» mientras Ana ya no puede abrirlo — y a
    diferencia del colaborador deshabilitado, aquí nadie sospecha, porque Ana
    sigue trabajando. Es exactamente el trabajo congelado que describe el
    comentario de dashboard.release_ticket, que existe para rescatarlo; esto lo
    evita en vez de tener que rescatarlo después.

    Los folios de la oficina de DESTINO no se tocan: allá sí los puede atender.
    """
    from sqlmodel import select

    from app.models import Ticket, TicketStatus, User

    objetivo = session.exec(select(User).where(User.username == username)).first()
    if objetivo is None:
        return False, "Ese colaborador no existe."
    if objetivo.office_id == office_id:
        return False, f"{username} ya es de esa oficina."

    huerfanos = session.exec(
        select(Ticket).where(
            Ticket.assigned_to == username,
            Ticket.office_id != office_id,
            Ticket.status != TicketStatus.CERRADO,
            Ticket.habilitado == True,  # noqa: E712
        )
    ).all()
    for folio in huerfanos:
        folio.assigned_to = None
        session.add(folio)

    objetivo.office_id = office_id
    session.add(objetivo)

    aviso = f"{username} ahora es de esta oficina."
    if huerfanos:
        aviso += (f" Se soltaron {len(huerfanos)} folio(s) abiertos que tenía en la anterior: "
                  f"vuelven a la cola de esa oficina, donde sí se pueden atender.")
    return True, aviso


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
        # Solo las habilitadas como destino: asignar a alguien a una oficina
        # dada de baja lo deja sin ver nada —su ruteo de entrada está cortado—
        # y el síntoma («no me aparecen folios») no apunta a la causa.
        oficinas = session.exec(
            select(Office).where(Office.habilitado == True).order_by(Office.id)  # noqa: E712
        ).all()
    return templates.TemplateResponse(
        "users.html",
        {"request": request, "user": admin, "users": users, "oficinas": oficinas,
         "nombres_oficina": {o.id: o.nombre for o in oficinas},
         "error": error, "ok": ok},
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
    office_id: int = Form(OFICINA_TLAXCALA_ID),
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
        oficina = session.get(Office, office_id)
        if oficina is None or not oficina.habilitado:
            return _render_list(request, admin, error="Esa oficina no existe o está dada de baja.")
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt())
        session.add(
            User(username=username, password_hash=hashed.decode(),
                 is_admin=role == "admin", office_id=office_id)
        )
        session.commit()

    log_admin(admin, "usuario:creado", f"{username} oficina={office_id}")
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


@router.post("/users/{username}/oficina")
def users_cambiar_oficina(request: Request, username: str, office_id: int = Form(...)):
    """Reasignar un colaborador desde /users — el camino inverso a
    /oficinas/{id}/usuarios, que jala desde la oficina.

    Los dos existen porque son dos flujos reales («¿de qué oficina es Ana?» y
    «¿a quién le pongo en Puebla?»), pero comparten `offices.mover_de_oficina`:
    una sola regla, incluida la de soltar los folios que quedan del otro lado.
    """
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect

    with get_session() as session:
        oficina = session.get(Office, office_id)
        if oficina is None or not oficina.habilitado:
            return _render_list(request, admin, error="Esa oficina no existe o está dada de baja.")
        # El nombre se copia DENTRO de la sesión: leerlo después de cerrarla es
        # un DetachedInstanceError, y el aviso lo necesita.
        nombre_oficina = oficina.nombre
        cambio, aviso = mover_de_oficina(session, username, office_id, admin=admin)
        if not cambio:
            return _render_list(request, admin, error=aviso)
        session.commit()

    log_admin(admin, "usuario:oficina", f"{username} -> oficina={office_id}")
    logger.info("Usuario %s movido a la oficina %s por %s", username, office_id, admin)
    return _render_list(request, admin, ok=aviso.replace("de esta oficina", f"de {nombre_oficina}"))
