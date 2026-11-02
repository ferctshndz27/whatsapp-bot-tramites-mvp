"""B2 de ext-cse — `folio_manual.crear_folio_manual`.

El colaborador atendió a un cliente por su teléfono personal y el folio nace a
mano. Lo que se protege aquí es lo que, si falla, no se nota hasta que alguien
reclama: que el folio quede en la oficina correcta, con dueño, con el agente IA
apagado (si no, el bot le contesta al cliente encima del colaborador) y sin
duplicarse cuando el mensaje del colaborador llega dos veces.
"""

import pytest
from sqlmodel import select

import app.db as db
from app.folio_manual import FolioManualError, crear_folio_manual
from app.models import (
    Client,
    Direction,
    Message,
    Ticket,
    TicketActivity,
    TicketStatus,
    User,
)

CLIENTE = "5215511112222"
CLIENTE_CANONICO = "525511112222"
SERVICIO = "sb-acta-nacimiento"


@pytest.fixture(autouse=True)
def _misma_base(session, monkeypatch):
    """`crear_folio_manual` resuelve el alcance con `auth.alcance`, que abre su
    propia sesión: sin esto lee una base distinta a la que sembró la prueba."""
    monkeypatch.setattr(db, "engine", session.get_bind())


def _usuario(session, nombre, **kw):
    session.add(User(username=nombre, password_hash="x", **kw))
    session.commit()


@pytest.fixture()
def equipo(session):
    _usuario(session, "ana")
    _usuario(session, "beto")
    _usuario(session, "carla", office_id=2)
    _usuario(session, "dora", habilitado=False)
    _usuario(session, "admin", is_admin=True)


def _crear(session, **kw):
    base = dict(
        creador="ana", phone=CLIENTE, nombre="Juan Pérez",
        service_id=SERVICIO, asignado_a="ana",
    )
    base.update(kw)
    return crear_folio_manual(session, **base)


def _codigo(session, **kw) -> str:
    with pytest.raises(FolioManualError) as e:
        _crear(session, **kw)
    return e.value.codigo


def _folios(session):
    session.expire_all()
    return list(session.exec(select(Ticket)).all())


def _acciones(session, ticket_id):
    session.expire_all()
    filas = session.exec(
        select(TicketActivity).where(TicketActivity.ticket_id == ticket_id)
    ).all()
    return {(a.username, a.action) for a in filas}


def test_cliente_nuevo_nace_manual_en_la_oficina_del_creador(session, equipo):
    """Si el cliente nuevo cayera en Tlaxcala por el default de la columna, el
    colaborador de otra oficina daría de alta a alguien que no vuelve a ver."""
    _usuario(session, "erika", office_id=2)

    t = _crear(session, creador="erika", asignado_a="erika")

    session.expire_all()
    cliente = session.get(Client, CLIENTE_CANONICO)
    assert cliente is not None, "no se creó el cliente con el teléfono canónico"
    assert (cliente.name, cliente.origen, cliente.office_id) == ("Juan Pérez", "manual", 2)
    assert (t.phone, t.office_id, t.origen) == (CLIENTE_CANONICO, 2, "manual")


def test_el_folio_nace_con_el_agente_apagado_y_los_datos_del_catalogo(session, equipo):
    """`agent_active=False`: con el bot encendido le respondería al cliente a
    la vez que el colaborador."""
    t = _crear(session)

    assert t.agent_active is False
    assert (t.service_id, t.client_name, t.assigned_to) == (SERVICIO, "Juan Pérez", "ana")
    assert t.service_name and t.category and t.price_info


def test_si_el_creador_se_queda_el_folio_nace_tomado(session, equipo):
    """TOMADO = «alguien lo tiene»: es cierto cuando el creador lo atiende."""
    t = _crear(session, asignado_a="ana")

    assert t.status == TicketStatus.TOMADO
    assert t.status_changed_at is not None
    assert ("ana", "alta-manual") in _acciones(session, t.id)


def test_si_se_lo_da_a_otro_nace_nuevo_con_dueno_y_queda_constancia(session, equipo):
    """Nace en NUEVO con dueño, igual que un folio auto-asignado: el compañero
    aún no lo ha mirado. La bitácora dice quién lo mandó y a quién."""
    t = _crear(session, asignado_a="beto")

    assert (t.status, t.assigned_to) == (TicketStatus.NUEVO, "beto")
    acciones = _acciones(session, t.id)
    assert ("ana", "alta-manual") in acciones
    assert ("ana", "reasignado:beto") in acciones


def test_cliente_existente_se_reutiliza_sin_renombrarlo(session, equipo):
    session.add(Client(phone=CLIENTE_CANONICO, name="Juan P.", origen="meta"))
    session.commit()

    t = _crear(session, nombre="Otro Nombre")

    session.expire_all()
    clientes = session.exec(select(Client)).all()
    assert len(clientes) == 1
    assert (clientes[0].name, clientes[0].origen) == ("Juan P.", "meta")
    assert t.client_name == "Juan P."


def test_cliente_de_otra_oficina_se_rechaza_y_no_deja_nada(session, equipo):
    """El teléfono es la llave: un cliente es de UNA oficina. Abrirle folio
    desde otra lo dejaría con un folio que su oficina no ve."""
    session.add(Client(phone=CLIENTE_CANONICO, name="Juan", office_id=2))
    session.commit()

    assert _codigo(session, creador="ana", asignado_a="ana") == "otra_oficina"
    assert _folios(session) == []


def test_el_admin_global_si_alcanza_a_la_otra_oficina_pero_el_folio_es_de_la_del_cliente(
    session, equipo
):
    session.add(Client(phone=CLIENTE_CANONICO, name="Juan", office_id=2))
    session.commit()

    t = _crear(session, creador="admin", asignado_a="carla")

    assert (t.office_id, t.assigned_to) == (2, "carla")


@pytest.mark.parametrize("asignado", ["dora", "carla", "nadie"])
def test_el_asignado_debe_estar_habilitado_y_ser_de_la_oficina_del_folio(
    session, equipo, asignado
):
    """Deshabilitado, de otra oficina o inexistente: el folio quedaría con un
    dueño que no puede abrirlo (trabajo congelado con dueño)."""
    assert _codigo(session, asignado_a=asignado) == "asignado"
    assert _folios(session) == []


@pytest.mark.parametrize("creador", ["dora", "nadie"])
def test_solo_un_colaborador_habilitado_puede_crear(session, equipo, creador):
    assert _codigo(session, creador=creador) == "creador"
    assert _folios(session) == []


def test_servicio_inexistente_se_rechaza_sin_crear_ni_al_cliente(session, equipo):
    """La validación va antes de escribir: un servicio malo no deja un cliente
    huérfano creado a medias."""
    assert _codigo(session, service_id="no-existe") == "servicio"
    session.expire_all()
    assert session.exec(select(Client)).all() == []


@pytest.mark.parametrize("phone", ["", "abc", None])
def test_telefono_inservible_se_rechaza(session, equipo, phone):
    assert _codigo(session, phone=phone) == "telefono"


def test_cliente_nuevo_exige_nombre(session, equipo):
    assert _codigo(session, nombre="  ") == "nombre"
    assert _folios(session) == []


def test_cliente_inhabilitado_no_recibe_folio(session, equipo):
    """Un cliente dado de baja no debe reaparecer por la puerta de atrás."""
    session.add(Client(phone=CLIENTE_CANONICO, name="Juan", habilitado=False))
    session.commit()

    assert _codigo(session) == "cliente_inhabilitado"


def test_doble_envio_no_duplica_el_folio_y_dice_cual_es(session, equipo):
    """El mismo reenvío puede llegar dos veces (reintento del proveedor): el
    segundo no abre otro folio del mismo trámite."""
    primero = _crear(session)

    with pytest.raises(FolioManualError) as e:
        _crear(session)

    assert e.value.codigo == "repetido"
    assert e.value.folio_id == primero.id
    assert len(_folios(session)) == 1


def test_un_folio_cerrado_no_cuenta_como_repetido(session, equipo):
    primero = _crear(session)
    primero.status = TicketStatus.CERRADO
    session.add(primero)
    session.commit()

    segundo = _crear(session)

    assert segundo.id != primero.id


def test_tope_de_folios_abiertos_por_cliente(session, equipo):
    """Mismo tope que el bot (`MAX_FOLIOS_ABIERTOS`): el colaborador no puede
    saltárselo por la extensión."""
    from app.conversation import MAX_FOLIOS_ABIERTOS

    session.add(Client(phone=CLIENTE_CANONICO, name="Juan"))
    for i in range(MAX_FOLIOS_ABIERTOS):
        session.add(Ticket(
            phone=CLIENTE_CANONICO, category="c", service_id=f"otro-{i}",
            service_name="S", price_info="$1",
        ))
    session.commit()

    assert _codigo(session) == "tope"


def test_los_mensajes_del_lote_quedan_ligados_al_folio(session, equipo):
    """Los archivos que el colaborador reenvió son la evidencia del trámite:
    deben aparecer en la ficha del folio."""
    m = Message(phone="5215500000001", direction=Direction.IN, body="[foto]")
    session.add(m)
    session.commit()
    session.refresh(m)

    t = _crear(session, mensajes=[m])

    session.expire_all()
    assert session.get(Message, m.id).ticket_id == t.id


def test_las_notas_del_colaborador_se_guardan_firmadas(session, equipo):
    t = _crear(session, notas="Pide acta de Puebla")

    assert "Pide acta de Puebla" in (t.notes or "")
    assert "ana" in (t.notes or "")
