"""Reparto del trabajo (fase 3b + cartera): quién se queda un folio recién nacido.

Un mensaje con varios trámites se decide **servicio por servicio**
(`elegir_por_servicio`, decisión del operador, 2026-09-24): cada folio va a
quien atiende ESE trámite, y el mismo asesor se queda varios solo si los
atiende. La escalada, que no trae servicio, usa `elegir`. Para cada folio, en
este orden, sobre un pool que ya sale acotado por producto:

0. **Quién atiende el producto** — el pool es la gente de la oficina que se
   inscribió en /catalog para lo que el cliente pidió (`CatalogAsesor`). Si
   nadie de la oficina lo atiende, el pool es la oficina entera, igual que
   antes: un producto sin inscritos no deja folios sin dueño, y mientras nadie
   se inscriba en nada el pool es el de siempre.
1. **Cartera** — los titulares del cliente (`app/cartera.py`) que están en el
   pool. Uno: ese. Varios (cliente compartido): el que lo atendió último; si
   ninguno lo ha atendido, el de menos carga. La cartera nunca pasa por encima
   de la cobertura: un titular que no atiende el trámite no está en el pool.
2. **Pegajosa** — un cliente que regresa cae con quien ya lo atendió, **si esa
   persona atiende lo que ahora pide**: primero quien llevó ese mismo trámite,
   luego el más reciente de cualquiera. Es la continuidad que los usuarios de
   la competencia reclaman cuando no existe (§2.6 del análisis de brechas):
   volver a explicar el caso desde cero es la queja, no la espera. Pero
   mandarle un trámite a quien no lo lleva no es continuidad, es un pase de
   mano diferido.
3. **Mismo mensaje** — si otro trámite de este mensaje ya quedó con alguien del
   pool, ese: no se parte al cliente sin necesidad.
4. **Reparto** — al asesor del pool con menos folios vivos que todavía quepa
   bajo el tope, y solo en horario.

Cartera, pegajosa y mismo mensaje no exigen horario ni respetan el tope: son
continuidad, y la continuidad con el cliente vale más que el equilibrio de la
carga. «Atendió» es un folio TRABAJADO (`_historial`): uno auto-asignado que
sigue en NUEVO nadie lo abrió, y contarlo pegaría al cliente a alguien que no
lo conoce.

Lo que este módulo NO hace, a propósito:

- **No decide quién atiende qué.** Lo declara cada colaborador en /catalog
  (regla del operador, 2026-09-18) y vive en el catálogo, que es donde está el
  producto; aquí solo se lee. El pool de partida sigue siendo `User.habilitado`
  de la oficina del folio —la misma pregunta que contesta la lista de
  transferencia (`dashboard.py`)—, y la inscripción solo lo estrecha.
- **No cuenta la carga por su cuenta.** Reusa `activity.carga_abierta`, que la
  fase 3a construyó justamente para esto. Un segundo contador con su propio
  criterio de «vivo» es la copia que se desincroniza.
- **No guarda estado propio.** Lee la cartera (su tabla vive en app/cartera.py);
  la pegajosa se deriva de los folios y la rotación, del último folio que
  recibió cada quien. El motivo de cada decisión va a la bitácora del log, no a
  una columna.
- **No escribe al decidir.** Las lecturas se hacen una vez y la cadena de
  reglas (`_decidir`) es una función pura; la bitácora se escribe después, con
  el folio ya creado.

ponytail: el pool son todos los usuarios habilitados, admins incluidos, y no
mira `User.estado` — es autorreportado y no caduca, así que un «ausente» que
nadie limpia desaparecería del reparto para siempre. Si algún día hace falta
repartir solo entre quien está de verdad conectado, la señal ya existe y es
`authlog.active_sessions()` (ventana de 5 minutos).
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func
from sqlmodel import Session, select

from app import cartera, horario
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

    Con un solo producto —como lo pide `elegir_por_servicio`, trámite por
    trámite— es simplemente «quien lo atiende». Con varios (`elegir` con una
    lista, que es un solo folio para todos) gana quien atiende más de los
    pedidos: si alguien los atiende todos, solo esa gente; si nadie los atiende
    todos, los que atienden alguno.

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


@dataclass(frozen=True)
class Decision:
    """A quién le toca un folio (`None` = se queda sin dueño) y por qué. El motivo
    es lo que dice la bitácora del log y lo que una prueba compara."""

    asesor: str | None
    motivo: str


def _historial(session: Session, phone: str) -> list[tuple[str, str]]:
    """`(asesor, service_id)` de los folios TRABAJADOS del cliente, del más
    reciente al más viejo. Es lo que «lo atendió» quiere decir en la pegajosa y
    en el desempate de un cliente compartido.

    Cuenta un folio cerrado —que el trámite pasado haya terminado no borra a
    quien lo llevó— pero NO uno que sigue en NUEVO: ese lo auto-asignó el
    reparto y nadie lo abrió (`provisional`). Contarlo pegaría al cliente a
    alguien que nunca habló con él.
    """
    filas = session.exec(
        select(Ticket.assigned_to, Ticket.service_id)
        .where(
            Ticket.phone == phone,
            Ticket.assigned_to.is_not(None),  # type: ignore[union-attr]
            Ticket.habilitado == True,  # noqa: E712
            Ticket.status != TicketStatus.NUEVO,
        )
        .order_by(Ticket.created_at.desc(), Ticket.id.desc())  # type: ignore[union-attr]
    ).all()
    return [(asesor, servicio) for asesor, servicio in filas if asesor is not None]


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


def _contexto(session: Session, phone: str) -> dict:
    """Todo lo que la cadena lee de la base, leído UNA vez por mensaje."""
    return {
        "titulares": cartera.duenos(session, phone),
        "historial": _historial(session, phone),
        "carga": carga_abierta(session),
        "ultimo": _ultimo_folio_por_asesor(session),
        "abierto": horario.abierto(),
    }


def _mas_reciente(
    historial: list[tuple[str, str]], candidatos: set[str], servicios: set[str] | None = None
) -> str | None:
    for asesor, servicio in historial:
        if asesor in candidatos and (servicios is None or servicio in servicios):
            return asesor
    return None


def _menos_cargado(candidatos: list[str], carga: dict, ultimo: dict[str, int]) -> str:
    return min(candidatos, key=lambda u: (carga.get(u, 0), ultimo.get(u, 0), u))


def _decidir(
    pool: list[str],
    service_ids: Sequence[str],
    *,
    titulares: set[str],
    historial: list[tuple[str, str]],
    en_mensaje: Sequence[str],
    carga: dict,
    ultimo: dict[str, int],
    abierto: bool,
) -> Decision:
    """La cadena del docstring del módulo, sin tocar la base: pura a propósito,
    para que cada regla se pruebe con conjuntos y no con un escenario entero."""
    en_pool = set(pool)
    # 1. Cartera: titulares que atienden el servicio.
    suyos = [u for u in pool if u in titulares]
    if suyos:
        if len(suyos) == 1:
            return Decision(suyos[0], "cartera")
        # Compartido: el que lo atendió último (decisión del operador); si ninguno
        # lo ha atendido, el de menos carga. Sin tope: el cliente es de ellos.
        return Decision(
            _mas_reciente(historial, set(suyos)) or _menos_cargado(suyos, carga, ultimo),
            "cartera",
        )
    # 2. Pegajosa: primero quien llevó este mismo trámite, luego cualquiera.
    # A propósito NO se revisa el tope: un cliente que regresa y cae con alguien
    # nuevo tiene que contar su caso otra vez.
    pegado = (
        _mas_reciente(historial, en_pool, set(service_ids)) if service_ids else None
    ) or _mas_reciente(historial, en_pool)
    if pegado is not None:
        return Decision(pegado, "pegajosa")
    # 3. Mismo mensaje: no partir al cliente si alguien ya elegido lo atiende.
    mismo = next((u for u in en_mensaje if u in en_pool), None)
    if mismo is not None:
        return Decision(mismo, "mismo-mensaje")
    # 4. Reparto, solo en horario: repartir de madrugada llena el panel de folios
    # que nadie va a mirar hasta la mañana y consume el tope con trabajo dormido.
    # La continuidad de arriba no tiene ese problema, y el aviso de horario (fase
    # 3a) ya le dijo al cliente cuándo hay alguien.
    if not abierto:
        return Decision(None, "fuera-de-horario")
    libres = [u for u in pool if carga.get(u, 0) < settings.carga_maxima_asesor]
    if not libres:
        # Todos al tope: el folio se queda sin asignar, que es lo que «sin
        # asignar» significa desde la fase 3b — desbordamiento.
        return Decision(None, "tope")
    return Decision(_menos_cargado(libres, carga, ultimo), "reparto")


def elegir(
    session: Session, phone: str, service_ids: Sequence[str] = ()
) -> tuple[str | None, str]:
    """A quién le toca UN folio. Devuelve `(asesor, motivo)`; `asesor` es `None`
    cuando el folio debe quedarse sin dueño.

    Lo usa la escalada, que no trae servicio (`service_ids` vacío: el pool es la
    oficina). Con varios productos es un solo folio para todos y gana quien más
    cubre (`_por_producto`); el alta ya no llama así: usa `elegir_por_servicio`.
    """
    if not settings.reparto_automatico:
        # Interruptor del Estudio (/agente), apagado por default. Sin él el
        # alta deja el folio sin dueño y todo el panel se comporta como antes
        # de la fase 3b: nadie se entera de que este módulo existe.
        return None, "apagado"

    elegibles = _elegibles(session, _oficina_del_telefono(session, phone))
    if not elegibles:
        return None, "sin-asesores"
    d = _decidir(
        _por_producto(session, elegibles, service_ids),
        service_ids,
        en_mensaje=(),
        **_contexto(session, phone),
    )
    return d.asesor, d.motivo


def elegir_por_servicio(
    session: Session, phone: str, service_ids: Sequence[str]
) -> dict[str, Decision]:
    """A quién le toca el folio de CADA trámite de un mensaje: `{service_id: Decision}`.

    Cada servicio corre la cadena con su propio pool (quien lo atiende), así que
    un mensaje con dos trámites que atienden personas distintas queda con dos
    dueños, y el mismo asesor se queda varios solo si los atiende (regla 3).
    Solo lee: los folios y su bitácora los escribe quien llama.
    """
    ids = list(dict.fromkeys(service_ids))
    if not settings.reparto_automatico:
        return {sid: Decision(None, "apagado") for sid in ids}
    elegibles = _elegibles(session, _oficina_del_telefono(session, phone))
    if not elegibles:
        return {sid: Decision(None, "sin-asesores") for sid in ids}
    contexto = _contexto(session, phone)
    decisiones: dict[str, Decision] = {}
    en_mensaje: list[str] = []
    for sid in ids:
        d = _decidir(
            _por_producto(session, elegibles, [sid]), [sid], en_mensaje=en_mensaje, **contexto
        )
        decisiones[sid] = d
        if d.asesor is not None:
            if d.asesor not in en_mensaje:
                en_mensaje.append(d.asesor)
            # Solo se actualiza `carga`: dentro de este loop el folio todavía no existe (lo
            # escribe quien llama, después del commit), así que no hay un Ticket.id real que
            # anotar en `ultimo`. Sin esto, dos titulares de la misma cartera —o dos candidatos
            # de reparto— se repartirían siempre al mismo dentro de un mensaje con varios
            # servicios, porque la regla de cartera no consulta `en_mensaje`.
            contexto["carga"][d.asesor] = contexto["carga"].get(d.asesor, 0) + 1
    return decisiones


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


def puede_tomar(session: Session, ticket: Ticket, user: str) -> bool:
    """¿Puede `user` quedarse este folio contestándole al cliente?

    Suyo o sin dueño: sí. Trabajado por otro: no. Provisional de otro (de
    reparto, todavía en NUEVO): solo si `user` está en el pool del servicio
    —lo atiende, o nadie de la oficina del folio lo atiende (`_por_producto`)—.
    Sin ese corte, con la asignación por servicio el primero que contestara se
    quedaba el trámite de un compañero que sí lo atiende, y la división se
    deshacía al primer mensaje. Es la «cola abierta» de siempre, acotada a
    quien puede llevar el trámite.

    Lo usan el permiso de escribir (`clients.conversacion_escribible`) y la toma
    al contestar (`inbox._pause_bot`): una sola regla para las dos.
    """
    if ticket.assigned_to in (None, user):
        return True
    if not provisional(ticket):
        return False
    pool = _por_producto(session, _elegibles(session, ticket.office_id), [ticket.service_id])
    return user in pool
