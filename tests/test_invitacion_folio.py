"""B8 de ext-cse — la invitación del folio manual desde el número A.

El cliente habló con el número PERSONAL del colaborador: para A no tiene ventana y su primer
mensaje es «iniciado por el negocio». Lo que se protege:

  - sin opt-in NADA sale, y no se marca nada (un opt-in tardío desbloquea la invitación);
  - con opt-in y sin ventana sale la PLANTILLA (ContentSid/variables, según el proveedor),
    nunca texto plano;
  - con ventana abierta sale texto plano (la ventana ES el permiso);
  - el saliente queda como `Message` con su wamid: el webhook de estado actualiza su `wa_status`;
  - el rastro en la bitácora del folio la hace idempotente (no se repite) y el fallo reintenta
    con la pausa y el tope de B7;
  - el diálogo registra el opt-in ANTES de crear el folio (ALTA_CONSENT); «no» crea sin opt-in;
  - la plantilla no aprobada degrada: sin envío y con rastro, el folio sigue vivo.
"""

from datetime import timedelta

import pytest
from sqlmodel import select

import app.db as db
from app import alta_manual_barrido as barrido
from app import alta_manual_dialogo as dialogo
from app import conversation
from app import invitacion
from app import messaging
from app import twilio_client, whatsapp_client
from app.catalog import catalog
from app.config import settings
from app.models import (
    Client,
    Conversation,
    Direction,
    Message,
    Plantilla,
    PROVIDER_META,
    PROVIDER_TWILIO,
    Ticket,
    TicketActivity,
    TicketStatus,
    User,
    utcnow,
)

COLAB = "525533334444"
CLIENTE = "525511112222"
SERVICIO = "sb-acta-nacimiento"
CREADOR = "ana"


@pytest.fixture
def s(session, monkeypatch, catalogo_vivo):
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(settings, "alta_manual_enabled", True)
    session.add(User(username="ana", password_hash="x", phone="+52 1 55 3333 4444"))
    session.add(User(username="beto", password_hash="x"))
    session.add(User(username="carla", password_hash="x", office_id=2))
    session.commit()
    return session


def _nombre_servicio():
    return catalog.service_by_id(SERVICIO).name


def _folio_manual(s):
    """Un folio creado SIN pasar por el diálogo (para probar el barrido en limpio)."""
    from app.folio_manual import crear_folio_manual

    return crear_folio_manual(s, CREADOR, CLIENTE, "Juan Pérez", SERVICIO, CREADOR)


def _plantilla(s, aprobada=True, habilitada=True):
    p = Plantilla(
        codigo=invitacion.CODIGO_PLANTILLA,
        texto_referencia="Hola {{1}}, {{2}} registró tu folio #{{3}} ({{4}}). Responde para que te atendamos.",
        num_variables=4,
        habilitado=habilitada,
        aprobada_meta=aprobada,
        meta_template_name="invitacion_folio_v1",
        twilio_content_sid="CTabc123",
    )
    s.add(p)
    s.commit()
    return p


def _entrante_a(s, phone=CLIENTE, hace_s=0):
    m = Message(phone=phone, direction=Direction.IN, body="hola", created_at=utcnow() - timedelta(seconds=hace_s))
    s.add(m)
    s.commit()
    return m


def _marcas(s, ticket_id):
    s.expire_all()
    return [
        (a.action, a.username)
        for a in s.exec(
            select(TicketActivity).where(TicketActivity.ticket_id == ticket_id)
        ).all()
    ]


# --- El diálogo: el opt-in se registra ANTES de crear el folio -----------------------------------


def test_sin_optin_el_si_del_resumen_pide_consentimiento_y_luego_crea(s):
    from tests.test_alta_manual_dialogo import _dice, _hasta_resumen

    _hasta_resumen(s)
    out = _dice(s, "sí")
    assert "contactemos desde ese número" in out[0]
    assert s.get(Conversation, COLAB).state == conversation.ALTA_CONSENT
    assert s.exec(select(Ticket)).all() == []  # nada se crea antes del consentimiento
    out = _dice(s, "sí")
    assert out[0].startswith("Listo ✅ Creé el folio #")
    c = s.get(Client, CLIENTE)
    assert c.opt_in_at is not None and CREADOR in (c.opt_in_source or "")


def test_consentir_no_crea_el_folio_sin_optin(s):
    from tests.test_alta_manual_dialogo import _dice, _hasta_resumen

    _hasta_resumen(s)
    _dice(s, "sí")
    out = _dice(s, "no")
    assert out[0].startswith("Listo ✅ Creé el folio #")
    c = s.get(Client, CLIENTE)
    assert c.opt_in_at is None


def test_cancelar_en_el_consentimiento_descarta_todo(s):
    from tests.test_alta_manual_dialogo import _dice, _hasta_resumen

    _hasta_resumen(s)
    _dice(s, "sí")
    out = _dice(s, "cancelar")
    assert out[0].startswith("Listo, descarté")
    assert s.exec(select(Ticket)).all() == []
    assert s.get(Client, CLIENTE) is None
    assert s.get(Conversation, COLAB).state == "inicio"


def test_un_consentido_no_vuelve_a_ser_preguntado(s):
    from tests.test_alta_manual_dialogo import _dice, _hasta_resumen

    c = Client(phone=CLIENTE, name="Juan Pérez", opt_in_at=utcnow(), opt_in_source="ficha")
    s.add(c)
    s.commit()
    _hasta_resumen(s)
    out = _dice(s, "sí")
    assert out[0].startswith("Listo ✅ Creé el folio #")


def test_una_respuesta_rara_en_el_consentimiento_se_repregunta(s):
    from tests.test_alta_manual_dialogo import _dice, _hasta_resumen

    _hasta_resumen(s)
    _dice(s, "sí")
    assert "Responde Sí" in _dice(s, "quizá")[0]
    assert s.get(Conversation, COLAB).state == conversation.ALTA_CONSENT


# --- invitar(): la decisión -----------------------------------------------------------


def test_sin_optin_no_sale_nada(s, monkeypatch):
    t = _folio_manual(s)
    enviados = []
    monkeypatch.setattr(invitacion.messaging, "send_text", lambda *a, **k: enviados.append(a) or "w1")
    monkeypatch.setattr(invitacion.messaging, "send_template", lambda *a, **k: enviados.append(a) or "w1")
    estado, cuerpo, plantilla, variables = invitacion.preparar(s, t, CREADOR)
    assert estado == "sin-consentir"
    wamid = invitacion.enviar(t, estado, cuerpo, plantilla, [])
    assert enviados == [] and wamid is None


def test_con_optin_sin_ventana_sale_plantilla_con_sid_y_variables(s, monkeypatch):
    _folio_manual(s)
    c = s.get(Client, CLIENTE)
    c.opt_in_at = utcnow()
    c.opt_in_source = "test"
    s.add(c)
    s.commit()
    _plantilla(s)
    enviados = []

    def _tpl(phone, template_name, content_sid, variables, provider=None):
        enviados.append((phone, template_name, content_sid, variables))
        return "wa.plantilla.1"

    monkeypatch.setattr(invitacion.messaging, "send_template", _tpl)
    t = s.exec(select(Ticket)).first()
    estado, cuerpo, plantilla, variables = invitacion.preparar(s, t, CREADOR)
    assert estado == "plantilla"
    wamid = invitacion.enviar(t, estado, cuerpo, plantilla, variables)
    assert wamid == "wa.plantilla.1"
    (phone, nombre, sid, vars_) = enviados[0]
    assert (phone, nombre, sid) == (CLIENTE, "invitacion_folio_v1", "CTabc123")
    assert vars_[0] == "Juan" and vars_[1] == CREADOR and vars_[2] == str(t.id)
    assert _nombre_servicio() in vars_[3]


def test_con_ventura_abierta_sale_texto_plano_sin_plantilla(s, monkeypatch):
    t = _folio_manual(s)
    _entrante_a(s)
    enviados = []
    monkeypatch.setattr(invitacion.messaging, "send_text", lambda p, b, provider=None: enviados.append((p, b)) or "wa.texto.1")
    tpl = []
    monkeypatch.setattr(invitacion.messaging, "send_template", lambda *a, **k: tpl.append(a) or "x")
    estado, cuerpo, plantilla, variables = invitacion.preparar(s, t, CREADOR)
    assert estado == "texto" and plantilla is None
    wamid = invitacion.enviar(t, estado, cuerpo, plantilla, [])
    assert wamid == "wa.texto.1" and tpl == []
    assert CLIENTE in enviados[0][0] and f"#{t.id}" in enviados[0][1] and CREADOR in enviados[0][1]


def test_sin_plantilla_aprobada_degrada(s):
    t = _folio_manual(s)
    c = s.get(Client, CLIENTE)
    c.opt_in_at = utcnow()
    s.add(c)
    s.commit()
    _plantilla(s, aprobada=False)
    estado, *_ = invitacion.preparar(s, t, CREADOR)
    assert estado == "sin-plantilla"


def test_registrar_deja_el_mensaje_con_su_wamid(s):
    t = _folio_manual(s)
    invitacion.registrar(s, t, "texto", "Hola", "wa.42")
    s.expire_all()
    (m,) = s.exec(select(Message).where(Message.wa_message_id == "wa.42")).all()
    assert (m.phone, m.direction, m.body) == (CLIENTE, Direction.OUT, "Hola")


# --- invitar_una_vez(): el barrido ------------------------------------------------------


def _con_optin(s, t):
    c = s.get(Client, CLIENTE)
    c.opt_in_at = utcnow()
    s.add(c)
    s.commit()
    return t


def test_el_barrido_invita_el_folio_creado_y_no_lo_repite(s, monkeypatch):
    t = _folio_manual(s)
    _con_optin(s, t)
    _plantilla(s)
    salidas = []
    monkeypatch.setattr(invitacion, "enviar", lambda t, e, c, p, v: salidas.append(e) or "wa.1")

    assert barrido.invitar_una_vez() == 1
    assert barrido.invitar_una_vez() == 0  # idempotente: el rastro lo detiene
    assert salidas == ["plantilla"]
    assert _marcas(s, t.id).count(("aviso-cliente", CREADOR)) == 1
    # El saliente quedó registrado con su wamid.
    assert s.exec(select(Message).where(Message.wa_message_id == "wa.1")).first() is not None


def test_el_barrido_no_invita_sin_optin_y_luego_si(s, monkeypatch):
    t = _folio_manual(s)
    _plantilla(s)
    salidas = []
    monkeypatch.setattr(invitacion, "enviar", lambda t, e, c, p, v: salidas.append(e) or "wa.1")

    assert barrido.invitar_una_vez() == 0
    assert "aviso-cliente" not in [a for a, _ in _marcas(s, t.id)]  # no se marca: se reintenta
    _con_optin(s, t)
    assert barrido.invitar_una_vez() == 1
    assert salidas == ["plantilla"]


def test_el_fallo_se_reintenta_con_pausa_y_tope(s, monkeypatch):
    t = _folio_manual(s)
    _con_optin(s, t)
    _plantilla(s)
    monkeypatch.setattr(invitacion, "enviar", lambda t, e, c, p, v: None)

    assert barrido.invitar_una_vez(utcnow()) == 0
    assert ("aviso-cliente-fallo", CREADOR) in _marcas(s, t.id)
    # En pausa: no reintenta.
    assert barrido.invitar_una_vez(utcnow() + timedelta(seconds=30)) == 0
    # Pasada la pausa reintenta; al tercer fallo se rinde.
    assert barrido.invitar_una_vez(utcnow() + timedelta(seconds=barrido.AVISO_PAUSA_S + 1)) == 0
    assert barrido.invitar_una_vez(utcnow() + timedelta(seconds=2 * barrido.AVISO_PAUSA_S + 2)) == 0
    assert barrido.invitar_una_vez(utcnow() + timedelta(seconds=3 * barrido.AVISO_PAUSA_S + 3)) == 0
    assert _marcas(s, t.id).count(("aviso-cliente-fallo", CREADOR)) == barrido.MAX_INTENTOS
    assert ("aviso-cliente", CREADOR) not in _marcas(s, t.id)


def test_fuera_de_la_vigencia_no_se_invita(s, monkeypatch):
    t = _folio_manual(s)
    _con_optin(s, t)
    _plantilla(s)
    salidas = []
    monkeypatch.setattr(invitacion, "enviar", lambda t, e, c, p, v: salidas.append(e) or "wa.1")
    alta = s.exec(select(TicketActivity).where(TicketActivity.action == "alta-manual")).first()
    alta.created_at = utcnow() - timedelta(hours=barrido.AVISO_VIGENCIA_H + 1)
    s.add(alta)
    s.commit()
    assert barrido.invitar_una_vez() == 0 and salidas == []


def test_apagado_no_invita(s, monkeypatch):
    t = _folio_manual(s)
    _con_optin(s, t)
    monkeypatch.setattr(settings, "alta_manual_enabled", False)
    assert barrido.invitar_una_vez() == 0


# --- Los adaptadores: send_template ----------------------------------------------------------


def test_meta_envia_plantilla_por_nombre_y_variables(monkeypatch):
    # REGLA: Meta envía por el `name` de la plantilla aprobada; las variables llenan
    # el body {{1}}… en el orden del texto aprobado (v3 §4.3).
    enviados = []
    monkeypatch.setattr(
        whatsapp_client, "_send", lambda phone, payload: enviados.append(payload) or "wa.tpl"
    )
    wamid = whatsapp_client.send_template(CLIENTE, "invitacion_folio_v1", None, ["Ana", "Luis", "42", "CURP"])
    assert wamid == "wa.tpl"
    (payload,) = enviados
    assert payload["type"] == "template"
    assert payload["template"]["name"] == "invitacion_folio_v1"
    params = payload["template"]["components"][0]["parameters"]
    assert [p["text"] for p in params] == ["Ana", "Luis", "42", "CURP"]


def test_meta_sin_nombre_no_envia(monkeypatch):
    enviados = []
    monkeypatch.setattr(whatsapp_client, "_send", lambda phone, payload: enviados.append(payload) or "wa.tpl")
    assert whatsapp_client.send_template(CLIENTE, None, None, ["x"]) is None
    assert enviados == []


def test_twilio_envia_plantilla_por_content_sid(monkeypatch):
    # REGLA: Twilio exige el ContentSid (el name de Meta no sirve ahí); las variables
    # van como ContentVariables numeradas en el mismo orden.
    import json

    enviados = []
    monkeypatch.setattr(
        twilio_client, "_post_message", lambda phone, data: enviados.append(data) or "SM.tpl"
    )
    wamid = twilio_client.send_template(CLIENTE, "invitacion_folio_v1", "CTabc123", ["Ana", "Luis", "42", "CURP"])
    assert wamid == "SM.tpl"
    (data,) = enviados
    assert data["ContentSid"] == "CTabc123"
    assert json.loads(data["ContentVariables"]) == {"1": "Ana", "2": "Luis", "3": "42", "4": "CURP"}


def test_twilio_sin_sid_no_envia(monkeypatch):
    enviados = []
    monkeypatch.setattr(twilio_client, "_post_message", lambda phone, data: enviados.append(data) or "SM.tpl")
    assert twilio_client.send_template(CLIENTE, "invitacion_folio_v1", None, ["x"]) is None
    assert enviados == []


def test_el_puerto_no_desecha_el_content_sid(s, monkeypatch):
    # REGLA: el puerto debe pasar el content_sid al adaptador: es lo ÚNICO que Twilio
    # necesita para enviar. Si lo descarta, la invitación de los clientes de Twilio
    # falla en silencio y el barrido se rinde tras los tres intentos.
    s.add(Conversation(phone=CLIENTE, provider=PROVIDER_TWILIO))
    s.commit()
    enviados = []
    monkeypatch.setattr(
        twilio_client,
        "send_template",
        lambda p, n, c, v: enviados.append((n, c, list(v))) or "SM.tpl",
    )
    wamid = messaging.send_template(CLIENTE, "invitacion_folio_v1", "CTabc123", ["Ana"])
    assert wamid == "SM.tpl"
    assert enviados == [("invitacion_folio_v1", "CTabc123", ["Ana"])]


# --- El canal: la invitación sale por A aunque el cliente nunca le haya escrito ----------------
#
# Lo encontró el juez de extremo a extremo de C1. El cliente del folio manual le habló al número
# PERSONAL del colaborador: no tiene conversación con el bot, y el puerto, sin conversación, caía
# en Meta. Las pruebas de arriba no lo veían porque parchan `messaging.send_template` o le siembran
# al cliente una conversación de Twilio. Aquí el diálogo entra por la cuenta de Twilio (el número
# A) y los adaptadores reales quedan detrás del puerto.


@pytest.fixture
def por_twilio(monkeypatch):
    """Todo lo que Ana le dice al agente entra por la cuenta de Twilio, que es el número A."""
    import functools

    import tests.test_alta_manual_dialogo as dialogo_tests

    monkeypatch.setattr(
        dialogo_tests, "handle_incoming",
        functools.partial(conversation.handle_incoming, provider=PROVIDER_TWILIO),
    )


def _alta_por_a(s, consiente="sí"):
    """Ana completa el alta con el agente; `consiente` es su respuesta a «¿aceptó que lo contactemos?»."""
    from tests.test_alta_manual_dialogo import _dice, _hasta_resumen

    _hasta_resumen(s)
    _dice(s, "sí")
    assert _dice(s, consiente)[0].startswith("Listo ✅ Creé el folio #")
    s.expire_all()
    (t,) = s.exec(select(Ticket)).all()
    return t


def test_el_folio_manual_guarda_el_canal_por_el_que_hablo_el_colaborador(s, por_twilio):
    """Si el folio nace con el canal por omisión (Meta) aunque todo pasó por A, el cliente queda
    ligado a una cuenta con la que nunca habló nadie: el reporte por canal lo cuenta mal y el
    puerto no tiene de dónde sacar el número A."""
    t = _alta_por_a(s)
    assert s.get(Conversation, COLAB).provider == PROVIDER_TWILIO  # la premisa: Ana habló por A
    assert t.provider == PROVIDER_TWILIO, "el folio manual no recuerda por qué cuenta llegó"


def test_sin_conversacion_el_cliente_se_atiende_por_el_canal_de_su_folio(s, por_twilio):
    """El cliente del folio manual no tiene conversación con el bot. Si el puerto lo manda a Meta,
    lo que se le escriba —la invitación, una respuesta desde el CRM— sale por la otra cuenta o no
    sale."""
    _alta_por_a(s)
    assert s.get(Conversation, CLIENTE) is None  # la premisa: nunca le ha escrito a A
    assert messaging.canal_de(CLIENTE) == PROVIDER_TWILIO


def test_la_conversacion_del_cliente_manda_sobre_el_canal_de_su_folio(s, por_twilio):
    """«Se contesta por donde entró»: si el cliente ya escribió por la otra cuenta, la ventana
    abierta es la de ese número, y el canal de su folio no debe cambiarla."""
    _alta_por_a(s)
    s.add(Conversation(phone=CLIENTE, provider=PROVIDER_META))
    s.commit()
    assert messaging.canal_de(CLIENTE) == PROVIDER_META


def test_la_invitacion_de_un_cliente_nuevo_sale_por_a_con_la_plantilla_de_twilio(s, por_twilio, monkeypatch):
    """El caso para el que existe B8: un cliente que nunca le escribió a A, con consentimiento y
    plantilla aprobada. Si sale por Meta, llega desde otro número —contra el consentimiento
    registrado, que es «ser contactado desde A»— o no llega: la plantilla vive en Twilio y el
    barrido se rinde tras tres intentos."""
    _plantilla(s)
    _alta_por_a(s, consiente="sí")
    twilio, meta = [], []
    monkeypatch.setattr(
        twilio_client, "send_template", lambda p, n, c, v: twilio.append((p, c, list(v))) or "SM.inv"
    )
    monkeypatch.setattr(whatsapp_client, "send_template", lambda p, n, c, v: meta.append(p) or "wamid.inv")

    assert barrido.invitar_una_vez() == 1
    assert meta == [], "la invitación salió por la cuenta de Meta, no por A"
    ((phone, sid, variables),) = twilio
    assert (phone, sid) == (CLIENTE, "CTabc123")
    assert variables[1] == CREADOR


# --- La ventana: el material que reenvió el colaborador no la abre --------------------------------
#
# Al crear el folio, el diálogo muda al chat del cliente lo que Ana reenvió: le pone el teléfono del
# cliente y sigue siendo ENTRANTE. Pero ese mensaje se lo mandó a A el colaborador, no el cliente:
# la ventana de 24 h de A sigue cerrada. Si se cuenta, la invitación toma el camino de texto plano
# —que no pide consentimiento y que el proveedor rechaza fuera de ventana— y el CRM ofrece escribir
# texto libre que nunca llega.


def _todo_lo_que_sale(monkeypatch):
    """Cada intento de envío, por cualquiera de las dos cuentas, de texto o de plantilla."""
    salidas = []
    for modulo, cuenta in ((twilio_client, "twilio"), (whatsapp_client, "meta")):
        monkeypatch.setattr(modulo, "send_text", lambda p, b, c=cuenta: salidas.append((c, "texto", p)) or "id.txt")
        monkeypatch.setattr(
            modulo, "send_template", lambda p, n, sid, v, c=cuenta: salidas.append((c, "plantilla", p)) or "id.tpl"
        )
    return salidas


def test_el_material_que_reenvio_ana_no_abre_la_ventana_de_a(s, por_twilio):
    from app import ventana

    _alta_por_a(s)
    s.expire_all()
    mudados = s.exec(select(Message).where(Message.phone == CLIENTE)).all()
    assert mudados and all(m.direction == Direction.IN for m in mudados)  # la premisa: está en su chat
    assert invitacion.ventana_abierta(s, CLIENTE) is False, "la ficha y la invitación ven una ventana falsa"
    assert ventana.abierta_para(s, CLIENTE) is False, "el CRM dejaría escribirle texto libre"


def test_el_cliente_que_escribe_a_a_si_abre_la_ventana_despues_del_material(s, por_twilio):
    """La otra cara: en cuanto el cliente le escribe a A, la ventana es real y la invitación va en
    texto plano. Sin esta prueba, «no contar entrantes en el chat del cliente» pasaría por arreglo."""
    from app import ventana

    _alta_por_a(s)
    _entrante_a(s)  # el cliente mismo le escribe a A
    assert invitacion.ventana_abierta(s, CLIENTE) is True
    assert ventana.abierta_para(s, CLIENTE) is True


def test_si_el_cliente_dijo_que_no_no_sale_nada_aunque_haya_material(s, por_twilio, monkeypatch):
    """«Sin opt-in NADA sale.» El material mudado no puede servir de permiso: la ventana es del
    cliente, no del colaborador que reenvió sus mensajes."""
    _plantilla(s)
    _alta_por_a(s, consiente="no")
    salidas = _todo_lo_que_sale(monkeypatch)

    assert barrido.invitar_una_vez() == 0
    assert salidas == [], f"se le intentó escribir a quien no consintió: {salidas}"
