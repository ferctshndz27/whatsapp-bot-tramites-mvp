"""Puerto único de mensajería: el resto de la app manda por aquí, no por un
proveedor concreto.

Son DOS cuentas de WhatsApp independientes —una en Meta, otra en Twilio— que
comparten este CRM. No hay failover entre ellas: se responde SIEMPRE por el
número donde entró el mensaje, porque la ventana de 24 h es POR NÚMERO y
contestar por el otro deja al negocio fuera de ventana con un remitente que el
cliente no reconoce.

Esa decisión vive aquí y en ningún otro lado: quien envía dice a QUIÉN, nunca
POR DÓNDE. Por eso dar de alta un proveedor nuevo se resuelve agregando una
rama en `_adaptador()`, y ni el webhook, ni la bandeja, ni cobranza se enteran.

Los dos adaptadores ya están cableados. Sin las credenciales de Twilio en el
entorno el canal queda inerte: `twilio_client.enabled()` es False, su webhook
rechaza toda firma, y como nadie puede crear una Conversation con
provider='twilio', `canal_de()` resuelve siempre a Meta. Lo comprueba
`tests/test_messaging_port.py::test_sin_credenciales_de_twilio_nadie_sale_por_ese_canal`.
"""

import logging

from app import twilio_client, whatsapp_client
from app.db import get_session
from app.models import PROVIDER_META, PROVIDER_TWILIO, Conversation

logger = logging.getLogger(__name__)


def canal_de(phone: str) -> str:
    """Canal por el que hay que contestarle a `phone`.

    Es el del ÚLTIMO mensaje entrante (Conversation.provider). Un cliente sin
    conversación —por ejemplo uno dado de alta a mano desde el panel— cae en
    Meta, que es la cuenta que existía primero.
    """
    with get_session() as session:
        conversacion = session.get(Conversation, phone)
        return conversacion.provider if conversacion else PROVIDER_META


def _adaptador(provider: str):
    """Módulo que sabe hablarle a ese proveedor.

    Se devuelve el MÓDULO, no la función: así la resolución del atributo ocurre
    en cada llamada, que es lo que permite que las pruebas sigan parchando
    `whatsapp_client.send_text` sin conocer este puerto.
    """
    if provider == PROVIDER_TWILIO:
        return twilio_client
    return whatsapp_client


def _resolver(phone: str, provider: str | None):
    return _adaptador(provider or canal_de(phone))


# ponytail: `canal_de` es un SELECT por llave primaria en cada envío. A este
# volumen es ruido; si algún día se mandan miles de mensajes por minuto, el
# canal se pasa explícito desde quien ya tiene la conversación en la mano
# (el parámetro `provider` existe justo para eso).


def send_text(phone: str, body: str, provider: str | None = None) -> str | None:
    return _resolver(phone, provider).send_text(phone, body)


def send_list(
    phone: str, body: str, button: str, rows: list[tuple[str, str]], provider: str | None = None
) -> str | None:
    return _resolver(phone, provider).send_list(phone, body, button, rows)


def send_buttons(
    phone: str,
    body: str,
    buttons: list[tuple[str, str]],
    provider: str | None = None,
    content_sid: str | None = None,
) -> str | None:
    """`content_sid` es la plantilla de la Content API que Twilio necesita para
    mandar botones nativos; Meta los arma solo y lo ignora."""
    return _resolver(phone, provider).send_buttons(phone, body, buttons, content_sid)


def send_media(
    phone: str,
    file_path: str,
    mime_type: str,
    caption: str = "",
    filename: str | None = None,
    provider: str | None = None,
) -> str | None:
    return _resolver(phone, provider).send_media(phone, file_path, mime_type, caption, filename)
