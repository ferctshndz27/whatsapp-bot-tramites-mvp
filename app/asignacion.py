"""Reparto del trabajo (fase 3b): quién se queda un folio recién nacido.

Dos reglas, en este orden, sobre un pool que ya sale acotado por producto:

0. **Quién atiende el producto** — el pool es la gente de la oficina que se
   inscribió en /catalog para lo que el cliente pidió (`CatalogAsesor`). Si
   nadie de la oficina lo atiende, el pool es la oficina entera, igual que
   antes: un producto sin inscritos no deja folios sin dueño, y mientras nadie
   se inscriba en nada este módulo se comporta exactamente como antes.
1. **Pegajosa** — un cliente que regresa cae con el último asesor que ya lo
   atendió, **si esa persona atiende lo que ahora pide**. Es la continuidad que
   los usuarios de la competencia reclaman cuando no existe (§2.6 del análisis
   de brechas): volver a explicar el caso desde cero es la queja, no la espera.
   Pero mandarle un trámite a quien no lo lleva no es continuidad, es un pase
   de mano diferido.
2. **Reparto** — sin historial, al asesor con menos folios vivos que todavía
   quepa bajo el tope.

Lo que este módulo NO hace, a propósito:

- **No decide quién atiende qué.** Lo declara cada colaborador en /catalog
  (regla del operador, 2026-09-18) y vive en el catálogo, que es donde está el
  producto; aquí solo se lee. El pool de partida sigue siendo `User.habilitado`
  de la oficina del folio —la misma pregunta que contesta la lista de
  transferencia (`dashboard.py`)—, y la inscripción solo lo estrecha.
- **No cuenta la carga por su cuenta.** Reusa `activity.carga_abierta`, que la
  fase 3a construyó justamente para esto. Un segundo contador con su propio
  criterio de «vivo» es la copia que se desincroniza.
- **No guarda estado.** No hay columna nueva ni tabla de política: la
  pegajosa se deriva del folio anterior y la rotación, del último folio que
  recibió cada quien. Nada que respaldar en el espejo MSSQL.

ponytail: el pool son todos los usuarios habilitados, admins incluidos, y no
mira `User.estado` — es autorreportado y no caduca, así que un «ausente» que
nadie limpia desaparecería del reparto para siempre. Si algún día hace falta
repartir solo entre quien está de verdad conectado, la señal ya existe y es
`authlog.active_sessions()` (ventana de 5 minutos).
"""

import logging
from collections.abc import Sequence

from sqlalchemy import func
from sqlmodel import Session, select

from app import horario
from app.activity import carga_abierta
from app.config import settings
from app.models import (
    OFICINA_TLAXCALA_ID,
    CatalogAsesor,
    Client,
    Ticket,
    TicketActivity,
    TicketStatus,
    User,
)

logger = logging.getLogger(__name__)

# Quien firma la bitácora cuando el movimiento no lo hizo una persona. Mismo
# actor que ya usa la cobranza automática.
ACTOR = "sistema"


def _elegibles(session: Session, office_id: int | None = None) -> list[str]:
    """Usuarios que pueden recibir trabajo. Mismo criterio que la lista de
    transferencia del panel: dados de alta, sin distinguir admin, y de la
    oficina del folio. Sin ese filtro un folio de Tlaxcala le caía a alguien de
    otra oficina que después no lo podía abrir (el detalle lo rechaza): trabajo
    congelado con dueño. `None` = todas, solo para contar en el Estudio."""
    consulta = select(User.username).where(User.habilitado == True)  # noqa: E712
    if office_id is not None:
        consulta = consulta.where(User.office_id == office_id)
    return list(session.exec(consulta.order_by(User.username)).all())


def _por_producto(
    session: Session, elegibles: list[str], service_ids: Sequence[str]
) -> list[str]:
    """Los elegibles que mejor cubren lo que se pidió; todos si nadie atiende nada.

    «Mejor cubren» porque un mensaje puede pedir varios trámites y el folio de
    todos va a UNA persona (un cliente que pide tres trámites es un cliente, no
    tres — ver conversation). Gana quien atiende más de los productos pedidos:
    si alguien los atiende todos, solo esa gente; si nadie los atiende todos, los
    que atienden alguno. Con un solo producto es simplemente «quien lo atiende».

    Consulta con la sesión que recibe y no con una propia: esto corre dentro de
    la conversación, que puede traer escrituras sin confirmar, y una sesión
    anidada con el engine de la suite comparte la conexión y las deshace en su
    close() (lo documenta cobranza.aplicar_evento).
    """
    if not service_ids or not elegibles:
        return elegibles
    cobertura = dict(
        session.exec(
            select(CatalogAsesor.username, func.count())
            .where(CatalogAsesor.service_id.in_(list(service_ids)))  # type: ignore[union-attr]
            .where(CatalogAsesor.habilitado == True)  # noqa: E712
            .group_by(CatalogAsesor.username)
        ).all()
    )
    mejor = max((cobertura.get(u, 0) for u in elegibles), default=0)
    if mejor == 0:
        # Nadie de ESTA oficina atiende lo pedido: el pool de siempre. Que en
        # otra oficina alguien lo atienda no cuenta — el folio es de aquí.
        return elegibles
    return [u for u in elegibles if cobertura.get(u, 0) == mejor]


def _oficina_del_telefono(session: Session, phone: str) -> int:
    """La oficina en la que nace el folio: la del cliente, igual que la toma
    conversation al crear el Ticket (`office_id=client.office_id`)."""
    client = session.get(Client, phone)
    return client.office_id if client is not None else OFICINA_TLAXCALA_ID


def _pegajoso(session: Session, phone: str, elegibles: set[str]) -> str | None:
    """El último asesor que atendió a este teléfono, si sigue dado de alta.

    Mira el folio anterior más reciente CON dueño, sin importar si está
    cerrado: que el trámite pasado haya terminado no borra a quien lo llevó.
    Si esa persona ya no está habilitada, no se busca al penúltimo — el
    cliente cae al reparto como cualquier otro.
    """
    dueno = session.exec(
        select(Ticket.assigned_to)
        .where(
            Ticket.phone == phone,
            Ticket.assigned_to.is_not(None),  # type: ignore[union-attr]
            Ticket.habilitado == True,  # noqa: E712
        )
        .order_by(Ticket.created_at.desc())
        .limit(1)
    ).first()
    return dueno if dueno in elegibles else None


def _ultimo_folio_por_asesor(session: Session) -> dict[str, int]:
    """Folio más alto que recibió cada quien. Sirve de desempate: entre dos
    asesores con la misma carga gana el que lleva más tiempo sin recibir. Sin
    esto el desempate sería alfabético y «ana» cargaría con todo siempre."""
    filas = session.exec(
        select(Ticket.assigned_to, func.max(Ticket.id))
        .where(Ticket.assigned_to.is_not(None))  # type: ignore[union-attr]
        .group_by(Ticket.assigned_to)
    ).all()
    return {asesor: ultimo for asesor, ultimo in filas if asesor is not None}


def _reparto(session: Session, elegibles: list[str]) -> str | None:
    """El asesor con menos folios vivos que quepa bajo el tope. `None` si
    todos están al tope: el folio se queda sin asignar, que es lo que «sin
    asignar» pasa a significar a partir de esta fase — desbordamiento."""
    carga = carga_abierta(session)
    ultimo = _ultimo_folio_por_asesor(session)
    libres = [u for u in elegibles if carga.get(u, 0) < settings.carga_maxima_asesor]
    if not libres:
        return None
    return min(libres, key=lambda u: (carga.get(u, 0), ultimo.get(u, 0), u))


def elegir(
    session: Session, phone: str, service_ids: Sequence[str] = ()
) -> tuple[str | None, str]:
    """A quién le toca este folio. Devuelve `(asesor, motivo)`; `asesor` es
    `None` cuando el folio debe quedarse sin dueño.

    `service_ids` son los productos del mensaje (vacío en la escalada, que no
    trae ninguno): estrechan el pool a quien los atiende ANTES de la pegajosa y
    del reparto, que no cambian — reciben la lista ya acotada. Por eso la
    pegajosa respeta el producto sin una línea propia: el asesor anterior solo
    gana si sigue en el pool.

    **El reparto solo corre en horario de atención; la pegajosa siempre.**
    Repartir de madrugada llena el panel de cada asesor con folios que nadie
    va a mirar hasta la mañana y consume el tope con trabajo dormido. La
    pegajosa no tiene ese problema: mandarle a un cliente que regresa el mismo
    asesor de siempre es correcto a cualquier hora, y el aviso de horario que
    la fase 3a agregó ya le dijo al cliente cuándo hay alguien.
    """
    if not settings.reparto_automatico:
        # Interruptor del Estudio (/agente), apagado por default. Sin él el
        # alta deja el folio sin dueño y todo el panel se comporta como antes
        # de la fase 3b: nadie se entera de que este módulo existe.
        return None, "apagado"

    elegibles = _elegibles(session, _oficina_del_telefono(session, phone))
    if not elegibles:
        return None, "sin-asesores"
    elegibles = _por_producto(session, elegibles, service_ids)

    pegado = _pegajoso(session, phone, set(elegibles))
    if pegado is not None:
        # A propósito NO se revisa el tope: la continuidad con el cliente vale
        # más que el equilibrio de la carga. Un cliente que regresa y cae con
        # alguien nuevo tiene que contar su caso otra vez.
        return pegado, "pegajosa"

    if not horario.abierto():
        return None, "fuera-de-horario"

    elegido = _reparto(session, elegibles)
    return elegido, "reparto" if elegido is not None else "tope"


def bitacora(session: Session, ticket: Ticket, motivo: str) -> None:
    """Deja constancia de que el folio se asignó solo, y por qué.

    Sin esta fila el histórico muestra un dueño que apareció de la nada. Se
    llama DESPUÉS del commit del folio, porque necesita su `id`.
    """
    if ticket.assigned_to is None or ticket.id is None:
        return
    session.add(
        TicketActivity(
            ticket_id=ticket.id,
            username=ACTOR,
            action=f"asignado-auto:{ticket.assigned_to}",
        )
    )
    logger.info(
        "Folio #%s asignado automáticamente a %s (%s)", ticket.id, ticket.assigned_to, motivo
    )


def provisional(ticket: Ticket) -> bool:
    """¿El dueño de este folio es de reparto y no ganado?

    El `status` ya lo contesta: un folio auto-asignado nace en `NUEVO` **con
    dueño** —`TOMADO` significaría «alguien lo tiene», que es falso para un
    folio que nadie ha mirado, y corrompería el embudo—, y pasa a `TOMADO` en
    cuanto una persona lo reclama o le contesta al cliente. Así que mientras
    siga en `NUEVO` el dueño es una sugerencia del sistema y cualquier asesor
    puede quedárselo contestando; después ya es de quien lo trabajó.

    Esto es lo que mantiene viva la regla «quien responde se queda el chat»
    (`inbox._pause_bot`): sin ella, con folios pre-asignados la guarda
    `assigned_to is None` no se cumpliría nunca y el trabajo de quien contesta
    se le acreditaría al dueño de reparto en todos los reportes.
    """
    return ticket.status == TicketStatus.NUEVO
