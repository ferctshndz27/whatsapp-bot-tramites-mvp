"""El formato de caso: «qué pasó / servicio / ¿requiere investigar? / datos» (operador, 2026-10-13).

El cliente copia un mensaje con cuatro campos y lo llena; el bot lo lee sin adivinar:
- investigar «Sí» → abre el caso para análisis directo;
- «No» → el servicio y los datos siguen el camino de un trámite;
- en blanco o «no sé» → la pregunta de siempre (Revisión o trámite).
Los archivos del cliente no se pierden: los de antes, los del pie de foto y los de después.

Si una de estas pruebas se pone roja: el cliente llena el formato y el bot lo trata como texto libre
(pregunta de más, nombra un trámite que no pidió) o pierde los archivos que mandó.
"""

import json

import pytest
from sqlmodel import select

import app.db as db
from app import formato_caso
from app.config import settings
from app.conversation import CASO_SERVICE_ID, handle_incoming
from app.formato_caso import FORMATO_MENSAJE, FORMATO_VACIO, leer
from app.models import Conversation, Message, Ticket

PHONE = "5215577770001"
HISTORIA = "Tengo dos NSS, uno de una fábrica y otro del empleo actual. No me aparecen todas mis semanas."
DATOS = "CURP HEGM860512MDFRRR09, NSS 12345678901 y 10987654321"


def _formato(historia=HISTORIA, servicio="unificación de NSS", investigar="Sí", datos=DATOS) -> str:
    return (
        f"📖 Qué pasó: {historia}\n"
        f"📋 Servicio que necesito: {servicio}\n"
        f"🔎 ¿Requiere investigar? (Sí / No): {investigar}\n"
        f"🪪 Datos que tengo (CURP, NSS, RFC): {datos}"
    )


@pytest.fixture
def s(session, monkeypatch):
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(settings, "contexto_folio_enabled", True)
    monkeypatch.setattr(settings, "revision_enabled", True)
    monkeypatch.setattr(settings, "contexto_min_palabras", 40)
    session.add(Conversation(phone=PHONE, state="menu_categoria", context_json=json.dumps({})))
    session.commit()
    return session


def _pdf(n: int) -> dict:
    return {"ref": f"media-{n}", "mime": "application/pdf", "nombre": f"doc-{n}.pdf", "caption": ""}


def _estado(s) -> str:
    s.expire_all()
    return s.get(Conversation, PHONE).state


def _folios(s) -> list[Ticket]:
    s.expire_all()
    return list(s.exec(select(Ticket).where(Ticket.phone == PHONE)).all())


def _mensaje(s, ref: str) -> Message:
    s.expire_all()
    return s.exec(select(Message).where(Message.phone == PHONE, Message.media_ref == ref)).one()


# ---- el lector ------------------------------------------------------------------------------


def test_lee_los_cuatro_campos():
    f = leer(_formato())
    assert (f.historia, f.servicio, f.investigar, f.datos) == (HISTORIA, "unificación de NSS", True, DATOS)


def test_el_valor_puede_ir_en_la_linea_de_abajo_y_sin_emojis():
    f = leer("Qué pasó:\nme cancelaron la CURP\n\nServicio que necesito:\nCURP\nRequiere investigar: no")
    assert f.historia == "me cancelaron la CURP" and f.servicio == "CURP" and f.investigar is False


def test_sin_acentos_ni_mayusculas():
    f = leer("QUE PASO: algo\nSERVICIO QUE NECESITO: acta de nacimiento\nREQUIERE INVESTIGAR: si")
    assert f.servicio == "acta de nacimiento" and f.investigar is True


@pytest.mark.parametrize("valor,esperado", [
    ("Sí", True), ("si", True), ("SI", True), ("sí, por favor", True), ("claro", True),
    ("No", False), ("no", False), ("no, solo el trámite", False),
    ("no sé", None), ("no se", None), ("", None), ("?", None), ("tal vez", None),
])
def test_investigar_solo_es_si_o_no_cuando_se_entiende(valor, esperado):
    f = leer(f"Qué pasó: algo\nServicio que necesito: curp\n¿Requiere investigar?: {valor}")
    assert f.investigar is esperado


def test_una_etiqueta_sola_o_dentro_de_una_frase_no_es_el_formato():
    assert leer("Servicio que necesito: acta de nacimiento") is None
    assert leer("hola, el servicio que necesito: un acta, y mis datos: curp") is None
    assert leer("quiero mi acta de nacimiento") is None


def test_el_formato_sin_llenar_se_lee_como_vacio():
    f = leer(FORMATO_MENSAJE)
    assert f is not None and f.vacio and f.investigar is None and f.datos is None


def test_el_texto_corrido_lleva_historia_servicio_y_datos():
    t = leer(_formato()).texto()
    assert HISTORIA in t and "unificación de NSS" in t and DATOS in t


def test_el_mensaje_del_formato_es_seguro_de_copiar():
    """Lo copia el cliente tal cual: sin palabras que disparan escalada o atajos del catálogo."""
    bajo = FORMATO_MENSAJE.lower()
    assert "asesor" not in bajo and "servicios" not in bajo and "menu" not in bajo
    assert len(FORMATO_MENSAJE) <= 1024
    for etiqueta in ("Qué pasó:", "Servicio que necesito:", "¿Requiere investigar?", "Datos que tengo"):
        assert etiqueta in FORMATO_MENSAJE


# ---- investigar «Sí»: abre el caso -----------------------------------------------------------


def test_si_abre_el_caso_sin_la_pregunta_de_revision(s):
    replies = handle_incoming(s, PHONE, _formato())

    folios = _folios(s)
    assert [t.service_id for t in folios] == [CASO_SERVICE_ID], replies
    assert "recibí tu caso" in "\n".join(replies).lower()
    assert "revisión o" not in "\n".join(replies).lower() and _estado(s) == "escalado"
    notas = folios[0].notes
    assert HISTORIA in notas and "unificación de NSS" in notas and DATOS in notas


def test_si_no_abre_un_tramite_aparte_aunque_la_historia_nombre_uno(s):
    handle_incoming(s, PHONE, _formato(historia="No me salen mis semanas cotizadas ni mi acta de nacimiento"))
    assert [t.service_id for t in _folios(s)] == [CASO_SERVICE_ID]


# ---- archivos --------------------------------------------------------------------------------


def test_los_archivos_de_antes_quedan_en_el_folio_del_caso(s):
    handle_incoming(s, PHONE, "", is_media=True, adjunto=_pdf(1))
    handle_incoming(s, PHONE, "", is_media=True, adjunto=_pdf(2))

    handle_incoming(s, PHONE, _formato())

    folio = _folios(s)[0]
    assert _mensaje(s, "media-1").ticket_id == folio.id and _mensaje(s, "media-2").ticket_id == folio.id


def test_el_formato_en_el_pie_de_la_foto_abre_el_caso_y_liga_la_foto(s):
    replies = handle_incoming(
        s, PHONE, _formato(), is_media=True,
        adjunto={**_pdf(7), "caption": _formato()},
    )

    folios = _folios(s)
    assert [t.service_id for t in folios] == [CASO_SERVICE_ID], replies
    assert _mensaje(s, "media-7").ticket_id == folios[0].id
    assert HISTORIA in folios[0].notes


def test_un_pie_de_foto_comun_no_se_lee_como_formato(s):
    """Control: una foto con «mi INE» se acusa y nada más."""
    replies = handle_incoming(s, PHONE, "mi INE", is_media=True, adjunto={**_pdf(8), "caption": "mi INE"})
    assert _folios(s) == [] and _estado(s) == "menu_categoria" and replies


def test_el_archivo_de_despues_en_el_camino_de_tramite_queda_en_el_folio(s):
    handle_incoming(s, PHONE, _formato(servicio="acta de nacimiento", investigar="No", historia="Es para mi hijo"))
    assert _estado(s) == "confirmar"
    handle_incoming(s, PHONE, "", is_media=True, adjunto=_pdf(9))

    handle_incoming(s, PHONE, "sí")

    folios = _folios(s)
    assert len(folios) == 1 and folios[0].service_id != CASO_SERVICE_ID
    assert _mensaje(s, "media-9").ticket_id == folios[0].id


def test_el_archivo_de_despues_con_el_caso_abierto_sigue_la_regla_del_folio(s):
    handle_incoming(s, PHONE, _formato())
    replies = handle_incoming(s, PHONE, "", is_media=True, adjunto=_pdf(10))
    assert replies, "el archivo no se puede quedar sin acuse"
    assert len(_folios(s)) == 1


# ---- investigar «No», en blanco, sin llenar --------------------------------------------------


def test_no_sigue_el_camino_de_un_tramite(s):
    replies = handle_incoming(s, PHONE, _formato(servicio="acta de nacimiento", investigar="No", datos="CURP HEGM860512MDFRRR09"))

    assert _estado(s) == "confirmar" and _folios(s) == []
    assert "Acta de nacimiento" in "\n".join(replies)


def test_no_con_servicio_no_se_ofrece_el_menu(s):
    replies = handle_incoming(s, PHONE, _formato(servicio="no sé", investigar="No"))
    assert _folios(s) == [] and _estado(s) == "menu_categoria"
    assert "Claro." in "\n".join(replies) or "categor" in "\n".join(replies).lower() or "1." in "\n".join(replies)


@pytest.mark.parametrize("investigar", ["", "no sé"])
def test_en_blanco_o_no_se_hace_la_pregunta_de_siempre(s, investigar):
    handle_incoming(s, PHONE, _formato(investigar=investigar))
    assert _estado(s) == "aclarando_caso" and _folios(s) == []


def test_la_pregunta_solo_ofrece_el_servicio_del_formato(s):
    """«CURP» en los datos o «semanas cotizadas» en la historia no son opciones del cliente."""
    handle_incoming(s, PHONE, _formato(investigar="", historia="No me salen mis semanas cotizadas"))
    s.expire_all()
    ids = json.loads(s.get(Conversation, PHONE).context_json)["servicios"]
    assert ids == ["imss-unificacion-nss"], ids


def test_los_codigos_de_los_datos_viajan_al_tramite_sin_volverse_otro_tramite(s):
    handle_incoming(s, PHONE, _formato(servicio="acta de nacimiento", investigar="No"))
    s.expire_all()
    contexto = json.loads(s.get(Conversation, PHONE).context_json)
    assert contexto["pending_service_ids"] == ["sb-acta-nacimiento"], contexto
    assert contexto["codigos"]["CURP"] == ["HEGM860512MDFRRR09"]


def test_el_formato_sin_llenar_se_avisa_y_no_abre_nada(s):
    replies = handle_incoming(s, PHONE, FORMATO_MENSAJE)
    assert replies == [FORMATO_VACIO] and _folios(s) == [] and _estado(s) == "menu_categoria"


@pytest.mark.parametrize("comando", ["formato", "Formato", "ayuda"])
def test_el_comando_manda_el_formato_y_no_mueve_el_estado(s, comando):
    assert handle_incoming(s, PHONE, comando) == [FORMATO_MENSAJE]
    assert _estado(s) == "menu_categoria"


# ---- cuándo NO aplica ------------------------------------------------------------------------


def test_con_un_folio_abierto_manda_la_regla_del_folio(s):
    handle_incoming(s, PHONE, "quiero mi acta de nacimiento")
    handle_incoming(s, PHONE, "sí")
    antes = len(_folios(s))

    handle_incoming(s, PHONE, _formato())

    assert len(_folios(s)) == antes, "no se abre un caso encima de un folio sin preguntar"
    assert _estado(s) == "aclarando_folio"


def test_el_formato_en_el_pie_de_foto_con_un_folio_abierto_no_abre_un_caso(s):
    handle_incoming(s, PHONE, "quiero mi acta de nacimiento")
    handle_incoming(s, PHONE, "sí")
    conv = s.get(Conversation, PHONE)  # el cliente dejó de contestar requisitos: vuelve al menú con el folio abierto
    conv.state = "menu_categoria"
    s.add(conv)
    s.commit()
    antes = len(_folios(s))
    assert antes == 1

    handle_incoming(s, PHONE, _formato(), is_media=True, adjunto={**_pdf(11), "caption": _formato()})

    assert len(_folios(s)) == antes and all(t.service_id != CASO_SERVICE_ID for t in _folios(s))


def test_un_cliente_en_escalado_sin_folios_abiertos_puede_usar_el_formato(s):
    """Su folio ya se cerró y la conversación se quedó en «escalado»: el formato no la deja atorada."""
    conv = s.get(Conversation, PHONE)
    conv.state = "escalado"
    s.add(conv)
    s.commit()

    handle_incoming(s, PHONE, _formato(servicio="acta de nacimiento", investigar="No"))

    assert _estado(s) == "confirmar"


def test_con_la_revision_apagada_no_se_lee_el_formato(s, monkeypatch):
    monkeypatch.setattr(settings, "revision_enabled", False)
    handle_incoming(s, PHONE, _formato())
    assert all(t.service_id != CASO_SERVICE_ID for t in _folios(s))


def test_el_texto_libre_sigue_igual(s):
    replies = handle_incoming(s, PHONE, "quiero mi acta de nacimiento")
    assert _estado(s) == "confirmar" and "Acta de nacimiento" in "\n".join(replies)


def test_el_modulo_expone_lo_que_el_bot_usa():
    assert formato_caso.COMANDOS == {"formato", "ayuda"}
