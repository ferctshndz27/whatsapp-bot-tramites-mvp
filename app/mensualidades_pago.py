"""Registrar y deshacer el pago de una mensualidad (F5).

REGLAS: pagar lo puede hacer cualquiera con la cuenta en su cartera (responsable o colaborador) o el
admin de la oficina; deshacer solo el responsable o el admin (corrige un error, no es rutina). El
pago es condicional en SQL (`pagado_at IS NULL`): si dos asesores pagan a la vez, el segundo recibe
«ya-pagada» y no pisa a quien pagó primero. Datos sintéticos en pruebas: nada real.
"""

from datetime import datetime

from sqlalchemy import update
from sqlmodel import Session, select

from app.models import Mensualidad, MensualidadAsesor, utcnow

MENSAJES_PAGO = {
    "ya-pagada": "Esa mensualidad ya estaba pagada; no se cambió.",
    "no-pagada": "Esa mensualidad no está pagada.",
    "permiso-deshacer": "Solo el responsable de la cuenta o un administrador puede deshacer un pago.",
}


class PagoRechazado(Exception):
    def __init__(self, codigo: str):
        super().__init__(codigo)
        self.codigo = codigo


def es_responsable(session: Session, cuenta_id: int, username: str) -> bool:
    return (
        session.exec(
            select(MensualidadAsesor.id)
            .where(MensualidadAsesor.cuenta_id == cuenta_id)
            .where(MensualidadAsesor.username == username)
            .where(MensualidadAsesor.rol == "responsable")
            .where(MensualidadAsesor.habilitado == True)  # noqa: E712
        ).first()
        is not None
    )


def _espejar(session: Session, mensualidad_id: int) -> None:
    """El UPDATE condicional no pasa por los listeners del ORM: se espeja a mano, tras el commit."""
    from app import archive

    m = session.get(Mensualidad, mensualidad_id)
    if m is not None:
        session.refresh(m)
        archive._safe(archive.archive_mensualidad, m)


def registrar_pago(session: Session, mensualidad_id: int, por: str, ahora: datetime | None = None) -> None:
    """Marca pagada la mensualidad. Levanta PagoRechazado('ya-pagada') si otro se adelantó."""
    r = session.exec(
        update(Mensualidad)
        .where(Mensualidad.id == mensualidad_id)
        .where(Mensualidad.pagado_at == None)  # noqa: E711
        .where(Mensualidad.habilitado == True)  # noqa: E712
        .values(pagado_at=ahora or utcnow(), pagado_por=por)
    )
    if r.rowcount != 1:
        raise PagoRechazado("ya-pagada")
    session.commit()
    _espejar(session, mensualidad_id)


def deshacer_pago(session: Session, mensualidad_id: int) -> None:
    r = session.exec(
        update(Mensualidad)
        .where(Mensualidad.id == mensualidad_id)
        .where(Mensualidad.pagado_at != None)  # noqa: E711
        .where(Mensualidad.habilitado == True)  # noqa: E712
        .values(pagado_at=None, pagado_por=None)
    )
    if r.rowcount != 1:
        raise PagoRechazado("no-pagada")
    session.commit()
    _espejar(session, mensualidad_id)
