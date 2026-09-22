from types import SimpleNamespace

from sqlmodel import select

import app.db as db
from app.config import settings
from app.dashboard import _code_grants_registration, _validate_registration, register_submit
from app.models import Invitation, User, utcnow


def _request() -> SimpleNamespace:
    # headers/client: el lockout del registro es por IP desde AUTH-2 y lee la IP del request.
    return SimpleNamespace(session={}, base_url="https://bot.esaumillan.cloud/", headers={}, client=None)


def test_short_password_rejected():
    assert "contraseña" in _validate_registration("ana", "corta")


def test_bad_username_rejected():
    assert _validate_registration("a", "12345678") is not None
    assert _validate_registration("ana maria!", "12345678") is not None


def test_valid_username_and_password_passes():
    assert _validate_registration("ana_lopez", "12345678") is None


def test_master_code_grants_registration(monkeypatch, session):
    monkeypatch.setattr(settings, "registration_code", "codigo-real")
    ok, invitation = _code_grants_registration(session, "codigo-real")
    assert ok is True
    assert invitation is None


def test_wrong_code_rejected_without_registration_code(monkeypatch, session):
    monkeypatch.setattr(settings, "registration_code", "")
    ok, invitation = _code_grants_registration(session, "cualquier-cosa")
    assert ok is False
    assert invitation is None


def test_individual_invitation_grants_registration(monkeypatch, session):
    monkeypatch.setattr(settings, "registration_code", "")
    session.add(Invitation(token="tok123", created_by="ana"))
    session.commit()

    ok, invitation = _code_grants_registration(session, "tok123")
    assert ok is True
    assert invitation is not None
    assert invitation.token == "tok123"


def test_used_invitation_is_rejected(monkeypatch, session):
    monkeypatch.setattr(settings, "registration_code", "")
    session.add(Invitation(token="usado1", created_by="ana", used_at=utcnow(), used_by="bob"))
    session.commit()

    ok, invitation = _code_grants_registration(session, "usado1")
    assert ok is False
    assert invitation is None


def test_register_submit_consumes_invitation(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(settings, "registration_code", "")
    session.add(Invitation(token="tok456", created_by="ana"))
    session.commit()

    register_submit(
        _request(), username="carlos", password="clave1234", code="tok456", phone="33 1234 5678"
    )

    user = session.exec(select(User).where(User.username == "carlos")).first()
    assert user is not None
    assert user.phone == "5213312345678"  # se guarda normalizado con el usuario

    invitation = session.exec(select(Invitation).where(Invitation.token == "tok456")).first()
    assert invitation.used_at is not None
    assert invitation.used_by == "carlos"
    assert invitation.phone == "5213312345678"  # el histórico muestra el teléfono


def test_register_submit_rejects_invalid_phone(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(settings, "registration_code", "")
    session.add(Invitation(token="tok999", created_by="ana"))
    session.commit()

    response = register_submit(
        _request(), username="dora", password="clave1234", code="tok999", phone="12345"
    )

    assert session.exec(select(User).where(User.username == "dora")).first() is None
    assert "10 dígitos" in response.body.decode()
    invitation = session.exec(select(Invitation).where(Invitation.token == "tok999")).first()
    assert invitation.used_at is None  # el código sigue disponible


def test_register_submit_rejects_reused_invitation(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(settings, "registration_code", "")
    session.add(Invitation(token="tok789", created_by="ana"))
    session.commit()

    register_submit(
        _request(), username="primero", password="clave1234", code="tok789", phone="3312345678"
    )
    register_submit(
        _request(), username="segundo", password="clave1234", code="tok789", phone="3387654321"
    )

    assert session.exec(select(User).where(User.username == "segundo")).first() is None
