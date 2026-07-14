from types import SimpleNamespace

import bcrypt

import app.db as db
from app.dashboard import add_ticket_note, reassign_ticket
from app.models import Ticket, TicketStatus, User


def _request(user: str) -> SimpleNamespace:
    return SimpleNamespace(session={"user": user})


def _seed(session) -> Ticket:
    for name in ("ana", "bob"):
        session.add(
            User(username=name, password_hash=bcrypt.hashpw(b"x" * 8, bcrypt.gensalt()).decode())
        )
    ticket = Ticket(
        phone="5215500003333",
        category="Servicios básicos",
        service_id="curp",
        service_name="CURP certificada",
        price_info="$80",
        assigned_to="ana",
        status=TicketStatus.TOMADO,
    )
    session.add(ticket)
    session.commit()
    session.refresh(ticket)
    return ticket


def test_reassign_transfers_with_context_note(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    ticket = _seed(session)

    reassign_ticket(_request("ana"), ticket.id, to_user="bob", note="Cliente ya pagó, falta entregar")

    session.refresh(ticket)
    assert ticket.assigned_to == "bob"
    assert "ana" in ticket.notes and "falta entregar" in ticket.notes


def test_reassign_rejected_if_not_assignee(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    ticket = _seed(session)

    reassign_ticket(_request("bob"), ticket.id, to_user="bob", note="")

    session.refresh(ticket)
    assert ticket.assigned_to == "ana"


def test_note_appended_by_assignee_only(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    ticket = _seed(session)

    add_ticket_note(_request("bob"), ticket.id, note="no soy el asignado")
    session.refresh(ticket)
    assert ticket.notes is None

    add_ticket_note(_request("ana"), ticket.id, note="avance registrado")
    session.refresh(ticket)
    assert "avance registrado" in ticket.notes
