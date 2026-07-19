from types import SimpleNamespace

from sqlmodel import select

import app.db as db
from app.clients import add_client_note, client_profile, delete_client_note
from app.kb import knowledge_base
from app.models import Client, ClientNote, FollowUp, SavedReply, Ticket, User
from app.my import complete_followup, create_followup, my_day, set_estado
from app.replies import replies_create, replies_delete

PHONE = "5215500002222"


def _request(user: str | None, path: str = "/my") -> SimpleNamespace:
    return SimpleNamespace(
        session={"user": user} if user else {},
        url=SimpleNamespace(path=path),
        headers={"referer": "/my"},
    )


def _seed(session) -> Ticket:
    session.add(User(username="ana", password_hash="x"))
    session.add(Client(phone=PHONE, name="María"))
    ticket = Ticket(
        phone=PHONE, category="Servicios básicos", service_id="curp",
        service_name="CURP certificada", price_info="$80", assigned_to="ana",
    )
    session.add(ticket)
    session.commit()
    session.refresh(ticket)
    return ticket


def test_followup_roundtrip(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    ticket = _seed(session)

    create_followup(_request("ana"), ticket.id, days=0, note="recontactar hoy")

    view = my_day(_request("ana"))
    assert len(view.context["due_now"]) == 1
    followup = session.exec(select(FollowUp)).one()
    assert followup.note == "recontactar hoy"

    complete_followup(_request("ana"), followup.id)
    session.refresh(followup)
    assert followup.done_at is not None
    assert my_day(_request("ana")).context["due_now"] == []


def test_followup_not_completable_by_other_user(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    ticket = _seed(session)
    create_followup(_request("ana"), ticket.id, days=1, note="x")
    followup = session.exec(select(FollowUp)).one()

    complete_followup(_request("bob"), followup.id)

    session.refresh(followup)
    assert followup.done_at is None  # sigue pendiente


def test_estado_change_and_whitelist(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _seed(session)

    set_estado(_request("ana"), estado="ocupado")
    ana = session.exec(select(User).where(User.username == "ana")).one()
    session.refresh(ana)
    assert ana.estado == "ocupado"

    set_estado(_request("ana"), estado="hackeado")  # fuera de la whitelist
    session.refresh(ana)
    assert ana.estado == "ocupado"  # sin cambios


def test_client_note_add_and_soft_delete(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _seed(session)

    add_client_note(_request("ana"), PHONE, body="ya pagó anticipo")
    note = session.exec(select(ClientNote)).one()
    assert note.author == "ana"

    view = client_profile(_request("ana", f"/clients/{PHONE}"), PHONE)
    assert len(view.context["notes"]) == 1

    delete_client_note(_request("ana"), PHONE, note.id)
    session.refresh(note)
    assert note.habilitado is False
    view = client_profile(_request("ana", f"/clients/{PHONE}"), PHONE)
    assert view.context["notes"] == []


def test_saved_reply_create_and_owner_only_delete(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _seed(session)

    replies_create(_request("ana"), title="saludo", body="Hola, gracias por escribir")
    reply = session.exec(select(SavedReply)).one()
    assert reply.username == "ana"

    replies_delete(_request("bob"), reply.id)  # ajeno: no borra
    session.refresh(reply)
    assert reply.habilitado is True

    replies_delete(_request("ana"), reply.id)
    session.refresh(reply)
    assert reply.habilitado is False


def test_kb_requires_login_and_lists_catalog(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())

    anon = knowledge_base(_request(None, "/kb"))
    assert anon.headers["location"] == "/login"

    view = knowledge_base(_request("ana", "/kb"))
    assert len(view.context["categories"]) > 0
