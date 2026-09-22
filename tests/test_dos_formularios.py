"""Un formulario grande y malicioso no se lleva la CPU del único worker.

Auditoría 2026-09, INFRA-2. `python-multipart` 0.0.20 parseaba «a;a;a;…» en
tiempo cuadrático (CVE-2026-53539): 1 MB a POST /login —sin sesión— costaba
3.97 s de CPU medidos en este repo (4.77 s en la auditoría), y como uvicorn
corre UN worker, el webhook de WhatsApp no contestaba mientras tanto. Con
0.0.32 el mismo cuerpo cuesta 0.05 s. La cota de abajo está 20× por encima del
arreglo y 40× por debajo de la falla: no mide rendimiento, mide que la
dependencia no se haya vuelto a bajar.

CPU del proceso y no reloj de pared a propósito: la suite corre bajo carga y
un `sleep` ajeno no debe poner esto en rojo; el parser sí gasta CPU.

Y el segundo caso es la otra CVE del mismo pin de starlette (CVE-2026-54283):
`request.form()` tiene un tope de campos que en x-www-form-urlencoded se
ignoraba. Medio millón de campos ahora es un 400, no un formulario parseado.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import create_engine

import app.db as db
from app.config import settings
from app.main import app

TOPE_CPU_SEGUNDOS = 1.0
URLENCODED = {"content-type": "application/x-www-form-urlencoded"}


@pytest.fixture(autouse=True)
def _base_para_el_otro_hilo(_isolated_db, monkeypatch):
    """El TestClient atiende en otro hilo y el `sqlite://` de `_isolated_db` le
    daría a ese hilo una base VACÍA («no such table: user»): el handler moriría
    con 500 y la prueba mediría eso, no el parser."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "geoip_enabled", False)


def test_un_megabyte_de_puntos_y_comas_a_login_no_cuesta_segundos_de_cpu():
    cliente = TestClient(app, base_url="https://testserver")
    inicio = time.process_time()
    r = cliente.post("/login", content=b"a;" * 500_000, headers=URLENCODED)
    cpu = time.process_time() - inicio
    assert r.status_code < 500, f"el cuerpo malicioso tumbó el handler: {r.status_code}"
    assert cpu < TOPE_CPU_SEGUNDOS, (
        f"1 MB de «a;» costó {cpu:.2f} s de CPU: el parser de formularios volvió a ser "
        "cuadrático (python-multipart < 0.0.30) y bloquea al único worker"
    )


def test_medio_millon_de_campos_se_rechaza_en_vez_de_parsearse():
    cliente = TestClient(app, base_url="https://testserver")
    r = cliente.post("/login", content=b"a&" * 500_000, headers=URLENCODED)
    assert r.status_code == 400, (
        f"request.form() aceptó medio millón de campos ({r.status_code}): el tope de "
        "campos de starlette no aplica a x-www-form-urlencoded (starlette < 1.3.1)"
    )
