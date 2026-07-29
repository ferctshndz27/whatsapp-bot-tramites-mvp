from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlmodel import Session

import app.db as db
from app import media_store, whatsapp_client
from app.inbox import build_conversation_items, inbox_bot_on, inbox_chat, inbox_delete_message, inbox_send
from app.models import Conversation, Direction, Message, Ticket, TicketActivity, TicketStatus


def _request(user: str) -> SimpleNamespace:
    return SimpleNamespace(session={"user": user}, url=SimpleNamespace(path="/inbox/000"))


def _ticket(session, phone: str, **kwargs) -> Ticket:
    ticket = Ticket(
        phone=phone, category="Servicios básicos", service_id="curp",
        service_name="CURP certificada", price_info="$80", **kwargs,
    )
    session.add(ticket)
    session.commit()
    session.refresh(ticket)
    return ticket


def test_manual_reply_auto_claims_and_pauses_agent(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(whatsapp_client, "send_text", lambda phone, body: "wamid.AUTO1")
    session.add(_msg("444", Direction.IN, "hola", hours_ago=1))
    session.add(Conversation(phone="444", state="escalado"))
    ticket = _ticket(session, "444", escalado=True)

    inbox_send(_request("ana"), "444", body="te ayudo yo")

    session.refresh(ticket)
    assert ticket.assigned_to == "ana"  # quien responde se queda el chat
    assert ticket.status == TicketStatus.TOMADO
    assert ticket.agent_active is False  # y el agente se apaga solo
    actions = {
        a.action
        for a in session.query(TicketActivity).filter(TicketActivity.ticket_id == ticket.id)
    }
    assert {"tomado", "agente:pausado-auto"} <= actions


def test_manual_reply_pauses_agent_without_stealing_assigned_ticket(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(whatsapp_client, "send_text", lambda phone, body: "wamid.AUTO2")
    session.add(_msg("445", Direction.IN, "hola", hours_ago=1))
    session.add(Conversation(phone="445", state="escalado"))
    ticket = _ticket(
        session, "445", escalado=True, assigned_to="bob", status=TicketStatus.TOMADO
    )

    inbox_send(_request("ana"), "445", body="apoyo con esto")

    session.refresh(ticket)
    assert ticket.assigned_to == "bob"  # el dueño no cambia
    assert ticket.agent_active is False
    entry = session.query(TicketActivity).filter(
        TicketActivity.action == "agente:pausado-auto"
    ).one()
    assert entry.username == "ana"  # queda registrado quién lo apagó


def test_bot_reactivation_also_resumes_ticket_agent(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    session.add(Conversation(phone="556", state="escalado"))
    ticket = _ticket(
        session, "556", escalado=True, assigned_to="bob",
        status=TicketStatus.TOMADO, agent_active=False,
    )

    inbox_bot_on(_request("ana"), "556")

    session.refresh(ticket)
    assert ticket.agent_active is True
    entry = session.query(TicketActivity).filter(
        TicketActivity.action == "agente:reanudado"
    ).one()
    assert entry.username == "ana"


def _msg(phone: str, direction: Direction, body: str, hours_ago: float = 0) -> Message:
    return Message(
        phone=phone,
        direction=direction,
        body=body,
        created_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
    )


def test_items_flag_waiting_and_expired_window(session):
    session.add(_msg("111", Direction.IN, "hola", hours_ago=30))
    session.add(_msg("222", Direction.IN, "buenas", hours_ago=1))
    session.add(_msg("222", Direction.OUT, "hola, ¿en qué ayudo?", hours_ago=0.5))
    session.add(Conversation(phone="111", state="escalado"))
    session.add(Conversation(phone="222", state="inicio"))
    session.commit()

    items = {i["phone"]: i for i in build_conversation_items(session)}

    assert items["111"]["waiting"] is True
    assert items["111"]["window_open"] is False
    assert items["111"]["bot_paused"] is True
    assert items["222"]["waiting"] is False
    assert items["222"]["window_open"] is True
    assert items["222"]["bot_paused"] is False


def test_send_logs_message_and_pauses_bot(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(whatsapp_client, "send_text", lambda phone, body: "wamid.SENT1")
    session.add(_msg("333", Direction.IN, "hola", hours_ago=1))
    session.add(Conversation(phone="333", state="inicio"))
    session.commit()

    inbox_send(_request("ana"), "333", body="¿En qué puedo ayudarte?")

    sent = session.query(Message).filter(
        Message.phone == "333", Message.direction == Direction.OUT
    ).one()
    assert sent.wa_message_id == "wamid.SENT1"
    assert sent.sent_by == "ana"
    conv = session.get(Conversation, "333")
    session.refresh(conv)
    assert conv.state == "escalado"


def _upload(nombre: str = "acta.pdf", tipo: str = "application/pdf", datos: bytes = b"%PDF-1.4 x"):
    """UploadFile como el que produce el parser multipart real.

    OJO: se importa de starlette, no de fastapi. `fastapi.UploadFile` es una
    subclase suya y FastAPI NO convierte: usar la de fastapi aquí haría que
    estas pruebas ejercitaran un objeto que en producción no existe."""
    import io

    from starlette.datastructures import UploadFile

    return UploadFile(filename=nombre, file=io.BytesIO(datos), headers={"content-type": tipo})


def test_composer_envia_archivo_con_el_texto_como_pie(monkeypatch, session, tmp_path):
    """Un solo formulario: si viene archivo, el texto viaja como pie (WhatsApp)."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(media_store.settings, "media_dir", str(tmp_path))
    enviados = []
    monkeypatch.setattr(
        whatsapp_client, "send_media", lambda *a: enviados.append(a) or "wamid.MEDIA1"
    )
    monkeypatch.setattr(
        whatsapp_client, "send_text",
        lambda *a: (_ for _ in ()).throw(AssertionError("con archivo NO se manda texto suelto")),
    )
    session.add(_msg("777", Direction.IN, "hola", hours_ago=1))
    session.commit()

    inbox_send(_request("ana"), "777", body="aquí está tu acta", file=_upload())

    sent = session.query(Message).filter(
        Message.phone == "777", Message.direction == Direction.OUT
    ).one()
    assert sent.body == "aquí está tu acta"       # el pie queda como cuerpo
    assert sent.media_path is not None            # y el archivo queda ligado
    assert sent.wa_message_id == "wamid.MEDIA1"
    assert enviados[0][3] == "aquí está tu acta"  # caption enviado a Meta


def test_composer_sin_texto_registra_el_tipo_de_archivo(monkeypatch, session, tmp_path):
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(media_store.settings, "media_dir", str(tmp_path))
    monkeypatch.setattr(whatsapp_client, "send_media", lambda *a: "wamid.MEDIA2")
    session.add(_msg("778", Direction.IN, "hola", hours_ago=1))
    session.commit()

    inbox_send(_request("ana"), "778", body="", file=_upload("foto.png", "image/png", b"\x89PNG.."))

    sent = session.query(Message).filter(
        Message.phone == "778", Message.direction == Direction.OUT
    ).one()
    assert sent.body == "[imagen: foto.png]"


def test_composer_ignora_el_input_de_archivo_vacio(monkeypatch, session):
    """El navegador manda la parte del archivo aunque esté vacía: eso es
    "sin adjunto" y debe irse por el camino de texto."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(whatsapp_client, "send_text", lambda phone, body: "wamid.TXT")
    session.add(_msg("779", Direction.IN, "hola", hours_ago=1))
    session.commit()

    inbox_send(
        _request("ana"), "779", body="solo texto",
        file=_upload("", "application/octet-stream", b""),
    )

    sent = session.query(Message).filter(
        Message.phone == "779", Message.direction == Direction.OUT
    ).one()
    assert sent.body == "solo texto"
    assert sent.media_path is None


def test_composer_vacio_no_envia_nada(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(
        whatsapp_client, "send_text",
        lambda *a: (_ for _ in ()).throw(AssertionError("no hay nada que enviar")),
    )
    session.add(_msg("780", Direction.IN, "hola", hours_ago=1))
    session.commit()

    inbox_send(_request("ana"), "780", body="   ")

    assert session.query(Message).filter(
        Message.phone == "780", Message.direction == Direction.OUT
    ).all() == []


def test_composer_manda_el_adjunto_por_http_real(monkeypatch, tmp_path):
    """La prueba que faltaba: POST multipart de verdad, cruzando el parser.

    Las otras llaman a inbox_send() en proceso, y ahí el objeto de archivo lo
    fabrica el test. Solo por HTTP se ve lo que FastAPI entrega realmente — un
    fallo aquí significa que el asesor adjunta y el cliente no recibe nada."""
    import bcrypt
    from fastapi.testclient import TestClient
    from sqlmodel import SQLModel, create_engine
    from sqlalchemy.pool import StaticPool

    from app import media_store
    from app.config import settings
    from app.main import app
    from app.models import User

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(media_store.settings, "media_dir", str(tmp_path))
    monkeypatch.setattr(settings, "mssql_host", "")  # sin espejo en pruebas
    enviados = []
    monkeypatch.setattr(
        whatsapp_client, "send_media", lambda *a: enviados.append(a) or "wamid.HTTP1"
    )
    monkeypatch.setattr(
        whatsapp_client, "send_text",
        lambda *a: (_ for _ in ()).throw(AssertionError("el adjunto se degradó a texto")),
    )
    clave = "clave-de-prueba-1234"
    with Session(engine) as s:
        s.add(User(
            username="ana", is_admin=True,
            password_hash=bcrypt.hashpw(clave.encode(), bcrypt.gensalt(rounds=4)).decode(),
        ))
        s.add(_msg("900", Direction.IN, "hola", hours_ago=1))
        s.commit()

    cliente = TestClient(app, base_url="https://testserver")
    login = cliente.post(
        "/login", data={"username": "ana", "password": clave}, follow_redirects=False
    )
    assert login.status_code == 303

    respuesta = cliente.post(
        "/inbox/900/send",
        data={"body": "aquí está tu acta"},
        files={"file": ("acta.pdf", b"%PDF-1.4 contenido", "application/pdf")},
        follow_redirects=False,
    )

    assert respuesta.status_code == 303
    assert enviados, "send_media nunca se llamó: el adjunto se perdió en el camino"
    assert enviados[0][3] == "aquí está tu acta"  # el texto viaja como pie
    with Session(engine) as s:
        sent = s.query(Message).filter(
            Message.phone == "900", Message.direction == Direction.OUT
        ).one()
    assert sent.media_path is not None
    assert sent.wa_message_id == "wamid.HTTP1"


def test_send_blocked_when_window_expired(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    called = []
    monkeypatch.setattr(whatsapp_client, "send_text", lambda *a: called.append(a))
    session.add(_msg("444", Direction.IN, "hola", hours_ago=30))
    session.commit()

    response = inbox_send(_request("ana"), "444", body="tarde")

    assert called == []
    assert "err=window" in response.headers["location"]
    out = session.query(Message).filter(
        Message.phone == "444", Message.direction == Direction.OUT
    ).all()
    assert out == []


def test_bot_reactivation(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    session.add(Conversation(phone="555", state="escalado"))
    session.commit()

    inbox_bot_on(_request("ana"), "555")

    conv = session.get(Conversation, "555")
    session.refresh(conv)
    assert conv.state == "inicio"


def test_advisor_can_soft_delete_own_sent_message(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(whatsapp_client, "send_text", lambda phone, body: "wamid.SENT2")
    session.add(_msg("666", Direction.IN, "hola", hours_ago=1))
    session.commit()
    inbox_send(_request("ana"), "666", body="mensaje a borrar")
    sent = session.query(Message).filter(
        Message.phone == "666", Message.direction == Direction.OUT
    ).one()

    inbox_delete_message(_request("ana"), "666", sent.id)

    session.refresh(sent)
    assert sent.habilitado is False


def test_advisor_cannot_delete_client_message(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    session.add(_msg("777", Direction.IN, "palabras del cliente", hours_ago=1))
    session.commit()
    client_msg = session.query(Message).filter(Message.phone == "777").one()

    inbox_delete_message(_request("ana"), "777", client_msg.id)

    session.refresh(client_msg)
    assert client_msg.habilitado is True


def test_soft_deleted_message_hidden_from_inbox_chat(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(whatsapp_client, "send_text", lambda phone, body: "wamid.SENT3")
    session.add(_msg("888", Direction.IN, "hola", hours_ago=1))
    session.commit()
    inbox_send(_request("ana"), "888", body="respuesta a ocultar")
    sent = session.query(Message).filter(
        Message.phone == "888", Message.direction == Direction.OUT
    ).one()

    inbox_delete_message(_request("ana"), "888", sent.id)

    response = inbox_chat(_request("ana"), "888")
    bodies = [m.body for m in response.context["messages"]]
    assert "respuesta a ocultar" not in bodies
    assert "hola" in bodies
