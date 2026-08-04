"""Canal (`provider`) y catálogo de plantillas — Fase 1 de la convivencia.

REGLA DE NEGOCIO: son DOS cuentas de WhatsApp independientes, cada una con su
número, que comparten este CRM. No hay failover entre ellas: se responde SIEMPRE
por el número donde entró el mensaje, porque la ventana de 24 h es por número y
contestar por el otro deja al negocio fuera de ventana con un remitente que el
cliente no reconoce.

Esta fase solo agrega la columna y el catálogo; el ruteo real llega en la Fase 4.
Lo que se blinda aquí es que el histórico NO quede sin canal: si `provider`
naciera NULL, todo reporte por cuenta saldría mal desde el primer día y ya no
habría forma de reconstruir a qué número llegó cada solicitud.
"""

import sqlite3

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

import app.db as db
from app.models import (
    PROVIDER_META,
    PROVIDER_TWILIO,
    Conversation,
    Message,
    Plantilla,
    Ticket,
)

CANONICO = "525500000000"


# ---------------------------------------------------------------------------
# El default sobre datos que ya existen
# ---------------------------------------------------------------------------


@pytest.fixture()
def bd_sin_provider(tmp_path, monkeypatch):
    """Base con filas históricas creadas ANTES de que existiera `provider`.

    Se construyen las tablas a mano, sin la columna, para reproducir lo que hay
    hoy en el VPS: create_all() no toca tablas existentes, así que la columna
    solo puede llegar por _add_missing_columns().
    """
    ruta = tmp_path / "sin-provider.db"
    con = sqlite3.connect(ruta)
    try:
        con.execute(
            "CREATE TABLE conversation (phone VARCHAR PRIMARY KEY, state VARCHAR NOT NULL,"
            " context_json VARCHAR NOT NULL, failed_attempts INTEGER NOT NULL,"
            " updated_at DATETIME NOT NULL)"
        )
        con.execute(
            "CREATE TABLE message (id INTEGER PRIMARY KEY, phone VARCHAR NOT NULL,"
            " direction VARCHAR(3) NOT NULL, body VARCHAR NOT NULL, created_at DATETIME NOT NULL)"
        )
        con.execute(
            "CREATE TABLE ticket (id INTEGER PRIMARY KEY, phone VARCHAR NOT NULL,"
            " client_name VARCHAR, category VARCHAR NOT NULL, service_id VARCHAR NOT NULL,"
            " service_name VARCHAR NOT NULL, price_info VARCHAR NOT NULL, status VARCHAR(10)"
            " NOT NULL, escalado BOOLEAN NOT NULL, notes VARCHAR, assigned_to VARCHAR,"
            " created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
        )
        con.execute(
            "INSERT INTO conversation VALUES (?,'inicio','{}',0,'2025-01-05 10:00:00')",
            (CANONICO,),
        )
        con.execute(
            "INSERT INTO message VALUES (1,?,'IN','hola','2025-01-05 10:00:01')", (CANONICO,)
        )
        con.execute(
            "INSERT INTO ticket VALUES (1,?,'Ana','actas','sb-acta-nacimiento','Acta',"
            "'A cotizar','NUEVO',0,NULL,NULL,'2025-01-05 10:00:02','2025-01-05 10:00:02')",
            (CANONICO,),
        )
        con.commit()
    finally:
        con.close()
    engine = create_engine(f"sqlite:///{ruta}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    return engine


def test_las_filas_historicas_quedan_en_meta_y_nunca_en_null(bd_sin_provider):
    # REGLA: todo lo anterior a esta fase entró por la única cuenta que hubo, la
    # de Meta. Un NULL ahí volvería inútil cualquier reporte por cuenta y no
    # habría forma de reconstruir el dato después.
    db.init_db()

    with Session(bd_sin_provider) as s:
        assert s.exec(select(Conversation)).one().provider == PROVIDER_META
        assert s.exec(select(Message)).one().provider == PROVIDER_META
        assert s.exec(select(Ticket)).one().provider == PROVIDER_META


def test_migrar_no_reasigna_canal_a_quien_ya_lo_tiene(bd_sin_provider):
    # REGLA: init_db() corre en cada arranque y en cada cron. Una segunda pasada
    # no puede pisar el canal de una conversación que ya pasó a Twilio.
    db.init_db()
    with Session(bd_sin_provider) as s:
        conv = s.exec(select(Conversation)).one()
        conv.provider = PROVIDER_TWILIO
        s.add(conv)
        s.commit()

    db.init_db()

    with Session(bd_sin_provider) as s:
        assert s.exec(select(Conversation)).one().provider == PROVIDER_TWILIO


def test_una_base_nueva_tambien_nace_con_el_default(tmp_path, monkeypatch):
    # REGLA: la base migrada y una nueva tienen que converger. Si el DEFAULT
    # solo lo pusiera el ALTER TABLE, un entorno recién creado se comportaría
    # distinto al VPS y el bug solo se vería en uno de los dos.
    engine = create_engine(
        f"sqlite:///{tmp_path / 'nueva.db'}", connect_args={"check_same_thread": False}
    )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()

    with engine.begin() as conn:
        # INSERT crudo, sin pasar por el ORM: si no hubiera DEFAULT del lado del
        # motor, esto reventaría por NOT NULL.
        conn.exec_driver_sql(
            "INSERT INTO conversation (phone, state, context_json, failed_attempts,"
            " updated_at, habilitado) VALUES ('525511112222','inicio','{}',0,"
            "'2026-01-01 00:00:00',1)"
        )
    with Session(engine) as s:
        assert s.exec(select(Conversation)).one().provider == PROVIDER_META


def test_el_modelo_default_a_meta_sin_pasar_por_la_migracion():
    # REGLA: el default vive en el modelo, no solo en el SQL. Crear un mensaje
    # desde el código sin decir el canal no puede dejarlo vacío.
    assert Message(phone=CANONICO, direction="in", body="x").provider == PROVIDER_META
    assert Conversation(phone=CANONICO).provider == PROVIDER_META


# ---------------------------------------------------------------------------
# Catálogo de plantillas
# ---------------------------------------------------------------------------


@pytest.fixture()
def sesion(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)
    with Session(engine) as s:
        yield s


def test_una_plantilla_nace_sin_aprobar_en_ningun_proveedor(sesion):
    # REGLA: la aprobación la da Meta y tarda días. Nacer aprobada haría que el
    # envío la intentara usar y fallara contra la API del proveedor.
    sesion.add(Plantilla(codigo="cobro_proximo", texto_referencia="Tu cargo de {{1}}…"))
    sesion.commit()

    guardada = sesion.get(Plantilla, "cobro_proximo")
    assert guardada.aprobada_meta is False
    assert guardada.aprobada_twilio is False
    assert guardada.categoria == "utility", (
        "UTILITY es la categoría correcta para avisos de trámite y cobranza: "
        "aprueba más rápido y cuesta menos que MARKETING"
    )


def test_la_misma_plantilla_guarda_el_identificador_de_los_dos_proveedores(sesion):
    # REGLA: es la razón de existir de esta tabla. Meta la llama por `name` y
    # Twilio por `ContentSid`; si esos dos ids vivieran hardcodeados en módulos
    # distintos, el día que alguien actualice uno solo las dos cuentas divergen
    # en silencio.
    sesion.add(
        Plantilla(
            codigo="menu_categorias",
            meta_template_name="menu_categorias_v1",
            twilio_content_sid="HX0000000000000000000000000000000000",
            aprobada_meta=True,
            aprobada_twilio=True,
            num_variables=2,
        )
    )
    sesion.commit()

    guardada = sesion.get(Plantilla, "menu_categorias")
    assert guardada.meta_template_name and guardada.twilio_content_sid, (
        "una plantilla sin los DOS identificadores no sirve para las dos cuentas"
    )


def test_el_codigo_de_plantilla_es_unico(sesion):
    # REGLA: `codigo` es la llave con la que el código pide la plantilla
    # ("cobro_proximo"). Dos filas con el mismo código harían ambiguo qué se
    # manda, y en cobranza eso es un aviso equivocado sobre dinero.
    sesion.add(Plantilla(codigo="cobro_exitoso"))
    sesion.commit()

    sesion.add(Plantilla(codigo="cobro_exitoso"))
    with pytest.raises(IntegrityError):
        sesion.commit()
