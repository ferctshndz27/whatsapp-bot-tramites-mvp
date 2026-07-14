import hashlib
import hmac

from sqlmodel import select

import app.db as db
from app.config import settings
from app.models import Direction, Message
from app.webhook import _process_status, _tag_sent_message, _valid_signature


def test_valid_signature_is_accepted(monkeypatch):
    monkeypatch.setattr(settings, "app_secret", "test-secret")
    body = b'{"hello":"world"}'
    sig = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    assert _valid_signature(body, sig) is True


def test_tampered_body_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "app_secret", "test-secret")
    sig = "sha256=" + hmac.new(b"test-secret", b'{"hello":"world"}', hashlib.sha256).hexdigest()
    assert _valid_signature(b'{"hello":"tampered"}', sig) is False


def test_missing_signature_header_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "app_secret", "test-secret")
    assert _valid_signature(b"{}", "") is False


def test_missing_app_secret_rejects_everything(monkeypatch):
    monkeypatch.setattr(settings, "app_secret", "")
    body = b"{}"
    sig = "sha256=" + hmac.new(b"anything", body, hashlib.sha256).hexdigest()
    assert _valid_signature(body, sig) is False


def test_status_update_is_tagged_and_recorded(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())

    session.add(Message(phone="5215500000000", direction=Direction.OUT, body="hola"))
    session.commit()

    _tag_sent_message("5215500000000", "wamid.ABC123")
    _process_status({"id": "wamid.ABC123", "status": "delivered", "timestamp": "1700000000"})

    row = session.exec(select(Message).where(Message.phone == "5215500000000")).one()
    assert row.wa_message_id == "wamid.ABC123"
    assert row.wa_status == "delivered"
    assert row.wa_status_at is not None
