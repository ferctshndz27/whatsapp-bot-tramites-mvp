from app.config import settings
from app.dashboard import _validate_registration


def test_registration_disabled_without_code(monkeypatch):
    monkeypatch.setattr(settings, "registration_code", "")
    assert "deshabilitado" in _validate_registration("ana", "12345678", "")


def test_wrong_invite_code_rejected(monkeypatch):
    monkeypatch.setattr(settings, "registration_code", "codigo-real")
    assert "incorrecto" in _validate_registration("ana", "12345678", "codigo-falso")


def test_short_password_rejected(monkeypatch):
    monkeypatch.setattr(settings, "registration_code", "codigo-real")
    assert "contraseña" in _validate_registration("ana", "corta", "codigo-real")


def test_bad_username_rejected(monkeypatch):
    monkeypatch.setattr(settings, "registration_code", "codigo-real")
    assert _validate_registration("a", "12345678", "codigo-real") is not None
    assert _validate_registration("ana maria!", "12345678", "codigo-real") is not None


def test_valid_registration_passes(monkeypatch):
    monkeypatch.setattr(settings, "registration_code", "codigo-real")
    assert _validate_registration("ana_lopez", "12345678", "codigo-real") is None
