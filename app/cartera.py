"""Cartera de clientes: qué colaboradores consideran suyo a un cliente.

Muchos a muchos sobre `ClienteAsesor` (un cliente puede ser compartido), gemela de
`CatalogAsesor`: quitarse apaga la fila y volver a agregarse la reactiva, así el espejo MSSQL
lo ve todo por after_update.

Lo que la cartera da, y lo que NO da (decisiones del operador, 2026-09-24/25):

- **Preferencia de asignación**: `asignacion` le da el folio a un titular, pero solo si atiende
  el servicio; la cartera nunca pasa por encima de quién atiende qué.
- **Visibilidad**: el titular ve la conversación en el CRM aunque no tenga folio abierto.
- **NO da escritura**: escribirle al cliente sigue siendo de quien tiene el folio
  (`clients.conversacion_escribible` no mira la cartera, a propósito). Con escritura, el titular
  podía contestar sobre el trámite que otro ya tomó — el hueco que AUTHZ-2 había cerrado.
- Un cliente que llega solo por WhatsApp no entra a ninguna cartera: entra quien lo registra.

Quién mueve la cartera: el cliente tiene que estar en el alcance de oficina de quien actúa; un
colaborador solo se agrega o se quita a sí mismo; un admin (global o de oficina) mueve a
cualquier usuario habilitado de la oficina del cliente.

Las funciones reciben la sesión de quien llama y hacen su propio commit. Devuelven un código,
no texto: la ruta lo traduce (mismo criterio que `clients._ERRORES`, auditoría 2026-09, INJ-5).
"""

from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.audit import log_admin
from app.auth import alcance, is_admin
from app.models import Client, ClienteAsesor, User
from app.utils import mask_phone

logger = logging.getLogger(__name__)

# Resultados. Los tres primeros son éxito (o nada que hacer); los demás, rechazos.
AGREGADO, YA_ESTABA, QUITADO, NO_ESTABA = "agregado", "ya-estaba", "quitado", "no-estaba"
NO_EXISTE, AJENO, USUARIO = "no_existe", "ajeno", "usuario"


def duenos(session: Session, phone: str) -> set[str]:
    """Los titulares vigentes del cliente: fila viva y usuario habilitado. Alguien dado de baja
    deja de contar sin que nadie tenga que limpiar su cartera."""
    return set(
        session.exec(
            select(ClienteAsesor.username)
            .join(User, User.username == ClienteAsesor.username)
            .where(ClienteAsesor.phone == phone)
            .where(ClienteAsesor.habilitado == True)  # noqa: E712
            .where(User.habilitado == True)  # noqa: E712
        ).all()
    )


def telefonos_de(username: str):
    """La consulta de los teléfonos de la cartera de alguien, para usarla dentro de un IN en
    SQL (el tablero del CRM y «Mis clientes»): filtrar antes del tope de filas, no después."""
    return (
        select(ClienteAsesor.phone)
        .where(ClienteAsesor.username == username)
        .where(ClienteAsesor.habilitado == True)  # noqa: E712
    )


def clientes_de(session: Session, username: str) -> set[str]:
    return set(session.exec(telefonos_de(username)).all())


def es_mio(session: Session, phone: str, username: str) -> bool:
    return username in duenos(session, phone)


def _validar(
    session: Session, phone: str, username: str, por: str, al_agregar: bool
) -> str | None:
    """El rechazo que toca, o None si `por` puede mover a `username` en la cartera de `phone`."""
    cliente = session.get(Client, phone)
    if cliente is None or not alcance(por).alcanza(cliente.office_id):
        # Mismo corte que la ficha: de otra oficina responde igual que «no existe».
        return NO_EXISTE
    if username != por and not is_admin(por):
        return AJENO
    if al_agregar:
        # Solo al agregar: quitar siempre es seguro, y un titular que cambió de oficina o
        # se dio de baja tiene que poder salir de la cartera.
        usuario = session.exec(select(User).where(User.username == username)).first()
        if usuario is None or not usuario.habilitado or usuario.office_id != cliente.office_id:
            # Un titular de otra oficina recibiría folios que después no puede abrir.
            return USUARIO
    return None


def _filas(session: Session, phone: str, username: str) -> list[ClienteAsesor]:
    return list(
        session.exec(
            select(ClienteAsesor)
            .where(ClienteAsesor.phone == phone)
            .where(ClienteAsesor.username == username)
            .order_by(ClienteAsesor.id.desc())  # type: ignore[union-attr]
        ).all()
    )


def agregar(session: Session, phone: str, username: str, por: str) -> str:
    """Mete a `username` en la cartera de `phone`. Se FIJA el estado, no se alterna: un doble
    clic o un reenvío del formulario no deshace lo que se acaba de hacer."""
    rechazo = _validar(session, phone, username, por, al_agregar=True)
    if rechazo:
        return rechazo
    filas = _filas(session, phone, username)
    if any(f.habilitado for f in filas):
        return YA_ESTABA
    if filas:
        # Se reactiva la más reciente: la historia de quién tuvo al cliente no se pierde.
        filas[0].habilitado = True
        session.add(filas[0])
    else:
        session.add(ClienteAsesor(phone=phone, username=username, created_by=por))
    try:
        session.commit()
    except IntegrityError:
        # Dos envíos simultáneos: ux_clienteasesor_vivo dejó entrar uno. El estado pedido ya está.
        session.rollback()
        return YA_ESTABA
    log_admin(por, "cartera:agregar", f"{phone} · {username}")
    logger.info("%s agregó a %s a la cartera de %s", por, username, mask_phone(phone))
    return AGREGADO


def quitar(session: Session, phone: str, username: str, por: str) -> str:
    rechazo = _validar(session, phone, username, por, al_agregar=False)
    if rechazo:
        return rechazo
    vivas = [f for f in _filas(session, phone, username) if f.habilitado]
    if not vivas:
        return NO_ESTABA
    for f in vivas:
        f.habilitado = False
        session.add(f)
    session.commit()
    log_admin(por, "cartera:quitar", f"{phone} · {username}")
    logger.info("%s quitó a %s de la cartera de %s", por, username, mask_phone(phone))
    return QUITADO
