"""Migración manual de columnas e idempotencia de init_db().

Este repo NO usa Alembic: los dicts `_*_COLUMNS_ADDED_AFTER_LAUNCH` de app/db.py
más `_add_missing_columns()` SON el único mecanismo de migración, y el deploy del
VPS hace `git reset --hard` + `docker compose up -d --build` sobre una base de
producción CON datos. Si alguien agrega una columna a app/models.py y olvida
registrarla en el dict correspondiente, el arranque revienta (o peor: consulta
una columna inexistente) y se descubre en producción, no aquí.

Lo que aporta este archivo frente a los que ya rozan el tema:
- tests/test_concurrency.py y tests/test_e2e_conversation.py llaman `init_db()`
  sobre una base VACÍA (les interesa el índice único como red anti-duplicados).
  Aquí el escenario es el de producción: una base VIEJA, con datos, a la que
  init_db() le tiene que agregar columnas sin perder nada.
- Ningún test existente compara los modelos contra lo que init_db() deja
  realmente en la base; ese candado (ver `test_toda_columna_de_los_modelos_...`)
  es el punto de mayor valor del archivo.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import SQLModel, create_engine

import app.db as db

# Import explícito (no basta con que app.db lo importe de rebote): este archivo
# compara SQLModel.metadata contra la base, así que necesita los modelos
# registrados aunque corra solo. Quién los registra en el cron es justo lo que
# vigila test_importar_solo_app_db_registra_todos_los_modelos_en_el_metadata.
from app import models  # noqa: F401  isort:skip

# Formato CRUDO de Meta: es el que tienen las filas anteriores a la
# normalización — justo el escenario que este archivo simula.
PHONE = "5215500000000"
# El mismo teléfono después de init_db(): _normalize_phones() lo reescribe.
PHONE_CANONICO = "525500000000"
_RAIZ = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Esquema DE LANZAMIENTO (literal, escrito a mano)
# ---------------------------------------------------------------------------
# Es el esquema que tenían estas tablas ANTES de las columnas de los dicts de
# app/db.py. Se escribe a mano a propósito: si se derivara de SQLModel.metadata
# la prueba compararía el código consigo mismo y no detectaría nada. Las tablas
# que nacieron después (nluevent, alert, catalog*, ...) NO están aquí — en una
# base vieja tampoco existen, y create_all() las tiene que crear completas.
_DDL_DE_LANZAMIENTO = (
    """
    CREATE TABLE client (
        phone VARCHAR NOT NULL,
        name VARCHAR,
        created_at DATETIME NOT NULL,
        PRIMARY KEY (phone)
    )
    """,
    """
    CREATE TABLE conversation (
        phone VARCHAR NOT NULL,
        state VARCHAR NOT NULL,
        context_json VARCHAR NOT NULL,
        failed_attempts INTEGER NOT NULL,
        updated_at DATETIME NOT NULL,
        PRIMARY KEY (phone)
    )
    """,
    """
    CREATE TABLE message (
        id INTEGER NOT NULL,
        phone VARCHAR NOT NULL,
        direction VARCHAR(3) NOT NULL,
        body VARCHAR NOT NULL,
        created_at DATETIME NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    "CREATE INDEX ix_message_phone ON message (phone)",
    """
    CREATE TABLE ticket (
        id INTEGER NOT NULL,
        phone VARCHAR NOT NULL,
        client_name VARCHAR,
        category VARCHAR NOT NULL,
        service_id VARCHAR NOT NULL,
        service_name VARCHAR NOT NULL,
        price_info VARCHAR NOT NULL,
        status VARCHAR(10) NOT NULL,
        escalado BOOLEAN NOT NULL,
        notes VARCHAR,
        assigned_to VARCHAR,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    "CREATE INDEX ix_ticket_phone ON ticket (phone)",
    """
    CREATE TABLE ticketactivity (
        id INTEGER NOT NULL,
        ticket_id INTEGER NOT NULL,
        username VARCHAR NOT NULL,
        action VARCHAR NOT NULL,
        created_at DATETIME NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE teammessage (
        id INTEGER NOT NULL,
        author VARCHAR NOT NULL,
        body VARCHAR NOT NULL,
        media_path VARCHAR,
        created_at DATETIME NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE "user" (
        id INTEGER NOT NULL,
        username VARCHAR NOT NULL,
        password_hash VARCHAR NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    'CREATE UNIQUE INDEX ix_user_username ON "user" (username)',
    """
    CREATE TABLE invitation (
        id INTEGER NOT NULL,
        token VARCHAR NOT NULL,
        phone VARCHAR,
        created_by VARCHAR NOT NULL,
        created_at DATETIME NOT NULL,
        used_at DATETIME,
        used_by VARCHAR,
        PRIMARY KEY (id)
    )
    """,
    "CREATE UNIQUE INDEX ix_invitation_token ON invitation (token)",
)

# Datos "históricos" de la base vieja: un cliente real a media conversación con
# su solicitud abierta. Si la migración los pierde, el negocio pierde el
# historial de trámites.
_DATOS_DE_LANZAMIENTO = (
    (
        "INSERT INTO client (phone, name, created_at) VALUES (?, ?, ?)",
        (PHONE, "Ana Ruiz", "2025-01-05 10:00:00"),
    ),
    (
        "INSERT INTO conversation (phone, state, context_json, failed_attempts, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (PHONE, "menu_servicio", '{"categoria": "actas"}', 1, "2025-01-05 10:00:00"),
    ),
    (
        "INSERT INTO message (id, phone, direction, body, created_at) VALUES (?, ?, ?, ?, ?)",
        (1, PHONE, "in", "quiero mi acta de nacimiento", "2025-01-05 10:00:01"),
    ),
    (
        "INSERT INTO ticket (id, phone, client_name, category, service_id, service_name, "
        "price_info, status, escalado, notes, assigned_to, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            1,
            PHONE,
            "Ana Ruiz",
            "actas",
            "acta_nacimiento",
            "Acta de nacimiento",
            "Depende del estado; un asesor te confirma",
            "nuevo",
            0,
            None,
            None,
            "2025-01-05 10:00:02",
            "2025-01-05 10:00:02",
        ),
    ),
    (
        "INSERT INTO ticketactivity (id, ticket_id, username, action, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (1, 1, "asesora", "tomado", "2025-01-05 10:00:03"),
    ),
    (
        "INSERT INTO teammessage (id, author, body, media_path, created_at) VALUES (?, ?, ?, ?, ?)",
        (1, "asesora", "yo veo la de Ana", None, "2025-01-05 10:00:04"),
    ),
    (
        'INSERT INTO "user" (id, username, password_hash) VALUES (?, ?, ?)',
        (1, "asesora", "hash-viejo"),
    ),
    (
        "INSERT INTO invitation (id, token, phone, created_by, created_at, used_at, used_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (1, "token-viejo", None, "asesora", "2025-01-05 10:00:05", None, None),
    ),
)

# Columnas que la migración DEBE agregar, escritas como literal (no leídas de
# los dicts de app/db.py: leerlas de ahí haría que borrar una entrada del dict
# también borrara la expectativa, y la prueba pasaría en verde).
_COLUMNAS_QUE_AGREGA_LA_MIGRACION = {
    "message": (
        "wa_message_id",
        "wa_status",
        "wa_status_at",
        "media_path",
        "sent_by",
        "habilitado",
    ),
    "teammessage": ("habilitado",),
    "user": ("is_admin", "habilitado", "estado", "phone"),
    "ticket": ("agent_active", "habilitado", "requirements_json"),
    "client": ("habilitado",),
    "conversation": ("habilitado",),
    "invitation": ("habilitado",),
    "ticketactivity": ("habilitado",),
}

# ---------------------------------------------------------------------------
# Esquema YA DESPLEGADO en producción (snapshot congelado)
# ---------------------------------------------------------------------------
# Retrato de las columnas que la base del VPS ya tiene hoy. NO es derivable del
# código: es estado externo, y por eso vive aquí como literal.
#
# ⚠️ Si una prueba se pone roja porque falta una columna, NO la agregues a este
# snapshot: regístrala en el dict `_*_COLUMNS_ADDED_AFTER_LAUNCH` de app/db.py,
# que es lo que la crea en la base de producción. Este snapshot solo crece
# cuando se agrega una TABLA nueva (esas create_all() las crea completas).
_ESQUEMA_DESPLEGADO = {
    # Cobranza con Stripe: cinco tablas NUEVAS, no columnas agregadas a tablas
    # existentes — por eso no hay dict de migracion para ellas y create_all()
    # las crea completas. Generadas desde los modelos, no tecleadas a mano.
    "cobro": (
        "id",
        "ticket_id",
        "phone",
        "client_name",
        "service_id",
        "concepto",
        "moneda",
        "monto_centavos",
        "monto_cotizado_centavos",
        "monto_catalogo_centavos",
        "monto_origen",
        "monto_pagado_centavos",
        "moneda_pagada",
        "monto_reembolsado_centavos",
        "comision_centavos",
        "neto_centavos",
        "moneda_liquidacion",
        "balance_transaction_id",
        "payout_id",
        "metodos_ofrecidos",
        "metodo_usado",
        "msi_meses_ofrecidos",
        "msi_meses_usado",
        "estado",
        "estado_motivo",
        "llave_cobro_vivo",
        "llave_generacion",
        "token_verificacion",
        "stripe_checkout_session_id",
        "stripe_payment_intent_id",
        "stripe_charge_id",
        "stripe_customer_id",
        "url_pago",
        "url_expira_at",
        "voucher_url",
        "voucher_expira_at",
        "detalle_pendiente_at",
        "livemode",
        "discrepancia",
        "disputado",
        "ultimo_evento_id",
        "ultimo_evento_created",
        "confirmado_por_asesor",
        "confirmado_por",
        "confirmado_at",
        "referencia_recibo",
        "plan_id",
        "origen",
        "creado_por",
        "cancelado_por",
        "enviado_at",
        "pagado_at",
        "cancelado_at",
        "aviso_pago_enviado_at",
        "reconciliado_at",
        "created_at",
        "updated_at",
        "habilitado",
    ),
    "intentocobro": (
        "id",
        "cobro_id",
        "numero",
        "clave_idempotencia",
        "operacion",
        "metodos_solicitados",
        "msi_meses",
        "monto_centavos",
        "moneda",
        "estado",
        "stripe_object",
        "stripe_id",
        "stripe_request_id",
        "http_status",
        "respuesta_json",
        "error_codigo",
        "error_mensaje",
        "reintentos",
        "origen",
        "creado_por",
        "wa_message_id",
        "livemode",
        "solicitado_at",
        "respondido_at",
        "cerrado_at",
        "created_at",
        "habilitado",
    ),
    "eventopasarela": (
        "id",
        "stripe_event_id",
        "tipo",
        "api_version",
        "livemode",
        "stripe_created",
        "recibido_at",
        "objeto_id",
        "monto_centavos",
        "moneda",
        "cobro_id",
        "intento_id",
        "payload_json",
        "procesado_at",
        "intentos_proceso",
        "resultado",
        "detalle",
        "habilitado",
    ),
    "plancobro": (
        "id",
        "phone",
        "ticket_id",
        "service_id",
        "concepto",
        "moneda",
        "monto_centavos",
        "periodicidad",
        "dia_cobro",
        "paymentmethod_id",
        "proxima_ejecucion_at",
        "ultima_ejecucion_at",
        "ultimo_periodo_generado",
        "cobros_generados",
        "intentos_fallidos_seguidos",
        "estado",
        "fin_at",
        "stripe_customer_id",
        "stripe_subscription_id",
        "creado_por",
        "created_at",
        "habilitado",
    ),
    "paymentmethod": (
        "id",
        "phone",
        "stripe_customer_id",
        "stripe_payment_method_id",
        "tipo",
        "marca",
        "ultimos4",
        "exp_mes",
        "exp_anio",
        "funding",
        "predeterminado",
        "autorizado_por",
        "autorizado_at",
        "estado",
        "created_at",
        "habilitado",
    ),
    "adminaudit": ("id", "admin", "action", "target", "created_at", "habilitado"),
    "alert": (
        "id",
        "code",
        "level",
        "title",
        "detail",
        "metric_value",
        "active",
        "created_at",
        "resolved_at",
    ),
    "catalogaudit": (
        "id",
        "admin",
        "entity",
        "entity_id",
        "field",
        "old_value",
        "new_value",
        "created_at",
        "habilitado",
    ),
    "catalogcategory": ("id", "name", "position", "habilitado"),
    "catalogservice": (
        "id",
        "category_id",
        "name",
        "price_info",
        "synonyms_json",
        "requirements",
        "position",
        "habilitado",
    ),
    "client": ("phone", "name", "created_at", "habilitado"),
    "clientnote": ("id", "phone", "author", "body", "created_at", "habilitado"),
    "conversation": (
        "phone",
        "state",
        "context_json",
        "failed_attempts",
        "updated_at",
        "habilitado",
    ),
    "feedback": ("id", "ticket_id", "phone", "rating", "created_at", "habilitado"),
    "followup": (
        "id",
        "ticket_id",
        "username",
        "note",
        "due_at",
        "done_at",
        "created_at",
        "habilitado",
    ),
    "invitation": (
        "id",
        "token",
        "phone",
        "created_by",
        "created_at",
        "used_at",
        "used_by",
        "habilitado",
    ),
    "ipgeocache": ("ip", "geo", "resolved_at"),
    "loginevent": (
        "id",
        "username",
        "success",
        "reason",
        "ip",
        "geo",
        "device",
        "user_agent",
        "created_at",
        "habilitado",
    ),
    "message": (
        "id",
        "phone",
        "direction",
        "body",
        "created_at",
        "wa_message_id",
        "wa_status",
        "wa_status_at",
        "media_path",
        "sent_by",
        "habilitado",
    ),
    "nluevent": (
        "id",
        "phone",
        "text",
        "capa",
        "service_id",
        "confianza",
        "termino_en_ticket",
        "created_at",
        "habilitado",
    ),
    "savedreply": ("id", "username", "title", "body", "created_at", "habilitado"),
    "synonymproposal": (
        "id",
        "service_id",
        "synonym",
        "origin",
        "status",
        "decided_by",
        "decided_at",
        "created_at",
        "habilitado",
    ),
    "teammessage": ("id", "author", "body", "media_path", "created_at", "habilitado"),
    "ticket": (
        "id",
        "phone",
        "client_name",
        "category",
        "service_id",
        "service_name",
        "price_info",
        "status",
        "escalado",
        "notes",
        "assigned_to",
        "agent_active",
        "created_at",
        "updated_at",
        "habilitado",
    ),
    "ticketactivity": ("id", "ticket_id", "username", "action", "created_at", "habilitado"),
    "user": ("id", "username", "password_hash", "is_admin", "habilitado", "estado", "phone"),
    "websession": (
        "id",
        "token",
        "username",
        "ip",
        "geo",
        "device",
        "user_agent",
        "created_at",
        "last_seen_at",
        "ended_at",
        "habilitado",
    ),
}

# Espeja las llamadas a _add_missing_columns() dentro de init_db(): sirve para
# detectar erratas en las LLAVES de los dicts (un "wa_statuss" crearía la
# columna equivocada en producción y dejaría la buena sin crear).
_DICTS_DE_MIGRACION = {
    "message": db._MESSAGE_COLUMNS_ADDED_AFTER_LAUNCH,
    "teammessage": db._TEAMMESSAGE_COLUMNS_ADDED_AFTER_LAUNCH,
    "user": db._USER_COLUMNS_ADDED_AFTER_LAUNCH,
    "ticket": db._TICKET_COLUMNS_ADDED_AFTER_LAUNCH,
    **{tabla: db._HABILITADO_COLUMN for tabla in db._TABLES_NEEDING_HABILITADO},
}


# ---------------------------------------------------------------------------
# Ayudas y fixtures locales
# ---------------------------------------------------------------------------


def _columnas(engine, tabla: str) -> list[str]:
    """Nombres de columna en el orden físico (row[1] de PRAGMA table_info)."""
    with engine.connect() as conn:
        return [row[1] for row in conn.exec_driver_sql(f'PRAGMA table_info("{tabla}")')]


def _indices(engine, tabla: str) -> dict[str, int]:
    """Índices de la tabla -> bandera de unicidad (row[1] nombre, row[2] unique)."""
    with engine.connect() as conn:
        return {row[1]: row[2] for row in conn.exec_driver_sql(f'PRAGMA index_list("{tabla}")')}


def _engine_de_archivo(ruta: Path):
    return create_engine(f"sqlite:///{ruta}", connect_args={"check_same_thread": False})


@pytest.fixture()
def bd_de_lanzamiento(tmp_path, monkeypatch):
    """Base SQLite de ARCHIVO con el esquema viejo y datos dentro, ya enchufada
    como `app.db.engine` (el autouse _isolated_db del conftest apunta a otra
    base en memoria: hay que volver a parchear, y sobre el módulo app.db para
    que init_db() y _add_missing_columns() vean el engine correcto)."""
    ruta = tmp_path / "vieja.db"
    con = sqlite3.connect(ruta)
    try:
        for ddl in _DDL_DE_LANZAMIENTO:
            con.execute(ddl)
        for sql, valores in _DATOS_DE_LANZAMIENTO:
            con.execute(sql, valores)
        con.commit()
    finally:
        con.close()
    engine = _engine_de_archivo(ruta)
    monkeypatch.setattr(db, "engine", engine)
    return engine


@pytest.fixture()
def bd_migrada(bd_de_lanzamiento):
    """La base vieja después de un arranque real (init_db() una vez)."""
    db.init_db()
    return bd_de_lanzamiento


# ---------------------------------------------------------------------------
# 1. Migración desde una base "vieja" con datos
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tabla", sorted(_COLUMNAS_QUE_AGREGA_LA_MIGRACION))
def test_la_migracion_agrega_las_columnas_nuevas_a_las_tablas_viejas(bd_migrada, tabla):
    # REGLA: el deploy del VPS corre sobre una base que ya existe. create_all()
    # NO toca las tablas existentes, así que las columnas posteriores al
    # lanzamiento solo llegan por _add_missing_columns(); si una falta, el
    # arranque revienta en producción.
    presentes = set(_columnas(bd_migrada, tabla))
    faltantes = [c for c in _COLUMNAS_QUE_AGREGA_LA_MIGRACION[tabla] if c not in presentes]
    assert not faltantes, f"init_db() no agregó a {tabla}: {faltantes}"


def test_la_migracion_no_pierde_los_datos_historicos(bd_migrada):
    # REGLA: migrar es agregar columnas, nunca recrear tablas. El historial de
    # trámites (cliente, conversación, mensajes, solicitud) es el activo del
    # negocio y tiene que sobrevivir intacto al deploy.
    with bd_migrada.connect() as conn:
        # Se consulta por la forma CANÓNICA: init_db() normaliza los teléfonos
        # históricos. El dato tiene que seguir ahí, solo bajo la llave nueva.
        cliente = conn.exec_driver_sql(
            "SELECT phone, name FROM client WHERE phone = ?", (PHONE_CANONICO,)
        ).fetchone()
        conversacion = conn.exec_driver_sql(
            "SELECT state, context_json, failed_attempts FROM conversation WHERE phone = ?",
            (PHONE_CANONICO,),
        ).fetchone()
        mensaje = conn.exec_driver_sql(
            "SELECT phone, direction, body FROM message WHERE id = 1"
        ).fetchone()
        ticket = conn.exec_driver_sql(
            "SELECT service_id, service_name, status, price_info FROM ticket WHERE id = 1"
        ).fetchone()
        usuario = conn.exec_driver_sql(
            'SELECT username, password_hash FROM "user" WHERE id = 1'
        ).fetchone()

    assert cliente == (PHONE_CANONICO, "Ana Ruiz")
    assert conversacion == ("menu_servicio", '{"categoria": "actas"}', 1)
    assert mensaje == (PHONE_CANONICO, "in", "quiero mi acta de nacimiento")
    assert ticket == (
        "acta_nacimiento",
        "Acta de nacimiento",
        "nuevo",
        "Depende del estado; un asesor te confirma",
    )
    assert usuario == ("asesora", "hash-viejo")


def test_las_filas_viejas_quedan_habilitadas_tras_migrar(bd_migrada):
    # REGLA: `habilitado` es el soft delete del panel (todas las vistas filtran
    # por habilitado == True). Si la columna se agregara sin DEFAULT, las filas
    # anteriores al deploy quedarían en NULL y el historial desaparecería del
    # dashboard sin que nadie borre nada.
    tablas = (
        "client",
        "conversation",
        "message",
        "ticket",
        "ticketactivity",
        "teammessage",
        "invitation",
        "user",
    )
    with bd_migrada.connect() as conn:
        valores = {
            tabla: conn.exec_driver_sql(f'SELECT habilitado FROM "{tabla}"').fetchall()
            for tabla in tablas
        }
    for tabla, filas in valores.items():
        assert filas, f"la fila histórica de {tabla} desapareció"
        assert all(fila[0] == 1 for fila in filas), f"{tabla} quedó con habilitado NULL/0"


def test_los_defaults_de_las_columnas_nuevas_respetan_las_reglas_de_negocio(bd_migrada):
    # REGLA (valores literales a propósito, no derivados de los dicts):
    #  - ticket.agent_active = 1 -> tras migrar el bot sigue contestando; apagarlo
    #    es una decisión explícita del colaborador, no un efecto del deploy.
    #  - user.is_admin = 0 -> migrar JAMÁS otorga privilegios de admin.
    #  - user.estado = 'disponible' -> el colaborador aparece disponible al equipo.
    with bd_migrada.connect() as conn:
        agent_active = conn.exec_driver_sql(
            "SELECT agent_active FROM ticket WHERE id = 1"
        ).scalar()
        is_admin, estado, telefono = conn.exec_driver_sql(
            'SELECT is_admin, estado, phone FROM "user" WHERE id = 1'
        ).fetchone()

    assert agent_active == 1
    assert is_admin == 0
    assert estado == "disponible"
    assert telefono is None  # columna anulable: sin default inventado


def test_init_db_crea_las_tablas_que_no_existian_en_la_base_vieja(bd_migrada):
    # REGLA: el mismo arranque que migra columnas tiene que crear las tablas
    # nacidas después (nluevent, alert, catalog*, ...). Si create_all() fuera un
    # no-op, el cron nocturno fallaría en silencio contra tablas inexistentes.
    with bd_migrada.connect() as conn:
        tablas = {
            row[0]
            for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    nacidas_despues = {"nluevent", "alert", "catalogservice", "catalogcategory", "synonymproposal"}
    assert nacidas_despues <= tablas


def test_el_indice_unico_de_wamid_se_crea_tambien_sobre_una_tabla_vieja(bd_migrada):
    # REGLA: ux_message_wa_message_id es la única red real contra webhooks
    # duplicados de Meta. La columna wa_message_id es posterior al lanzamiento,
    # así que en producción el índice se crea sobre una tabla que YA existía;
    # row[2] es la bandera de unicidad: mirar solo el nombre dejaría pasar una
    # degradación a CREATE INDEX.
    indices = _indices(bd_migrada, "message")
    assert "ux_message_wa_message_id" in indices
    assert indices["ux_message_wa_message_id"] == 1, "el índice existe pero no es UNIQUE"

    with bd_migrada.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO message (phone, direction, body, created_at, wa_message_id, habilitado) "
            "VALUES (?, 'in', 'hola', '2025-02-01 09:00:00', 'wamid.DUP', 1)",
            (PHONE,),
        )
    with pytest.raises(IntegrityError):
        with bd_migrada.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO message (phone, direction, body, created_at, wa_message_id, "
                "habilitado) VALUES (?, 'in', 'hola otra vez', '2025-02-01 09:00:01', "
                "'wamid.DUP', 1)",
                (PHONE,),
            )


def test_varios_mensajes_pueden_quedar_sin_wamid(bd_migrada):
    # REGLA: cada mensaje saliente se guarda ANTES de conocer su wamid y se
    # taggea después (webhook._tag_sent_message). Si wa_message_id se hubiera
    # migrado como NOT NULL, o el índice no fuera parcial, el segundo saliente
    # sin taggear tumbaría la respuesta al cliente.
    with bd_migrada.begin() as conn:
        for i in range(3):
            conn.exec_driver_sql(
                "INSERT INTO message (phone, direction, body, created_at, habilitado) "
                "VALUES (?, 'out', ?, '2025-02-01 09:00:00', 1)",
                (PHONE, f"respuesta {i}"),
            )
    with bd_migrada.connect() as conn:
        sin_tag = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM message WHERE wa_message_id IS NULL"
        ).scalar()
    assert sin_tag == 4  # el mensaje histórico + los 3 salientes


# ---------------------------------------------------------------------------
# 2. Idempotencia: el cron y el arranque web llaman init_db() todo el tiempo
# ---------------------------------------------------------------------------


def test_init_db_es_idempotente_tres_veces_seguidas(bd_de_lanzamiento):
    # REGLA: init_db() corre en cada arranque del contenedor y en cada corrida
    # del cron nocturno (app.jobs.nightly). Tiene que ser repetible sin
    # excepciones y sin duplicar columnas, índices ni filas.
    db.init_db()
    columnas_tras_la_primera = {
        tabla: _columnas(bd_de_lanzamiento, tabla) for tabla in _ESQUEMA_DESPLEGADO
    }

    for vuelta in (2, 3):
        try:
            db.init_db()
        except OperationalError as exc:  # pragma: no cover - solo si hay regresión
            pytest.fail(f"init_db() no es idempotente: reventó en la vuelta {vuelta}: {exc}")

    for tabla, esperadas in columnas_tras_la_primera.items():
        actuales = _columnas(bd_de_lanzamiento, tabla)
        assert actuales == esperadas, f"{tabla} cambió de columnas al repetir init_db()"
        assert len(actuales) == len(set(actuales)), f"{tabla} tiene columnas duplicadas"

    with bd_de_lanzamiento.connect() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM message").scalar() == 1
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM ticket").scalar() == 1
        assert conn.exec_driver_sql(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'index' AND name = 'ux_message_wa_message_id'"
        ).scalar() == 1


# ---------------------------------------------------------------------------
# 3. El candado: modelos vs. lo que init_db() deja realmente en la base
# ---------------------------------------------------------------------------


def test_toda_columna_de_los_modelos_existe_tras_init_db_sobre_el_esquema_desplegado(
    tmp_path, monkeypatch
):
    # REGLA (la más importante del archivo): agregar un campo a app/models.py
    # NO lo crea en la base de producción — las tablas ya existen y create_all()
    # las salta. Solo lo crea el dict `_*_COLUMNS_ADDED_AFTER_LAUNCH` de
    # app/db.py. Aquí se levanta una base con el esquema QUE HOY TIENE EL VPS
    # (snapshot congelado, estado externo) y se comprueba que después de
    # init_db() la base cubre TODO lo que declaran los modelos.
    #
    # Si esta prueba se pone roja nombrando una columna: NO toques el snapshot,
    # agrega la columna al dict correspondiente de app/db.py (y al de
    # app/archive.py si la tabla se espeja a MSSQL).
    ruta = tmp_path / "produccion.db"
    con = sqlite3.connect(ruta)
    try:
        for tabla, columnas in _ESQUEMA_DESPLEGADO.items():
            declaracion = ", ".join(f'"{c}"' for c in columnas)
            con.execute(f'CREATE TABLE "{tabla}" ({declaracion})')
        con.commit()
    finally:
        con.close()

    engine = _engine_de_archivo(ruta)
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()

    faltantes: dict[str, list[str]] = {}
    for nombre, tabla in SQLModel.metadata.tables.items():
        if nombre not in _ESQUEMA_DESPLEGADO:
            continue  # tabla nueva: create_all() la crea completa, no necesita migración
        en_la_base = set(_columnas(engine, nombre))
        ausentes = [c.name for c in tabla.columns if c.name not in en_la_base]
        if ausentes:
            faltantes[nombre] = ausentes
    assert not faltantes, (
        "columnas declaradas en app/models.py que init_db() NO crea sobre una base "
        f"ya existente: {faltantes}. Regístralas en el dict "
        "_*_COLUMNS_ADDED_AFTER_LAUNCH de app/db.py."
    )


def test_toda_tabla_de_los_modelos_esta_registrada_en_el_snapshot_de_produccion():
    # REGLA: el candado de arriba solo puede vigilar las tablas que conoce. Una
    # tabla nueva hay que anotarla aquí (con sus columnas de hoy) para que a
    # partir de la siguiente columna que se le agregue quede vigilada.
    del_metadata = set(SQLModel.metadata.tables)
    del_snapshot = set(_ESQUEMA_DESPLEGADO)
    assert del_metadata - del_snapshot == set(), (
        "tablas nuevas sin registrar en _ESQUEMA_DESPLEGADO: "
        f"{sorted(del_metadata - del_snapshot)}"
    )
    assert del_snapshot - del_metadata == set(), (
        "tablas del snapshot que ya no existen en app/models.py: "
        f"{sorted(del_snapshot - del_metadata)}"
    )


def test_los_dicts_de_migracion_no_declaran_columnas_fantasma():
    # REGLA: una errata en la LLAVE de un dict (p.ej. "wa_statuss") crea en
    # producción una columna basura y deja la de verdad sin crear, sin ruido.
    # Toda llave tiene que corresponder a un campo real del modelo.
    fantasmas: dict[str, list[str]] = {}
    for tabla, columnas in _DICTS_DE_MIGRACION.items():
        del_modelo = {c.name for c in SQLModel.metadata.tables[tabla].columns}
        sobrantes = sorted(set(columnas) - del_modelo)
        if sobrantes:
            fantasmas[tabla] = sobrantes
    assert not fantasmas, (
        f"columnas en los dicts de app/db.py que ningún modelo declara: {fantasmas}"
    )


# ---------------------------------------------------------------------------
# 4. Base nueva vs. base migrada: no pueden divergir
# ---------------------------------------------------------------------------


def test_la_base_migrada_y_una_base_nueva_terminan_con_las_mismas_columnas(
    bd_migrada, tmp_path, monkeypatch
):
    # REGLA: el VPS migra una base con años de datos; los entornos nuevos (y
    # estas pruebas) nacen de create_all(). Si los dos caminos no convergen al
    # mismo esquema, aparece un bug que SOLO se ve en producción.
    ruta_nueva = tmp_path / "nueva.db"
    engine_nuevo = _engine_de_archivo(ruta_nueva)
    monkeypatch.setattr(db, "engine", engine_nuevo)
    db.init_db()

    diferencias: dict[str, dict[str, list[str]]] = {}
    for tabla in _ESQUEMA_DESPLEGADO:
        en_migrada = set(_columnas(bd_migrada, tabla))
        en_nueva = set(_columnas(engine_nuevo, tabla))
        if en_migrada != en_nueva:
            diferencias[tabla] = {
                "solo_en_migrada": sorted(en_migrada - en_nueva),
                "solo_en_nueva": sorted(en_nueva - en_migrada),
            }
    assert not diferencias, f"el esquema migrado y el nuevo divergen: {diferencias}"


def test_una_base_nueva_tambien_queda_con_el_indice_unico_de_wamid(tmp_path, monkeypatch):
    # REGLA: la protección anti-duplicados no puede depender de que la base
    # venga de una migración; un entorno recién creado tiene que nacer con ella.
    engine = _engine_de_archivo(tmp_path / "nueva.db")
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()

    indices = _indices(engine, "message")
    assert indices.get("ux_message_wa_message_id") == 1


# ---------------------------------------------------------------------------
# 5. create_all() no puede volver a ser un no-op para el cron
# ---------------------------------------------------------------------------


def test_importar_solo_app_db_registra_todos_los_modelos_en_el_metadata(tmp_path):
    # REGLA: `python -m app.jobs` llama init_db() ANTES de importar los módulos
    # que traen los modelos. Si app/db.py dejara de importar app.models,
    # SQLModel.metadata estaría vacío y create_all() crearía CERO tablas en
    # silencio (fue un bug real). Subproceso limpio porque en esta suite los
    # modelos ya están importados por otros tests.
    codigo = (
        "import app.db;"
        "from sqlmodel import SQLModel;"
        "print(','.join(sorted(SQLModel.metadata.tables)))"
    )
    entorno = {**os.environ, "DB_PATH": str(tmp_path / "no-se-debe-crear.db")}
    proceso = subprocess.run(
        [sys.executable, "-c", codigo],
        cwd=_RAIZ,
        capture_output=True,
        text=True,
        timeout=120,
        env=entorno,
    )
    assert proceso.returncode == 0, proceso.stderr
    tablas = set(proceso.stdout.strip().split(","))
    assert tablas == set(SQLModel.metadata.tables), (
        "importar app.db ya no registra todos los modelos: create_all() sería un "
        f"no-op en el cron. Faltan: {sorted(set(SQLModel.metadata.tables) - tablas)}"
    )
