"""Una conversación, varios folios con dueños distintos (decisión del operador, 2026-09-25).

La asignación por servicio parte un mensaje en folios de personas distintas, pero el chat de
WhatsApp sigue siendo UNO. Lo que se protege aquí:

- cada quien ve y escribe sobre SU folio: la tarjeta, el permiso y la toma al contestar salen
  del mismo selector (`clients.folio_de`), para que la lectura y la escritura no vuelvan a
  divergir (AUTHZ-2);
- la cartera deja VER la conversación, nunca escribir;
- contestar no le quita a nadie el folio provisional de un servicio que quien contesta no
  atiende: si no, la división se deshace al primer mensaje;
- el bot calla mientras CUALQUIER folio abierto tenga el agente apagado, y cada quien reactiva
  lo suyo (el admin, todo);
- tomar un folio al contestar es atómico: dos que contestan a la vez no se lo quedan los dos.

Escena base (`_division`): el cliente pidió visa y pasaporte; ana atiende visa y bob pasaporte,
así que la asignación dejó un folio provisional (NUEVO) a cada uno. pedro no atiende ninguno.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from types import SimpleNamespace

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

import app.db as db
from app import asignacion, inbox, whatsapp_client
from app.clients import folio_vigente
from app.config import settings
from app.conversation import handle_incoming
from app.inbox import inbox_bot_on, inbox_send
from app.main import app
from app.models import (
    CatalogAsesor,
    Client,
    ClienteAsesor,
    Conversation,
    Direction,
    Message,
    Ticket,
    TicketActivity,
    TicketStatus,
    User,
    utcnow,
)

CLAVE = "clave-de-prueba"
HASH = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()
CLI = "525500000051"


@pytest.fixture(autouse=True)
def escena(monkeypatch):
    """Base real (StaticPool + init_db) para poder mezclar el CRM por HTTP con las rutas de la
    bandeja llamadas directo. Lo enviado a WhatsApp se captura: se mide si salió, no si se
    intentó."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "geoip_enabled", False)
    enviados: list[str] = []
    # Un wamid distinto por envío: con init_db() el índice único ux_message_wa_message_id
    # existe, y dos salientes con el mismo id chocarían como en producción.
    monkeypatch.setattr(
        whatsapp_client, "send_text",
        lambda phone, body: enviados.append(body) or f"wamid.{len(enviados)}",
    )
    with Session(engine) as s:
        for nombre, admin in (("jefa", True), ("ana", False), ("bob", False), ("pedro", False)):
            s.add(User(username=nombre, password_hash=HASH, is_admin=admin, office_id=1))
        s.add(Client(phone=CLI, name="Cliente Varios", office_id=1))
        s.add(Conversation(phone=CLI, state="escalado", office_id=1))
        s.add(Message(phone=CLI, direction=Direction.IN, body="hola",
                      created_at=utcnow() - timedelta(minutes=5)))
        s.add(CatalogAsesor(service_id="visa", username="ana"))
        s.add(CatalogAsesor(service_id="pasaporte", username="bob"))
        s.commit()
    return enviados


def _folio(service: str, dueno: str | None, status=TicketStatus.NUEVO, hace_s: int = 0,
           agent_active: bool = True, phone: str = CLI) -> int:
    with Session(db.engine) as s:
        t = Ticket(phone=phone, client_name="Cliente Varios", category="c", service_id=service,
                   service_name=service.capitalize(), price_info="$1", status=status,
                   assigned_to=dueno, office_id=1, agent_active=agent_active,
                   created_at=utcnow() - timedelta(seconds=hace_s))
        s.add(t)
        s.commit()
        s.refresh(t)
        return t.id


def _division(**kw) -> tuple[int, int]:
    """El resultado de la asignación por servicio: visa → ana (más viejo), pasaporte → bob."""
    return _folio("visa", "ana", hace_s=60, **kw), _folio("pasaporte", "bob", hace_s=30, **kw)


def _ticket(ticket_id: int) -> Ticket:
    with Session(db.engine) as s:
        return s.get(Ticket, ticket_id)


def _acciones() -> list[tuple[str, str, int]]:
    with Session(db.engine) as s:
        return [(a.username, a.action, a.ticket_id)
                for a in s.exec(select(TicketActivity).order_by(TicketActivity.id)).all()]


def _cartera(username: str, phone: str = CLI) -> None:
    with Session(db.engine) as s:
        s.add(ClienteAsesor(phone=phone, username=username, created_by=username))
        s.commit()


def _req(user: str) -> SimpleNamespace:
    return SimpleNamespace(session={"user": user}, url=SimpleNamespace(path=f"/inbox/{CLI}"))


def _sesion(username: str) -> TestClient:
    cliente = TestClient(app, base_url="https://testserver")
    r = cliente.post("/login", data={"username": username, "password": CLAVE},
                     follow_redirects=False)
    assert r.status_code == 303, f"el login de {username} falló"
    return cliente


def _tarjeta(username: str, phone: str = CLI) -> dict | None:
    r = _sesion(username).get("/crm/api/tablero")
    assert r.status_code == 200, r.text
    return next((c for c in r.json()["contactos"] if c["phone"] == phone), None)


def _responde_el_bot(texto: str = "¿cómo va?") -> list[str]:
    with Session(db.engine) as s:
        return handle_incoming(s, CLI, texto)


# --- Un selector para tarjeta, permiso y acción -----------------------------


def _t(i: int, dueno: str | None, status: TicketStatus) -> Ticket:
    return Ticket(id=i, phone=CLI, category="c", service_id="s", service_name="s",
                  price_info="", status=status, assigned_to=dueno)


def test_cada_quien_tiene_su_folio_abierto_y_los_demas_el_vigente_de_siempre():
    """La lista llega del más nuevo al más viejo, como la consulta."""
    folios = [_t(9, "bob", TicketStatus.NUEVO), _t(8, "ana", TicketStatus.TOMADO)]
    assert folio_vigente(folios, "ana").id == 8, "ana no ve su propio folio"
    assert folio_vigente(folios, "bob").id == 9
    assert folio_vigente(folios, "pedro").id == 9, "sin folio propio, el vigente de siempre"
    assert folio_vigente(folios).id == 9, "sin usuario, la regla de siempre (admins)"


def test_un_folio_cerrado_propio_no_le_gana_al_abierto_de_otro():
    """Si no, un colaborador cuyo folio ya se cerró seguiría viendo —y escribiendo en— la
    conversación que ahora atiende otro."""
    folios = [_t(9, "ana", TicketStatus.CERRADO), _t(8, "bob", TicketStatus.TOMADO)]
    assert folio_vigente(folios, "ana").id == 8


# --- Quién puede tomar un folio al contestar --------------------------------


def test_el_folio_propio_y_el_sin_dueno_se_pueden_tomar():
    with Session(db.engine) as s:
        propio = s.get(Ticket, _folio("pasaporte", "ana"))
        sin_dueno = s.get(Ticket, _folio("pasaporte", None))
        assert asignacion.puede_tomar(s, propio, "ana")
        assert asignacion.puede_tomar(s, sin_dueno, "pedro")


def test_el_provisional_ajeno_solo_lo_toma_quien_atiende_el_servicio():
    """El provisional de bob es de pasaporte, que atiende bob: ana (que no lo atiende) no se lo
    queda por contestar. Si nadie de la oficina atiende el servicio, sigue la «cola abierta»
    de siempre: cualquiera."""
    with Session(db.engine) as s:
        de_bob = s.get(Ticket, _folio("pasaporte", "bob"))
        assert not asignacion.puede_tomar(s, de_bob, "ana"), "ana le quitaría el pasaporte a bob"
        s.add(CatalogAsesor(service_id="pasaporte", username="ana"))
        s.commit()
        assert asignacion.puede_tomar(s, de_bob, "ana"), "control: si ana también lo atiende, sí"
        de_nadie = s.get(Ticket, _folio("curp", "bob"))
        assert asignacion.puede_tomar(s, de_nadie, "pedro"), "nadie atiende curp: cola abierta"


def test_un_folio_trabajado_por_otro_no_se_toma():
    with Session(db.engine) as s:
        tomado = s.get(Ticket, _folio("curp", "bob", status=TicketStatus.TOMADO))
        assert not asignacion.puede_tomar(s, tomado, "ana")
        assert not asignacion.puede_tomar(s, tomado, "jefa"), "ni el admin, por contestar"


# --- Ver: cada quien su folio; la cartera también ve ------------------------


def test_cada_dueno_ve_la_conversacion_con_su_folio():
    _division()
    assert _tarjeta("ana")["ticket"]["service_name"] == "Visa"
    assert _tarjeta("bob")["ticket"]["service_name"] == "Pasaporte"
    assert _sesion("ana").get(f"/crm/api/chat/{CLI}").json()["ticket"]["service_name"] == "Visa"
    assert _sesion("bob").get(f"/crm/api/panel/{CLI}").json()["ticket"]["service_name"] == "Pasaporte"
    # Sin folio ni cartera, nada: el corte de siempre.
    assert _tarjeta("pedro") is None
    assert _sesion("pedro").get(f"/crm/api/chat/{CLI}").status_code == 404


def test_la_cartera_deja_ver_la_conversacion_aunque_no_tenga_folio():
    _division()
    _cartera("pedro")
    assert _tarjeta("pedro") is not None, "la cartera no le dio la conversación"
    assert _sesion("pedro").get(f"/crm/api/chat/{CLI}").status_code == 200
    # Un cliente de su cartera sin ningún folio (un lead) también está en su tablero.
    with Session(db.engine) as s:
        s.add(Client(phone="525500000052", name="Lead", office_id=1))
        s.commit()
    _cartera("pedro", "525500000052")
    assert _tarjeta("pedro", "525500000052") is not None


def test_la_cartera_no_da_permiso_de_escribir(escena):
    """La decisión del 2026-09-25, cerrada por la ruta: pedro ve el chat por su cartera, pero el
    folio vigente es el provisional de bob (pasaporte, que pedro no atiende)."""
    _, pasaporte = _division()
    _cartera("pedro")
    r = inbox_send(_req("pedro"), CLI, body="hola, soy pedro")
    assert r.headers["location"] == "/" and escena == [], "la cartera le dejó escribir"
    assert _ticket(pasaporte).assigned_to == "bob"
    assert _ticket(pasaporte).status == TicketStatus.NUEVO


# --- Contestar: cada quien consolida SU folio -------------------------------


def test_contestar_consolida_el_folio_propio_y_no_toca_el_del_otro(escena):
    """Antes contestar tomaba el folio abierto MÁS RECIENTE: ana, al contestar sobre su visa, se
    quedaba el pasaporte de bob y la división se deshacía al primer mensaje."""
    visa, pasaporte = _division()
    inbox_send(_req("ana"), CLI, body="te ayudo con la visa")
    assert (_ticket(visa).assigned_to, _ticket(visa).status) == ("ana", TicketStatus.TOMADO)
    assert (_ticket(pasaporte).assigned_to, _ticket(pasaporte).status) == ("bob", TicketStatus.NUEVO)
    inbox_send(_req("bob"), CLI, body="y yo con el pasaporte")
    assert (_ticket(pasaporte).assigned_to, _ticket(pasaporte).status) == ("bob", TicketStatus.TOMADO)
    assert escena == ["te ayudo con la visa", "y yo con el pasaporte"]
    assert not any(a.startswith("tomado-de:") for _, a, _ in _acciones())


def test_quien_no_atiende_el_servicio_no_le_quita_el_provisional_a_quien_si(escena):
    """El admin siempre puede escribir, pero contestar no le da el pasaporte de bob: no lo
    atiende. El bot sí se calla (hay una persona hablando) y el folio sigue siendo de bob."""
    _, pasaporte = _division()
    inbox_send(_req("jefa"), CLI, body="hola, soy la jefa")
    assert escena == ["hola, soy la jefa"]
    t = _ticket(pasaporte)
    assert (t.assigned_to, t.status) == ("bob", TicketStatus.NUEVO), "el admin se quedó el pasaporte"
    assert t.agent_active is False


def test_el_detalle_solo_promete_que_pasa_a_ti_si_contestar_se_la_queda():
    """«Si le contestas al cliente, la solicitud pasa a ti» era verdad con un folio por chat.
    Con la división ya no siempre: ana tiene su visa en esta conversación (contestar consolida
    la suya) y pedro no atiende pasaporte. Prometerlo sería mandarlos a contestar en balde."""
    _, pasaporte = _division()
    for quien in ("ana", "pedro"):
        html = _sesion(quien).get(f"/tickets/{pasaporte}").text
        assert "todavía no la abre" in html
        assert "pasa a ti" not in html, f"a {quien} se le promete un folio que contestar no le da"
    # Control: un provisional de un servicio que nadie atiende, en una conversación donde
    # pedro no tiene folio propio: contestar sí se lo queda.
    with Session(db.engine) as s:
        s.add(Client(phone="525500000053", name="Otro", office_id=1))
        s.commit()
    curp = _folio("curp", "bob", phone="525500000053")
    assert "pasa a ti" in _sesion("pedro").get(f"/tickets/{curp}").text


# --- El bot calla si cualquier folio lo tiene apagado ------------------------


def test_el_bot_calla_si_cualquier_folio_abierto_tiene_el_agente_apagado():
    """Antes miraba solo el folio abierto más reciente: ana apagaba el agente en su visa (el
    más viejo), el pasaporte de bob seguía con el agente prendido, y el bot le contestaba al
    cliente encima de ana."""
    visa, _ = _division()
    assert _responde_el_bot() != [], "control: con todo prendido el bot contesta"
    with Session(db.engine) as s:
        t = s.get(Ticket, visa)
        t.agent_active = False
        s.add(t)
        s.commit()
    assert _responde_el_bot() == [], "el bot habló encima de un asesor"


def test_reactivar_el_bot_solo_reactiva_lo_propio_y_el_admin_todo():
    visa, pasaporte = _division(status=TicketStatus.TOMADO, agent_active=False)
    inbox_bot_on(_req("ana"), CLI)
    assert _ticket(visa).agent_active is True
    assert _ticket(pasaporte).agent_active is False, "ana le reactivó el bot a bob"
    assert _responde_el_bot() == [], "bob sigue atendiendo: el bot no puede hablar"
    inbox_bot_on(_req("jefa"), CLI)
    assert _ticket(pasaporte).agent_active is True
    reanudados = {(u, t) for u, a, t in _acciones() if a == "agente:reanudado"}
    assert reanudados == {("ana", visa), ("jefa", pasaporte)}


def test_la_cabecera_del_chat_dice_quien_tiene_el_bot_apagado():
    """Con varios folios el bot puede estar callado por el folio de OTRO: «el agente sigue
    activo» sería mentira para ana, que mira el suyo."""
    _folio("visa", "ana", hace_s=60)
    _folio("pasaporte", "bob", status=TicketStatus.TOMADO, hace_s=30, agent_active=False)
    # Conversación NO escalada: así «bot pausado» solo puede salir del folio apagado.
    with Session(db.engine) as s:
        conv = s.get(Conversation, CLI)
        conv.state = "inicio"
        s.add(conv)
        s.commit()
    chat = _sesion("ana").get(f"/crm/api/chat/{CLI}").json()
    assert chat["agente_activo"] is False
    assert chat["contacto"]["apagado_por"] == ["bob"]
    assert chat["contacto"]["bot_pausado"] is True
    assert _tarjeta("ana")["bot_pausado"] is True


# --- La toma es atómica -------------------------------------------------------


def test_dos_que_contestan_a_la_vez_no_se_quedan_los_dos_el_folio(tmp_path, monkeypatch):
    """Con leer-y-escribir por el ORM, ana y bob leían el folio sin dueño, los dos lo
    «tomaban» y la bitácora decía que lo tomaron los dos; el dueño final lo decidía quién
    confirmaba al último. La barrera fuerza justo esa ventana: los dos ya decidieron que pueden
    tomarlo y ninguno ha escrito."""
    engine = create_engine(
        f"sqlite:///{tmp_path}/tramites.db", connect_args={"check_same_thread": False}
    )
    event.listen(engine, "connect", db._set_sqlite_pragmas)
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    with Session(engine) as s:
        for nombre in ("ana", "bob"):
            s.add(User(username=nombre, password_hash="x", office_id=1))
        s.add(Client(phone=CLI, office_id=1))
        s.add(Conversation(phone=CLI, state="escalado", office_id=1))
        folio = Ticket(phone=CLI, category="c", service_id="curp", service_name="CURP",
                       price_info="", status=TicketStatus.NUEVO, office_id=1)
        s.add(folio)
        s.commit()
        folio_id = folio.id

    barrera = threading.Barrier(2)
    original = asignacion.puede_tomar

    def _decide_y_espera(session, ticket, user):
        resultado = original(session, ticket, user)
        barrera.wait(timeout=20)
        return resultado

    monkeypatch.setattr(asignacion, "puede_tomar", _decide_y_espera)
    errores: list[BaseException] = []

    def _contesta(user: str) -> None:
        try:
            with Session(engine) as s:
                inbox._pause_bot(s, CLI, user)
        except BaseException as exc:  # noqa: BLE001 — se reporta abajo
            errores.append(exc)

    hilos = [threading.Thread(target=_contesta, args=(u,)) for u in ("ana", "bob")]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=40)
    assert not errores, errores
    with Session(engine) as s:
        dueno = s.get(Ticket, folio_id).assigned_to
        tomas = [a for a in s.exec(select(TicketActivity).where(
            TicketActivity.ticket_id == folio_id)).all() if a.action.startswith("tomado")]
    assert dueno in {"ana", "bob"}
    assert [a.username for a in tomas] == [dueno], f"la bitácora dice que lo tomaron: {tomas}"
    engine.dispose()
