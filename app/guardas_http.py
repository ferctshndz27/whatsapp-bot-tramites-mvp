"""Dos guardas de entrada para todo el panel, previas a cualquier ruta.

- **Origen cruzado.** Un POST que mueve dinero (`/cobros/nuevo`) se defendía solo con
  `SameSite=Lax` en la cookie. Eso es una defensa del navegador; esta es la del servidor: si el
  navegador declara un `Origin` y no es el del panel, la petición no llega al handler. Sin
  `Origin` (curl, Meta, Twilio, Stripe, formularios del mismo sitio en navegadores viejos) no se
  decide aquí: esos caminos tienen su propia firma o su cookie.
- **Tope del cuerpo.** `request.form()` limita el número de campos, no los bytes. Un cuerpo de
  formulario o JSON legítimo del panel pesa unos KB; los adjuntos van en multipart y tienen su
  tope en `media_store`, por eso el multipart no se mide aquí.

- **Cookie dentro de un marco.** La extensión del CRM muestra el panel dentro de web.whatsapp.com.
  Ahí la cookie `SameSite=Lax` es de terceros: el navegador la descarta y el login vuelve a
  `/login`. Si la petición declara `Sec-Fetch-Dest: iframe`, la cookie de sesión sale
  `SameSite=None; Secure; Partitioned`: queda atada al sitio que enmarca (WhatsApp Web) y no viaja
  a ningún otro. Nadie más puede llegar con `iframe`: `X-Frame-Options` y `frame-ancestors 'none'`
  impiden enmarcar el panel, salvo con la regla que instala la extensión. Fuera de un marco la
  cookie no cambia: sigue `Lax`.

No es autenticación: no sustituye el guard de sesión de cada handler.
"""

from __future__ import annotations

import re
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


_SAMESITE = re.compile(r";\s*samesite=[^;]*", re.IGNORECASE)
_ATRIBUTOS_MARCO = {"secure", "partitioned"}


def cookie_para_marco(valor: str) -> str:
    """Reescribe un `Set-Cookie` de sesión para que lo guarde un navegador dentro de un marco."""
    valor = _SAMESITE.sub("", valor)
    quedan = {a.strip().lower() for a in valor.split(";")[1:]}
    extras = [a for a in ("Secure", "Partitioned") if a.lower() not in quedan]
    return "; ".join([valor, "SameSite=None", *extras])


async def cookie_en_marco(request, call_next):
    """Solo la cookie de sesión, solo si la petición es un marco (ver el docstring del módulo)."""
    respuesta = await call_next(request)
    if request.headers.get("sec-fetch-dest", "").lower() not in ("iframe", "frame"):
        return respuesta
    respuesta.raw_headers = [
        (k, cookie_para_marco(v.decode("latin-1")).encode("latin-1"))
        if k.lower() == b"set-cookie" and v.lower().startswith(b"session=")
        else (k, v)
        for k, v in respuesta.raw_headers
    ]
    return respuesta
