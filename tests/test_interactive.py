"""Fase 1: mensajes interactivos de WhatsApp (listas/botones) y feedback 👍/👎."""

import json
from types import SimpleNamespace

from sqlmodel import select

import app.db as db
import app.nlu as nlu
from app.conversation import handle_incoming, presentation
from app.models import Conversation, Direction, Feedback, Ticket, User
from app.webhook import _extract_text
from app.whatsapp_client import _buttons_payload, _list_payload

PHONE = "5215511112222"


def _keywords_only(monkeypatch):
    monkeypatch.setattr(nlu.settings, "anthropic_api_key", "")


def test_list_payload_caps_rows_and_truncates_titles():
    rows = [(str(i), "Nombre de servicio larguísimo que no cabe") for i in range(1, 13)]

    payload = _list_payload("5215500000000", "cuerpo", "Ver opciones", rows)

    built = payload["interactive"]["action"]["sections"][0]["rows"]
    assert len(built) == 10  # tope de Meta
    assert all(len(r["title"]) <= 24 for r in built)
    assert built[0]["id"] == "1"


def test_buttons_payload_shape():
    payload = _buttons_payload(
        "5215500000000", "¿Confirmas?", [("si", "✅ Sí, confirmar"), ("no", "❌ No")]
    )

    buttons = payload["interactive"]["action"]["buttons"]
    assert [b["reply"]["id"] for b in buttons] == ["si", "no"]
    assert all(len(b["reply"]["title"]) <= 20 for b in buttons)


def test_extract_text_variants():
    assert _extract_text({"type": "text", "text": {"body": "hola"}}) == ("hola", False)
    assert _extract_text(
        {"type": "interactive", "interactive": {"list_reply": {"id": "3", "title": "X"}}}
    ) == ("3", False)
    assert _extract_text(
        {"type": "interactive", "interactive": {"button_reply": {"id": "si", "title": "Sí"}}}
    ) == ("si", False)
    assert _extract_text({"type": "image"}) == ("", True)


def test_confirmation_becomes_buttons(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _keywords_only(monkeypatch)

    replies = handle_incoming(session, PHONE, "necesito un acta de nacimiento")
    outbound = presentation(session, PHONE, replies)

    last = outbound[-1]
    assert isinstance(last, dict)
    assert [b[0] for b in last["buttons"]] == ["si", "no"]
    assert "onfirmas" in last["text"]  # el texto original queda como cuerpo


def test_category_menu_becomes_list(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _keywords_only(monkeypatch)

    replies = handle_incoming(session, PHONE, "servicios")
    outbound = presentation(session, PHONE, replies)

    last = outbound[-1]
    assert isinstance(last, dict) and "list" in last
    assert last["list"][0][0] == "1"  # el id es el número que se teclearía
    assert len(last["list"]) == 9  # las 9 categorías del catálogo semilla
    assert "1." in last["text"]  # opciones visibles en el cuerpo (Meta no auto-abre la lista)


def test_greeting_stays_plain_text(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _keywords_only(monkeypatch)

    replies = handle_incoming(session, PHONE, "hola")
    outbound = presentation(session, PHONE, replies)

    assert outbound == replies  # sin menú ni confirmación: texto tal cual


def test_big_service_menu_paginates_as_list(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    # Infonavit tiene 12 servicios: página de 8 + "Ver más" + "Volver" = 10 filas.
    session.add(
        Conversation(
            phone=PHONE, state="menu_servicio",
            context_json=json.dumps({"category_id": "infonavit", "page": 0}),
        )
    )
    session.commit()
    from app.catalog import catalog

    replies = [catalog.format_service_menu("infonavit")]
    outbound = presentation(session, PHONE, replies)

    last = outbound[-1]
    assert isinstance(last, dict) and "list" in last
    ids = [r[0] for r in last["list"]]
    assert len(ids) == 10  # tope exacto de Meta
    assert ids[:2] == ["1", "2"]
    assert "mas" in ids  # pagina en nativo ("mas" = lo que teclearía)
    assert ids[-1] == "0"  # Volver siempre presente


def test_last_page_list_has_no_more_row(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    session.add(
        Conversation(
            phone=PHONE, state="menu_servicio",
            context_json=json.dumps({"category_id": "infonavit", "page": 1}),
        )
    )
    session.commit()
    from app.catalog import catalog

    outbound = presentation(session, PHONE, [catalog.format_service_menu("infonavit", page=1)])

    ids = [r[0] for r in outbound[-1]["list"]]
    assert ids == ["9", "10", "11", "12", "0"]  # última página: sin "Ver más"


def test_list_payload_adds_description_for_long_titles():
    rows = [("1", "Constancia de situación fiscal con CURP"), ("2", "Corto")]

    built = _list_payload("5215500000000", "b", "Ver opciones", rows)
    built_rows = built["interactive"]["action"]["sections"][0]["rows"]

    assert len(built_rows[0]["title"]) <= 24
    assert built_rows[0]["description"].startswith("Constancia de situación fiscal")
    assert "description" not in built_rows[1]  # título corto: sin descripción


def test_feedback_button_records_once(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _keywords_only(monkeypatch)
    ticket = Ticket(
        phone=PHONE, category="c", service_id="s", service_name="S", price_info="$1"
    )
    session.add(ticket)
    session.commit()
    session.refresh(ticket)

    first = handle_incoming(session, PHONE, f"fb:{ticket.id}:up")
    handle_incoming(session, PHONE, f"fb:{ticket.id}:down")  # segundo toque: se ignora

    rows = session.exec(select(Feedback)).all()
    assert len(rows) == 1
    assert rows[0].rating == "up"
    assert "racias" in first[0]


def test_feedback_for_foreign_ticket_not_recorded(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _keywords_only(monkeypatch)
    ticket = Ticket(
        phone="5215599999999", category="c", service_id="s", service_name="S", price_info="$1"
    )
    session.add(ticket)
    session.commit()
    session.refresh(ticket)

    replies = handle_incoming(session, PHONE, f"fb:{ticket.id}:up")

    assert session.exec(select(Feedback)).all() == []  # ticket de otro chat
    assert "racias" in replies[0]  # se agradece igual, sin registrar


def test_finish_ticket_sends_feedback_buttons(monkeypatch, session):
    import app.dashboard as dashboard
    import app.inbox as inbox

    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(inbox, "_window_open", lambda *_: True)
    sent: dict = {}
    monkeypatch.setattr(dashboard.messaging, "send_text", lambda p, b: "wamid-1")
    monkeypatch.setattr(
        dashboard.messaging, "send_buttons",
        lambda p, b, btns: sent.update(buttons=btns) or "wamid-2",
    )
    session.add(User(username="jefa", password_hash="x", is_admin=True))
    ticket = Ticket(
        phone=PHONE, category="c", service_id="s", service_name="S",
        price_info="$1", assigned_to="jefa",
    )
    session.add(ticket)
    session.commit()
    session.refresh(ticket)

    request = SimpleNamespace(session={"user": "jefa"})
    dashboard.finish_ticket(request, ticket.id)

    # REGLA: los ids son tecleables. Twilio no tiene botones nativos y degrada
    # el mensaje a texto poniendo el id delante de cada opción; con los ids
    # viejos el cliente leía "fb:25:up) 👍 Sí, excelente".
    ids = [b[0] for b in sent["buttons"]]
    assert ids == ["1", "2"]
    assert all(":" not in opcion_id for opcion_id in ids), (
        "un id con ':' se le muestra tal cual al cliente de Twilio"
    )


def test_encuesta_contestada_por_texto_registra_la_calificacion(monkeypatch, session):
    # REGLA: en Twilio el cliente no toca un botón, teclea. La calificación se
    # resuelve contra el último folio cerrado, sin que la opción cargue el id.
    from app.conversation import FEEDBACK_PROMPT
    from app.models import Message, TicketStatus

    monkeypatch.setattr(db, "engine", session.get_bind())
    _keywords_only(monkeypatch)
    ticket = Ticket(
        phone=PHONE, category="c", service_id="s", service_name="S",
        price_info="$1", status=TicketStatus.CERRADO,
    )
    session.add(ticket)
    session.add(
        Message(
            phone=PHONE, direction=Direction.OUT,
            body=f"{FEEDBACK_PROMPT} (1 👍 Sí, excelente / 2 👎 Puede mejorar)",
        )
    )
    session.commit()
    session.refresh(ticket)

    replies = handle_incoming(session, PHONE, "1")

    filas = session.exec(select(Feedback)).all()
    assert len(filas) == 1, "la respuesta tecleada no quedó registrada"
    assert filas[0].rating == "up"
    assert filas[0].ticket_id == ticket.id
    assert "racias" in replies[0]


def test_un_numero_fuera_de_la_encuesta_no_se_toma_como_calificacion(monkeypatch, session):
    # REGLA: si el último mensaje que le mandamos NO fue la encuesta, un "1" del
    # cliente pertenece a esa conversación (un menú) y no a la calificación.
    from app.models import Message, TicketStatus

    monkeypatch.setattr(db, "engine", session.get_bind())
    _keywords_only(monkeypatch)
    session.add(
        Ticket(
            phone=PHONE, category="c", service_id="s", service_name="S",
            price_info="$1", status=TicketStatus.CERRADO,
        )
    )
    session.add(
        Message(phone=PHONE, direction=Direction.OUT, body="¿De qué categoría lo necesitas?")
    )
    session.commit()

    handle_incoming(session, PHONE, "1")

    assert session.exec(select(Feedback)).all() == []
