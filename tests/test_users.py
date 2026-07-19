from types import SimpleNamespace

import bcrypt
from sqlmodel import select

import app.db as db
from app.models import User
from app.users import (
    users_create,
    users_list,
    users_reset_password,
    users_toggle_enabled,
    users_toggle_role,
)


def _request(user: str | None) -> SimpleNamespace:
    return SimpleNamespace(session={"user": user} if user else {}, url=SimpleNamespace(path="/users"))


def _make_user(session, username: str, is_admin: bool = False, habilitado: bool = True) -> None:
    session.add(
        User(username=username, password_hash="x", is_admin=is_admin, habilitado=habilitado)
    )
    session.commit()


def test_non_admin_is_redirected(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _make_user(session, "colab")

    response = users_list(_request("colab"))

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_anonymous_is_redirected_to_login(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())

    response = users_list(_request(None))

    assert response.headers["location"] == "/login"


def test_admin_sees_user_list(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _make_user(session, "jefa", is_admin=True)
    _make_user(session, "colab")

    response = users_list(_request("jefa"))

    usernames = [u.username for u in response.context["users"]]
    assert usernames == ["colab", "jefa"]


def test_admin_creates_user_with_bcrypt_hash(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _make_user(session, "jefa", is_admin=True)

    users_create(_request("jefa"), username="Nuevo_Colab", password="secreta123")

    created = session.exec(select(User).where(User.username == "nuevo_colab")).one()
    assert created.is_admin is False
    assert created.habilitado is True
    assert bcrypt.checkpw(b"secreta123", created.password_hash.encode())


def test_create_rejects_duplicate_username(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _make_user(session, "jefa", is_admin=True)
    _make_user(session, "colab")

    response = users_create(_request("jefa"), username="colab", password="secreta123")

    assert "ya existe" in response.context["error"]


def test_create_rejects_short_password(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _make_user(session, "jefa", is_admin=True)

    response = users_create(_request("jefa"), username="valido", password="corta")

    assert response.context["error"] is not None
    assert session.exec(select(User).where(User.username == "valido")).first() is None


def test_reset_password_updates_hash(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _make_user(session, "jefa", is_admin=True)
    _make_user(session, "colab")

    users_reset_password(_request("jefa"), "colab", password="nueva-clave-1")

    updated = session.exec(select(User).where(User.username == "colab")).one()
    session.refresh(updated)
    assert bcrypt.checkpw(b"nueva-clave-1", updated.password_hash.encode())


def test_toggle_disables_and_enables(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _make_user(session, "jefa", is_admin=True)
    _make_user(session, "colab")

    users_toggle_enabled(_request("jefa"), "colab")
    target = session.exec(select(User).where(User.username == "colab")).one()
    session.refresh(target)
    assert target.habilitado is False

    users_toggle_enabled(_request("jefa"), "colab")
    session.refresh(target)
    assert target.habilitado is True


def test_admin_cannot_disable_self(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _make_user(session, "jefa", is_admin=True)

    response = users_toggle_enabled(_request("jefa"), "jefa")

    assert "ti mismo" in response.context["error"]
    jefa = session.exec(select(User).where(User.username == "jefa")).one()
    session.refresh(jefa)
    assert jefa.habilitado is True


def test_admin_cannot_change_own_role(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _make_user(session, "jefa", is_admin=True)

    response = users_toggle_role(_request("jefa"), "jefa")

    assert "propio rol" in response.context["error"]


def test_role_toggle_promotes_and_demotes(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _make_user(session, "jefa", is_admin=True)
    _make_user(session, "colab")

    users_toggle_role(_request("jefa"), "colab")
    target = session.exec(select(User).where(User.username == "colab")).one()
    session.refresh(target)
    assert target.is_admin is True
