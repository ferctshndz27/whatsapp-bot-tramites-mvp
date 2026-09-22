"""CRUD de colaboradores — accesible solo para administradores.

Alta, reset de contraseña, cambio de rol y habilitar/deshabilitar
(soft-delete; nunca DELETE físico). Un admin no puede deshabilitarse ni
quitarse el rol a sí mismo, para no dejar el panel sin administrador.

Dos niveles de admin (auth.is_admin_global): el GLOBAL gestiona a todos; el de
OFICINA (`solo_su_oficina`) solo a la gente de su oficina y nunca a un admin
global —si pudiera, se quitaba el techo degradando a quien se lo puso—. Lo que
el de oficina da de alta o nombra admin nace admin de oficina, y mover gente
entre oficinas o quitarle/ponerle el techo a un admin es solo del global.
"""

import logging

import bcrypt
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from app.templating import templates
from sqlmodel import select

from app.audit import log_admin
from app.auth import alcance, is_admin, is_admin_global
from app.authlog import revocar_sesiones
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


def _global_or_redirect(request: Request):
    """Como _admin_or_redirect, pero solo el admin GLOBAL: lo de todas las
    oficinas (mover gente entre oficinas, /oficinas y sus llaves)."""
    user = request.session.get("user")
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    if not is_admin_global(user):
        return None, RedirectResponse("/", status_code=303)
    return user, None


def _es_admin_global(u: User) -> bool:
    return u.is_admin and not u.solo_su_oficina


def _gestionable(vista, objetivo: User) -> bool:
    """¿Puede un admin con este alcance tocar la cuenta de `objetivo`? El
    global, a todos; el de oficina, a los de su oficina que no sean admin
    global. Recibe el alcance ya resuelto: la lista lo pregunta por fila."""
    if vista.ve_todas:
        return True
    return objetivo.office_id == vista.office_id and not _es_admin_global(objetivo)


def _render_list(request: Request, admin: str, error: str | None = None, ok: str | None = None):
    vista = alcance(admin)
    with get_session() as session:
        users = session.exec(vista.filtrar(select(User), User.office_id).order_by(User.username)).all()
        # Solo las habilitadas como destino: asignar a alguien a una oficina
        # dada de baja lo deja sin ver nada —su ruteo de entrada está cortado—
        # y el síntoma («no me aparecen folios») no apunta a la causa. El
        # admin de oficina solo ve la suya: es la única en la que puede dar altas.
        oficinas = session.exec(
            vista.filtrar(select(Office), Office.id)
            .where(Office.habilitado == True).order_by(Office.id)  # noqa: E712
        ).all()
        nombres = {o.id: o.nombre for o in session.exec(select(Office)).all()}
    return templates.TemplateResponse(
        request, "users.html",
        {"request": request, "user": admin, "users": users, "oficinas": oficinas,
         "nombres_oficina": nombres, "admin_global": vista.ve_todas,
         "gestionables": {u.username for u in users if _gestionable(vista, u)},
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
    username: str = Form(""),
    password: str = Form(""),
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
    vista = alcance(admin)
    if not vista.ve_todas:
        # El de oficina da altas SOLO en la suya y nunca de admin global: el
        # formulario ya no lo ofrece, pero el POST lo arma el navegador.
        if office_id != vista.office_id:
            return _render_list(request, admin, error="Solo puedes dar de alta gente de tu oficina.")
        if role == "admin":
            return _render_list(request, admin, error="Solo un admin global crea admins globales.")

    with get_session() as session:
        if session.exec(select(User).where(User.username == username)).first():
            return _render_list(request, admin, error="Ese usuario ya existe.")
        oficina = session.get(Office, office_id)
        if oficina is None or not oficina.habilitado:
            return _render_list(request, admin, error="Esa oficina no existe o está dada de baja.")
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt())
        session.add(
            User(username=username, password_hash=hashed.decode(),
                 # Un rol desconocido cae en colaborador, el menos privilegiado.
                 is_admin=role in ("admin", "admin_oficina"),
                 solo_su_oficina=role == "admin_oficina", office_id=office_id)
        )
        session.commit()

    log_admin(admin, "usuario:creado", f"{username} oficina={office_id}")
    logger.info("Usuario %s creado por %s (rol %s)", username, admin, role)
    return _render_list(request, admin, ok=f"Usuario {username} creado.")


@router.post("/users/{username}/password")
def users_reset_password(request: Request, username: str, password: str = Form("")):
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
        # Fuera de su alcance responde igual que «no existe».
        if target is None or not _gestionable(alcance(admin), target):
            return _render_list(request, admin, error="Usuario no encontrado.")
        target.password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        session.add(target)
        # Una contraseña se resetea porque se perdió o porque se filtró, y en el
        # segundo caso quien la tiene ya está adentro: cambiarla sin cerrar lo
        # abierto no saca a nadie (auditoría 2026-09, AUTH-1c). En la misma
        # transacción que el hash: si esto falla, la contraseña tampoco cambia.
        # Quien se la cambia a sí mismo conserva la sesión desde la que lo hizo.
        revocar_sesiones(
            session, username, excepto=request.session.get("sid") if username == admin else None
        )
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
        if target is None or not _gestionable(alcance(admin), target):
            return _render_list(request, admin, error="Usuario no encontrado.")
        target.is_admin = not target.is_admin
        if target.is_admin and not alcance(admin).ve_todas:
            # Un admin nombrado por un admin de oficina nace con su mismo techo:
            # sin esto, «Hacer admin» fabricaba un admin GLOBAL desde abajo.
            target.solo_su_oficina = True
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
        if target is None or not _gestionable(alcance(admin), target):
            return _render_list(request, admin, error="Usuario no encontrado.")
        target.habilitado = not target.habilitado
        session.add(target)
        if not target.habilitado:
            # El guardia de authlog ya lo saca en su siguiente request por
            # `habilitado`; esto cierra además sus websessions, para que /logins
            # deje de mostrarlo conectado y para que rehabilitarlo no le devuelva
            # una sesión vieja: vuelve a entrar con su contraseña. En la misma
            # transacción que la baja, por lo mismo que en el reset.
            revocar_sesiones(session, username)
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

    Solo el admin global: mover a alguien es sacarlo de una oficina y meterlo
    en otra, y el de oficina no alcanza la de destino.
    """
    admin, redirect = _global_or_redirect(request)
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


@router.post("/users/{username}/solo-oficina")
def users_toggle_solo_oficina(request: Request, username: str):
    """Pone o quita el techo de un admin: admin global <-> admin de oficina.

    Solo el global. Un admin de oficina no puede quitárselo a nadie —ni a sí
    mismo—: sería salirse de su oficina por la puerta de este formulario.
    """
    admin, redirect = _global_or_redirect(request)
    if redirect:
        return redirect
    if username == admin:
        # El último global que se limita a sí mismo deja el panel sin nadie
        # que administre oficinas y llaves.
        return _render_list(request, admin, error="No puedes cambiar tu propio alcance.")

    with get_session() as session:
        target = session.exec(select(User).where(User.username == username)).first()
        if target is None or not target.is_admin:
            return _render_list(request, admin, error="Solo un admin puede limitarse a su oficina.")
        target.solo_su_oficina = not target.solo_su_oficina
        session.add(target)
        session.commit()
        target_limitado = target.solo_su_oficina
        alcance_nuevo = "de oficina" if target_limitado else "global"

    log_admin(admin, "usuario:admin-oficina" if target_limitado else "usuario:admin-global", username)
    logger.info("%s ahora es admin %s (por %s)", username, alcance_nuevo, admin)
    return _render_list(request, admin, ok=f"{username} ahora es admin {alcance_nuevo}.")
