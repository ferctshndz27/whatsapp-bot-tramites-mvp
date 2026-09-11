from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlmodel import Session, SQLModel

import app.archive as archive
import app.db as db
from app.config import settings
from app.models import (
    Client,
    Cobro,
    Direction,
    Invitation,
    Message,
    TeamMessage,
    Ticket,
    TicketActivity,
    TicketStatus,
    User,
)

# El espejo MSSQL de estas pruebas es SQLite: archive.py le pasa datetimes
# crudos por text(), que en producción recibe pymssql sin aviso, y aquí caen en
# el adaptador por defecto de sqlite3 (obsoleto desde Python 3.12). Es del
# doble, no del código: se calla solo en los archivos que usan ese doble.
pytestmark = pytest.mark.filterwarnings(
    "ignore:The default datetime adapter is deprecated:DeprecationWarning"
)

ARCHIVE_DDL = [
    """CREATE TABLE wa_clients (
        phone TEXT PRIMARY KEY, name TEXT,
        habilitado INTEGER DEFAULT 1, created_at TIMESTAMP,
        email TEXT, origen TEXT, idioma TEXT DEFAULT 'es-MX',
        opt_in_at TIMESTAMP, opt_in_source TEXT, opt_out_at TIMESTAMP,
        atributos_json TEXT, last_activity_at TIMESTAMP,
        notas_internas TEXT, office_id INTEGER DEFAULT 1)""",
    """CREATE TABLE wa_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER UNIQUE,
        phone TEXT REFERENCES wa_clients(phone), direction TEXT, body TEXT,
        wa_message_id TEXT, wa_status TEXT, media_path TEXT, sent_by TEXT,
        habilitado INTEGER DEFAULT 1, provider TEXT DEFAULT 'meta',
        pricing_category TEXT, pricing_type TEXT, pricing_billable INTEGER,
        created_at TIMESTAMP)""",
    "CREATE TABLE team_users (username TEXT PRIMARY KEY)",
    """CREATE TABLE team_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER UNIQUE, room_id INTEGER,
        author TEXT REFERENCES team_users(username), body TEXT,
        media_path TEXT, habilitado INTEGER DEFAULT 1, created_at TIMESTAMP,
        office_id INTEGER DEFAULT 1)""",
    """CREATE TABLE chat_rooms (
        source_id INTEGER PRIMARY KEY, name TEXT, room_type TEXT,
        habilitado INTEGER DEFAULT 1, created_at TIMESTAMP)""",
    """CREATE TABLE chat_members (
        source_id INTEGER PRIMARY KEY, room_id INTEGER, username TEXT,
        is_muted INTEGER DEFAULT 0, habilitado INTEGER DEFAULT 1, joined_at TIMESTAMP)""",
    """CREATE TABLE message_reactions (
        source_id INTEGER PRIMARY KEY, message_id INTEGER, username TEXT,
        reaction TEXT, created_at TIMESTAMP)""",
    """CREATE TABLE message_reminders (
        source_id INTEGER PRIMARY KEY, message_id INTEGER, username TEXT,
        remind_at TIMESTAMP, note TEXT, is_dismissed INTEGER DEFAULT 0, created_at TIMESTAMP)""",
    """CREATE TABLE app_users (
        source_id INTEGER PRIMARY KEY, username TEXT UNIQUE,
        password_hash TEXT, is_admin INTEGER DEFAULT 0,
        habilitado INTEGER DEFAULT 1, estado TEXT DEFAULT 'disponible',
        phone TEXT, office_id INTEGER DEFAULT 1, solo_su_oficina INTEGER DEFAULT 0)""",
    """CREATE TABLE followups (
        source_id INTEGER PRIMARY KEY, ticket_id INTEGER, username TEXT, note TEXT,
        due_at TIMESTAMP, done_at TIMESTAMP, habilitado INTEGER DEFAULT 1,
        created_at TIMESTAMP)""",
    """CREATE TABLE client_notes (
        source_id INTEGER PRIMARY KEY, phone TEXT, author TEXT, body TEXT,
        habilitado INTEGER DEFAULT 1, created_at TIMESTAMP)""",
    """CREATE TABLE saved_replies (
        source_id INTEGER PRIMARY KEY, username TEXT, title TEXT, body TEXT,
        habilitado INTEGER DEFAULT 1, created_at TIMESTAMP,
        office_id INTEGER DEFAULT 1)""",
    """CREATE TABLE invitations (
        source_id INTEGER PRIMARY KEY, token TEXT, phone TEXT, created_by TEXT,
        created_at TIMESTAMP, used_at TIMESTAMP, used_by TEXT,
        habilitado INTEGER DEFAULT 1,
        office_id INTEGER NOT NULL DEFAULT 1)""",
    """CREATE TABLE tickets (
        source_id INTEGER PRIMARY KEY, phone TEXT, client_name TEXT, category TEXT,
        service_id TEXT, service_name TEXT, price_info TEXT, status TEXT,
        escalado INTEGER DEFAULT 0, notes TEXT, assigned_to TEXT,
        habilitado INTEGER DEFAULT 1, agent_active INTEGER DEFAULT 1,
        requirements_json TEXT,
        provider TEXT DEFAULT 'meta',
        first_response_at TIMESTAMP, first_response_by TEXT,
        resolved_at TIMESTAMP, status_changed_at TIMESTAMP,
        created_at TIMESTAMP, updated_at TIMESTAMP,
        office_id INTEGER DEFAULT 1)""",
    # Mínimas: _normalize_phones_espejo solo les lee/escribe phone. Las demás
    # columnas del espejo real no participan en ninguna prueba de este archivo.
    """CREATE TABLE feedback (
        source_id INTEGER PRIMARY KEY, phone TEXT)""",
    """CREATE TABLE nlu_events (
        source_id INTEGER PRIMARY KEY, phone TEXT)""",
    """CREATE TABLE labels (
        source_id INTEGER PRIMARY KEY, nombre TEXT, color TEXT,
        descripcion TEXT, habilitado INTEGER DEFAULT 1,
        created_at TIMESTAMP, office_id INTEGER DEFAULT 1)""",
    """CREATE TABLE ticket_labels (
        source_id INTEGER PRIMARY KEY, ticket_id INTEGER, label_id INTEGER,
        created_by TEXT, habilitado INTEGER DEFAULT 1,
        created_at TIMESTAMP)""",
    """CREATE TABLE ticket_activity (
        source_id INTEGER PRIMARY KEY, ticket_id INTEGER, username TEXT,
        action TEXT, habilitado INTEGER DEFAULT 1, created_at TIMESTAMP)""",
    """CREATE TABLE conversations (
        phone TEXT PRIMARY KEY, state TEXT, context_json TEXT,
        failed_attempts INTEGER, habilitado INTEGER DEFAULT 1,
        provider TEXT DEFAULT 'meta', updated_at TIMESTAMP,
        office_id INTEGER DEFAULT 1)""",
    """CREATE TABLE admin_audit (
        source_id INTEGER PRIMARY KEY, admin TEXT, action TEXT, target TEXT,
        habilitado INTEGER DEFAULT 1, created_at TIMESTAMP)""",
    """CREATE TABLE cobros (
        source_id INTEGER PRIMARY KEY, ticket_id INTEGER, phone TEXT,
        client_name TEXT, service_id TEXT, concepto TEXT, moneda TEXT,
        monto_centavos INTEGER, monto_cotizado_centavos INTEGER,
        monto_catalogo_centavos INTEGER, monto_origen TEXT,
        monto_pagado_centavos INTEGER, moneda_pagada TEXT,
        monto_reembolsado_centavos INTEGER DEFAULT 0, comision_centavos INTEGER,
        neto_centavos INTEGER, moneda_liquidacion TEXT,
        balance_transaction_id TEXT, payout_id TEXT, metodos_ofrecidos TEXT,
        metodo_usado TEXT, msi_meses_ofrecidos TEXT, msi_meses_usado INTEGER,
        estado TEXT, estado_motivo TEXT, llave_cobro_vivo TEXT,
        llave_generacion TEXT, token_verificacion TEXT,
        stripe_checkout_session_id TEXT, stripe_payment_intent_id TEXT,
        stripe_charge_id TEXT, stripe_customer_id TEXT, url_pago TEXT,
        url_expira_at TIMESTAMP, voucher_url TEXT, voucher_expira_at TIMESTAMP,
        detalle_pendiente_at TIMESTAMP, livemode INTEGER,
        discrepancia INTEGER DEFAULT 0, disputado INTEGER DEFAULT 0,
        ultimo_evento_id TEXT, ultimo_evento_created INTEGER,
        confirmado_por_asesor INTEGER DEFAULT 0, confirmado_por TEXT,
        confirmado_at TIMESTAMP, referencia_recibo TEXT, plan_id INTEGER,
        origen TEXT, creado_por TEXT, cancelado_por TEXT, enviado_at TIMESTAMP,
        pagado_at TIMESTAMP, cancelado_at TIMESTAMP,
        aviso_pago_enviado_at TIMESTAMP, reconciliado_at TIMESTAMP,
        created_at TIMESTAMP, updated_at TIMESTAMP,
        habilitado INTEGER DEFAULT 1, office_id INTEGER DEFAULT 1)""",
    """CREATE TABLE intentos_cobro (
        source_id INTEGER PRIMARY KEY, cobro_id INTEGER, numero INTEGER,
        clave_idempotencia TEXT, operacion TEXT, metodos_solicitados TEXT,
        msi_meses TEXT, monto_centavos INTEGER, moneda TEXT, estado TEXT,
        stripe_object TEXT, stripe_id TEXT, stripe_request_id TEXT,
        http_status INTEGER, respuesta_json TEXT, error_codigo TEXT,
        error_mensaje TEXT, reintentos INTEGER DEFAULT 0, origen TEXT,
        creado_por TEXT, wa_message_id INTEGER, livemode INTEGER,
        solicitado_at TIMESTAMP, respondido_at TIMESTAMP, cerrado_at TIMESTAMP,
        created_at TIMESTAMP, habilitado INTEGER DEFAULT 1)""",
    """CREATE TABLE eventos_pasarela (
        source_id INTEGER PRIMARY KEY, stripe_event_id TEXT, tipo TEXT,
        api_version TEXT, livemode INTEGER, stripe_created INTEGER,
        recibido_at TIMESTAMP, objeto_id TEXT, monto_centavos INTEGER,
        moneda TEXT, cobro_id INTEGER, intento_id INTEGER, payload_json TEXT,
        procesado_at TIMESTAMP, intentos_proceso INTEGER DEFAULT 0,
        resultado TEXT, detalle TEXT, habilitado INTEGER DEFAULT 1)""",
    """CREATE TABLE planes_cobro (
        source_id INTEGER PRIMARY KEY, phone TEXT, ticket_id INTEGER,
        service_id TEXT, concepto TEXT, moneda TEXT, monto_centavos INTEGER,
        periodicidad TEXT, dia_cobro INTEGER, paymentmethod_id INTEGER,
        proxima_ejecucion_at TIMESTAMP, ultima_ejecucion_at TIMESTAMP,
        ultimo_periodo_generado TEXT, cobros_generados INTEGER DEFAULT 0,
        intentos_fallidos_seguidos INTEGER DEFAULT 0, estado TEXT,
        fin_at TIMESTAMP, stripe_customer_id TEXT, stripe_subscription_id TEXT,
        creado_por TEXT, created_at TIMESTAMP, habilitado INTEGER DEFAULT 1)""",
    """CREATE TABLE payment_methods (
        source_id INTEGER PRIMARY KEY, phone TEXT, stripe_customer_id TEXT,
        stripe_payment_method_id TEXT, tipo TEXT, marca TEXT, ultimos4 TEXT,
        exp_mes INTEGER, exp_anio INTEGER, funding TEXT,
        predeterminado INTEGER DEFAULT 0, autorizado_por TEXT,
        autorizado_at TIMESTAMP, estado TEXT, created_at TIMESTAMP,
        habilitado INTEGER DEFAULT 1)""",
]


@pytest.fixture()
def mirror(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        for ddl in ARCHIVE_DDL:
            conn.execute(text(ddl))
    monkeypatch.setattr(archive, "_engine", engine)
    monkeypatch.setattr(archive, "_ready", True)
    monkeypatch.setattr(settings, "mssql_host", "test")
    monkeypatch.setattr(settings, "mssql_password", "test")
    yield engine
    engine.dispose()


def _wa_message(**kw) -> Message:
    base = dict(
        id=1,
        phone="5215500004444",
        direction=Direction.IN,
        body="hola",
        created_at=datetime.now(timezone.utc),
    )
    base.update(kw)
    return Message(**base)


def test_wa_message_archived_with_linked_client(mirror):
    archive.archive_wa_message(_wa_message())

    with mirror.connect() as conn:
        client = conn.execute(text("SELECT phone FROM wa_clients")).first()
        msg = conn.execute(text("SELECT phone, direction, body FROM wa_messages")).first()
    assert client[0] == "5215500004444"
    assert tuple(msg) == ("5215500004444", "in", "hola")


def test_wa_status_synced_by_source_id(mirror):
    archive.archive_wa_message(_wa_message(id=7, direction=Direction.OUT))

    archive.sync_wa_message(
        _wa_message(id=7, direction=Direction.OUT, wa_message_id="wamid.X", wa_status="read")
    )

    with mirror.connect() as conn:
        row = conn.execute(
            text("SELECT wa_message_id, wa_status FROM wa_messages WHERE source_id = 7")
        ).first()
    assert tuple(row) == ("wamid.X", "read")


def test_el_precio_llega_al_espejo_aunque_el_insert_original_nunca_haya_ocurrido(mirror):
    """La ruta de pérdida SILENCIOSA y PERMANENTE del veredicto de cobro.

    El precio solo viaja por UPDATE: el status de Meta es posterior al alta del
    mensaje. Si MSSQL estaba caído en el instante del INSERT —modo de falla que
    `_safe()` silencia por diseño— el UPDATE posterior no encontraba fila, y
    `backfill()` excluía `wa_messages` en aquel entonces, así que «Sincronizar
    ahora» tampoco lo recuperaba: ese cobro no aparecía en la contabilidad NUNCA.

    Se simula no llamando a archive_wa_message() antes del sync.
    """
    fila = _wa_message(
        id=99,
        direction=Direction.OUT,
        wa_message_id="wamid.PERDIDO",
        wa_status="sent",
        pricing_category="utility",
        pricing_type="regular",
        pricing_billable=True,
    )

    archive.sync_wa_message(fila)

    with mirror.connect() as conn:
        row = conn.execute(
            text(
                "SELECT pricing_category, pricing_type, wa_message_id"
                " FROM wa_messages WHERE source_id = 99"
            )
        ).first()
        # El remate pasa por _upsert_client: sin la fila del cliente, el FK del
        # espejo rechazaría el INSERT y el precio se perdería igual.
        cliente = conn.execute(
            text("SELECT phone FROM wa_clients WHERE phone = '5215500004444'")
        ).first()
    assert row is not None, "el UPDATE no encontró fila y nadie la insertó: cobro perdido"
    assert tuple(row) == ("utility", "regular", "wamid.PERDIDO")
    assert cliente is not None, "el mensaje entró sin su cliente: el FK del espejo lo rechazaría"


def test_wa_message_soft_delete_synced(mirror):
    archive.archive_wa_message(_wa_message(id=8, direction=Direction.OUT))

    archive.sync_wa_message(_wa_message(id=8, direction=Direction.OUT, habilitado=False))

    with mirror.connect() as conn:
        row = conn.execute(
            text("SELECT habilitado FROM wa_messages WHERE source_id = 8")
        ).first()
    assert row[0] == 0


def test_team_message_soft_delete_synced(mirror):
    archive.archive_team_message(
        TeamMessage(id=9, author="ana", body="oculto luego", created_at=datetime.now(timezone.utc))
    )

    archive.sync_team_message(
        TeamMessage(id=9, author="ana", body="oculto luego", habilitado=False)
    )

    with mirror.connect() as conn:
        row = conn.execute(
            text("SELECT habilitado FROM team_messages WHERE source_id = 9")
        ).first()
    assert row[0] == 0


def test_team_message_archived_with_linked_user(mirror):
    archive.archive_team_message(
        TeamMessage(id=3, author="ana", body="hola equipo", created_at=datetime.now(timezone.utc))
    )

    with mirror.connect() as conn:
        user = conn.execute(text("SELECT username FROM team_users")).first()
        msg = conn.execute(text("SELECT author, body FROM team_messages")).first()
    assert user[0] == "ana"
    assert tuple(msg) == ("ana", "hola equipo")


def test_listeners_mirror_session_inserts(mirror):
    app_engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(app_engine)

    with Session(app_engine) as s:
        s.add(Message(phone="5215500005555", direction=Direction.IN, body="vía listener"))
        s.add(TeamMessage(author="bob", body="listener de equipo"))
        s.commit()

    with mirror.connect() as conn:
        wa = conn.execute(text("SELECT body FROM wa_messages")).first()
        team = conn.execute(text("SELECT body FROM team_messages")).first()
    assert wa[0] == "vía listener"
    assert team[0] == "listener de equipo"


def test_disabled_mirror_is_noop(monkeypatch):
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "mssql_password", "")

    app_engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(app_engine)
    with Session(app_engine) as s:
        s.add(Message(phone="5215500006666", direction=Direction.IN, body="sin espejo"))
        s.commit()


def test_user_archived_with_bcrypt_hash_not_plaintext(mirror):
    archive.archive_user(
        User(id=1, username="ana", password_hash="$2b$12$abc.hashbcrypt", is_admin=True)
    )

    with mirror.connect() as conn:
        row = conn.execute(
            text("SELECT username, password_hash, is_admin FROM app_users")
        ).first()
    assert row[0] == "ana"
    assert row[1].startswith("$2b$")  # es un hash bcrypt, nunca la contraseña
    assert row[2] == 1


def test_user_upsert_updates_admin_flag(mirror):
    archive.archive_user(User(id=2, username="bob", password_hash="h", is_admin=False))
    archive.archive_user(User(id=2, username="bob", password_hash="h", is_admin=True))

    with mirror.connect() as conn:
        rows = conn.execute(text("SELECT is_admin FROM app_users WHERE source_id = 2")).all()
    assert len(rows) == 1  # upsert, no duplica
    assert rows[0][0] == 1


def test_invitation_archived_and_marked_used(mirror):
    archive.archive_invitation(
        Invitation(id=1, token="tok1", created_by="ana", created_at=datetime.now(timezone.utc))
    )
    archive.archive_invitation(
        Invitation(
            id=1, token="tok1", created_by="ana",
            created_at=datetime.now(timezone.utc),
            used_at=datetime.now(timezone.utc), used_by="carlos",
        )
    )

    with mirror.connect() as conn:
        row = conn.execute(
            text("SELECT token, used_by FROM invitations WHERE source_id = 1")
        ).first()
    assert row[0] == "tok1"
    assert row[1] == "carlos"


def test_ticket_archived_and_status_updated(mirror):
    def _ticket(**kw):
        base = dict(
            id=5, phone="521550000", category="Servicios básicos", service_id="curp",
            service_name="CURP", price_info="$80", status=TicketStatus.NUEVO,
            created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
        )
        base.update(kw)
        return Ticket(**base)

    archive.archive_ticket(_ticket())
    archive.archive_ticket(_ticket(status=TicketStatus.CERRADO, assigned_to="ana"))

    with mirror.connect() as conn:
        rows = conn.execute(
            text("SELECT status, assigned_to FROM tickets WHERE source_id = 5")
        ).all()
    assert len(rows) == 1
    assert tuple(rows[0]) == ("cerrado", "ana")


def test_activity_archived(mirror):
    archive.archive_activity(
        TicketActivity(id=1, ticket_id=5, username="ana", action="cerrado",
                       created_at=datetime.now(timezone.utc))
    )

    with mirror.connect() as conn:
        row = conn.execute(
            text("SELECT ticket_id, username, action FROM ticket_activity")
        ).first()
    assert tuple(row) == (5, "ana", "cerrado")


def test_admin_audit_archived(mirror):
    from app.models import AdminAudit

    archive.archive_admin_audit(
        AdminAudit(id=1, admin="jefa", action="usuario:deshabilitado", target="colab",
                   created_at=datetime.now(timezone.utc))
    )

    with mirror.connect() as conn:
        row = conn.execute(
            text("SELECT admin, action, target FROM admin_audit")
        ).first()
    assert tuple(row) == ("jefa", "usuario:deshabilitado", "colab")


def test_conversation_archived_by_phone(mirror):
    from app.models import Conversation

    archive.archive_conversation(
        Conversation(phone="521999", state="menu_categoria", context_json="{}",
                     failed_attempts=1, updated_at=datetime.now(timezone.utc))
    )
    archive.archive_conversation(
        Conversation(phone="521999", state="escalado", context_json="{}",
                     failed_attempts=0, updated_at=datetime.now(timezone.utc))
    )

    with mirror.connect() as conn:
        rows = conn.execute(text("SELECT state FROM conversations WHERE phone = '521999'")).all()
    assert len(rows) == 1  # keyed por phone, no duplica
    assert rows[0][0] == "escalado"


def test_espejo_de_cobranza_corre_despues_del_commit_no_dentro_del_flush(mirror):
    """Dentro del flush, el round-trip a MSSQL RETIENE el escritor de SQLite.

    `timeout` de pymssql es por statement y valía más que el busy_timeout de
    SQLite, así que con MSSQL lento —no caído; ahí `_safe` sí degrada la
    excepción a warning— el INSERT de eventopasarela del webhook moría con
    "database is locked" y devolvía 503, quemando reintentos de Stripe.
    """
    from sqlmodel import create_engine as crear

    from app.models import Cobro

    engine = crear("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    def _espejadas() -> int:
        with mirror.connect() as conn:
            return conn.execute(text("SELECT COUNT(*) FROM cobros")).scalar()

    with Session(engine) as s:
        s.add(Cobro(phone="5215500001111", concepto="CURP", monto_centavos=125000,
                    token_verificacion="tok-diferido", creado_por="ana"))
        s.flush()
        assert _espejadas() == 0, "el espejo corrió DENTRO del flush"
        s.commit()
        assert _espejadas() == 1, "el espejo no corrió al hacer commit"

    engine.dispose()


def test_cobro_archived_and_upsert_rewrites(mirror):
    """El upsert del espejo reescribe la MISMA fila (source_id): reprocesar un
    cobro tras un cambio de estado no lo duplica ni pierde el candado."""
    from app.models import Cobro

    def _cobro(**kw):
        base = dict(
            id=1, phone="5215500001111", concepto="CURP (folio #42)",
            monto_centavos=125000, estado="borrador",
            llave_cobro_vivo="5215500001111:svc:curp",
            token_verificacion="tok-sintetico", creado_por="ana",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        base.update(kw)
        return Cobro(**base)

    archive.archive_cobro(_cobro())
    archive.archive_cobro(_cobro(estado="pendiente", llave_cobro_vivo=None))

    with mirror.connect() as conn:
        rows = conn.execute(
            text("SELECT estado, llave_cobro_vivo, monto_centavos FROM cobros"
                 " WHERE source_id = 1")
        ).all()
    assert len(rows) == 1  # upsert, no duplica
    assert tuple(rows[0]) == ("pendiente", None, 125000)


def test_evento_pasarela_payload_recortado_a_32kb(mirror):
    """Un payload de 40 KB se espeja recortado: NVARCHAR(MAX) aguanta, pero el
    contrato del dominio es 32 KB y el espejo no debe confiar en que el
    escritor ya recortó."""
    from app.models import EventoPasarela

    archive.archive_evento_pasarela(
        EventoPasarela(
            id=1, stripe_event_id="evt_grande", tipo="checkout.session.completed",
            stripe_created=100, recibido_at=datetime.now(timezone.utc),
            payload_json="x" * 40_000,
        )
    )

    with mirror.connect() as conn:
        row = conn.execute(
            text("SELECT LENGTH(payload_json) FROM eventos_pasarela WHERE source_id = 1")
        ).first()
    assert row[0] == 32_768


def test_safe_no_martilla_init_dentro_de_60s(monkeypatch):
    """BUG prevenido (§7.4 del diseño): con MSSQL caído, cada webhook de Stripe
    disparaba un init() completo (CREATE DATABASE + todo el DDL, sin timeout).
    _safe debe intentar init() UNA vez y luego callar 60 s, no martillar."""
    llamadas = {"n": 0}

    def _init_que_falla():
        llamadas["n"] += 1
        # init() real captura sus propias excepciones y deja _ready=False.

    monkeypatch.setattr(archive, "_ready", False)
    monkeypatch.setattr(archive, "_PROXIMO_INIT", None)
    monkeypatch.setattr(archive, "init", _init_que_falla)
    monkeypatch.setattr(settings, "mssql_host", "test")
    monkeypatch.setattr(settings, "mssql_password", "test")

    archive._safe(archive.archive_user, None)
    archive._safe(archive.archive_user, None)
    archive._safe(archive.archive_user, None)

    assert llamadas["n"] == 1, (
        "init() se reintentó dentro de la ventana de 60 s: tormenta de DDL "
        "en el request path del webhook"
    )


def test_listeners_mirror_new_entities(mirror):
    app_engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(app_engine)

    with Session(app_engine) as s:
        s.add(User(username="jefa", password_hash="$2b$12$h", is_admin=True))
        s.add(Ticket(phone="521111", category="c", service_id="s", service_name="n",
                     price_info="$1"))
        s.commit()

    with mirror.connect() as conn:
        user = conn.execute(text("SELECT username FROM app_users")).first()
        ticket = conn.execute(text("SELECT phone FROM tickets")).first()
    assert user[0] == "jefa"
    assert ticket[0] == "521111"


# ---------------------------------------------------------------------------
# Normalización de teléfonos del espejo (el "7 vs 14" de /system)
# ---------------------------------------------------------------------------


def test_backfill_fusiona_telefonos_crudos_del_espejo(mirror):
    """El espejo acumuló la forma cruda de Meta junto a la canónica porque
    db._normalize_phones() corre sin listeners ORM. backfill() debe fusionar
    la cruda con la canónica y repuntar a sus hijos, en ese orden (FK)."""
    with mirror.begin() as conn:
        conn.execute(text(
            "INSERT INTO wa_clients (phone, name, habilitado, created_at) "
            "VALUES ('5215500004444', 'Ana', 1, '2025-01-01')"))
        conn.execute(text(
            "INSERT INTO wa_clients (phone, name, habilitado, created_at) "
            "VALUES ('525500004444', 'Ana', 1, '2025-01-01')"))
        conn.execute(text(
            "INSERT INTO wa_messages (source_id, phone, direction, body, created_at) "
            "VALUES (1, '5215500004444', 'in', 'hola', '2025-01-01')"))
    archive.backfill()
    with mirror.connect() as conn:
        clientes = [r[0] for r in conn.execute(text("SELECT phone FROM wa_clients"))]
        assert clientes == ["525500004444"], "la cruda se fusiona con la canónica"
        assert (
            conn.execute(text("SELECT phone FROM wa_messages")).scalar()
            == "525500004444"
        ), "el mensaje viejo queda repuntado al padre canónico"


def test_backfill_renombra_cuando_no_hay_canonica(mirror):
    with mirror.begin() as conn:
        conn.execute(text(
            "INSERT INTO wa_clients (phone, name, habilitado, created_at) "
            "VALUES ('5215500007777', 'Beto', 1, '2025-01-01')"))
    archive.backfill()
    with mirror.connect() as conn:
        clientes = [r[0] for r in conn.execute(text("SELECT phone FROM wa_clients"))]
        assert clientes == ["525500007777"]


def test_mirror_status_marca_desfase_cuando_el_espejo_tiene_de_mas(mirror):
    """El '>=' viejo dejaba pasar un espejo inflado como 'al día'."""
    from app.system import mirror_status

    with mirror.begin() as conn:
        conn.execute(text(
            "INSERT INTO wa_clients (phone, name, habilitado, created_at) "
            "VALUES ('525500009999', 'Caro', 1, '2025-01-01')"))
    filas = {f["table"]: f for f in mirror_status()}
    assert filas["wa_clients"]["sqlite"] == 0
    assert filas["wa_clients"]["mssql"] == 1
    assert not filas["wa_clients"]["ok"], "más filas en el espejo también es desfase"


def test_backfill_repara_los_mensajes_que_la_caida_de_mssql_se_trago(mirror, session):
    """BUG B3: los mensajes solo viajaban por listener. Si MSSQL estaba caído
    en el instante del INSERT, el hueco era permanente: /system marcaba
    wa_messages ok=False para siempre y «Sincronizar ahora» no podía
    repararlo. Ahora backfill() los upsertea por source_id."""
    session.add(_wa_message(id=41, phone="525500004444", body="perdido en la caída"))
    session.add(
        TeamMessage(
            id=42, author="ana", body="también perdido",
            created_at=datetime.now(timezone.utc),
        )
    )
    session.commit()

    archive.backfill()
    archive.backfill()  # idempotente: reprocesar no duplica ni revienta el UNIQUE

    with mirror.connect() as conn:
        wa = conn.execute(
            text("SELECT COUNT(*), MAX(body) FROM wa_messages WHERE source_id = 41")
        ).first()
        equipo = conn.execute(
            text("SELECT COUNT(*), MAX(body) FROM team_messages WHERE source_id = 42")
        ).first()
    assert tuple(wa) == (1, "perdido en la caída"), "backfill no reparó wa_messages"
    assert tuple(equipo) == (1, "también perdido"), "backfill no reparó team_messages"


# ---------------------------------------------------------------------------
# Multi-oficina (PR 1 de docs/SPEC-MULTI-OFICINA.md §8): office_id viaja al espejo
# ---------------------------------------------------------------------------
# Siempre office_id=2: con 1, el DEFAULT del fake enmascararía un upsert que no
# escribe la columna (la fila saldría en 1 aunque SQLite dijera otra cosa).


def test_office_id_viaja_al_espejo_por_upsert_generico(mirror):
    archive.archive_ticket(Ticket(id=7, phone="521111", category="c", service_id="s",
                                  service_name="n", price_info="$1", office_id=2))
    archive.archive_user(User(id=3, username="ana", password_hash="h", office_id=2))
    archive.sync_client_name(Client(phone="5215500001111", name="Ana", office_id=2))
    with mirror.connect() as conn:
        assert conn.execute(text("SELECT office_id FROM tickets WHERE source_id = 7")).scalar() == 2
        assert conn.execute(text("SELECT office_id FROM app_users WHERE source_id = 3")).scalar() == 2
        assert conn.execute(text("SELECT office_id FROM wa_clients WHERE phone = '5215500001111'")).scalar() == 2


def test_office_id_viaja_al_espejo_por_insert_literal(mirror):
    # archive_team_message es la única de las 15 que no pasa por _upsert.
    archive.archive_team_message(
        TeamMessage(id=9, author="bob", body="x", office_id=2,
                    created_at=datetime.now(timezone.utc))
    )
    with mirror.connect() as conn:
        assert conn.execute(text("SELECT office_id FROM team_messages WHERE source_id = 9")).scalar() == 2


def test_office_id_viaja_en_la_copia_diferida_de_cobro(mirror):
    # Molde: el helper _cobro() de test_cobro_archived_and_upsert_rewrites (mismos
    # campos mínimos). Pasa por _instantanea, la copia que _diferir encola tras el
    # commit: si esa copia no trajera office_id, el espejo de cobros quedaría en 1.
    from app.models import Cobro

    cobro = Cobro(id=1, phone="5215500001111", service_id="svc", concepto="CURP",
                  moneda="mxn", monto_centavos=125000, estado="borrador",
                  llave_cobro_vivo="5215500001111:svc:curp", token_verificacion="tok",
                  creado_por="ana", created_at=datetime.now(timezone.utc),
                  updated_at=datetime.now(timezone.utc), office_id=2)
    archive.archive_cobro(archive._instantanea(cobro))
    with mirror.connect() as conn:
        assert conn.execute(text("SELECT office_id FROM cobros WHERE source_id = 1")).scalar() == 2


def test_el_backfill_dice_en_que_etapa_se_cortó(mirror, monkeypatch, caplog):
    """REGLA: un backfill a medias tiene que decir DÓNDE se cortó.

    Los 31 bucles viven bajo un solo `try`, así que una caída de MSSQL a media
    corrida deja las primeras tablas espejadas y las últimas no. El log decía
    solo «incompleto», y a partir de ahí tocaba comparar los conteos de /system
    tabla por tabla para adivinar por dónde iba. La etapa no arregla el corte
    —el backfill es idempotente y el siguiente arranque lo retoma— pero convierte
    media hora de diagnóstico en una línea.
    """
    import logging

    def _revienta(_cobro):
        raise RuntimeError("MSSQL se cayó a media corrida")

    # Falla en cobranza: hay etapas antes (que sí completaron) y después.
    monkeypatch.setattr(archive, "archive_cobro", _revienta)

    # El Cobro va en la base OPERATIVA: backfill() lee de ahí y escribe al espejo.
    with Session(db.engine) as s:
        s.add(Cobro(phone="5215500000000", concepto="acta", monto_centavos=1000,
                    estado="borrador", token_verificacion="tok", creado_por="ana"))
        s.commit()

    with caplog.at_level(logging.WARNING, logger="app.archive"):
        archive.backfill()

    assert "incompleto" in caplog.text
    assert "cobranza" in caplog.text, (
        f"el log no dice en qué etapa se cortó: {caplog.text[-300:]}"
    )


def test_el_espejo_falso_no_inventa_tablas_que_el_real_no_tiene():
    """Candado del doble: toda tabla de ARCHIVE_DDL existe en el DDL de verdad.

    El fake es MÍNIMO a propósito —`nlu_events` declara dos columnas contra las
    ocho reales— y eso no es deuda: si `archive_*()` escribiera una columna que
    el fake no tiene, el INSERT revienta y la prueba se pone roja, que es la
    protección que se quiere. Lo que un fake mínimo NO puede detectar es un
    nombre que dejó de existir: si mañana se renombra una tabla en
    `_SCHEMA_DDL` y aquí se queda la vieja, estas pruebas siguen verdes
    escribiendo en una tabla que en producción ya no está — verdes en vacío,
    que es la falla que tests/test_mirror_consistency.py existe para evitar en
    el otro extremo.

    Solo se comparan NOMBRES: exigir columna por columna obligaría a mantener a
    mano las 32 tablas completas y el doble dejaría de ser un doble.
    """
    import re

    def nombres(bloques):
        return {
            m.group(1)
            for b in bloques
            if (m := re.search(r"CREATE TABLE\s+(?:dbo\.)?(\w+)", b))
        }

    del_fake = nombres(ARCHIVE_DDL)
    del_real = nombres(archive._SCHEMA_DDL)

    assert del_fake, "el detector no encontró tablas en el doble: candado ciego"
    assert del_real, "el detector no encontró tablas en _SCHEMA_DDL: candado ciego"

    fantasmas = sorted(del_fake - del_real)
    assert fantasmas == [], (
        f"el doble crea tablas que el espejo real no tiene: {fantasmas}. "
        "Si se renombró una tabla en _SCHEMA_DDL, hay que renombrarla aquí; si no, "
        "estas pruebas escriben en una tabla que en producción no existe."
    )
