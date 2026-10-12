"""Regla del folio de extremo a extremo (casos-analisis PR4, diseño §5.1, §5.4 y §8.3).

Un cliente con un folio abierto escribe información o manda un archivo: el bot pregunta «¿es para
tu folio #N?» y, si sí, lo ANEXA a ese folio (notas, actividad y liga de los mensajes) sin deducir
trámites. Antes troceaba ese texto en «¿Confirmas estos 3 trámites?» (caso real del 2026-09-24).

Si una de estas pruebas se pone roja, el bot le hace a un cliente real una de estas cosas: le abre
trámites que no pidió con la información de su folio, le liga a su folio un «ok» o una pregunta
suelta, se queda callado mientras la pregunta está pendiente, secuestra una confirmación o un
requisito con la pregunta del folio, o cambia de conducta con la perilla apagada.
"""

import json
from datetime import timedelta

import pytest
from sqlmodel import select

import app.db as db
from app import adjuntos
from app.catalog import catalog
from app.config import settings
from app.conversation import (
    CONFIRM_BUTTONS,
    FOLIO_BOTONES,
    FOLIO_LO_SUMO,
    FOLIO_LO_SUMO_ARCHIVO,
    MAX_NOTE_TEXT_CASO,
    _menu_rows,
    handle_incoming,
    presentation,
)
from app.models import (
    CAPAS_EXTRA,
    ClientNote,
    Conversation,
    Message,
    NluEvent,
    Ticket,
    TicketActivity,
    utcnow,
)
from app.whatsapp_client import BUTTON_TITLE_MAX, LIST_DESC_MAX, LIST_TITLE_MAX, MAX_BUTTONS, MAX_LIST_ROWS

PHONE = "5215522223333"
CASO = (
    "Buenas tardes, ya tengo lo de la CURP, también revisé mis semanas cotizadas y el NSS en PDF "
    "porque me dijeron en la ventanilla que los necesitaba, le mando la información para que la "
    "revise y me diga si falta algo más de mi parte para continuar con el trámite que tenemos abierto"
)
PDF = {"ref": "media-1", "mime": "application/pdf", "nombre": "constancia.pdf", "caption": ""}


@pytest.fixture
def s(session, monkeypatch, request):
    monkeypatch.setattr(db, "engine", session.get_bind())
    request.getfixturevalue("catalogo_vivo")
    monkeypatch.setattr(settings, "contexto_folio_enabled", True)
    monkeypatch.setattr(settings, "revision_enabled", False)
    monkeypatch.setattr(settings, "contexto_min_palabras", 40)
    return session


def _folio(s, servicio_id="imss-unificacion-nss", **extra) -> Ticket:
    servicio = catalog.service_by_id(servicio_id)
    t = Ticket(phone=PHONE, category=servicio.category_name, service_id=servicio.id,
               service_name=servicio.name, price_info=servicio.price_info, **extra)
    s.add(t)
    s.commit()
    return t


def _conv(s, estado="menu_categoria", contexto=None):
    s.add(Conversation(phone=PHONE, state=estado, context_json=json.dumps(contexto or {})))
    s.commit()


def _estado(s):
    s.expire_all()
    conv = s.get(Conversation, PHONE)
    return conv.state, json.loads(conv.context_json or "{}")


def _ligados(s, ticket_id):
    s.expire_all()
    return s.exec(select(Message).where(Message.ticket_id == ticket_id)).all()


def _folios(s):
    return s.exec(select(Ticket).where(Ticket.phone == PHONE)).all()


def _servicio_existe(servicio_id):
    assert catalog.service_by_id(servicio_id) is not None, servicio_id


# --- la pregunta y el anexo -------------------------------------------------------------------


def test_la_informacion_del_folio_recien_creado_se_anexa_a_ese_folio(s):
    folio = _folio(s)
    _conv(s)

    replies = handle_incoming(s, PHONE, CASO)

    assert _estado(s)[0] == "aclarando_folio"
    assert f"folio #{folio.id}" in replies[-1]
    assert not any("Confirmas estos" in r for r in replies)

    replies = handle_incoming(s, PHONE, "si")

    s.expire_all()
    assert len(_folios(s)) == 1, "abrió folios nuevos"
    assert f"#{folio.id}" in replies[0] and "lo agregué" in replies[0]
    assert CASO[:60] in s.get(Ticket, folio.id).notes
    assert [m.body for m in _ligados(s, folio.id)] == [CASO]
    acciones = [a.action for a in s.exec(select(TicketActivity).where(TicketActivity.ticket_id == folio.id))]
    assert f"info-cliente:{len(CASO)}c" in acciones
    assert _estado(s)[0] == "menu_categoria"


def test_con_un_folio_la_pregunta_lleva_botones_que_caben(s):
    _folio(s)
    _conv(s)
    replies = handle_incoming(s, PHONE, CASO)

    salida = presentation(s, PHONE, replies)

    assert salida[-1]["buttons"] == FOLIO_BOTONES
    assert len(FOLIO_BOTONES) <= MAX_BUTTONS
    assert all(len(titulo) <= BUTTON_TITLE_MAX for _, titulo in FOLIO_BOTONES)


def test_con_varios_folios_el_agente_pregunta_a_cual_y_anexa_al_elegido(s):
    viejo = _folio(s, "sb-curp")
    nuevo = _folio(s)
    _conv(s)

    replies = handle_incoming(s, PHONE, CASO)
    salida = presentation(s, PHONE, replies)
    filas = salida[-1]["list"]
    assert len(filas) == 4 and len(filas) <= MAX_LIST_ROWS
    assert filas[0][1].startswith(f"#{nuevo.id} · ") and filas[1][1].startswith(f"#{viejo.id} · ")
    assert filas[2][1] == "🆕 Es algo nuevo" and filas[3][1] == "📋 Otro folio"

    handle_incoming(s, PHONE, "2")

    assert [m.body for m in _ligados(s, viejo.id)] == [CASO]
    assert _ligados(s, nuevo.id) == []


def test_con_siete_abiertos_la_lista_trae_ocho_mas_dos_filas_y_otro_folio_acepta_un_numero(s):
    servicios = [sv.id for c in catalog.categories for sv in c.services][:9]
    folios = [_folio(s, sid) for sid in servicios]
    _conv(s)

    replies = handle_incoming(s, PHONE, CASO)
    filas = presentation(s, PHONE, replies)[-1]["list"]

    assert len(filas) == 10 == MAX_LIST_ROWS
    for _, titulo in filas:
        # whatsapp_client recorta a 24 y manda el nombre completo a la descripción (72).
        assert len(titulo[:LIST_TITLE_MAX]) <= LIST_TITLE_MAX and len(titulo) <= LIST_DESC_MAX
    assert handle_incoming(s, PHONE, "10")[0].startswith("Escribe el número de tu folio")
    el_mas_viejo = folios[0]  # el 9.º no cupo en la lista
    assert handle_incoming(s, PHONE, "#99999")[0].startswith("Escribe el número de tu folio")
    handle_incoming(s, PHONE, f"#{el_mas_viejo.id}")
    assert [m.body for m in _ligados(s, el_mas_viejo.id)] == [CASO]


def test_un_archivo_con_folio_abierto_pregunta_para_cual_y_lo_liga(s):
    folio = _folio(s)
    _conv(s)

    replies = handle_incoming(s, PHONE, "", is_media=True, adjunto=PDF)

    assert replies[0].startswith("Recibí tu archivo 📎. ¿Esta información es para tu folio")
    handle_incoming(s, PHONE, "si")
    ligados = _ligados(s, folio.id)
    assert len(ligados) == 1 and ligados[0].media_ref == "media-1"
    acciones = [a.action for a in s.exec(select(TicketActivity).where(TicketActivity.ticket_id == folio.id))]
    assert "archivo-cliente:1" in acciones
    assert not any(a.startswith("info-cliente") for a in acciones), "sin texto no hay nota"


def test_texto_y_archivos_mezclados_hacen_una_sola_pregunta_y_todo_queda_en_el_folio(s):
    folio = _folio(s)
    _conv(s)

    assert "¿Esta información" in handle_incoming(s, PHONE, CASO)[0]
    assert handle_incoming(s, PHONE, "", is_media=True, adjunto=PDF) == [FOLIO_LO_SUMO_ARCHIVO]
    otro = CASO.replace("Buenas tardes", "Y otra cosa")
    assert handle_incoming(s, PHONE, otro) == [FOLIO_LO_SUMO]
    assert handle_incoming(s, PHONE, "", is_media=True, adjunto={**PDF, "ref": "media-2"}) == [FOLIO_LO_SUMO_ARCHIVO]
    handle_incoming(s, PHONE, "si")

    assert len(_ligados(s, folio.id)) == 4
    notas = s.get(Ticket, folio.id).notes
    assert CASO[:40] in notas and otro[:40] in notas


def test_un_ok_en_medio_no_queda_ligado_al_folio(s):
    folio = _folio(s)
    _conv(s)
    handle_incoming(s, PHONE, CASO)

    respuesta = handle_incoming(s, PHONE, "ok")
    assert "¿Esta información" in respuesta[0], "un «ok» sin respuesta dejó al cliente en silencio"
    handle_incoming(s, PHONE, "si")

    assert [m.body for m in _ligados(s, folio.id)] == [CASO]


def test_una_pregunta_de_tiempos_en_medio_recibe_re_prompt_y_no_se_liga(s):
    folio = _folio(s)
    _conv(s)
    handle_incoming(s, PHONE, CASO)

    assert "¿Esta información" in handle_incoming(s, PHONE, "¿y cuánto tarda?")[0]
    handle_incoming(s, PHONE, "si")

    assert [m.body for m in _ligados(s, folio.id)] == [CASO]


def test_un_si_con_texto_detras_no_resuelve_y_re_pregunta(s):
    """Mutación 14: «sí» por prefijo resolvería por el cliente."""
    folio = _folio(s)
    _conv(s)
    handle_incoming(s, PHONE, CASO)

    replies = handle_incoming(s, PHONE, "Sí, y también")

    assert _estado(s)[0] == "aclarando_folio"
    assert "¿Esta información" in replies[0]
    assert _ligados(s, folio.id) == []


# --- dónde NO corre ----------------------------------------------------------------------------


def test_un_texto_largo_en_confirmar_no_pregunta_por_el_folio(s):
    """Mutación 12: la regla en `confirmar` secuestraría la confirmación."""
    _folio(s)
    _conv(s, "confirmar", {"pending_service_ids": ["sb-curp"]})

    handle_incoming(s, PHONE, CASO)

    assert _estado(s)[0] != "aclarando_folio"


def test_un_archivo_en_confirmar_recibe_el_acuse_y_la_confirmacion_sigue_pendiente(s):
    _folio(s)
    _conv(s, "confirmar", {"pending_service_ids": ["sb-curp"]})

    assert handle_incoming(s, PHONE, "", is_media=True, adjunto=PDF) == [adjuntos.ACUSE]
    estado, contexto = _estado(s)
    assert estado == "confirmar" and contexto["pending_service_ids"] == ["sb-curp"]


def test_un_si_con_curp_en_confirmar_re_pregunta_como_hoy(s):
    _folio(s)
    _conv(s, "confirmar", {"pending_service_ids": ["sb-curp"]})

    handle_incoming(s, PHONE, "si mi curp es GOMA800101HDFRRN09")

    assert _estado(s)[0] != "aclarando_folio"


def test_un_archivo_sin_folios_abiertos_recibe_el_acuse_de_hoy(s):
    _conv(s)
    assert handle_incoming(s, PHONE, "", is_media=True, adjunto=PDF) == [adjuntos.ACUSE]
    assert _estado(s)[0] == "menu_categoria"


def test_un_archivo_no_soportado_no_pregunta(s):
    _folio(s)
    _conv(s)
    assert handle_incoming(s, PHONE, "", is_media=True, adjunto={**PDF, "mime": "application/zip"}) == [
        adjuntos.NO_SOPORTADO
    ]
    assert _estado(s)[0] == "menu_categoria"


def _pidiendo_curp(s):
    folio = _folio(s, "sb-curp")
    folio.requirements_json = json.dumps([{"req": "clave curp", "valor": None, "pedido": True}])
    s.add(folio)
    s.commit()
    hace_una_hora = utcnow() - timedelta(hours=1)
    s.add(Conversation(phone=PHONE, state="pidiendo_requisitos",
                       context_json=json.dumps({"cola": [[folio.id, 0]]}), updated_at=hace_una_hora))
    s.commit()
    return folio, hace_una_hora


def test_mientras_pide_un_requisito_una_curp_sola_sigue_siendo_la_respuesta(s):
    """Mutación 2 (al revés): la regla no se come la respuesta al requisito."""
    folio, _ = _pidiendo_curp(s)

    handle_incoming(s, PHONE, "mi curp es GOMA800101HDFRRN09")

    s.expire_all()
    assert "GOMA800101HDFRRN09" in s.get(Ticket, folio.id).requirements_json
    assert _estado(s)[0] != "aclarando_folio"


def test_mientras_pide_un_requisito_un_caso_largo_pregunta_por_el_folio_y_luego_repite_la_pregunta_pendiente_sin_rejuvenecer_la_captura(s):
    """Mutación 2: sin la regla en `pidiendo_requisitos`, el caso se guarda como la CURP."""
    folio, antes = _pidiendo_curp(s)

    assert "¿Esta información" in handle_incoming(s, PHONE, CASO)[0]
    replies = handle_incoming(s, PHONE, "si")

    s.expire_all()
    assert "GOMA" not in (s.get(Ticket, folio.id).requirements_json or "")
    assert json.loads(s.get(Ticket, folio.id).requirements_json)[0]["valor"] is None
    assert "clave curp" in replies[-1], "no repitió la pregunta pendiente"
    conv = s.get(Conversation, PHONE)
    assert conv.state == "pidiendo_requisitos"
    assert abs((conv.updated_at.replace(tzinfo=None) - antes.replace(tzinfo=None)).total_seconds()) < 1


def test_dos_servicios_cortos_con_folio_abierto_preguntan_por_el_folio(s):
    _folio(s)
    _conv(s)
    handle_incoming(s, PHONE, "quiero acta y curp")
    assert _estado(s)[0] == "aclarando_folio"


def test_en_escalado_un_estatus_largo_recibe_el_estatus_y_la_informacion_pregunta_por_el_folio_antes_de_intentar_un_tramite(s):
    """Mutación 3: sin la regla en el escalado, el caso se troceaba en «¿Confirmas estos 3?»."""
    _folio(s, "escalado" if catalog.service_by_id("escalado") else "imss-unificacion-nss", escalado=True)
    _conv(s, "escalado")

    estatus = handle_incoming(s, PHONE, "¿cómo va mi trámite? " + " ".join(["dime"] * 45))
    assert estatus[0].startswith("Estatus de tus solicitudes")

    replies = handle_incoming(s, PHONE, CASO)
    assert _estado(s)[0] == "aclarando_folio"
    assert not any("Confirmas estos" in r for r in replies)


# --- las salidas -------------------------------------------------------------------------------


def test_es_algo_nuevo_con_revision_apagada_llama_al_handler_del_estado_previo(s):
    """Mutación 8: sin «Es algo nuevo», el cliente no tiene cómo decir que no es de su folio."""
    folio = _folio(s)
    _conv(s, "menu_categoria")
    handle_incoming(s, PHONE, "quiero acta y curp")

    replies = handle_incoming(s, PHONE, "nuevo")

    estado, contexto = _estado(s)
    assert estado not in {"aclarando_folio"}
    assert _ligados(s, folio.id) == []
    assert any("acta" in r.lower() for r in replies)


def test_es_algo_nuevo_en_escalado_sigue_la_rama_escalada(s):
    _folio(s, escalado=True)
    _conv(s, "escalado")
    handle_incoming(s, PHONE, CASO)

    replies = handle_incoming(s, PHONE, "nuevo")

    assert _estado(s)[0] in {"escalado", "confirmar", "eligiendo_sugerencia"}
    assert replies


def test_es_algo_nuevo_no_dispara_atajos_por_una_palabra_dentro_del_caso(s):
    """«servicios» dentro de un caso largo no es pedir el catálogo: el handler, no _dispatch."""
    _folio(s)
    _conv(s, "menu_categoria")
    largo = CASO + " y quiero saber de sus servicios"
    handle_incoming(s, PHONE, largo)

    replies = handle_incoming(s, PHONE, "nuevo")

    assert not any(r.startswith(catalog.format_category_menu()[:30]) for r in replies)


def test_solo_archivos_y_es_algo_nuevo_con_revision_apagada_acusa_y_ofrece_el_menu(s):
    _folio(s)
    _conv(s)
    handle_incoming(s, PHONE, "", is_media=True, adjunto=PDF)

    replies = handle_incoming(s, PHONE, "nuevo")

    assert replies[0].startswith("Recibí tu archivo 📎. ¿Qué trámite necesitas?")
    assert _estado(s)[0] == "menu_categoria"


# --- caducidad, perillas y datos ---------------------------------------------------------------


def test_tras_el_ttl_un_si_no_confirma_nada_y_la_conversacion_queda_en_menu(s):
    folio = _folio(s)
    _conv(s)
    handle_incoming(s, PHONE, CASO)
    conv = s.get(Conversation, PHONE)
    conv.updated_at = utcnow() - timedelta(hours=7)
    s.add(conv)
    s.commit()

    handle_incoming(s, PHONE, "si")

    assert _ligados(s, folio.id) == []
    assert _estado(s)[0] != "aclarando_folio"


def test_apagar_la_perilla_con_una_pregunta_en_vuelo_descarta_el_contexto_sin_reinterpretar_la_fila(s, monkeypatch):
    """Mutación 7 (en vuelo): «2» era una fila de la lista; apagada la perilla, no se anexa."""
    _folio(s, "sb-curp")
    folio = _folio(s)
    _conv(s)
    handle_incoming(s, PHONE, CASO)
    monkeypatch.setattr(settings, "contexto_folio_enabled", False)

    handle_incoming(s, PHONE, "1")

    assert _ligados(s, folio.id) == []
    assert _estado(s)[0] != "aclarando_folio"


def test_con_la_perilla_apagada_nada_cambia(s, monkeypatch):
    """Mutación 7: perilla ignorada → cambia la conducta con OFF."""
    monkeypatch.setattr(settings, "contexto_folio_enabled", False)
    _folio(s)
    _conv(s)

    handle_incoming(s, PHONE, CASO)
    assert _estado(s)[0] != "aclarando_folio"
    assert handle_incoming(s, PHONE, "", is_media=True, adjunto=PDF) == [adjuntos.ACUSE]


def test_las_claves_de_un_tercero_anexadas_no_van_a_la_ficha_del_remitente(s):
    _folio(s)
    _conv(s)
    con_curp = CASO + " la CURP de mi mamá es GOMA800101HDFRRN09"
    handle_incoming(s, PHONE, con_curp)
    handle_incoming(s, PHONE, "su nss es 12 34 56 7890 1")  # corta, durante la espera
    handle_incoming(s, PHONE, "si")

    notas = s.exec(select(ClientNote).where(ClientNote.phone == PHONE)).all()
    assert not any("GOMA800101HDFRRN09" in n.body or "12345678901" in n.body for n in notas)


def test_un_evento_por_anexo_y_sin_texto(s):
    _folio(s)
    _conv(s)
    handle_incoming(s, PHONE, CASO)
    handle_incoming(s, PHONE, "si")

    eventos = s.exec(select(NluEvent).where(NluEvent.capa == "folio")).all()
    assert len(eventos) == 1 and eventos[0].text == ""
    assert "folio" in CAPAS_EXTRA


def test_el_tope_del_anexo_es_de_2000_y_no_de_500(s):
    """Mutación 6 (la parte de PR4): un caso de 1500 caracteres llega entero."""
    folio = _folio(s)
    _conv(s)
    largo = (CASO + " ") * 6
    assert 1000 < len(largo) <= MAX_NOTE_TEXT_CASO

    handle_incoming(s, PHONE, largo)
    handle_incoming(s, PHONE, "si")

    assert largo.strip() in s.get(Ticket, folio.id).notes


def test_el_mismo_wa_message_id_dos_veces_no_anexa_dos_veces(s):
    folio = _folio(s)
    _conv(s)
    handle_incoming(s, PHONE, CASO, wa_message_id="wamid.1")
    handle_incoming(s, PHONE, CASO, wa_message_id="wamid.1")
    handle_incoming(s, PHONE, "si", wa_message_id="wamid.2")

    assert s.get(Ticket, folio.id).notes.count(CASO[:40]) == 1
    assert len(_ligados(s, folio.id)) == 1


def test_la_lista_de_folios_solo_existe_en_aclarando_folio(s):
    _conv(s)
    assert _menu_rows(s.get(Conversation, PHONE)) == [] or s.get(Conversation, PHONE).state != "aclarando_folio"
    assert CONFIRM_BUTTONS != FOLIO_BOTONES


# --- la ficha del remitente (diseño §5.4: la extracción va después de la regla) ----------------
#
# `_extract_client_data` copia a la ficha CURP, RFC y correo (TIPOS_EN_FICHA; el NSS no entra). Si
# corriera sobre un texto que la regla toma para anexarlo al folio, o durante la espera de la
# respuesta, la ficha del remitente guardaría claves que quizá son de un tercero (la CURP de la
# mamá). Estas dos pruebas vigilan esas dos puertas; la de arriba, `...de_un_tercero_anexadas...`,
# vigila la tercera (el camino normal).

CURP_AJENA = "GOMA800101HDFRRN09"


def _curps_en_la_ficha(s) -> list[str]:
    s.expire_all()
    notas = s.exec(select(ClientNote).where(ClientNote.phone == PHONE)).all()
    return [n.body for n in notas if CURP_AJENA in n.body]


def test_control_con_la_regla_apagada_una_curp_suelta_si_llega_a_la_ficha(s, monkeypatch):
    """Control de las dos pruebas de abajo: con la perilla apagada el bot hace lo de hoy y la
    CURP tecleada SÍ queda en la ficha. Sin este control, «no está en la ficha» podría ser cierto
    solo porque la CURP del texto no se detecta, y las dos pruebas no medirían nada."""
    monkeypatch.setattr(settings, "contexto_folio_enabled", False)
    _folio(s)
    _conv(s)

    handle_incoming(s, PHONE, "mi curp es " + CURP_AJENA)

    assert _curps_en_la_ficha(s), "con la regla apagada la extracción de hoy debe seguir funcionando"


def test_durante_la_espera_una_clave_no_se_copia_a_la_ficha_del_remitente(s):
    """Mutación 15: extraer también en aclarando_*. Mientras el bot espera el «sí» del folio, lo que
    se teclea puede ser de otra persona; si entrara a la ficha, el remitente quedaría con datos que
    no son suyos. La clave es una CURP porque el NSS no entra a la ficha y no mediría nada."""
    _folio(s)
    _conv(s)
    handle_incoming(s, PHONE, CASO)
    assert _estado(s)[0] == "aclarando_folio"

    handle_incoming(s, PHONE, "la curp de mi mamá es " + CURP_AJENA)  # corta, durante la espera

    assert _estado(s)[0] == "aclarando_folio", "la pregunta del folio sigue en pie"
    assert _curps_en_la_ficha(s) == [], "la clave tecleada durante la espera llegó a la ficha"


def test_en_escalado_la_clave_de_un_tercero_anexada_no_va_a_la_ficha_del_remitente(s):
    """Mutación 17: la rama de escalado toma el mensaje con la regla del folio pero deja la
    extracción encendida. Es el camino del cliente que ya espera a un asesor y manda la información
    de otra persona: antes de la regla se anexaba al folio y, además, se copiaba a su ficha."""
    _folio(s, escalado=True)
    _conv(s, "escalado")

    handle_incoming(s, PHONE, CASO + " la CURP de mi mamá es " + CURP_AJENA)
    assert _estado(s)[0] == "aclarando_folio", "la regla del folio tomó el mensaje"
    handle_incoming(s, PHONE, "si")

    assert _curps_en_la_ficha(s) == [], "la clave anexada al folio llegó a la ficha del remitente"
