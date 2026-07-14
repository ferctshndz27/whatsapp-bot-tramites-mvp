from types import SimpleNamespace

import app.db as db
from app.conversation import handle_incoming
from app.dashboard import claim_ticket, release_ticket, update_ticket_status
from app.models import Direction, Message, Ticket, TicketStatus


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
