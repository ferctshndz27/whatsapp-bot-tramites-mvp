"""Guía de trámites: base de conocimiento del equipo.

Requisitos y precio de cada servicio del catálogo, en una vista de consulta
rápida para que todos los colaboradores den la misma información. El admin
la alimenta desde el panel Catálogo (campo requisitos).
"""

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from app.templating import templates

from app.auth import is_admin
from app.catalog import catalog

router = APIRouter()


@router.get("/kb")
def knowledge_base(request: Request):
    user = request.session.get("user")
    if not user:
        return RedirectResponse("/login", status_code=303)
    catalog._maybe_reload()
    return templates.TemplateResponse(
        "kb.html", {"request": request, "user": user, "categories": catalog.categories}
    )
