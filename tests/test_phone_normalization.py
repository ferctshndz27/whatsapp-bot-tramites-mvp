"""Identidad del cliente: un humano = una fila, venga por donde venga.

REGLA DE NEGOCIO: `client.phone` es la LLAVE PRIMARIA del cliente. Meta entrega
'5215500000000' y Twilio entregará 'whatsapp:+5215500000000'. Sin normalizar en
el borde, la MISMA persona genera dos clientes, dos conversaciones y dos
historiales — y en cobranza, dos calendarios de cobro sobre la misma tarjeta.

Estas pruebas cubren los tres puntos donde se puede romper:
  1. La función pura (app.utils.normalize_phone)
  2. El borde entrante (app.webhook._process_message)
  3. Los datos YA guardados con el formato viejo (app.db._normalize_phones)

El punto 3 es el que casi se nos va: normalizar solo lo entrante haría que un
cliente existente no se encuentre y se cree duplicado — exactamente el bug que
la normalización quiere prevenir.
"""

import sqlite3

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

import app.db as db
from app.models import Client, Conversation, Message, Ticket
from app.utils import normalize_phone

CRUDO_META = "5215500000000"
CANONICO = "525500000000"


# ---------------------------------------------------------------------------
# 1. La función pura
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entrada",
    [
        "whatsapp:+5215500000000",  # Twilio
        "5215500000000",  # Meta con el "1" de México
        "525500000000",  # Meta sin el "1"
        "+52 155 0000 0000",  # tecleado por un humano
        "(55) 0000-0000",  # nacional, como lo escribiría un asesor
        "5500000000",  # nacional pelón
    ],
)
def test_todas_las_formas_del_mismo_numero_colapsan_a_una(entrada):
    # REGLA: seis formas de escribir a la misma persona, un solo cliente.
    assert normalize_phone(entrada) == CANONICO, (
        f"{entrada!r} no resolvió al canónico: crearía un cliente duplicado"
    )


@pytest.mark.parametrize("basura", ["", None, "sin dígitos", "   "])
def test_lo_que_no_es_telefono_devuelve_vacio_y_no_inventa(basura):
    # REGLA: mejor rechazar que fabricar un teléfono. El webhook descarta el
    # mensaje si esto sale vacío.
    assert normalize_phone(basura) == ""


@pytest.mark.parametrize("corto_o_largo", ["123", "1234567", "1234567890123456"])
def test_lo_que_no_cabe_en_e164_se_rechaza(corto_o_largo):
    # REGLA: `phone` es la LLAVE PRIMARIA del cliente. Aceptar "123" crearía una
    # fila imposible de encontrar después y, en cobranza, un cobro que nadie
    # puede conciliar. E.164 va de 8 a 15 dígitos; fuera de ahí no es teléfono.
    assert normalize_phone(corto_o_largo) == ""


def test_lada_extranjera_conserva_sus_digitos():
    # REGLA: el ajuste del "1" es una regla MEXICANA. Aplicarla a otras ladas
    # mutilaría números buenos; no conocemos sus reglas, así que no se inventan.
    assert normalize_phone("+1 415 555 0100") == "14155550100"


def test_normalizar_es_idempotente():
    # REGLA: corre en cada arranque y en cada mensaje. Si no fuera idempotente,
    # el teléfono se degradaría con cada pasada.
    assert normalize_phone(normalize_phone(CRUDO_META)) == normalize_phone(CRUDO_META)


# ---------------------------------------------------------------------------
# 2. El borde entrante
# ---------------------------------------------------------------------------


def _mudo(monkeypatch):
    """Silencia los envíos a Meta: aquí solo importa lo que queda en la base."""
    from app import webhook

    for fn in ("send_text", "send_list", "send_buttons"):
        monkeypatch.setattr(webhook.whatsapp_client, fn, lambda *a, **k: None)
    return webhook


def test_el_webhook_guarda_el_canonico_aunque_meta_mande_el_crudo(monkeypatch):
    # REGLA: el formato crudo del proveedor NO entra a la base. Este es el
    # candado que hace que Meta y Twilio compartan cliente en la Fase 4.
    webhook = _mudo(monkeypatch)

    webhook._process_message(
        {"from": CRUDO_META, "id": "wamid.TEST1", "type": "text", "text": {"body": "hola"}}
    )

    with Session(db.engine) as s:
        clientes = s.exec(select(Client)).all()
        mensajes = s.exec(select(Message).where(Message.phone == CANONICO)).all()

    assert [c.phone for c in clientes] == [CANONICO], (
        f"el cliente quedó con el formato crudo: {[c.phone for c in clientes]}"
    )
    assert mensajes, "el mensaje no quedó bajo el teléfono canónico"


def test_dos_formatos_del_mismo_humano_no_crean_dos_clientes(monkeypatch):
    # REGLA: el caso Meta + Twilio. Es LA prueba de este archivo.
    webhook = _mudo(monkeypatch)

    for i, formato in enumerate((CRUDO_META, CANONICO, "whatsapp:+5215500000000")):
        webhook._process_message(
            {"from": formato, "id": f"wamid.T{i}", "type": "text", "text": {"body": "hola"}}
        )

    with Session(db.engine) as s:
        clientes = s.exec(select(Client)).all()
        conversaciones = s.exec(select(Conversation)).all()

    assert len(clientes) == 1, f"tres formatos crearon {len(clientes)} clientes: {clientes}"
    assert len(conversaciones) == 1, (
        f"tres formatos crearon {len(conversaciones)} conversaciones: la máquina "
        "de estados quedaría partida en dos"
    )


# ---------------------------------------------------------------------------
# 3. Los datos ya guardados (la migración)
# ---------------------------------------------------------------------------


def _base_con_formato_viejo(tmp_path, monkeypatch, filas_extra=()):
    """Base con el esquema actual pero teléfonos en formato crudo de Meta."""
    ruta = tmp_path / "vieja.db"
    engine = create_engine(f"sqlite:///{ruta}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    SQLModel.metadata.create_all(engine)

    con = sqlite3.connect(ruta)
    try:
        con.execute(
            "INSERT INTO client (phone, name, created_at, habilitado) VALUES (?,?,?,1)",
            (CRUDO_META, "Ana Ruiz", "2025-01-05 10:00:00"),
        )
        con.execute(
            "INSERT INTO conversation (phone, state, context_json, failed_attempts,"
            " updated_at, habilitado) VALUES (?,?,?,?,?,1)",
            (CRUDO_META, "menu_servicio", '{"categoria": "actas"}', 1, "2025-01-05 10:00:00"),
        )
        con.execute(
            "INSERT INTO message (phone, direction, body, created_at, habilitado)"
            " VALUES (?,'IN','quiero mi acta','2025-01-05 10:00:01',1)",
            (CRUDO_META,),
        )
        con.execute(
            "INSERT INTO ticket (phone, category, service_id, service_name, price_info,"
            " status, escalado, agent_active, created_at, updated_at, habilitado)"
            " VALUES (?,'actas','sb-acta-nacimiento','Acta','A cotizar','NUEVO',0,1,"
            "'2025-01-05 10:00:02','2025-01-05 10:00:02',1)",
            (CRUDO_META,),
        )
        for sql, params in filas_extra:
            con.execute(sql, params)
        con.commit()
    finally:
        con.close()
    return engine


def test_la_migracion_reescribe_los_telefonos_historicos(tmp_path, monkeypatch):
    # REGLA: sin esto, normalizar lo entrante haría que el cliente histórico no
    # se encuentre y se duplique. La migración es parte de la corrección.
    engine = _base_con_formato_viejo(tmp_path, monkeypatch)

    db.init_db()

    with Session(engine) as s:
        clientes = s.exec(select(Client)).all()
        assert [c.phone for c in clientes] == [CANONICO]
        assert clientes[0].name == "Ana Ruiz", "la migración perdió el nombre"
        assert s.exec(select(Conversation)).first().phone == CANONICO
        assert s.exec(select(Message)).first().phone == CANONICO
        assert s.exec(select(Ticket)).first().phone == CANONICO


def test_la_migracion_conserva_el_estado_de_la_conversacion(tmp_path, monkeypatch):
    # REGLA: migrar es cambiar la llave, NUNCA reiniciar el flujo. Un cliente a
    # media conversación tiene que poder seguir donde estaba tras el deploy.
    engine = _base_con_formato_viejo(tmp_path, monkeypatch)

    db.init_db()

    with Session(engine) as s:
        conv = s.exec(select(Conversation)).one()
    assert conv.state == "menu_servicio"
    assert conv.context_json == '{"categoria": "actas"}'
    assert conv.failed_attempts == 1


def test_si_ya_existen_las_dos_formas_queda_un_solo_cliente(tmp_path, monkeypatch):
    # REGLA: la base de producción puede tener YA las dos formas (el wa_id de
    # Meta cambió de formato con los años). Fusionar sin perder el historial de
    # ninguna de las dos es el caso difícil de esta migración.
    engine = _base_con_formato_viejo(
        tmp_path,
        monkeypatch,
        filas_extra=(
            (
                "INSERT INTO client (phone, name, created_at, habilitado) VALUES (?,?,?,1)",
                (CANONICO, "Ana R.", "2025-02-01 09:00:00"),
            ),
            (
                "INSERT INTO message (phone, direction, body, created_at, habilitado)"
                " VALUES (?,'IN','segundo mensaje','2025-02-01 09:00:01',1)",
                (CANONICO,),
            ),
        ),
    )

    db.init_db()

    with Session(engine) as s:
        clientes = s.exec(select(Client)).all()
        mensajes = s.exec(select(Message)).all()

    assert len(clientes) == 1, f"la fusión dejó {len(clientes)} clientes: {clientes}"
    assert clientes[0].phone == CANONICO
    # Los DOS mensajes sobreviven y quedan colgados del mismo cliente: fusionar
    # no puede costar historial.
    assert len(mensajes) == 2, "la fusión perdió mensajes del histórico"
    assert {m.phone for m in mensajes} == {CANONICO}


def test_la_migracion_es_idempotente(tmp_path, monkeypatch):
    # REGLA: init_db() corre en cada arranque del contenedor y en cada corrida
    # del cron nocturno. Una segunda pasada no puede mover nada.
    engine = _base_con_formato_viejo(tmp_path, monkeypatch)

    db.init_db()
    with Session(engine) as s:
        antes = [(c.phone, c.name) for c in s.exec(select(Client)).all()]
        mensajes_antes = len(s.exec(select(Message)).all())

    db.init_db()
    db.init_db()

    with Session(engine) as s:
        despues = [(c.phone, c.name) for c in s.exec(select(Client)).all()]
        mensajes_despues = len(s.exec(select(Message)).all())

    assert antes == despues
    assert mensajes_antes == mensajes_despues
