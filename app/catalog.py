import json
from dataclasses import dataclass
from pathlib import Path

CATALOG_PATH = Path(__file__).parent / "catalog.json"

PAGE_SIZE = 10


@dataclass(frozen=True)
class Service:
    id: str
    name: str
    price_info: str
    category_id: str
    category_name: str


@dataclass(frozen=True)
class Category:
    id: str
    name: str
    services: list[Service]


class Catalog:
    def __init__(self, path: Path = CATALOG_PATH):
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.categories: list[Category] = []
        for cat in raw["categories"]:
            services = [
                Service(
                    id=svc["id"],
                    name=svc["name"],
                    price_info=svc["price_info"],
                    category_id=cat["id"],
                    category_name=cat["name"],
                )
                for svc in cat["services"]
            ]
            self.categories.append(Category(id=cat["id"], name=cat["name"], services=services))

        self._service_by_id = {s.id: s for c in self.categories for s in c.services}
        self._category_by_id = {c.id: c for c in self.categories}

    def category_by_index(self, index: int) -> Category | None:
        if 1 <= index <= len(self.categories):
            return self.categories[index - 1]
        return None

    def category_by_id(self, category_id: str) -> Category | None:
        return self._category_by_id.get(category_id)

    def service_by_id(self, service_id: str) -> Service | None:
        return self._service_by_id.get(service_id)

    def service_by_index(self, category_id: str, index: int) -> Service | None:
        category = self.category_by_id(category_id)
        if category is None or not (1 <= index <= len(category.services)):
            return None
        return category.services[index - 1]

    def search_by_keyword(self, text: str) -> list[Service]:
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
        lines = ["¿Qué tipo de servicio necesitas? Responde con el número:"]
        for i, c in enumerate(self.categories, start=1):
            lines.append(f"{i}. {c.name}")
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
        return "\n".join(lines)


catalog = Catalog()
