import re
import uuid
from pathlib import Path

from fastapi import UploadFile

from app.config import settings

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

    media_dir = Path(settings.media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}{ext}"
    (media_dir / name).write_bytes(data)
    return f"/media/{name}", str(media_dir / name), None
