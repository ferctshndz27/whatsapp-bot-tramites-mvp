"""Bus de avisos en memoria: el panel se entera de que hay mensajes nuevos sin esperar al reloj.

El CRM sondeaba y nada más: 20 s el tablero, 8 s el chat abierto (static/js/crm.js). Un turno
completo —el cliente escribe, la cascada NLU tarda sus segundos, el bot contesta— podía tardar
medio minuto en verse. Esto avisa en cuanto el mensaje está en la base.

**El aviso no lleva datos.** Ni teléfono, ni cuerpo, ni oficina: solo «hay algo nuevo». Quién
puede ver qué ya lo deciden `auth.alcance()` y el folio vigente (crm._conversacion_visible), y
duplicar esa regla en el transporte es garantizar que un día diverjan y que el stream filtre un
número a quien el panel le niega la conversación. Con un aviso mudo, el navegador vuelve a
preguntar por los endpoints que YA autorizan.
- ponytail: por eso mismo despierta a todos los paneles abiertos, no solo a los de la oficina
  del mensaje. Con el volumen de hoy es ruido de fondo; si se sostiene más de un mensaje por
  segundo o hay más de 20 paneles abiertos, el aviso lleva `office_id` —derivable sin consulta
  extra desde el identity map de la sesión que insertó— y aquí se filtra por alcance.

**El punto de captura es el mapper, no los seis sitios que insertan mensajes**
(conversation._log_message, inbox, dashboard ×3, cobranza_panel ×2): un `publicar()` en cada uno
son seis oportunidades de olvidarse, y la séptima llega con el próximo endpoint. Es el mismo
enganche que ya usa el espejo MSSQL (archive.py:2000), por el mismo motivo.

**Se marca en `after_insert` y se publica en `after_commit`**, porque `after_insert` corre dentro
del flush: avisar ahí anunciaría mensajes que un rollback todavía puede borrar.

**`publicar()` se llama desde cualquier hilo.** La cadena entera del webhook corre en
`run_in_threadpool` (webhook.py:74) porque es síncrona y uvicorn tiene un solo worker; tocar una
`asyncio.Queue` desde ese hilo sería una carrera. De ahí `call_soon_threadsafe` sobre el loop que
el lifespan captura al arrancar.

- ponytail: un bus en memoria asume UN worker, que es lo que hoy lanza el Dockerfile (uvicorn sin
  `--workers`) y de lo que ya depende la cadena síncrona del webhook. Con dos workers cada uno
  tendría el suyo y la mitad de los paneles no se enteraría —en silencio—; el día que se arranque
  con `--workers > 1`, el transporte tiene que pasar a long-poll con cursor sobre `message.id`,
  que no necesita estado compartido.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import threading

from sqlalchemy import event
from sqlalchemy.orm import Session as SesionSQLAlchemy
from sqlalchemy.orm import object_session

from app.models import Message

logger = logging.getLogger(__name__)

# Avisos en vuelo que se le guardan a un suscriptor. Una cola que crece es un navegador que dejó
# de leer; el aviso que se tira no cuesta nada, porque el siguiente refetch trae todo de todas
# formas y el sondeo sigue ahí como respaldo.
TOPE_COLA = 8

MARCA = "hay_mensajes"  # bandera en Session.info entre el flush y el commit

# Centinela: no es un aviso, es «cierra el stream». Viaja por la misma cola que todo lo demás
# para no tener que vigilar dos cosas a la vez dentro del generador.
CIERRE = object()

_loop: asyncio.AbstractEventLoop | None = None
_suscriptores: set[asyncio.Queue] = set()
_apagado_encadenado = False


def capturar_loop() -> None:
    """Lo llama el lifespan (app/main.py), que es quien corre dentro del event loop."""
    global _loop
    _loop = asyncio.get_running_loop()
    _encadenar_apagado()


def _encadenar_apagado() -> None:
    """Que los streams se cierren solos cuando el proceso recibe la señal de apagado.

    No basta con la librería: uvicorn instala sus manejadores de señal ANTES de importar la app
    —`serve()` abre `capture_signals()` y es `_serve()`, ya dentro, quien hace `config.load()`—,
    y registra el método ligado `self.handle_exit` de ese instante. Cualquier parche que llegue
    con el import de la app (el de sse-starlette, por ejemplo) queda apuntando a una referencia
    que ya nadie usa. Medido en este repo: con un panel abierto, SIGTERM dejaba a uvicorn en
    «Waiting for connections to close» hasta el SIGKILL de Docker.

    Desde el lifespan sí se llega a tiempo: para entonces el manejador de uvicorn ya está
    puesto, así que se toma con `getsignal`, se encadena el nuestro delante y se le llama
    después — uvicorn sigue apagándose exactamente igual.
    """
    global _apagado_encadenado
    if _apagado_encadenado or threading.current_thread() is not threading.main_thread():
        return  # una sola vez por proceso, y solo desde el hilo principal
    for sig in (signal.SIGTERM, signal.SIGINT):
        previo = signal.getsignal(sig)

        def manejar(num, marco, previo=previo):
            loop = _loop  # el vigente, no el que hubiera al encadenar
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(cerrar_suscriptores)
            if callable(previo):
                previo(num, marco)

        signal.signal(sig, manejar)
    _apagado_encadenado = True


def cerrar_suscriptores() -> None:
    """Le dice a cada stream abierto que termine. Corre ya dentro del loop."""
    for cola in list(_suscriptores):
        cola.put_nowait(CIERRE)  # sin pasar por el tope: esto no es un aviso que se pueda tirar


def soltar_loop() -> None:
    """Al apagar. Sin esto, `publicar()` seguiría empujando a un loop cerrado."""
    global _loop
    _loop = None


def suscribir() -> asyncio.Queue:
    cola: asyncio.Queue = asyncio.Queue()
    _suscriptores.add(cola)
    return cola


def desuscribir(cola: asyncio.Queue) -> None:
    _suscriptores.discard(cola)


def conectados() -> int:
    return len(_suscriptores)


def publicar() -> None:
    """Avisa a los paneles conectados. Llamable desde cualquier hilo, y nunca levanta.

    Sin loop —la suite, un script, un `python -c`— no hay a quién avisar y no pasa nada: esto
    corre justo después de guardar un mensaje, y un fallo del panel no puede tumbar el webhook.
    Es el mismo trato que `archive._safe()` le da al espejo.
    """
    loop = _loop
    if loop is None or not _suscriptores:
        return
    try:
        for cola in list(_suscriptores):
            loop.call_soon_threadsafe(_encolar, cola)
    except RuntimeError:  # el loop se cerró entre el if y el envío
        logger.debug("Aviso del CRM descartado: el event loop ya no corre")


def _encolar(cola: asyncio.Queue) -> None:
    if cola.qsize() < TOPE_COLA:
        cola.put_nowait(1)


def _registrar_listeners() -> None:
    @event.listens_for(Message, "after_insert")
    @event.listens_for(Message, "after_update")
    def _marcar(mapper, connection, target) -> None:
        # También en `after_update`: un acuse de Meta (enviado/entregado/leído) y un mensaje
        # oculto por el asesor cambian lo que el panel pinta, y llegan por UPDATE, no por INSERT.
        sesion = object_session(target)
        if sesion is not None:
            sesion.info[MARCA] = True

    @event.listens_for(SesionSQLAlchemy, "after_rollback")
    def _olvidar(sesion) -> None:
        # `info` sobrevive al rollback: sin esto, la marca de un mensaje que se deshizo se
        # cobraría en el siguiente commit de esa misma sesión, avisando de algo que no pasó.
        sesion.info.pop(MARCA, None)

    @event.listens_for(SesionSQLAlchemy, "after_commit")
    def _avisar(sesion) -> None:
        # A nivel de clase: cubre todas las sesiones de la app (las de SQLModel heredan de esta).
        # Para cualquier commit que no tocó mensajes, esto es un `dict.pop` y ya.
        if sesion.info.pop(MARCA, False):
            publicar()


_registrar_listeners()
