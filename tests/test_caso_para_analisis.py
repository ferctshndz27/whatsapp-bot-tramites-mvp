"""Regla de la revisión y caso para análisis de extremo a extremo (casos-analisis PR5, §5.2, §5.3, §8.3).

Un cliente sin folio abierto que pide que alguien revise su caso, o que mete varios trámites en un
texto largo, recibe «¿revisión de un asesor o un trámite?» antes de que se abra nada. Si elige la
revisión nace UN folio «Caso para análisis» sin producto, con todo su texto, y los mensajes que
fue sumando quedan ligados. Antes ese texto se troceaba en «¿Confirmas estos 3 trámites?».

Si una de estas pruebas se pone roja, el bot le hace a un cliente real una de estas cosas: le
abre trámites que no pidió, le abre dos casos por una sola solicitud o le pega una solicitud nueva
al caso de la anterior, le asigna el caso a quien atiende un producto que el caso no es, pierde
la mitad de lo que escribió, le liga un «ok» suelto, o cambia de conducta con la perilla apagada.
"""

import json

import pytest
from sqlmodel import select

import app.db as db
from app.catalog import catalog
from app.config import settings
from app.conversation import (
    CASO_BOTONES,
    CASO_NOTA_ARCHIVOS,
    CASO_PREGUNTA_BOTONES,
    CASO_RECIBIDO,
    FOLIO_LO_SUMO,
    FOLIO_LO_SUMO_ARCHIVO,
    MAX_NOTE_TEXT_CASO,
    handle_incoming,
    presentation,
)
from app.models import (
    CAPAS_EXTRA,
    CatalogAsesor,
    Conversation,
    Message,
    NluEvent,
    Ticket,
    TicketActivity,
    TicketStatus,
    User,
)
from app.whatsapp_client import BUTTON_TITLE_MAX, LIST_DESC_MAX, MAX_BUTTONS, MAX_LIST_ROWS

PHONE = "5215533334444"
SITUACION = "tengo una situación con mi pensión, qué se puede hacer"
DOS_LARGO = (
    "Buenos días, quiero sacar mi acta de nacimiento y también la descarga de mi CURP porque me "
    "las piden para entrar a mi empleo nuevo la próxima semana; si me pueden decir cuánto cuesta "
    "cada una y cuándo me las entregan, se los agradezco mucho."
)
CASO_LARGO = (
    "Buenas tardes, ya tengo lo de la CURP, también revisé mis semanas cotizadas y el NSS en PDF "
    "porque me dijeron en la ventanilla que los necesitaba, le mando la información para que la "
    "revise y me diga si falta algo más de mi parte para continuar con el trámite que tenemos abierto"
)
PDF = {"ref": "media-1", "mime": "application/pdf", "nombre": "resolucion.pdf", "caption": ""}


@pytest.fixture
def s(session, monkeypatch, request):
    monkeypatch.setattr(db, "engine", session.get_bind())
    request.getfixturevalue("catalogo_vivo")
    monkeypatch.setattr(settings, "contexto_folio_enabled", True)
    monkeypatch.setattr(settings, "revision_enabled", True)
    monkeypatch.setattr(settings, "contexto_min_palabras", 40)
    return session


def _conv(s, estado="menu_categoria", contexto=None):
    s.add(Conversation(phone=PHONE, state=estado, context_json=json.dumps(contexto or {})))
    s.commit()


def _estado(s):
    s.expire_all()
    conv = s.get(Conversation, PHONE)
    return conv.state, json.loads(conv.context_json or "{}")


def _tickets(s):
    s.expire_all()
    return s.exec(select(Ticket).where(Ticket.phone == PHONE)).all()


def _casos(s):
    return [t for t in _tickets(s) if t.service_id == "analisis"]


def _ligados(s, ticket_id):
    s.expire_all()
    return s.exec(select(Message).where(Message.ticket_id == ticket_id)).all()


def _folio(s, servicio_id="sb-curp", **extra) -> Ticket:
    sv = catalog.service_by_id(servicio_id)
    t = Ticket(phone=PHONE, category=sv.category_name, service_id=sv.id, service_name=sv.name,
               price_info=sv.price_info, **extra)
    s.add(t)
    s.commit()
    return t


# --- la pregunta ---------------------------------------------------------------------------------


def test_una_frase_sin_servicios_ofrece_revision_con_botones_que_caben(s):
    _conv(s)
    replies = handle_incoming(s, PHONE, SITUACION)

    estado, contexto = _estado(s)
    assert estado == "aclarando_caso" and contexto["servicios"] == []
    salida = presentation(s, PHONE, replies)
    assert salida[-1] == {"text": CASO_PREGUNTA_BOTONES, "buttons": CASO_BOTONES}
    assert len(CASO_BOTONES) <= MAX_BUTTONS
    assert all(len(t) <= BUTTON_TITLE_MAX for _, t in CASO_BOTONES)
    assert _tickets(s) == [], "abrió un folio antes de que el cliente eligiera"


def test_dos_servicios_en_un_texto_largo_ofrecen_la_lista_con_revision_primero(s):
    _conv(s)
    replies = handle_incoming(s, PHONE, DOS_LARGO)

    assert _estado(s)[1]["servicios"] == ["sb-acta-nacimiento", "sb-curp"]
    filas = presentation(s, PHONE, replies)[-1]["list"]
    assert filas[0] == ("1", "🔎 Revisión de un asesor")
    assert [f[1] for f in filas[1:3]] == [catalog.service_by_id("sb-acta-nacimiento").name,
                                          catalog.service_by_id("sb-curp").name]
    assert filas[-1] == ("4", "📋 Otro trámite")
    assert len(filas) <= MAX_LIST_ROWS and all(len(t) <= LIST_DESC_MAX for _, t in filas)
    assert not any("$" in r or "precio" in r.lower() for r in replies), "la pregunta no cotiza (D11)"


# --- la revisión: el caso para análisis -----------------------------------------------------------


def test_elegir_revision_abre_un_caso_sin_producto_con_todo_el_texto(s):
    _conv(s)
    handle_incoming(s, PHONE, SITUACION)

    replies = handle_incoming(s, PHONE, "revision")

    casos = _casos(s)
    assert len(casos) == 1 and len(_tickets(s)) == 1, "abrió más que el caso"
    caso = casos[0]
    assert caso.escalado and caso.status == TicketStatus.NUEVO and caso.price_info == "N/A"
    assert caso.notes == "Caso para análisis. Mensaje del cliente:\n" + SITUACION
    assert [m.body for m in _ligados(s, caso.id)] == [SITUACION]
    acciones = [a.action for a in s.exec(select(TicketActivity).where(TicketActivity.ticket_id == caso.id))]
    assert f"info-cliente:{len(SITUACION)}c" in acciones
    assert replies[0].startswith(CASO_RECIBIDO)
    assert _estado(s)[0] == "escalado"
    eventos = s.exec(select(NluEvent).where(NluEvent.capa == "analisis")).all()
    assert len(eventos) == 1 and eventos[0].text == "" and "analisis" in CAPAS_EXTRA


def test_en_la_lista_la_fila_uno_es_la_revision(s):
    _conv(s)
    handle_incoming(s, PHONE, DOS_LARGO)
    handle_incoming(s, PHONE, "1")
    assert len(_casos(s)) == 1 and len(_tickets(s)) == 1


def test_el_caso_se_asigna_sin_pista_de_producto(s, monkeypatch):
    """Mutación 5: con los servicios nombrados como pista, el caso iría a quien atiende el acta."""
    monkeypatch.setattr(settings, "reparto_automatico", True)
    for nombre in ("ana", "beto"):
        s.add(User(username=nombre, password_hash="x"))
    s.add(CatalogAsesor(service_id="sb-acta-nacimiento", username="beto"))
    s.add(Ticket(phone="5210000000000", category="c", service_id="s", service_name="S",
                 price_info="$1", assigned_to="beto"))
    s.commit()
    _conv(s)
    handle_incoming(s, PHONE, DOS_LARGO)

    handle_incoming(s, PHONE, "1")

    assert _casos(s)[0].assigned_to == "ana", "el caso siguió la pista de un producto"


def test_sin_asesores_el_caso_queda_en_el_pool(s, monkeypatch):
    monkeypatch.setattr(settings, "reparto_automatico", False)
    _conv(s)
    handle_incoming(s, PHONE, SITUACION)
    handle_incoming(s, PHONE, "revision")
    caso = _casos(s)[0]
    assert caso.assigned_to is None
    assert not any(a.action.startswith("asignado-auto")
                   for a in s.exec(select(TicketActivity).where(TicketActivity.ticket_id == caso.id)))


def test_un_caso_de_1500_caracteres_llega_entero(s):
    """Mutación 6: con el tope de 500, el asesor lee un tercio del caso."""
    _conv(s)
    largo = (SITUACION + " y ") * 25
    assert 1000 < len(largo) <= MAX_NOTE_TEXT_CASO
    handle_incoming(s, PHONE, largo)
    handle_incoming(s, PHONE, "revision")
    assert largo in _casos(s)[0].notes


def test_con_un_caso_abierto_es_algo_nuevo_y_revision_abre_otro_caso(s):
    """Defecto de producción (operador, 2026-10-12, caso #74): el cliente con un caso abierto
    manda otra solicitud, contesta «Es algo nuevo» y elige «Revisión»; el bot le pegaba la
    solicitud al caso anterior en vez de abrirle su folio. Esta prueba afirmaba lo contrario
    («se anexa a ese caso y no abre otro», diseño §5.3); cambió porque cambiarla ES la tarea, por
    decisión del operador, no para que pase."""
    caso = _folio(s, "sb-curp")
    caso.service_id, caso.service_name, caso.escalado = "analisis", "Caso para análisis", True
    s.add(caso)
    s.commit()
    _conv(s)
    handle_incoming(s, PHONE, CASO_LARGO + " tengo una situación")
    assert _estado(s)[0] == "aclarando_folio"
    handle_incoming(s, PHONE, "nuevo")
    assert _estado(s)[0] == "aclarando_caso"

    replies = handle_incoming(s, PHONE, "revision")

    casos = sorted(_casos(s), key=lambda t: t.id)
    assert len(casos) == 2, "no abrió el folio de la solicitud nueva"
    assert replies[0].startswith(CASO_RECIBIDO)
    anterior, nuevo = casos
    assert anterior.id == caso.id
    assert "tengo una situación" not in (anterior.notes or ""), "pegó la solicitud al caso anterior"
    assert "tengo una situación" in nuevo.notes


def test_el_cliente_que_manda_otra_solicitud_recibe_su_propio_folio_y_el_primero_queda_intacto(s):
    """El flujo del operador de punta a punta, sin sembrar nada: solicitud → caso → segunda
    solicitud → «¿es para tu folio #N?» → «Es algo nuevo» → «Revisión». Si se pone roja, el
    cliente que ya confirmó que lo suyo es nuevo ve su solicitud pegada al folio de la anterior:
    el asesor del primero la lee como parte de otra cosa y la segunda nunca tiene folio propio."""
    _conv(s)
    handle_incoming(s, PHONE, DOS_LARGO)
    handle_incoming(s, PHONE, "1")
    primero = _casos(s)[0]
    notas_antes = primero.notes
    ligados_antes = sorted(m.id for m in _ligados(s, primero.id))

    pregunta = handle_incoming(s, PHONE, CASO_LARGO)
    assert f"folio #{primero.id}" in pregunta[0], "no preguntó si era del folio abierto"
    handle_incoming(s, PHONE, "nuevo")
    replies = handle_incoming(s, PHONE, "1")

    assert replies[0].startswith(CASO_RECIBIDO), "no confirmó el folio nuevo"
    casos = sorted(_casos(s), key=lambda t: t.id)
    assert len(casos) == 2 and casos[0].id == primero.id, "no nació un segundo folio"
    segundo = casos[1]
    assert primero.notes == notas_antes, "la solicitud nueva se pegó a las notas del caso anterior"
    assert CASO_LARGO[:60] in segundo.notes and DOS_LARGO[:60] not in segundo.notes
    assert sorted(m.id for m in _ligados(s, primero.id)) == ligados_antes, "el primero ganó mensajes"
    assert [m.body for m in _ligados(s, segundo.id)] == [CASO_LARGO], "el segundo no quedó con lo suyo"
    for caso in casos:
        acciones = [a.action for a in s.exec(select(TicketActivity).where(TicketActivity.ticket_id == caso.id))]
        assert sum(a.startswith("info-cliente") for a in acciones) == 1, f"folio #{caso.id}: {acciones}"
    assert _estado(s)[0] == "escalado"


def test_solo_archivos_y_es_algo_nuevo_con_un_caso_abierto_abre_otro_caso_con_el_archivo(s):
    """Lo mismo cuando la solicitud nueva es solo un archivo: antes se pegaba al caso anterior."""
    caso = _folio(s, "sb-curp")
    caso.service_id, caso.service_name, caso.escalado = "analisis", "Caso para análisis", True
    s.add(caso)
    s.commit()
    _conv(s)
    handle_incoming(s, PHONE, "", is_media=True, adjunto=PDF)
    assert handle_incoming(s, PHONE, "nuevo") == [CASO_PREGUNTA_BOTONES]

    handle_incoming(s, PHONE, "revision")

    casos = sorted(_casos(s), key=lambda t: t.id)
    assert len(casos) == 2, "no abrió el folio del archivo nuevo"
    assert casos[1].notes == CASO_NOTA_ARCHIVOS
    assert [m.media_ref for m in _ligados(s, casos[1].id)] == ["media-1"]
    assert not _ligados(s, casos[0].id), "el archivo se pegó al caso anterior"


def test_si_es_del_mismo_folio_el_caso_sigue_siendo_uno_y_se_le_anexa(s):
    """El otro lado de la misma pregunta (D3): «Sí, de ese folio» SÍ junta. Si se pone roja, la
    corrección de arriba se pasó de largo y el cliente que sigue hablando de lo mismo recibe un
    folio por cada mensaje."""
    _conv(s)
    handle_incoming(s, PHONE, DOS_LARGO)
    handle_incoming(s, PHONE, "1")
    primero = _casos(s)[0]

    handle_incoming(s, PHONE, CASO_LARGO)
    replies = handle_incoming(s, PHONE, "si")

    assert len(_casos(s)) == 1, "abrió otro caso aunque el cliente dijo que era del mismo folio"
    assert f"folio #{primero.id}" in replies[0]
    assert CASO_LARGO[:60] in primero.notes


# --- mientras está pendiente ------------------------------------------------------------------------


def test_archivos_y_texto_en_medio_se_suman_y_quedan_en_el_caso(s):
    _conv(s)
    handle_incoming(s, PHONE, SITUACION)
    assert handle_incoming(s, PHONE, "", is_media=True, adjunto=PDF) == [FOLIO_LO_SUMO_ARCHIVO]
    assert handle_incoming(s, PHONE, CASO_LARGO) == [FOLIO_LO_SUMO]
    assert CASO_BOTONES and "revise tu caso" in handle_incoming(s, PHONE, "ok")[0]
    handle_incoming(s, PHONE, "revision")

    caso = _casos(s)[0]
    cuerpos = sorted(m.body for m in _ligados(s, caso.id))
    assert len(cuerpos) == 3 and "ok" not in cuerpos, "ligó el «ok» o perdió un mensaje"
    assert CASO_LARGO[:40] in caso.notes
    acciones = [a.action for a in s.exec(select(TicketActivity).where(TicketActivity.ticket_id == caso.id))]
    assert "archivo-cliente:1" in acciones


def test_solo_archivos_y_es_algo_nuevo_con_revision_encendida_ofrece_los_botones(s):
    _folio(s)
    _conv(s)
    handle_incoming(s, PHONE, "", is_media=True, adjunto=PDF)
    replies = handle_incoming(s, PHONE, "nuevo")
    assert replies == [CASO_PREGUNTA_BOTONES]

    handle_incoming(s, PHONE, "revision")

    caso = _casos(s)[0]
    assert caso.notes == CASO_NOTA_ARCHIVOS
    assert [m.media_ref for m in _ligados(s, caso.id)] == ["media-1"]


# --- las otras salidas -----------------------------------------------------------------------------


def test_elegir_un_servicio_lo_confirma_y_al_si_liga_lo_sumado_a_ese_folio(s):
    _conv(s)
    handle_incoming(s, PHONE, DOS_LARGO)

    replies = handle_incoming(s, PHONE, "3")

    estado, contexto = _estado(s)
    assert estado == "confirmar" and contexto["pending_service_ids"] == ["sb-curp"]
    assert contexto["desde_id"] is not None
    assert "Confirmas" in replies[0] or "confirm" in replies[0].lower()
    handle_incoming(s, PHONE, "si")
    folios = _tickets(s)
    assert [t.service_id for t in folios] == ["sb-curp"]
    assert [m.body for m in _ligados(s, folios[0].id)] == [DOS_LARGO]


def test_el_nombre_del_servicio_en_vez_del_numero_tambien_elige(s):
    _conv(s)
    handle_incoming(s, PHONE, DOS_LARGO)
    handle_incoming(s, PHONE, catalog.service_by_id("sb-acta-nacimiento").name)
    assert _estado(s)[1]["pending_service_ids"] == ["sb-acta-nacimiento"]


def test_otro_tramite_lleva_al_menu_y_no_liga_nada(s):
    _conv(s)
    handle_incoming(s, PHONE, DOS_LARGO)
    replies = handle_incoming(s, PHONE, "4")
    assert _estado(s)[0] == "menu_categoria"
    assert catalog.format_category_menu()[:30] in replies[0]
    s.expire_all()
    assert all(m.ticket_id is None for m in s.exec(select(Message)).all())
    assert _tickets(s) == []


def test_un_texto_que_no_es_respuesta_re_pregunta(s):
    _conv(s)
    handle_incoming(s, PHONE, SITUACION)
    assert handle_incoming(s, PHONE, "¿y cuánto tarda?") == [CASO_PREGUNTA_BOTONES]
    assert _estado(s)[0] == "aclarando_caso"


# --- dónde corre y dónde no ---------------------------------------------------------------------------


@pytest.mark.parametrize("estado,contexto", [
    ("inicio", {}),
    ("menu_categoria", {}),
    ("menu_servicio", {"category_id": "salud", "page": 0}),
])
def test_dispara_sin_folios_desde_cada_estado_de_la_tabla(s, estado, contexto):
    _conv(s, estado, contexto)
    handle_incoming(s, PHONE, SITUACION)
    assert _estado(s)[0] == "aclarando_caso"


def test_dispara_en_escalado_cuando_ya_no_hay_folios_abiertos(s):
    _conv(s, "escalado")
    handle_incoming(s, PHONE, SITUACION)
    assert _estado(s)[0] == "aclarando_caso"


def test_con_un_folio_abierto_y_la_regla_del_folio_apagada_no_ofrece_revision(s, monkeypatch):
    """Fila OFF/ON de la matriz: con folio, el fallo original sigue (lo avisa el panel)."""
    monkeypatch.setattr(settings, "contexto_folio_enabled", False)
    _folio(s)
    _conv(s)
    handle_incoming(s, PHONE, SITUACION)
    assert _estado(s)[0] != "aclarando_caso"


def test_con_la_perilla_apagada_nada_cambia(s, monkeypatch):
    monkeypatch.setattr(settings, "revision_enabled", False)
    _conv(s)
    handle_incoming(s, PHONE, DOS_LARGO)
    estado, _ = _estado(s)
    # El camino de hoy con este texto (confirmar o «¿cuál acta?»): lo fija test_conversation; aquí
    # basta con que la regla no lo tome.
    assert estado in {"confirmar", "eligiendo_sugerencia"}
    assert _casos(s) == []


def test_apagar_la_perilla_con_la_pregunta_en_vuelo_no_reinterpreta_la_fila(s, monkeypatch):
    _conv(s)
    handle_incoming(s, PHONE, DOS_LARGO)
    monkeypatch.setattr(settings, "revision_enabled", False)
    handle_incoming(s, PHONE, "1")
    assert _casos(s) == [] and _estado(s)[0] != "aclarando_caso"


def test_un_pedido_corto_de_un_servicio_sigue_el_camino_de_hoy(s):
    _conv(s)
    handle_incoming(s, PHONE, "necesito que revisen mi acta de nacimiento")
    assert _estado(s)[0] == "confirmar"
