"""Pago de mensualidades (F5) y exportación .xlsx (F6): quién puede, qué sale y qué NO sale.

Datos sintéticos: nada real. Los CURP/NSS/RFC de aquí son inventados.
"""

import io
import zipfile
from datetime import date, datetime

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

import app.db as db
from app import mensualidades as dom
from app import mensualidades_export as exp
from app.config import settings
from app.main import app
from app.models import (
    AdminAudit,
    ClienteMensualidad,
    CuentaCobranza,
    Mensualidad,
    MensualidadAsesor,
    OFICINA_TLAXCALA_ID,
    User,
)

HOY = date(2026, 11, 18)
CLAVE = "clave-de-prueba"
HASH_CLAVE = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()
CURP = "XXXX010101HDFABC09"


@pytest.fixture()
def base(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "geoip_enabled", False)
    monkeypatch.setattr(dom, "hoy_mx", lambda ahora=None: HOY)
    with Session(engine) as s:
        for nombre, admin, oficina in (("ana", False, 1), ("beto", False, 1), ("cris", False, 1),
                                       ("root", True, 1), ("otra", False, 2)):
            s.add(User(username=nombre, password_hash=HASH_CLAVE, is_admin=admin, office_id=oficina))
        c = CuentaCobranza(codigo="7", nombre="Titular Siete", importe_centavos=150000, dia_pago=20,
                           primer_pago=date(2026, 10, 20), office_id=OFICINA_TLAXCALA_ID)
        s.add(c)
        s.flush()
        s.add(ClienteMensualidad(nombre="Persona Uno", cuenta_id=c.id, curp=CURP, nss="12345678901",
                                 rfc="XXXX010101AB1"))
        s.add(MensualidadAsesor(cuenta_id=c.id, username="ana", rol="responsable"))
        s.add(MensualidadAsesor(cuenta_id=c.id, username="beto", rol="colaborador"))
        s.add(Mensualidad(cuenta_id=c.id, periodo="2026-11", vence_el=date(2026, 11, 20), importe_centavos=150000))
        s.commit()
    return engine


def _entrar(usuario):
    http = TestClient(app, base_url="https://testserver")
    r = http.post("/login", data={"username": usuario, "password": CLAVE}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return http


def _mid(engine):
    with Session(engine) as s:
        return s.exec(select(Mensualidad.id)).one()


def _m(engine):
    with Session(engine) as s:
        return s.exec(select(Mensualidad)).one()


def test_colaborador_registra_el_pago_y_queda_quien(base):
    r = _entrar("beto").post(f"/mensualidades/{_mid(base)}/pago", data={"mes": "2026-11"}, follow_redirects=False)
    assert r.status_code == 303 and "pagada=1" in r.headers["location"]
    m = _m(base)
    assert m.pagado_at is not None and m.pagado_por == "beto"


def test_el_segundo_pago_simultaneo_recibe_ya_pagada_y_no_pisa(base):
    mid = _mid(base)
    _entrar("ana").post(f"/mensualidades/{mid}/pago", data={}, follow_redirects=False)
    r = _entrar("beto").post(f"/mensualidades/{mid}/pago", data={}, follow_redirects=False)
    assert "err=ya-pagada" in r.headers["location"]
    assert _m(base).pagado_por == "ana"


def test_quien_no_tiene_la_cuenta_recibe_404_y_no_paga(base):
    for u in ("cris", "otra"):
        r = _entrar(u).post(f"/mensualidades/{_mid(base)}/pago", data={}, follow_redirects=False)
        assert r.status_code == 404
    assert _m(base).pagado_at is None


def test_sin_sesion_redirige_al_login(base):
    http = TestClient(app, base_url="https://testserver")
    r = http.post(f"/mensualidades/{_mid(base)}/pago", data={}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert _m(base).pagado_at is None


def test_deshacer_solo_responsable_o_admin(base):
    mid = _mid(base)
    _entrar("beto").post(f"/mensualidades/{mid}/pago", data={}, follow_redirects=False)
    r = _entrar("beto").post(f"/mensualidades/{mid}/pago/deshacer", data={}, follow_redirects=False)
    assert "err=permiso-deshacer" in r.headers["location"] and _m(base).pagado_at is not None
    r = _entrar("ana").post(f"/mensualidades/{mid}/pago/deshacer", data={}, follow_redirects=False)
    assert "pagada=0" in r.headers["location"]
    m = _m(base)
    assert m.pagado_at is None and m.pagado_por is None
    # Deshacer lo que no está pagado no inventa nada.
    r = _entrar("root").post(f"/mensualidades/{mid}/pago/deshacer", data={}, follow_redirects=False)
    assert "err=no-pagada" in r.headers["location"]


def test_el_admin_paga_y_queda_en_bitacora_sin_importes(base):
    _entrar("root").post(f"/mensualidades/{_mid(base)}/pago", data={}, follow_redirects=False)
    with Session(base) as s:
        a = s.exec(select(AdminAudit).where(AdminAudit.action == "mensualidades-pago")).one()
    assert a.target == "cuenta:7 periodo:2026-11" and "1500" not in a.target


def test_la_vista_muestra_boton_y_mes_destino_no_inyecta(base):
    html = _entrar("ana").get("/mensualidades?mes=2026-11").text
    assert "Registrar pago" in html and "Exportar a Excel" in html
    r = _entrar("ana").post(f"/mensualidades/{_mid(base)}/pago", data={"mes": "<script>"}, follow_redirects=False)
    assert "<script>" not in r.headers["location"] and "mes=2026-11" in r.headers["location"]
    html = _entrar("ana").get("/mensualidades?mes=2026-11&err=<b>x</b>").text
    assert "<b>x</b>" not in html


# ---- exportación ---------------------------------------------------------------------------

def _xlsx(resp):
    z = zipfile.ZipFile(io.BytesIO(resp.content))
    assert z.testzip() is None
    return {n: z.read(n).decode() for n in z.namelist()}


def test_export_es_xlsx_real_y_omite_pii_por_omision(base):
    r = _entrar("ana").get("/mensualidades/export.xlsx")
    assert r.status_code == 200 and r.headers["content-type"].startswith(exp.MIME_XLSX)
    assert r.content[:2] == b"PK" and ".xlsx" in r.headers["content-disposition"]
    todo = "".join(_xlsx(r).values())
    assert "Persona Uno" in todo and "Titular Siete" in todo
    for dato in (CURP, "12345678901", "XXXX010101AB1", "CURP", "NSS", "RFC"):
        assert dato not in todo, dato


def test_export_con_casilla_incluye_pii_completo(base):
    todo = "".join(_xlsx(_entrar("ana").get("/mensualidades/export.xlsx?pii=1")).values())
    for dato in (CURP, "12345678901", "XXXX010101AB1"):
        assert dato in todo, dato
    with Session(base) as s:
        a = s.exec(select(AdminAudit).where(AdminAudit.action == "mensualidades-export")).one()
    assert "pii:1" in a.target and CURP not in a.target


def test_export_respeta_la_cartera_y_exige_sesion(base):
    todo = "".join(_xlsx(_entrar("cris").get("/mensualidades/export.xlsx?pii=1")).values())
    assert "Persona Uno" not in todo and CURP not in todo
    r = TestClient(app, base_url="https://testserver").get("/mensualidades/export.xlsx", follow_redirects=False)
    assert r.status_code == 303


def test_export_neutraliza_formulas():
    datos = exp.construir_xlsx([("H", ["a"], [["=HYPERLINK(\"x\")"], ["bien"], [12.5]])])
    hoja = zipfile.ZipFile(io.BytesIO(datos)).read("xl/worksheets/sheet1.xml").decode()
    assert "'=HYPERLINK" in hoja and ">=HYPERLINK" not in hoja and "<v>12.5</v>" in hoja


def test_export_pagado_y_fechas(base):
    _entrar("ana").post(f"/mensualidades/{_mid(base)}/pago", data={}, follow_redirects=False)
    hoja = _xlsx(_entrar("ana").get("/mensualidades/export.xlsx"))["xl/worksheets/sheet1.xml"]
    assert "2026-11-20" in hoja and "Pagado" in hoja and "ana" in hoja and "<v>1500.0</v>" in hoja
