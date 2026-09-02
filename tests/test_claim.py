from types import SimpleNamespace

from sqlmodel import select

import app.dashboard as dashboard
import app.db as db
from app.conversation import handle_incoming
from app.dashboard import (
    claim_ticket,
    finish_ticket,
    release_ticket,
    toggle_ticket_agent,
    update_ticket_status,
)
from app.models import Conversation, Direction, Message, Ticket, TicketActivity, TicketStatus, User


def _request(user: str) -> SimpleNamespace:
    return SimpleNamespace(session={"user": user})


def _make_ticket(session, **kwargs) -> Ticket:
    ticket = Ticket(
        phone="5215500001111",
        category="Servicios básicos",
        service_id="acta_nac",
        service_name="Acta de nacimiento",
        price_info="$150",
        **kwargs,
    )
    session.add(ticket)
    session.commit()
    session.refresh(ticket)
    return ticket


def test_claim_assigns_free_ticket(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    ticket = _make_ticket(session)

    claim_ticket(_request("ana"), ticket.id)

    session.refresh(ticket)
    assert ticket.assigned_to == "ana"
    assert ticket.status == TicketStatus.TOMADO


def test_claim_does_not_steal_taken_ticket(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    ticket = _make_ticket(session, assigned_to="bob", status=TicketStatus.TOMADO)

    claim_ticket(_request("ana"), ticket.id)

    session.refresh(ticket)
    assert ticket.assigned_to == "bob"


def test_claim_rejected_on_closed_ticket(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    ticket = _make_ticket(session, status=TicketStatus.CERRADO)

    claim_ticket(_request("ana"), ticket.id)

    session.refresh(ticket)
    assert ticket.assigned_to is None
    assert ticket.status == TicketStatus.CERRADO


def test_release_only_by_assignee(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    ticket = _make_ticket(session, assigned_to="bob", status=TicketStatus.TOMADO)

    release_ticket(_request("ana"), ticket.id)
    session.refresh(ticket)
    assert ticket.assigned_to == "bob"

    release_ticket(_request("bob"), ticket.id)
    session.refresh(ticket)
    assert ticket.assigned_to is None
    assert ticket.status == TicketStatus.NUEVO


def test_status_change_only_by_assignee(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    ticket = _make_ticket(session, assigned_to="bob", status=TicketStatus.TOMADO)

    update_ticket_status(_request("ana"), ticket.id, status="cerrado")
    session.refresh(ticket)
    assert ticket.status == TicketStatus.TOMADO

    update_ticket_status(_request("bob"), ticket.id, status="cerrado")
    session.refresh(ticket)
    assert ticket.status == TicketStatus.CERRADO


def test_claim_resets_agent_active_to_true(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    # Ticket que quedó pausado de una atención anterior y se soltó.
    ticket = _make_ticket(session, escalado=True, agent_active=False)

    claim_ticket(_request("ana"), ticket.id)

    session.refresh(ticket)
    assert ticket.agent_active is True


def test_agent_toggle_only_by_assignee(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    ticket = _make_ticket(session, assigned_to="bob", status=TicketStatus.TOMADO, escalado=True)

    toggle_ticket_agent(_request("ana"), ticket.id)
    session.refresh(ticket)
    assert ticket.agent_active is True  # sin cambios: ana no es la asignada

    toggle_ticket_agent(_request("bob"), ticket.id)
    session.refresh(ticket)
    assert ticket.agent_active is False

    toggle_ticket_agent(_request("bob"), ticket.id)
    session.refresh(ticket)
    assert ticket.agent_active is True


def test_agent_toggle_logs_activity_with_username(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    ticket = _make_ticket(session, assigned_to="bob", status=TicketStatus.TOMADO, escalado=True)

    toggle_ticket_agent(_request("bob"), ticket.id)

    entry = session.exec(
        select(TicketActivity).where(TicketActivity.ticket_id == ticket.id)
    ).one()
    assert entry.username == "bob"
    assert entry.action == "agente:pausado"


def test_release_resets_agent_and_notifies_escalated_client(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    sent = []
    monkeypatch.setattr(
        dashboard.messaging, "send_text",
        lambda phone, body: sent.append((phone, body)) or "wamid.NOTICE",
    )
    ticket = _make_ticket(
        session, assigned_to="bob", status=TicketStatus.TOMADO, escalado=True, agent_active=False
    )
    session.add(Message(phone=ticket.phone, direction=Direction.IN, body="hola"))
    session.commit()

    release_ticket(_request("bob"), ticket.id)

    session.refresh(ticket)
    assert ticket.assigned_to is None
    assert ticket.agent_active is True  # el bot vuelve a cubrir el chat
    assert len(sent) == 1
    assert "revisando más a fondo" in sent[0][1]
    assert "folio #" in sent[0][1]
    logged = session.exec(
        select(Message).where(Message.phone == ticket.phone, Message.direction == Direction.OUT)
    ).all()
    assert len(logged) == 1


def test_release_of_unescalated_ticket_sends_no_notice(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    sent = []
    monkeypatch.setattr(
        dashboard.messaging, "send_text",
        lambda phone, body: sent.append((phone, body)) or "wamid.X",
    )
    ticket = _make_ticket(session, assigned_to="bob", status=TicketStatus.TOMADO)
    session.add(Message(phone=ticket.phone, direction=Direction.IN, body="hola"))
    session.commit()

    release_ticket(_request("bob"), ticket.id)

    assert sent == []


def test_release_notice_skipped_when_24h_window_closed(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    sent = []
    monkeypatch.setattr(
        dashboard.messaging, "send_text",
        lambda phone, body: sent.append((phone, body)) or "wamid.X",
    )
    # Escalado pero sin ningún mensaje entrante: ventana cerrada.
    ticket = _make_ticket(session, assigned_to="bob", status=TicketStatus.TOMADO, escalado=True)

    release_ticket(_request("bob"), ticket.id)

    session.refresh(ticket)
    assert ticket.assigned_to is None
    assert sent == []


def test_finish_closes_ticket_resets_flow_and_notifies(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    sent = []
    monkeypatch.setattr(
        dashboard.messaging, "send_text",
        lambda phone, body: sent.append(body) or "wamid.FIN",
    )
    ticket = _make_ticket(
        session, assigned_to="bob", status=TicketStatus.EN_PROCESO,
        escalado=True, agent_active=False,
    )
    session.add(Message(phone=ticket.phone, direction=Direction.IN, body="hola"))
    session.add(Conversation(phone=ticket.phone, state="escalado"))
    session.commit()

    finish_ticket(_request("bob"), ticket.id)

    session.refresh(ticket)
    assert ticket.status == TicketStatus.CERRADO
    assert ticket.agent_active is True  # el bot vuelve a cubrir el chat
    conv = session.get(Conversation, ticket.phone)
    session.refresh(conv)
    assert conv.state == "inicio"  # la conversación sale del modo escalado
    assert len(sent) == 1 and "terminada" in sent[0] and "folio #" in sent[0]
    actions = {
        a.action
        for a in session.query(TicketActivity).filter(TicketActivity.ticket_id == ticket.id)
    }
    assert "cerrado" in actions


def test_finish_rejected_for_non_assignee(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    sent = []
    monkeypatch.setattr(
        dashboard.messaging, "send_text",
        lambda phone, body: sent.append(body) or "wamid.X",
    )
    ticket = _make_ticket(session, assigned_to="bob", status=TicketStatus.TOMADO)

    finish_ticket(_request("ana"), ticket.id)

    session.refresh(ticket)
    assert ticket.status == TicketStatus.TOMADO  # sin cambios
    assert sent == []


def test_admin_can_finish_any_ticket(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(dashboard.messaging, "send_text", lambda phone, body: "wamid.F2")
    session.add(User(username="jefa", password_hash="x", is_admin=True))
    ticket = _make_ticket(session, assigned_to="bob", status=TicketStatus.TOMADO)
    session.commit()

    finish_ticket(_request("jefa"), ticket.id)

    session.refresh(ticket)
    assert ticket.status == TicketStatus.CERRADO


def test_duplicate_webhook_message_ignored(session):
    replies_first = handle_incoming(
        session, "5215500002222", "Hola", wa_message_id="wamid.DUP1"
    )
    replies_second = handle_incoming(
        session, "5215500002222", "Hola", wa_message_id="wamid.DUP1"
    )

    assert replies_first
    assert replies_second == []
    stored = session.query(Message).filter(
        Message.wa_message_id == "wamid.DUP1", Message.direction == Direction.IN
    ).all()
    assert len(stored) == 1


def test_el_aviso_y_la_encuesta_se_guardan(monkeypatch, session):
    """REGLA: cerrar un folio deja DOS mensajes salientes en la base — el aviso
    de cierre y la encuesta. Ninguno se puede perder.

    Este test existe por un bug real del PR 2 (multi-oficina): leer una
    credencial con `session=` DESPUÉS del session.add del aviso disparaba el
    autoflush de esa fila, y la sesión anidada que abre send_buttons —bajo el
    pool en memoria de la suite comparte la conexión DBAPI— la deshacía con su
    ROLLBACK al cerrar. El aviso desaparecía sin un solo error, y ningún test
    lo veía porque todos afirmaban sobre la lista `sent`, no sobre las filas.

    Por eso aquí NO se parcha send_buttons: la sesión anidada tiene que correr
    de verdad. Solo se corta la red, que es lo único que no puede pasar.
    """
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(
        dashboard.messaging, "send_text",
        lambda phone, body: "wamid.AVISO",
    )
    # send_buttons SIN parchar: llega hasta messaging.canal_de y whatsapp_client,
    # que abren sus propias sesiones. Se corta la red y nada más.
    monkeypatch.setattr(
        "app.whatsapp_client._post",
        lambda *a, **k: {"messages": [{"id": "wamid.ENCUESTA"}]},
        raising=False,
    )
    ticket = _make_ticket(session, assigned_to="bob", status=TicketStatus.EN_PROCESO)
    session.add(Message(phone=ticket.phone, direction=Direction.IN, body="hola"))
    session.commit()

    finish_ticket(_request("bob"), ticket.id)

    session.expire_all()
    salientes = session.query(Message).filter(
        Message.phone == ticket.phone, Message.direction == Direction.OUT
    ).all()
    cuerpos = [m.body for m in salientes]
    assert len(salientes) == 2, (
        f"se perdió un mensaje saliente al cerrar el folio: {cuerpos}"
    )
    assert any("folio #" in c for c in cuerpos), f"falta el aviso de cierre: {cuerpos}"
    assert any("👍" in c or "1 " in c for c in cuerpos), f"falta la encuesta: {cuerpos}"
