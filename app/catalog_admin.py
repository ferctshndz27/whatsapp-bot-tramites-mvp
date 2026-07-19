"""Edición del catálogo de servicios desde el panel — solo administradores.

Sin esto, cambiar un precio requiere commit + push + deploy. Todas las
mutaciones pasan por la API de app.catalog (escritura atómica + recarga
en caliente: el bot ve el cambio en el siguiente mensaje) y quedan en la
bitácora de auditoría.
"""

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.audit import log_admin
from app.auth import is_admin
from app.catalog import catalog

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.globals["is_admin"] = is_admin


def _admin_or_redirect(request: Request):
    user = request.session.get("user")
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    if not is_admin(user):
        return None, RedirectResponse("/", status_code=303)
    return user, None


def _render(request: Request, admin: str, error: str | None = None):
    catalog._maybe_reload()
    return templates.TemplateResponse(
        "catalog.html",
        {"request": request, "user": admin, "raw": catalog.raw, "error": error},
    )


def _apply(request: Request, admin: str, action: str, target: str, error: str | None):
    """Auditar si la mutación fue exitosa; re-renderizar con el error si no."""
    if error:
        return _render(request, admin, error=error)
    log_admin(admin, f"catalogo:{action}", target)
    logger.info("Catálogo: %s %s por %s", action, target, admin)
    return RedirectResponse("/catalog", status_code=303)


@router.get("/catalog")
def catalog_view(request: Request):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    return _render(request, admin)


@router.post("/catalog/categories")
def category_create(request: Request, name: str = Form(...)):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    if not name.strip():
        return _render(request, admin, error="El nombre no puede estar vacío.")
    return _apply(request, admin, "categoria_nueva", name.strip(), catalog.add_category(name))


@router.post("/catalog/categories/{category_id}/rename")
def category_rename(request: Request, category_id: str, name: str = Form(...)):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    if not name.strip():
        return _render(request, admin, error="El nombre no puede estar vacío.")
    return _apply(
        request, admin, "categoria_renombrada", category_id,
        catalog.rename_category(category_id, name),
    )


@router.post("/catalog/categories/{category_id}/toggle")
def category_toggle(request: Request, category_id: str):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    return _apply(
        request, admin, "categoria_toggle", category_id, catalog.toggle_category(category_id)
    )


@router.post("/catalog/services")
def service_create(
    request: Request,
    category_id: str = Form(...),
    name: str = Form(...),
    price_info: str = Form(...),
):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    if not name.strip() or not price_info.strip():
        return _render(request, admin, error="Nombre y precio son obligatorios.")
    return _apply(
        request, admin, "servicio_nuevo", name.strip(),
        catalog.add_service(category_id, name, price_info),
    )


@router.post("/catalog/services/{service_id}/update")
def service_update(
    request: Request,
    service_id: str,
    name: str = Form(...),
    price_info: str = Form(...),
    synonyms: str = Form(""),
):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    if not name.strip() or not price_info.strip():
        return _render(request, admin, error="Nombre y precio son obligatorios.")
    synonym_list = [s for s in (part.strip() for part in synonyms.split(",")) if s]
    return _apply(
        request, admin, "servicio_editado", service_id,
        catalog.update_service(service_id, name, price_info, synonym_list),
    )


@router.post("/catalog/services/{service_id}/toggle")
def service_toggle(request: Request, service_id: str):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    return _apply(
        request, admin, "servicio_toggle", service_id, catalog.toggle_service(service_id)
    )
