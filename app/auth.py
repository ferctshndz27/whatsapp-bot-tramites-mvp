"""Quién es el usuario de la sesión y qué alcance tiene.

`is_admin` gatea las secciones de administración. `alcance` responde la otra
pregunta —QUÉ FILAS puede ver— y existe para que esa decisión viva en UN lugar
y no repetida en cada vista (docs/SPEC-MULTI-OFICINA.md §5).

ponytail: los routers resuelven la sesión con su propio `_current_user` en vez
de un `Depends(get_current_user)`. Una auditoría pidió centralizarlo y se
rechazó en esa forma (septiembre de 2026, docs/PLAN-CORRECCION-AUDITORIA.md):
lo que de verdad protegía —que una ruta nueva no naciera pública— ya lo cierra
la enumeración del router en tests/test_authz.py, que cubre las rutas de hoy y
las de mañana sin tocar `app/`. Y la técnica propuesta, `HTTPException(303,
headers={"Location": ...})`, no es la forma idiomática de redirigir en FastAPI.
Reabrir si la duplicación empieza a divergir de verdad —cuerpos distintos, no
copias— y entonces con `RedirectResponse`, no con excepciones.
"""

from typing import NamedTuple

from sqlmodel import select

from app.db import get_session
from app.models import OFICINA_TLAXCALA_ID, User


def is_admin(username: str | None) -> bool:
    if not username:
        return False
    with get_session() as session:
        user = session.exec(select(User).where(User.username == username)).first()
        return bool(user and user.is_admin)


class Alcance(NamedTuple):
    """Qué oficina ve este usuario. `ve_todas` gana sobre `office_id`."""

    office_id: int
    ve_todas: bool

    def filtrar(self, consulta, columna):
        """La consulta acotada a su oficina, o intacta si las ve todas.

        Se pasa la COLUMNA y no el modelo porque varias vistas consultan con
        joins donde `Cobro.office_id` y `Ticket.office_id` conviven: adivinar
        cuál filtrar desde aquí sería adivinar mal la mitad de las veces.
        """
        return consulta if self.ve_todas else consulta.where(columna == self.office_id)


def alcance(username: str | None) -> Alcance:
    """El alcance del usuario en sesión — UN SELECT, la misma fila que is_admin.

    Un admin ve todas las oficinas: §5 lo decidió así al descartar el segundo
    sistema de roles (admin global / admin de oficina), y /oficinas ya asume esa
    consecuencia. Un colaborador ve la suya.

    Sin usuario —o con uno que ya no existe— devuelve Tlaxcala SIN ver todas:
    el caso solo se alcanza con una sesión huérfana, y ahí lo seguro es el
    alcance más estrecho, no el más ancho.
    """
    if not username:
        return Alcance(OFICINA_TLAXCALA_ID, False)
    with get_session() as session:
        user = session.exec(select(User).where(User.username == username)).first()
    if user is None:
        return Alcance(OFICINA_TLAXCALA_ID, False)
    return Alcance(user.office_id, user.is_admin)
