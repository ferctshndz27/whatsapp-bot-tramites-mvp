"""Los errores de base de datos no arrastran datos personales a la bitácora.

Auditoría 2026-09 (INFRA-3): SQLAlchemy incluye por defecto los parámetros
ligados en el mensaje de cada excepción («[parameters: ('5215512345678', 'mi
CURP es …')]»), y `archive._safe` registra `exc` completo en cada fallo del
espejo. Con `hide_parameters` el mensaje dice «[SQL parameters hidden]». Los
tres engines del proyecto (SQLite y los dos de MSSQL) comparten la opción.
"""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

import app.archive as archive
import app.db as db
from app.config import settings

CURP = "GOMC800101HDFRRN09"
TELEFONO = "5215512345678"


def test_un_error_de_integridad_no_muestra_los_parametros():
    engine = create_engine("sqlite://", **db.OPCIONES_ENGINE)
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE t (tel TEXT UNIQUE, cuerpo TEXT)"))
        conn.execute(
            text("INSERT INTO t VALUES (:tel, :cuerpo)"),
            {"tel": TELEFONO, "cuerpo": f"mi curp es {CURP}"},
        )
        with pytest.raises(IntegrityError) as excinfo:
            conn.execute(
                text("INSERT INTO t VALUES (:tel, :cuerpo)"),
                {"tel": TELEFONO, "cuerpo": f"mi curp es {CURP}"},
            )

    mensaje = str(excinfo.value)
    assert TELEFONO not in mensaje and CURP not in mensaje, mensaje
    assert "hidden" in mensaje


def test_el_control_no_es_trivial_sin_la_opcion():
    """El mismo error, con un engine sin la opción, SÍ filtra la CURP: prueba
    de que la aserción de arriba mide la opción y no la casualidad."""
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE t (tel TEXT UNIQUE)"))
        conn.execute(text("INSERT INTO t VALUES (:tel)"), {"tel": CURP})
        with pytest.raises(IntegrityError) as excinfo:
            conn.execute(text("INSERT INTO t VALUES (:tel)"), {"tel": CURP})
    assert CURP in str(excinfo.value)


def test_la_opcion_esta_encendida():
    assert db.OPCIONES_ENGINE.get("hide_parameters") is True


def test_el_engine_del_espejo_mssql_lleva_la_misma_opcion(monkeypatch):
    """No se levanta ningún MSSQL: se intercepta `create_engine` y se mira con
    qué se llamó. Las credenciales son de mentira y no se conecta a nada."""
    monkeypatch.setattr(archive, "_engine", None)
    monkeypatch.setattr(settings, "mssql_host", "mssql.invalid")
    monkeypatch.setattr(settings, "mssql_password", "no-es-real")
    llamadas: list[dict] = []

    def _falso_create_engine(url, **kwargs):
        llamadas.append(kwargs)
        return object()

    monkeypatch.setattr(archive, "create_engine", _falso_create_engine)
    archive._get_engine()
    monkeypatch.setattr(archive, "_engine", None)

    assert llamadas and all(k.get("hide_parameters") is True for k in llamadas), llamadas
