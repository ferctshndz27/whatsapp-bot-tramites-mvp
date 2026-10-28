"""B5 de ext-cse — el barrido que cierra el lote y pregunta el número del cliente.

Lo que se protege: que el agente pregunte UNA vez y solo cuando el colaborador lleva callado la
espera configurada; que un reenvío a tiempo reinicie el reloj; que dos barridos no dupliquen la
pregunta; que un envío fallido se reintente con tope en vez de perderse o repetirse sin fin; que
nunca se mande con una sesión abierta; y que con la perilla apagada no salga nada.
"""

import json
from datetime import timedelta

import pytest
from sqlmodel import select

import app.db as db
from app import alta_manual_barrido as barrido
from app import messaging
from app.alta_manual_barrido import MAX_INTENTOS, barrer_una_vez, pregunta_numero
from app.config import settings
from app.conversation import ALTA_LOTE, ALTA_NUMERO, CAPTURE_TTL_HOURS, handle_incoming
from app.models import Conversation, Direction, Message, User, utcnow

COLAB = "525533334444"
PDF = {"ref": "media-1", "mime": "application/pdf", "nombre": "acta.pdf", "caption": ""}


@pytest.fixture
def s(session, monkeypatch, catalogo_vivo):
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(settings, "alta_manual_enabled", True)
    monkeypatch.setattr(settings, "alta_manual_espera_s", 30)
    session.add(User(username="ana", password_hash="x", phone="+52 1 55 3333 4444"))
    session.commit()
    return session


@pytest.fixture
def enviados(monkeypatch):
    """Intercepta la red y anota si había una sesión abierta con escrituras pendientes."""
    salida = []

    def _send(phone, body, provider=None):
        salida.append((phone, body))
        return f"wamid.out{len(salida)}"

    monkeypatch.setattr(messaging, "send_text", _send)
    return salida


def _conv(s):
    s.expire_all()
    return s.get(Conversation, COLAB)


def _ctx(s):
    return json.loads(_conv(s).context_json)


def _envejecer(s, segundos):
    s.expire_all()
    conv = s.get(Conversation, COLAB)
    conv.updated_at = utcnow() - timedelta(seconds=segundos)
    s.add(conv)
    s.commit()


def _lote(s, mensajes=("Juan Pérez, su acta",), archivos=0):
    for texto in mensajes:
        handle_incoming(s, COLAB, texto)
    for i in range(archivos):
        handle_incoming(s, COLAB, "", is_media=True, adjunto=PDF, wa_message_id=f"wamid.{i}")


def test_con_la_espera_cumplida_pregunta_el_numero_una_vez(s, enviados):
    _lote(s)
    _envejecer(s, 31)
    assert barrer_una_vez() == 1
    assert len(enviados) == 1 and enviados[0][0] == COLAB
    assert "número" in enviados[0][1].lower()
    assert _conv(s).state == ALTA_NUMERO
    assert _ctx(s)["pregunta"] == "enviada"
    # Segunda pasada: ya no hay nada que preguntar.
    assert barrer_una_vez() == 0
    assert len(enviados) == 1


def test_antes_de_la_espera_no_pregunta(s, enviados):
    _lote(s)
    _envejecer(s, 25)
    assert barrer_una_vez() == 0
    assert enviados == []
    assert _conv(s).state == ALTA_LOTE


def test_la_espera_sale_de_la_perilla(s, enviados, monkeypatch):
    monkeypatch.setattr(settings, "alta_manual_espera_s", 60)
    _lote(s)
    _envejecer(s, 45)
    assert barrer_una_vez() == 0
    monkeypatch.setattr(settings, "alta_manual_espera_s", 40)
    assert barrer_una_vez() == 1


def test_un_reenvio_a_tiempo_reinicia_el_reloj(s, enviados):
    _lote(s)
    _envejecer(s, 31)
    handle_incoming(s, COLAB, "y también su CURP")
    assert barrer_una_vez() == 0
    assert enviados == []


def test_con_la_perilla_apagada_no_sale_nada(s, enviados, monkeypatch):
    _lote(s)
    _envejecer(s, 600)
    monkeypatch.setattr(settings, "alta_manual_enabled", False)
    assert barrer_una_vez() == 0
    assert enviados == []
    assert _conv(s).state == ALTA_LOTE


def test_la_pregunta_cuenta_mensajes_y_archivos(s, enviados):
    _lote(s, mensajes=("uno", "dos"), archivos=2)
    _envejecer(s, 31)
    barrer_una_vez()
    assert "4 mensajes (2 archivos)" in enviados[0][1]


def test_el_texto_en_singular(s):
    assert pregunta_numero(1, 0).startswith("Recibí 1 mensaje.")
    assert "1 archivo)" in pregunta_numero(3, 1)


def test_el_saliente_queda_registrado_con_su_wamid(s, enviados):
    _lote(s)
    _envejecer(s, 31)
    barrer_una_vez()
    s.expire_all()
    salientes = s.exec(
        select(Message).where(Message.phone == COLAB).where(Message.direction == Direction.OUT)
    ).all()
    assert [m.wa_message_id for m in salientes] == ["wamid.out1"]
    assert salientes[0].body == enviados[0][1]


def test_dos_barridos_a_la_vez_no_duplican_la_pregunta(s, enviados, monkeypatch):
    """El segundo barrido leyó el candidato ANTES de que el primero lo reclamara."""
    _lote(s)
    _envejecer(s, 31)
    originales = barrido._candidatos
    viejos = []

    def _congelado(session, ahora):
        if not viejos:
            viejos.extend(originales(session, ahora))
        return list(viejos)

    monkeypatch.setattr(barrido, "_candidatos", _congelado)
    assert barrer_una_vez() == 1
    assert barrer_una_vez() == 0  # trae el candidato viejo: el UPDATE condicionado no toca nada
    assert len(enviados) == 1


def test_un_envio_fallido_se_reintenta_con_tope(s, monkeypatch):
    llamadas = []
    monkeypatch.setattr(messaging, "send_text", lambda *a, **k: llamadas.append(1))  # devuelve None
    _lote(s)
    _envejecer(s, 31)
    assert barrer_una_vez() == 0
    assert _conv(s).state == ALTA_NUMERO and _ctx(s)["pregunta"] == "pendiente"
    # Pasada la espera, se reclama de nuevo; con el tope se rinde.
    for _ in range(MAX_INTENTOS + 2):
        _envejecer(s, 31)
        barrer_una_vez()
    assert len(llamadas) == MAX_INTENTOS


def test_un_reintento_espera_su_turno(s, monkeypatch):
    """Una pregunta `pendiente` joven puede estar en vuelo: no se reclama otra vez."""
    llamadas = []
    monkeypatch.setattr(messaging, "send_text", lambda *a, **k: llamadas.append(1))
    _lote(s)
    _envejecer(s, 31)
    barrer_una_vez()
    barrer_una_vez()
    assert len(llamadas) == 1


def test_un_envio_que_lanza_no_mata_el_barrido(s, monkeypatch):
    def _revienta(*a, **k):
        raise RuntimeError("red caída")

    monkeypatch.setattr(messaging, "send_text", _revienta)
    _lote(s)
    _envejecer(s, 31)
    assert barrer_una_vez() == 0
    assert _ctx(s)["pregunta"] == "pendiente"


def test_un_lote_caducado_no_recibe_pregunta(s, enviados):
    _lote(s)
    _envejecer(s, CAPTURE_TTL_HOURS * 3600 + 60)
    assert barrer_una_vez() == 0
    assert enviados == []


def test_cancelar_despues_de_la_pregunta_descarta_todo(s, enviados):
    _lote(s)
    _envejecer(s, 31)
    barrer_una_vez()
    handle_incoming(s, COLAB, "cancelar")
    assert _conv(s).state == "inicio"
    assert barrer_una_vez() == 0


def test_un_mensaje_tras_la_pregunta_reabre_el_lote(s, enviados):
    _lote(s)
    _envejecer(s, 31)
    barrer_una_vez()
    handle_incoming(s, COLAB, "me faltó su CURP")
    assert _conv(s).state == ALTA_LOTE
    assert _ctx(s)["total"] == 2
    _envejecer(s, 31)
    assert barrer_una_vez() == 1  # pregunta de nuevo, con el lote completo
    assert len(enviados) == 2


def test_el_lifespan_arranca_y_cancela_el_barrido(monkeypatch):
    import asyncio

    import app.main as main
    from fastapi.testclient import TestClient

    estado = {"corrio": False, "cancelado": False}

    async def _bucle(*a, **k):
        estado["corrio"] = True
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            estado["cancelado"] = True
            raise

    monkeypatch.setattr(main, "on_startup", lambda: None)
    monkeypatch.setattr(main.alta_manual_barrido, "bucle", _bucle)
    with TestClient(main.app):
        pass
    assert estado == {"corrio": True, "cancelado": True}


def test_un_reenvio_entre_la_lectura_y_el_reclamo_gana_al_barrido(s, enviados, monkeypatch):
    """El candidato se leyó con la espera cumplida y, antes del reclamo, el colaborador reenvió algo:
    el UPDATE condicionado al `updated_at` leído no debe tocar el lote (sigue en `alta_manual_lote`)."""
    _lote(s)
    _envejecer(s, 31)
    originales = barrido._candidatos

    def _lee_y_luego_llega_un_mensaje(session, ahora):
        candidatos = originales(session, ahora)
        handle_incoming(s, COLAB, "y también su CURP")
        return candidatos

    monkeypatch.setattr(barrido, "_candidatos", _lee_y_luego_llega_un_mensaje)
    assert barrer_una_vez() == 0
    assert enviados == []
    assert _conv(s).state == ALTA_LOTE
    assert _ctx(s)["total"] == 2


def test_una_pregunta_ya_enviada_no_se_repite_con_el_paso_del_tiempo(s, enviados):
    _lote(s)
    _envejecer(s, 31)
    barrer_una_vez()
    for _ in range(3):
        _envejecer(s, 600)
        assert barrer_una_vez() == 0
    assert len(enviados) == 1


def test_el_bucle_sobrevive_a_una_pasada_que_revienta(monkeypatch):
    import asyncio

    pasadas = []

    def _barrido():
        pasadas.append(1)
        if len(pasadas) == 1:
            raise RuntimeError("base bloqueada")
        if len(pasadas) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(barrido, "barrer_una_vez", _barrido)

    async def _corre():
        await barrido.bucle(intervalo=0)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_corre())
    assert len(pasadas) == 3
