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

import app.dashboard as dashboard
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


def _cpu_de_un_login(cliente: TestClient, cuerpo: bytes) -> tuple[int, float]:
    inicio = time.process_time()
    r = cliente.post("/login", content=cuerpo, headers=URLENCODED)
    return r.status_code, time.process_time() - inicio


def test_un_megabyte_de_puntos_y_comas_a_login_no_cuesta_segundos_de_cpu(monkeypatch):
    """Se mide lo que el cuerpo hostil cuesta DE MÁS que un login normal, no el login.

    Un POST a /login ya cuesta ~0.2 s de CPU aquí con cualquier cuerpo (verificar la clave) y
    ~5 veces eso en Jenkins: el tope absoluto de 1.0 s sobre el login entero cayó en 1.04 s sin
    que el parser tuviera culpa (build #2 de fix/seguridad-cobro-manual). Restar un login de
    control, hecho en la misma máquina y el mismo segundo, deja solo el parser: ~0.01 s sano
    contra ~4 s si vuelve a ser cuadrático.

    Los contadores de bloqueo se vacían: pasados cinco fallos el login contesta sin evaluar la
    clave y la resta saldría negativa —verde por la razón equivocada—."""
    monkeypatch.setattr(dashboard, "_login_attempts", {})
    cliente = TestClient(app, base_url="https://testserver")
    _cpu_de_un_login(cliente, b"username=calentar&password=x")  # imports perezosos, primera conexión
    _, control = _cpu_de_un_login(cliente, b"username=control&password=x")
    estado, bruto = _cpu_de_un_login(cliente, b"a;" * 500_000)
    de_mas = bruto - control
    assert estado < 500, f"el cuerpo malicioso tumbó el handler: {estado}"
    assert de_mas < TOPE_CPU_SEGUNDOS, (
        f"1 MB de «a;» costó {de_mas:.2f} s de CPU más que un login normal ({control:.2f} s): "
        "el parser de formularios volvió a ser cuadrático (python-multipart < 0.0.30) y "
        "bloquea al único worker"
    )


def test_medio_millon_de_campos_se_rechaza_en_vez_de_parsearse():
    cliente = TestClient(app, base_url="https://testserver")
    r = cliente.post("/login", content=b"a&" * 500_000, headers=URLENCODED)
    assert r.status_code == 400, (
        f"request.form() aceptó medio millón de campos ({r.status_code}): el tope de "
        "campos de starlette no aplica a x-www-form-urlencoded (starlette < 1.3.1)"
    )
