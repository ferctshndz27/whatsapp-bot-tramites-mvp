"""Edición del catálogo de servicios desde el panel — de todos los colaboradores.

Sin esto, cambiar un precio requiere commit + push + deploy. Todas las
mutaciones pasan por la API de app.catalog (SQLite: el bot ve el cambio en
el siguiente mensaje) y quedan auditadas dos veces: la bitácora general
(AdminAudit) y el detalle campo a campo con valor anterior/nuevo
(CatalogAudit, visible al final de la página del catálogo).

Abierto a cualquier usuario con sesión, no solo al admin global (regla del
operador, 2026-09-18). Dos cosas lo sostienen:

- El catálogo es GLOBAL (SPEC-MULTI-OFICINA §11.3): un solo catálogo para todas
  las oficinas, así que no hay «catálogo de otra oficina» que un colaborador
  pudiera pisar. Lo que cambia uno lo ve el bot de todas, y por eso cada cambio
  lleva nombre en el historial.
- Cada colaborador declara aquí qué productos atiende (CatalogAsesor), y eso
  es lo que usa la asignación para mandarle sus folios. Se inscribe solo a sí
  mismo: la ruta toma el usuario de la sesión, no de un campo del formulario.
"""

import logging
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import RedirectResponse
from sqlmodel import select

from app import nlu
from app.templating import templates
from app.audit import log_admin
from app.auth import alcance
from app.catalog import catalog
from app.db import get_session
from app.models import User

logger = logging.getLogger(__name__)

router = APIRouter()


def _usuario_o_redirect(request: Request):
    user = request.session.get("user")
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    return user, None


def _visibles(user: str) -> set[str]:
    """A quién puede ver este usuario en la columna «Lo atienden»: la gente de
    su oficina, o de todas si es admin global — la misma regla que /users. El
    catálogo es de todos; la gente de cada oficina es de su oficina."""
    consulta = alcance(user).filtrar(select(User.username), User.office_id)
    with get_session() as session:
        return set(session.exec(consulta).all())


def _render(request: Request, admin: str, error: str | None = None, choques: list[str] | None = None):
    visibles = _visibles(admin)
    return templates.TemplateResponse(
        request, "catalog.html",
        {
            "request": request,
            "user": admin,
            "categories": catalog.admin_view(visibles),
            "changes": catalog.recent_changes(60, visibles),
            "error": error,
            "choques_revision": choques or [],
        },
    )


def _apply(request: Request, admin: str, action: str, target: str, error: str | None):
    """Auditar si la mutación fue exitosa; re-renderizar con el error si no."""
    if error:
        return _render(request, admin, error=error)
    log_admin(admin, f"catalogo:{action}", target)
    logger.info("Catálogo: %s %s por %s", action, target, admin)
    return RedirectResponse("/catalog", status_code=303)


@router.get("/catalog")
def catalog_view(request: Request, choque: Annotated[list[str] | None, Query()] = None):
    admin, redirect = _usuario_o_redirect(request)
    if redirect:
        return redirect
    return _render(request, admin, choques=[c[:80] for c in (choque or [])[:10]])


def choques_con_revision(sinonimos: list[str]) -> list[str]:
    """Los sinónimos que son, tal cual, una frase de `nlu.FRASES_REVISION` (casos-analisis §5.2,
    capa 3 de la colisión). Mientras lo sean, `pide_revision` ignora esa frase (capa 2): el
    cliente que la escriba irá a ese trámite en vez de que se le ofrezca la revisión. No bloquea:
    el colaborador decide; solo se le avisa."""
    frases = set(nlu.FRASES_REVISION)
    return [s for s in sinonimos if nlu._norm(s).strip() in frases]


@router.post("/catalog/categories")
def category_create(request: Request, name: str = Form("")):
    admin, redirect = _usuario_o_redirect(request)
    if redirect:
        return redirect
    if not name.strip():
        return _render(request, admin, error="El nombre no puede estar vacío.")
    return _apply(
        request, admin, "categoria_nueva", name.strip(), catalog.add_category(name, admin=admin)
    )


@router.post("/catalog/categories/{category_id}/rename")
def category_rename(request: Request, category_id: str, name: str = Form("")):
    admin, redirect = _usuario_o_redirect(request)
    if redirect:
        return redirect
    if not name.strip():
        return _render(request, admin, error="El nombre no puede estar vacío.")
    return _apply(
        request, admin, "categoria_renombrada", category_id,
        catalog.rename_category(category_id, name, admin=admin),
    )


@router.post("/catalog/categories/{category_id}/toggle")
def category_toggle(request: Request, category_id: str):
    admin, redirect = _usuario_o_redirect(request)
    if redirect:
        return redirect
    return _apply(
        request, admin, "categoria_toggle", category_id,
        catalog.toggle_category(category_id, admin=admin),
    )


@router.post("/catalog/services")
def service_create(
    request: Request,
    category_id: str = Form(""),
    name: str = Form(""),
    price_info: str = Form(""),
):
    admin, redirect = _usuario_o_redirect(request)
    if redirect:
        return redirect
    if not name.strip() or not price_info.strip():
        return _render(request, admin, error="Nombre y precio son obligatorios.")
    return _apply(
        request, admin, "servicio_nuevo", name.strip(),
        catalog.add_service(category_id, name, price_info, admin=admin),
    )


@router.post("/catalog/services/{service_id}/update")
def service_update(
    request: Request,
    service_id: str,
    name: str = Form(""),
    price_info: str = Form(""),
    synonyms: str = Form(""),
    requirements: str = Form(""),
):
    admin, redirect = _usuario_o_redirect(request)
    if redirect:
        return redirect
    if not name.strip() or not price_info.strip():
        return _render(request, admin, error="Nombre y precio son obligatorios.")
    synonym_list = [s for s in (part.strip() for part in synonyms.split(",")) if s]
    respuesta = _apply(
        request, admin, "servicio_editado", service_id,
        catalog.update_service(
            service_id, name, price_info, synonym_list, requirements, admin=admin
        ),
    )
    choques = choques_con_revision(synonym_list)
    if choques and isinstance(respuesta, RedirectResponse):
        return RedirectResponse("/catalog?" + urlencode([("choque", c) for c in choques]), status_code=303)
    return respuesta


@router.post("/catalog/services/{service_id}/toggle")
def service_toggle(request: Request, service_id: str):
    admin, redirect = _usuario_o_redirect(request)
    if redirect:
        return redirect
    return _apply(
        request, admin, "servicio_toggle", service_id,
        catalog.toggle_service(service_id, admin=admin),
    )


@router.post("/catalog/services/{service_id}/atencion")
def service_attention(request: Request, service_id: str, atiende: str = Form("")):
    """«Lo atiendo» / «Dejo de atenderlo» — siempre sobre quien tiene la sesión.

    Sin campo de usuario a propósito: si la ruta aceptara un nombre, cualquiera
    podría llenarle la bandeja a un compañero inscribiéndolo en productos ajenos.
    """
    user, redirect = _usuario_o_redirect(request)
    if redirect:
        return redirect
    quiere = atiende == "si"
    return _apply(
        request, user, "atencion", f"{service_id}:{'si' if quiere else 'no'}",
        catalog.fijar_atencion(service_id, user, quiere),
    )
