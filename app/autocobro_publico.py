"""Páginas públicas del autocobro (las abre el CLIENTE, sin sesión).

F2: solo los términos. La liga con token (`/autocobro/{token}`) llega en F3.
"""

from fastapi import APIRouter, HTTPException, Request

from app import autocobro_terminos as terminos
from app.templating import templates

router = APIRouter()


def _pagina(request: Request, version: int):
    if version not in terminos.VERSIONES:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request, "legal_autocobro.html",
        {
            "request": request,
            "version": version,
            "vigente": version == terminos.VIGENTE,
            "huella": terminos.hash_texto(version),
            "secciones": terminos.secciones(version),
        },
    )


@router.get("/legal/autocobro")
def terminos_vigentes(request: Request):
    return _pagina(request, terminos.VIGENTE)


@router.get("/legal/autocobro/v{version}")
def terminos_por_version(request: Request, version: int):
    return _pagina(request, version)
