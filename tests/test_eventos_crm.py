"""El bus de avisos del CRM (app/eventos.py): qué se publica, cuándo y desde dónde.

Lo que protege:

- que el aviso salga **después del commit** y no en el flush, que un rollback todavía puede
  deshacer — avisar antes haría que el panel pidiera un mensaje que nunca existió;
- que una ráfaga no multiplique el trabajo del panel ni haga crecer una cola sin techo;
- que publicar **desde otro hilo** llegue igual, porque así llega todo lo del webhook: su cadena
  entera corre en `run_in_threadpool` (webhook.py:74);
- que sin event loop —la suite, un script, un `python -c`— no pase absolutamente nada. Esto
  corre justo después de guardar un mensaje: un fallo aquí no puede tumbar al bot.

El stream que consume estos avisos se prueba de punta a punta en tests/test_crm.py.
"""

import asyncio

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app import eventos
from app.models import Direction, Message

TELEFONO = "525500000099"


@pytest.fixture
def motor():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture(autouse=True)
def _bus_limpio():
    """El bus es estado de módulo: sin esto un test hereda los suscriptores del anterior."""
    yield
    eventos._suscriptores.clear()
    eventos.soltar_loop()


@pytest.fixture
def avisos(monkeypatch):
    """Cuenta llamadas a publicar() sin montar un loop: lo que importa aquí es el CUÁNDO."""
    llamadas = []
    monkeypatch.setattr(eventos, "publicar", lambda: llamadas.append(1))
    return llamadas


def _mensaje(**extra) -> Message:
    return Message(phone=TELEFONO, direction=Direction.IN, body="Hola", **extra)


def test_sin_event_loop_publicar_no_hace_nada_y_no_levanta():
    eventos.soltar_loop()
    eventos.suscribir()
    eventos.publicar()  # si esto levantara, levantaría dentro del webhook


def test_el_aviso_sale_en_el_commit_y_no_en_el_flush(motor, avisos):
    with Session(motor) as s:
        s.add(_mensaje())
        s.flush()
        assert avisos == [], "el flush todavía se puede deshacer: nadie debe enterarse"
        s.commit()
    assert avisos == [1]


def test_un_mensaje_deshecho_no_avisa_ni_se_cobra_en_el_siguiente_commit(motor, avisos):
    with Session(motor) as s:
        s.add(_mensaje())
        s.flush()
        s.rollback()
        assert avisos == []
        s.add(Message(phone=TELEFONO, direction=Direction.OUT, body="otro asunto"))
        s.commit()
    assert avisos == [1], "un aviso por el mensaje que sí se guardó, no dos"


def test_dos_mensajes_del_mismo_commit_son_un_solo_aviso(motor, avisos):
    with Session(motor) as s:
        s.add(_mensaje())
        s.add(Message(phone=TELEFONO, direction=Direction.OUT, body="Buenas"))
        s.commit()
    assert avisos == [1]


def test_un_acuse_de_meta_tambien_avisa(motor, avisos):
    """`wa_status` llega por UPDATE: sin escuchar el update, los ✓✓ esperarían al sondeo."""
    with Session(motor) as s:
        m = _mensaje()
        s.add(m)
        s.commit()
        avisos.clear()
        m.wa_status = "read"
        s.add(m)
        s.commit()
    assert avisos == [1]


def test_un_commit_que_no_toca_mensajes_no_avisa(motor, avisos):
    with Session(motor) as s:
        s.commit()
    assert avisos == []


def test_publicar_desde_otro_hilo_llega_a_la_cola():
    """El caso real: la cadena del webhook commitea en un hilo, el stream vive en el loop."""

    async def principal():
        eventos.capturar_loop()
        cola = eventos.suscribir()
        await asyncio.to_thread(eventos.publicar)
        await asyncio.sleep(0.05)  # deja correr los call_soon_threadsafe
        return cola.qsize()

    assert asyncio.run(principal()) == 1


def test_una_rafaga_no_hace_crecer_la_cola_sin_techo():
    """Una cola que crece es un navegador que dejó de leer; el aviso que se tira no cuesta."""

    async def principal():
        eventos.capturar_loop()
        cola = eventos.suscribir()
        for _ in range(eventos.TOPE_COLA * 3):
            eventos.publicar()
        await asyncio.sleep(0.05)
        return cola.qsize()

    assert asyncio.run(principal()) == eventos.TOPE_COLA


def test_el_suscriptor_que_se_va_deja_de_recibir():
    async def principal():
        eventos.capturar_loop()
        cola = eventos.suscribir()
        eventos.desuscribir(cola)
        eventos.publicar()
        await asyncio.sleep(0.05)
        return cola.qsize(), eventos.conectados()

    assert asyncio.run(principal()) == (0, 0)


def test_el_cierre_pasa_por_encima_del_tope_de_la_cola():
    """Un aviso se puede tirar; el cierre no.

    Si el centinela se perdiera porque la cola está llena, ese stream no terminaría y uvicorn
    volvería a quedarse en «Waiting for connections to close» hasta el SIGKILL de Docker.
    """

    async def principal():
        eventos.capturar_loop()
        cola = eventos.suscribir()
        for _ in range(eventos.TOPE_COLA * 2):
            eventos.publicar()
        await asyncio.sleep(0.05)
        eventos.cerrar_suscriptores()
        return [cola.get_nowait() for _ in range(cola.qsize())]

    avisos = asyncio.run(principal())
    assert avisos.count(eventos.CIERRE) == 1
    assert avisos[-1] is eventos.CIERRE
    assert len(avisos) == eventos.TOPE_COLA + 1
