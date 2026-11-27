"""Datos del autocobro, F1 (spec autocobranza): esquema y espejo, sin comportamiento.

Qué se rompe en el negocio si esto se pone rojo:
- Sin las columnas nuevas en una base VIEJA, el arranque del VPS revienta o el plan no puede llevar
  su monto total y su saldo (el autocobro no sabría cuándo dejar de cobrar).
- Sin los cinco lugares del espejo, la prueba del consentimiento (lo que se enseña ante una
  disputa) no llega a MSSQL y /system no lo dice.
- Si un modelo guardara número de tarjeta o CVV, el bot entraría a PCI completo.
Datos sintéticos: ningún teléfono, tarjeta ni correo reales.
"""

import ast
import inspect
import re
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.archive as archive
import app.db as db
import app.system as system
from app import models
from app.models import ConsentimientoCobro, LigaAutocobro, PaymentMethod, PlanCobro

COLUMNAS_NUEVAS_DEL_PLAN = {
    "monto_total_centavos",
    "saldo_pendiente_centavos",
    "cargos_previstos",
    "fecha_inicio",
    "consentimiento_id",
}
_ARBOL = ast.parse(Path(archive.__file__).read_text(encoding="utf-8"))


def _funcion(nombre: str) -> ast.FunctionDef:
    return next(n for n in _ARBOL.body if isinstance(n, ast.FunctionDef) and n.name == nombre)


# --- Los cinco lugares del espejo, para ConsentimientoCobro --------------------------------------


def test_lugar_1_el_ddl_crea_la_tabla_del_consentimiento():
    tablas = {
        m.group(1)
        for ddl in archive._SCHEMA_DDL
        for m in [re.search(r"CREATE\s+TABLE\s+(\w+)", ddl)]
        if m
    }
    assert "consentimientos_cobro" in tablas


def test_lugar_2_el_upsert_escribe_su_tabla():
    fn = getattr(archive, "archive_consentimiento_cobro", None)
    assert callable(fn), "falta archive.archive_consentimiento_cobro()"
    assert '"consentimientos_cobro"' in inspect.getsource(fn)


def test_lugar_3_el_listener_corre_despues_del_commit_y_cubre_insert_y_update():
    """El webhook de Stripe escribe esta tabla: dentro del flush retendría el escritor de SQLite."""
    eventos: set[str] = set()
    usa_diferir = False
    for interna in ast.walk(_funcion("_register_listeners")):
        if not isinstance(interna, ast.FunctionDef):
            continue
        if not any(
            isinstance(s, ast.Name) and s.id == "archive_consentimiento_cobro"
            for s in ast.walk(interna)
        ):
            continue
        usa_diferir = any(
            isinstance(s, ast.Name) and s.id == "_diferir" for s in ast.walk(interna)
        )
        for dec in interna.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and len(dec.args) == 2
                and isinstance(dec.args[0], ast.Name)
                and dec.args[0].id == "ConsentimientoCobro"
            ):
                eventos.add(dec.args[1].value)
    assert eventos == {"after_insert", "after_update"}, eventos
    assert usa_diferir, "el consentimiento debe espejarse con _diferir, no con _safe"


def _listener_de(nombre_archivador: str) -> ast.FunctionDef:
    for interna in _funcion("_register_listeners").body:
        if isinstance(interna, ast.FunctionDef) and any(
            isinstance(s, ast.Name) and s.id == nombre_archivador for s in ast.walk(interna)
        ):
            return interna
    pytest.fail(f"no hay listener de {nombre_archivador}")


@pytest.mark.parametrize("archivador", ["archive_plan_cobro", "archive_payment_method"])
def test_lugar_3b_plan_y_tarjeta_tambien_se_espejan_despues_del_commit(archivador):
    """Desde F3 el webhook de Stripe los escribe: con _safe retendrían el escritor de SQLite."""
    nombres = {s.id for s in ast.walk(_listener_de(archivador)) if isinstance(s, ast.Name)}
    assert "_diferir" in nombres and "_safe" not in nombres


def test_lugar_4_el_backfill_repara_la_tabla():
    llamadas = {
        n.func.id
        for n in ast.walk(_funcion("backfill"))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "archive_consentimiento_cobro" in llamadas


def test_lugar_5_mirror_tables_la_cuenta():
    assert (ConsentimientoCobro, "consentimientos_cobro") in system.MIRROR_TABLES


def test_la_liga_no_se_espeja_porque_su_hash_es_un_secreto_de_acceso():
    assert all(m is not LigaAutocobro for m, _ in system.MIRROR_TABLES)
    assert not hasattr(archive, "archive_liga_autocobro")


def test_el_espejo_del_plan_lleva_las_columnas_nuevas_en_su_dict():
    cols = archive._COLUMNS_ADDED_AFTER_LAUNCH["planes_cobro"]
    assert COLUMNAS_NUEVAS_DEL_PLAN <= set(cols)
    campos = inspect.getsource(archive.archive_plan_cobro)
    for c in COLUMNAS_NUEVAS_DEL_PLAN:
        assert f'"{c}"' in campos, f"archive_plan_cobro no manda {c}"


# --- Migración sobre una base vieja ---------------------------------------------------------------


@pytest.fixture()
def base_vieja(monkeypatch):
    """Una base de producción: `plancobro` como la dejó la primera entrega de Stripe, con una fila."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        for c in COLUMNAS_NUEVAS_DEL_PLAN:
            # Los nombres salen de la constante de arriba, no de entrada externa.
            conn.execute(text(f"ALTER TABLE plancobro DROP COLUMN {c}"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        conn.execute(
            text(
                "INSERT INTO plancobro (phone, concepto, moneda, monto_centavos, periodicidad, "
                "estado, intentos_fallidos_seguidos, cobros_generados, creado_por, created_at, habilitado) "
                "VALUES ('525500000000', 'prueba', 'mxn', 50000, 'mensual', 'activo', 0, 0, 'ana', "
                "'2026-11-01 00:00:00', 1)"
            )
        )
    return engine


def test_la_migracion_agrega_las_columnas_nuevas_sin_perder_la_fila(base_vieja):
    db.init_db()
    with base_vieja.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(plancobro)"))}
        fila = conn.execute(
            text("SELECT concepto, monto_total_centavos, saldo_pendiente_centavos FROM plancobro")
        ).one()
    assert COLUMNAS_NUEVAS_DEL_PLAN <= cols
    assert fila == ("prueba", None, None), "NULL es «sin tope todavía»: no se inventa un monto"


def test_init_db_es_idempotente_con_las_columnas_nuevas(base_vieja):
    db.init_db()
    db.init_db()


# --- Reglas de las tablas nuevas ------------------------------------------------------------------


@pytest.fixture()
def base_real(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    return engine


def test_dos_ligas_no_comparten_hash(base_real):
    """Un hash repetido haría que una liga abriera el plan de otro cliente."""
    from datetime import timedelta

    ahora = models.utcnow()
    with Session(base_real) as s:
        s.add(LigaAutocobro(plan_id=1, token_hash="a" * 64, expira_at=ahora + timedelta(hours=72)))
        s.commit()
        s.add(LigaAutocobro(plan_id=2, token_hash="a" * 64, expira_at=ahora + timedelta(hours=72)))
        with pytest.raises(IntegrityError):
            s.commit()


def test_el_consentimiento_guarda_la_prueba_completa(base_real):
    with Session(base_real) as s:
        c = ConsentimientoCobro(
            plan_id=1,
            phone="525500000000",
            version_terminos="v1",
            hash_texto="b" * 64,
            texto_mandato="Autorizo cargos de hasta $500.00 MXN",
            tope_cargo_centavos=50000,
            monto_total_centavos=300000,
            periodicidad="mensual",
            dia_cobro=5,
            contrato_folio="C-0001",
            stripe_setup_intent_id="seti_prueba",
            stripe_mandate_id="mandate_prueba",
        )
        s.add(c)
        s.commit()
        s.refresh(c)
    assert c.aceptado_at is not None and c.revocado_at is None and c.habilitado is True


def test_ningun_modelo_del_modulo_guarda_numero_de_tarjeta_ni_cvv():
    """Decisión del operador, 2026-11-26: el cobro posterior usa el pm_ de Stripe, no el PAN."""
    prohibido = re.compile(r"pan\b|cvv|cvc|numero_tarjeta|num_tarjeta|card_number|tarjeta_completa|security_code", re.I)
    for modelo in (PlanCobro, PaymentMethod, ConsentimientoCobro, LigaAutocobro):
        for campo in modelo.model_fields:
            assert not prohibido.search(campo), f"{modelo.__name__}.{campo} parece dato de tarjeta"
