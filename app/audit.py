"""Registro de acciones administrativas en base de datos.

Módulo mínimo y sin dependencias de routers para que cualquier módulo
(users, dashboard, inbox, system, catálogo) pueda auditar sin ciclos de
import. Cada fila se espeja a MSSQL por el listener de app.archive.
"""

from app.db import get_session
from app.models import AdminAudit


def log_admin(admin: str, action: str, target: str | None = None) -> None:
    with get_session() as session:
        session.add(AdminAudit(admin=admin, action=action, target=target))
        session.commit()
