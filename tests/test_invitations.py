from types import SimpleNamespace

import app.db as db
from app import whatsapp_client
from app.dashboard import invitations_create, invitations_list
from app.models import Invitation, User, utcnow


def _request(user: str) -> SimpleNamespace:
    return SimpleNamespace(
        session={"user": user},
        url=SimpleNamespace(path="/invitations"),
        base_url="https://bot.esaumillan.cloud/",
    )


def _make_admin(session, username: str) -> None:
    session.add(User(username=username, password_hash="x", is_admin=True))
    session.commit()


def test_creates_invitation_without_phone(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _make_admin(session, "ana")

    response = invitations_create(_request("ana"), phone="")

    invitation = session.query(Invitation).one()
    assert invitation.created_by == "ana"
    assert invitation.phone is None
    assert invitation.used_at is None
    assert response.context["new_token"] == invitation.token
    assert response.context["sent"] is False


def test_creates_invitation_and_sends_whatsapp(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(whatsapp_client, "send_text", lambda phone, body: "wamid.INV1")
    _make_admin(session, "ana")

    response = invitations_create(_request("ana"), phone="5215500001234")

    invitation = session.query(Invitation).one()
    assert invitation.phone == "5215500001234"
    assert response.context["sent"] is True


def test_invitation_send_failure_still_creates_token(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(whatsapp_client, "send_text", lambda phone, body: None)
    _make_admin(session, "ana")

    response = invitations_create(_request("ana"), phone="5215500009999")

    assert session.query(Invitation).count() == 1
    assert response.context["sent"] is False


def test_invitations_list_shows_pending_and_used(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _make_admin(session, "ana")
    session.add(Invitation(token="pend1", created_by="ana"))
    session.add(Invitation(token="used1", created_by="ana", used_at=utcnow(), used_by="carlos"))
    session.commit()

    response = invitations_list(_request("ana"))

    tokens = {i.token for i in response.context["invitations"]}
    assert tokens == {"pend1", "used1"}


def test_non_admin_cannot_list_invitations(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    session.add(User(username="colaborador", password_hash="x", is_admin=False))
    session.commit()

    response = invitations_list(_request("colaborador"))

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_non_admin_cannot_create_invitations(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    session.add(User(username="colaborador", password_hash="x", is_admin=False))
    session.commit()

    response = invitations_create(_request("colaborador"), phone="")

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert session.query(Invitation).count() == 0


def test_unknown_user_cannot_access_invitations(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())

    response = invitations_list(_request("fantasma"))

    assert response.status_code == 303
    assert response.headers["location"] == "/"
