"""Catálogo de servicios en SQLite (catalogcategory / catalogservice).

Antes vivía en un JSON editable (settings.catalog_path). Ahora la fuente de
verdad es la base de datos: cada cambio del panel queda auditado campo a
campo en catalogaudit (quién, qué, valor anterior y nuevo) y se espeja a
MSSQL como el resto de la data.

Migración sin pérdida: si las tablas están vacías se importa UNA sola vez el
JSON — primero el archivo vivo (en producción /data/catalog.json, con las
modificaciones hechas desde el panel) y si no existe, la semilla del repo.
Después el JSON queda solo como respaldo histórico; ya no se escribe.

Soft-delete: categorías y servicios llevan "habilitado". El bot solo ve lo
habilitado; el panel admin ve todo.
"""

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from sqlmodel import select

from app.config import settings
from app.db import get_session
from app.models import CatalogAudit, CatalogCategory, CatalogService

logger = logging.getLogger(__name__)

SEED_PATH = Path(__file__).parent / "catalog.json"

PAGE_SIZE = 10

# Etiquetas en español para el historial de cambios del panel.
FIELD_LABELS = {
    "name": "nombre",
    "price_info": "precio",
    "synonyms": "sinónimos",
    "requirements": "requisitos",
    "habilitado": "visible",
    "creado": "creación",
}


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


def _bool_label(value: bool) -> str:
    return "sí" if value else "no"


def _import_into(session, data: dict) -> None:
    for ci, cat in enumerate(data.get("categories", [])):
        cat_id = cat.get("id") or slugify(cat["name"])
        session.add(
            CatalogCategory(
                id=cat_id,
                name=cat["name"],
                position=ci,
                habilitado=cat.get("habilitado", True),
            )
        )
        for si, svc in enumerate(cat.get("services", [])):
            session.add(
                CatalogService(
                    id=svc.get("id") or slugify(svc["name"]),
                    category_id=cat_id,
                    name=svc["name"],
                    price_info=svc.get("price_info", ""),
                    synonyms_json=json.dumps(svc.get("synonyms", []), ensure_ascii=False),
                    requirements=svc.get("requirements", ""),
                    position=si,
                    habilitado=svc.get("habilitado", True),
                )
            )


class Catalog:
    def __init__(self, seed_path: str | Path | None = None):
        # seed_path: JSON alternativo para la importación inicial (tests).
        self._seed_path = Path(seed_path) if seed_path else None
        self._categories: list[Category] = []
        self._service_by_id: dict[str, Service] = {}
        self._category_by_id: dict[str, Category] = {}

    # ---- Carga ----

    def _seed_source(self) -> Path | None:
        for candidate in (self._seed_path, Path(settings.catalog_path), SEED_PATH):
            if candidate is not None and candidate.exists():
                return candidate
        return None

    def _refresh(self) -> None:
        # ponytail: recarga completa en cada acceso — son ~60 filas en SQLite
        # local, sub-milisegundo; cachear con número de versión si el catálogo
        # llega a miles de filas.
        with get_session() as session:
            if session.exec(select(CatalogCategory).limit(1)).first() is None:
                source = self._seed_source()
                if source is not None:
                    _import_into(session, json.loads(source.read_text(encoding="utf-8")))
                    session.commit()
                    logger.info("Catálogo importado a SQLite desde %s", source)
            cats = session.exec(
                select(CatalogCategory).order_by(CatalogCategory.position)
            ).all()
            svcs = session.exec(
                select(CatalogService).order_by(CatalogService.position)
            ).all()

        by_cat: dict[str, list[CatalogService]] = {}
        for s in svcs:
            by_cat.setdefault(s.category_id, []).append(s)

        self._categories = []
        for cat in cats:
            if not cat.habilitado:
                continue
            services = [
                Service(
                    id=s.id,
                    name=s.name,
                    price_info=s.price_info,
                    category_id=cat.id,
                    category_name=cat.name,
                    synonyms=tuple(json.loads(s.synonyms_json or "[]")),
                    requirements=s.requirements or "",
                )
                for s in by_cat.get(cat.id, [])
                if s.habilitado
            ]
            self._categories.append(Category(id=cat.id, name=cat.name, services=services))

        self._service_by_id = {s.id: s for c in self._categories for s in c.services}
        self._category_by_id = {c.id: c for c in self._categories}

    def _maybe_reload(self) -> None:  # nombre heredado de la época del JSON
        self._refresh()

    @property
    def categories(self) -> list[Category]:
        self._refresh()
        return self._categories

    # ---- Escritura (panel admin) — cada cambio queda en catalogaudit ----

    @staticmethod
    def _audit(session, entity: str, entity_id: str, field: str,
               old: str | None, new: str | None, admin: str) -> None:
        session.add(
            CatalogAudit(
                admin=admin, entity=entity, entity_id=entity_id,
                field=field, old_value=old, new_value=new,
            )
        )

    def add_category(self, name: str, admin: str = "sistema") -> str | None:
        """Devuelve un mensaje de error, o None si se agregó."""
        self._refresh()
        cat_id = slugify(name)
        with get_session() as session:
            if session.get(CatalogCategory, cat_id):
                return "Ya existe una categoría con ese nombre."
            positions = [c.position for c in session.exec(select(CatalogCategory)).all()]
            session.add(
                CatalogCategory(
                    id=cat_id, name=name.strip(), position=max(positions, default=-1) + 1
                )
            )
            self._audit(session, "categoria", cat_id, "creado", None, name.strip(), admin)
            session.commit()
        self._refresh()
        return None

    def rename_category(self, category_id: str, name: str, admin: str = "sistema") -> str | None:
        self._refresh()
        with get_session() as session:
            cat = session.get(CatalogCategory, category_id)
            if cat is None:
                return "Categoría no encontrada."
            new_name = name.strip()
            if cat.name != new_name:
                self._audit(session, "categoria", category_id, "name", cat.name, new_name, admin)
                cat.name = new_name
                session.add(cat)
                session.commit()
        self._refresh()
        return None

    def toggle_category(self, category_id: str, admin: str = "sistema") -> str | None:
        self._refresh()
        with get_session() as session:
            cat = session.get(CatalogCategory, category_id)
            if cat is None:
                return "Categoría no encontrada."
            self._audit(
                session, "categoria", category_id, "habilitado",
                _bool_label(cat.habilitado), _bool_label(not cat.habilitado), admin,
            )
            cat.habilitado = not cat.habilitado
            session.add(cat)
            session.commit()
        self._refresh()
        return None

    def add_service(
        self, category_id: str, name: str, price_info: str, admin: str = "sistema"
    ) -> str | None:
        self._refresh()
        with get_session() as session:
            if session.get(CatalogCategory, category_id) is None:
                return "Categoría no encontrada."
            svc_id = slugify(name)
            if session.get(CatalogService, svc_id):
                return "Ya existe un servicio con ese nombre."
            positions = [
                s.position
                for s in session.exec(
                    select(CatalogService).where(CatalogService.category_id == category_id)
                ).all()
            ]
            session.add(
                CatalogService(
                    id=svc_id,
                    category_id=category_id,
                    name=name.strip(),
                    price_info=price_info.strip(),
                    position=max(positions, default=-1) + 1,
                )
            )
            self._audit(
                session, "servicio", svc_id, "creado", None,
                f"{name.strip()} — {price_info.strip()}", admin,
            )
            session.commit()
        self._refresh()
        return None

    def update_service(
        self,
        service_id: str,
        name: str,
        price_info: str,
        synonyms: list[str] | None = None,
        requirements: str | None = None,
        admin: str = "sistema",
    ) -> str | None:
        self._refresh()
        with get_session() as session:
            svc = session.get(CatalogService, service_id)
            if svc is None:
                return "Servicio no encontrado."
            if svc.name != name.strip():
                self._audit(session, "servicio", service_id, "name", svc.name, name.strip(), admin)
                svc.name = name.strip()
            if svc.price_info != price_info.strip():
                self._audit(
                    session, "servicio", service_id, "price_info",
                    svc.price_info, price_info.strip(), admin,
                )
                svc.price_info = price_info.strip()
            if synonyms is not None:
                cleaned = [s.strip() for s in synonyms if s.strip()]
                current = json.loads(svc.synonyms_json or "[]")
                if current != cleaned:
                    self._audit(
                        session, "servicio", service_id, "synonyms",
                        ", ".join(current), ", ".join(cleaned), admin,
                    )
                    svc.synonyms_json = json.dumps(cleaned, ensure_ascii=False)
            if requirements is not None and (svc.requirements or "") != requirements.strip():
                self._audit(
                    session, "servicio", service_id, "requirements",
                    svc.requirements or "", requirements.strip(), admin,
                )
                svc.requirements = requirements.strip()
            session.add(svc)
            session.commit()
        self._refresh()
        return None

    def add_synonym(self, service_id: str, synonym: str, admin: str = "sistema") -> str | None:
        cleaned = synonym.strip()
        if not cleaned:
            return "El sinónimo no puede estar vacío."
        self._refresh()
        with get_session() as session:
            svc = session.get(CatalogService, service_id)
            if svc is None:
                return "Servicio no encontrado."
            existing = json.loads(svc.synonyms_json or "[]")
            if cleaned.lower() in (s.lower() for s in existing):
                return "Ese sinónimo ya existe."
            new_list = existing + [cleaned]
            self._audit(
                session, "servicio", service_id, "synonyms",
                ", ".join(existing), ", ".join(new_list), admin,
            )
            svc.synonyms_json = json.dumps(new_list, ensure_ascii=False)
            session.add(svc)
            session.commit()
        self._refresh()
        return None

    def toggle_service(self, service_id: str, admin: str = "sistema") -> str | None:
        self._refresh()
        with get_session() as session:
            svc = session.get(CatalogService, service_id)
            if svc is None:
                return "Servicio no encontrado."
            self._audit(
                session, "servicio", service_id, "habilitado",
                _bool_label(svc.habilitado), _bool_label(not svc.habilitado), admin,
            )
            svc.habilitado = not svc.habilitado
            session.add(svc)
            session.commit()
        self._refresh()
        return None

    # ---- Vistas del panel (incluyen lo deshabilitado) ----

    def admin_view(self) -> list[dict]:
        self._refresh()  # dispara la importación inicial si hiciera falta
        with get_session() as session:
            cats = session.exec(
                select(CatalogCategory).order_by(CatalogCategory.position)
            ).all()
            svcs = session.exec(
                select(CatalogService).order_by(CatalogService.position)
            ).all()
        by_cat: dict[str, list[dict]] = {}
        for s in svcs:
            by_cat.setdefault(s.category_id, []).append(
                {
                    "id": s.id,
                    "name": s.name,
                    "price_info": s.price_info,
                    "synonyms": ", ".join(json.loads(s.synonyms_json or "[]")),
                    "requirements": s.requirements or "",
                    "habilitado": s.habilitado,
                }
            )
        return [
            {
                "id": c.id,
                "name": c.name,
                "habilitado": c.habilitado,
                "services": by_cat.get(c.id, []),
            }
            for c in cats
        ]

    def recent_changes(self, limit: int = 50) -> list[dict]:
        with get_session() as session:
            rows = session.exec(
                select(CatalogAudit)
                .order_by(CatalogAudit.created_at.desc(), CatalogAudit.id.desc())
                .limit(limit)
            ).all()
            names = {c.id: c.name for c in session.exec(select(CatalogCategory)).all()}
            names.update({s.id: s.name for s in session.exec(select(CatalogService)).all()})
        return [
            {
                "created_at": r.created_at,
                "admin": r.admin,
                "kind": "Categoría" if r.entity == "categoria" else "Servicio",
                "target": names.get(r.entity_id, r.entity_id),
                "field": FIELD_LABELS.get(r.field, r.field),
                "old": r.old_value,
                "new": r.new_value,
            }
            for r in rows
        ]

    # ---- Lectura (bot) ----

    def category_by_index(self, index: int) -> Category | None:
        self._refresh()
        if 1 <= index <= len(self._categories):
            return self._categories[index - 1]
        return None

    def category_by_id(self, category_id: str) -> Category | None:
        self._refresh()
        return self._category_by_id.get(category_id)

    def service_by_id(self, service_id: str) -> Service | None:
        self._refresh()
        return self._service_by_id.get(service_id)

    def service_by_index(self, category_id: str, index: int) -> Service | None:
        category = self.category_by_id(category_id)
        if category is None or not (1 <= index <= len(category.services)):
            return None
        return category.services[index - 1]

    def search_by_keyword(self, text: str) -> list[Service]:
        self._refresh()
        normalized = text.strip().lower()
        if len(normalized) < 3:
            return []
        return [
            s
            for c in self._categories
            for s in c.services
            if normalized in s.name.lower()
        ]

    def format_category_menu(self) -> str:
        self._refresh()
        lines = ["¿Qué tipo de servicio necesitas? Responde con el número:"]
        for i, c in enumerate(self._categories, start=1):
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
