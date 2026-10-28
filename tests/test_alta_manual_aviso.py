"""B7 de ext-cse — el aviso al colaborador a quien le dejaron el folio.

Lo que se protege: que el aviso salga una sola vez y solo cuando el asignado NO es quien lo creó;
que un envío fallido se reintente con pausa y tope en vez de perderse o repetirse sin fin; que un
asignado sin celular no trabe el barrido; y que el rastro quede en la bitácora del folio.
"""

from datetime import timedelta

import pytest
from sqlmodel import select

import app.db as db
from app import alta_manual_barrido as barrido
from app import messaging
from app.alta_manual_barrido import MAX_INTENTOS, avisar_una_vez
from app.config import settings
from app.folio_manual import crear_folio_manual
from app.models import Direction, Message, TicketActivity, User, utcnow

CLIENTE = "525511112222"
BETO = "525544445555"
SERVICIO = "sb-acta-nacimiento"


@pytest.fixture
def s(session, monkeypatch, catalogo_vivo):
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(settings, "alta_manual_enabled", True)
    session.add(User(username="ana", password_hash="x", phone="+52 1 55 3333 4444"))
    session.add(User(username="beto", password_hash="x", phone="+52 1 55 4444 5555"))
    session.add(User(username="sin", password_hash="x"))
    session.commit()
    return session


@pytest.fixture
def enviados(monkeypatch):
    salida = []

    def _send(phone, body, provider=None):
        salida.append((phone, body))
        return f"wamid.aviso{len(salida)}"

    monkeypatch.setattr(messaging, "send_text", _send)
    return salida


def _folio(s, asignado):
    return crear_folio_manual(s, "ana", CLIENTE, "Juan Pérez", SERVICIO, asignado)


def _acciones(s, ticket):
    s.expire_all()
    filas = s.exec(select(TicketActivity).where(TicketActivity.ticket_id == ticket.id)).all()
    return [f.action for f in filas]


def _envejecer_fallos(s, segundos):
    s.expire_all()
    for f in s.exec(select(TicketActivity).where(TicketActivity.action == "aviso-alta-fallo")).all():
        f.created_at = utcnow() - timedelta(seconds=segundos)
        s.add(f)
    s.commit()


def test_avisa_al_asignado_una_sola_vez(s, enviados):
    t = _folio(s, "beto")
    assert avisar_una_vez() == 1
    assert enviados[0][0] == BETO
    assert f"#{t.id}" in enviados[0][1] and "ana" in enviados[0][1] and "Juan Pérez" in enviados[0][1]
    assert avisar_una_vez() == 0
    assert len(enviados) == 1
    assert "aviso-alta" in _acciones(s, t)


def test_si_el_folio_es_del_propio_creador_no_avisa(s, enviados):
    _folio(s, "ana")
    assert avisar_una_vez() == 0
    assert enviados == []


def test_el_aviso_queda_registrado_en_el_chat_del_asignado(s, enviados):
    _folio(s, "beto")
    avisar_una_vez()
    s.expire_all()
    (m,) = s.exec(select(Message).where(Message.phone == BETO).where(Message.direction == Direction.OUT)).all()
    assert m.wa_message_id == "wamid.aviso1" and m.body == enviados[0][1]


def test_con_la_perilla_apagada_no_avisa(s, enviados, monkeypatch):
    _folio(s, "beto")
    monkeypatch.setattr(settings, "alta_manual_enabled", False)
    assert avisar_una_vez() == 0 and enviados == []


def test_un_asignado_sin_celular_se_marca_y_no_traba(s, enviados):
    t = _folio(s, "sin")
    assert avisar_una_vez() == 0
    assert "aviso-alta-sin-celular" in _acciones(s, t)
    assert avisar_una_vez() == 0
    assert _acciones(s, t).count("aviso-alta-sin-celular") == 1


def test_un_envio_fallido_se_reintenta_con_pausa_y_tope(s, monkeypatch):
    llamadas = []
    monkeypatch.setattr(messaging, "send_text", lambda *a, **k: llamadas.append(1))  # None
    t = _folio(s, "beto")
    assert avisar_una_vez() == 0
    assert avisar_una_vez() == 0  # dentro de la pausa: no insiste
    assert len(llamadas) == 1
    for _ in range(MAX_INTENTOS + 2):
        _envejecer_fallos(s, 61)
        avisar_una_vez()
    assert len(llamadas) == MAX_INTENTOS
    assert _acciones(s, t).count("aviso-alta-fallo") == MAX_INTENTOS


def test_un_reintento_exitoso_cierra_el_aviso(s, monkeypatch):
    respuestas = iter([None, "wamid.ok"])
    monkeypatch.setattr(messaging, "send_text", lambda *a, **k: next(respuestas))
    t = _folio(s, "beto")
    avisar_una_vez()
    _envejecer_fallos(s, 61)
    assert avisar_una_vez() == 1
    assert "aviso-alta" in _acciones(s, t)


def test_un_envio_que_lanza_no_mata_el_barrido(s, monkeypatch):
    def _revienta(*a, **k):
        raise RuntimeError("red caída")

    monkeypatch.setattr(messaging, "send_text", _revienta)
    t = _folio(s, "beto")
    assert avisar_una_vez() == 0
    assert "aviso-alta-fallo" in _acciones(s, t)


def test_un_alta_vieja_ya_no_se_avisa(s, enviados):
    t = _folio(s, "beto")
    s.expire_all()
    for a in s.exec(select(TicketActivity).where(TicketActivity.ticket_id == t.id)).all():
        a.created_at = utcnow() - timedelta(hours=barrido.AVISO_VIGENCIA_H + 1)
        s.add(a)
    s.commit()
    assert avisar_una_vez() == 0 and enviados == []


def test_el_bucle_tambien_avisa(s, enviados):
    _folio(s, "beto")
    barrido._pasada()
    assert len(enviados) == 1


def test_las_marcas_nuevas_tienen_etiqueta_en_activity():
    from app import activity

    for clave in ("alta-manual", "aviso-alta", "aviso-alta-fallo", "aviso-alta-sin-celular"):
        assert activity._label(clave) != clave
