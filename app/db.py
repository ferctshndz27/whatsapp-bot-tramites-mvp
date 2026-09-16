import logging

from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine

# Importar los modelos aquí es lo que puebla SQLModel.metadata: sin esto,
# el create_all() de init_db() es un no-op para quien no haya importado
# app.models por su cuenta. El arranque web funcionaba de rebote, pero el cron
# (`python -m app.jobs`) llama init_db() ANTES de importar los módulos que
# traen los modelos: creaba cero tablas y las alertas fallaban en silencio.
from app import models  # noqa: F401
from app.config import settings

logger = logging.getLogger(__name__)

engine = create_engine(f"sqlite:///{settings.db_path}", connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    # WAL: lecturas no bloquean escrituras (necesario para backup en caliente
    # y para que el dashboard lea mientras el bot escribe mensajes entrantes).
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()

# ponytail: no alembic for this MVP; new nullable columns are added by hand here.
# Upgrade to a real migration tool if the schema starts changing often.
_MESSAGE_COLUMNS_ADDED_AFTER_LAUNCH = {
    "wa_message_id": "VARCHAR",
    "wa_status": "VARCHAR",
    "wa_status_at": "DATETIME",
    "media_path": "VARCHAR",
    "sent_by": "VARCHAR",
    "habilitado": "BOOLEAN NOT NULL DEFAULT 1",
    # DEFAULT 'meta': todo lo que ya existe entró o salió por la cuenta de Meta,
    # que es la única que hubo hasta ahora. Ponerlo NULL dejaría el histórico sin
    # canal y los reportes por cuenta saldrían mal para siempre.
    "provider": "VARCHAR NOT NULL DEFAULT 'meta'",
    # Medidor de mensajería (fase 2). Nullable SIN default a propósito: el
    # histórico previo no tiene veredicto de cobro de Meta y NULL lo dice; un
    # DEFAULT inventaría que esos mensajes fueron gratis.
    "pricing_category": "VARCHAR",
    "pricing_type": "VARCHAR",
    "pricing_billable": "BOOLEAN",
}
_CONVERSATION_COLUMNS_ADDED_AFTER_LAUNCH = {
    "provider": "VARCHAR NOT NULL DEFAULT 'meta'",
}
_TEAMMESSAGE_COLUMNS_ADDED_AFTER_LAUNCH = {
    "habilitado": "BOOLEAN NOT NULL DEFAULT 1",
    "room_id": "INTEGER",
}
# «Eliminar chat» solo para él (como WhatsApp). Nullable sin default a
# propósito: NULL es «nunca lo eliminó»; un default inventaría una fecha.
_CHATMEMBER_COLUMNS_ADDED_AFTER_LAUNCH = {
    "borrado_at": "DATETIME",
}
_USER_COLUMNS_ADDED_AFTER_LAUNCH = {
    "is_admin": "BOOLEAN NOT NULL DEFAULT 0",
    "habilitado": "BOOLEAN NOT NULL DEFAULT 1",
    "estado": "VARCHAR NOT NULL DEFAULT 'disponible'",
    "phone": "VARCHAR",
    "solo_su_oficina": "BOOLEAN NOT NULL DEFAULT 0",
}
# Tablas que ya existían antes de agregar el campo "habilitado" (soft delete);
# message/teammessage/user ya lo tienen en sus dicts propios de arriba.
_HABILITADO_COLUMN = {"habilitado": "BOOLEAN NOT NULL DEFAULT 1"}
_TABLES_NEEDING_HABILITADO = ("client", "conversation", "ticket", "invitation", "ticketactivity")
# Multi-oficina (docs/SPEC-MULTI-OFICINA.md §2.3). office_id: DEFAULT 1 porque
# TODO lo que existe hoy es de Tlaxcala — es la única oficina que ha operado.
# Dejarlo NULL dejaría el histórico sin oficina y cada reporte por oficina
# saldría mal para siempre; NOT NULL sin DEFAULT tumbaría el arranque (SQLite
# lo rechaza sobre tabla con datos, ver requirements_json abajo). SQLite rellena
# las filas existentes con 1 en el mismo ALTER: no hay backfill que escribir.
_OFFICE_COLUMN = {"office_id": f"INTEGER NOT NULL DEFAULT {models.OFICINA_TLAXCALA_ID}"}
# Las 15 tablas que el panel filtra directamente. Las que solo se alcanzan por
# join (message, ticketlabel, intentocobro, ...) heredan la oficina del padre y
# NO llevan la columna. Mismo mecanismo que _TABLES_NEEDING_HABILITADO: 10 de
# estas no tienen dict propio — nacieron por create_all, pero en el VPS ya
# existen y office_id solo les llega por ALTER.
_TABLES_NEEDING_OFFICE = (
    "client", "conversation", "ticket", "user", "teammessage", "cobro",
    "catalogcategory", "catalogservice", "synonymproposal", "nluevent", "infodoc",
    "label", "savedreply", "plantilla", "agentconfig",
    # La 16ª, agregada después de la Fase 7: la oficina se decide al invitar
    # (models.Invitation), no cuando la persona acepta.
    "invitation",
)
_TICKET_COLUMNS_ADDED_AFTER_LAUNCH = {
    "agent_active": "BOOLEAN NOT NULL DEFAULT 1",
    # Nullable a propósito: los folios viejos no tienen requisitos capturados y
    # migrar no debe inventarlos (además SQLite rechaza ADD COLUMN NOT NULL sin
    # DEFAULT sobre una tabla con datos: tumbaría el arranque en el VPS).
    "requirements_json": "VARCHAR",
    "provider": "VARCHAR NOT NULL DEFAULT 'meta'",
    # Métricas de operación (fase 1). NULL en folios viejos a propósito: un
    # NULL honesto vale más que un tiempo inventado desde created_at.
    "first_response_at": "DATETIME",
    "first_response_by": "VARCHAR",
    "resolved_at": "DATETIME",
    "status_changed_at": "DATETIME",
}
_CLIENT_COLUMNS_ADDED_AFTER_LAUNCH = {
    "email": "VARCHAR",
    "origen": "VARCHAR",
    "idioma": "VARCHAR NOT NULL DEFAULT 'es-MX'",
    "opt_in_at": "DATETIME",
    "opt_in_source": "VARCHAR",
    "opt_out_at": "DATETIME",
    "atributos_json": "VARCHAR",
    "last_activity_at": "DATETIME",
    "notas_internas": "VARCHAR",
    # Embudo del CRM (/crm). Nullable sin default a propósito: NULL es «en la
    # primera etapa» y evita un UPDATE masivo que el espejo no vería.
    "etapa_crm_id": "INTEGER",
    "etapa_crm_at": "DATETIME",
    "etapa_crm_por": "VARCHAR",
}


# El candado ux_cobro_vivo es la defensa contra el doble cobro: mientras un
# cobro tenga instrumento vivo (link o voucher de OXXO), no se puede crear otro
# para el mismo (teléfono, servicio). Va por servicio y no por folio porque con
# OXXO el cliente paga días después, cuando el folio ya se cerró y el bot
# fabricó uno nuevo — un candado por ticket_id no vería nada.
_COBRANZA_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_cobro_vivo "
    "ON cobro(llave_cobro_vivo) WHERE llave_cobro_vivo IS NOT NULL",
    # Mata el F5 y el doble clic en "Generar cobro".
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_cobro_generacion "
    "ON cobro(llave_generacion) WHERE llave_generacion IS NOT NULL",
    # Impiden que dos cobros reclamen el mismo objeto de Stripe al resolver un evento.
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_cobro_payment_intent "
    "ON cobro(stripe_payment_intent_id) WHERE stripe_payment_intent_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_cobro_session "
    "ON cobro(stripe_checkout_session_id) WHERE stripe_checkout_session_id IS NOT NULL",
    # Un solo intento en vuelo por cobro: si el proceso murió sin saber si Stripe
    # alcanzó a crear el instrumento, nadie puede lanzar otro hasta resolverlo.
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_intento_vivo "
    "ON intentocobro(cobro_id) WHERE estado = 'enviando'",
    # La cola del barrido de reconciliación: filtra por estado y ordena por
    # reconciliado_at para rotar. Sin él, cada noche es un scan de cobro.
    "CREATE INDEX IF NOT EXISTS ix_cobro_reconciliacion "
    "ON cobro(estado, reconciliado_at)",
)


# Tablas con una columna de teléfono que apunta al mismo humano. Las dos
# primeras lo tienen como PRIMARY KEY, así que normalizarlas puede colisionar
# (ver _normalize_phones). `user.phone` es el celular del colaborador: no es
# llave de nada, pero se normaliza por consistencia.
_TABLAS_CON_TELEFONO = ("message", "ticket", "clientnote", "feedback", "nluevent", "user")
_TABLAS_CON_TELEFONO_COMO_PK = ("client", "conversation")


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    # Antes de la siembra: con la columna retirada todavía NOT NULL, hasta el
    # INSERT de Tlaxcala revienta.
    for table, columns in _COLUMNS_RETIRED.items():
        _drop_retired_columns(table, columns)
    # SPEC §7: la oficina 1 existe ANTES de que ninguna tabla reciba office_id.
    _seed_office_tlaxcala()
    _add_missing_columns("message", _MESSAGE_COLUMNS_ADDED_AFTER_LAUNCH)
    _add_missing_columns("teammessage", _TEAMMESSAGE_COLUMNS_ADDED_AFTER_LAUNCH)
    _add_missing_columns("chatmember", _CHATMEMBER_COLUMNS_ADDED_AFTER_LAUNCH)
    _add_missing_columns("user", _USER_COLUMNS_ADDED_AFTER_LAUNCH)
    _add_missing_columns("ticket", _TICKET_COLUMNS_ADDED_AFTER_LAUNCH)
    _add_missing_columns("conversation", _CONVERSATION_COLUMNS_ADDED_AFTER_LAUNCH)
    _add_missing_columns("client", _CLIENT_COLUMNS_ADDED_AFTER_LAUNCH)
    _seed_crm_etapas()
    for table in _TABLES_NEEDING_HABILITADO:
        _add_missing_columns(table, _HABILITADO_COLUMN)
    for table in _TABLES_NEEDING_OFFICE:
        _add_missing_columns(table, _OFFICE_COLUMN)
    with engine.begin() as conn:
        # Respaldo contra webhooks duplicados concurrentes: el chequeo en
        # conversation._already_processed cubre el caso normal; este índice
        # garantiza unicidad si dos reintentos de Meta llegan a la vez.
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_message_wa_message_id "
                "ON message(wa_message_id) WHERE wa_message_id IS NOT NULL"
            )
        )
        # Cobranza: índices únicos PARCIALES. El "WHERE ... IS NOT NULL" es lo
        # que los hace útiles — permiten infinitas filas con el campo en NULL y
        # prohíben dos con el mismo valor. SQLModel no sabe declararlos, así que
        # van a mano igual que el de arriba.
        for sql in _COBRANZA_INDEXES:
            conn.execute(text(sql))
        # Etiquetas: una fila puente VIVA por (folio, etiqueta). Parcial sobre
        # habilitado para que re-etiquetar tras un quitar (soft delete) pueda
        # reactivar la fila vieja o crear una nueva sin chocar con el índice.
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_ticketlabel_viva "
                "ON ticketlabel(ticket_id, label_id) WHERE habilitado = 1"
            )
        )
        # Llaves por oficina: una fila VIVA por (oficina, proveedor, clave).
        # Parcial sobre habilitado, como ux_ticketlabel_viva: dar de baja es
        # apagar la fila, y volver a dar de alta la misma llave no choca con la
        # apagada — el audit conserva la vieja.
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_officecredential_viva "
                "ON officecredential(office_id, provider, clave) WHERE habilitado = 1"
            )
        )

    _normalize_phones()


def _seed_crm_etapas() -> None:
    """Siembra las cuatro etapas del embudo del CRM si la tabla está vacía.

    Vive en init_db por la misma razón que _seed_office_tlaxcala: el cron
    también llama init_db() y puede ser el primer proceso sobre una base vacía.
    Se comprueba «tabla vacía» y NO cada nombre: un admin que renombró o dio de
    baja una etapa no la quiere de vuelta en el siguiente arranque. Va por el
    ORM para que el espejo MSSQL reciba las filas por su listener. Los ids van
    EXPLÍCITOS (1..4) por la misma razón que Tlaxcala: una base restaurada —o la
    tabla sin PK del candado de tests/test_db_migrations.py— no genera ids sola.
    """
    from sqlmodel import select

    with Session(engine) as session:
        if session.exec(select(models.CrmEtapa)).first() is not None:
            return
        for orden, (nombre, color) in enumerate(models.ETAPAS_CRM_DEFAULT):
            session.add(models.CrmEtapa(id=orden + 1, nombre=nombre, color=color, ord=orden))
        session.commit()


def _seed_office_tlaxcala() -> None:
    """Siembra la oficina 1 (Tlaxcala) si no existe.

    Vive en init_db y no en main.on_startup porque el cron (app/jobs.py:43)
    también llama init_db() y puede ser el primer proceso que toque una base
    vacía. Idempotente: init_db corre en cada arranque y en cada corrida del
    cron. id=1 EXPLÍCITO — no se confía en el autoincrement: la fila es lo que
    referencia cada `office_id DEFAULT 1` del histórico, y una base restaurada
    (o la tabla sin PK del candado de tests/test_db_migrations.py) tiene que
    dar el mismo 1. Se comprueba por id, no por «tabla vacía»: una Tlaxcala
    dada de baja (habilitado=0) NO se resiembra. `engine` se resuelve en la
    llamada, que es lo que parchan los tests (tests/conftest.py:14).
    """
    with Session(engine) as session:
        if session.get(models.Office, models.OFICINA_TLAXCALA_ID) is not None:
            return
        session.add(
            models.Office(
                id=models.OFICINA_TLAXCALA_ID,
                nombre="Tlaxcala",
                slug="tlaxcala",
                ciudad="Tlaxcala",
            )
        )
        try:
            session.commit()
        except IntegrityError:
            # Carrera arranque-del-contenedor vs cron: los dos vieron la tabla
            # vacía y el otro proceso ganó el INSERT. La fila ya está.
            session.rollback()
            with Session(engine) as verificacion:
                if verificacion.get(models.Office, models.OFICINA_TLAXCALA_ID) is None:
                    # NO fue la carrera: hay otra oficina ocupando el nombre o el
                    # slug y la fila 1 sigue sin existir, así que todo office_id
                    # del histórico apunta a la nada. Silenciarlo lo volvería
                    # invisible arranque tras arranque.
                    logger.error(
                        "No se pudo sembrar la oficina %s (Tlaxcala): el nombre o el slug "
                        "ya los tiene otra fila. Todo office_id del histórico apunta a una "
                        "oficina inexistente hasta que se corrija a mano.",
                        models.OFICINA_TLAXCALA_ID,
                    )


def _normalize_phones() -> None:
    """Lleva los teléfonos ya guardados al formato canónico (ver utils).

    Hace falta porque las filas anteriores a esta migración traen el formato
    crudo de Meta ('5215500000000'). Sin esto, normalizar solo lo ENTRANTE haría
    que un cliente existente no se encuentre y se cree duplicado — justo el bug
    que la normalización quiere prevenir.

    Idempotente: sobre datos ya canónicos no escribe nada, así que corre seguro
    en cada arranque y en cada corrida del cron.
    """
    from app.utils import normalize_phone

    with engine.begin() as conn:
        existentes = {
            row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }

        # Las de PK primero: si el canónico YA existe, hay dos filas para el
        # mismo humano y se conserva la canónica (la vieja se descarta después
        # de repuntar a sus hijos). Si no existe, es un simple renombre.
        for tabla in _TABLAS_CON_TELEFONO_COMO_PK:
            if tabla not in existentes:
                continue
            for (crudo,) in conn.execute(text(f"SELECT phone FROM {tabla}")).fetchall():
                canonico = normalize_phone(crudo)
                if not canonico or canonico == crudo:
                    continue
                ya_existe = conn.execute(
                    text(f"SELECT 1 FROM {tabla} WHERE phone = :c"), {"c": canonico}
                ).first()
                if ya_existe:
                    # Duplicado real: el histórico se preserva porque los hijos
                    # (mensajes, tickets, notas) se repuntan al canónico abajo.
                    conn.execute(text(f"DELETE FROM {tabla} WHERE phone = :p"), {"p": crudo})
                    logger.warning(
                        "Teléfono duplicado en %s: %s se fusiona con %s", tabla, crudo, canonico
                    )
                else:
                    conn.execute(
                        text(f"UPDATE {tabla} SET phone = :c WHERE phone = :p"),
                        {"c": canonico, "p": crudo},
                    )

        for tabla in _TABLAS_CON_TELEFONO:
            if tabla not in existentes:
                continue
            columna = "phone"
            filas = conn.execute(
                text(f'SELECT DISTINCT {columna} FROM "{tabla}" WHERE {columna} IS NOT NULL')
            ).fetchall()
            for (crudo,) in filas:
                canonico = normalize_phone(crudo)
                if canonico and canonico != crudo:
                    conn.execute(
                        text(f'UPDATE "{tabla}" SET {columna} = :c WHERE {columna} = :p'),
                        {"c": canonico, "p": crudo},
                    )


def _add_missing_columns(table: str, columns: dict[str, str]) -> None:
    with engine.begin() as conn:
        existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
        for name, col_type in columns.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}"))


# Columnas que se quitaron de un modelo pero que la base ya desplegada conserva.
# NO son huérfanas inofensivas: create_all() las creó NOT NULL y SIN DEFAULT
# (el default vivía en Python), así que todo INSERT del modelo —que ya no las
# llena— revienta. office.zona_horaria (PR 1 multi-oficina, retirada en el PR 4)
# dejó /oficinas sin poder dar de alta nada. DROP COLUMN existe desde SQLite
# 3.35; la imagen python:3.12-slim trae 3.40. No sirve para una columna con
# índice, UNIQUE o PK: esa necesita reconstruir la tabla.
_COLUMNS_RETIRED = {
    "office": ("zona_horaria",),
}


def _drop_retired_columns(table: str, columns: tuple[str, ...]) -> None:
    with engine.begin() as conn:
        existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
        for name in columns:
            if name in existing:
                logger.warning("Quitando la columna retirada %s.%s", table, name)
                conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {name}"))


def get_session() -> Session:
    return Session(engine)
