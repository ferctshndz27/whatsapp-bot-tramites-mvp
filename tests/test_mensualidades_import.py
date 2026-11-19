"""Importación del piloto: titulares y personas. Datos SINTÉTICOS: aquí no hay CURP ni NSS reales.

Qué rompe en el negocio: una importación que duplica cuentas al repetirse, que pierde un NSS al
completarlo mal, o que deja pasar un RFC basura como si fuera dato.
"""

from datetime import date

import pytest
from sqlalchemy import func
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

import app.db as db
from app.mensualidades_import import PREFIJO, importar, normalizar
from app.models import ClienteMensualidad, CuentaCobranza

CURP_OK = "AAAA000101HDFRRL01"


def _fila(**kw):
    base = dict(NOMBRE="Ana Prueba", CURP=CURP_OK, NSS="12345678901", RFC="", ESTATUS="ALTA",
                MODALIDAD="BASICA", CLIENTE="7", ALTA="45683")
    return {**base, **kw}


@pytest.fixture()
def s(monkeypatch):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", e)
    db.init_db()
    with Session(e) as sesion:
        yield sesion


def test_nss_de_10_digitos_se_completa_con_cero_y_avisa():
    d, avisos = normalizar(_fila(NSS="2345678901"))
    assert d["nss"] == "02345678901"
    assert any("NSS" in a for a in avisos)


def test_nss_numerico_de_excel_con_decimal_no_se_corrompe():
    d, _ = normalizar(_fila(NSS="12345678901.0"))
    assert d["nss"] == "12345678901"


def test_nss_inservible_se_omite_en_lugar_de_guardar_basura():
    d, avisos = normalizar(_fila(NSS="123"))
    assert d["nss"] is None and avisos


def test_curp_se_limpia_y_la_de_17_se_conserva_con_aviso():
    d, avisos = normalizar(_fila(CURP=f" {CURP_OK.lower()} "))
    assert d["curp"] == CURP_OK and not avisos
    d, avisos = normalizar(_fila(CURP=CURP_OK[:-1]))
    assert d["curp"] == CURP_OK[:-1] and any("CURP" in a for a in avisos)


@pytest.mark.parametrize("rfc", ["SIN RFC", "XX", "N/A"])
def test_rfc_basura_se_omite_y_uno_valido_se_conserva(rfc):
    d, avisos = normalizar(_fila(RFC=rfc))
    assert d["rfc"] is None and avisos
    d, avisos = normalizar(_fila(RFC="AAAA000101AB1"))
    assert d["rfc"] == "AAAA000101AB1" and not avisos


def test_nombre_sin_espacios_sobrantes_y_alta_desde_serial():
    d, _ = normalizar(_fila(NOMBRE="  Ana   María  Prueba "))
    assert d["nombre"] == "Ana María Prueba"
    assert d["fecha_inicio"] == date(2025, 1, 26)


@pytest.mark.parametrize("malo", [dict(NOMBRE=""), dict(CLIENTE=""), dict(MODALIDAD="XYZ"), dict(ESTATUS="?")])
def test_filas_inservibles_se_rechazan(malo):
    with pytest.raises(ValueError):
        normalizar(_fila(**malo))


def test_agrupa_las_personas_bajo_su_titular(s):
    filas = [
        _fila(NOMBRE="Uno", CURP="AAAA000101HDFRRL01", NSS="11111111111", CLIENTE="7"),
        _fila(NOMBRE="Dos", CURP="BBBB000101HDFRRL02", NSS="22222222222", CLIENTE="7"),
        _fila(NOMBRE="Tres", CURP="CCCC000101HDFRRL03", NSS="33333333333", CLIENTE="8.0"),
    ]
    r = importar(s, filas, office_id=1)
    s.commit()
    assert (r.cuentas_nuevas, r.personas_nuevas) == (2, 3)
    cuentas = {c.codigo: c for c in s.exec(select(CuentaCobranza))}
    assert set(cuentas) == {"7", "8"}, "el 8.0 de Excel debe ser el código 8"
    por_cuenta = {
        cod: sorted(p.nombre for p in s.exec(select(ClienteMensualidad).where(ClienteMensualidad.cuenta_id == c.id)))
        for cod, c in cuentas.items()
    }
    assert por_cuenta == {"7": ["Dos", "Uno"], "8": ["Tres"]}
    assert all(c.importe_centavos is None and c.primer_pago is None for c in cuentas.values()), (
        "el Excel no trae importe ni primer pago: las cuentas nacen «Por completar»"
    )


def test_repetir_la_importacion_no_duplica_nada(s):
    filas = [_fila(NOMBRE="Uno", CLIENTE="7"), _fila(NOMBRE="Dos", CURP="", NSS="", CLIENTE="7")]
    importar(s, filas, office_id=1)
    s.commit()
    r = importar(s, filas, office_id=1)
    s.commit()
    assert (r.cuentas_nuevas, r.personas_nuevas, r.personas_existentes) == (0, 0, 2)
    assert s.exec(select(func.count()).select_from(CuentaCobranza)).one() == 1
    assert s.exec(select(func.count()).select_from(ClienteMensualidad)).one() == 2


def test_una_fila_mala_no_corta_las_demas_y_el_error_no_lleva_datos_personales(s):
    filas = [_fila(NOMBRE="Uno"), _fila(NOMBRE="", CURP="ZZZZ000101HDFRRL09"), _fila(NOMBRE="Tres", CURP="", NSS="")]
    r = importar(s, filas, office_id=1)
    assert r.personas_nuevas == 2
    assert r.errores == ["fila 3: sin nombre"]
    assert "ZZZZ" not in str(r.errores)


def test_los_avisos_quedan_en_observaciones_sin_el_dato_personal(s):
    importar(s, [_fila(NSS="2345678901", CURP=CURP_OK[:-1])], office_id=1)
    s.commit()
    p = s.exec(select(ClienteMensualidad)).one()
    assert p.observaciones.startswith(PREFIJO)
    assert "2345678901" not in p.observaciones and CURP_OK[:-1] not in p.observaciones
    assert p.nss == "02345678901"


def test_baja_en_el_excel_conserva_a_la_persona_con_su_baja(s):
    importar(s, [_fila(ESTATUS="BAJA")], office_id=1)
    s.commit()
    p = s.exec(select(ClienteMensualidad)).one()
    assert p.habilitado is True and p.baja_at is not None
