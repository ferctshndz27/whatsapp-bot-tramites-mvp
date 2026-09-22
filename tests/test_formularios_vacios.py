"""Un campo de formulario vacío llega al handler, que contesta con SU mensaje.

Desde fastapi 0.116 un `str = Form(...)` recibe `""` y lo trata como campo
AUSENTE: la respuesta es un JSON 422 crudo («Field required») en vez del
formulario repintado con «Nombre y ciudad son obligatorios». Con 0.115.6 el
vacío sí llegaba al handler. Al subir los pines por las CVE de `starlette` y
`python-multipart` (auditoría 2026-09, INFRA-2) se conservó el comportamiento
del panel con `str = Form("")`: el vacío llega, el handler lo valida como
siempre, y un campo ausente del todo —que un formulario del navegador nunca
manda— pasa de 422 a `""`, que el handler trata igual que el vacío.

`/login` es el caso que más importa: con `Form(...)` una contraseña vacía ya
no llegaba a `login_submit`, así que no quedaba LoginEvent y el lockout no
contaba el intento.

La primera prueba enumera las rutas de la app para que el handler que se
escriba mañana no reintroduzca el 422 sin que nadie lo note; la segunda es el
comportamiento, por HTTP, en la forma que la auditoría del panel ya usa.
"""

from __future__ import annotations

import bcrypt
from fastapi import params
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import create_engine, select

import app.db as db
from app.config import settings
from app.db import get_session
from app.main import app
from app.models import LoginEvent, User

CLAVE = "clave-de-prueba-1234"


def test_ningun_campo_de_texto_obligatorio_deja_al_navegador_con_un_422():
    """Estático: si esto se pone rojo, el campo señalado contesta JSON crudo
    cuando el usuario del panel lo manda vacío. Los `int`/`bool` no entran:
    un vacío ahí ya era 422 antes y el navegador no puede mandar otra cosa."""
    culpables = []
    for ruta in app.routes:
        if not isinstance(ruta, APIRoute):
            continue
        for campo in ruta.dependant.body_params:
            if not isinstance(campo.field_info, params.Form):
                continue
            if campo.field_info.is_required() and campo.field_info.annotation is str:
                culpables.append(f"{sorted(ruta.methods)[0]} {ruta.path} · {campo.name}")
    assert not culpables, (
        "campos `str = Form(...)`: un vacío contesta 422 en vez del formulario con su mensaje. "
        "Decláralos `str = Form(\"\")` y valida el vacío en el handler:\n  " + "\n  ".join(culpables)
    )


def test_una_contrasena_vacia_en_login_sigue_dejando_rastro(monkeypatch):
    """El comportamiento, no la declaración: el vacío llega a `login_submit`,
    que responde con el error genérico de siempre y deja el LoginEvent que el
    lockout cuenta. Con un 422 del framework no habría ni lo uno ni lo otro."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "geoip_enabled", False)
    with get_session() as s:
        s.add(User(username="ana", password_hash=bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()))
        s.commit()

    cliente = TestClient(app, base_url="https://testserver")
    r = cliente.post("/login", data={"username": "ana", "password": ""}, follow_redirects=False)

    assert r.status_code == 200 and "detail" not in r.text, (
        "la contraseña vacía no llegó al handler: el navegador recibió el 422 del framework"
    )
    with get_session() as s:
        intentos = s.exec(select(LoginEvent).where(LoginEvent.username == "ana")).all()
    assert intentos and not any(e.success for e in intentos), (
        "el intento con contraseña vacía no dejó LoginEvent: el lockout no lo cuenta"
    )
