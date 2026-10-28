"""B3 de ext-cse — el lote del alta manual (`alta_manual_lote`).

El colaborador atiende a un cliente por su teléfono personal y le reenvía el material al agente. Lo
que se protege aquí: que SOLO un colaborador habilitado entre al flujo (cualquier otro número sigue
la cascada de siempre, y con la perilla apagada nadie entra), que el agente no le conteste mientras
junta el lote, que los mensajes y archivos queden anotados sin guardarse en la ficha de nadie, y que
«cancelar» y la caducidad lo descarten en vez de arrastrarlo a un folio que nadie pidió.
"""

import json
from datetime import timedelta

import pytest
from sqlmodel import select

import app.db as db
from app import agent_settings, conversation
from app.config import Settings, settings
from app.conversation import (
    ALTA_LOTE,
    ALTA_LOTE_CANCELADO,
    CAPTURE_TTL_HOURS,
    MAX_LOTE_MENSAJES,
    handle_incoming,
)
from app.models import Conversation, Direction, Message, Ticket, User, utcnow

COLAB = "525533334444"
COLAB_TECLEADO = "+52 1 55 3333 4444"
EXTRANO = "525599990000"
PDF = {"ref": "media-1", "mime": "application/pdf", "nombre": "acta.pdf", "caption": ""}


@pytest.fixture
def s(session, monkeypatch, catalogo_vivo):
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(settings, "alta_manual_enabled", True)
    session.add(User(username="ana", password_hash="x", phone=COLAB_TECLEADO))
    session.commit()
    return session


def _conv(session, phone=COLAB) -> Conversation:
    session.expire_all()
    return session.get(Conversation, phone)


def _ctx(session, phone=COLAB) -> dict:
    return json.loads(_conv(session, phone).context_json)


def _ids(session, phone=COLAB, direction=Direction.IN) -> list[int]:
    session.expire_all()
    filas = session.exec(
        select(Message).where(Message.phone == phone).where(Message.direction == direction)
        .order_by(Message.id)
    ).all()
    return [m.id for m in filas]


def test_el_colaborador_entra_y_el_agente_calla(s):
    assert handle_incoming(s, COLAB, "Juan Pérez, quiere su acta de nacimiento") == []
    conv = _conv(s)
    assert conv.state == ALTA_LOTE
    ctx = _ctx(s)
    assert ctx["colaborador"] == "ana"
    assert ctx["mensajes"] == _ids(s)
    # Calla de verdad: ni respuesta ni saliente registrado.
    assert _ids(s, direction=Direction.OUT) == []


def test_el_telefono_del_colaborador_se_compara_normalizado(s):
    # El User se guardó como «+52 1 55 3333 4444» y el webhook entrega «525533334444».
    handle_incoming(s, COLAB, "hola")
    assert _conv(s).state == ALTA_LOTE


def test_un_numero_desconocido_sigue_la_cascada(s):
    respuestas = handle_incoming(s, EXTRANO, "hola")
    assert respuestas  # el saludo y el menú de siempre
    assert _conv(s, EXTRANO).state != ALTA_LOTE


def test_un_colaborador_deshabilitado_no_entra(s):
    u = s.exec(select(User).where(User.username == "ana")).one()
    u.habilitado = False
    s.add(u)
    s.commit()
    assert handle_incoming(s, COLAB, "hola")
    assert _conv(s).state != ALTA_LOTE


def test_un_usuario_sin_telefono_no_captura_a_nadie(s):
    u = s.exec(select(User).where(User.username == "ana")).one()
    u.phone = None
    s.add(u)
    s.commit()
    assert handle_incoming(s, EXTRANO, "hola")
    assert _conv(s, EXTRANO).state != ALTA_LOTE


def test_con_la_perilla_apagada_nadie_entra(s, monkeypatch):
    monkeypatch.setattr(settings, "alta_manual_enabled", False)
    assert handle_incoming(s, COLAB, "hola")
    assert _conv(s).state != ALTA_LOTE


def test_los_mensajes_y_archivos_se_acumulan_en_orden(s):
    handle_incoming(s, COLAB, "Juan Pérez")
    handle_incoming(s, COLAB, "", is_media=True, adjunto=PDF, wa_message_id="wamid.A")
    handle_incoming(s, COLAB, "quiere su acta")
    ids = _ids(s)
    assert len(ids) == 3
    assert _ctx(s)["mensajes"] == ids
    assert _conv(s).state == ALTA_LOTE
    assert _ids(s, direction=Direction.OUT) == []


def test_el_reintento_del_proveedor_no_duplica_el_mensaje(s):
    handle_incoming(s, COLAB, "Juan", wa_message_id="wamid.X")
    handle_incoming(s, COLAB, "Juan", wa_message_id="wamid.X")
    assert len(_ctx(s)["mensajes"]) == 1


def test_lo_del_lote_no_va_a_la_ficha_de_nadie(s, monkeypatch):
    llamadas = []
    monkeypatch.setattr(conversation, "_extract_client_data", lambda *a, **k: llamadas.append(a))
    handle_incoming(s, COLAB, "su CURP es GOMC800101HDFRRR09 y su correo juan@correo.com")
    assert llamadas == []


def test_el_lote_no_abre_folios(s):
    handle_incoming(s, COLAB, "Juan Pérez quiere su acta de nacimiento")
    assert s.exec(select(Ticket)).all() == []


def test_cancelar_descarta_el_lote(s):
    handle_incoming(s, COLAB, "Juan Pérez")
    handle_incoming(s, COLAB, "quiere su acta")
    assert handle_incoming(s, COLAB, "Cancelar") == [ALTA_LOTE_CANCELADO]
    assert _conv(s).state == "inicio"
    assert _ctx(s) == {}
    # Lo que escribió sigue en el chat, sin ligar a ningún folio.
    assert all(m.ticket_id is None for m in s.exec(select(Message)).all())
    # Y lo siguiente empieza un lote NUEVO, sin arrastrar el anterior.
    handle_incoming(s, COLAB, "otro cliente")
    assert len(_ctx(s)["mensajes"]) == 1


def test_cancelar_sin_lote_no_abre_uno(s):
    assert handle_incoming(s, COLAB, "cancelar") == [ALTA_LOTE_CANCELADO]
    assert _conv(s).state == "inicio"


def test_cancelar_solo_vale_como_palabra_completa(s):
    handle_incoming(s, COLAB, "Juan Pérez")
    handle_incoming(s, COLAB, "quiere cancelar su acta de matrimonio")
    assert _conv(s).state == ALTA_LOTE
    assert len(_ctx(s)["mensajes"]) == 2


def test_un_lote_caducado_se_descarta_y_este_mensaje_abre_otro(s):
    handle_incoming(s, COLAB, "cliente viejo")
    viejo = _ids(s)
    conv = _conv(s)
    conv.updated_at = utcnow() - timedelta(hours=CAPTURE_TTL_HOURS + 1)
    s.add(conv)
    s.commit()
    assert handle_incoming(s, COLAB, "cliente nuevo") == []
    ctx = _ctx(s)
    assert ctx["mensajes"] == [i for i in _ids(s) if i not in viejo]
    assert len(ctx["mensajes"]) == 1


def test_el_tope_de_mensajes_del_lote_no_revienta_ni_crece(s, monkeypatch):
    monkeypatch.setattr(conversation, "MAX_LOTE_MENSAJES", 3)
    for i in range(5):
        assert handle_incoming(s, COLAB, f"dato {i}") == []
    ctx = _ctx(s)
    assert ctx["mensajes"] == _ids(s)[:3]
    assert ctx["total"] == 5


def test_el_tope_por_omision_es_finito():
    assert 1 <= MAX_LOTE_MENSAJES <= 500


def test_si_el_colaborador_ya_no_es_colaborador_a_media_captura_sigue_la_cascada(s):
    handle_incoming(s, COLAB, "Juan Pérez")
    u = s.exec(select(User).where(User.username == "ana")).one()
    u.habilitado = False
    s.add(u)
    s.commit()
    assert handle_incoming(s, COLAB, "hola")  # un estado que la FSM no atiende cae a `inicio`
    assert _conv(s).state != ALTA_LOTE


def test_la_perilla_nace_apagada_y_es_editable(monkeypatch):
    monkeypatch.delenv("ALTA_MANUAL_ENABLED", raising=False)
    assert Settings().alta_manual_enabled is False
    monkeypatch.setenv("ALTA_MANUAL_ENABLED", "1")
    assert Settings().alta_manual_enabled is True
    assert agent_settings.CONFIG_EDITABLE["alta_manual_enabled"].tipo == "bool"
