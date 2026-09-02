"""Multi-oficina, PR 1 (docs/SPEC-MULTI-OFICINA.md §8): esquema, siembra y candados.

Lo que necesita un arranque REAL usa `bd_inicializada`: el autouse _isolated_db
de conftest.py (tests/conftest.py:7-15) solo hace create_all() y nunca init_db(),
así que sin ella `office` está vacía y ux_officecredential_viva no existe. Lo que
solo mira los modelos usa SQLModel.metadata directo."""
import sqlite3

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel

import app.db as db
from app.models import OFICINA_TLAXCALA_ID, Office, OfficeCredential

# Las 15 tablas de §2.3 como LITERAL: derivarlas de db._TABLES_NEEDING_OFFICE
# haría que borrar una de la tupla también borrara la expectativa.
TABLAS_CON_OFICINA = frozenset({
    "client", "conversation", "ticket", "user", "teammessage", "cobro",
    "catalogcategory", "catalogservice", "synonymproposal", "nluevent", "infodoc",
    "label", "savedreply", "plantilla", "agentconfig",
})


def _engine_de_archivo(ruta):
    return create_engine(f"sqlite:///{ruta}", connect_args={"check_same_thread": False})


@pytest.fixture()
def bd_inicializada(tmp_path, monkeypatch):
    """Base de ARCHIVO tras un arranque real, enchufada como app.db.engine
    (patrón tests/test_db_migrations.py:606-635)."""
    engine = _engine_de_archivo(tmp_path / "nueva.db")
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    yield engine
    engine.dispose()


def _pragma(engine, tabla: str, columna: str):
    """(type, notnull, dflt_value) de una columna según PRAGMA table_info."""
    with engine.connect() as conn:
        for row in conn.exec_driver_sql(f'PRAGMA table_info("{tabla}")'):
            if row[1] == columna:
                return (row[2], row[3], row[4])
    return None


# --- Modelos ---------------------------------------------------------------

def test_solo_las_15_tablas_de_la_spec_llevan_office_id():
    # officecredential también tiene office_id, pero es la tabla hija, no una de §2.3.
    con_columna = {
        nombre for nombre, tabla in SQLModel.metadata.tables.items()
        if "office_id" in tabla.columns
    } - {"officecredential"}
    assert con_columna == TABLAS_CON_OFICINA
    assert set(db._TABLES_NEEDING_OFFICE) == TABLAS_CON_OFICINA
    assert len(db._TABLES_NEEDING_OFFICE) == 15  # sin duplicados en la tupla


@pytest.mark.parametrize("tabla", sorted(TABLAS_CON_OFICINA))
def test_office_id_nace_not_null_con_server_default_1(tabla):
    # Sin server_default una base create_all divergiría de la migrada: misma razón
    # que Conversation.provider (ver el comentario de _office_id_field en models.py).
    col = SQLModel.metadata.tables[tabla].columns["office_id"]
    assert col.nullable is False
    assert col.server_default is not None
    assert col.server_default.arg.text == str(OFICINA_TLAXCALA_ID)
    assert not col.foreign_keys  # sin FK: el ALTER de producción no la crearía
    assert not any("office_id" in [c.name for c in ix.columns]
                   for ix in SQLModel.metadata.tables[tabla].indexes)


# --- Siembra ---------------------------------------------------------------

def test_init_db_siembra_tlaxcala_como_oficina_1(bd_inicializada):
    with bd_inicializada.connect() as conn:
        filas = conn.exec_driver_sql(
            "SELECT id, nombre, slug, ciudad, zona_horaria, habilitado FROM office"
        ).fetchall()
    assert filas == [(1, "Tlaxcala", "tlaxcala", "Tlaxcala", "America/Mexico_City", 1)]


def test_la_siembra_es_idempotente_y_no_pisa_una_fila_editada(bd_inicializada):
    # Idempotencia por id, no por conteo: un rename hecho desde el panel (PR 3)
    # tiene que sobrevivir a cada arranque.
    with bd_inicializada.begin() as conn:
        conn.exec_driver_sql("UPDATE office SET nombre = 'Tlaxcala Centro' WHERE id = 1")
    db.init_db()
    db.init_db()
    with bd_inicializada.connect() as conn:
        assert conn.exec_driver_sql("SELECT id, nombre FROM office").fetchall() == [
            (1, "Tlaxcala Centro")
        ]


def test_la_siembra_fija_el_id_1_aunque_la_tabla_no_tenga_pk(tmp_path, monkeypatch):
    # Mismo escenario que el candado de tests/test_db_migrations.py:845-857: la
    # tabla existe sin tipos ni PK (sin autoincrement). id=1 tiene que ser explícito.
    ruta = tmp_path / "sin_pk.db"
    con = sqlite3.connect(ruta)
    try:
        con.execute('CREATE TABLE "office" ("id", "nombre", "slug", "ciudad", '
                    '"zona_horaria", "habilitado", "created_at")')
        con.commit()
    finally:
        con.close()
    engine = _engine_de_archivo(ruta)
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    db.init_db()
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT id FROM office").fetchall() == [(1,)]


def test_officecredential_nace_vacia(bd_inicializada):
    # El PR 1 no siembra llaves: las filas de Tlaxcala son PR 4.
    with bd_inicializada.connect() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM officecredential").scalar() == 0


# --- DEFAULT del lado del motor -------------------------------------------

def test_office_id_tiene_default_1_en_base_nueva_sin_pasar_por_python(bd_inicializada):
    # Molde tests/test_provider_channel.py:114-131: un INSERT crudo sin la columna
    # funciona y cae en 1 — el DEFAULT vive en el DDL, no solo en el modelo.
    with bd_inicializada.begin() as conn:
        conn.exec_driver_sql(
            'INSERT INTO agentconfig ("key", value, updated_at, habilitado) '
            "VALUES ('k', 'v', '2026-01-01 00:00:00', 1)"
        )
    with bd_inicializada.connect() as conn:
        assert conn.exec_driver_sql("SELECT office_id FROM agentconfig").scalar() == 1


# --- Índice único parcial --------------------------------------------------

def test_una_sola_llave_viva_por_oficina_proveedor_y_clave(bd_inicializada):
    with bd_inicializada.connect() as conn:
        indices = {row[1]: row[2] for row in conn.exec_driver_sql(
            'PRAGMA index_list("officecredential")')}
    assert indices.get("ux_officecredential_viva") == 1, "existe pero no es UNIQUE"

    def llave(**kw):
        base = dict(office_id=1, provider="meta", clave="WHATSAPP_TOKEN",
                    valor_cif=b"\x00", updated_by="qa")
        base.update(kw)
        return OfficeCredential(**base)

    with Session(bd_inicializada) as s:
        s.add(llave())
        s.commit()
        s.add(llave())
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()
        # Dar de baja es apagar: la re-alta no choca con la fila apagada.
        s.exec(text("UPDATE officecredential SET habilitado = 0"))  # noqa: sqlmodel exec de text
        s.commit()
        s.add(llave())
        s.commit()
        assert s.exec(text("SELECT COUNT(*) FROM officecredential")).scalar() == 2


# --- BLOB ------------------------------------------------------------------

def test_valor_cif_se_guarda_como_blob_y_regresa_bytes(bd_inicializada):
    assert _pragma(bd_inicializada, "officecredential", "valor_cif")[0] == "BLOB"
    with Session(bd_inicializada) as s:
        s.add(OfficeCredential(office_id=1, provider="stripe", clave="STRIPE_SECRET_KEY",
                               valor_cif=b"gAAAA\x00\xff", updated_by="qa"))
        s.commit()
    with Session(bd_inicializada) as s:
        fila = s.get(OfficeCredential, 1)
        assert fila.valor_cif == b"gAAAA\x00\xff"
        assert isinstance(fila.valor_cif, bytes)


def test_si_otra_fila_ocupa_el_nombre_la_siembra_falla_a_gritos(tmp_path, monkeypatch, caplog):
    """REGLA: la guardia de IntegrityError existe para la carrera arranque-vs-cron,
    no para tapar una base inconsistente. Si otra oficina ya tiene el nombre o el
    slug, la fila 1 NO se crea y todo office_id del histórico apunta a la nada:
    eso tiene que quedar en el log, no pasar en silencio arranque tras arranque.
    """
    engine = _engine_de_archivo(tmp_path / "ocupada.db")
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Office(id=99, nombre="Tlaxcala", slug="tlaxcala", ciudad="Tlaxcala"))
        s.commit()

    with caplog.at_level("ERROR", logger="app.db"):
        db.init_db()

    with Session(engine) as s:
        assert s.get(Office, OFICINA_TLAXCALA_ID) is None, (
            "el UNIQUE de nombre/slug impide la siembra: la fila 1 no debe aparecer"
        )
    assert any("no se pudo sembrar la oficina" in r.message.lower() for r in caplog.records), (
        f"la falla quedó muda; registros: {[r.message for r in caplog.records]}"
    )
    engine.dispose()
