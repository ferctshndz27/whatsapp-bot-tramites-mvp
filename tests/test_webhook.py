import hashlib
import hmac

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

import app.db as db
from app.config import settings
from app.models import Direction, Message
from app.webhook import _process_status, _tag_sent_message, _valid_signature, router


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


# ---------------------------------------------------------------------------
# Handshake GET — caracterización ANTES de que la llave se resuelva por oficina
#
# `verify_webhook` tenía CERO cobertura. Estas cuatro pruebas fijan el
# comportamiento de HOY para que la migración a credenciales.credencial()
# (docs/SPEC-MULTI-OFICINA.md §3) no lo cambie sin querer: la herencia al .env
# tiene que reproducirlo byte por byte, incluido el fail-open de la última.
#
# Todas parchan `settings.verify_token`: conftest.py no lo blanquea, así que sin
# el parche el resultado dependería del .env del desarrollador.
# ---------------------------------------------------------------------------


@pytest.fixture()
def cliente():
    """El handshake GET no toca la base: basta el router montado en un FastAPI
    pelón (patrón de tests/test_stripe_webhook.py), sin los cuatro middlewares
    de app.main ni la siembra del usuario del dashboard."""
    api = FastAPI()
    api.include_router(router)
    return TestClient(api)


def test_handshake_con_token_correcto_devuelve_el_challenge(cliente, monkeypatch):
    monkeypatch.setattr(settings, "verify_token", "token-de-prueba")

    r = cliente.get(
        "/webhook?hub.mode=subscribe&hub.verify_token=token-de-prueba&hub.challenge=RETO123"
    )

    assert r.status_code == 200
    assert r.text == "RETO123"


def test_handshake_con_token_incorrecto_da_403(cliente, monkeypatch):
    monkeypatch.setattr(settings, "verify_token", "token-de-prueba")

    r = cliente.get(
        "/webhook?hub.mode=subscribe&hub.verify_token=el-equivocado&hub.challenge=RETO123"
    )

    assert r.status_code == 403
    assert "RETO123" not in r.text


def test_handshake_sin_modo_subscribe_da_403(cliente, monkeypatch):
    """El token correcto NO alcanza: `hub.mode` tiene que ser "subscribe"."""
    monkeypatch.setattr(settings, "verify_token", "token-de-prueba")

    sin_modo = cliente.get("/webhook?hub.verify_token=token-de-prueba&hub.challenge=RETO123")
    otro_modo = cliente.get(
        "/webhook?hub.mode=unsubscribe&hub.verify_token=token-de-prueba&hub.challenge=RETO123"
    )

    assert sin_modo.status_code == 403
    assert otro_modo.status_code == 403


def test_handshake_sin_credencial_o_con_token_vacio_da_403(cliente, monkeypatch):
    """Antes esta prueba fijaba el fail-open: con `verify_token` vacío, un
    `hub.verify_token=` presente-y-vacío comparaba "" == "" y devolvía 200. Su
    propio docstring dejaba anotado el endurecimiento como pendiente; la
    auditoría 2026-09 (EXT-6) lo cerró: sin credencial configurada no hay
    handshake, y con credencial el token vacío tampoco pasa. Cambiar esta
    prueba ES la tarea."""
    monkeypatch.setattr(settings, "verify_token", "")
    con_vacio = cliente.get("/webhook?hub.mode=subscribe&hub.verify_token=&hub.challenge=RETO123")
    sin_param = cliente.get("/webhook?hub.mode=subscribe&hub.challenge=RETO123")
    assert con_vacio.status_code == 403 and sin_param.status_code == 403

    monkeypatch.setattr(settings, "verify_token", "token-de-prueba")
    vacio_con_credencial = cliente.get("/webhook?hub.mode=subscribe&hub.verify_token=&hub.challenge=RETO123")
    assert vacio_con_credencial.status_code == 403
    assert "RETO123" not in vacio_con_credencial.text