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

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.config import settings
from app.db import get_session
from app.models import CatalogAsesor, CatalogAudit, CatalogCategory, CatalogService, User

logger = logging.getLogger(__name__)

SEED_PATH = Path(__file__).parent / "catalog.json"

# 8 servicios por página: cada página cabe en una lista nativa de WhatsApp
# (8 + "Ver más opciones" + "Volver" = 10 filas, el tope de Meta).
PAGE_SIZE = 8

# Etiquetas en español para el historial de cambios del panel.
FIELD_LABELS = {
    "name": "nombre",
    "price_info": "precio",
    "synonyms": "sinónimos",
    "requirements": "requisitos",
    "habilitado": "visible",
    "creado": "creación",
    "asesores": "quién lo atiende",
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


# ---- Requisitos: del catálogo al folio ----
# Fuente única para el agente (los pide por WhatsApp) y para el panel (el
# asesor ve cuáles llegaron). UNA LÍNEA = UN dato a pedir: quien edita el
# catálogo decide cómo separarlos, sin heurísticas que troceen una frase
# ("acta de nacimiento, original y copia" es un solo requisito).
MAX_REQUISITOS = 5
_VINETA_RE = re.compile(r"^\s*(?:[-*•·–]|\d+[.)])\s*")


def parse_requirements(text: str) -> list[str]:
    """Requisitos del catálogo como lista de datos a pedir (sin viñetas)."""
    items = []
    for linea in (text or "").splitlines():
        item = _VINETA_RE.sub("", linea).strip()
        if item:
            items.append(item)
    return items[:MAX_REQUISITOS]


def requisitos_texto(service) -> str:
    """Requisitos en una línea para mostrar ("INE, CURP" o "no especificados").
    Fuente única del formato: lo usan el corpus de la capa informativa
    (rag._docs) y el tool_result de cotizar_servicio (agent_tools)."""
    items = parse_requirements(service.requirements)
    return ", ".join(items) if items else "no especificados"


def requisitos_iniciales(requirements: str) -> str | None:
    """JSON para congelar en el ticket al darlo de alta; None si el servicio no
    tiene requisitos documentados (entonces el agente no pregunta nada).
    `pedido` se vuelve True cuando el agente emite la pregunta: así el panel
    distingue "sin respuesta" (se preguntó) de "sin preguntar"."""
    items = parse_requirements(requirements)
    if not items:
        return None
    return json.dumps(
        [{"req": i, "valor": None, "pedido": False} for i in items], ensure_ascii=False
    )


def requisitos_del_ticket(requirements_json: str | None) -> list[dict]:
    """Lee el JSON congelado en el folio. Tolera nulo o corrupto (folios viejos,
    escritura a medias): en ese caso no hay requisitos que mostrar."""
    if not requirements_json:
        return []
    try:
        data = json.loads(requirements_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [
        {"req": str(d["req"]), "valor": d.get("valor"), "pedido": bool(d.get("pedido"))}
        for d in data
        if isinstance(d, dict) and d.get("req")
    ]


# Lo que se escribe en el catálogo llega a los prompts del clasificador y del
# agente (nlu.catalog_listing pega los nombres tal cual) y al corpus RAG (los
# requisitos). Auditoría 2026-09, AUTHZ-3 / IA-1: un nombre con «\n» metía
# «Regla prioritaria: …» en el system prompt, y un requisito con «anticipo al
# 55 1234 5678 o en https://pago-tramites.example» se colaba en las respuestas
# informativas saltándose el filtro que SÍ aplica agente_studio a un InfoDoc.
NOMBRE_MAX = 80
PRECIO_MAX = 120
_URL_RE = re.compile(r"https?://|www\.|\b[a-z0-9-]+\.(?:com|mx|net|org|info|io|app|link|me)\b", re.IGNORECASE)


def una_linea(texto: str, maximo: int) -> str:
    """Espacios colapsados (incluidos saltos de línea) y largo acotado: es lo
    único que puede entrar a un prompt como nombre o precio."""
    return " ".join((texto or "").split())[:maximo]


def motivo_requisitos_invalidos(texto: str) -> str | None:
    """La misma regla dura que agente_studio aplica al corpus, aplicada a los
    requisitos (que también entran al corpus): sin precios, sin CURP, sin
    teléfonos, y además sin enlaces. Import perezoso: agente_studio importa
    este módulo al cargar."""
    if not texto or not texto.strip():
        return None
    from app.agente_studio import _motivo_rechazo

    motivo = _motivo_rechazo(texto)
    if motivo:
        return f"Requisitos rechazados: {motivo[0].lower()}{motivo[1:]}"
    if _URL_RE.search(texto):
        return "Requisitos rechazados: no llevan enlaces ni dominios."
    return None


# Sinónimos que ningún humano debería poder aprobar (IA-3): la capa literal
# corre ANTES que cualquier LLM y para TODAS las oficinas, así que «de» como
# sinónimo de acta de nacimiento manda «quiero algo de infonavit» a actas.
SINONIMO_MIN, SINONIMO_MAX, SINONIMO_MAX_PALABRAS = 3, 60, 5
_SINONIMO_CHARS_RE = re.compile(r"^[a-z0-9ñáéíóúü ]+$")
_SINONIMOS_VACIOS = {
    "de", "del", "la", "el", "los", "las", "un", "una", "unos", "unas", "para", "por", "con",
    "sin", "que", "como", "donde", "cuando", "esto", "eso", "algo", "mas", "más", "muy", "hola",
    "buenos dias", "buenas tardes", "buenas noches", "gracias", "ok", "okay", "vale", "si",
    "sí", "no", "ayuda", "info", "informacion", "información", "tramite", "trámite", "tramites",
    "trámites", "servicio", "servicios", "precio", "costo", "quiero", "necesito", "favor",
}

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
        name, price_info = una_linea(name, NOMBRE_MAX), una_linea(price_info, PRECIO_MAX)
        if not name or not price_info:
            return "Nombre y precio son obligatorios."
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
        name, price_info = una_linea(name, NOMBRE_MAX), una_linea(price_info, PRECIO_MAX)
        if not name or not price_info:
            return "Nombre y precio son obligatorios."
        if synonyms is not None:
            for s in synonyms:
                if s.strip():
                    motivo = self.motivo_sinonimo_invalido(s, service_id)
                    if motivo:
                        return motivo
        if requirements is not None:
            motivo = motivo_requisitos_invalidos(requirements)
            if motivo:
                return motivo
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

    def colision_de(self, synonym: str, service_id: str) -> str | None:
        """El nombre del OTRO servicio o categoría con el que chocaría este
        sinónimo (mismo texto, normalizado), o None. Un choque hace ambigua la
        capa literal para todas las oficinas."""
        from app.nlu import _norm

        objetivo = _norm(synonym)
        for c in self.categories:
            if _norm(c.name) == objetivo:
                return f"la categoría «{c.name}»"
            for s in c.services:
                if s.id == service_id:
                    continue
                if _norm(s.name) == objetivo or objetivo in (_norm(x) for x in s.synonyms):
                    return f"«{s.name}»"
        return None

    def motivo_sinonimo_invalido(self, synonym: str, service_id: str) -> str | None:
        """La ÚNICA regla de qué sinónimo puede entrar al catálogo, venga del
        panel, del minero nocturno o de un grupo de no-entendidos. None = válido."""
        from app.nlu import _norm

        limpio = _norm(synonym)
        if limpio in _SINONIMOS_VACIOS:
            return f"«{synonym.strip()}» es una palabra vacía: mandaría cualquier frase a este servicio."
        if len(limpio) < SINONIMO_MIN or len(limpio) > SINONIMO_MAX:
            return f"Un sinónimo tiene entre {SINONIMO_MIN} y {SINONIMO_MAX} caracteres."
        if len(limpio.split()) > SINONIMO_MAX_PALABRAS:
            return f"Un sinónimo tiene como mucho {SINONIMO_MAX_PALABRAS} palabras."
        if not _SINONIMO_CHARS_RE.match(limpio):
            return "Un sinónimo solo lleva letras, números y espacios."
        choque = self.colision_de(synonym, service_id)
        if choque:
            return f"«{synonym.strip()}» ya nombra a {choque}: la capa literal quedaría ambigua."
        return None

    def add_synonym(self, service_id: str, synonym: str, admin: str = "sistema") -> str | None:
        cleaned = synonym.strip()
        if not cleaned:
            return "El sinónimo no puede estar vacío."
        self._refresh()
        motivo = self.motivo_sinonimo_invalido(cleaned, service_id)
        if motivo:
            return motivo
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

    def remove_synonym(self, service_id: str, synonym: str, admin: str = "sistema") -> str | None:
        """Quita un sinónimo aprobado. Espejo exacto de add_synonym (misma
        auditoría y mismo refresco de caché): el catálogo sigue siendo el
        único dueño de sus sinónimos, sin importar qué pantalla los edite."""
        cleaned = synonym.strip()
        self._refresh()
        with get_session() as session:
            svc = session.get(CatalogService, service_id)
            if svc is None:
                return "Servicio no encontrado."
            existing = json.loads(svc.synonyms_json or "[]")
            new_list = [s for s in existing if s.lower() != cleaned.lower()]
            if len(new_list) == len(existing):
                return "Ese sinónimo no está en el servicio."
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

    # ---- Quién atiende qué producto (CatalogAsesor) ----
    #
    # Lo declara cada colaborador por sí mismo en /catalog y lo lee la asignación
    # (asignacion._por_producto) para mandarle los folios de lo que atiende. El
    # catálogo sigue siendo global; lo que es de cada quien es su inscripción.

    def asesores_por_servicio(self, visibles: set[str] | None = None) -> dict[str, list[str]]:
        """{service_id: [usernames]} de las inscripciones vivas de usuarios
        habilitados, restringido a `visibles` si llega. `None` = todos.

        El catálogo no sabe de oficinas ni de sesiones, y no debe aprenderlo aquí:
        es global por decisión (SPEC-MULTI-OFICINA §11.3; los candados SEIS_DIFERIDO
        y SIN_SESION de tests/test_alcance_oficina.py lo vigilan leyendo este
        archivo). A quién puede ver cada quien lo decide el panel
        (catalog_admin._visibles) y llega ya resuelto como `visibles`.
        """
        consulta = (
            select(CatalogAsesor.service_id, CatalogAsesor.username)
            .join(User, User.username == CatalogAsesor.username)
            .where(CatalogAsesor.habilitado == True)  # noqa: E712
            .where(User.habilitado == True)  # noqa: E712
            .order_by(CatalogAsesor.username)
        )
        with get_session() as session:
            filas = session.exec(consulta).all()
        por_servicio: dict[str, list[str]] = {}
        for service_id, username in filas:
            if visibles is None or username in visibles:
                por_servicio.setdefault(service_id, []).append(username)
        return por_servicio

    def fijar_atencion(self, service_id: str, username: str, atiende: bool) -> str | None:
        """Inscribe o da de baja a `username` como quien atiende el servicio.

        Se FIJA el estado y no se alterna: la página manda el que quiere, así un
        doble clic o un reenvío del formulario no deshace lo que se acaba de hacer.
        Dar de baja apaga la fila y volver a inscribirse reactiva la misma (patrón
        TicketLabel), de modo que el espejo lo ve todo por after_update.
        """
        with get_session() as session:
            if session.get(CatalogService, service_id) is None:
                return "Servicio no encontrado."
            filas = session.exec(
                select(CatalogAsesor)
                .where(CatalogAsesor.service_id == service_id)
                .order_by(CatalogAsesor.id.desc())  # type: ignore[union-attr]
            ).all()
            antes = sorted({f.username for f in filas if f.habilitado})
            # La viva primero: es la que hay que apagar al darse de baja. Si no
            # hay viva, la más reciente apagada, que es la que se reactiva.
            propias = [f for f in filas if f.username == username]
            mia = next((f for f in propias if f.habilitado), None) or next(iter(propias), None)
            if atiende == (username in antes):
                return None  # ya estaba así: nada que escribir ni auditar
            if mia is None:
                session.add(CatalogAsesor(service_id=service_id, username=username,
                                          created_by=username))
            else:
                mia.habilitado = atiende
                session.add(mia)
            # Solo el cambio de quien lo hizo, NUNCA la lista entera: la lista
            # mezcla gente de todas las oficinas y el historial se ve desde todas.
            self._audit(session, "servicio", service_id, "asesores",
                        "lo atiende" if not atiende else "no lo atiende",
                        "lo atiende" if atiende else "no lo atiende", username)
            try:
                session.commit()
            except IntegrityError:
                # Dos envíos simultáneos del mismo colaborador: el índice
                # ux_catalogasesor_vivo dejó entrar uno. El estado pedido ya está.
                session.rollback()
        return None

    # ---- Vistas del panel (incluyen lo deshabilitado) ----

    def admin_view(self, visibles: set[str] | None = None) -> list[dict]:
        self._refresh()  # dispara la importación inicial si hiciera falta
        with get_session() as session:
            cats = session.exec(
                select(CatalogCategory).order_by(CatalogCategory.position)
            ).all()
            svcs = session.exec(
                select(CatalogService).order_by(CatalogService.position)
            ).all()
        asesores = self.asesores_por_servicio(visibles)
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
                    "asesores": asesores.get(s.id, []),
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

    def recent_changes(self, limit: int = 50, visibles: set[str] | None = None) -> list[dict]:
        """Lo último que se cambió del catálogo, con quién lo hizo.

        Los cambios al catálogo se ven desde todas las oficinas con su autor: el
        catálogo es de todas y quien le cambia el precio a un trámite se lo
        cambia a todas. Las inscripciones («quién lo atiende») NO: son de la
        gente, y la gente de cada oficina es de su oficina — con `visibles`, solo
        las de quien se puede ver.
        """
        with get_session() as session:
            consulta = select(CatalogAudit).order_by(
                CatalogAudit.created_at.desc(), CatalogAudit.id.desc()
            )
            if visibles is not None:
                consulta = consulta.where(
                    (CatalogAudit.field != "asesores")
                    | CatalogAudit.admin.in_(sorted(visibles))  # type: ignore[union-attr]
                )
            rows = session.exec(consulta.limit(limit)).all()
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
