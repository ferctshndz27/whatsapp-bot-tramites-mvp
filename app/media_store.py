import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
import uuid
from pathlib import Path

from fastapi import UploadFile

from app.config import settings

logger = logging.getLogger(__name__)

MB = 1024 * 1024
ALLOWED_MEDIA = {
    "image/jpeg": (".jpg", 5 * MB),
    "image/png": (".png", 5 * MB),
    "image/webp": (".webp", 5 * MB),
    "application/pdf": (".pdf", 16 * MB),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (".docx", 16 * MB),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (".xlsx", 16 * MB),
}
MEDIA_NAME_RE = re.compile(r"^[a-f0-9]{32}\.(jpg|png|webp|pdf|docx|xlsx)$")

# El Content-Type lo declara el cliente; los primeros bytes no. Un HTML con
# `.pdf` declarado se guardaba con extensión .pdf y nosniff lo contenía, pero
# el archivo entraba al almacén de documentos de clientes igual (auditoría
# 2026-09, INJ-8). docx y xlsx son ZIP: comparten firma.
_FIRMAS = {
    ".jpg": (b"\xff\xd8\xff",),
    ".png": (b"\x89PNG",),
    ".webp": (b"RIFF",),
    ".pdf": (b"%PDF-",),
    ".docx": (b"PK\x03\x04",),
    ".xlsx": (b"PK\x03\x04",),
}


def _firma_coincide(ext: str, data: bytes) -> bool:
    if ext == ".webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return any(data.startswith(firma) for firma in _FIRMAS[ext])


def validar_bytes(content_type: str | None, data: bytes, permitidos: dict = ALLOWED_MEDIA) -> str | None:
    """Código de error («tipo» o «peso») o None si el archivo pasa.

    Separado de `save_upload` para los chats con clientes, que validan igual pero
    NO escriben a disco (app/adjuntos.py): el archivo vive en Meta o en Twilio.
    """
    if content_type not in permitidos:
        return "tipo"
    ext, max_size = permitidos[content_type]
    if len(data) > max_size:
        return "peso"
    if not _firma_coincide(ext, data):
        return "tipo"
    return None


def save_upload(file: UploadFile) -> tuple[str | None, str | None, str | None]:
    """Valida y guarda un archivo subido (chat de equipo).

    Devuelve (ruta_web, ruta_disco, None) o (None, None, código_de_error).
    """
    data = file.file.read()
    error = validar_bytes(file.content_type, data)
    if error:
        return None, None, error
    ext, _ = ALLOWED_MEDIA[file.content_type]

    media_dir = Path(settings.media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}{ext}"
    (media_dir / name).write_bytes(data)
    return f"/media/{name}", str(media_dir / name), None


# --------------------------------------------------------------------------
# URL pública firmada (la necesita Twilio)
# --------------------------------------------------------------------------
# Meta recibe el binario y devuelve un media id; Twilio NO acepta subir el
# archivo: exige una `MediaUrl` de la que él lo descarga. Como MEDIA_DIR guarda
# documentos de clientes (actas, constancias fiscales), publicarlo abierto sería
# una fuga: cualquiera con el nombre del archivo lo leería para siempre.
#
# Solución: URL firmada y de vida corta. Twilio descarga en segundos, así que
# quince minutos sobran y dejan margen para un reintento suyo.
FIRMA_TTL_SEGUNDOS = 15 * 60


def _firmar(name: str, expira: int) -> str:
    return hmac.new(
        settings.session_secret.encode(), f"{name}|{expira}".encode(), hashlib.sha256
    ).hexdigest()


def signed_media_path(name: str, ttl: int = FIRMA_TTL_SEGUNDOS) -> str | None:
    """Ruta pública firmada de un adjunto, o None si no se puede firmar."""
    if not MEDIA_NAME_RE.match(name):
        return None
    if not settings.session_secret:
        # Sin secreto estable no hay firma verificable. Mejor no mandar el
        # archivo que publicarlo sin protección.
        logger.error("No se puede firmar la URL del adjunto: falta SESSION_SECRET")
        return None
    expira = int(time.time()) + ttl
    return f"/media/publico/{name}?exp={expira}&sig={_firmar(name, expira)}"


def verify_media_token(name: str, expira: str, firma: str) -> bool:
    """Valida la firma y la vigencia de una URL pública de adjunto."""
    if not settings.session_secret or not MEDIA_NAME_RE.match(name):
        return False
    try:
        vence = int(expira)
    except (TypeError, ValueError):
        return False
    if vence < time.time():
        return False
    return hmac.compare_digest(_firmar(name, vence), firma or "")


# --------------------------------------------------------------------------
# Almacén efímero EN MEMORIA (salida por Twilio, sin disco)
# --------------------------------------------------------------------------
# Twilio no acepta subir el archivo: exige una MediaUrl pública de la que él lo
# descarga. Los archivos de los chats con clientes ya no se escriben al VPS
# (regla del operador, 2026-09-28), así que el archivo se sirve desde RAM
# durante unos minutos con un token imposible de adivinar
# (inbox.media_efimero). Funciona porque uvicorn corre con UN proceso
# (Dockerfile): con varios workers, el que recibe la descarga de Twilio no
# tendría el archivo.
# ponytail: un reinicio del contenedor entre el envío y la descarga de Twilio
# pierde el archivo (Twilio marca el mensaje como fallido y el asesor reintenta).
# Si algún día hay varios workers, esto pasa a un almacén compartido.
EFIMERO_TTL_SEGUNDOS = 15 * 60
# Tope de la RAM que puede ocupar el almacén entero. Sin él, envíos seguidos de
# 16 MB se acumulan 15 min cada uno en el único proceso (revisión de seguridad,
# 2026-09-29). 64 MB son cuatro documentos al tope a la vez: de sobra para el
# ritmo de un equipo que envía a mano. Lleno, el envío falla y el asesor
# reintenta en unos minutos; es mejor eso que tumbar el bot.
EFIMERO_MAX_BYTES = 64 * 1024 * 1024
_EFIMEROS: dict[str, tuple[float, bytes, str, str | None]] = {}
_EFIMEROS_LOCK = threading.Lock()


def _purgar_efimeros(ahora: float) -> None:
    for token in [t for t, (vence, *_) in _EFIMEROS.items() if vence < ahora]:
        del _EFIMEROS[token]


def publicar_efimero(data: bytes, mime: str, nombre: str | None = None) -> str | None:
    """Guarda el archivo en memoria y devuelve el token de su URL pública, o
    None si el almacén ya llegó a EFIMERO_MAX_BYTES."""
    token = secrets.token_urlsafe(32)
    ahora = time.time()
    with _EFIMEROS_LOCK:
        _purgar_efimeros(ahora)
        ocupado = sum(len(d) for _, d, _, _ in _EFIMEROS.values())
        if ocupado + len(data) > EFIMERO_MAX_BYTES:
            logger.warning("Almacén efímero lleno (%d bytes); adjunto no publicado", ocupado)
            return None
        _EFIMEROS[token] = (ahora + EFIMERO_TTL_SEGUNDOS, data, mime, nombre)
    return token


def tomar_efimero(token: str) -> tuple[bytes, str, str | None] | None:
    """(contenido, mime, nombre) si el token existe y no venció. No se borra al
    leerlo: Twilio puede reintentar la descarga dentro del plazo."""
    with _EFIMEROS_LOCK:
        _purgar_efimeros(time.time())
        entrada = _EFIMEROS.get(token)
    if entrada is None:
        return None
    _, data, mime, nombre = entrada
    return data, mime, nombre
