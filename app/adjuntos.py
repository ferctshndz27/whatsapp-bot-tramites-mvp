"""Archivos de los chats con clientes: viven en Meta o en Twilio, nunca en el VPS.

Regla del operador (2026-09-28): los PDF y capturas que se cruzan clientes y
colaboradores no se guardan en el VPS. El VPS guarda solo la REFERENCIA del
archivo en `message` (media_ref, media_mime, media_nombre, media_bytes,
media_vence_at) y lo trae del proveedor cada vez que alguien lo abre en el CRM.
Lo que el operador aceptó a cambio: un archivo vive lo que el proveedor lo
conserve.

Datos del proveedor verificados en su documentación (2026-09-28):

- Meta: el media id que llega en un webhook vence a los 7 DÍAS; el de una
  subida, a los 30. Por eso lo que manda el cliente se RENUEVA: se descarga en
  memoria y se vuelve a subir como archivo del negocio, en segundo plano, y la
  referencia pasa a durar 30 días. Si la renovación falla queda la de 7.
- Twilio: guarda lo recibido hasta que alguien lo borre (sin vencimiento), y
  para enviar exige una URL pública de la que él descarga
  (media_store.publicar_efimero, en RAM).
- Ninguno de los dos acepta WebP como imagen (solo como sticker): aquí no entra.

Lo que este módulo NO hace, a propósito:

- **No valida el contenido del documento.** Recibir un archivo es guardarlo para
  el asesor; revisarlo es del asesor (la línea del producto).
- **No escribe a disco.** Todo pasa por memoria con tope de tamaño. El chat de
  equipo sí sigue en /data/media (media_store.save_upload): no tiene proveedor
  donde vivir.
- **No toca `media_path`.** Es el histórico en disco, que se queda como está.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlmodel import select

from app import messaging
from app.db import get_session
from app.media_store import ALLOWED_MEDIA, validar_bytes
from app.models import PROVIDER_META, Direction, Message, utcnow

logger = logging.getLogger(__name__)

# Lo que se acepta en los chats con clientes: lo de siempre menos WebP.
TIPOS_CLIENTE = {m: v for m, v in ALLOWED_MEDIA.items() if m != "image/webp"}

VIDA_META_WEBHOOK = timedelta(days=7)
VIDA_META_SUBIDA = timedelta(days=30)

# Qué dice el bot. Acusa recibo y NADA más: no opina del archivo.
ACUSE = "Recibí tu archivo 📎. Un asesor lo revisará."
NO_SOPORTADO = (
    "Por ahora solo puedo recibir PDF, fotos o capturas (JPG o PNG), Word o Excel. "
    "¿Me lo mandas en alguno de esos formatos?"
)


def de_meta(message: dict) -> dict | None:
    """El archivo de un mensaje entrante de Meta, o None si no trae imagen ni documento.

    Campos según la referencia de webhooks de Meta: `image` trae caption,
    mime_type, sha256, id y url; `document` además filename.
    """
    tipo = message.get("type")
    if tipo not in ("image", "document"):
        return None
    datos = message.get(tipo) or {}
    if not datos.get("id"):
        return None
    return {
        "ref": datos["id"],
        "mime": (datos.get("mime_type") or "").split(";")[0].strip(),
        "nombre": datos.get("filename"),
        "caption": datos.get("caption") or "",
    }


def soportado(adjunto: dict | None) -> bool:
    return bool(adjunto) and adjunto.get("mime") in TIPOS_CLIENTE


def tope(mime: str | None) -> int:
    return TIPOS_CLIENTE[mime][1] if mime in TIPOS_CLIENTE else 0


def cuerpo(adjunto: dict | None, texto: str) -> str:
    """El `body` del mensaje: el pie si lo trae, si no qué llegó."""
    if texto.strip():
        return texto
    if not adjunto:
        return "[archivo adjunto]"
    nombre = adjunto.get("nombre")
    if not soportado(adjunto):
        return f"[archivo no soportado{': ' + nombre if nombre else ''}]"
    if (adjunto.get("mime") or "").startswith("image/"):
        return "[imagen]"
    return f"[documento{': ' + nombre if nombre else ''}]"


def sellar(m: Message, adjunto: dict | None, provider: str) -> None:
    """Copia la referencia al mensaje. Solo de tipos soportados: el visor nunca
    sirve un tipo que no pasó por la lista."""
    if not soportado(adjunto):
        return
    m.media_ref = adjunto["ref"]
    m.media_mime = adjunto["mime"]
    # 255: el ancho de wa_messages.media_nombre en el espejo. Un nombre más largo
    # haría fallar ese INSERT para siempre (backfill reintenta el mismo valor).
    m.media_nombre = (adjunto.get("nombre") or "")[:255] or None
    m.media_vence_at = utcnow() + VIDA_META_WEBHOOK if provider == PROVIDER_META else None


# --- Renovación de Meta (7 → 30 días) ---------------------------------------

# Un solo hilo: las renovaciones son raras y pesadas (descarga + subida), y
# serializarlas acota la memoria a un archivo a la vez.
_EJECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="renovar-adjunto")


def renovar_en_segundo_plano(wa_message_id: str) -> None:
    """Agenda la renovación sin hacer esperar al webhook: Meta espera un 200 pronto."""
    _EJECUTOR.submit(_renovar_seguro, wa_message_id)


def _renovar_seguro(wa_message_id: str) -> None:
    try:
        renovar_meta(wa_message_id)
    except Exception:  # un hilo de fondo no tiene a quién propagarle nada
        logger.exception("Renovación de adjunto falló (wamid=%s)", wa_message_id)


def renovar_meta(wa_message_id: str) -> bool:
    """Vuelve a subir a Meta el archivo de un mensaje entrante para que dure 30 días.

    Todo en memoria. Se valida el contenido real (tipo y primeros bytes) antes
    de subirlo: la referencia nueva la sirve el visor del CRM. True si renovó.
    """
    with get_session() as session:
        m = session.exec(
            select(Message).where(
                Message.wa_message_id == wa_message_id, Message.direction == Direction.IN
            )
        ).first()
        # media_bytes solo lo llena una renovación: un reintento de Meta del
        # mismo mensaje no vuelve a descargar ni a subir.
        if m is None or m.provider != PROVIDER_META or not m.media_ref or m.media_bytes:
            return False
        ref, mime, nombre = m.media_ref, m.media_mime, m.media_nombre

    traido = messaging.descargar_media(ref, PROVIDER_META, tope(mime))
    if traido is None:
        logger.warning("No se pudo descargar de Meta el adjunto de %s; queda la referencia de 7 días", wa_message_id)
        return False
    data, _ = traido
    if validar_bytes(mime, data, TIPOS_CLIENTE):
        logger.warning("Adjunto de %s no pasó la validación de contenido; no se renueva", wa_message_id)
        return False
    nuevo = messaging.subir_media_meta(data, mime, nombre)
    if nuevo is None:
        return False

    with get_session() as session:
        m = session.get(Message, m.id)
        if m is None:
            return False
        m.media_ref = nuevo
        m.media_bytes = len(data)
        m.media_vence_at = utcnow() + VIDA_META_SUBIDA
        session.add(m)
        session.commit()
    return True


# --- Traer para el visor ----------------------------------------------------


def vencido(m: Message, ahora: datetime | None = None) -> bool:
    if m.media_vence_at is None:
        return False
    vence = m.media_vence_at
    ahora = ahora or utcnow()
    if vence.tzinfo is None:  # SQLite devuelve naive; utcnow() es aware
        ahora = ahora.replace(tzinfo=None)
    return vence <= ahora


# La ficha del folio (/tickets/{id}) lista los archivos que el bot ligó a ese folio. Acotada como
# el resto del panel: un folio sin tope de casos puede juntar muchos.
MAX_ARCHIVOS_FICHA = 50
_TIPOS_LEGIBLES = {
    "application/pdf": "PDF",
    "image/jpeg": "Foto",
    "image/png": "Imagen",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Excel",
}


def tamano_legible(n: int | None) -> str:
    if not n:
        return "—"
    if n < 1024 * 1024:
        return f"{round(n / 1024)} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def del_folio(session, ticket, limite: int = MAX_ARCHIVOS_FICHA) -> tuple[list[dict], int]:
    """(los `limite` archivos más recientes ligados a `ticket`, cuántos hay en total), ya como los
    pinta la ficha. Solo mensajes DEL MISMO teléfono que el folio: el `ticket_id` de una fila ajena
    (un dato dañado) no la mete en la ficha de otro cliente. Solo la referencia: el archivo lo trae
    el visor del CRM, con su propio permiso."""
    condiciones = (
        Message.ticket_id == ticket.id,
        Message.phone == ticket.phone,
        Message.media_ref.is_not(None),  # type: ignore[union-attr]
        Message.habilitado == True,  # noqa: E712
    )
    total = session.exec(select(func.count()).select_from(Message).where(*condiciones)).one()
    filas = session.exec(
        select(Message).where(*condiciones).order_by(Message.id.desc()).limit(limite)  # type: ignore[union-attr]
    ).all()
    return [
        {
            "id": m.id,
            "nombre": m.media_nombre or "Archivo",
            "tipo": _TIPOS_LEGIBLES.get(m.media_mime or "", "Archivo"),
            "tamano": tamano_legible(m.media_bytes),
            "fecha": m.created_at,
            "vencido": vencido(m),
        }
        for m in filas
    ], total


def traer(m: Message) -> tuple[bytes, str] | str:
    """(contenido, mime) del archivo de `m`, o un código: «sin-archivo»,
    «vencido», «no-disponible» (el proveedor no lo dio) o «tipo» (lo que llegó
    no es lo que dice ser)."""
    if not m.media_ref or m.media_mime not in TIPOS_CLIENTE:
        return "sin-archivo"
    if vencido(m):
        return "vencido"
    traido = messaging.descargar_media(m.media_ref, m.provider, tope(m.media_mime))
    if traido is None:
        return "no-disponible"
    data, _ = traido
    # El tipo que se sirve es el GUARDADO, no el que diga el proveedor ahora, y
    # los primeros bytes tienen que cuadrar con él.
    if validar_bytes(m.media_mime, data, TIPOS_CLIENTE):
        return "tipo"
    return data, m.media_mime
