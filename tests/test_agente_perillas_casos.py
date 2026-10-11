"""/agente muestra las perillas de casos-analisis con su matriz y las guarda (tarea 19, diseño §5.5).

Viven en el panel del nodo «Asesor humano»: las dos reglas terminan en un asesor, y así no cambia
la cadena de flechas del lienzo. Si se pone roja, el admin no tiene dónde encender (o apagar en una
emergencia) la regla del folio sin tocar el .env y recrear el contenedor.
"""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

import app.db as db
from app import agent_settings
from app.config import settings
from app.main import app
from app.models import AgentConfig, User

CLAVE = "clave-de-prueba-1234"
RAIZ = Path(__file__).resolve().parents[1]
PERILLAS = ("contexto_folio_enabled", "revision_enabled", "contexto_min_palabras")


@pytest.fixture(autouse=True)
def entorno(_isolated_db, monkeypatch):
    import bcrypt

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "geoip_enabled", False)
    monkeypatch.setattr(settings, "contexto_folio_enabled", False)
    monkeypatch.setattr(settings, "revision_enabled", False)
    monkeypatch.setattr(settings, "contexto_min_palabras", 40)
    clave = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()
    with Session(db.engine) as s:
        s.add(User(username="admin", password_hash=clave, is_admin=True))
        s.add(User(username="colab", password_hash=clave, is_admin=False))
        s.commit()


def _admin() -> TestClient:
    c = TestClient(app, base_url="https://testserver")
    assert c.post("/login", data={"username": "admin", "password": CLAVE}, follow_redirects=False).status_code == 303
    return c


def _datos(html: str) -> dict:
    crudo = re.search(r'<script type="application/json" id="agente-datos">(.*?)</script>', html, re.S).group(1)
    return json.loads(crudo)["estado"]


def test_la_pagina_publica_las_tres_apagadas_y_la_matriz():
    estado = _datos(_admin().get("/agente").text)

    for clave in PERILLAS:
        assert clave in estado["config"], clave
    assert estado["config"]["contexto_folio_enabled"]["valor"] is False
    assert estado["config"]["revision_enabled"]["valor"] is False
    assert estado["config"]["contexto_min_palabras"]["valor"] == 40

    matriz = estado["matriz_casos"]
    assert len(matriz) == 5
    assert matriz[0][:2] == ["OFF", "OFF"] and "hoy" in matriz[0][2]
    assert "en vuelo" in matriz[4][0] or "en vuelo" in matriz[4][2]


def test_el_nodo_asesor_humano_lleva_las_perillas():
    estado = _datos(_admin().get("/agente").text)
    nodo = next(n for n in estado["nodos"] if n["id"] == "escalada")
    assert nodo["perillas"] == list(PERILLAS)


def test_el_lienzo_pinta_las_perillas_y_la_matriz():
    """El JS no tiene pruebas unitarias: se fija que lea lo que el servidor publica."""
    js = (RAIZ / "static/js/agente.js").read_text(encoding="utf-8")
    assert "n.perillas" in js
    assert "ESTADO.matriz_casos" in js


def test_guardar_una_cambia_agentconfig_y_settings():
    c = _admin()

    r = c.post("/agente/config", json={"key": "contexto_folio_enabled", "value": "1"})
    assert r.status_code == 200
    r = c.post("/agente/config", json={"key": "contexto_min_palabras", "value": "60"})
    assert r.status_code == 200

    with Session(db.engine) as s:
        assert s.get(AgentConfig, "contexto_folio_enabled").value == "1"
        assert s.get(AgentConfig, "contexto_min_palabras").value == "60"
    assert settings.contexto_folio_enabled is True
    assert settings.contexto_min_palabras == 60


def test_guardar_fuera_de_rango_se_rechaza():
    r = _admin().post("/agente/config", json={"key": "contexto_min_palabras", "value": "500"})
    assert r.status_code == 400
    assert settings.contexto_min_palabras == 40


def test_sin_sesion_o_sin_admin_se_rechaza():
    anonimo = TestClient(app, base_url="https://testserver")
    assert anonimo.post("/agente/config", json={"key": "revision_enabled", "value": "1"}).status_code in (401, 403)
    colab = TestClient(app, base_url="https://testserver")
    colab.post("/login", data={"username": "colab", "password": CLAVE}, follow_redirects=False)
    assert colab.post("/agente/config", json={"key": "revision_enabled", "value": "1"}).status_code == 403
    assert settings.revision_enabled is False
    assert agent_settings.CONFIG_EDITABLE["revision_enabled"].tipo == "bool"
