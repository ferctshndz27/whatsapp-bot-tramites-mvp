"""Fase 3b — reparto del trabajo: asignación pegajosa y tope de carga.

Lo que se protege aquí es que un folio nuevo YA nace con dueño y que eso no le
quitó al equipo dos cosas que sí tenía: quedarse un chat contestándolo, y ver
el desbordamiento en la cola de «sin asignar».

Antes de esta fase la suite entera podía pasar en verde mientras el alta
cambiaba de comportamiento: las doce afirmaciones sobre `assigned_to` que había
en las pruebas sembraban el folio a mano, y las dos únicas que lo creaban por el
flujo real solo miraban el `status`. Por eso varias de estas pruebas pasan por
`handle_incoming` aunque sea más lento: es el único camino que no miente.
"""

import pathlib
from datetime import datetime

import pytest
from sqlmodel import select

import app.db as db
from app import asignacion, horario, whatsapp_client
from app.catalog import catalog
from app.config import settings
from app.conversation import handle_incoming
from app.inbox import inbox_send
from app.models import (
    Conversation,
    Direction,
    Message,
    Ticket,
    TicketActivity,
    TicketStatus,
    User,
    utcnow,
)
from app.templating import BUSINESS_TZ

PHONE = "5215511112222"


@pytest.fixture(autouse=True)
def _reparto_encendido(monkeypatch):
    """El reparto nace APAGADO (lo enciende un admin desde el Estudio), así que
    sin esto todo este archivo mediría el interruptor en vez de la regla. Los
    tres casos que sí miran el apagador lo vuelven a poner en False."""
    monkeypatch.setattr(settings, "reparto_automatico", True)


def _usuarios(session, *nombres: str, habilitado: bool = True) -> None:
    for n in nombres:
        session.add(User(username=n, password_hash="x", habilitado=habilitado))
    session.commit()


def _folio(session, **kw) -> Ticket:
    base = dict(
        phone=PHONE,
        category="c",
        service_id="s",
        service_name="S",
        price_info="$1",
        status=TicketStatus.NUEVO,
    )
    base.update(kw)
    t = Ticket(**base)
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


def _sin_requisitos(*service_ids: str) -> None:
    """El catálogo de producción trae requisitos en casi todo; estas pruebas
    miran el reparto, no la captura."""
    for sid in service_ids:
        svc = catalog.service_by_id(sid)
        assert (
            catalog.update_service(
                svc.id, svc.name, svc.price_info, requirements="", admin="prueba"
            )
            is None
        )


def _alta_real(session) -> list[Ticket]:
    """Un folio creado por el camino de verdad: pedir el trámite y confirmar."""
    _sin_requisitos("sb-acta-nacimiento")
    handle_incoming(session, PHONE, "Necesito un acta de nacimiento")
    handle_incoming(session, PHONE, "sí")
    return list(session.exec(select(Ticket).where(Ticket.phone == PHONE)).all())


def _fuera_de_horario(monkeypatch):
    monkeypatch.setattr(
        horario, "ahora", lambda: datetime(2026, 8, 30, 3, 0, tzinfo=BUSINESS_TZ)
    )


# --- El alta ya reparte -----------------------------------------------------


def test_el_alta_por_el_flujo_real_deja_el_folio_con_dueno(session):
    """La prueba que no existía: hasta la 3b nadie verificaba a quién le queda
    un folio recién creado, porque nunca le quedaba a nadie."""
    _usuarios(session, "ana")

    tickets = _alta_real(session)

    assert len(tickets) == 1
    assert tickets[0].assigned_to == "ana", "el folio nació huérfano"


def test_el_folio_auto_asignado_nace_en_nuevo_y_no_en_tomado(session):
    """TOMADO significa «alguien lo tiene» y sería falso para un folio que nadie
    ha mirado: el embudo de operacion_stats contaría trabajo inexistente."""
    _usuarios(session, "ana")

    tickets = _alta_real(session)

    assert tickets[0].status == TicketStatus.NUEVO


def test_el_historico_dice_que_el_folio_se_asigno_solo(session):
    """Sin esta fila el dueño aparece de la nada y la tabla de equipo deja de
    ser auditable."""
    _usuarios(session, "ana")

    tickets = _alta_real(session)

    filas = session.exec(
        select(TicketActivity).where(TicketActivity.ticket_id == tickets[0].id)
    ).all()
    acciones = {(f.username, f.action) for f in filas}
    assert ("sistema", "asignado-auto:ana") in acciones


def test_sin_asesores_dados_de_alta_el_folio_nace_sin_dueno(session):
    """Degradación segura: sin nadie a quien repartir, el flujo de siempre."""
    tickets = _alta_real(session)

    assert tickets[0].assigned_to is None


def test_un_mensaje_con_dos_tramites_va_a_un_solo_asesor(session):
    """Un cliente que pide tres trámites de un jalón es un cliente, no tres.
    Partirlo entre asesores es justo lo que la pegajosa existe para evitar."""
    _usuarios(session, "ana", "beto")
    _sin_requisitos("sb-acta-nacimiento", "sb-acta-matrimonio")
    handle_incoming(session, PHONE, "menu")
    handle_incoming(session, PHONE, "1")
    handle_incoming(session, PHONE, "1 y 2")
    handle_incoming(session, PHONE, "sí")

    tickets = list(session.exec(select(Ticket).where(Ticket.phone == PHONE)).all())
    assert len(tickets) == 2
    assert tickets[0].assigned_to == tickets[1].assigned_to is not None


# --- La pegajosa ------------------------------------------------------------


def test_el_cliente_que_regresa_cae_con_su_asesor_de_siempre(session):
    """La queja literal de los usuarios de la competencia (§2.6) es tener que
    volver a contar el caso desde cero."""
    _usuarios(session, "ana", "beto")
    _folio(session, assigned_to="beto", status=TicketStatus.CERRADO)

    asesor, motivo = asignacion.elegir(session, PHONE)

    assert (asesor, motivo) == ("beto", "pegajosa")


def test_la_pegajosa_se_pega_al_ULTIMO_asesor_no_al_primero(session):
    """Con un solo folio previo sembrado, invertir el `ORDER BY` del módulo no
    rompía una sola prueba: `desc()` y `asc()` eran indistinguibles. Un cliente
    que cambió de asesor tiene que regresar con el de la última vez."""
    from datetime import datetime

    _usuarios(session, "ana", "beto")
    _folio(
        session,
        assigned_to="ana",
        status=TicketStatus.CERRADO,
        created_at=datetime(2026, 1, 1, 12, 0),
    )
    _folio(
        session,
        assigned_to="beto",
        status=TicketStatus.CERRADO,
        created_at=datetime(2026, 6, 1, 12, 0),
    )

    asesor, motivo = asignacion.elegir(session, PHONE)

    assert (asesor, motivo) == ("beto", "pegajosa")


def test_la_pegajosa_manda_aunque_el_asesor_este_saturado(session):
    """Asimetría deliberada: la continuidad con el cliente vale más que el
    equilibrio de la carga."""
    _usuarios(session, "ana", "beto")
    for _ in range(settings.carga_maxima_asesor + 3):
        _folio(session, assigned_to="beto")

    asesor, motivo = asignacion.elegir(session, PHONE)

    assert (asesor, motivo) == ("beto", "pegajosa")


def test_un_asesor_dado_de_baja_no_se_queda_con_el_cliente(session):
    """Si no, el cliente quedaría pegado para siempre a alguien que ya no entra
    al panel — el mismo agujero que congelaba folios antes del rescate de admin."""
    _usuarios(session, "ana")
    _usuarios(session, "beto", habilitado=False)
    _folio(session, assigned_to="beto", status=TicketStatus.CERRADO)

    asesor, motivo = asignacion.elegir(session, PHONE)

    assert (asesor, motivo) == ("ana", "reparto")


# --- El reparto -------------------------------------------------------------


def test_el_reparto_elige_al_menos_cargado(session):
    _usuarios(session, "ana", "beto")
    _folio(session, phone="5210000000001", assigned_to="ana")
    _folio(session, phone="5210000000002", assigned_to="ana")
    _folio(session, phone="5210000000003", assigned_to="beto")

    asesor, motivo = asignacion.elegir(session, PHONE)

    assert (asesor, motivo) == ("beto", "reparto")


def test_con_la_misma_carga_gana_quien_lleva_mas_tiempo_sin_recibir(session):
    """Sin este desempate el orden sería alfabético y «ana» cargaría con todo
    para siempre. Por eso aquí gana «beto»: si la prueba esperara a «ana» no
    distinguiría la rotación del simple orden alfabético."""
    _usuarios(session, "ana", "beto")
    _folio(session, phone="5210000000001", assigned_to="beto", status=TicketStatus.CERRADO)
    _folio(session, phone="5210000000002", assigned_to="ana", status=TicketStatus.CERRADO)
    # El último folio repartido fue de «ana», así que ahora toca «beto».
    _folio(session, phone="5210000000003", assigned_to="ana", status=TicketStatus.CERRADO)

    asesor, _ = asignacion.elegir(session, "5219999999999")

    assert asesor == "beto", "el desempate está cayendo en el orden alfabético"


def test_con_todos_al_tope_el_folio_se_queda_sin_asignar(session):
    """El desbordamiento es lo que «sin asignar» pasa a significar en esta fase."""
    _usuarios(session, "ana")
    for i in range(settings.carga_maxima_asesor):
        _folio(session, phone=f"52100000000{i:02d}", assigned_to="ana")

    asesor, motivo = asignacion.elegir(session, PHONE)

    assert (asesor, motivo) == (None, "tope")


def test_un_folio_cerrado_deja_de_ocupar_lugar_bajo_el_tope(session):
    _usuarios(session, "ana")
    for i in range(settings.carga_maxima_asesor):
        _folio(
            session,
            phone=f"52100000000{i:02d}",
            assigned_to="ana",
            status=TicketStatus.CERRADO,
        )

    asesor, motivo = asignacion.elegir(session, PHONE)

    assert (asesor, motivo) == ("ana", "reparto")


# --- El horario -------------------------------------------------------------


def test_fuera_de_horario_no_se_reparte_trabajo_nuevo(session, monkeypatch):
    """Repartir de madrugada llena el panel de cada asesor con folios que nadie
    va a mirar hasta la mañana y consume el tope con trabajo dormido."""
    _fuera_de_horario(monkeypatch)
    _usuarios(session, "ana")

    asesor, motivo = asignacion.elegir(session, PHONE)

    assert (asesor, motivo) == (None, "fuera-de-horario")


def test_fuera_de_horario_la_pegajosa_sigue_funcionando(session, monkeypatch):
    """Mandarle a un cliente que regresa el mismo asesor de siempre es correcto
    a cualquier hora; el aviso de la fase 3a ya le dijo cuándo hay alguien."""
    _fuera_de_horario(monkeypatch)
    _usuarios(session, "ana", "beto")
    _folio(session, assigned_to="beto", status=TicketStatus.CERRADO)

    asesor, motivo = asignacion.elegir(session, PHONE)

    assert (asesor, motivo) == ("beto", "pegajosa")


# --- La colisión con «quien responde se queda el chat» ----------------------


def _contesta(session, monkeypatch, usuario: str, phone: str = PHONE) -> None:
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(whatsapp_client, "send_text", lambda p, b: "wamid.TEST")
    session.add(
        Message(phone=phone, direction=Direction.IN, body="hola", created_at=utcnow())
    )
    session.add(Conversation(phone=phone, state="escalado"))
    session.commit()
    from types import SimpleNamespace

    inbox_send(
        SimpleNamespace(session={"user": usuario}, url=SimpleNamespace(path="/inbox/x")),
        phone,
        body="yo te ayudo",
    )


def test_quien_contesta_se_queda_el_folio_del_reparto(session, monkeypatch):
    """Sin esto la guarda `assigned_to is None` no se cumpliría NUNCA con folios
    pre-asignados, y el trabajo de quien contesta se le acreditaría al dueño de
    reparto en toda la tabla de equipo."""
    _usuarios(session, "ana", "beto")
    ticket = _folio(session, assigned_to="ana")

    _contesta(session, monkeypatch, "beto")

    session.refresh(ticket)
    assert ticket.assigned_to == "beto", "el dueño de reparto se quedó el crédito"
    assert ticket.first_response_by == "beto"
    assert ticket.status == TicketStatus.TOMADO


def test_el_despojo_de_un_folio_provisional_queda_en_el_historico(session, monkeypatch):
    """Un cambio de dueño sin nadie apareciendo haciéndolo es justo lo que
    vuelve inauditable la tabla de equipo."""
    _usuarios(session, "ana", "beto")
    ticket = _folio(session, assigned_to="ana")

    _contesta(session, monkeypatch, "beto")

    acciones = [
        a.action
        for a in session.exec(
            select(TicketActivity).where(TicketActivity.ticket_id == ticket.id)
        ).all()
    ]
    assert "tomado-de:ana" in acciones


def test_un_folio_ya_trabajado_no_cambia_de_dueno_solo(session, monkeypatch):
    """En cuanto alguien lo trabajó —NUEVO deja de serlo— el dueño está ganado:
    si no, cada respuesta lo movería y el folio haría ping-pong."""
    _usuarios(session, "ana", "beto")
    ticket = _folio(session, assigned_to="ana", status=TicketStatus.TOMADO)

    _contesta(session, monkeypatch, "beto")

    session.refresh(ticket)
    assert ticket.assigned_to == "ana"


def test_contestar_el_folio_propio_no_ensucia_el_historico(session, monkeypatch):
    """El dueño de reparto que contesta su propio folio lo TOMA, no se lo quita
    a nadie."""
    _usuarios(session, "ana")
    ticket = _folio(session, assigned_to="ana")

    _contesta(session, monkeypatch, "ana")

    acciones = [
        a.action
        for a in session.exec(
            select(TicketActivity).where(TicketActivity.ticket_id == ticket.id)
        ).all()
    ]
    assert "tomado" in acciones
    assert not any(a.startswith("tomado-de:") for a in acciones)


# --- Lo que el cliente ve ---------------------------------------------------


def test_el_bot_no_dice_en_espera_de_asignacion_si_ya_hay_asesor(session):
    """Copy que la 3b volvería mentira: el folio nace en NUEVO pero YA tiene
    dueño. Y nunca se le dice al cliente quién lo lleva."""
    from app.conversation import _status_reply

    _folio(session, assigned_to="ana", service_id="sb-curp", service_name="CURP")

    texto = _status_reply(session, PHONE)

    assert "en espera de asignación" not in texto
    assert "con asesor asignado" in texto
    assert "ana" not in texto, "el nombre del asesor es organización interna"


def test_un_folio_sin_dueno_sigue_diciendo_que_espera_asignacion(session):
    from app.conversation import _status_reply

    _folio(session, service_id="sb-curp", service_name="CURP")

    assert "en espera de asignación" in _status_reply(session, PHONE)


# --- El desbordamiento sigue visible ---------------------------------------


def test_el_desbordamiento_sigue_contandose_como_sin_dueno(session):
    """La columna que la fase 3a puso en /activity es la que hace visible el
    reparto; el folio que no cupo tiene que seguir apareciendo bajo `None`."""
    from app.activity import carga_abierta

    _usuarios(session, "ana")
    for i in range(settings.carga_maxima_asesor):
        _folio(session, phone=f"52100000000{i:02d}", assigned_to="ana")
    asesor, _ = asignacion.elegir(session, PHONE)
    _folio(session, assigned_to=asesor)

    assert carga_abierta(session).get(None) == 1


# --- El folio de escalación también se reparte -----------------------------


def test_el_folio_de_escalacion_nace_con_dueno(session):
    """Segundo sitio de creación de folios. Sin esta prueba, borrar la
    asignación de `_escalate` dejaba la suite entera en verde: un cliente que
    pide asesor —el caso MÁS urgente— caía en la cola sin repartir."""
    _usuarios(session, "ana")

    handle_incoming(session, PHONE, "hola")
    handle_incoming(session, PHONE, "quiero hablar con un asesor")

    folio = session.exec(select(Ticket).where(Ticket.phone == PHONE)).first()
    assert folio is not None and folio.escalado is True
    assert folio.assigned_to == "ana"
    acciones = [
        a.action
        for a in session.exec(
            select(TicketActivity).where(TicketActivity.ticket_id == folio.id)
        ).all()
    ]
    assert "asignado-auto:ana" in acciones


# --- Transferir es trabajo humano ------------------------------------------


def test_transferir_un_folio_repartido_lo_saca_de_la_ventana_provisional(session, monkeypatch):
    """Sin esto una transferencia deliberada se deshacía con un solo mensaje:
    `reassign` no tocaba el `status`, así que el folio seguía pareciendo
    provisional y se lo quedaba el primer compañero que contestara."""
    from types import SimpleNamespace

    from app.dashboard import reassign_ticket

    _usuarios(session, "ana", "beto", "carlos")
    ticket = _folio(session, assigned_to="ana")
    peticion = SimpleNamespace(session={"user": "ana"}, url=SimpleNamespace(path="/tickets/1"))
    monkeypatch.setattr(db, "engine", session.get_bind())
    reassign_ticket(peticion, ticket.id, to_user="carlos", note="")

    session.expire_all()
    ticket = session.get(Ticket, ticket.id)
    assert ticket.assigned_to == "carlos"
    assert ticket.status == TicketStatus.TOMADO, "el folio sigue siendo robable"
    assert asignacion.provisional(ticket) is False


# --- El histórico se lee en español ----------------------------------------


@pytest.mark.parametrize(
    "action, esperado",
    [
        ("tomado-de:ana", "le tomó la solicitud a ana"),
        ("asignado-auto:ana", "el sistema le asignó la solicitud a ana"),
    ],
)
def test_las_acciones_nuevas_se_traducen_en_el_historico(action, esperado):
    """`_label` devuelve la cadena cruda para lo que no conoce: sin estas dos
    ramas el panel imprimiría «tomado-de:ana» en medio de frases en español."""
    from app.activity import _label

    assert _label(action) == esperado


# --- Lo que ve el compañero que abre un folio ajeno -------------------------


@pytest.fixture()
def panel(monkeypatch):
    """Panel real con un admin logueado: la plantilla del folio hay que verla
    renderizada, no adivinarla desde el contexto."""
    import bcrypt
    from sqlalchemy.pool import StaticPool
    from sqlmodel import create_engine

    from app.db import get_session

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    with get_session() as s:
        s.add(
            User(
                username="jefa",
                password_hash=bcrypt.hashpw(b"clave-de-prueba", bcrypt.gensalt()).decode(),
                is_admin=True,
            )
        )
        s.add(User(username="ana", password_hash="x"))
        s.commit()
    yield engine


def _abre_el_folio(estado: TicketStatus) -> str:
    from fastapi.testclient import TestClient

    from app.db import get_session
    from app.main import app

    with get_session() as s:
        s.add(
            Ticket(
                phone=PHONE,
                category="c",
                service_id="s",
                service_name="S",
                price_info="$1",
                status=estado,
                assigned_to="ana",
            )
        )
        s.commit()
    cliente = TestClient(app, base_url="https://testserver")
    cliente.post(
        "/login", data={"username": "jefa", "password": "clave-de-prueba"}, follow_redirects=False
    )
    return cliente.get("/tickets/1").text


def test_un_folio_repartido_no_dice_que_alguien_lo_esta_atendiendo(panel):
    """«La está atendiendo ana» sobre un folio que nadie ha abierto es falso, y
    esconde que contestarle al cliente basta para quedárselo."""
    html = _abre_el_folio(TicketStatus.NUEVO)

    assert "La está atendiendo" not in html
    assert "todavía no la abre" in html
    assert "pasa a ti" in html


def test_un_folio_ya_trabajado_sigue_diciendo_que_lo_atiende_su_dueno(panel):
    html = _abre_el_folio(TicketStatus.TOMADO)

    assert "La está atendiendo" in html
    assert "todavía no la abre" not in html


# --- El interruptor del Estudio --------------------------------------------


def test_el_reparto_nace_apagado():
    """Repartir cambia a quién le aparece el trabajo en /my. Esa decisión la
    toma un admin cuando el equipo está listo, no un despliegue."""
    from app.config import Settings

    # Settings() releído desde el entorno, sin los overrides que el panel haya
    # guardado: esto es el default con el que arranca un contenedor nuevo.
    assert Settings().reparto_automatico is False


def test_apagado_el_alta_deja_el_folio_sin_dueno(session, monkeypatch):
    """Con el interruptor abajo, el alta se comporta EXACTAMENTE como antes de
    la fase 3b: nadie se entera de que este módulo existe."""
    monkeypatch.setattr(settings, "reparto_automatico", False)
    _usuarios(session, "ana")

    tickets = _alta_real(session)

    assert tickets[0].assigned_to is None
    assert asignacion.elegir(session, PHONE) == (None, "apagado")


def test_apagado_no_se_escribe_bitacora_de_reparto(session, monkeypatch):
    monkeypatch.setattr(settings, "reparto_automatico", False)
    _usuarios(session, "ana")

    tickets = _alta_real(session)

    acciones = [
        a.action
        for a in session.exec(
            select(TicketActivity).where(TicketActivity.ticket_id == tickets[0].id)
        ).all()
    ]
    assert not any(a.startswith("asignado-auto:") for a in acciones)


def test_el_interruptor_es_una_perilla_del_estudio():
    """Si no está en CONFIG_EDITABLE, el panel no lo pinta y `guardar` lo
    rechaza: el nodo tendría un botón que no puede guardar nada."""
    from app.agent_settings import CONFIG_EDITABLE

    assert CONFIG_EDITABLE["reparto_automatico"].tipo == "bool"


def test_solo_un_admin_puede_encender_el_reparto(panel, monkeypatch):
    """El endpoint de configuración del Estudio es admin-only; un colaborador
    no puede repartirse el trabajo del equipo."""
    import bcrypt
    from fastapi.testclient import TestClient

    from app.db import get_session
    from app.main import app

    monkeypatch.setattr(settings, "reparto_automatico", False)
    with get_session() as s:
        usuario = s.exec(select(User).where(User.username == "ana")).one()
        usuario.password_hash = bcrypt.hashpw(b"clave-de-prueba", bcrypt.gensalt()).decode()
        s.add(usuario)
        s.commit()

    cliente = TestClient(app, base_url="https://testserver")
    cliente.post(
        "/login", data={"username": "ana", "password": "clave-de-prueba"}, follow_redirects=False
    )
    r = cliente.post("/agente/config", json={"key": "reparto_automatico", "value": "1"})

    assert r.status_code in (401, 403)
    assert settings.reparto_automatico is False, "un no-admin encendió el reparto"


def test_el_nodo_del_reparto_aparece_en_el_estudio(session):
    """El nodo tiene que existir en el lienzo con su clave de configuración: sin
    `flag` el botón de encender no se dibuja."""
    from app.agente_studio import _estado

    estado = _estado(session)
    nodo = next(n for n in estado["nodos"] if n["id"] == "reparto")

    assert nodo["flag"] == "reparto_automatico"
    assert nodo["fijo"] is False
    assert nodo["activo"] is True  # la fixture lo encendió


def test_el_nodo_cuenta_los_folios_que_repartio(session):
    from app.agente_studio import _estado

    _usuarios(session, "ana")
    _alta_real(session)

    nodo = next(n for n in _estado(session)["nodos"] if n["id"] == "reparto")
    assert nodo["mensajes"] == 1


def test_el_nodo_encendido_sin_asesores_avisa(session):
    """Un nodo encendido que no puede trabajar es invisible sin esto: el reparto
    se vería sano mientras deja todos los folios sin dueño."""
    from app.agente_studio import _estado

    nodo = next(n for n in _estado(session)["nodos"] if n["id"] == "reparto")

    assert any("no hay" in a for a in nodo["avisos"])


def test_apagado_el_nodo_no_regana(session, monkeypatch):
    monkeypatch.setattr(settings, "reparto_automatico", False)
    from app.agente_studio import _estado

    nodo = next(n for n in _estado(session)["nodos"] if n["id"] == "reparto")

    assert nodo["activo"] is False
    assert nodo["avisos"] == []


# --- El lienzo tiene que saber dibujar el nodo nuevo ------------------------


def _claves_del_mapa(nombre: str) -> set[str]:
    """Claves del literal `BASE`/`ICONO` del lienzo, leídas de static/js/agente.js."""
    import re

    html = pathlib.Path("static/js/agente.js").read_text()
    bloque = re.search(rf"const {nombre} = \{{(.*?)\}};", html, re.S)
    assert bloque, f"no se encontró el literal {nombre} en agente.js"
    return set(re.findall(r"(\w+)\s*:", bloque.group(1)))


@pytest.mark.parametrize("mapa", ["BASE", "ICONO"])
def test_todo_nodo_del_servidor_tiene_su_lugar_en_el_lienzo(session, mapa):
    """Agregar un nodo en Python y olvidar su coordenada en el JavaScript dejaba
    `/agente` EN BLANCO: el `TypeError` abortaba el script de nivel superior y se
    llevaba por delante los interruptores de las otras capas, que sí servían.
    Respondía 200, así que ninguna prueba de ruta lo notaba."""
    from app.agente_studio import _estado

    ids = {n["id"] for n in _estado(session)["nodos"]}

    assert ids <= _claves_del_mapa(mapa), (
        f"nodos sin entrada en {mapa}: {sorted(ids - _claves_del_mapa(mapa))}"
    )
