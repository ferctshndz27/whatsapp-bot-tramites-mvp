"""Botones numerados en Twilio: «1) Revisión / 2) Un trámite» (operador, 2026-10-12).

Twilio no tiene botones nativos: `twilio_client._numerar` degrada los botones a texto. Antes
mostraba el id interno («revision) 🔎 Revisión») y el bot solo entendía la palabra. Ahora se numera
por posición y el bot entiende el número O el nombre, en cada pregunta con botones.

La prueba central parte de lo que `presentation` EMITE de verdad, no de listas copiadas: para cada
pregunta con botones toma su texto Twilio, lee los renglones «N) título» y comprueba, con dos
teléfonos gemelos, que teclear N deja la conversación exactamente donde la deja tocar el botón.

Si una se pone roja, un cliente de Twilio ve una opción numerada, la teclea y el bot no la entiende
(o la entiende como otra cosa): repregunta, o abre un folio que no pidió.
"""

import json
import re

import pytest

import app.db as db
from app import twilio_client, whatsapp_client
from app.catalog import catalog
from app.config import settings
from app.conversation import (
    CASO_BOTONES,
    CASO_PREGUNTA_BOTONES,
    CONFIRM_BUTTONS,
    FEEDBACK_OPCIONES,
    FEEDBACK_PROMPT,
    FOLIO_BOTONES,
    handle_incoming,
    presentation,
)
from app.models import Conversation, Ticket

A = "5215540000001"
B = "5215540000002"
SITUACION = "tengo una situación con mi pensión, qué se puede hacer"
CASO_LARGO = (
    "Buenas tardes, ya tengo lo de la CURP, también revisé mis semanas cotizadas y el NSS en PDF "
    "porque me dijeron en la ventanilla que los necesitaba, le mando la información para que la "
    "revise y me diga si falta algo más de mi parte para continuar con el trámite que tenemos abierto"
)
RENGLON = re.compile(r"^(\d+)\) (.+)$", re.MULTILINE)


@pytest.fixture
def s(session, monkeypatch, request):
    monkeypatch.setattr(db, "engine", session.get_bind())
    request.getfixturevalue("catalogo_vivo")
    monkeypatch.setattr(settings, "contexto_folio_enabled", True)
    monkeypatch.setattr(settings, "revision_enabled", True)
    monkeypatch.setattr(settings, "contexto_min_palabras", 40)
    return session


def _conv(s, phone, estado="menu_categoria"):
    s.add(Conversation(phone=phone, state=estado, context_json=json.dumps({})))
    s.commit()


def _folio(s, phone):
    servicio = catalog.service_by_id("imss-unificacion-nss")
    s.add(Ticket(phone=phone, category=servicio.category_name, service_id=servicio.id,
                 service_name=servicio.name, price_info=servicio.price_info))
    s.commit()


def _caso(s, phone):
    _conv(s, phone)
    return presentation(s, phone, handle_incoming(s, phone, SITUACION))


def _folio_unico(s, phone):
    _folio(s, phone)
    _conv(s, phone)
    return presentation(s, phone, handle_incoming(s, phone, CASO_LARGO))


def _confirmar_uno(s, phone):
    _conv(s, phone)
    return presentation(s, phone, handle_incoming(s, phone, "quiero mi acta de nacimiento"))


def _confirmar_varios(s, phone):
    _conv(s, phone)
    return presentation(s, phone, handle_incoming(s, phone, "quiero acta y curp"))


def _responde(s, phone, texto):
    replies = handle_incoming(s, phone, texto)
    s.expire_all()
    return [re.sub(r"#\d+", "#N", r) for r in replies], s.get(Conversation, phone).state


# Pregunta → (cómo llegar a ella, estado esperado). Las que salen numeradas en Twilio.
PREGUNTAS = {
    "caso sin servicios": (_caso, "aclarando_caso", CASO_BOTONES),
    "folio con un solo folio": (_folio_unico, "aclarando_folio", FOLIO_BOTONES),
    "confirmar un servicio": (_confirmar_uno, "confirmar", CONFIRM_BUTTONS),
}


@pytest.mark.parametrize("nombre", PREGUNTAS)
def test_cada_pregunta_con_botones_sale_numerada_en_twilio(s, nombre):
    """Lo que el cliente de Twilio lee: «1) …», «2) …», no el id interno. Si se pone roja, vuelve a
    verse «revision) 🔎 Revisión»."""
    llegar, estado, botones = PREGUNTAS[nombre]
    salida = llegar(s, A)[-1]

    assert isinstance(salida, dict) and salida["buttons"] == list(botones), salida
    texto = twilio_client._numerar(salida["text"], salida["buttons"])
    renglones = RENGLON.findall(texto)

    assert [n for n, _ in renglones] == [str(i) for i in range(1, len(botones) + 1)], texto
    assert [t for _, t in renglones] == [t for _, t in botones]
    for opcion_id, _ in botones:
        if not opcion_id.isdigit():
            assert f"{opcion_id})" not in texto, f"se filtró el id interno «{opcion_id}»"


@pytest.mark.parametrize("posicion", [0, 1])
@pytest.mark.parametrize("nombre", PREGUNTAS)
def test_teclear_el_numero_es_lo_mismo_que_tocar_el_boton(s, nombre, posicion):
    """El invariante: el número que Twilio muestra lleva la conversación al mismo lugar que el id del
    botón. Dos teléfonos gemelos; uno teclea el número, el otro el id. Si se pone roja, el cliente
    teclea «1» y el bot repregunta o entiende otra cosa."""
    llegar, estado, botones = PREGUNTAS[nombre]
    llegar(s, A)
    llegar(s, B)

    por_numero = _responde(s, A, str(posicion + 1))
    por_id = _responde(s, B, botones[posicion][0])

    assert por_numero == por_id, f"«{posicion + 1}» no equivale al botón «{botones[posicion][1]}»"
    assert por_id[1] != estado, "el botón no hizo nada"


@pytest.mark.parametrize("nombre", PREGUNTAS)
def test_las_dos_opciones_llevan_a_lugares_distintos(s, nombre):
    """Control del invariante: si ambas opciones dieran lo mismo, el gemelo sería una igualdad vacía."""
    llegar, _, botones = PREGUNTAS[nombre]
    llegar(s, A)
    llegar(s, B)

    assert _responde(s, A, botones[0][0]) != _responde(s, B, botones[1][0])


@pytest.mark.parametrize("nombre,palabra,posicion", [
    ("caso sin servicios", "revisión", 0), ("caso sin servicios", "revision", 0),
    ("caso sin servicios", "trámite", 1), ("caso sin servicios", "tramite", 1),
    ("caso sin servicios", "un trámite", 1),
    ("folio con un solo folio", "sí", 0), ("folio con un solo folio", "nuevo", 1),
    ("confirmar un servicio", "sí", 0), ("confirmar un servicio", "no", 1),
])
def test_el_nombre_de_la_opcion_sigue_entendiendose(s, nombre, palabra, posicion):
    """El operador pidió número O nombre: la palabra no se pierde al agregar el número."""
    llegar, _, botones = PREGUNTAS[nombre]
    llegar(s, A)
    llegar(s, B)

    assert _responde(s, A, palabra) == _responde(s, B, botones[posicion][0])


# --- lo que NO debe cambiar ---------------------------------------------------------------------


def test_un_numero_fuera_de_las_opciones_no_elige_nada(s):
    """«3» ante dos opciones repregunta: no se inventa una tercera."""
    _caso(s, A)
    _, estado = _responde(s, A, "3")
    assert estado == "aclarando_caso"

    # Confirmar tiene tres botones desde 2026-10-13 (el 3 es «Cancelar»): la tercera opción ya existe.
    _confirmar_uno(s, B)
    _, estado = _responde(s, B, "4")
    assert estado == "confirmar"


def test_con_varios_servicios_los_digitos_siguen_eligiendo_servicios(s):
    """«¿Confirmas estos 2 trámites?» ya numera sus servicios (B1): ahí «1» es el servicio 1, y los
    botones Sí/No NO se vuelven a numerar, o habría dos «1)»."""
    salida = _confirmar_varios(s, A)[-1]
    assert "1. " in salida["text"], salida["text"]

    texto = twilio_client._numerar(salida["text"], salida["buttons"])
    assert not RENGLON.search(texto.split(salida["text"])[-1]), f"dos numeraciones juntas:\n{texto}"
    assert "si)" not in texto and "no)" not in texto, "se filtró el id interno"
    assert "• ✅ Sí" in texto and "• ❌ No" in texto

    replies, estado = _responde(s, A, "1")
    assert estado == "confirmar" and "Va, lo dejamos así" in replies[0], replies


def test_un_digito_suelto_en_el_menu_sigue_siendo_la_categoria(s):
    """Sin pregunta de botones pendiente, «1» es la categoría 1: ningún dígito se vuelve global."""
    _conv(s, A)
    replies, estado = _responde(s, A, "1")
    assert estado == "menu_servicio", (estado, replies)


def test_meta_conserva_los_ids_de_los_botones():
    """Meta no cambia: el toque manda el id (`revision`, `si`…), no un número."""
    for botones in (CASO_BOTONES, FOLIO_BOTONES, CONFIRM_BUTTONS):
        payload = whatsapp_client._buttons_payload(A, "¿?", list(botones))
        enviados = [b["reply"]["id"] for b in payload["interactive"]["action"]["buttons"]]
        assert enviados == [i for i, _ in botones]


def test_la_encuesta_sigue_numerada_como_siempre():
    texto = twilio_client._numerar(FEEDBACK_PROMPT, FEEDBACK_OPCIONES)
    assert [n for n, _ in RENGLON.findall(texto)] == ["1", "2"]


# --- `_numerar` directo -------------------------------------------------------------------------


def test_numerar_pone_el_numero_de_posicion_y_no_el_id():
    texto = twilio_client._numerar(CASO_PREGUNTA_BOTONES, list(CASO_BOTONES))

    assert "1) 🔎 Revisión" in texto and "2) 📋 Un trámite" in texto
    assert "revision)" not in texto and "tramite)" not in texto


def test_numerar_no_vuelve_a_numerar_un_cuerpo_que_ya_trae_su_lista():
    cuerpo = "¿Confirmas estos 2 trámites?\n1. Acta\n2. CURP\nResponde sí o no."
    texto = twilio_client._numerar(cuerpo, [("si", "✅ Sí"), ("no", "❌ No")])

    assert "• ✅ Sí" in texto and "• ❌ No" in texto
    assert "si)" not in texto and "no)" not in texto and "1) ✅ Sí" not in texto


def test_numerar_respeta_el_tope_de_opciones():
    opciones = [(f"o{i}", f"Opción {i}") for i in range(1, 15)]
    texto = twilio_client._numerar("Elige:", opciones)

    assert "10) Opción 10" in texto and "11) Opción 11" not in texto
