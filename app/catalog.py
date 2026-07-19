"""Catálogo de servicios: configuración como dato (JSON editable).

El archivo vive en settings.catalog_path (en producción, el volumen
persistente /data/catalog.json — el JSON del repo es solo la semilla del
primer arranque, porque cada deploy pisa el código con git reset).

Recarga en caliente: cada acceso público revisa el mtime del archivo, así
las ediciones del panel admin las ve el bot al instante sin reiniciar.
Escritura atómica (tmp + os.replace) para que una lectura concurrente
nunca vea un JSON a medias.

Soft-delete: categorías y servicios llevan "habilitado" (default true).
El bot solo ve los habilitados; el panel admin ve todos.
"""

import json
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

SEED_PATH = Path(__file__).parent / "catalog.json"

PAGE_SIZE = 10


@dataclass(frozen=True)
class Service:
    id: str
    name: str
    price_info: str
    category_id: str
    category_name: str
    # Sinónimos/frases coloquiales con que los clientes piden el servicio;
    # editables desde el panel Catálogo, usados por la deducción de intención.
    synonyms: tuple[str, ...] = ()
    # Requisitos/documentos del trámite (base de conocimiento del equipo).
    requirements: str = ""


@dataclass(frozen=True)
class Category:
    id: str
    name: str
    services: list[Service]


def slugify(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "item"


class Catalog:
    def __init__(self, path: str | Path | None = None):
        self._path = Path(path) if path else Path(settings.catalog_path)
        self._mtime = 0.0
        self._ensure_seed()
        self._load()

    def _ensure_seed(self) -> None:
        if not self._path.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(SEED_PATH, self._path)

    def _load(self) -> None:
        self.raw = json.loads(self._path.read_text(encoding="utf-8"))
        self._mtime = self._path.stat().st_mtime
        # Vista del bot: solo lo habilitado.
        self.categories = []
        for cat in self.raw["categories"]:
            if not cat.get("habilitado", True):
                continue
            services = [
                Service(
                    id=svc["id"],
                    name=svc["name"],
                    price_info=svc["price_info"],
                    category_id=cat["id"],
                    category_name=cat["name"],
                    synonyms=tuple(svc.get("synonyms", [])),
                    requirements=svc.get("requirements", ""),
                )
                for svc in cat["services"]
                if svc.get("habilitado", True)
            ]
            self.categories.append(Category(id=cat["id"], name=cat["name"], services=services))

        self._service_by_id = {s.id: s for c in self.categories for s in c.services}
        self._category_by_id = {c.id: c for c in self.categories}

    def _maybe_reload(self) -> None:
        try:
            if self._path.stat().st_mtime != self._mtime:
                self._load()
        except OSError:
            pass  # archivo momentáneamente inaccesible: seguir con lo cargado

    # ---- Escritura (panel admin) ----

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(tmp, self._path)
        self._load()

    def _raw_category(self, category_id: str) -> dict | None:
        return next((c for c in self.raw["categories"] if c["id"] == category_id), None)

    def _raw_service(self, service_id: str) -> tuple[dict, dict] | None:
        for cat in self.raw["categories"]:
            for svc in cat["services"]:
                if svc["id"] == service_id:
                    return cat, svc
        return None

    def add_category(self, name: str) -> str | None:
        """Devuelve un mensaje de error, o None si se agregó."""
        self._maybe_reload()
        cat_id = slugify(name)
        if self._raw_category(cat_id):
            return "Ya existe una categoría con ese nombre."
        self.raw["categories"].append(
            {"id": cat_id, "name": name.strip(), "habilitado": True, "services": []}
        )
        self._save()
        return None

    def rename_category(self, category_id: str, name: str) -> str | None:
        self._maybe_reload()
        cat = self._raw_category(category_id)
        if cat is None:
            return "Categoría no encontrada."
        cat["name"] = name.strip()
        self._save()
        return None

    def toggle_category(self, category_id: str) -> str | None:
        self._maybe_reload()
        cat = self._raw_category(category_id)
        if cat is None:
            return "Categoría no encontrada."
        cat["habilitado"] = not cat.get("habilitado", True)
        self._save()
        return None

    def add_service(self, category_id: str, name: str, price_info: str) -> str | None:
        self._maybe_reload()
        cat = self._raw_category(category_id)
        if cat is None:
            return "Categoría no encontrada."
        svc_id = slugify(name)
        if self._raw_service(svc_id):
            return "Ya existe un servicio con ese nombre."
        cat["services"].append(
            {
                "id": svc_id,
                "name": name.strip(),
                "price_info": price_info.strip(),
                "synonyms": [],
                "habilitado": True,
            }
        )
        self._save()
        return None

    def update_service(
        self,
        service_id: str,
        name: str,
        price_info: str,
        synonyms: list[str] | None = None,
        requirements: str | None = None,
    ) -> str | None:
        self._maybe_reload()
        found = self._raw_service(service_id)
        if found is None:
            return "Servicio no encontrado."
        _, svc = found
        svc["name"] = name.strip()
        svc["price_info"] = price_info.strip()
        if synonyms is not None:
            svc["synonyms"] = [s.strip() for s in synonyms if s.strip()]
        if requirements is not None:
            svc["requirements"] = requirements.strip()
        self._save()
        return None

    def add_synonym(self, service_id: str, synonym: str) -> str | None:
        self._maybe_reload()
        found = self._raw_service(service_id)
        if found is None:
            return "Servicio no encontrado."
        _, svc = found
        cleaned = synonym.strip()
        if not cleaned:
            return "El sinónimo no puede estar vacío."
        existing = svc.setdefault("synonyms", [])
        if cleaned.lower() in (s.lower() for s in existing):
            return "Ese sinónimo ya existe."
        existing.append(cleaned)
        self._save()
        return None

    def toggle_service(self, service_id: str) -> str | None:
        self._maybe_reload()
        found = self._raw_service(service_id)
        if found is None:
            return "Servicio no encontrado."
        _, svc = found
        svc["habilitado"] = not svc.get("habilitado", True)
        self._save()
        return None

    # ---- Lectura (bot) ----

    def category_by_index(self, index: int) -> Category | None:
        self._maybe_reload()
        if 1 <= index <= len(self.categories):
            return self.categories[index - 1]
        return None

    def category_by_id(self, category_id: str) -> Category | None:
        self._maybe_reload()
        return self._category_by_id.get(category_id)

    def service_by_id(self, service_id: str) -> Service | None:
        self._maybe_reload()
        return self._service_by_id.get(service_id)

    def service_by_index(self, category_id: str, index: int) -> Service | None:
        category = self.category_by_id(category_id)
        if category is None or not (1 <= index <= len(category.services)):
            return None
        return category.services[index - 1]

    def search_by_keyword(self, text: str) -> list[Service]:
        self._maybe_reload()
        normalized = text.strip().lower()
        if len(normalized) < 3:
            return []
        return [
            s
            for c in self.categories
            for s in c.services
            if normalized in s.name.lower()
        ]

    def format_category_menu(self) -> str:
        self._maybe_reload()
        lines = ["¿Qué tipo de servicio necesitas? Responde con el número:"]
        for i, c in enumerate(self.categories, start=1):
            lines.append(f"{i}. {c.name}")
        lines.append("\nEscribe *menu* en cualquier momento para reiniciar.")
        return "\n".join(lines)

    def format_service_menu(self, category_id: str, page: int = 0) -> str:
        category = self.category_by_id(category_id)
        if category is None:
            return ""
        start = page * PAGE_SIZE
        chunk = category.services[start : start + PAGE_SIZE]
        lines = [f"Servicios de {category.name} — responde con el número:"]
        for offset, s in enumerate(chunk, start=1):
            lines.append(f"{start + offset}. {s.name}")
        if start + PAGE_SIZE < len(category.services):
            lines.append("Escribe 'más' para ver más opciones.")
        lines.append("0. Volver al menú anterior")
        lines.append("\nEscribe *menu* en cualquier momento para reiniciar.")
        return "\n".join(lines)


catalog = Catalog()
