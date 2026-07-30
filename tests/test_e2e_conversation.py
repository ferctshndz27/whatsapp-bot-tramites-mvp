"""E2E de la conversación por la ruta HTTP real: POST /webhook firmado →
webhook → conversation → presentation → whatsapp_client.

A diferencia de test_conversation.py (que llama handle_incoming directo), aquí
cada jornada entra por la ruta de Meta con firma HMAC válida y se verifican
TRES cosas: lo que ve el cliente (payloads capturados del doble de
whatsapp_client), el estado persistido (Conversation.state/context_json) y los
efectos en base (Ticket / Message / NluEvent).
"""

import hashlib
import hmac
import itertools
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.catalog import catalog
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

import app.db as db
import app.webhook as webhook
import app.whatsapp_client as whatsapp_client
from app.config import settings
from app.main import app
from app.models import (
    Client,
    Conversation,
    Direction,
    Feedback,
    Message,
    NluEvent,
    Ticket,
    TicketStatus,
)

PHONE = "5215500000000"
PHONE2 = "5215511112222"
APP_SECRET = "secreto-de-prueba-e2e"

# Sin context manager: no dispara el lifespan/startup (init_db + seed del
# usuario del panel + archive/backfill). Mismo patrón que tests/test_security.py.
client = TestClient(app)

# Contador global de wamids de entrada: init_db() crea el índice único parcial
# ux_message_wa_message_id, así que dos entrantes con el mismo id reventarían
# el insert (y el try/except del webhook se comería el error en silencio).
_in_ids = itertools.count(1)


def _next_in_id() -> str:
    return f"wamid.IN{next(_in_ids)}"


# ---- Fixtures locales ----


@pytest.fixture(autouse=True)
def e2e_engine(_isolated_db, monkeypatch):
    """Engine compartido entre hilos + init_db() real.

    Depende de _isolated_db (autouse del conftest) para correr DESPUÉS y pisar
    su engine. StaticPool es obligatorio: TestClient atiende la app en el hilo
    del portal de anyio y el SingletonThreadPool por defecto le entregaría a
    ese hilo una base :memory: distinta y VACÍA (el POST devolvería 200 con
    cero filas y el error 'no such table' lo tragaría el try/except).

    init_db() en vez de create_all para tener el índice único de wamid como en
    producción. Además se apagan el LLM (nada de red) y el espejo MSSQL.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "app_secret", APP_SECRET)
    monkeypatch.setattr(settings, "anthropic_api_key", "")  # deducción determinista, sin red
    monkeypatch.setattr(settings, "mssql_host", "")  # apaga archive.enabled()
    return engine


class FakeWhatsApp:
    """Doble de whatsapp_client que captura los envíos y devuelve wamids únicos."""

    def __init__(self):
        self.sent: list[dict] = []
        self.list_returns_none = False  # simula un interactivo rechazado por Meta
        self.text_fails_once = False    # simula el error 131047 en el primer envío
        # Números a los que Meta rechaza TODO envío (interactivo y texto): es el
        # caso real del 131047 cuando se venció la ventana de 24 h de ese chat.
        self.fail_sends_to: set[str] = set()
        self._wamids = itertools.count(1)
        self._text_calls = 0

    def _wamid(self) -> str:
        return f"wamid.OUT{next(self._wamids)}"

    def send_text(self, to_phone: str, body: str) -> str | None:
        self._text_calls += 1
        self.sent.append({"kind": "text", "to": to_phone, "text": body})
        if to_phone in self.fail_sends_to:
            return None
        if self.text_fails_once and self._text_calls == 1:
            return None
        return self._wamid()

    def send_list(self, to_phone: str, body: str, button: str, rows) -> str | None:
        self.sent.append(
            {"kind": "list", "to": to_phone, "text": body, "button": button, "rows": list(rows)}
        )
        if self.list_returns_none or to_phone in self.fail_sends_to:
            return None
        return self._wamid()

    def send_buttons(self, to_phone: str, body: str, buttons) -> str | None:
        self.sent.append(
            {"kind": "buttons", "to": to_phone, "text": body, "buttons": list(buttons)}
        )
        if to_phone in self.fail_sends_to:
            return None
        return self._wamid()

    # -- lecturas cómodas para las aserciones --
    @property
    def kinds(self) -> list[str]:
        return [s["kind"] for s in self.sent]

    @property
    def texts(self) -> list[str]:
        return [s["text"] for s in self.sent]

    def clear(self) -> None:
        self.sent.clear()


@pytest.fixture()
def wa(e2e_engine, monkeypatch) -> FakeWhatsApp:
    fake = FakeWhatsApp()
    # webhook.py importa el MÓDULO whatsapp_client, así que parchear el módulo alcanza.
    monkeypatch.setattr(whatsapp_client, "send_text", fake.send_text)
    monkeypatch.setattr(whatsapp_client, "send_list", fake.send_list)
    monkeypatch.setattr(whatsapp_client, "send_buttons", fake.send_buttons)
    return fake


@contextmanager
def _db():
    """Sesión contra app.db.engine (el que usa la app). La fixture `session`
    del conftest apunta a OTRO engine: es inservible para E2E por HTTP."""
    with Session(db.engine) as session:
        yield session


# ---- Helpers de firma y envelope de Meta ----


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


def _post(payload: dict, signature: str | None = None, sign: bool = True):
    """Firma EXACTAMENTE los bytes que se mandan (json= reserializaría y
    rompería la firma con un 403 engañoso)."""
    body = json.dumps(payload).encode()
    headers = {"content-type": "application/json"}
    if signature is not None:
        headers["x-hub-signature-256"] = signature
    elif sign:
        headers["x-hub-signature-256"] = _sign(body)
    return client.post("/webhook", content=body, headers=headers)


def _text_msg(body: str, phone: str = PHONE, wamid: str | None = None) -> dict:
    return {
        "from": phone,
        "id": wamid or _next_in_id(),
        "timestamp": "1700000000",
        "type": "text",
        "text": {"body": body},
    }


def _list_reply(row_id: str, phone: str = PHONE, title: str = "opción") -> dict:
    return {
        "from": phone,
        "id": _next_in_id(),
        "timestamp": "1700000000",
        "type": "interactive",
        "interactive": {"type": "list_reply", "list_reply": {"id": row_id, "title": title}},
    }


def _button_reply(button_id: str, phone: str = PHONE, title: str = "Sí") -> dict:
    return {
        "from": phone,
        "id": _next_in_id(),
        "timestamp": "1700000000",
        "type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": {"id": button_id, "title": title}},
    }


def _media_msg(phone: str = PHONE) -> dict:
    return {
        "from": phone,
        "id": _next_in_id(),
        "timestamp": "1700000000",
        "type": "image",
        "image": {"id": "media-abc", "mime_type": "image/jpeg"},
    }


def _envelope(*messages: dict, statuses: list[dict] | None = None) -> dict:
    value: dict = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "5215500000001", "phone_number_id": "PNID"},
    }
    if messages:
        value["messages"] = list(messages)
    if statuses:
        value["statuses"] = statuses
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": value}]}],
    }


def _status(wamid: str, state: str, timestamp: int) -> dict:
    return {
        "id": wamid,
        "status": state,
        "timestamp": str(timestamp),
        "recipient_id": PHONE,
    }


def _send_text(body: str, phone: str = PHONE, wamid: str | None = None):
    return _post(_envelope(_text_msg(body, phone=phone, wamid=wamid)))


def _messages(session, phone: str = PHONE) -> list[Message]:
    return session.exec(
        select(Message).where(Message.phone == phone).order_by(Message.id)
    ).all()


def _tickets(session, phone: str = PHONE) -> list[Ticket]:
    return session.exec(
        select(Ticket).where(Ticket.phone == phone).order_by(Ticket.id)
    ).all()


def _conv(session, phone: str = PHONE) -> Conversation:
    return session.get(Conversation, phone)


# ---- Jornadas ----



def _sin_requisitos(*service_ids: str) -> None:
    """Borra los requisitos documentados de servicios reales. El catálogo de
    producción (importado 2026-07-29) trae requisitos en casi todo; las pruebas
    que ejercitan el flujo SIN captura los limpian para no depender del dato."""
    for sid in service_ids:
        svc = catalog.service_by_id(sid)
        assert catalog.update_service(
            svc.id, svc.name, svc.price_info, requirements="", admin="prueba"
        ) is None


def test_jornada_completa_por_menu_crea_ticket_con_datos_del_catalogo(wa):
    """Regla: saludo → menú → categoría → servicio → 'sí' termina en UN ticket
    cuyo nombre/precio/categoría salen TAL CUAL del catálogo (el bot nunca
    inventa precio) y el cliente recibe su folio. La captura de requisitos se
    prueba en test_conversation; aquí se limpia para aislar la mecánica."""
    _sin_requisitos("sb-acta-nacimiento")
    for text in ["hola", "servicios", "1", "1", "si"]:
        assert _send_text(text).status_code == 200

    # (a) lo que ve el cliente
    assert wa.kinds == ["text", "list", "list", "buttons", "text"]
    assert "ayudarte" in wa.texts[0].lower()
    assert [r[0] for r in wa.sent[1]["rows"]] == [str(i) for i in range(1, 10)]  # 9 categorías
    assert [r[0] for r in wa.sent[2]["rows"]] == ["1", "2", "3", "4", "5", "6", "0"]
    assert [b[0] for b in wa.sent[3]["buttons"]] == ["si", "no"]
    assert "folio #1" in wa.texts[4]
    assert all(s["to"] == PHONE for s in wa.sent)

    with _db() as session:
        # (b) estado persistido
        conv = _conv(session)
        assert conv.state == "menu_categoria"  # tras cerrar, vuelve al menú
        assert conv.context_json == "{}"

        # (c) efectos en base
        tickets = _tickets(session)
        assert len(tickets) == 1
        assert tickets[0].service_id == "sb-acta-nacimiento"
        assert tickets[0].service_name == "Acta de nacimiento"
        assert tickets[0].price_info == "$25.00"  # precio real del catálogo de producción
        assert tickets[0].category == "Servicios básicos"
        assert tickets[0].status == TicketStatus.NUEVO
        assert tickets[0].escalado is False

        rows = _messages(session)
        assert [m.direction for m in rows] == [Direction.IN, Direction.OUT] * 5
        # El flujo puramente numérico no pasa por la cascada de deducción.
        assert session.exec(select(NluEvent)).all() == []


def test_pedido_directo_en_el_primer_mensaje_no_lista_servicios(wa):
    """Regla de negocio: si el primer mensaje ya trae el servicio, se saluda y
    se va directo a confirmar. NUNCA se lista el catálogo."""
    _send_text("necesito mi acta de nacimiento")

    assert wa.kinds == ["buttons"]  # ni una sola lista
    assert [b[0] for b in wa.sent[0]["buttons"]] == ["si", "no"]
    assert "Acta de nacimiento" in wa.texts[0]
    assert "1." not in wa.texts[0]  # sin listado

    _send_text("si")

    with _db() as session:
        tickets = _tickets(session)
        assert len(tickets) == 1
        assert tickets[0].service_id == "sb-acta-nacimiento"

        events = session.exec(select(NluEvent)).all()
        assert len(events) == 1
        assert events[0].capa == "nombre"
        assert events[0].service_id == "sb-acta-nacimiento"
        assert events[0].termino_en_ticket is True


def test_multiservicio_en_un_mensaje_confirma_una_vez_y_crea_dos_tickets(wa):
    """Regla: varios servicios en un mismo mensaje se confirman JUNTOS (UN solo
    mensaje interactivo) y generan un ticket por servicio."""
    _sin_requisitos("sb-acta-nacimiento", "sb-curp")
    _send_text("quiero acta de nacimiento y descarga de curp")

    assert wa.kinds == ["buttons"]  # una sola confirmación, no dos
    assert "Acta de nacimiento ($25.00)" in wa.texts[0]
    assert "Descarga de CURP ($10.00)" in wa.texts[0]

    _post(_envelope(_button_reply("si")))

    assert "2 solicitudes" in wa.texts[-1]
    assert wa.texts[-1].count("folio #") == 2

    with _db() as session:
        tickets = _tickets(session)
        assert [t.service_id for t in tickets] == ["sb-acta-nacimiento", "sb-curp"]

        events = session.exec(select(NluEvent)).all()
        assert len(events) == 1
        assert events[0].capa == "multi"
        assert events[0].service_id == "sb-acta-nacimiento,sb-curp"
        assert events[0].termino_en_ticket is True


def test_jornada_a_puros_toques_equivale_a_teclear_los_numeros(wa):
    """Regla: list_reply.id / button_reply.id se desenvuelven al mismo texto que
    el cliente habría tecleado — la jornada a toques da el mismo ticket."""
    _sin_requisitos("sb-acta-nacimiento")
    _send_text("servicios")
    _post(_envelope(_list_reply("1")))       # Servicios básicos
    _post(_envelope(_list_reply("1")))       # Acta de nacimiento
    _post(_envelope(_button_reply("si")))

    assert wa.kinds == ["list", "list", "buttons", "text"]
    assert "folio #1" in wa.texts[-1]

    with _db() as session:
        conv = _conv(session)
        assert conv.state == "menu_categoria"
        assert conv.context_json == "{}"

        tickets = _tickets(session)
        assert len(tickets) == 1
        assert tickets[0].service_id == "sb-acta-nacimiento"
        assert tickets[0].price_info == "$25.00"


def test_paginacion_de_infonavit_ida_y_vuelta_respeta_el_tope_de_meta(wa):
    """Regla: 12 servicios se paginan de 8 en 8; la lista nativa nunca pasa de
    10 filas; 'mas' avanza y '0' regresa al menú limpiando el contexto."""
    _send_text("menu")
    _post(_envelope(_list_reply("4")))  # Infonavit

    pagina_0 = [r[0] for r in wa.sent[-1]["rows"]]
    assert pagina_0 == ["1", "2", "3", "4", "5", "6", "7", "8", "mas", "0"]
    assert len(pagina_0) == 10  # tope exacto de Meta
    with _db() as session:
        assert json.loads(_conv(session).context_json) == {
            "category_id": "infonavit", "page": 0
        }

    _post(_envelope(_list_reply("mas")))

    pagina_1 = [r[0] for r in wa.sent[-1]["rows"]]
    assert pagina_1 == ["9", "10", "11", "12", "0"]  # última página: sin "Ver más"
    with _db() as session:
        assert json.loads(_conv(session).context_json) == {
            "category_id": "infonavit", "page": 1
        }

    _post(_envelope(_list_reply("0")))

    assert [r[0] for r in wa.sent[-1]["rows"]] == [str(i) for i in range(1, 10)]
    with _db() as session:
        conv = _conv(session)
        assert conv.state == "menu_categoria"
        assert conv.context_json == "{}"


def test_escalacion_crea_ticket_escalado_y_sigue_atendiendo_un_tramite_concreto(wa):
    """Regla: pedir asesor crea el ticket interno 'escalado' y calla el menú;
    pero un pedido concreto de trámite SÍ se atiende estando escalado."""
    _send_text("quiero hablar con un asesor")

    assert wa.kinds == ["text"]
    assert "asesor te contactará" in wa.texts[0].lower()

    with _db() as session:
        tickets = _tickets(session)
        assert len(tickets) == 1
        assert tickets[0].service_id == "escalado"
        assert tickets[0].escalado is True
        assert "quiero hablar con un asesor" in tickets[0].notes
        assert _conv(session).state == "escalado"

    _send_text("necesito un acta de divorcio")

    assert wa.kinds == ["text", "buttons"]  # botones nativos, no el "espera" enlatado
    assert "Acta de divorcio" in wa.texts[1]
    with _db() as session:
        assert _conv(session).state == "confirmar"


def test_agente_apagado_guarda_silencio_total_pero_deja_registro_del_mensaje(wa):
    """Regla: con ticket abierto y agent_active=False el bot no envía NADA,
    pero el mensaje del cliente sí queda registrado para la bandeja."""
    _send_text("quiero hablar con un asesor")

    with _db() as session:
        ticket = _tickets(session)[-1]  # el abierto más reciente
        ticket.assigned_to = "ana"
        ticket.agent_active = False
        session.add(ticket)
        session.commit()

    wa.clear()
    assert _send_text("sigo esperando").status_code == 200

    assert wa.sent == []  # cero llamadas a la API de Meta

    with _db() as session:
        assert _conv(session).state == "escalado"
        bodies = [m.body for m in _messages(session) if m.direction == Direction.IN]
        assert "sigo esperando" in bodies  # el asesor sí lo ve en la bandeja


# Era xfail(strict): documentaba que el guard de agent_active vivía solo en la
# rama "escalado". Corregido — el silencio aplica en cualquier estado — y esta
# prueba pasó a ser la regresión que lo protege.
def test_agente_apagado_sigue_silenciado_cuando_la_conversacion_avanza_de_estado(wa):
    """Regla: el 'silencio total' depende del ticket (agent_active=False), no
    del estado de la conversación."""
    _send_text("quiero hablar con un asesor")
    _send_text("necesito un acta de divorcio")  # deja la conversación en 'confirmar'

    with _db() as session:
        for ticket in _tickets(session):
            if ticket.status != TicketStatus.CERRADO:
                ticket.agent_active = False
                session.add(ticket)
        session.commit()

    wa.clear()
    _send_text("si")

    assert wa.sent == []  # el bot apagado no debe hablar
    with _db() as session:
        # ni crear solicitudes a espaldas del asesor
        assert [t.service_id for t in _tickets(session)] == ["escalado"]


def test_consulta_de_estatus_no_rompe_el_estado_ni_la_confirmacion_pendiente(wa):
    """Regla: preguntar el estatus funciona desde cualquier estado sin tocarlo:
    ni pierde la página del submenú ni cancela una confirmación pendiente."""
    _send_text("necesito mi acta de nacimiento")
    _send_text("si")  # folio #1

    # (a) parado en el submenú de una categoría
    _send_text("servicios")
    _send_text("1")
    with _db() as session:
        conv = _conv(session)
        estado_antes, contexto_antes = conv.state, conv.context_json
    assert estado_antes == "menu_servicio"

    _send_text("como va mi tramite")

    assert "folio #1" in wa.texts[-1]
    assert "Acta de nacimiento" in wa.texts[-1]
    with _db() as session:
        conv = _conv(session)
        assert conv.state == estado_antes
        assert conv.context_json == contexto_antes  # byte a byte

    # (b) con una confirmación pendiente
    _send_text("5")  # Descarga de CURP
    with _db() as session:
        assert json.loads(_conv(session).context_json)["pending_service_ids"] == ["sb-curp"]

    _send_text("como va mi tramite")

    assert "folio #1" in wa.texts[-1]
    with _db() as session:
        conv = _conv(session)
        assert conv.state == "confirmar"
        assert json.loads(conv.context_json)["pending_service_ids"] == ["sb-curp"]

    _send_text("si")

    with _db() as session:
        # el contexto sobrevivió a la consulta: el trámite a medio confirmar se cerró
        assert [t.service_id for t in _tickets(session)] == ["sb-acta-nacimiento", "sb-curp"]


def test_botones_de_feedback_registran_una_sola_vez_y_no_los_de_otro_chat(wa):
    """Regla: fb:<ticket>:<up|down> se atiende en cualquier estado, deja UNA
    sola opinión por ticket y un botón de otro teléfono se agradece sin registrar."""
    with _db() as session:
        propio = Ticket(
            phone=PHONE, category="Servicios básicos", service_id="sb-curp",
            service_name="Descarga de CURP", price_info="A cotizar",
        )
        ajeno = Ticket(
            phone=PHONE2, category="Servicios básicos", service_id="sb-acta-nacimiento",
            service_name="Acta de nacimiento", price_info="A cotizar",
        )
        session.add(propio)
        session.add(ajeno)
        session.commit()
        session.refresh(propio)
        session.refresh(ajeno)
        id_propio, id_ajeno = propio.id, ajeno.id

    _post(_envelope(_button_reply(f"fb:{id_propio}:up")))
    _post(_envelope(_button_reply(f"fb:{id_propio}:down")))  # segundo toque: se ignora
    _post(_envelope(_button_reply(f"fb:{id_ajeno}:up")))     # botón de otro chat

    assert wa.kinds == ["text", "text", "text"]  # agradecimiento plano, sin menús
    assert all("racias" in t for t in wa.texts)

    with _db() as session:
        rows = session.exec(select(Feedback)).all()
        assert len(rows) == 1
        assert rows[0].ticket_id == id_propio
        assert rows[0].rating == "up"


def test_adjunto_recibe_aviso_y_se_guarda_como_archivo_adjunto(wa):
    """Regla: el bot no recibe documentos — avisa, guarda el cuerpo como
    '[archivo adjunto]' y no mueve el estado de la conversación."""
    assert _post(_envelope(_media_msg())).status_code == 200

    assert wa.kinds == ["text"]
    assert "no puedo recibir documentos" in wa.texts[0].lower()

    with _db() as session:
        entrantes = [m for m in _messages(session) if m.direction == Direction.IN]
        assert len(entrantes) == 1
        assert entrantes[0].body == "[archivo adjunto]"
        assert _conv(session).state == "inicio"


def test_firma_hmac_invalida_devuelve_403_sin_ningun_efecto_secundario(wa):
    """Regla: la firma se valida ANTES de parsear el JSON — 403 y cero efectos."""
    payload = _envelope(_text_msg("necesito mi acta de nacimiento"))

    assert _post(payload, signature="sha256=deadbeef").status_code == 403
    assert _post(payload, sign=False).status_code == 403  # sin header

    assert wa.sent == []
    with _db() as session:
        assert session.exec(select(Message)).all() == []
        assert session.exec(select(Client)).all() == []
        assert session.exec(select(Ticket)).all() == []

    # Control positivo: el MISMO payload firmado sí produce efectos — así el
    # vacío de arriba prueba el rechazo, no un arnés muerto.
    assert _post(payload).status_code == 200
    assert wa.kinds == ["buttons"]
    with _db() as session:
        assert len(session.exec(select(Message)).all()) == 2


def test_un_mensaje_que_revienta_no_tumba_el_webhook_ni_al_siguiente(wa, monkeypatch):
    """Regla: un fallo procesando un mensaje solo se loguea — el webhook
    devuelve 200 (Meta no debe reintentar el lote) y los demás sí se atienden."""
    real = webhook.handle_incoming

    def _explota(session, phone, text, **kwargs):
        if "explota" in text:
            raise RuntimeError("fallo simulado procesando el mensaje")
        return real(session, phone, text, **kwargs)

    monkeypatch.setattr(webhook, "handle_incoming", _explota)

    response = _post(_envelope(_text_msg("explota ahora"), _text_msg("hola")))

    assert response.status_code == 200
    assert wa.kinds == ["text"]  # solo la respuesta del segundo mensaje
    assert "ayudarte" in wa.texts[0].lower()


def test_statuses_de_meta_actualizan_el_mensaje_del_wamid_correspondiente(wa, caplog):
    """Regla: value.statuses[] actualiza wa_status/wa_status_at de la fila del
    wamid correspondiente; un wamid desconocido se ignora sin romper."""
    _send_text("hola")

    with _db() as session:
        salidas = [m for m in _messages(session) if m.direction == Direction.OUT]
        assert len(salidas) == 1
        wamid = salidas[0].wa_message_id
        assert wamid == "wamid.OUT1"

    for estado, ts in [("sent", 1700000000), ("delivered", 1700000060), ("read", 1700000120)]:
        assert _post(_envelope(statuses=[_status(wamid, estado, ts)])).status_code == 200

    # wamid que no existe en la base: se ignora en silencio. "Sin romper" no se
    # puede medir con el 200 — _process_status corre dentro de un try/except que
    # loguea y sigue, así que una AttributeError sobre `row=None` daría 200 igual
    # y esta prueba seguiría verde mintiendo. Los statuses de wamids ajenos son
    # rutina (mensajes que el asesor manda desde WhatsApp Manager): tienen que
    # salir por el `return` temprano, no por el manejador de excepciones.
    with caplog.at_level(logging.ERROR):
        respuesta = _post(_envelope(statuses=[_status("wamid.DESCONOCIDO", "read", 1700009999)]))
    assert respuesta.status_code == 200
    errores_webhook = [r for r in caplog.records if r.name == "app.webhook"]
    assert errores_webhook == [], f"el wamid desconocido se manejó vía excepción: {caplog.text}"

    with _db() as session:
        rows = _messages(session)
        assert len(rows) == 2  # el status no crea filas
        salida = [m for m in rows if m.direction == Direction.OUT][0]
        assert salida.wa_status == "read"
        marca = salida.wa_status_at
        if marca.tzinfo is not None:
            marca = marca.astimezone(timezone.utc).replace(tzinfo=None)
        assert marca == datetime(2023, 11, 14, 22, 15, 20)  # 1700000120 UTC
        entrante = [m for m in rows if m.direction == Direction.IN][0]
        assert entrante.wa_status is None  # el wamid ajeno no tocó nada más


def test_fallback_a_texto_plano_cuando_el_interactivo_no_sale(wa):
    """Regla: si send_list falla el cliente NO se queda sin respuesta — sale el
    mismo texto por send_text y la fila OUT se etiqueta con ESE wamid."""
    wa.list_returns_none = True

    _send_text("servicios")

    assert wa.kinds == ["list", "text"]
    assert wa.sent[0]["text"] == wa.sent[1]["text"]  # mismo cuerpo, otro formato

    with _db() as session:
        salidas = [m for m in _messages(session) if m.direction == Direction.OUT]
        assert len(salidas) == 1  # el fallback no duplica la fila en la bitácora
        assert salidas[0].wa_message_id == "wamid.OUT1"  # el wamid del texto que sí salió


def test_reintento_de_meta_con_el_mismo_wamid_no_duplica_nada(wa, caplog):
    """Regla: idempotencia — un reintento de Meta no registra otro mensaje, no
    vuelve a deducir y no reenvía nada (dos tickets del mismo trámite a un
    cliente que va a pagar sería grave)."""
    payload = _envelope(_text_msg("necesito un acta de divorcio", wamid="wamid.DUP"))

    with caplog.at_level(logging.ERROR):
        assert _post(payload).status_code == 200
        assert _post(payload).status_code == 200  # reintento idéntico

    assert wa.kinds == ["buttons"]  # un solo envío

    with _db() as session:
        rows = _messages(session)
        assert [m.direction for m in rows] == [Direction.IN, Direction.OUT]
        assert len(session.exec(select(NluEvent)).all()) == 1

    # El resultado observable es idéntico lo maneje el guard `_already_processed`
    # o lo atrape el índice único con un IntegrityError que el try/except del
    # webhook se traga. No son lo mismo: si el guard desaparece, CADA reintento
    # de Meta (que son comunes) pasa a ser un error logueado, y la defensa queda
    # solo en un índice que el _isolated_db de conftest ni siquiera crea.
    assert caplog.records == [], f"el reintento se manejó vía excepción: {caplog.text}"


def test_cliente_bloqueado_no_recibe_respuesta_ni_deja_rastro(wa):
    """Regla: un Client con habilitado=False se ignora por completo — única vía
    de bloqueo operativo ante spam/abuso."""
    with _db() as session:
        session.add(Client(phone=PHONE, habilitado=False))
        session.commit()

    assert _send_text("hola").status_code == 200

    assert wa.sent == []
    with _db() as session:
        assert session.exec(select(Message)).all() == []

    # Control positivo: otro teléfono (no bloqueado) sí es atendido en el mismo
    # arnés — el silencio de arriba es el bloqueo, no una base muerta.
    _send_text("hola", phone=PHONE2)

    assert [s["to"] for s in wa.sent] == [PHONE2]
    with _db() as session:
        assert [m.phone for m in session.exec(select(Message)).all()] == [PHONE2, PHONE2]


def test_lote_de_meta_con_varios_mensajes_del_mismo_telefono_avanza_la_maquina(wa):
    """Regla: Meta agrupa mensajes — los del mismo teléfono se procesan en
    orden y cada uno ve el estado que dejó el anterior (cada _process_message
    abre su propia sesión y commitea)."""
    response = _post(
        _envelope(
            _text_msg("hola"),
            _text_msg("necesito un acta de nacimiento"),
            _text_msg("si"),
        )
    )

    assert response.status_code == 200
    assert wa.kinds == ["text", "buttons", "text"]
    assert "folio #1" in wa.texts[2]

    with _db() as session:
        assert len(_tickets(session)) == 1
        rows = _messages(session)
        assert [m.direction for m in rows] == [Direction.IN, Direction.OUT] * 3
        salidas = [m.wa_message_id for m in rows if m.direction == Direction.OUT]
        assert salidas == ["wamid.OUT1", "wamid.OUT2", "wamid.OUT3"]


def test_lote_con_dos_telefonos_no_cruza_respuestas_ni_wamids(wa):
    """Regla de privacidad del canal: cada cliente recibe SU respuesta y el acuse
    (wamid) que devuelve Meta se asigna a la fila de ESE teléfono — nunca se
    filtra el chat de uno a otro ni se le acredita a uno lo que salió para otro.

    El lote lleva a propósito un envío FALLIDO de PHONE: Meta rechaza todo lo que
    va a ese número (131047, ventana de 24 h vencida), así que el interactivo y su
    fallback en texto devuelven None y su fila OUT queda HUÉRFANA (sin wamid).
    Esa fila es el punto de la prueba: cuando después llega el wamid de PHONE2 ya
    existe una fila OUT más vieja y sin taggear, pero de OTRO teléfono, y lo único
    que impide que se robe el acuse es el `.where(Message.phone == phone)` de
    webhook._tag_sent_message. Sin el envío fallido el orden secuencial
    sobredetermina el resultado (a esas alturas no queda ninguna fila pendiente de
    PHONE) y el filtro por teléfono no decidiría nada.
    """
    wa.fail_sends_to = {PHONE}

    _post(
        _envelope(
            _text_msg("necesito un acta de nacimiento", phone=PHONE),
            _text_msg("necesito un acta de divorcio", phone=PHONE2),
        )
    )

    # (a) lo que ve cada cliente: nada del chat del otro. A PHONE se le intentó
    # dos veces (interactivo + fallback a texto plano) y a PHONE2 una sola.
    assert [s["to"] for s in wa.sent] == [PHONE, PHONE, PHONE2]
    assert wa.kinds == ["buttons", "text", "buttons"]
    assert "Acta de nacimiento" in wa.texts[0]
    assert "Acta de divorcio" not in wa.texts[0]
    assert "Acta de divorcio" in wa.texts[2]
    assert "Acta de nacimiento" not in wa.texts[2]

    # (b) el acuse cae en la fila del teléfono al que Meta sí le entregó
    with _db() as session:
        filas_a = _messages(session, PHONE)
        assert [m.direction for m in filas_a] == [Direction.IN, Direction.OUT]
        # A PHONE no le salió NADA: su fila OUT se queda sin acuse. Marcarla como
        # entregada sería mentirle al panel sobre un mensaje que nunca llegó.
        assert filas_a[1].wa_message_id is None

        filas_b = _messages(session, PHONE2)
        assert [m.direction for m in filas_b] == [Direction.IN, Direction.OUT]
        # Único wamid emitido en todo el lote (un envío rechazado no gasta id).
        assert filas_b[1].wa_message_id == "wamid.OUT1"


@pytest.mark.xfail(
    reason="BUG: _tag_sent_message (app/webhook.py:115-129) etiqueta la fila OUT más "
    "vieja sin taggear, sin filtrar por el envío en curso; si un envío falla "
    "(send_text -> None, p.ej. error 131047 de Meta) esa fila se roba el wamid del "
    "SIGUIENTE envío y los acuses marcan como entregado un mensaje que nunca llegó",
    strict=True,
)
def test_wamid_no_se_asigna_a_la_fila_equivocada_tras_un_envio_fallido(wa):
    """Regla: el wamid que devuelve Meta debe etiquetar la fila OUT de ESA
    respuesta; la fila de un envío fallido debe quedarse sin wamid."""
    wa.text_fails_once = True

    _send_text("hola")   # este envío falla: su fila debe quedar sin wamid
    _send_text("hola")   # este sí sale: 'wamid.OUT1' es suyo

    with _db() as session:
        salidas = [m for m in _messages(session) if m.direction == Direction.OUT]
        assert len(salidas) == 2
        assert salidas[0].wa_message_id is None
        assert salidas[1].wa_message_id == "wamid.OUT1"
