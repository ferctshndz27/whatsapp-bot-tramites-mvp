"""Panel de oficinas — docs/SPEC-MULTI-OFICINA.md §5. Solo administradores.

Alta de oficinas, CRUD de sus credenciales y movimiento de colaboradores. Dos
reglas que este router NO puede relajar:

1. **El valor de una credencial nunca sale de aquí.** Ni al HTML, ni al log, ni
   a `AdminAudit.target` —esta última se espeja a MSSQL, así que un valor ahí
   viajaría en claro a otra base y anularía el cifrado de §3.1—. Lo único que se
   pinta es la pista (últimos 4) que devuelve `credenciales.estado()`.
2. **Una llave `compartida` no se puede CREAR desde aquí** (§5: se renderiza de
   solo lectura como «heredada del global»), y la comprobación vive en el
   endpoint además del template: ocultar un botón no es una regla.

   CONSECUENCIA PARA EL PR 4: la migración de Tlaxcala del §7 no puede pasar por
   este panel para las cuatro llaves marcadas `compartida` — esas filas las
   escribe un script llamando a `credenciales.guardar()` directo, que es
   exactamente lo que hace tests/test_oficinas.py en la prueba de /system.

3. **El `provider` no pasa por el formulario.** Lo deriva `credenciales` del
   inventario (§5, constricción 2). Una fila con provider ajeno no casa con el
   WHERE del resolvedor y heredaría en silencio: el panel la mostraría `propia`
   mientras el sistema sigue usando la del .env.
"""

import logging
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlmodel import func, select

from app import credenciales
from app.audit import log_admin
from app.config import CREDENCIALES
from app.db import get_session
from app.models import ZONA_HORARIA_DEFAULT, Office, User
from app.templating import templates
from app.users import _admin_or_redirect

logger = logging.getLogger(__name__)

router = APIRouter()


def _llaves_de(office_id: int) -> list[dict]:
    """Las 10 del inventario, SIEMPRE las 10 (§5: «no deja huecos»).

    `editable` sale de `compartida`, el estado sale de los datos: una llave
    compartida con fila viva se pinta `propia` —porque eso es lo que el
    resolvedor va a usar— y conserva su botón de baja. Lo que `compartida`
    suprime es el formulario de alta, no el estado ni la manera de deshacerlo.
    """
    filas = []
    for clave, meta in CREDENCIALES.items():
        estado, pista = credenciales.estado(clave, office_id=office_id)
        filas.append({
            "clave": clave,
            "provider": meta.provider,
            "es_secreto": meta.es_secreto,
            "compartida": meta.compartida,
            "estado": estado,
            "pista": pista,
            "editable": not meta.compartida or estado == credenciales.PROPIA,
        })
    return filas


def _render_lista(request: Request, admin: str, error=None, ok=None):
    with get_session() as session:
        oficinas = session.exec(select(Office).order_by(Office.id)).all()
        conteo = dict(
            session.exec(
                select(User.office_id, func.count(User.id))
                .where(User.habilitado == True)  # noqa: E712
                .group_by(User.office_id)
            ).all()
        )
    # Los estados se resuelven con la sesión YA cerrada. Leerlos con una
    # escritura pendiente dispara el autoflush, que toma el candado de ESCRITOR
    # de SQLite — el mismo error que costó un mensaje perdido en dashboard.py
    # (_notify_finish). Aquí no hay escritura en vuelo y así se queda.
    resumen = []
    for oficina in oficinas:
        llaves = _llaves_de(oficina.id)
        resumen.append({
            "oficina": oficina,
            "usuarios": conteo.get(oficina.id, 0),
            "propias": sum(1 for l in llaves if l["estado"] == credenciales.PROPIA),
            "ilegibles": sum(1 for l in llaves if l["estado"] == credenciales.ILEGIBLE),
            "ausentes": sum(1 for l in llaves if l["estado"] == credenciales.AUSENTE),
        })
    return templates.TemplateResponse(
        "oficinas.html",
        {"request": request, "user": admin, "resumen": resumen,
         "tz_default": ZONA_HORARIA_DEFAULT, "error": error, "ok": ok},
    )


def _detalle_o_lista(request: Request, admin: str, office_id: int, error=None, ok=None):
    """_render_detalle o la lista si esa oficina no existe.

    Sin esto, un POST a /oficinas/999/llaves devolvía None como cuerpo de la
    respuesta —un 500 por una URL tecleada a mano—.
    """
    return (_render_detalle(request, admin, office_id, error, ok)
            or RedirectResponse("/oficinas", status_code=303))


def _render_detalle(request: Request, admin: str, office_id: int, error=None, ok=None):
    with get_session() as session:
        oficina = session.get(Office, office_id)
        if oficina is None:
            return None
        usuarios = session.exec(
            select(User).where(User.office_id == office_id).order_by(User.username)
        ).all()
        otros = session.exec(
            select(User).where(User.office_id != office_id, User.habilitado == True)  # noqa: E712
            .order_by(User.username)
        ).all()
    return templates.TemplateResponse(
        "oficina.html",
        {"request": request, "user": admin, "oficina": oficina, "usuarios": usuarios,
         "otros": otros, "llaves": _llaves_de(office_id),
         "hay_llave_maestra": bool(credenciales.settings.credentials_key),
         "error": error, "ok": ok},
    )


@router.get("/oficinas")
def oficinas_lista(request: Request):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    return _render_lista(request, admin)


@router.post("/oficinas")
def oficinas_crear(
    request: Request,
    nombre: str = Form(...),
    slug: str = Form(...),
    ciudad: str = Form(...),
    zona_horaria: str = Form(ZONA_HORARIA_DEFAULT),
):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect

    nombre, ciudad = nombre.strip(), ciudad.strip()
    slug = slug.strip().lower()
    zona_horaria = zona_horaria.strip() or ZONA_HORARIA_DEFAULT

    if not nombre or not ciudad:
        return _render_lista(request, admin, error="Nombre y ciudad son obligatorios.")
    # El slug es la URL de /webhooks/stripe/{slug} del PR 5: lo que se acepte
    # hoy es el endpoint de mañana. Un slug con espacios o acentos se guarda sin
    # ruido y rompe allá, lejos de aquí.
    if not slug or not all(c.isalnum() and c.isascii() or c == "-" for c in slug):
        return _render_lista(
            request, admin,
            error="El slug solo admite minúsculas, números y guiones (será la URL del webhook).",
        )
    try:
        ZoneInfo(zona_horaria)
    except Exception:
        return _render_lista(
            request, admin,
            error=f"Zona horaria desconocida: {zona_horaria}. Ejemplo: {ZONA_HORARIA_DEFAULT}.",
        )

    try:
        with get_session() as session:
            session.add(Office(nombre=nombre, slug=slug, ciudad=ciudad,
                               zona_horaria=zona_horaria))
            session.commit()
    except IntegrityError:
        # nombre y slug son UNIQUE. Sin esta rama, un alta repetida es un 500.
        return _render_lista(request, admin, error="Ya existe una oficina con ese nombre o slug.")

    log_admin(admin, "oficina:creada", slug)
    logger.info("Oficina %s (%s) creada por %s", nombre, slug, admin)
    return _render_lista(request, admin, ok=f"Oficina {nombre} creada.")


@router.get("/oficinas/{office_id}")
def oficina_detalle(request: Request, office_id: int):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    respuesta = _detalle_o_lista(request, admin, office_id)
    return respuesta if respuesta is not None else RedirectResponse("/oficinas", status_code=303)


@router.post("/oficinas/{office_id}/llaves")
def llave_guardar(request: Request, office_id: int, clave: str = Form(...), valor: str = Form(...)):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect

    if clave not in CREDENCIALES:
        return _detalle_o_lista(request, admin, office_id, error="Esa credencial no existe.")
    if not valor.strip():
        return _detalle_o_lista(request, admin, office_id, error="El valor no puede ir vacío.")
    # El formulario de estas ya no se pinta (§5: «de solo lectura como heredada
    # del global»), pero ocultar un botón no es una regla: sin esta rama un POST
    # directo crea la fila igual, y una llave compartida convertida en propia de
    # UNA oficina es exactamente lo que el paso 4 de §7 evita. La UI y la API
    # tienen que decir lo mismo.
    #
    # No cierra la puerta a rotar lo que YA es propio: si hay fila viva, el
    # resolvedor la está usando y negar su reemplazo dejaría una llave
    # irreemplazable desde el panel.
    estado_actual, _ = credenciales.estado(clave, office_id=office_id)
    if CREDENCIALES[clave].compartida and estado_actual != credenciales.PROPIA:
        return _render_detalle(
            request, admin, office_id,
            error=f"{clave} es compartida por todas las oficinas: se administra en el .env.",
        )
    # Primer punto del sistema donde CREDENTIALS_KEY deja de ser opcional. El
    # mensaje nombra la variable porque la acción del operador es generarla y
    # ponerla en el .env; guardar en claro «mientras tanto» está prohibido (§3.1).
    if not credenciales.settings.credentials_key:
        return _render_detalle(
            request, admin, office_id,
            error="Falta CREDENTIALS_KEY en el .env: sin ella no se puede cifrar la credencial.",
        )

    credenciales.guardar(clave, office_id=office_id, valor=valor.strip(), updated_by=admin)
    # El target lleva la llave y la oficina, JAMÁS el valor ni la pista:
    # adminaudit se espeja a MSSQL y ahí viajaría en claro.
    log_admin(admin, "credencial:guardada", f"{clave} oficina={office_id}")
    logger.info("Credencial %s de la oficina %s actualizada por %s", clave, office_id, admin)
    return _detalle_o_lista(request, admin, office_id, ok=f"{clave} guardada para esta oficina.")


@router.post("/oficinas/{office_id}/llaves/{clave}/baja")
def llave_baja(request: Request, office_id: int, clave: str):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    if clave not in CREDENCIALES:
        return _detalle_o_lista(request, admin, office_id, error="Esa credencial no existe.")

    if not credenciales.dar_de_baja(clave, office_id=office_id):
        return _detalle_o_lista(request, admin, office_id,
                               error=f"{clave} no tenía fila propia en esta oficina.")
    log_admin(admin, "credencial:baja", f"{clave} oficina={office_id}")
    logger.info("Credencial %s de la oficina %s dada de baja por %s", clave, office_id, admin)
    return _detalle_o_lista(request, admin, office_id,
                           ok=f"{clave} dada de baja: la oficina vuelve a heredar del .env.")


@router.post("/oficinas/{office_id}/usuarios")
def usuario_mover(request: Request, office_id: int, username: str = Form(...)):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect

    with get_session() as session:
        if session.get(Office, office_id) is None:
            return RedirectResponse("/oficinas", status_code=303)
        objetivo = session.exec(select(User).where(User.username == username)).first()
        if objetivo is None:
            return _detalle_o_lista(request, admin, office_id, error="Ese colaborador no existe.")
        objetivo.office_id = office_id
        session.add(objetivo)
        session.commit()

    log_admin(admin, "usuario:oficina", f"{username} -> oficina={office_id}")
    logger.info("Usuario %s movido a la oficina %s por %s", username, office_id, admin)
    return _detalle_o_lista(request, admin, office_id, ok=f"{username} ahora es de esta oficina.")
