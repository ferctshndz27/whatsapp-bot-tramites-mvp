"""La cartera del colaborador: `/my/cobros`, la tarjeta de `/my` y el corte
por asesor de `/cobros`.

Lo que se defiende aquí son tres cosas que se rompen en silencio:

1. Que la cartera personal NO pierda el cobro cuyo folio ya no existe — el
   voucher de OXXO, justo el que más urge cobrar.
2. Que `/my/cobros` sea del colaborador y no del admin: es una cola de trabajo.
   Si alguien le pega un `_admin_or_redirect` "por consistencia", la vista deja
   de existir para quien la necesita y nadie se entera, porque el admin la ve
   bien.
3. Que la suma del corte por asesor sea exactamente el «En la calle» global.
   Dos números que deberían cuadrar y se calculan por separado son dos números
   que un día no cuadran, y ahí nadie sabe cuál miente.
"""

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

import app.db as db
from app import cobranza, cobranza_panel
from app.config import settings
from app.main import app
from app.models import Cobro, Ticket, TicketStatus, User

CLAVE = "contrasena-larga-123"
HASH_CLAVE = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt()).decode()
ANA = "ana"
BETO = "beto"
JEFA = "jefa"
FUEREÑO = "puebla"  # colaborador de OTRA oficina
PHONE = "5215511112222"

OFICINA_A = 1
OFICINA_B = 2


@pytest.fixture()
def entorno(_isolated_db, monkeypatch):
    # StaticPool: TestClient atiende en el hilo del portal de anyio y sin él
    # ese hilo vería una base :memory: distinta y VACÍA.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")  # apaga el espejo
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_cartera")

    with Session(engine) as s:
        s.add(User(username=ANA, password_hash=HASH_CLAVE, is_admin=False,
                   office_id=OFICINA_A))
        s.add(User(username=BETO, password_hash=HASH_CLAVE, is_admin=False,
                   office_id=OFICINA_A))
        s.add(User(username=JEFA, password_hash=HASH_CLAVE, is_admin=True,
                   office_id=OFICINA_A))
        s.add(User(username=FUEREÑO, password_hash=HASH_CLAVE, is_admin=False,
                   office_id=OFICINA_B))
        s.commit()
    yield engine


def _ticket(s: Session, ticket_id: int, asignado: str | None,
            office_id: int = OFICINA_A) -> Ticket:
    t = Ticket(id=ticket_id, phone=PHONE, client_name="Cliente",
               category="Servicios básicos", service_id="curp",
               service_name="CURP certificada", price_info="$800",
               status=TicketStatus.NUEVO,
               assigned_to=asignado, office_id=office_id)
    s.add(t)
    return t


def _cobro(s: Session, cobro_id: int, creado_por: str, ticket_id: int | None,
           estado: str = "pendiente", pedido: int = 100_000,
           recibido: int | None = None, office_id: int = OFICINA_A) -> Cobro:
    c = Cobro(id=cobro_id, phone=PHONE, concepto="CURP", ticket_id=ticket_id,
              monto_centavos=pedido, monto_pagado_centavos=recibido,
              estado=estado, creado_por=creado_por, office_id=office_id,
              token_verificacion=f"tok-{cobro_id}", livemode=False)
    s.add(c)
    return c


def _cliente(username: str) -> TestClient:
    c = TestClient(app, base_url="https://testserver")
    r = c.post("/login", data={"username": username, "password": CLAVE},
               follow_redirects=False)
    assert r.status_code == 303, "el login de la prueba falló"
    return c


def _mios(session: Session, user: str) -> set[int]:
    return {c.id for c in session.exec(cobranza.mios(select(Cobro), user)).all()}


# ---------------------------------------------------------------------------
# El predicado
# ---------------------------------------------------------------------------


def test_mios_toma_las_dos_puertas_y_solo_esas(entorno):
    """«Suyo» son dos cosas —lo generé yo, O el folio es mío— y ninguna más.

    Sin la rama `creado_por`, el asesor pierde de vista el cobro en cuanto
    suelta el folio. Sin la rama `assigned_to`, el que hereda un folio no ve la
    deuda que hereda con él.
    """
    with Session(entorno) as s:
        _ticket(s, 1, asignado=ANA)
        _ticket(s, 2, asignado=BETO)
        _cobro(s, 10, creado_por=ANA, ticket_id=2)    # lo generó ana, folio de beto
        _cobro(s, 11, creado_por=BETO, ticket_id=1)   # lo generó beto, folio de ana
        _cobro(s, 12, creado_por=BETO, ticket_id=2)   # ajeno por los dos lados
        s.commit()

        assert _mios(s, ANA) == {10, 11}
        assert _mios(s, BETO) == {10, 11, 12}


def test_mios_no_pierde_el_voucher_cuyo_folio_ya_no_existe(entorno):
    """El caso que un INNER JOIN se come callado.

    `Cobro.ticket_id` es nullable a propósito: con OXXO el cliente paga días
    después, cuando el folio ya se cerró y `_latest_open_ticket` no lo ve. Ese
    cobro sigue siendo el más urgente de la lista y tiene que aparecer.
    """
    with Session(entorno) as s:
        _cobro(s, 20, creado_por=ANA, ticket_id=None)
        s.commit()
        assert _mios(s, ANA) == {20}, (
            "el cobro sin folio desapareció: el join con Ticket dejó de ser OUTER"
        )


def test_mios_no_duplica_cuando_el_folio_existe(entorno):
    """El otro lado del join: una fila por cobro, no una por coincidencia.

    Si el `outerjoin` se armara contra algo que no es la PK de Ticket, la lista
    saldría inflada —no vacía— y los totales con ella. Se cuenta explícitamente.
    """
    with Session(entorno) as s:
        _ticket(s, 1, asignado=ANA)
        _cobro(s, 30, creado_por=ANA, ticket_id=1)  # califica por AMBAS ramas
        s.commit()
        filas = s.exec(cobranza.mios(select(Cobro), ANA)).all()
        assert len(filas) == 1, "el cobro que califica por las dos ramas salió doble"


# ---------------------------------------------------------------------------
# La vista
# ---------------------------------------------------------------------------


def test_my_cobros_es_del_colaborador_no_del_admin(entorno):
    """REGLA: /my/cobros NO es admin-only, y ese es todo el punto.

    Un `_admin_or_redirect` puesto «por consistencia» con el resto del panel
    deja la vista sin su único usuario, y el que hace la revisión —admin— la ve
    funcionando perfecto.
    """
    with Session(entorno) as s:
        _cobro(s, 40, creado_por=ANA, ticket_id=None)
        s.commit()

    respuesta = _cliente(ANA).get("/my/cobros", follow_redirects=False)
    assert respuesta.status_code == 200, (
        "el colaborador fue rechazado de su propia cartera"
    )
    assert "CURP" in respuesta.text


def test_my_cobros_no_muestra_los_de_otro_asesor(entorno):
    with Session(entorno) as s:
        _ticket(s, 2, asignado=BETO)
        _cobro(s, 50, creado_por=BETO, ticket_id=2, pedido=777_00)
        s.commit()

    texto = _cliente(ANA).get("/my/cobros").text
    assert "777" not in texto, "la cartera de ana trae un cobro de beto"


def test_my_cobros_no_cruza_oficinas(entorno):
    """`alcance()` sigue siendo la frontera dura: la cartera acota DENTRO de
    ella, no en su lugar. Un colaborador de otra oficina no ve esta caja aunque
    el cobro llevara su nombre."""
    with Session(entorno) as s:
        _cobro(s, 60, creado_por=FUEREÑO, ticket_id=None,
               pedido=999_00, office_id=OFICINA_A)
        s.commit()

    texto = _cliente(FUEREÑO).get("/my/cobros").text
    assert "999" not in texto, "un cobro de otra oficina se coló en la cartera"


def test_my_cobros_le_pide_login_al_anonimo(entorno):
    respuesta = TestClient(app, base_url="https://testserver").get(
        "/my/cobros", follow_redirects=False
    )
    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# El corte por asesor
# ---------------------------------------------------------------------------


def test_la_suma_por_asesor_es_exactamente_el_en_la_calle(entorno):
    """El invariante que hace creíble la vista del admin.

    Se mete un SPEI parcial a propósito —pagado a medias, estado todavía
    pendiente— porque es el caso donde «pedido» y «recibido» se separan: si el
    corte por asesor usara el pagado y el global el pedido, ahí es donde
    dejarían de cuadrar.
    """
    with Session(entorno) as s:
        _cobro(s, 70, creado_por=ANA, ticket_id=None, pedido=100_000)
        _cobro(s, 71, creado_por=ANA, ticket_id=None, pedido=50_000,
               recibido=20_000)  # SPEI parcial: sigue pendiente
        _cobro(s, 72, creado_por=BETO, ticket_id=None, pedido=30_000)
        _cobro(s, 73, creado_por=BETO, ticket_id=None, estado="pagado",
               pedido=80_000, recibido=80_000)
        s.commit()

    respuesta = _cliente(JEFA).get("/cobros")
    contexto = respuesta.context
    asesores = contexto["asesores"]
    assert {a["asesor"] for a in asesores} == {ANA, BETO}
    assert sum(a["en_la_calle"] for a in asesores) == contexto["en_la_calle"]
    assert sum(a["recuperado"] for a in asesores) == contexto["recuperado"]
    # El conteo también, y no es cosmético: `total_n` decide `hay_mas`, o sea
    # si aparece el enlace «Siguientes». Un total corto de más esconde cobros
    # reales detrás de una página a la que ya no se puede llegar. Se pone a
    # prueba con DOS asesores compartiendo el mismo estado, que es donde un
    # pliegue que pisa en vez de sumar se ve.
    assert sum(a["n"] for a in asesores) == contexto["total_n"] == 4
    # Y los números concretos, para que el invariante no se cumpla por ser 0=0.
    por_nombre = {a["asesor"]: a for a in asesores}
    assert por_nombre[ANA]["en_la_calle"] == 100_000 + (50_000 - 20_000)
    assert por_nombre[BETO]["en_la_calle"] == 30_000
    assert por_nombre[BETO]["recuperado"] == 80_000


def test_el_corte_no_se_mueve_cuando_se_suelta_el_folio(entorno):
    """Se agrupa por `creado_por` y no por `assigned_to` justamente por esto.

    `release_ticket` deja `assigned_to = None`. Un corte por el asignado
    mandaría ese dinero a un cubo «sin asesor» y le cambiaría el pasado a un
    mes ya cerrado; `creado_por` es permanente y NOT NULL.
    """
    with Session(entorno) as s:
        _ticket(s, 1, asignado=ANA)
        _cobro(s, 80, creado_por=ANA, ticket_id=1, pedido=60_000)
        s.commit()

    antes = _cliente(JEFA).get("/cobros").context["asesores"]

    with Session(entorno) as s:
        folio = s.get(Ticket, 1)
        folio.assigned_to = None  # el asesor suelta el folio
        s.add(folio)
        s.commit()

    despues = _cliente(JEFA).get("/cobros").context["asesores"]
    assert antes == despues, "soltar el folio movió el corte por asesor"
    assert despues[0]["asesor"] == ANA


def test_lo_confirmado_por_el_asesor_se_reporta_pero_no_se_descuenta(entorno):
    """El cobro que el asesor ya vio pagado sigue `pendiente` a propósito — el
    rango 4 es exclusivo de Stripe. La cartera lo reporta aparte en vez de
    descontarlo: descontarlo sería fingir que Stripe lo confirmó, y esconderlo
    le reclama al asesor dinero que él sabe que llegó.

    Es también la prueba de que `totales` ACUMULA: llegan dos filas del mismo
    estado —confirmada y no— y asignar en vez de sumar se comía una.
    """
    with Session(entorno) as s:
        _cobro(s, 100, creado_por=ANA, ticket_id=None, pedido=70_000)
        c = _cobro(s, 101, creado_por=ANA, ticket_id=None, pedido=30_000)
        c.confirmado_por_asesor = True
        c.confirmado_por = ANA
        s.commit()

    contexto = _cliente(ANA).get("/my/cobros").context
    assert contexto["en_la_calle"] == 100_000, "lo confirmado se descontó de la cartera"
    assert contexto["confirmado_sin_stripe"] == 30_000
    # Los dos cobros están en el MISMO estado: si `totales` asignara en vez de
    # acumular, aquí se vería uno solo.
    assert contexto["totales"]["pendiente"]["n"] == 2
    assert contexto["totales"]["pendiente"]["pedido"] == 100_000
    assert contexto["total_n"] == 2


# ---------------------------------------------------------------------------
# La tarjeta de /my
# ---------------------------------------------------------------------------


def test_la_tarjeta_de_my_dice_lo_mismo_que_la_cartera(entorno):
    """La tarjeta es la puerta: una vista a la que hay que acordarse de entrar
    no cambia el comportamiento de nadie. Y tiene que decir el mismo número que
    la vista a la que lleva, o deja de ser creíble a la segunda vez."""
    with Session(entorno) as s:
        _cobro(s, 90, creado_por=ANA, ticket_id=None, pedido=45_000)
        _cobro(s, 91, creado_por=ANA, ticket_id=None, estado="pagado",
               pedido=10_000, recibido=10_000)  # ya no está en la calle
        s.commit()

    cliente = _cliente(ANA)
    vivos, en_la_calle = cobranza_panel.cartera_de(ANA)
    assert (vivos, en_la_calle) == (1, 45_000)

    cartera = cliente.get("/my/cobros").context
    assert cartera["en_la_calle"] == en_la_calle

    contexto = cliente.get("/my").context
    assert contexto["cobros_en_la_calle"] == en_la_calle
    assert contexto["cobros_vivos"] == vivos
