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

from datetime import timedelta

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

import app.db as db
from app import cobranza, cobranza_panel
from app.config import settings
from app.main import app
from app.models import Cobro, Ticket, TicketStatus, User, utcnow

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


def test_el_colaborador_encuentra_su_cartera_en_el_menu(entorno):
    """La vista existía y funcionaba, y aun así el dueño no la encontró: la
    única puerta era una tarjeta dentro de /my. Una ruta que funciona pero a la
    que nadie llega vale lo mismo que una que no existe.

    Va en «Operación» y no bajo «Administración», así que tiene que salirle a
    un colaborador — que es justo quien no puede ver /cobros.
    """
    cliente = _cliente(ANA)
    menu = cliente.get("/my").text
    assert 'href="/my/cobros"' in menu, "la cartera no aparece en el menú lateral"
    assert 'href="/cobros"' not in menu, (
        "el menú le ofrece /cobros a un colaborador, que va a rebotar a /"
    )
    # Y el ítem activo es UNO solo: con `startswith('/my')` se marcaban los dos.
    assert cliente.get("/my/cobros").text.count('aria-current="page"') == 1


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
# La antigüedad, en las DOS vistas
# ---------------------------------------------------------------------------


def test_la_antiguedad_sale_en_las_dos_vistas(entorno):
    """El punto entero del parcial compartido.

    Si la antigüedad viajara por contexto en vez de ser un global de Jinja,
    olvidarla en una de las dos rutas dejaría la celda VACÍA sin un solo error
    —Jinja usa Undefined permisivo— y la vista mutilada sería la del
    colaborador, que es justo quien menos lo reporta. Esta prueba pide las dos.
    """
    with Session(entorno) as s:
        _cobro(s, 200, creado_por=ANA, ticket_id=None, pedido=90_000)
        viejo = s.get(Cobro, 200)
        viejo.created_at = utcnow() - timedelta(days=5)
        s.add(viejo)
        s.commit()

    assert "5 días sin pagar" in _cliente(ANA).get("/my/cobros").text
    assert "5 días sin pagar" in _cliente(JEFA).get("/cobros").text


def test_un_cobro_reciente_no_se_marca(entorno):
    """El silencio de los primeros días es lo que le da valor al tercero."""
    with Session(entorno) as s:
        _cobro(s, 201, creado_por=ANA, ticket_id=None, pedido=90_000)
        s.commit()

    texto = _cliente(ANA).get("/my/cobros").text
    assert "sin pagar" not in texto
    assert "hoy" in texto


def test_la_tarjeta_de_atrasados_no_es_de_la_pagina(entorno):
    """La razón de ser de la tarjeta, y lo que la columna sola NO resuelve.

    La lista va por created_at DESC y pagina de 100 en 100: la página 1 es por
    definición lo más nuevo, así que los cobros viejos —los únicos por los que
    se pidió ver la antigüedad— caen en la última página, donde nadie mira. Se
    siembran 100 recientes que llenan la página 1 y 3 viejos que no salen en
    ella, y la tarjeta tiene que contar los 3 igual.
    """
    with Session(entorno) as s:
        for i in range(100):
            _cobro(s, 300 + i, creado_por=ANA, ticket_id=None, pedido=1_000)
        for i in range(3):
            c = _cobro(s, 500 + i, creado_por=ANA, ticket_id=None, pedido=40_000)
            c.created_at = utcnow() - timedelta(days=10)
        s.commit()

    respuesta = _cliente(JEFA).get("/cobros")
    contexto = respuesta.context
    assert len(contexto["cobros"]) == 100, "la página 1 dejó de estar llena"
    ids_visibles = {c.id for c in contexto["cobros"]}
    assert not ids_visibles & {500, 501, 502}, (
        "los viejos salieron en la página 1; la prueba dejó de probar lo suyo"
    )
    assert contexto["viejos"]["n"] == 3
    assert contexto["viejos"]["centavos"] == 120_000
    assert "Con más de 3 días" in respuesta.text


def test_la_tarjeta_de_atrasados_cuenta_lo_mismo_que_la_lista(entorno):
    """Si contara una población distinta de la que se ve, sería la primera
    cifra que se deja de creer. Un cobro viejo de OTRA oficina no suma."""
    with Session(entorno) as s:
        c = _cobro(s, 600, creado_por=ANA, ticket_id=None, pedido=50_000,
                   office_id=OFICINA_B)
        c.created_at = utcnow() - timedelta(days=9)
        propio = _cobro(s, 601, creado_por=ANA, ticket_id=None, pedido=30_000)
        propio.created_at = utcnow() - timedelta(days=9)
        s.commit()

    # ANA es de la oficina A: solo tiene que ver el suyo.
    contexto = _cliente(ANA).get("/my/cobros").context
    assert contexto["viejos"]["n"] == 1
    assert contexto["viejos"]["centavos"] == 30_000


def test_lo_ya_pagado_no_cuenta_como_atrasado(entorno):
    """Solo escalan los ESTADOS_VIVOS — la MISMA población que suma en
    `en_la_calle`, así que la tarjeta y las filas marcadas hablan del mismo
    dinero. Un pagado de hace un año no es cartera atrasada."""
    with Session(entorno) as s:
        c = _cobro(s, 700, creado_por=ANA, ticket_id=None, estado="pagado",
                   pedido=80_000, recibido=80_000)
        c.created_at = utcnow() - timedelta(days=365)
        c.pagado_at = utcnow() - timedelta(days=363)
        s.commit()

    respuesta = _cliente(ANA).get("/my/cobros")
    assert respuesta.context["viejos"]["n"] == 0
    assert "Con más de" not in respuesta.text
    # Y en la fila dice cuánto TARDÓ, no su edad.
    assert "pagó en 2 días" in respuesta.text
    assert "365" not in respuesta.text


def test_la_tarjeta_descuenta_lo_ya_recibido(entorno):
    """Mismo criterio que `en_la_calle`: un SPEI parcial dejó dinero adentro y
    lo que falta es la diferencia, no el pedido entero."""
    with Session(entorno) as s:
        c = _cobro(s, 800, creado_por=ANA, ticket_id=None,
                   pedido=100_000, recibido=40_000)
        c.created_at = utcnow() - timedelta(days=6)
        s.commit()

    assert _cliente(ANA).get("/my/cobros").context["viejos"]["centavos"] == 60_000


def test_una_ficha_vencida_pide_regenerar_en_vez_de_insistir(entorno):
    """La mentira más cara que puede contar esta celda: decirle «5 días sin
    pagar» a un link que ya no abre manda al asesor a gastar llamadas en un
    pago que es imposible."""
    with Session(entorno) as s:
        c = _cobro(s, 900, creado_por=ANA, ticket_id=None, pedido=70_000)
        c.created_at = utcnow() - timedelta(days=5)
        c.voucher_expira_at = utcnow() - timedelta(days=1)
        s.commit()

    texto = _cliente(ANA).get("/my/cobros").text
    assert "ficha vencida" in texto


def test_la_tabla_no_queda_chueca(entorno):
    """Barata y para el futuro: hoy pasa trivialmente porque la antigüedad
    entró DENTRO de la celda «Creado» y no como décima columna. Existe para que
    el día que alguien sí agregue una y olvide el <th>, los encabezados
    corridos se vean en rojo aquí y no en producción.

    Se cuenta con regex que acepta atributos —no con text.count("<td>")— que es
    justo lo que vuelve inútil a un assert de este tipo.
    """
    import re
    with Session(entorno) as s:
        _cobro(s, 950, creado_por=ANA, ticket_id=None, pedido=10_000)
        s.commit()

    for cliente, ruta in ((_cliente(JEFA), "/cobros"), (_cliente(ANA), "/my/cobros")):
        html = cliente.get(ruta).text
        thead = re.search(r"<thead>(.*?)</thead>", html, re.S).group(1)
        primera = re.search(r"<tbody>\s*<tr[^>]*>(.*?)</tr>", html, re.S).group(1)
        assert len(re.findall(r"<th[\s>]", thead)) == len(re.findall(r"<td[\s>]", primera)), (
            f"{ruta}: el <thead> y la primera fila no tienen el mismo número de celdas"
        )


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
