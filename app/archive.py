"""Archivo espejo en MSSQL de toda la data operativa.

SQLite sigue siendo la base operativa del bot; este módulo replica a SQL
Server cada entidad: clientes, mensajes de WhatsApp, chat de equipo,
colaboradores (contraseña como hash bcrypt — nunca en claro), invitaciones,
solicitudes, actividad y estado de conversaciones.

Escritura best-effort: si MSSQL no responde, el bot sigue funcionando y solo
se registra el fallo en el log. Al arrancar corre un backfill idempotente que
sincroniza cualquier fila que falte (histórico previo al espejo o cambios
hechos mientras MSSQL estaba caído).

Se activa definiendo MSSQL_HOST y MSSQL_SA_PASSWORD (ver .env).
"""

# ponytail: monolito A PROPÓSITO, revisado y rechazado en septiembre de 2026
# (docs/PLAN-CORRECCION-AUDITORIA.md). Partirlo en schema/upsert/listeners/sync
# suena bien y sale caro: la regla de los CINCO lugares —el footgun que AGENTS.md
# nombra así— pasaría a ser cinco lugares en cinco archivos, y hoy los cuatro
# primeros se ven de un `grep <tabla> app/archive.py`. Además
# tests/test_mirror_consistency.py lee el AST de UN archivo: como paquete revienta
# al recolectar, y su detector de llamadas solo entiende `ast.Name`, así que en
# cuanto backfill() llamara a `upsert.archive_x(...)` el candado quedaría VERDE EN
# VACÍO — que es la falla que ese test existe para evitar. Dos tercios de estas
# líneas son _SCHEMA_DDL y mapeo columna-a-columna, no lógica.
# Reabrir SOLO si entran dos autores en paralelo o el DDL deja de caber en una
# lectura; y entonces el candado se reescribe ANTES de mover una sola función.

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine, event, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session as SASession
from sqlalchemy.orm import object_session

from app.config import settings
# Solo la constante: app.models no importa nada de app.*, así que no hay ciclo.
# El resto de los modelos se importa dentro de las funciones (ver backfill).
from app.models import OFICINA_TLAXCALA_ID

logger = logging.getLogger(__name__)

_engine = None
_ready = False

_SCHEMA_DDL = [
    """
    IF OBJECT_ID('wa_clients', 'U') IS NULL
    CREATE TABLE wa_clients (
        phone      NVARCHAR(32)  NOT NULL PRIMARY KEY,
        name       NVARCHAR(200) NULL,
        habilitado BIT           NOT NULL DEFAULT 1,
        created_at DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME()
    )
    """,
    """
    IF OBJECT_ID('wa_messages', 'U') IS NULL
    CREATE TABLE wa_messages (
        id            BIGINT IDENTITY(1,1) PRIMARY KEY,
        source_id     BIGINT        NOT NULL UNIQUE,
        phone         NVARCHAR(32)  NOT NULL,
        direction     NVARCHAR(3)   NOT NULL,
        body          NVARCHAR(MAX) NOT NULL,
        wa_message_id NVARCHAR(128) NULL,
        wa_status     NVARCHAR(32)  NULL,
        media_path    NVARCHAR(300) NULL,
        sent_by       NVARCHAR(64)  NULL,
        habilitado    BIT           NOT NULL DEFAULT 1,
        pricing_category NVARCHAR(40) NULL,
        pricing_type     NVARCHAR(40) NULL,
        pricing_billable BIT          NULL,
        created_at    DATETIME2     NOT NULL,
        CONSTRAINT fk_wa_messages_client
            FOREIGN KEY (phone) REFERENCES wa_clients(phone)
    )
    """,
    """
    IF OBJECT_ID('team_users', 'U') IS NULL
    CREATE TABLE team_users (
        username NVARCHAR(64) NOT NULL PRIMARY KEY
    )
    """,
    """
    IF OBJECT_ID('team_messages', 'U') IS NULL
    CREATE TABLE team_messages (
        id         BIGINT IDENTITY(1,1) PRIMARY KEY,
        source_id  BIGINT        NOT NULL UNIQUE,
        author     NVARCHAR(64)  NOT NULL,
        body       NVARCHAR(MAX) NOT NULL,
        media_path NVARCHAR(300) NULL,
        habilitado BIT           NOT NULL DEFAULT 1,
        created_at DATETIME2     NOT NULL,
        CONSTRAINT fk_team_messages_user
            FOREIGN KEY (author) REFERENCES team_users(username)
    )
    """,
    # Colaboradores. La contraseña se guarda tal cual está en SQLite: un hash
    # bcrypt irreversible, nunca la contraseña en texto plano.
    """
    IF OBJECT_ID('app_users', 'U') IS NULL
    CREATE TABLE app_users (
        source_id     BIGINT        NOT NULL PRIMARY KEY,
        username      NVARCHAR(64)  NOT NULL UNIQUE,
        password_hash NVARCHAR(200) NOT NULL,
        is_admin      BIT           NOT NULL DEFAULT 0,
        habilitado    BIT           NOT NULL DEFAULT 1,
        estado        NVARCHAR(20)  NOT NULL DEFAULT 'disponible',
        phone         NVARCHAR(20)  NULL
    )
    """,
    """
    IF OBJECT_ID('followups', 'U') IS NULL
    CREATE TABLE followups (
        source_id  BIGINT        NOT NULL PRIMARY KEY,
        ticket_id  BIGINT        NOT NULL,
        username   NVARCHAR(64)  NOT NULL,
        note       NVARCHAR(500) NOT NULL,
        due_at     DATETIME2     NOT NULL,
        done_at    DATETIME2     NULL,
        habilitado BIT           NOT NULL DEFAULT 1,
        created_at DATETIME2     NOT NULL
    )
    """,
    """
    IF OBJECT_ID('client_notes', 'U') IS NULL
    CREATE TABLE client_notes (
        source_id  BIGINT        NOT NULL PRIMARY KEY,
        phone      NVARCHAR(32)  NOT NULL,
        author     NVARCHAR(64)  NOT NULL,
        body       NVARCHAR(MAX) NOT NULL,
        habilitado BIT           NOT NULL DEFAULT 1,
        created_at DATETIME2     NOT NULL
    )
    """,
    """
    IF OBJECT_ID('saved_replies', 'U') IS NULL
    CREATE TABLE saved_replies (
        source_id  BIGINT        NOT NULL PRIMARY KEY,
        username   NVARCHAR(64)  NOT NULL,
        title      NVARCHAR(100) NOT NULL,
        body      NVARCHAR(MAX) NOT NULL,
        habilitado BIT           NOT NULL DEFAULT 1,
        created_at DATETIME2     NOT NULL
    )
    """,
    """
    IF OBJECT_ID('invitations', 'U') IS NULL
    CREATE TABLE invitations (
        source_id  BIGINT       NOT NULL PRIMARY KEY,
        token      NVARCHAR(64) NOT NULL,
        phone      NVARCHAR(32) NULL,
        created_by NVARCHAR(64) NOT NULL,
        created_at DATETIME2    NOT NULL,
        used_at    DATETIME2    NULL,
        used_by    NVARCHAR(64) NULL,
        habilitado BIT          NOT NULL DEFAULT 1
    )
    """,
    # Sin FKs en las tablas de solicitud/actividad a propósito: es un archivo
    # de solo lectura y las FKs complicarían el orden del backfill sin aportar
    # integridad que SQLite (la base operativa) no garantice ya.
    """
    IF OBJECT_ID('tickets', 'U') IS NULL
    CREATE TABLE tickets (
        source_id    BIGINT        NOT NULL PRIMARY KEY,
        phone        NVARCHAR(32)  NOT NULL,
        client_name  NVARCHAR(200) NULL,
        category     NVARCHAR(100) NOT NULL,
        service_id   NVARCHAR(64)  NOT NULL,
        service_name NVARCHAR(200) NOT NULL,
        price_info   NVARCHAR(200) NOT NULL,
        status       NVARCHAR(20)  NOT NULL,
        escalado     BIT           NOT NULL DEFAULT 0,
        notes        NVARCHAR(MAX) NULL,
        assigned_to  NVARCHAR(64)  NULL,
        habilitado   BIT           NOT NULL DEFAULT 1,
        created_at   DATETIME2     NOT NULL,
        updated_at   DATETIME2     NOT NULL
    )
    """,
    """
    IF OBJECT_ID('ticket_activity', 'U') IS NULL
    CREATE TABLE ticket_activity (
        source_id  BIGINT        NOT NULL PRIMARY KEY,
        ticket_id  BIGINT        NOT NULL,
        username   NVARCHAR(64)  NOT NULL,
        action     NVARCHAR(100) NOT NULL,
        habilitado BIT           NOT NULL DEFAULT 1,
        created_at DATETIME2     NOT NULL
    )
    """,
    """
    IF OBJECT_ID('admin_audit', 'U') IS NULL
    CREATE TABLE admin_audit (
        source_id  BIGINT        NOT NULL PRIMARY KEY,
        admin      NVARCHAR(64)  NOT NULL,
        action     NVARCHAR(100) NOT NULL,
        target     NVARCHAR(200) NULL,
        habilitado BIT           NOT NULL DEFAULT 1,
        created_at DATETIME2     NOT NULL
    )
    """,
    """
    IF OBJECT_ID('login_events', 'U') IS NULL
    CREATE TABLE login_events (
        source_id  BIGINT        NOT NULL PRIMARY KEY,
        username   NVARCHAR(64)  NOT NULL,
        success    BIT           NOT NULL,
        reason     NVARCHAR(100) NULL,
        ip         NVARCHAR(64)  NULL,
        geo        NVARCHAR(200) NULL,
        device     NVARCHAR(100) NULL,
        user_agent NVARCHAR(300) NULL,
        habilitado BIT           NOT NULL DEFAULT 1,
        created_at DATETIME2     NOT NULL
    )
    """,
    """
    IF OBJECT_ID('web_sessions', 'U') IS NULL
    CREATE TABLE web_sessions (
        source_id    BIGINT        NOT NULL PRIMARY KEY,
        token        NVARCHAR(64)  NOT NULL,
        username     NVARCHAR(64)  NOT NULL,
        ip           NVARCHAR(64)  NULL,
        geo          NVARCHAR(200) NULL,
        device       NVARCHAR(100) NULL,
        user_agent   NVARCHAR(300) NULL,
        created_at   DATETIME2     NOT NULL,
        last_seen_at DATETIME2     NOT NULL,
        ended_at     DATETIME2     NULL,
        habilitado   BIT           NOT NULL DEFAULT 1
    )
    """,
    """
    IF OBJECT_ID('catalog_categories', 'U') IS NULL
    CREATE TABLE catalog_categories (
        id         NVARCHAR(64)  NOT NULL PRIMARY KEY,
        name       NVARCHAR(200) NOT NULL,
        position   INT           NOT NULL DEFAULT 0,
        habilitado BIT           NOT NULL DEFAULT 1
    )
    """,
    """
    IF OBJECT_ID('catalog_services', 'U') IS NULL
    CREATE TABLE catalog_services (
        id            NVARCHAR(64)  NOT NULL PRIMARY KEY,
        category_id   NVARCHAR(64)  NOT NULL,
        name          NVARCHAR(200) NOT NULL,
        price_info    NVARCHAR(200) NOT NULL,
        synonyms_json NVARCHAR(MAX) NOT NULL,
        requirements  NVARCHAR(MAX) NOT NULL,
        position      INT           NOT NULL DEFAULT 0,
        habilitado    BIT           NOT NULL DEFAULT 1
    )
    """,
    """
    IF OBJECT_ID('catalog_audit', 'U') IS NULL
    CREATE TABLE catalog_audit (
        source_id  BIGINT        NOT NULL PRIMARY KEY,
        admin      NVARCHAR(64)  NOT NULL,
        entity     NVARCHAR(20)  NOT NULL,
        entity_id  NVARCHAR(64)  NOT NULL,
        field      NVARCHAR(30)  NOT NULL,
        old_value  NVARCHAR(MAX) NULL,
        new_value  NVARCHAR(MAX) NULL,
        habilitado BIT           NOT NULL DEFAULT 1,
        created_at DATETIME2     NOT NULL
    )
    """,
    """
    IF OBJECT_ID('synonym_proposals', 'U') IS NULL
    CREATE TABLE synonym_proposals (
        source_id  BIGINT        NOT NULL PRIMARY KEY,
        service_id NVARCHAR(64)  NOT NULL,
        synonym    NVARCHAR(200) NOT NULL,
        origin     NVARCHAR(20)  NOT NULL,
        status     NVARCHAR(12)  NOT NULL,
        decided_by NVARCHAR(64)  NULL,
        decided_at DATETIME2     NULL,
        habilitado BIT           NOT NULL DEFAULT 1,
        created_at DATETIME2     NOT NULL
    )
    """,
    """
    IF OBJECT_ID('alerts', 'U') IS NULL
    CREATE TABLE alerts (
        source_id    BIGINT        NOT NULL PRIMARY KEY,
        code         NVARCHAR(64)  NOT NULL,
        level        NVARCHAR(12)  NOT NULL,
        title        NVARCHAR(200) NOT NULL,
        detail       NVARCHAR(MAX) NOT NULL,
        metric_value FLOAT         NULL,
        active       BIT           NOT NULL DEFAULT 1,
        resolved_at  DATETIME2     NULL,
        created_at   DATETIME2     NOT NULL
    )
    """,
    """
    IF OBJECT_ID('feedback', 'U') IS NULL
    CREATE TABLE feedback (
        source_id  BIGINT       NOT NULL PRIMARY KEY,
        ticket_id  BIGINT       NOT NULL,
        phone      NVARCHAR(32) NOT NULL,
        rating     NVARCHAR(8)  NOT NULL,
        habilitado BIT          NOT NULL DEFAULT 1,
        created_at DATETIME2    NOT NULL
    )
    """,
    """
    IF OBJECT_ID('nlu_events', 'U') IS NULL
    CREATE TABLE nlu_events (
        source_id          BIGINT        NOT NULL PRIMARY KEY,
        phone              NVARCHAR(32)  NOT NULL,
        text               NVARCHAR(200) NOT NULL,
        capa               NVARCHAR(20)  NOT NULL,
        service_id         NVARCHAR(300) NULL,
        confianza          FLOAT         NULL,
        termino_en_ticket  BIT           NOT NULL DEFAULT 0,
        habilitado         BIT           NOT NULL DEFAULT 1,
        created_at         DATETIME2     NOT NULL
    )
    """,
    # Estudio del Agente (/agente): configuración runtime y corpus informativo.
    """
    IF OBJECT_ID('agent_config', 'U') IS NULL
    CREATE TABLE agent_config (
        [key]       NVARCHAR(64)   NOT NULL PRIMARY KEY,
        value       NVARCHAR(200)  NOT NULL,
        updated_by  NVARCHAR(150)  NULL,
        updated_at  DATETIME2      NOT NULL,
        habilitado  BIT            NOT NULL DEFAULT 1
    )
    """,
    """
    IF OBJECT_ID('info_docs', 'U') IS NULL
    CREATE TABLE info_docs (
        source_id   BIGINT         NOT NULL PRIMARY KEY,
        texto       NVARCHAR(MAX)  NOT NULL,
        position    INT            NOT NULL DEFAULT 0,
        habilitado  BIT            NOT NULL DEFAULT 1,
        updated_by  NVARCHAR(150)  NULL,
        created_at  DATETIME2      NOT NULL,
        updated_at  DATETIME2      NOT NULL
    )
    """,
    # Catálogo de plantillas aprobadas. Una fila por plantilla, con su
    # identificador en CADA proveedor: es lo que impide que las dos cuentas
    # diverjan cuando alguien actualiza solo una.
    """
    IF OBJECT_ID('message_templates', 'U') IS NULL
    CREATE TABLE message_templates (
        codigo             NVARCHAR(64)  NOT NULL PRIMARY KEY,
        categoria          NVARCHAR(20)  NOT NULL,
        texto_referencia   NVARCHAR(MAX) NOT NULL,
        num_variables      INT           NOT NULL DEFAULT 0,
        meta_template_name NVARCHAR(128) NULL,
        twilio_content_sid NVARCHAR(64)  NULL,
        aprobada_meta      BIT           NOT NULL DEFAULT 0,
        aprobada_twilio    BIT           NOT NULL DEFAULT 0,
        habilitado         BIT           NOT NULL DEFAULT 1,
        created_at         DATETIME2     NOT NULL,
        updated_at         DATETIME2     NOT NULL
    )
    """,
    """
    IF OBJECT_ID('conversations', 'U') IS NULL
    CREATE TABLE conversations (
        phone           NVARCHAR(32)  NOT NULL PRIMARY KEY,
        state           NVARCHAR(50)  NOT NULL,
        context_json    NVARCHAR(MAX) NOT NULL,
        failed_attempts INT           NOT NULL,
        habilitado      BIT           NOT NULL DEFAULT 1,
        updated_at      DATETIME2     NOT NULL
    )
    """,
    # Cobranza: dinero como BIGINT de centavos (nunca FLOAT ni DECIMAL) y sin
    # FKs — mismo criterio del resto del archivo: es un espejo de solo lectura
    # y las FKs complicarían el orden del backfill.
    """
    IF OBJECT_ID('cobros', 'U') IS NULL
    CREATE TABLE cobros (
        source_id                   BIGINT        NOT NULL PRIMARY KEY,
        ticket_id                   BIGINT        NULL,
        phone                       NVARCHAR(32)  NOT NULL,
        client_name                 NVARCHAR(200) NULL,
        service_id                  NVARCHAR(64)  NULL,
        concepto                    NVARCHAR(300) NOT NULL,
        moneda                      NVARCHAR(8)   NOT NULL,
        monto_centavos              BIGINT        NOT NULL,
        monto_cotizado_centavos     BIGINT        NULL,
        monto_catalogo_centavos     BIGINT        NULL,
        monto_origen                NVARCHAR(12)  NOT NULL,
        monto_pagado_centavos       BIGINT        NULL,
        moneda_pagada               NVARCHAR(8)   NULL,
        monto_reembolsado_centavos  BIGINT        NOT NULL DEFAULT 0,
        comision_centavos           BIGINT        NULL,
        neto_centavos               BIGINT        NULL,
        moneda_liquidacion          NVARCHAR(8)   NULL,
        balance_transaction_id      NVARCHAR(128) NULL,
        payout_id                   NVARCHAR(128) NULL,
        metodos_ofrecidos           NVARCHAR(50)  NOT NULL,
        metodo_usado                NVARCHAR(20)  NULL,
        msi_meses_ofrecidos         NVARCHAR(20)  NULL,
        msi_meses_usado             INT           NULL,
        estado                      NVARCHAR(30)  NOT NULL,
        estado_motivo               NVARCHAR(500) NULL,
        llave_cobro_vivo            NVARCHAR(128) NULL,
        llave_generacion            NVARCHAR(64)  NULL,
        token_verificacion          NVARCHAR(64)  NOT NULL,
        stripe_checkout_session_id  NVARCHAR(128) NULL,
        stripe_payment_intent_id    NVARCHAR(128) NULL,
        stripe_charge_id            NVARCHAR(128) NULL,
        stripe_customer_id          NVARCHAR(128) NULL,
        url_pago                    NVARCHAR(MAX) NULL,
        url_expira_at               DATETIME2     NULL,
        voucher_url                 NVARCHAR(MAX) NULL,
        voucher_expira_at           DATETIME2     NULL,
        detalle_pendiente_at        DATETIME2     NULL,
        livemode                    BIT           NULL,
        discrepancia                BIT           NOT NULL DEFAULT 0,
        disputado                   BIT           NOT NULL DEFAULT 0,
        ultimo_evento_id            NVARCHAR(128) NULL,
        ultimo_evento_created       BIGINT        NULL,
        confirmado_por_asesor       BIT           NOT NULL DEFAULT 0,
        confirmado_por              NVARCHAR(64)  NULL,
        confirmado_at               DATETIME2     NULL,
        referencia_recibo           NVARCHAR(100) NULL,
        plan_id                     BIGINT        NULL,
        origen                      NVARCHAR(10)  NOT NULL,
        creado_por                  NVARCHAR(64)  NOT NULL,
        cancelado_por               NVARCHAR(64)  NULL,
        enviado_at                  DATETIME2     NULL,
        pagado_at                   DATETIME2     NULL,
        cancelado_at                DATETIME2     NULL,
        aviso_pago_enviado_at       DATETIME2     NULL,
        reconciliado_at             DATETIME2     NULL,
        created_at                  DATETIME2     NOT NULL,
        updated_at                  DATETIME2     NOT NULL,
        habilitado                  BIT           NOT NULL DEFAULT 1
    )
    """,
    """
    IF OBJECT_ID('intentos_cobro', 'U') IS NULL
    CREATE TABLE intentos_cobro (
        source_id           BIGINT        NOT NULL PRIMARY KEY,
        cobro_id            BIGINT        NOT NULL,
        numero              INT           NOT NULL,
        clave_idempotencia  NVARCHAR(64)  NOT NULL,
        operacion           NVARCHAR(20)  NOT NULL,
        metodos_solicitados NVARCHAR(50)  NOT NULL,
        msi_meses           NVARCHAR(20)  NULL,
        monto_centavos      BIGINT        NOT NULL,
        moneda              NVARCHAR(8)   NOT NULL,
        estado              NVARCHAR(16)  NOT NULL,
        stripe_object       NVARCHAR(32)  NULL,
        stripe_id           NVARCHAR(128) NULL,
        stripe_request_id   NVARCHAR(128) NULL,
        http_status         INT           NULL,
        respuesta_json      NVARCHAR(MAX) NULL,
        error_codigo        NVARCHAR(64)  NULL,
        error_mensaje       NVARCHAR(500) NULL,
        reintentos          INT           NOT NULL DEFAULT 0,
        origen              NVARCHAR(10)  NOT NULL,
        creado_por          NVARCHAR(64)  NOT NULL,
        wa_message_id       BIGINT        NULL,
        livemode            BIT           NULL,
        solicitado_at       DATETIME2     NOT NULL,
        respondido_at       DATETIME2     NULL,
        cerrado_at          DATETIME2     NULL,
        created_at          DATETIME2     NOT NULL,
        habilitado          BIT           NOT NULL DEFAULT 1
    )
    """,
    """
    IF OBJECT_ID('eventos_pasarela', 'U') IS NULL
    CREATE TABLE eventos_pasarela (
        source_id        BIGINT        NOT NULL PRIMARY KEY,
        stripe_event_id  NVARCHAR(128) NOT NULL,
        tipo             NVARCHAR(64)  NOT NULL,
        api_version      NVARCHAR(20)  NULL,
        livemode         BIT           NULL,
        stripe_created   BIGINT        NOT NULL,
        recibido_at      DATETIME2     NOT NULL,
        objeto_id        NVARCHAR(128) NULL,
        monto_centavos   BIGINT        NULL,
        moneda           NVARCHAR(8)   NULL,
        cobro_id         BIGINT        NULL,
        intento_id       BIGINT        NULL,
        payload_json     NVARCHAR(MAX) NOT NULL,
        procesado_at     DATETIME2     NULL,
        intentos_proceso INT           NOT NULL DEFAULT 0,
        resultado        NVARCHAR(12)  NULL,
        detalle          NVARCHAR(500) NULL,
        habilitado       BIT           NOT NULL DEFAULT 1
    )
    """,
    """
    IF OBJECT_ID('planes_cobro', 'U') IS NULL
    CREATE TABLE planes_cobro (
        source_id                  BIGINT        NOT NULL PRIMARY KEY,
        phone                      NVARCHAR(32)  NOT NULL,
        ticket_id                  BIGINT        NULL,
        service_id                 NVARCHAR(64)  NULL,
        concepto                   NVARCHAR(300) NOT NULL,
        moneda                     NVARCHAR(8)   NOT NULL,
        monto_centavos             BIGINT        NOT NULL,
        periodicidad               NVARCHAR(12)  NOT NULL,
        dia_cobro                  INT           NULL,
        paymentmethod_id           BIGINT        NULL,
        proxima_ejecucion_at       DATETIME2     NULL,
        ultima_ejecucion_at        DATETIME2     NULL,
        ultimo_periodo_generado    NVARCHAR(10)  NULL,
        cobros_generados           INT           NOT NULL DEFAULT 0,
        intentos_fallidos_seguidos INT           NOT NULL DEFAULT 0,
        estado                     NVARCHAR(12)  NOT NULL,
        fin_at                     DATETIME2     NULL,
        stripe_customer_id         NVARCHAR(128) NULL,
        stripe_subscription_id     NVARCHAR(128) NULL,
        creado_por                 NVARCHAR(64)  NOT NULL,
        created_at                 DATETIME2     NOT NULL,
        habilitado                 BIT           NOT NULL DEFAULT 1
    )
    """,
    # Nunca PAN, nunca CVC: solo el identificador pm_ de Stripe y los últimos 4.
    """
    IF OBJECT_ID('payment_methods', 'U') IS NULL
    CREATE TABLE payment_methods (
        source_id                BIGINT        NOT NULL PRIMARY KEY,
        phone                    NVARCHAR(32)  NOT NULL,
        stripe_customer_id       NVARCHAR(128) NOT NULL,
        stripe_payment_method_id NVARCHAR(128) NOT NULL,
        tipo                     NVARCHAR(20)  NOT NULL,
        marca                    NVARCHAR(20)  NULL,
        ultimos4                 NVARCHAR(4)   NULL,
        exp_mes                  INT           NULL,
        exp_anio                 INT           NULL,
        funding                  NVARCHAR(10)  NULL,
        predeterminado           BIT           NOT NULL DEFAULT 0,
        autorizado_por           NVARCHAR(64)  NULL,
        autorizado_at            DATETIME2     NULL,
        estado                   NVARCHAR(12)  NOT NULL,
        created_at               DATETIME2     NOT NULL,
        habilitado               BIT           NOT NULL DEFAULT 1
    )
    """,
    # Etiquetas (fase 1): catálogo + puente folio-etiqueta.
    """
    IF OBJECT_ID('labels', 'U') IS NULL
    CREATE TABLE labels (
        source_id   BIGINT        NOT NULL PRIMARY KEY,
        nombre      NVARCHAR(80)  NOT NULL,
        color      NVARCHAR(16)  NOT NULL,
        descripcion NVARCHAR(300) NULL,
        habilitado  BIT           NOT NULL DEFAULT 1,
        created_at  DATETIME2     NOT NULL
    )
    """,
    """
    IF OBJECT_ID('ticket_labels', 'U') IS NULL
    CREATE TABLE ticket_labels (
        source_id  BIGINT       NOT NULL PRIMARY KEY,
        ticket_id  BIGINT       NOT NULL,
        label_id   BIGINT       NOT NULL,
        created_by NVARCHAR(64) NULL,
        habilitado BIT          NOT NULL DEFAULT 1,
        created_at DATETIME2    NOT NULL
    )
    """,
]

# office_id en el espejo (docs/SPEC-MULTI-OFICINA.md §2.3): el MISMO default que
# en SQLite, derivado de la misma constante — no una copia que se pueda desfasar.
# UNA cadena para las 15 tablas; el test de drift compara cada llave contra ella.
# T-SQL rellena las filas existentes en el mismo ALTER (precedente: habilitado).
_OFFICE_ID_MSSQL = f"INT NOT NULL DEFAULT {OFICINA_TLAXCALA_ID}"

# ponytail: igual que db.py, sin alembic — columnas agregadas después del
# lanzamiento se suman a mano vía ALTER TABLE si no existen.
_COLUMNS_ADDED_AFTER_LAUNCH = {
    # DEFAULT 'meta': el histórico completo entró por la única cuenta que hubo
    # hasta ahora. Dejarlo NULL rompería cualquier reporte por cuenta.
    "wa_messages": {
        "habilitado": "BIT NOT NULL DEFAULT 1",
        "provider": "NVARCHAR(20) NOT NULL DEFAULT 'meta'",
        # Medidor (fase 2) — nullable: NULL es «sin veredicto de Meta».
        "pricing_category": "NVARCHAR(40) NULL",
        "pricing_type": "NVARCHAR(40) NULL",
        "pricing_billable": "BIT NULL",
    },
    "team_messages": {"habilitado": "BIT NOT NULL DEFAULT 1", "office_id": _OFFICE_ID_MSSQL},
    "wa_clients": {
        "habilitado": "BIT NOT NULL DEFAULT 1",
        # Ficha CRM (fase 1) — mismos campos que Client en models.py.
        "email": "NVARCHAR(200) NULL",
        "origen": "NVARCHAR(20) NULL",
        "idioma": "NVARCHAR(10) NOT NULL DEFAULT 'es-MX'",
        "opt_in_at": "DATETIME2 NULL",
        "opt_in_source": "NVARCHAR(120) NULL",
        "opt_out_at": "DATETIME2 NULL",
        "atributos_json": "NVARCHAR(MAX) NULL",
        "last_activity_at": "DATETIME2 NULL",
        "notas_internas": "NVARCHAR(MAX) NULL",
        "office_id": _OFFICE_ID_MSSQL,
    },
    "app_users": {
        "habilitado": "BIT NOT NULL DEFAULT 1",
        "estado": "NVARCHAR(20) NOT NULL DEFAULT 'disponible'",
        "phone": "NVARCHAR(20) NULL",
        "office_id": _OFFICE_ID_MSSQL,
    },
    "invitations": {"habilitado": "BIT NOT NULL DEFAULT 1", "office_id": _OFFICE_ID_MSSQL},
    "tickets": {
        "habilitado": "BIT NOT NULL DEFAULT 1",
        "agent_active": "BIT NOT NULL DEFAULT 1",
        "requirements_json": "NVARCHAR(MAX) NULL",
        "provider": "NVARCHAR(20) NOT NULL DEFAULT 'meta'",
        # Métricas de operación (fase 1).
        "first_response_at": "DATETIME2 NULL",
        "first_response_by": "NVARCHAR(64) NULL",
        "resolved_at": "DATETIME2 NULL",
        "status_changed_at": "DATETIME2 NULL",
        "office_id": _OFFICE_ID_MSSQL,
    },
    "ticket_activity": {"habilitado": "BIT NOT NULL DEFAULT 1"},
    "conversations": {
        "habilitado": "BIT NOT NULL DEFAULT 1",
        "provider": "NVARCHAR(20) NOT NULL DEFAULT 'meta'",
        "office_id": _OFFICE_ID_MSSQL,
    },
    # Multi-oficina: las 10 tablas de §2.3 que no habían recibido columnas
    # después del lanzamiento. Solo office_id.
    "cobros": {"office_id": _OFFICE_ID_MSSQL},
    "catalog_categories": {"office_id": _OFFICE_ID_MSSQL},
    "catalog_services": {"office_id": _OFFICE_ID_MSSQL},
    "synonym_proposals": {"office_id": _OFFICE_ID_MSSQL},
    "nlu_events": {"office_id": _OFFICE_ID_MSSQL},
    "info_docs": {"office_id": _OFFICE_ID_MSSQL},
    "labels": {"office_id": _OFFICE_ID_MSSQL},
    "saved_replies": {"office_id": _OFFICE_ID_MSSQL},
    "message_templates": {"office_id": _OFFICE_ID_MSSQL},
    "agent_config": {"office_id": _OFFICE_ID_MSSQL},
}


def enabled() -> bool:
    return bool(settings.mssql_host and settings.mssql_password)


def _get_engine():
    global _engine
    if _engine is None and enabled():
        url = (
            f"mssql+pymssql://{settings.mssql_user}:{settings.mssql_password}"
            f"@{settings.mssql_host}:{settings.mssql_port}/{settings.mssql_db}"
        )
        # Timeouts explícitos: sin ellos, un MSSQL arrancando bloquea el
        # request del webhook de Stripe hasta que Stripe corta a los 30 s.
        #
        # `timeout` es POR STATEMENT y los listeners corren dentro del flush,
        # o sea reteniendo el escritor de SQLite. Tiene que quedar por debajo
        # del busy_timeout=5000 de app/db.py: con 10 s, un MSSQL LENTO (no
        # caído — ahí no hay excepción que degradar) hacía que el INSERT de
        # eventopasarela del webhook muriera con "database is locked" y
        # devolviera 503, quemando los reintentos de Stripe.
        _engine = create_engine(
            url, pool_pre_ping=True, connect_args={"login_timeout": 5, "timeout": 3}
        )
    return _engine


def init() -> None:
    """Crea la base y las tablas si no existen. Llamar al arrancar la app."""
    global _ready
    if not enabled():
        logger.info("Archivo MSSQL deshabilitado (MSSQL_HOST/MSSQL_SA_PASSWORD sin definir)")
        return
    try:
        master_url = (
            f"mssql+pymssql://{settings.mssql_user}:{settings.mssql_password}"
            f"@{settings.mssql_host}:{settings.mssql_port}/master"
        )
        master = create_engine(
            master_url,
            isolation_level="AUTOCOMMIT",
            connect_args={"login_timeout": 5, "timeout": 10},
        )
        with master.connect() as conn:
            conn.execute(
                text(
                    f"IF DB_ID('{settings.mssql_db}') IS NULL "
                    f"CREATE DATABASE [{settings.mssql_db}]"
                )
            )
        master.dispose()

        engine = _get_engine()
        with engine.begin() as conn:
            for ddl in _SCHEMA_DDL:
                conn.execute(text(ddl))
            for table, columns in _COLUMNS_ADDED_AFTER_LAUNCH.items():
                for name, col_type in columns.items():
                    conn.execute(
                        text(
                            f"IF COL_LENGTH('{table}', '{name}') IS NULL "
                            f"ALTER TABLE {table} ADD {name} {col_type}"
                        )
                    )
        _ready = True
        logger.info("Archivo MSSQL listo (base %s en %s)", settings.mssql_db, settings.mssql_host)
    except Exception:
        # ponytail: si MSSQL no está listo al arrancar, se reintenta el
        # esquema en la primera escritura (ver _safe).
        logger.exception("No se pudo inicializar el archivo MSSQL; se reintentará al escribir")


def _naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _upsert_client(
    conn, phone: str, name: str | None = None, habilitado: bool | None = None
) -> None:
    row = conn.execute(
        text("SELECT name, habilitado FROM wa_clients WHERE phone = :p"), {"p": phone}
    ).first()
    if row is None:
        conn.execute(
            text(
                "INSERT INTO wa_clients (phone, name, habilitado, created_at)"
                " VALUES (:p, :n, :hab, :c)"
            ),
            {"p": phone, "n": name, "hab": habilitado if habilitado is not None else True,
             "c": datetime.utcnow()},
        )
        return
    updates = {}
    if name and row[0] != name:
        updates["name"] = name
    if habilitado is not None and bool(row[1]) != habilitado:
        updates["habilitado"] = habilitado
    if updates:
        set_clause = ", ".join(f"{c} = :{c}" for c in updates)
        conn.execute(
            text(f"UPDATE wa_clients SET {set_clause} WHERE phone = :p"), {**updates, "p": phone}
        )


def archive_wa_message(m) -> None:
    with _get_engine().begin() as conn:
        _upsert_client(conn, m.phone)
        conn.execute(
            text(
                "INSERT INTO wa_messages (source_id, phone, direction, body, wa_message_id,"
                " wa_status, media_path, sent_by, habilitado, provider, pricing_category,"
                " pricing_type, pricing_billable, created_at)"
                " VALUES (:sid, :p, :d, :b, :wid, :ws, :mp, :sb, :hab, :prov, :pcat, :ptype,"
                " :pbill, :c)"
            ),
            {
                "sid": m.id,
                "p": m.phone,
                "d": m.direction.value,
                "b": m.body,
                "wid": m.wa_message_id,
                "ws": m.wa_status,
                "mp": m.media_path,
                "sb": m.sent_by,
                "hab": m.habilitado,
                "prov": m.provider,
                "pcat": m.pricing_category,
                "ptype": m.pricing_type,
                "pbill": m.pricing_billable,
                "c": _naive_utc(m.created_at),
            },
        )


def sync_wa_message(m) -> None:
    """Sincroniza wamid/estado de entrega/precio cuando el registro local se actualiza.

    Si el UPDATE no toca ninguna fila, el INSERT original nunca llegó al espejo
    (MSSQL caído justo en ese instante, que `_safe` silencia por diseño) y se
    inserta ahora. Sin este remate el veredicto de cobro se perdía PARA SIEMPRE:
    el precio solo viaja por UPDATE —el status es posterior al alta. Este mismo
    upsert por source_id es el que `backfill()` reutiliza para reparar
    wa_messages tras una caída de MSSQL.
    """
    with _get_engine().begin() as conn:
        resultado = conn.execute(
            text(
                "UPDATE wa_messages SET wa_message_id = :wid, wa_status = :ws, habilitado = :hab,"
                " pricing_category = :pcat, pricing_type = :ptype, pricing_billable = :pbill"
                " WHERE source_id = :sid"
            ),
            {
                "wid": m.wa_message_id,
                "ws": m.wa_status,
                "hab": m.habilitado,
                "pcat": m.pricing_category,
                "ptype": m.pricing_type,
                "pbill": m.pricing_billable,
                "sid": m.id,
            },
        )
        if resultado.rowcount:
            return
    archive_wa_message(m)


def archive_team_message(m) -> None:
    with _get_engine().begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM team_users WHERE username = :u"), {"u": m.author}
        ).first()
        if exists is None:
            conn.execute(text("INSERT INTO team_users (username) VALUES (:u)"), {"u": m.author})
        conn.execute(
            text(
                "INSERT INTO team_messages"
                " (source_id, author, body, media_path, habilitado, created_at, office_id)"
                " VALUES (:sid, :a, :b, :mp, :hab, :c, :off)"
            ),
            {
                "sid": m.id,
                "a": m.author,
                "b": m.body,
                "mp": m.media_path,
                "hab": m.habilitado,
                "c": _naive_utc(m.created_at),
                "off": m.office_id,
            },
        )


def sync_team_message(m) -> None:
    """Sincroniza el campo habilitado (soft-delete) de un mensaje de equipo.

    Mismo remate que sync_wa_message: si el UPDATE no tocó fila, el INSERT
    original nunca llegó al espejo (MSSQL caído en ese instante) y se inserta
    ahora. Upsert por source_id — idempotente, apto para backfill()."""
    with _get_engine().begin() as conn:
        resultado = conn.execute(
            text("UPDATE team_messages SET habilitado = :hab WHERE source_id = :sid"),
            {"hab": m.habilitado, "sid": m.id},
        )
        if resultado.rowcount:
            return
    archive_team_message(m)


def sync_client_name(c) -> None:
    # Nombre histórico; hoy sincroniza la ficha completa del cliente. El
    # _upsert_client incremental sigue existiendo aparte: es el que garantiza
    # la fila para el FK de wa_messages sin pisar campos que no conoce.
    _upsert(
        "wa_clients", "phone", c.phone,
        {
            "name": c.name,
            "habilitado": c.habilitado,
            "created_at": _naive_utc(c.created_at),
            "email": c.email,
            "origen": c.origen,
            "idioma": c.idioma,
            "opt_in_at": _naive_utc(c.opt_in_at) if c.opt_in_at else None,
            "opt_in_source": c.opt_in_source,
            "opt_out_at": _naive_utc(c.opt_out_at) if c.opt_out_at else None,
            "atributos_json": c.atributos_json,
            "last_activity_at": (
                _naive_utc(c.last_activity_at) if c.last_activity_at else None
            ),
            "notas_internas": c.notas_internas,
            "office_id": c.office_id,
        },
    )


def _upsert(table: str, key_col: str, key_val, columns: dict) -> None:
    """UPSERT genérico por clave primaria: intenta UPDATE y, si no afectó
    filas, hace INSERT. Idempotente — sirve igual para insert, update y
    backfill (reprocesar una fila ya espejada no la duplica)."""
    with _get_engine().begin() as conn:
        params = {**columns, "_key": key_val}
        set_clause = ", ".join(f"{c} = :{c}" for c in columns)
        result = conn.execute(
            text(f"UPDATE {table} SET {set_clause} WHERE {key_col} = :_key"), params
        )
        if result.rowcount == 0:
            cols = ", ".join([key_col, *columns])
            vals = ", ".join([":_key", *(f":{c}" for c in columns)])
            conn.execute(text(f"INSERT INTO {table} ({cols}) VALUES ({vals})"), params)


def archive_user(u) -> None:
    _upsert(
        "app_users", "source_id", u.id,
        {
            "username": u.username,
            "password_hash": u.password_hash,
            "is_admin": u.is_admin,
            "habilitado": u.habilitado,
            "estado": u.estado,
            "phone": u.phone,
            "office_id": u.office_id,
        },
    )


def archive_followup(f) -> None:
    _upsert(
        "followups", "source_id", f.id,
        {
            "ticket_id": f.ticket_id,
            "username": f.username,
            "note": f.note,
            "due_at": _naive_utc(f.due_at),
            "done_at": _naive_utc(f.done_at) if f.done_at else None,
            "habilitado": f.habilitado,
            "created_at": _naive_utc(f.created_at),
        },
    )


def archive_client_note(n) -> None:
    _upsert(
        "client_notes", "source_id", n.id,
        {
            "phone": n.phone,
            "author": n.author,
            "body": n.body,
            "habilitado": n.habilitado,
            "created_at": _naive_utc(n.created_at),
        },
    )


def archive_saved_reply(r) -> None:
    _upsert(
        "saved_replies", "source_id", r.id,
        {
            "username": r.username,
            "title": r.title,
            "body": r.body,
            "habilitado": r.habilitado,
            "created_at": _naive_utc(r.created_at),
            "office_id": r.office_id,
        },
    )


def archive_invitation(i) -> None:
    _upsert(
        "invitations", "source_id", i.id,
        {
            "token": i.token,
            "phone": i.phone,
            "office_id": i.office_id,
            "created_by": i.created_by,
            "created_at": _naive_utc(i.created_at),
            "used_at": _naive_utc(i.used_at) if i.used_at else None,
            "used_by": i.used_by,
            "habilitado": i.habilitado,
        },
    )


def archive_ticket(t) -> None:
    _upsert(
        "tickets", "source_id", t.id,
        {
            "phone": t.phone,
            "client_name": t.client_name,
            "category": t.category,
            "service_id": t.service_id,
            "service_name": t.service_name,
            "price_info": t.price_info,
            "status": t.status.value,
            "escalado": t.escalado,
            "notes": t.notes,
            "assigned_to": t.assigned_to,
            "created_at": _naive_utc(t.created_at),
            "updated_at": _naive_utc(t.updated_at),
            "habilitado": t.habilitado,
            "agent_active": t.agent_active,
            "requirements_json": t.requirements_json,
            "provider": t.provider,
            "first_response_at": (
                _naive_utc(t.first_response_at) if t.first_response_at else None
            ),
            "first_response_by": t.first_response_by,
            "resolved_at": _naive_utc(t.resolved_at) if t.resolved_at else None,
            "status_changed_at": (
                _naive_utc(t.status_changed_at) if t.status_changed_at else None
            ),
            "office_id": t.office_id,
        },
    )


def archive_label(l) -> None:
    _upsert(
        "labels", "source_id", l.id,
        {
            "nombre": l.nombre,
            "color": l.color,
            "descripcion": l.descripcion,
            "habilitado": l.habilitado,
            "created_at": _naive_utc(l.created_at),
            "office_id": l.office_id,
        },
    )


def archive_ticket_label(tl) -> None:
    _upsert(
        "ticket_labels", "source_id", tl.id,
        {
            "ticket_id": tl.ticket_id,
            "label_id": tl.label_id,
            "created_by": tl.created_by,
            "habilitado": tl.habilitado,
            "created_at": _naive_utc(tl.created_at),
        },
    )


def archive_activity(a) -> None:
    _upsert(
        "ticket_activity", "source_id", a.id,
        {
            "ticket_id": a.ticket_id,
            "username": a.username,
            "action": a.action,
            "created_at": _naive_utc(a.created_at),
            "habilitado": a.habilitado,
        },
    )


def archive_admin_audit(a) -> None:
    _upsert(
        "admin_audit", "source_id", a.id,
        {
            "admin": a.admin,
            "action": a.action,
            "target": a.target,
            "habilitado": a.habilitado,
            "created_at": _naive_utc(a.created_at),
        },
    )


def archive_login_event(e) -> None:
    _upsert(
        "login_events", "source_id", e.id,
        {
            "username": e.username,
            "success": e.success,
            "reason": e.reason,
            "ip": e.ip,
            "geo": e.geo,
            "device": e.device,
            "user_agent": e.user_agent,
            "habilitado": e.habilitado,
            "created_at": _naive_utc(e.created_at),
        },
    )


def archive_web_session(w) -> None:
    _upsert(
        "web_sessions", "source_id", w.id,
        {
            "token": w.token,
            "username": w.username,
            "ip": w.ip,
            "geo": w.geo,
            "device": w.device,
            "user_agent": w.user_agent,
            "created_at": _naive_utc(w.created_at),
            "last_seen_at": _naive_utc(w.last_seen_at),
            "ended_at": _naive_utc(w.ended_at) if w.ended_at else None,
            "habilitado": w.habilitado,
        },
    )


def archive_catalog_category(c) -> None:
    _upsert(
        "catalog_categories", "id", c.id,
        {
            "name": c.name,
            "position": c.position,
            "habilitado": c.habilitado,
            "office_id": c.office_id,
        },
    )


def archive_catalog_service(s) -> None:
    _upsert(
        "catalog_services", "id", s.id,
        {
            "category_id": s.category_id,
            "name": s.name,
            "price_info": s.price_info,
            "synonyms_json": s.synonyms_json,
            "requirements": s.requirements,
            "position": s.position,
            "habilitado": s.habilitado,
            "office_id": s.office_id,
        },
    )


def archive_catalog_audit(a) -> None:
    _upsert(
        "catalog_audit", "source_id", a.id,
        {
            "admin": a.admin,
            "entity": a.entity,
            "entity_id": a.entity_id,
            "field": a.field,
            "old_value": a.old_value,
            "new_value": a.new_value,
            "habilitado": a.habilitado,
            "created_at": _naive_utc(a.created_at),
        },
    )


def archive_synonym_proposal(p) -> None:
    _upsert(
        "synonym_proposals", "source_id", p.id,
        {
            "service_id": p.service_id,
            "synonym": p.synonym,
            "origin": p.origin,
            "status": p.status,
            "decided_by": p.decided_by,
            "decided_at": _naive_utc(p.decided_at) if p.decided_at else None,
            "habilitado": p.habilitado,
            "created_at": _naive_utc(p.created_at),
            "office_id": p.office_id,
        },
    )


def archive_alert(a) -> None:
    _upsert(
        "alerts", "source_id", a.id,
        {
            "code": a.code,
            "level": a.level,
            "title": a.title,
            "detail": a.detail,
            "metric_value": a.metric_value,
            "active": a.active,
            "resolved_at": _naive_utc(a.resolved_at) if a.resolved_at else None,
            "created_at": _naive_utc(a.created_at),
        },
    )


def archive_feedback(f) -> None:
    _upsert(
        "feedback", "source_id", f.id,
        {
            "ticket_id": f.ticket_id,
            "phone": f.phone,
            "rating": f.rating,
            "habilitado": f.habilitado,
            "created_at": _naive_utc(f.created_at),
        },
    )


def archive_nlu_event(e) -> None:
    _upsert(
        "nlu_events", "source_id", e.id,
        {
            "phone": e.phone,
            "text": e.text,
            "capa": e.capa,
            "service_id": e.service_id,
            "confianza": e.confianza,
            "termino_en_ticket": e.termino_en_ticket,
            "habilitado": e.habilitado,
            "created_at": _naive_utc(e.created_at),
            "office_id": e.office_id,
        },
    )


def archive_agent_config(a) -> None:
    _upsert(
        "agent_config", "[key]", a.key,
        {
            "value": a.value,
            "updated_by": a.updated_by,
            "updated_at": _naive_utc(a.updated_at),
            "habilitado": a.habilitado,
            "office_id": a.office_id,
        },
    )


def archive_info_doc(d) -> None:
    _upsert(
        "info_docs", "source_id", d.id,
        {
            "texto": d.texto,
            "position": d.position,
            "habilitado": d.habilitado,
            "updated_by": d.updated_by,
            "created_at": _naive_utc(d.created_at),
            "updated_at": _naive_utc(d.updated_at),
            "office_id": d.office_id,
        },
    )


def archive_conversation(c) -> None:
    _upsert(
        "conversations", "phone", c.phone,
        {
            "state": c.state,
            "context_json": c.context_json,
            "failed_attempts": c.failed_attempts,
            "updated_at": _naive_utc(c.updated_at),
            "habilitado": c.habilitado,
            "provider": c.provider,
            "office_id": c.office_id,
        },
    )


def archive_plantilla(p) -> None:
    _upsert(
        "message_templates", "codigo", p.codigo,
        {
            "categoria": p.categoria,
            "texto_referencia": p.texto_referencia,
            "num_variables": p.num_variables,
            "meta_template_name": p.meta_template_name,
            "twilio_content_sid": p.twilio_content_sid,
            "aprobada_meta": p.aprobada_meta,
            "aprobada_twilio": p.aprobada_twilio,
            "habilitado": p.habilitado,
            "created_at": _naive_utc(p.created_at),
            "updated_at": _naive_utc(p.updated_at),
            "office_id": p.office_id,
        },
    )


def archive_cobro(c) -> None:
    _upsert(
        "cobros", "source_id", c.id,
        {
            "ticket_id": c.ticket_id,
            "phone": c.phone,
            "client_name": c.client_name,
            "service_id": c.service_id,
            "concepto": c.concepto,
            "moneda": c.moneda,
            "monto_centavos": c.monto_centavos,
            "monto_cotizado_centavos": c.monto_cotizado_centavos,
            "monto_catalogo_centavos": c.monto_catalogo_centavos,
            "monto_origen": c.monto_origen,
            "monto_pagado_centavos": c.monto_pagado_centavos,
            "moneda_pagada": c.moneda_pagada,
            "monto_reembolsado_centavos": c.monto_reembolsado_centavos,
            "comision_centavos": c.comision_centavos,
            "neto_centavos": c.neto_centavos,
            "moneda_liquidacion": c.moneda_liquidacion,
            "balance_transaction_id": c.balance_transaction_id,
            "payout_id": c.payout_id,
            "metodos_ofrecidos": c.metodos_ofrecidos,
            "metodo_usado": c.metodo_usado,
            "msi_meses_ofrecidos": c.msi_meses_ofrecidos,
            "msi_meses_usado": c.msi_meses_usado,
            "estado": c.estado,
            "estado_motivo": c.estado_motivo,
            "llave_cobro_vivo": c.llave_cobro_vivo,
            "llave_generacion": c.llave_generacion,
            "token_verificacion": c.token_verificacion,
            "stripe_checkout_session_id": c.stripe_checkout_session_id,
            "stripe_payment_intent_id": c.stripe_payment_intent_id,
            "stripe_charge_id": c.stripe_charge_id,
            "stripe_customer_id": c.stripe_customer_id,
            "url_pago": c.url_pago,
            "url_expira_at": _naive_utc(c.url_expira_at) if c.url_expira_at else None,
            "voucher_url": c.voucher_url,
            "voucher_expira_at": _naive_utc(c.voucher_expira_at) if c.voucher_expira_at else None,
            "detalle_pendiente_at": _naive_utc(c.detalle_pendiente_at) if c.detalle_pendiente_at else None,
            "livemode": c.livemode,
            "discrepancia": c.discrepancia,
            "disputado": c.disputado,
            "ultimo_evento_id": c.ultimo_evento_id,
            "ultimo_evento_created": c.ultimo_evento_created,
            "confirmado_por_asesor": c.confirmado_por_asesor,
            "confirmado_por": c.confirmado_por,
            "confirmado_at": _naive_utc(c.confirmado_at) if c.confirmado_at else None,
            "referencia_recibo": c.referencia_recibo,
            "plan_id": c.plan_id,
            "origen": c.origen,
            "creado_por": c.creado_por,
            "cancelado_por": c.cancelado_por,
            "enviado_at": _naive_utc(c.enviado_at) if c.enviado_at else None,
            "pagado_at": _naive_utc(c.pagado_at) if c.pagado_at else None,
            "cancelado_at": _naive_utc(c.cancelado_at) if c.cancelado_at else None,
            "aviso_pago_enviado_at": _naive_utc(c.aviso_pago_enviado_at) if c.aviso_pago_enviado_at else None,
            "reconciliado_at": _naive_utc(c.reconciliado_at) if c.reconciliado_at else None,
            "created_at": _naive_utc(c.created_at),
            "updated_at": _naive_utc(c.updated_at),
            "habilitado": c.habilitado,
            "office_id": c.office_id,
        },
    )


def archive_intento_cobro(i) -> None:
    _upsert(
        "intentos_cobro", "source_id", i.id,
        {
            "cobro_id": i.cobro_id,
            "numero": i.numero,
            "clave_idempotencia": i.clave_idempotencia,
            "operacion": i.operacion,
            "metodos_solicitados": i.metodos_solicitados,
            "msi_meses": i.msi_meses,
            "monto_centavos": i.monto_centavos,
            "moneda": i.moneda,
            "estado": i.estado,
            "stripe_object": i.stripe_object,
            "stripe_id": i.stripe_id,
            "stripe_request_id": i.stripe_request_id,
            "http_status": i.http_status,
            # Recorte defensivo: el dominio ya guarda recortado, pero el espejo
            # no debe confiar en eso para no reventar el NVARCHAR.
            "respuesta_json": i.respuesta_json[:8192] if i.respuesta_json else None,
            "error_codigo": i.error_codigo,
            "error_mensaje": i.error_mensaje,
            "reintentos": i.reintentos,
            "origen": i.origen,
            "creado_por": i.creado_por,
            "wa_message_id": i.wa_message_id,
            "livemode": i.livemode,
            "solicitado_at": _naive_utc(i.solicitado_at),
            "respondido_at": _naive_utc(i.respondido_at) if i.respondido_at else None,
            "cerrado_at": _naive_utc(i.cerrado_at) if i.cerrado_at else None,
            "created_at": _naive_utc(i.created_at),
            "habilitado": i.habilitado,
        },
    )


def archive_evento_pasarela(e) -> None:
    _upsert(
        "eventos_pasarela", "source_id", e.id,
        {
            "stripe_event_id": e.stripe_event_id,
            "tipo": e.tipo,
            "api_version": e.api_version,
            "livemode": e.livemode,
            "stripe_created": e.stripe_created,
            "recibido_at": _naive_utc(e.recibido_at),
            "objeto_id": e.objeto_id,
            "monto_centavos": e.monto_centavos,
            "moneda": e.moneda,
            "cobro_id": e.cobro_id,
            "intento_id": e.intento_id,
            "payload_json": e.payload_json[:32768] if e.payload_json else "",
            "procesado_at": _naive_utc(e.procesado_at) if e.procesado_at else None,
            "intentos_proceso": e.intentos_proceso,
            "resultado": e.resultado,
            "detalle": e.detalle,
            "habilitado": e.habilitado,
        },
    )


def archive_plan_cobro(p) -> None:
    _upsert(
        "planes_cobro", "source_id", p.id,
        {
            "phone": p.phone,
            "ticket_id": p.ticket_id,
            "service_id": p.service_id,
            "concepto": p.concepto,
            "moneda": p.moneda,
            "monto_centavos": p.monto_centavos,
            "periodicidad": p.periodicidad,
            "dia_cobro": p.dia_cobro,
            "paymentmethod_id": p.paymentmethod_id,
            "proxima_ejecucion_at": _naive_utc(p.proxima_ejecucion_at) if p.proxima_ejecucion_at else None,
            "ultima_ejecucion_at": _naive_utc(p.ultima_ejecucion_at) if p.ultima_ejecucion_at else None,
            "ultimo_periodo_generado": p.ultimo_periodo_generado,
            "cobros_generados": p.cobros_generados,
            "intentos_fallidos_seguidos": p.intentos_fallidos_seguidos,
            "estado": p.estado,
            "fin_at": _naive_utc(p.fin_at) if p.fin_at else None,
            "stripe_customer_id": p.stripe_customer_id,
            "stripe_subscription_id": p.stripe_subscription_id,
            "creado_por": p.creado_por,
            "created_at": _naive_utc(p.created_at),
            "habilitado": p.habilitado,
        },
    )


def archive_payment_method(p) -> None:
    _upsert(
        "payment_methods", "source_id", p.id,
        {
            "phone": p.phone,
            "stripe_customer_id": p.stripe_customer_id,
            "stripe_payment_method_id": p.stripe_payment_method_id,
            "tipo": p.tipo,
            "marca": p.marca,
            "ultimos4": p.ultimos4,
            "exp_mes": p.exp_mes,
            "exp_anio": p.exp_anio,
            "funding": p.funding,
            "predeterminado": p.predeterminado,
            "autorizado_por": p.autorizado_por,
            "autorizado_at": _naive_utc(p.autorizado_at) if p.autorizado_at else None,
            "estado": p.estado,
            "created_at": _naive_utc(p.created_at),
            "habilitado": p.habilitado,
        },
    )


# Mínimo 60 s entre intentos de init() desde _safe: SQL Server Express tarda
# decenas de segundos en aceptar logins tras un reinicio, y sin este freno un
# webhook de Stripe que llega en esa ventana dispararía el DDL completo (sin
# timeout) en CADA escritura, hasta que Stripe corta a los 30 s.
_PROXIMO_INIT: datetime | None = None


def _safe(fn, target) -> None:
    if not enabled():
        return
    global _ready, _PROXIMO_INIT
    try:
        if not _ready:
            ahora = datetime.utcnow()
            if _PROXIMO_INIT and ahora < _PROXIMO_INIT:
                return  # MSSQL caído hace <60 s: se salta el espejo, backfill repara
            init()
            if not _ready:
                _PROXIMO_INIT = ahora + timedelta(seconds=60)
                return
            _PROXIMO_INIT = None
        fn(target)
    except Exception as exc:
        logger.warning("No se pudo archivar en MSSQL (%s): %s", fn.__name__, exc)


# Cola por sesión: los listeners de cobranza encolan aquí durante el flush y
# el after_commit la drena, ya fuera de la transacción de SQLite.
_COLA_ESPEJO = "_espejo_cobranza_pendiente"


def _instantanea(target):
    """Copia desprendida de las columnas, tomada DENTRO del flush.

    Después del commit los atributos quedan expirados: leerlos dispararía un
    SELECT por objeto, o reventaría si la sesión ya cerró. Las funciones
    archive_* solo leen atributos, así que un namespace con los valores
    alcanza y además congela exactamente lo que se va a espejar.
    """
    estado = sa_inspect(target)
    return SimpleNamespace(**{
        attr.key: getattr(target, attr.key) for attr in estado.mapper.column_attrs
    })


def _diferir(fn, target) -> None:
    """Encola el espejo para DESPUÉS del commit, en vez de correrlo dentro del
    flush.

    Dentro del flush, el round-trip a MSSQL retiene el escritor de SQLite. Con
    MSSQL lento —no caído; ahí `_safe` sí degrada la excepción a warning— el
    INSERT de `eventopasarela` del webhook moría con "database is locked" y
    devolvía 503, quemando los reintentos de Stripe. Lo que se pierda si el
    proceso muere entre el commit y el espejo lo repara `backfill()` en el
    siguiente arranque, igual que cuando MSSQL está caído.
    """
    if not enabled():
        return
    sesion = object_session(target)
    if sesion is None:  # sin sesión no hay after_commit que drene: al vuelo
        _safe(fn, target)
        return
    sesion.info.setdefault(_COLA_ESPEJO, []).append((fn, _instantanea(target)))


def _drenar_cola_espejo(sesion) -> None:
    for fn, copia in sesion.info.pop(_COLA_ESPEJO, ()):
        _safe(fn, copia)


# Tablas del espejo con teléfono de CLIENTE. app_users queda fuera a propósito:
# su phone es el celular del colaborador (ver el comentario en app/db.py).
_TABLAS_ESPEJO_TELEFONO_PK = ("wa_clients", "conversations")
_TABLAS_ESPEJO_TELEFONO = (
    "wa_messages",
    "client_notes",
    "invitations",
    "tickets",
    "feedback",
    "nlu_events",
    "cobros",
    "planes_cobro",
    "payment_methods",
)


def _normalize_phones_espejo() -> None:
    """Réplica en MSSQL de db._normalize_phones().

    Aquella corre con SQL directo sobre SQLite y brinca los listeners ORM, así
    que el espejo nunca se entera de los renombres/fusiones y acumula filas con
    el teléfono crudo de Meta ('5215…') junto a las canónicas ('525…') — el
    "7 vs 14" que /system mostraba como 'al día' antes de este fix.

    Orden por el FK de wa_messages → wa_clients: primero se garantiza el padre
    canónico, luego se repuntan los hijos, y al final cae la fila cruda.
    Idempotente: sobre datos canónicos no escribe nada.
    """
    from app.utils import normalize_phone

    with _get_engine().begin() as conn:
        for (crudo,) in conn.execute(text("SELECT phone FROM wa_clients")).fetchall():
            canonico = normalize_phone(crudo)
            if not canonico or canonico == crudo:
                continue
            ya_existe = conn.execute(
                text("SELECT 1 FROM wa_clients WHERE phone = :c"), {"c": canonico}
            ).first()
            if not ya_existe:
                conn.execute(
                    text(
                        "INSERT INTO wa_clients"
                        " (phone, name, habilitado, created_at, office_id)"
                        " SELECT :c, name, habilitado, created_at, office_id"
                        " FROM wa_clients WHERE phone = :p"
                    ),
                    {"c": canonico, "p": crudo},
                )
            for hija in _TABLAS_ESPEJO_TELEFONO:
                conn.execute(
                    text(f"UPDATE {hija} SET phone = :c WHERE phone = :p"),
                    {"c": canonico, "p": crudo},
                )
            conn.execute(text("DELETE FROM wa_clients WHERE phone = :p"), {"p": crudo})
            logger.warning(
                "Espejo: teléfono crudo %s fusionado con %s", crudo, canonico
            )

        # conversations: phone es PK pero sin hijos por FK — fusión directa.
        for (crudo,) in conn.execute(text("SELECT phone FROM conversations")).fetchall():
            canonico = normalize_phone(crudo)
            if not canonico or canonico == crudo:
                continue
            ya_existe = conn.execute(
                text("SELECT 1 FROM conversations WHERE phone = :c"), {"c": canonico}
            ).first()
            if ya_existe:
                conn.execute(
                    text("DELETE FROM conversations WHERE phone = :p"), {"p": crudo}
                )
            else:
                conn.execute(
                    text("UPDATE conversations SET phone = :c WHERE phone = :p"),
                    {"c": canonico, "p": crudo},
                )

        # Barrido final: filas hijas cuyo crudo no tenía padre en wa_clients
        # (p.ej. nlu_events de números que nunca llegaron a cliente).
        for tabla in _TABLAS_ESPEJO_TELEFONO:
            if tabla == "wa_messages":
                continue  # FK: solo se repunta junto con su padre, arriba
            for (crudo,) in conn.execute(
                text(f"SELECT DISTINCT phone FROM {tabla}")
            ).fetchall():
                canonico = normalize_phone(crudo)
                if canonico and canonico != crudo:
                    conn.execute(
                        text(f"UPDATE {tabla} SET phone = :c WHERE phone = :p"),
                        {"c": canonico, "p": crudo},
                    )


def backfill() -> None:
    """Sincroniza a MSSQL toda la data que ya existe en SQLite. Idempotente
    (gracias al upsert), así que corre seguro en cada arranque: cubre el
    histórico previo al espejo y cualquier cambio hecho mientras MSSQL
    estuvo caído. Best-effort — no tumba el arranque si MSSQL no responde."""
    if not enabled():
        return
    # Antes de upsertear: limpia los teléfonos crudos que el espejo acumuló
    # (db._normalize_phones corre sin listeners y MSSQL nunca se enteró).
    # _safe(fn, target) es la firma de los listeners; aquí no hay target.
    _safe(lambda _: _normalize_phones_espejo(), None)
    from app.db import get_session
    from app.models import (
        AdminAudit,
        Alert,
        CatalogAudit,
        CatalogCategory,
        CatalogService,
        Client,
        ClientNote,
        Cobro,
        Conversation,
        EventoPasarela,
        Feedback,
        FollowUp,
        IntentoCobro,
        Invitation,
        LoginEvent,
        Message,
        TeamMessage,
        AgentConfig,
        InfoDoc,
        NluEvent,
        PaymentMethod,
        PlanCobro,
        Plantilla,
        SavedReply,
        SynonymProposal,
        Ticket,
        TicketActivity,
        User,
        Label,
        TicketLabel,
        WebSession,
    )
    from sqlmodel import select

    # Los mensajes (WhatsApp y chat de equipo) se reparan con sus sync_*, que
    # son upserts por source_id (UPDATE y, si no hay fila, INSERT): reprocesar
    # lo ya espejado no duplica ni rompe el UNIQUE(source_id). Antes se
    # excluían y una caída de MSSQL dejaba huecos permanentes (bug B3).
    try:
        if not _ready:
            init()
        with get_session() as session:
            for c in session.exec(select(Client)).all():
                sync_client_name(c)
            for u in session.exec(select(User)).all():
                archive_user(u)
            for t in session.exec(select(Ticket)).all():
                archive_ticket(t)
            for a in session.exec(select(TicketActivity)).all():
                archive_activity(a)
            for i in session.exec(select(Invitation)).all():
                archive_invitation(i)
            for conv in session.exec(select(Conversation)).all():
                archive_conversation(conv)
            for aa in session.exec(select(AdminAudit)).all():
                archive_admin_audit(aa)
            for f in session.exec(select(FollowUp)).all():
                archive_followup(f)
            for n in session.exec(select(ClientNote)).all():
                archive_client_note(n)
            for r in session.exec(select(SavedReply)).all():
                archive_saved_reply(r)
            for e in session.exec(select(LoginEvent)).all():
                archive_login_event(e)
            for w in session.exec(select(WebSession)).all():
                archive_web_session(w)
            for cc in session.exec(select(CatalogCategory)).all():
                archive_catalog_category(cc)
            for cs in session.exec(select(CatalogService)).all():
                archive_catalog_service(cs)
            for ca in session.exec(select(CatalogAudit)).all():
                archive_catalog_audit(ca)
            for ne in session.exec(select(NluEvent)).all():
                archive_nlu_event(ne)
            for ac in session.exec(select(AgentConfig)).all():
                archive_agent_config(ac)
            for doc in session.exec(select(InfoDoc)).all():
                archive_info_doc(doc)
            for f in session.exec(select(Feedback)).all():
                archive_feedback(f)
            for sp in session.exec(select(SynonymProposal)).all():
                archive_synonym_proposal(sp)
            for al in session.exec(select(Alert)).all():
                archive_alert(al)
            for co in session.exec(select(Cobro)).all():
                archive_cobro(co)
            for ic in session.exec(select(IntentoCobro)).all():
                archive_intento_cobro(ic)
            for ep in session.exec(select(EventoPasarela)).all():
                archive_evento_pasarela(ep)
            for pc in session.exec(select(PlanCobro)).all():
                archive_plan_cobro(pc)
            for pm in session.exec(select(PaymentMethod)).all():
                archive_payment_method(pm)
            for pl in session.exec(select(Plantilla)).all():
                archive_plantilla(pl)
            for lb in session.exec(select(Label)).all():
                archive_label(lb)
            for tl in session.exec(select(TicketLabel)).all():
                archive_ticket_label(tl)
            for m in session.exec(select(Message)).all():
                sync_wa_message(m)
            for tm in session.exec(select(TeamMessage)).all():
                sync_team_message(tm)
        logger.info("Backfill de MSSQL completado")
    except Exception as exc:
        logger.warning("Backfill de MSSQL incompleto: %s", exc)


def _register_listeners() -> None:
    # ponytail: la réplica corre dentro del flush de SQLModel — con este
    # volumen es inofensivo; mover a una cola si algún día hay miles de
    # mensajes por minuto.
    from app.models import (
        AdminAudit,
        Alert,
        CatalogAudit,
        CatalogCategory,
        CatalogService,
        Client,
        ClientNote,
        Cobro,
        Conversation,
        EventoPasarela,
        FollowUp,
        Feedback,
        IntentoCobro,
        Invitation,
        LoginEvent,
        Message,
        AgentConfig,
        InfoDoc,
        NluEvent,
        PaymentMethod,
        PlanCobro,
        Plantilla,
        SavedReply,
        SynonymProposal,
        TeamMessage,
        Ticket,
        TicketActivity,
        Label,
        TicketLabel,
        User,
        WebSession,
    )

    @event.listens_for(Message, "after_insert")
    def _wa_insert(mapper, connection, target):
        _safe(archive_wa_message, target)

    @event.listens_for(Message, "after_update")
    def _wa_update(mapper, connection, target):
        _safe(sync_wa_message, target)

    @event.listens_for(TeamMessage, "after_insert")
    def _team_insert(mapper, connection, target):
        _safe(archive_team_message, target)

    @event.listens_for(TeamMessage, "after_update")
    def _team_update(mapper, connection, target):
        _safe(sync_team_message, target)

    @event.listens_for(Client, "after_insert")
    @event.listens_for(Client, "after_update")
    def _client_upsert(mapper, connection, target):
        _safe(sync_client_name, target)

    @event.listens_for(Label, "after_insert")
    @event.listens_for(Label, "after_update")
    def _label_upsert(mapper, connection, target):
        _safe(archive_label, target)

    @event.listens_for(TicketLabel, "after_insert")
    @event.listens_for(TicketLabel, "after_update")
    def _ticket_label_upsert(mapper, connection, target):
        _safe(archive_ticket_label, target)

    @event.listens_for(User, "after_insert")
    @event.listens_for(User, "after_update")
    def _user_upsert(mapper, connection, target):
        _safe(archive_user, target)

    @event.listens_for(Invitation, "after_insert")
    @event.listens_for(Invitation, "after_update")
    def _invitation_upsert(mapper, connection, target):
        _safe(archive_invitation, target)

    @event.listens_for(Ticket, "after_insert")
    @event.listens_for(Ticket, "after_update")
    def _ticket_upsert(mapper, connection, target):
        _safe(archive_ticket, target)

    @event.listens_for(TicketActivity, "after_insert")
    @event.listens_for(TicketActivity, "after_update")
    def _activity_upsert(mapper, connection, target):
        _safe(archive_activity, target)

    @event.listens_for(Conversation, "after_insert")
    @event.listens_for(Conversation, "after_update")
    def _conversation_upsert(mapper, connection, target):
        _safe(archive_conversation, target)

    @event.listens_for(AdminAudit, "after_insert")
    @event.listens_for(AdminAudit, "after_update")
    def _admin_audit_upsert(mapper, connection, target):
        _safe(archive_admin_audit, target)

    @event.listens_for(FollowUp, "after_insert")
    @event.listens_for(FollowUp, "after_update")
    def _followup_upsert(mapper, connection, target):
        _safe(archive_followup, target)

    @event.listens_for(ClientNote, "after_insert")
    @event.listens_for(ClientNote, "after_update")
    def _client_note_upsert(mapper, connection, target):
        _safe(archive_client_note, target)

    @event.listens_for(SavedReply, "after_insert")
    @event.listens_for(SavedReply, "after_update")
    def _saved_reply_upsert(mapper, connection, target):
        _safe(archive_saved_reply, target)

    @event.listens_for(LoginEvent, "after_insert")
    @event.listens_for(LoginEvent, "after_update")
    def _login_event_upsert(mapper, connection, target):
        _safe(archive_login_event, target)

    @event.listens_for(WebSession, "after_insert")
    @event.listens_for(WebSession, "after_update")
    def _web_session_upsert(mapper, connection, target):
        _safe(archive_web_session, target)

    @event.listens_for(CatalogCategory, "after_insert")
    @event.listens_for(CatalogCategory, "after_update")
    def _catalog_category_upsert(mapper, connection, target):
        _safe(archive_catalog_category, target)

    @event.listens_for(CatalogService, "after_insert")
    @event.listens_for(CatalogService, "after_update")
    def _catalog_service_upsert(mapper, connection, target):
        _safe(archive_catalog_service, target)

    @event.listens_for(CatalogAudit, "after_insert")
    @event.listens_for(CatalogAudit, "after_update")
    def _catalog_audit_upsert(mapper, connection, target):
        _safe(archive_catalog_audit, target)

    @event.listens_for(NluEvent, "after_insert")
    @event.listens_for(NluEvent, "after_update")
    def _nlu_event_upsert(mapper, connection, target):
        _safe(archive_nlu_event, target)

    @event.listens_for(AgentConfig, "after_insert")
    @event.listens_for(AgentConfig, "after_update")
    def _agent_config_upsert(mapper, connection, target):
        _safe(archive_agent_config, target)

    @event.listens_for(InfoDoc, "after_insert")
    @event.listens_for(InfoDoc, "after_update")
    def _info_doc_upsert(mapper, connection, target):
        _safe(archive_info_doc, target)

    @event.listens_for(Feedback, "after_insert")
    @event.listens_for(Feedback, "after_update")
    def _feedback_upsert(mapper, connection, target):
        _safe(archive_feedback, target)

    @event.listens_for(SynonymProposal, "after_insert")
    @event.listens_for(SynonymProposal, "after_update")
    def _synonym_proposal_upsert(mapper, connection, target):
        _safe(archive_synonym_proposal, target)

    @event.listens_for(Alert, "after_insert")
    @event.listens_for(Alert, "after_update")
    def _alert_upsert(mapper, connection, target):
        _safe(archive_alert, target)

    # Las tres tablas de DINERO se espejan DESPUÉS del commit: son las que
    # escribe el webhook de Stripe, y un MSSQL lento dentro del flush retiene
    # el escritor de SQLite hasta tumbar el request con 503. Las demás tablas
    # siguen síncronas: su volumen es bajo y no están en ese camino.
    @event.listens_for(SASession, "after_commit")
    def _espejo_tras_commit(session):
        _drenar_cola_espejo(session)

    @event.listens_for(Cobro, "after_insert")
    @event.listens_for(Cobro, "after_update")
    def _cobro_upsert(mapper, connection, target):
        _diferir(archive_cobro, target)

    @event.listens_for(IntentoCobro, "after_insert")
    @event.listens_for(IntentoCobro, "after_update")
    def _intento_cobro_upsert(mapper, connection, target):
        _diferir(archive_intento_cobro, target)

    @event.listens_for(EventoPasarela, "after_insert")
    @event.listens_for(EventoPasarela, "after_update")
    def _evento_pasarela_upsert(mapper, connection, target):
        _diferir(archive_evento_pasarela, target)

    @event.listens_for(PlanCobro, "after_insert")
    @event.listens_for(PlanCobro, "after_update")
    def _plan_cobro_upsert(mapper, connection, target):
        _safe(archive_plan_cobro, target)

    @event.listens_for(PaymentMethod, "after_insert")
    @event.listens_for(PaymentMethod, "after_update")
    def _payment_method_upsert(mapper, connection, target):
        _safe(archive_payment_method, target)

    @event.listens_for(Plantilla, "after_insert")
    @event.listens_for(Plantilla, "after_update")
    def _plantilla_upsert(mapper, connection, target):
        _safe(archive_plantilla, target)


_register_listeners()
