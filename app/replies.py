"""Respuestas guardadas personales de cada colaborador.

Como las respuestas rápidas de WhatsApp Business: cada quien guarda las
suyas y aparecen en el compositor del chat, detrás de las respuestas rápidas
globales del negocio (app/quick_replies.json).

Se administran dentro del CRM (/crm): el diálogo «Mis respuestas» de
static/js/crm.js las lee y escribe por /crm/api/respuestas (app/crm.py). Este
módulo conserva las REGLAS —largo, tope, dueño y oficina de nacimiento— para
que la API no tenga una segunda copia. La vista vieja /replies se retiró; su
ruta solo reenvía al CRM con el diálogo abierto, por los enlaces guardados.
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from sqlmodel import select

from app.auth import alcance
from app.models import SavedReply

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_REPLIES = 30
MAX_TITULO = 100
MAX_TEXTO = 1000


class RespuestasAlTope(ValueError):
    """Ya tiene MAX_REPLIES guardadas: la API lo distingue de un campo vacío."""


def _current_user(request: Request) -> str | None:
    return request.session.get("user")


def personal_replies(session, username: str) -> list[SavedReply]:
    return session.exec(
        select(SavedReply)
        .where(SavedReply.username == username)
        .where(SavedReply.habilitado == True)  # noqa: E712
        .order_by(SavedReply.title)
    ).all()


def crear_respuesta(session, username: str, titulo, texto) -> SavedReply:
    """Guarda una respuesta de `username`. Lanza ValueError con el motivo, en
    español y tal cual lo leerá el asesor; RespuestasAlTope si ya no le caben.

    El largo se RECORTA en vez de rechazarse, como hacía el formulario viejo:
    los campos del diálogo ya traen maxlength y solo una petición fabricada
    llega aquí con más."""
    if not isinstance(titulo, str) or not isinstance(texto, str):
        raise ValueError("Título y texto son obligatorios.")
    titulo, texto = titulo.strip()[:MAX_TITULO], texto.strip()[:MAX_TEXTO]
    if not titulo or not texto:
        raise ValueError("Título y texto son obligatorios.")
    if len(personal_replies(session, username)) >= MAX_REPLIES:
        raise RespuestasAlTope(f"Máximo {MAX_REPLIES} respuestas guardadas — elimina alguna.")
    # Nacen en la oficina de quien las guarda. Se LEEN por usuario (más
    # estrecho que la oficina), pero con el DEFAULT de la columna una
    # respuesta de Puebla quedaba archivada como de Tlaxcala en el espejo.
    respuesta = SavedReply(username=username, title=titulo, body=texto,
                           office_id=alcance(username).office_id)
    session.add(respuesta)
    session.commit()
    session.refresh(respuesta)
    logger.info("Respuesta guardada #%s creada por %s", respuesta.id, username)
    return respuesta


def borrar_respuesta(session, username: str, reply_id: int) -> bool:
    """Baja lógica, solo de las propias. False si no existe, ya estaba dada de
    baja o es de otra persona: la API contesta lo mismo en los tres casos para
    no confirmar que la respuesta de alguien más existe."""
    respuesta = session.get(SavedReply, reply_id)
    if respuesta is None or respuesta.username != username or not respuesta.habilitado:
        return False
    respuesta.habilitado = False
    session.add(respuesta)
    session.commit()
    return True


@router.get("/replies")
def replies_list(request: Request):
    """La vista vive en el CRM: esta ruta solo reenvía y abre «Mis respuestas»."""
    if not _current_user(request):
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/crm?respuestas=1", status_code=303)
