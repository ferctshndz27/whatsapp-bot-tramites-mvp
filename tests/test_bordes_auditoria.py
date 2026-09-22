"""Bordes menores de la auditoría 2026-09, cerrados en un solo grupo: AUTHZ-5,
INJ-5, INJ-6, INJ-8 y el `/health` asíncrono de IA-4. (EXT-6 vive en
tests/test_webhook.py junto a las otras pruebas del handshake; INJ-3 se cerró
con INJ-1 y su prueba está en tests/test_navegacion.py.)

Cada prueba dice lo que se rompía en el negocio si vuelve a rojo.
"""

from __future__ import annotations

import inspect
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import create_engine

import app.db as db
import app.main as main
from app import media_store
from app.clients import _ERRORES
from app.config import settings
from app.db import get_session
from app.main import SECURITY_HEADERS, app
from app.models import User
from app.my import set_estado


# ---------------------------------------------------------------------------
# AUTHZ-5 — /my/estado vuelve solo a rutas del panel
# ---------------------------------------------------------------------------


def _request_estado(referer: str | None) -> SimpleNamespace:
    return SimpleNamespace(session={"user": "ana"}, headers={"referer": referer} if referer is not None else {})


@pytest.fixture()
def con_ana(_isolated_db, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    with get_session() as s:
        s.add(User(username="ana", password_hash="x"))
        s.commit()


@pytest.mark.parametrize(
    "referer, destino",
    [
        ("https://bot.ejemplo.mx/crm?chat=521", "/crm?chat=521"),
        ("https://bot.ejemplo.mx/tickets/7", "/tickets/7"),
        ("https://evil.example/robar", "/robar"),  # se descarta el host: solo la ruta
        ("//evil.example/x", "/x"),  # protocolo-relativa: el host se descarta, queda la ruta
        ("javascript:alert(1)", "/my"),
        (None, "/my"),
    ],
)
def test_cambiar_de_estado_regresa_solo_a_una_ruta_del_panel(con_ana, referer, destino):
    """Era la única redirección del panel que no pasaba por `utils.ruta_interna`:
    devolvía el Referer entero. El navegador lo pone, así que la explotación es
    limitada, pero el patrón tiene un solo dueño."""
    r = set_estado(_request_estado(referer), estado="ocupado")
    assert r.status_code == 303 and r.headers["location"] == destino


# ---------------------------------------------------------------------------
# INJ-5 — /clients?error= es un código, no texto libre
# ---------------------------------------------------------------------------


@pytest.fixture()
def panel(_isolated_db, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "geoip_enabled", False)
    import bcrypt
    with get_session() as s:
        s.add(User(username="jefa", password_hash=bcrypt.hashpw(b"clave-de-prueba-1234", bcrypt.gensalt(rounds=4)).decode(), is_admin=True))
        s.commit()
    cliente = TestClient(app, base_url="https://testserver")
    r = cliente.post("/login", data={"username": "jefa", "password": "clave-de-prueba-1234"}, follow_redirects=False)
    assert r.status_code == 303
    return cliente


def test_un_texto_libre_en_error_no_se_pinta_en_el_panel(panel):
    """`/clients?error=Tu sesión expiró, entra en https://evil.example` se
    mostraba —escapado, sin XSS— dentro del panel: un enlace bastaba para
    suplantar un aviso del sistema. Ahora solo los códigos conocidos hablan."""
    r = panel.get("/clients?error=Tu%20sesi%C3%B3n%20expir%C3%B3,%20entra%20en%20evil.example")
    assert r.status_code == 200
    assert "evil.example" not in r.text and "expiró" not in r.text

    r = panel.get("/clients?error=telefono")
    assert _ERRORES["telefono"] in r.text, "el código conocido dejó de traducirse"


def test_las_altas_invalidas_siguen_avisando_por_codigo(panel):
    r = panel.post("/clients", data={"phone": "12", "name": "x"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/clients?error=telefono"
    r = panel.post("/clients", data={"phone": "3312345678", "name": ""}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/clients?error=nombre"


# ---------------------------------------------------------------------------
# INJ-6 — el 500 también sale con cabeceras de seguridad
# ---------------------------------------------------------------------------


def test_un_500_lleva_las_mismas_cabeceras_que_cualquier_respuesta(monkeypatch):
    """ServerErrorMiddleware envuelve a todos los middlewares, incluido el de
    cabeceras: el 500 salía sin CSP ni X-Frame-Options. El cuerpo sigue fijo."""
    import app.dashboard as dashboard

    def _revienta(*_a, **_k):
        raise RuntimeError("simulado")

    monkeypatch.setattr(dashboard.templates, "TemplateResponse", _revienta)
    cliente = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    r = cliente.get("/login")
    assert r.status_code == 500
    for nombre, valor in SECURITY_HEADERS.items():
        assert r.headers.get(nombre) == valor, f"el 500 no lleva {nombre}"
    assert "simulado" not in r.text and "Traceback" not in r.text


def test_health_es_asincrono_para_no_pelear_el_threadpool():
    """Un `def` manda /health al threadpool que comparte con el webhook y el
    panel; con el pool saturado por Ollama, el cron del deploy leía «caído» un
    servicio que solo estaba ocupado, y hacía rollback de un commit sano."""
    assert inspect.iscoroutinefunction(main.health)


# ---------------------------------------------------------------------------
# INJ-8 — los bytes tienen que coincidir con el tipo declarado
# ---------------------------------------------------------------------------


def _subida(nombre: str, tipo: str, datos: bytes) -> UploadFile:
    return UploadFile(filename=nombre, file=BytesIO(datos), headers={"content-type": tipo})


@pytest.mark.parametrize(
    "tipo, datos",
    [
        ("application/pdf", b"<html><script>alert(1)</script>"),
        ("image/png", b"%PDF-1.4 disfrazado"),
        ("image/jpeg", b"GIF89a"),
        ("image/webp", b"RIFF\x00\x00\x00\x00WAVE"),  # RIFF pero no WEBP
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"%PDF-1.4"),
    ],
)
def test_un_archivo_disfrazado_se_rechaza_como_tipo(tmp_path, monkeypatch, tipo, datos):
    """El Content-Type lo declara quien sube; los primeros bytes no. Un HTML con
    «application/pdf» entraba al almacén de documentos de clientes."""
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    ruta, disco, error = media_store.save_upload(_subida("acta.pdf", tipo, datos))
    assert (ruta, disco, error) == (None, None, "tipo")
    assert not list(tmp_path.iterdir()), "el archivo disfrazado se escribió igual"


@pytest.mark.parametrize(
    "tipo, datos",
    [
        ("application/pdf", b"%PDF-1.4 contenido"),
        ("image/png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 8),
        ("image/jpeg", b"\xff\xd8\xff\xe0" + b"\x00" * 8),
        ("image/webp", b"RIFF\x10\x00\x00\x00WEBPVP8 "),
        ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", b"PK\x03\x04" + b"\x00" * 8),
    ],
)
def test_un_archivo_legitimo_sigue_entrando(tmp_path, monkeypatch, tipo, datos):
    """Control positivo: sin él, un chequeo que rechazara TODO pasaría la de arriba."""
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    ruta, disco, error = media_store.save_upload(_subida("archivo", tipo, datos))
    assert error is None and ruta and disco
    assert len(list(tmp_path.iterdir())) == 1
