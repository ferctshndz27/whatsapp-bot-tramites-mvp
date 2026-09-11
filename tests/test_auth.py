from sqlmodel import select

import app.db as db
from app.auth import is_admin
from app.config import settings
from app.dashboard import seed_dashboard_user
from app.models import User


def test_is_admin_false_for_unknown_user(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    assert is_admin("nadie") is False


def test_is_admin_false_for_missing_username():
    assert is_admin(None) is False
    assert is_admin("") is False


def test_is_admin_true_for_flagged_user(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    session.add(User(username="jefa", password_hash="x", is_admin=True))
    session.commit()

    assert is_admin("jefa") is True


def test_is_admin_false_for_regular_collaborator(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    session.add(User(username="colega", password_hash="x", is_admin=False))
    session.commit()

    assert is_admin("colega") is False


def test_seed_dashboard_user_creates_admin(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(settings, "dashboard_username", "admin")
    monkeypatch.setattr(settings, "dashboard_password", "clave-segura-1234")

    seed_dashboard_user(settings)

    user = session.exec(select(User).where(User.username == "admin")).one()
    assert user.is_admin is True


def test_seed_dashboard_user_promotes_existing_user(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(settings, "dashboard_username", "admin")
    monkeypatch.setattr(settings, "dashboard_password", "clave-segura-1234")
    session.add(User(username="admin", password_hash="hash-viejo", is_admin=False))
    session.commit()

    seed_dashboard_user(settings)

    user = session.exec(select(User).where(User.username == "admin")).one()
    assert user.is_admin is True
    assert user.password_hash == "hash-viejo"  # no se toca la contraseña ya existente
