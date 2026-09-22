import hashlib
import hmac
import logging
import re
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


def save_upload(file: UploadFile) -> tuple[str | None, str | None, str | None]:
    """Valida y guarda un archivo subido.

    Devuelve (ruta_web, ruta_disco, None) o (None, None, código_de_error).
    """
    if file.content_type not in ALLOWED_MEDIA:
        return None, None, "tipo"
    ext, max_size = ALLOWED_MEDIA[file.content_type]

    data = file.file.read()
    if len(data) > max_size:
        return None, None, "peso"
    if not _firma_coincide(ext, data):
        return None, None, "tipo"

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
