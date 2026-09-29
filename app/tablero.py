"""El tablero PERSONAL del CRM (/crm): las columnas de cada quien y dónde tiene a cada cliente.

Decisión del operador (2026-09-29): cada usuario —admins incluidos, como «Mis respuestas»— crea,
renombra, recolorea, reordena y da de baja SUS columnas (`CrmColumna`) y acomoda ahí a sus
clientes (`CrmUbicacion`) sin tocar el tablero de nadie. Antes era un solo embudo global
(`CrmEtapa`) que administraba el admin global. Se descartó abrir ese embudo a todos porque la baja
de una columna, hecha por cualquiera, movía a los clientes de TODAS las oficinas.

Tres reglas sostienen que nadie vea a sus clientes «brincar»:

- **El primer día es igual al último del embudo global.** El tablero nace la primera vez que su
  dueño lo abre, como copia de las etapas globales vivas (`origen_id`). Un cliente que ese usuario
  todavía no ha movido cae en la copia de su `Client.etapa_crm_id`, o en la de la primera etapa
  global si no tiene o la suya ya no existe: la misma regla de antes. Ese campo y `CrmEtapa`
  quedan congelados; nadie los escribe ya.
- **Reordenar no mueve a nadie.** Por eso un cliente sin ubicación no cae en «la primera columna»
  (que cambia al arrastrar) sino en la copia de su etapa global.
- **La baja de una columna es UNA escritura:** `habilitado=0` y `destino_id` = la primera que
  queda. La resolución sigue la cadena de destinos, así que sus clientes se quedan donde se les
  dijo que iban aunque después se reordene o se dé de baja también el destino. Reescribir una
  ubicación por cliente costaba una copia por cliente en el espejo, y dejaba fuera a los que
  venían del embudo global sin ubicación propia.

Lo que este módulo NO decide: quién ve qué cliente. Eso es de app/crm.py, por oficina, folio y
cartera. Aquí solo se contesta «en qué columna de MI tablero está», y una columna de otro no
existe para nadie más: `columna_propia` responde None igual que para un id inventado.

Las funciones reciben la sesión de quien llama y hacen su propio commit, así que esa sesión
tiene que llegar SIN cambios pendientes: la siembra y `mover` resuelven un choque de índice con
un rollback, que se llevaría también lo que el llamador no hubiera confirmado (`ubicar` es la
excepción: prepara y deja el commit a quien llama). Los rechazos son ValueError con el motivo en
español, tal como lo leerá el asesor (el patrón de app/replies.py). Los que la API contesta con
409 tienen su propia subclase.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.models import (
    ETAPAS_CRM_DEFAULT,
    Client,
    CrmColumna,
    CrmEtapa,
    CrmUbicacion,
    utcnow,
)

logger = logging.getLogger(__name__)

# Tope de columnas VIVAS por persona: es de uso, no de almacenamiento. Veinte columnas de 300 px
# ya son un tablero de seis metros; nadie organiza su trabajo así. Dos altas a la vez desde dos
# pestañas pueden pasarlo por una o dos columnas, y se acepta: el daño es de quien lo hace.
MAX_COLUMNAS = 20
# Tope de FILAS del tablero, vivas y de baja. Las bajas no se purgan —el espejo no tiene DELETE—,
# así que sin él crear y dar de baja en bucle crecía la tabla, el espejo y cada carga del tablero
# sin límite (revisión de seguridad, 2026-09-29). Quinientas son diez años creando una columna
# por semana: el tope está para el script, no para una persona.
MAX_FILAS = 500
# El INTEGER de SQLite: un id de ruta más grande no puede ser de nadie, y consultarlo revienta.
_MAX_ID = 2**63 - 1


class ColumnasAlTope(ValueError):
    """Ya tiene MAX_COLUMNAS vivas: la API lo contesta con 409, no con 400."""


class NombreRepetido(ValueError):
    """Otra columna viva de la misma persona ya se llama así: 409."""


def clave_nombre(nombre: str) -> str:
    """«Por  Cobrar» y «por cobrar» son el mismo nombre para quien lee el tablero."""
    return " ".join(nombre.split()).casefold()


@dataclass
class Tablero:
    """El tablero de una persona, cargado una vez por petición."""

    usuario: str
    activas: list[CrmColumna]          # vivas, por (ord, id): el orden de la pantalla
    todas: dict[int, CrmColumna]       # incluidas las de baja: su destino_id sigue sirviendo
    por_origen: dict[int, int]         # etapa global → la columna que la copió
    ubicaciones: dict[str, int]        # teléfono → columna donde esta persona lo dejó
    etapas_globales: list[int]         # vivas por ord: la primera recibe a quien no tiene etapa
    # Columna de baja → la viva en que termina su cadena (None = la cadena se corta). Se llena
    # al resolver: cada tramo se recorre una vez por carga, no una vez por cliente.
    _destinos: dict[int, int | None] = field(default_factory=dict, repr=False, compare=False)

    @property
    def ids(self) -> list[int]:
        return [c.id for c in self.activas]

    def viva(self, columna_id: int | None) -> int | None:
        """La columna viva a la que lleva `columna_id` siguiendo la cadena de destinos.

        Sin punto de partida, o si la cadena se corta, la primera del tablero. `dar_de_baja`
        cuida que la cadena no se cierre ni apunte a una muerta, aun con dos bajas a la vez; el
        tope del camino es un cinturón por si una base restaurada a mano trajera un ciclo.
        """
        vivas = set(self.ids)
        camino: list[int] = []
        actual = columna_id
        while actual is not None and actual not in vivas:
            if actual in self._destinos:  # otro cliente ya recorrió este tramo
                actual = self._destinos[actual]
                break
            if len(camino) > len(self.todas):  # ciclo
                actual = None
                break
            camino.append(actual)
            columna = self.todas.get(actual)
            actual = columna.destino_id if columna is not None else None
        for paso in camino:
            self._destinos[paso] = actual
        if actual in vivas:
            return actual
        return self.ids[0] if self.ids else None

    def columna_de(self, cliente: Client) -> int | None:
        """En qué columna de este tablero está el cliente.

        Su ubicación, si esta persona lo movió alguna vez. Si no, la copia de su etapa global
        (la de `Client.etapa_crm_id` si sigue viva; si no, la primera): el cliente se ve donde
        se veía antes del tablero personal, y reordenar no lo mueve.
        """
        inicio = self.ubicaciones.get(cliente.phone)
        if inicio is None:
            etapa = cliente.etapa_crm_id
            if etapa not in self.etapas_globales:
                etapa = self.etapas_globales[0] if self.etapas_globales else None
            inicio = self.por_origen.get(etapa) if etapa is not None else None
        return self.viva(inicio)


def _etapas_globales(session) -> list[CrmEtapa]:
    return session.exec(
        select(CrmEtapa)
        .where(CrmEtapa.habilitado == True)  # noqa: E712
        .order_by(CrmEtapa.ord, CrmEtapa.id)
    ).all()


def _sembrar(session, usuario: str) -> None:
    """Primera vez que `usuario` abre el tablero: una copia de las etapas globales vivas.

    Si no quedara ninguna viva —hoy no se puede: nadie las edita ya y la última nunca se dio de
    baja—, las cuatro del plan, sin origen. El índice ux_crmcolumna_origen (db.init_db) hace que
    dos primeras cargas simultáneas (dos pestañas, el tablero y el chat inicial) no le dejen el
    tablero doble: la segunda choca, se descarta y lee lo que sembró la primera.
    """
    base = [(e.id, e.nombre, e.color) for e in _etapas_globales(session)]
    if not base:
        base = [(None, nombre, color) for nombre, color in ETAPAS_CRM_DEFAULT]
    for orden, (origen, nombre, color) in enumerate(base):
        session.add(CrmColumna(username=usuario, nombre=nombre, color=color, ord=orden, origen_id=origen))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        logger.info("El tablero de %s ya lo había sembrado otra petición", usuario)


def _columnas(session, usuario: str) -> list[CrmColumna]:
    return session.exec(select(CrmColumna).where(CrmColumna.username == usuario)).all()


def cargar(session, usuario: str) -> Tablero:
    """El tablero de `usuario`, sembrado si es la primera vez que lo abre.

    Se siembra solo si no tiene NINGUNA fila, ni siquiera de baja: quien dio de baja una columna
    no la quiere de vuelta (el mismo criterio que db._seed_crm_etapas).
    """
    todas = _columnas(session, usuario)
    if not todas:
        _sembrar(session, usuario)
        todas = _columnas(session, usuario)
    ubicaciones = session.exec(
        select(CrmUbicacion.phone, CrmUbicacion.columna_id).where(CrmUbicacion.username == usuario)
    ).all()
    return Tablero(
        usuario=usuario,
        activas=sorted((c for c in todas if c.habilitado), key=lambda c: (c.ord, c.id)),
        todas={c.id: c for c in todas},
        por_origen={c.origen_id: c.id for c in todas if c.origen_id is not None},
        ubicaciones={phone: columna_id for phone, columna_id in ubicaciones},
        etapas_globales=[e.id for e in _etapas_globales(session)],
    )


def columna_propia(session, usuario: str, columna_id: int) -> CrmColumna | None:
    """La columna VIVA de `usuario` con ese id. None si no existe, está de baja o es de otro:
    la API contesta lo mismo en los tres casos para no confirmar que el tablero de alguien más
    tiene ese id."""
    if not 0 < columna_id <= _MAX_ID:
        return None
    columna = session.get(CrmColumna, columna_id)
    if columna is None or not columna.habilitado or columna.username != usuario:
        return None
    return columna


def crear(session, usuario: str, nombre: str, color: str) -> CrmColumna:
    """Columna nueva al final del tablero de `usuario`. Nombre y color ya vienen validados."""
    tablero = cargar(session, usuario)
    if len(tablero.activas) >= MAX_COLUMNAS:
        raise ColumnasAlTope(f"Máximo {MAX_COLUMNAS} etapas en tu tablero — elimina alguna.")
    if len(tablero.todas) >= MAX_FILAS:
        raise ColumnasAlTope(f"Tu tablero llegó al máximo de {MAX_FILAS} etapas creadas.")
    if any(clave_nombre(c.nombre) == clave_nombre(nombre) for c in tablero.activas):
        raise NombreRepetido("Ya tienes una etapa con ese nombre")
    # Siempre una fila nueva, aunque haya una de baja con el mismo nombre. Revivirla le
    # devolvería los clientes que su baja mandó a otra columna.
    columna = CrmColumna(
        username=usuario,
        nombre=nombre,
        color=color,
        ord=max((c.ord for c in tablero.activas), default=-1) + 1,
    )
    session.add(columna)
    session.commit()
    session.refresh(columna)
    return columna


def editar(session, usuario: str, columna: CrmColumna, cambios: dict) -> CrmColumna:
    """Aplica nombre, color u ord (ya validados) a una columna propia y viva."""
    if "nombre" in cambios:
        clave = clave_nombre(cambios["nombre"])
        otras = [c for c in cargar(session, usuario).activas if c.id != columna.id]
        if any(clave_nombre(c.nombre) == clave for c in otras):
            raise NombreRepetido("Ya tienes una etapa con ese nombre")
    for campo, valor in cambios.items():
        setattr(columna, campo, valor)
    session.add(columna)
    session.commit()
    session.refresh(columna)
    return columna


def dar_de_baja(session, usuario: str, columna: CrmColumna) -> CrmColumna:
    """Baja de una columna propia. Devuelve la que hereda a sus clientes: la primera que queda.

    Una escritura: los clientes llegan a su destino por la cadena de `destino_id` (Tablero.viva),
    ni una ubicación reescrita. La única columna no se da de baja: un tablero sin columnas no
    tiene dónde poner a nadie.

    Dos bajas a la vez (dos pestañas) leen las mismas «restantes»: cada una puede elegir como
    destino la columna que la otra está quitando —un ciclo, y clientes que brincan al reordenar—
    o dejar el tablero sin columnas (revisiones del 2026-09-29). SQLite ordena los dos commits,
    así que la que confirma después ve a la otra al releer, y corrige lo SUYO: se deshace si ya
    no queda ninguna viva, o apunta a la primera viva si su destino murió. Todo por el ORM, para
    que el espejo vea cada paso; el tablero puede verse mal unos milisegundos, nunca quedarse así.
    """
    restantes = [c for c in cargar(session, usuario).activas if c.id != columna.id]
    if not restantes:
        raise ValueError("No se puede eliminar la única etapa")
    destino = restantes[0]
    columna.habilitado = False
    columna.destino_id = destino.id
    session.add(columna)
    session.commit()

    vivas = cargar(session, usuario).activas
    if not vivas:
        columna.habilitado = True
        columna.destino_id = None
        session.add(columna)
        session.commit()
        raise ValueError("No se puede eliminar la única etapa")
    if columna.destino_id not in {c.id for c in vivas}:
        destino = vivas[0]
        columna.destino_id = destino.id
        session.add(columna)
        session.commit()
    session.refresh(destino)
    return destino


def reordenar(session, usuario: str, orden) -> list[CrmColumna]:
    """Deja las columnas vivas de `usuario` en el orden de `orden` (sus ids, de izquierda a
    derecha). Tiene que traer cada una exactamente una vez: una lista a medias dejaría dos
    columnas con el mismo `ord`, y un id ajeno no se acepta aunque exista.

    El largo va primero: una lista enorme se rechaza sin recorrerla. Con el mismo largo y el
    mismo conjunto que las columnas vivas, no puede traer repetidos.

    Solo escribe las que cambian de lugar, y ninguna ubicación: reordenar no mueve a nadie.
    """
    tablero = cargar(session, usuario)
    if (
        not isinstance(orden, list)
        or len(orden) != len(tablero.ids)
        or not all(isinstance(i, int) and not isinstance(i, bool) for i in orden)
        or set(orden) != set(tablero.ids)
    ):
        raise ValueError("El orden debe traer cada una de tus etapas una sola vez")
    for posicion, columna_id in enumerate(orden):
        columna = tablero.todas[columna_id]
        if columna.ord != posicion:
            columna.ord = posicion
            session.add(columna)
    session.commit()
    return cargar(session, usuario).activas


def ubicar(session, usuario: str, phone: str, columna_id: int) -> None:
    """Deja PREPARADA en la sesión la ubicación del cliente en esa columna, sin commit.

    Para quien tiene que confirmarla junto con otra escritura: el alta de contacto guarda
    cliente y columna en el MISMO commit, así un fallo no deja un contacto sin su columna (y un
    reintento que contesta «ya existe»). El llamador ya comprobó que la columna es suya y está
    viva, y que el cliente está en su alcance.
    """
    ubicacion = session.exec(
        select(CrmUbicacion)
        .where(CrmUbicacion.username == usuario)
        .where(CrmUbicacion.phone == phone)
    ).first()
    if ubicacion is None:
        ubicacion = CrmUbicacion(username=usuario, phone=phone, columna_id=columna_id)
    else:
        ubicacion.columna_id = columna_id
        ubicacion.movida_at = utcnow()
    session.add(ubicacion)


def mover(session, usuario: str, phone: str, columna_id: int) -> None:
    """Deja al cliente en esa columna del tablero de `usuario`, con commit (mismas condiciones
    que `ubicar`).

    Upsert sobre ux_crmubicacion: si otra petición del mismo usuario creó la fila entre el SELECT
    y el INSERT (doble clic, dos pestañas), el INSERT choca y se reescribe la fila que ganó. El
    último movimiento es el que queda, que es lo que la persona hizo al final.
    """
    ubicar(session, usuario, phone, columna_id)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        ubicar(session, usuario, phone, columna_id)
        session.commit()
