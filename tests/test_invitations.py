from types import SimpleNamespace

import app.db as db
from app.dashboard import invitations_create, invitations_list
from app.models import OFICINA_TLAXCALA_ID, Invitation, Office, User, utcnow


def _request(user: str) -> SimpleNamespace:
    return SimpleNamespace(
        session={"user": user},
        url=SimpleNamespace(path="/invitations"),
        base_url="https://bot.esaumillan.cloud/",
    )


def _sembrar_oficina(session) -> None:
    """La oficina 1 en una base que no pasó por init_db (mismo motivo y misma
    forma que tests/test_users.py)."""
    if session.get(Office, OFICINA_TLAXCALA_ID) is None:
        session.add(Office(id=OFICINA_TLAXCALA_ID, nombre="Tlaxcala", slug="tlaxcala",
                           ciudad="Tlaxcala"))
        session.commit()


def _make_admin(session, username: str) -> None:
    _sembrar_oficina(session)
    session.add(User(username=username, password_hash="x", is_admin=True))
    session.commit()


def test_creates_single_use_code_without_phone(monkeypatch, session):
    """El envío por WhatsApp se eliminó: el código se genera para copiarse
    y compartirse manualmente (botón "Copiar código")."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    _make_admin(session, "ana")

    response = invitations_create(_request("ana"), office_id=OFICINA_TLAXCALA_ID)

    invitation = session.query(Invitation).one()
    assert invitation.created_by == "ana"
    assert invitation.phone is None
    assert invitation.used_at is None
    assert response.context["new_token"] == invitation.token
    assert "Copiar código" in response.body.decode()


def test_invitations_list_shows_pending_and_used(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _make_admin(session, "ana")
    session.add(Invitation(token="pend1", created_by="ana"))
    session.add(Invitation(token="used1", created_by="ana", used_at=utcnow(), used_by="carlos"))
    session.commit()

    response = invitations_list(_request("ana"))

    tokens = {i.token for i in response.context["invitations"]}
    assert tokens == {"pend1", "used1"}
    body = response.body.decode()
    # Solo los códigos sin usar y habilitados muestran el botón de copiar.
    assert body.count('data-code="pend1"') == 1
    assert 'data-code="used1"' not in body
    assert "código copiado" in body


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

    response = invitations_create(_request("colaborador"), office_id=OFICINA_TLAXCALA_ID)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert session.query(Invitation).count() == 0


def test_unknown_user_cannot_access_invitations(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())

    response = invitations_list(_request("fantasma"))

    assert response.status_code == 303
    assert response.headers["location"] == "/"
