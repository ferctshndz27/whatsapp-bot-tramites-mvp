"""Espejo MSSQL de las tablas del calendario de cobranza (spec calendario-cobranza-mensualidades).

Si una de estas tres tablas no llega a MSSQL, la cobranza queda invisible en el archivo y /system no
lo dice. tests/test_mirror_consistency.py audita las tablas que YA están en el DDL; esta prueba
exige que estas tres lleguen a los CINCO lugares, porque una tabla olvidada en todos no la detecta
ninguna auditoría genérica:

  1. app/archive.py `_SCHEMA_DDL`            4. app/archive.py `backfill()`
  2. app/archive.py `archive_<algo>()`       5. app/system.py  `MIRROR_TABLES`
  3. app/archive.py `_register_listeners()`

Más los índices únicos parciales, que `create_all()` no crea (viven en `init_db()`): sin ellos dos
asesores podrían registrar al mismo CURP o cobrar dos veces el mismo mes.
Datos sintéticos: aquí no hay CURP ni NSS reales.
"""

import ast
import inspect
import re
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

import app.archive as archive
import app.db as db
import app.system as system
from app.models import ClienteMensualidad, Mensualidad, MensualidadAsesor

TABLAS = {
    ClienteMensualidad: ("cliente_mensualidades", "archive_cliente_mensualidad"),
    MensualidadAsesor: ("mensualidad_asesores", "archive_mensualidad_asesor"),
    Mensualidad: ("mensualidades", "archive_mensualidad"),
}

_ARBOL = ast.parse(Path(archive.__file__).read_text(encoding="utf-8"))


def _funcion(nombre: str) -> ast.FunctionDef:
    return next(n for n in _ARBOL.body if isinstance(n, ast.FunctionDef) and n.name == nombre)


def _tablas_ddl() -> set[str]:
    return {
        m.group(1)
        for ddl in archive._SCHEMA_DDL
        for m in [re.search(r"CREATE\s+TABLE\s+(\w+)", ddl)]
        if m
    }


@pytest.mark.parametrize("modelo", list(TABLAS))
def test_lugar_1_el_ddl_crea_la_tabla(modelo):
    assert TABLAS[modelo][0] in _tablas_ddl()


@pytest.mark.parametrize("modelo", list(TABLAS))
def test_lugar_2_existe_el_upsert_y_escribe_su_tabla(modelo):
    tabla, funcion = TABLAS[modelo]
    fn = getattr(archive, funcion, None)
    assert callable(fn), f"falta archive.{funcion}()"
    assert f'"{tabla}"' in inspect.getsource(fn)


@pytest.mark.parametrize("modelo", list(TABLAS))
def test_lugar_3_hay_listener_de_insert_y_update(modelo):
    _, funcion = TABLAS[modelo]
    eventos: set[str] = set()
    for interna in ast.walk(_funcion("_register_listeners")):
        if not isinstance(interna, ast.FunctionDef):
            continue
        if not any(isinstance(s, ast.Name) and s.id == funcion for s in ast.walk(interna)):
            continue
        for dec in interna.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and len(dec.args) == 2
                and isinstance(dec.args[0], ast.Name)
                and dec.args[0].id == modelo.__name__
                and isinstance(dec.args[1], ast.Constant)
            ):
                eventos.add(dec.args[1].value)
    assert eventos == {"after_insert", "after_update"}, eventos


@pytest.mark.parametrize("modelo", list(TABLAS))
def test_lugar_4_el_backfill_repara_la_tabla(modelo):
    _, funcion = TABLAS[modelo]
    llamadas = {
        n.func.id
        for n in ast.walk(_funcion("backfill"))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert funcion in llamadas


@pytest.mark.parametrize("modelo", list(TABLAS))
def test_lugar_5_mirror_tables_la_cuenta(modelo):
    assert (modelo, TABLAS[modelo][0]) in system.MIRROR_TABLES


def test_la_oficina_es_la_del_cliente_y_no_se_duplica_en_el_puente_ni_en_las_mensualidades():
    assert "office_id" in ClienteMensualidad.model_fields
    assert "office_id" not in MensualidadAsesor.model_fields
    assert "office_id" not in Mensualidad.model_fields


@pytest.fixture()
def base_real(monkeypatch):
    """`init_db()` real: los índices únicos parciales son SQL a mano."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    return engine


def _cliente(**kw):
    base = dict(nombre="Prueba Uno", curp="AAAA000101HDFRRL01", nss="12345678901")
    return ClienteMensualidad(**{**base, **kw})


def test_curp_y_nss_son_unicos_solo_entre_clientes_vivos(base_real):
    """Dos asesores dando de alta a la misma persona la duplicarían en la cobranza."""
    with Session(base_real) as s:
        s.add(_cliente())
        s.commit()
        s.add(_cliente(nombre="Otro", nss="99999999999"))
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()
        s.add(_cliente(nombre="Otro", curp="BBBB000101HDFRRL02"))
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()
        # Una fila apagada (corrección de captura) no impide volver a darlo de alta.
        s.exec(text("UPDATE clientemensualidad SET habilitado = 0"))
        s.add(_cliente(nombre="Otro"))
        s.commit()


def test_un_cliente_no_tiene_dos_mensualidades_vivas_del_mismo_periodo(base_real):
    """Un doble clic o dos procesos generando periodos cobrarían dos veces el mes."""
    with Session(base_real) as s:
        c = _cliente()
        s.add(c)
        s.commit()
        for _ in range(2):
            s.add(
                Mensualidad(
                    cliente_id=c.id, periodo="2026-12", vence_el=date(2026, 12, 5), importe_centavos=50000
                )
            )
        with pytest.raises(IntegrityError):
            s.commit()


def test_el_puente_admite_un_par_vivo_y_un_solo_responsable_vivo(base_real):
    """Un cliente con dos responsables dejaría a dos asesores dándolo de baja a la vez."""
    with Session(base_real) as s:
        c = _cliente()
        s.add(c)
        s.commit()
        s.add(MensualidadAsesor(cliente_id=c.id, username="ana", rol="responsable"))
        s.add(MensualidadAsesor(cliente_id=c.id, username="beto", rol="colaborador"))
        s.commit()
        s.add(MensualidadAsesor(cliente_id=c.id, username="ana", rol="colaborador"))
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()
        s.add(MensualidadAsesor(cliente_id=c.id, username="carla", rol="responsable"))
        with pytest.raises(IntegrityError):
            s.commit()
