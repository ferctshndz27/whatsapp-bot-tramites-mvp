"""Dos guardas de entrada para todo el panel, previas a cualquier ruta.

- **Origen cruzado.** Un POST que mueve dinero (`/cobros/nuevo`) se defendía solo con
  `SameSite=Lax` en la cookie. Eso es una defensa del navegador; esta es la del servidor: si el
  navegador declara un `Origin` y no es el del panel, la petición no llega al handler. Sin
  `Origin` (curl, Meta, Twilio, Stripe, formularios del mismo sitio en navegadores viejos) no se
  decide aquí: esos caminos tienen su propia firma o su cookie.
- **Tope del cuerpo.** `request.form()` limita el número de campos, no los bytes. Un cuerpo de
  formulario o JSON legítimo del panel pesa unos KB; los adjuntos van en multipart y tienen su
  tope en `media_store`, por eso el multipart no se mide aquí.

No es autenticación: no sustituye el guard de sesión de cada handler.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi.responses import PlainTextResponse

from app.config import settings

METODOS_CON_EFECTO = {"POST", "PUT", "PATCH", "DELETE"}
MAX_CUERPO_BYTES = 2 * 1024 * 1024


def _host(valor: str) -> str:
    return urlsplit(valor).netloc.lower()


def origen_permitido(origin: str | None, host: str | None, base_publica: str = "") -> bool:
    """True si el `Origin` declarado es el propio panel (o no hay `Origin`)."""
    if origin is None:
        return True
    # «null» lo mandan los documentos en sandbox y ciertas redirecciones entre sitios: no es el panel.
    esquema = urlsplit(origin).scheme
    if esquema not in ("http", "https"):
        return False
    propios = {h.lower() for h in (host, _host(base_publica)) if h}
    return _host(origin) in propios


async def guardas_http(request, call_next):
    if request.method in METODOS_CON_EFECTO:
        if not origen_permitido(request.headers.get("origin"), request.headers.get("host"),
                                settings.public_base_url):
            return PlainTextResponse("Origen no permitido.", status_code=403)
        tipo = request.headers.get("content-type", "").lower()
        largo = request.headers.get("content-length", "")
        if (not tipo.startswith("multipart/") and largo.isdigit()
                and int(largo) > MAX_CUERPO_BYTES):
            return PlainTextResponse("Cuerpo demasiado grande.", status_code=413)
    return await call_next(request)
