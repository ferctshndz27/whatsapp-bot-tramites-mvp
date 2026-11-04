"""El agente en el WhatsApp Web del colaborador (ext-cse, «chispa»).

La extensión ve que un cliente escribió en el chat abierto, se lo pasa al mismo cerebro del
bot (`conversation.handle_incoming`, con el canal «web») y escribe en el chat lo que éste
conteste. Aquí solo vive la mitad del servidor: recibir el texto y devolver las respuestas.

Lo que esta ruta deliberadamente NO hace:
- No envía nada: no hay API de WhatsApp en este canal (`web_client`). Quien escribe en el chat,
  y cuándo, es la extensión: mínimo 20 s entre mensajes, nunca ráfagas.
- No contesta a clientes de otra oficina (`auth.alcance`): devuelve [] como si nada hubiera que decir.
- No salta ninguna regla del bot: si el colaborador ya tomó el folio, o el agente está apagado
  para ese chat, `handle_incoming` devuelve [] y no se contesta nada.
- Los botones y las listas de WhatsApp no existen aquí: salen como texto numerado, que es lo que
  el bot ya entiende («1», «sí»).
"""

import logging

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse
from sqlmodel import select

from app import auth, conversation
from app.db import get_session
from app.models import PROVIDER_WEB, Client, Conversation, User
from app.twilio_client import _numerar
from app.utils import mask_phone, normalize_phone

logger = logging.getLogger(__name__)
router = APIRouter()

# Un texto de WhatsApp admite 4096; un cliente que pega más no es un chat que el agente deba contestar.
MAX_TEXTO = 2000
MAX_ID = 128


def a_texto(item) -> str:
    """Una respuesta del bot como el texto plano que se escribe en el chat."""
    if isinstance(item, str):
        return item
    if "buttons" in item:
        return _numerar(item["text"], item["buttons"])
    return item["text"]


@router.post("/crm/api/agente/entrante")
def agente_entrante(request: Request, payload: dict = Body(default={})):
    usuario = request.session.get("user")
    if not usuario:
        return JSONResponse({"error": "Sesión requerida"}, status_code=403)
    telefono = normalize_phone(str(payload.get("phone") or ""))
    texto = payload.get("text")
    wa_id = payload.get("wa_id")
    if not telefono or not isinstance(texto, str) or not texto.strip() or len(texto) > MAX_TEXTO:
        return JSONResponse({"error": "Mensaje no válido"}, status_code=400)
    if wa_id is not None and (not isinstance(wa_id, str) or len(wa_id) > MAX_ID):
        return JSONResponse({"error": "Mensaje no válido"}, status_code=400)
    # Antes de abrir la sesión de escritura: `alcance` abre la suya (ver backend-senior, «helpers que abren su sesión»).
    alcance = auth.alcance(usuario)
    with get_session() as session:
        fila = session.exec(select(User).where(User.username == usuario, User.habilitado == True)).first()  # noqa: E712
        if fila is None:
            return JSONResponse({"error": "Sesión requerida"}, status_code=403)
        # Alcance por oficina: si el cliente ya es de otra oficina, el agente de esta persona no lo contesta
        # (respondería con folios y datos que no ve). Uno nuevo nace en la oficina de quien lo atiende.
        existente = session.get(Client, telefono)
        conv = session.get(Conversation, telefono)
        if any(x is not None and not alcance.alcanza(x.office_id) for x in (existente, conv)):
            return {"respuestas": []}
        # El id de WhatsApp Web no es el wamid de Meta: con prefijo no choca con el índice único de
        # message.wa_message_id, y un reintento de la extensión (mismo id) no duplica el mensaje.
        wamid = f"web:{wa_id}" if wa_id else None
        respuestas = conversation.handle_incoming(
            session, telefono, texto.strip(), wa_message_id=wamid,
            provider=PROVIDER_WEB, office_id=fila.office_id,
        )
        if respuestas:
            respuestas = conversation.presentation(session, telefono, respuestas)
    logger.info("Agente web: %s → %d respuesta(s)", mask_phone(telefono), len(respuestas))
    return {"respuestas": [a_texto(r) for r in respuestas]}
