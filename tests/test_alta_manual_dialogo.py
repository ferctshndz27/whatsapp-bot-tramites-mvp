"""B6 de ext-cse — el diálogo del alta manual: número → confirmar → nombre → trámite → asignado → resumen.

Lo que se protege: que NADA se escriba hasta el «Sí» del resumen (cancelar o equivocarse no deja un
cliente ni un folio a medias), que el material del lote termine en el chat del cliente y ligado al
folio, que cada entrada inválida se re-pregunte en vez de avanzar, y que ninguna lista pase de las
10 filas que admite Meta.
"""

import json

import pytest
from sqlmodel import select

import app.db as db
from app import alta_manual_dialogo as dialogo
from app import conversation
from app.catalog import catalog
from app.conversation import (
    ALTA_ASIGNADO,
    ALTA_CONFIRMA,
    ALTA_LOTE,
    ALTA_NOMBRE,
    ALTA_NUMERO,
    ALTA_RESUMEN,
    ALTA_SERVICIO,
    handle_incoming,
    presentation,
)
from app.config import settings
from app.models import Client, Conversation, Direction, Message, Ticket, TicketStatus, User

COLAB = "525533334444"
CLIENTE = "525511112222"
SERVICIO = "sb-acta-nacimiento"


@pytest.fixture
def s(session, monkeypatch, catalogo_vivo):
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(settings, "alta_manual_enabled", True)
    session.add(User(username="ana", password_hash="x", phone="+52 1 55 3333 4444"))
    session.add(User(username="beto", password_hash="x"))
    session.add(User(username="carla", password_hash="x", office_id=2))
    session.commit()
    return session


def _dice(s, texto, **kw):
    return handle_incoming(s, COLAB, texto, **kw)


def _estado(s):
    s.expire_all()
    return s.get(Conversation, COLAB).state


def _ctx(s):
    s.expire_all()
    return json.loads(s.get(Conversation, COLAB).context_json)


def _nombre_servicio():
    return catalog.service_by_id(SERVICIO).name


def _hasta_numero(s):
    """Lote de un mensaje y la pregunta ya enviada (lo que deja el barrido)."""
    _dice(s, "Juan Pérez, quiere su acta")
    conv = s.get(Conversation, COLAB)
    ctx = json.loads(conv.context_json)
    conversation._set_state(s, conv, ALTA_NUMERO, {**ctx, "pregunta": "enviada"})


def _hasta_servicio(s, numero="5511112222", nombre="Juan Pérez"):
    _hasta_numero(s)
    _dice(s, numero)
    _dice(s, "sí")
    if s.get(Client, CLIENTE) is None:
        _dice(s, nombre)


def _hasta_resumen(s, asignado="1"):
    _hasta_servicio(s)
    _dice(s, _nombre_servicio())
    _dice(s, asignado)


def _folios(s):
    s.expire_all()
    return list(s.exec(select(Ticket)).all())


def test_el_camino_feliz_crea_el_folio_y_muda_el_material(s):
    _hasta_numero(s)
    assert _dice(s, "55 1111 2222")[0].startswith(f"El número es {CLIENTE} (cliente nuevo)")
    assert _estado(s) == ALTA_CONFIRMA
    assert "nuevo" in _dice(s, "sí")[0]
    assert _estado(s) == ALTA_NOMBRE
    assert _dice(s, "Juan Pérez")[0].startswith("¿Qué trámite es?")
    _dice(s, _nombre_servicio())
    assert _estado(s) == ALTA_ASIGNADO
    resumen = _dice(s, "1")[0]
    # Tras la preposición va «ti» («asignado a ti»), no «tú».
    assert "Juan Pérez" in resumen and _nombre_servicio() in resumen and "• Asignado a: ti\n" in resumen, resumen
    assert _estado(s) == ALTA_RESUMEN
    # B8: el «sí» del resumen primero pide el consentimiento para invitar desde A.
    assert "contactemos desde ese número" in _dice(s, "sí")[0]
    assert _estado(s) == conversation.ALTA_CONSENT
    listo = _dice(s, "sí")[0]
    assert listo.startswith("Listo ✅ Creé el folio #") and listo.endswith(", asignado a ti."), listo
    (t,) = _folios(s)
    assert (t.phone, t.service_id, t.assigned_to, t.origen) == (CLIENTE, SERVICIO, "ana", "manual")
    assert t.status == TicketStatus.TOMADO and t.agent_active is False
    assert s.get(Client, CLIENTE).name == "Juan Pérez"
    assert _estado(s) == "inicio"
    # El material se mudó al chat del cliente y quedó ligado al folio.
    s.expire_all()
    material = s.exec(select(Message).where(Message.ticket_id == t.id)).all()
    assert material and all(m.phone == CLIENTE and m.direction == Direction.IN for m in material)


def test_nada_se_escribe_antes_del_si_final(s):
    _hasta_resumen(s)
    assert _folios(s) == []
    assert s.get(Client, CLIENTE) is None


def test_cancelar_en_cualquier_paso_no_deja_nada(s):
    for paso in (_hasta_numero, _hasta_servicio, _hasta_resumen):
        paso(s)
        _dice(s, "cancelar")
        assert _estado(s) == "inicio"
        assert _folios(s) == [] and s.get(Client, CLIENTE) is None
        s.get(Conversation, COLAB).state = "inicio"


def test_corregir_vuelve_a_pedir_el_numero_sin_reabrir_la_pregunta_del_barrido(s):
    _hasta_numero(s)
    _dice(s, "5511112222")
    assert "correcto" in _dice(s, "corregir")[0]
    assert _estado(s) == ALTA_NUMERO and _ctx(s)["pregunta"] == "enviada"


def test_un_numero_invalido_se_repregunta(s):
    _hasta_numero(s)
    assert "no es válido" in _dice(s, "123")[0]
    assert _estado(s) == ALTA_NUMERO


def test_el_numero_del_propio_colaborador_se_rechaza(s):
    _hasta_numero(s)
    assert "tu número" in _dice(s, "55 3333 4444")[0]
    assert _estado(s) == ALTA_NUMERO


def test_texto_que_no_es_numero_sigue_siendo_material_del_lote(s):
    _hasta_numero(s)
    assert _dice(s, "me faltó su CURP") == []
    assert _estado(s) == ALTA_LOTE and _ctx(s)["total"] == 2


def test_un_cliente_existente_salta_el_nombre_y_muestra_el_suyo(s):
    s.add(Client(phone=CLIENTE, name="María López"))
    s.commit()
    _hasta_numero(s)
    assert "(María López)" in _dice(s, "5511112222")[0]
    assert _dice(s, "sí")[0].startswith("¿Qué trámite es?")


def test_un_cliente_de_otra_oficina_se_rechaza_al_dar_el_numero(s):
    s.add(Client(phone=CLIENTE, name="Ajeno", office_id=2))
    s.commit()
    _hasta_numero(s)
    assert "otra oficina" in _dice(s, "5511112222")[0]
    assert _estado(s) == ALTA_NUMERO


def test_un_cliente_dado_de_baja_se_rechaza_al_dar_el_numero(s):
    s.add(Client(phone=CLIENTE, name="Baja", habilitado=False))
    s.commit()
    _hasta_numero(s)
    assert "dado de baja" in _dice(s, "5511112222")[0]


def test_un_nombre_invalido_se_repregunta(s):
    _hasta_numero(s)
    _dice(s, "5511112222")
    _dice(s, "sí")
    assert "nombre" in _dice(s, "5")[0].lower()
    assert _estado(s) == ALTA_NOMBRE


def test_una_respuesta_rara_en_la_confirmacion_se_repregunta(s):
    _hasta_numero(s)
    _dice(s, "5511112222")
    assert "Sí o Corregir" in _dice(s, "quizá")[0]
    assert _estado(s) == ALTA_CONFIRMA


def test_el_trámite_se_elige_por_numero_o_por_nombre(s):
    _hasta_servicio(s)
    primero = dialogo._servicios()[0]
    _dice(s, "1")
    assert _estado(s) == ALTA_ASIGNADO and _ctx(s)["servicio"] == primero.id


def test_un_trámite_inexistente_repite_la_lista(s):
    _hasta_servicio(s)
    assert _dice(s, "zzzzzz")[0].startswith("¿Qué trámite es?")
    assert _estado(s) == ALTA_SERVICIO


def test_la_lista_de_trámites_pagina_de_8_y_nunca_pasa_de_10_filas(s):
    total = len(dialogo._servicios())
    assert total > dialogo.POR_PAGINA, "el catálogo de prueba debe obligar a paginar"
    _hasta_servicio(s)
    pagina = 0
    vistos = []
    while True:
        out = presentation(s, COLAB, [_ultima(s)])
        filas = out[-1]["list"]
        assert len(filas) <= 10
        vistos += [t for i, t in filas if i not in (dialogo.ID_MAS, dialogo.ID_ANTERIORES)]
        if not any(i == dialogo.ID_MAS for i, _ in filas):
            break
        _dice(s, dialogo.ID_MAS)
        pagina += 1
    assert vistos == [x.name for x in dialogo._servicios()]
    # Y «Anteriores» regresa.
    _dice(s, dialogo.ID_ANTERIORES)
    assert _ctx(s)["pagina"] == pagina - 1


def _ultima(s):
    """La pregunta vigente del paso, tal como la diría el agente."""
    conv = s.get(Conversation, COLAB)
    if conv.state == ALTA_SERVICIO:
        return dialogo._lista_servicios(s, conv, _ctx(s), _ctx(s).get("pagina", 0))[0]
    raise AssertionError(conv.state)


def test_yo_va_primero_y_solo_salen_los_de_la_oficina(s):
    _hasta_servicio(s)
    s.add(User(username="aaron", password_hash="x"))  # alfabéticamente antes que «ana»
    s.commit()
    texto = _dice(s, _nombre_servicio())[0]
    lineas = texto.splitlines()
    assert lineas[1] == "1. Yo (ana)"
    assert "2. aaron" in lineas and "3. beto" in lineas and "carla" not in texto  # carla es de otra oficina


def test_asignar_a_otro_deja_el_folio_nuevo_con_su_dueno(s):
    _hasta_servicio(s)
    _dice(s, _nombre_servicio())
    resumen = _dice(s, "beto")[0]
    assert "Asignado a: beto" in resumen
    _dice(s, "sí")
    _dice(s, "sí")  # B8: consentimiento para invitar desde A
    (t,) = _folios(s)
    assert (t.assigned_to, t.status) == ("beto", TicketStatus.NUEVO)


def test_un_asignado_invalido_repite_la_lista(s):
    _hasta_servicio(s)
    _dice(s, _nombre_servicio())
    assert _dice(s, "carla")[0].startswith("¿A quién se lo asigno?")
    assert _estado(s) == ALTA_ASIGNADO


def test_no_en_el_resumen_vuelve_a_elegir_tramite_sin_crear(s):
    _hasta_resumen(s)
    assert _dice(s, "no")[0].startswith("¿Qué trámite es?")
    assert _folios(s) == []


def test_una_respuesta_rara_en_el_resumen_se_repregunta(s):
    _hasta_resumen(s)
    assert "¿Lo creo?" in _dice(s, "mmm")[0]
    assert _estado(s) == ALTA_RESUMEN


def test_un_folio_repetido_avisa_y_vuelve_a_elegir(s):
    _hasta_resumen(s)
    _dice(s, "sí")
    _dice(s, "sí")  # B8: consentimiento; primer folio
    _hasta_resumen(s)
    respuesta = _dice(s, "sí")  # el cliente ya consintió: va directo al folio
    assert "ya tiene el folio #" in respuesta[0] and respuesta[1].startswith("¿Qué trámite es?")
    assert _estado(s) == ALTA_SERVICIO
    assert len(_folios(s)) == 1


def test_un_id_ajeno_en_el_lote_no_se_muda(s):
    """Los ids viajan en un JSON: solo se mudan los mensajes que de verdad son de este colaborador."""
    ajeno = Message(phone="525599990000", direction=Direction.IN, body="secreto")
    s.add(ajeno)
    s.commit()
    _hasta_resumen(s)
    conv = s.get(Conversation, COLAB)
    ctx = json.loads(conv.context_json)
    ctx["mensajes"].append(ajeno.id)
    conversation._set_state(s, conv, ALTA_RESUMEN, ctx)
    _dice(s, "sí")
    _dice(s, "sí")  # B8: consentimiento
    s.expire_all()
    ajeno = s.get(Message, ajeno.id)
    assert ajeno.phone == "525599990000" and ajeno.ticket_id is None


def test_un_paso_caducado_se_descarta_y_el_mensaje_abre_otro_lote(s):
    from datetime import timedelta

    from app.models import utcnow

    _hasta_servicio(s)
    conv = s.get(Conversation, COLAB)
    conv.updated_at = utcnow() - timedelta(hours=conversation.CAPTURE_TTL_HOURS + 1)
    s.add(conv)
    s.commit()
    assert _dice(s, "1") == []
    assert _estado(s) == ALTA_LOTE and _ctx(s)["total"] == 1


def test_confirmaciones_con_botones_y_listas(s):
    _hasta_numero(s)
    out = presentation(s, COLAB, _dice(s, "5511112222"))
    assert [b[0] for b in out[-1]["buttons"]] == ["si", "corregir"]
    _dice(s, "sí")
    _dice(s, "Juan Pérez")
    out = presentation(s, COLAB, [_ultima(s)])
    assert out[-1]["list"][0][0] == "1"
    _dice(s, _nombre_servicio())
    conv = s.get(Conversation, COLAB)
    out = presentation(s, COLAB, [dialogo._lista_asignados(s, conv, _ctx(s), 0)[0]])
    assert out[-1]["list"][0] == ("1", "Yo (ana)")
    resumen = _dice(s, "1")
    out = presentation(s, COLAB, resumen)
    assert [b[0] for b in out[-1]["buttons"]] == ["si", "no", "cancelar"]


def test_con_la_perilla_apagada_nadie_entra_al_dialogo(s, monkeypatch):
    monkeypatch.setattr(settings, "alta_manual_enabled", False)
    assert _dice(s, "5511112222")  # la cascada de siempre contesta
    assert _folios(s) == []
