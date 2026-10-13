"""Botones nativos de Twilio en las preguntas del bot (plantillas quick-reply de la Content API).

Twilio no manda botones con un envío suelto: exige una plantilla `twilio/quick-reply` creada antes
y enviada por `ContentSid`. Solo la encuesta tenía la suya; las preguntas del caso, del folio y de
confirmar salían como texto numerado. Ahora cada una busca su plantilla por los ids de sus botones, y
si no hay plantilla (o falla) sale el texto numerado de siempre.

Si una de estas pruebas se pone roja, el cliente de Twilio ve texto donde debía haber botones, o peor:
toca un botón y el bot recibe un id que no entiende, o a la pregunta del caso le llega la plantilla
de la confirmación.
"""

import json

import pytest

import app.db as db
from app import messaging, twilio_client, webhook
from app.catalog import catalog
from app.config import settings
from app.conversation import (
    CASO_BOTONES,
    CONFIRM_BUTTONS,
    FEEDBACK_OPCIONES,
    FOLIO_BOTONES,
    handle_incoming,
    presentation,
)
from app.models import PROVIDER_META, PROVIDER_TWILIO, Conversation, Ticket

PHONE = "5215540000001"
SID_CASO = "HX00000000000000000000000000000c01"
SID_FOLIO = "HX00000000000000000000000000000f02"
SID_CONFIRMAR = "HX00000000000000000000000000000d03"
SID_ENCUESTA = "HX00000000000000000000000000000e04"

PREGUNTAS = {
    "caso": (CASO_BOTONES, SID_CASO),
    "folio": (FOLIO_BOTONES, SID_FOLIO),
    "confirmar": (CONFIRM_BUTTONS, SID_CONFIRMAR),
}


@pytest.fixture
def plantillas(monkeypatch):
    monkeypatch.setattr(settings, "twilio_content_sid_caso", SID_CASO)
    monkeypatch.setattr(settings, "twilio_content_sid_folio", SID_FOLIO)
    monkeypatch.setattr(settings, "twilio_content_sid_confirmar", SID_CONFIRMAR)


@pytest.fixture
def enviados(monkeypatch):
    registro: list[dict] = []
    monkeypatch.setattr(
        twilio_client, "_post_message", lambda to, data: registro.append(data) or "SMok"
    )
    return registro


# --- cada pregunta busca SU plantilla -----------------------------------------------------------


@pytest.mark.parametrize("nombre", PREGUNTAS)
def test_cada_pregunta_se_manda_con_la_plantilla_de_sus_botones(plantillas, enviados, nombre):
    """Si se pone roja, la pregunta sale como texto o con la plantilla de otra pregunta."""
    botones, sid = PREGUNTAS[nombre]

    twilio_client.send_buttons(PHONE, "¿Pregunta de prueba?", list(botones))

    assert enviados[0]["ContentSid"] == sid, enviados[0]
    assert json.loads(enviados[0]["ContentVariables"]) == {"1": "¿Pregunta de prueba?"}
    assert "Body" not in enviados[0], "con plantilla no se manda además el texto degradado"


def test_las_tres_plantillas_son_distintas_entre_si(plantillas, enviados):
    """Control: si las tres preguntas resolvieran al mismo SID, la prueba anterior no distinguiría nada."""
    for botones, _ in PREGUNTAS.values():
        twilio_client.send_buttons(PHONE, "¿?", list(botones))

    assert len({d["ContentSid"] for d in enviados}) == 3


def test_el_cuerpo_largo_de_varios_servicios_tambien_viaja_en_la_variable(plantillas, enviados):
    """«¿Confirmas estos N trámites?» lleva la lista numerada en el cuerpo; los botones son Sí/No."""
    cuerpo = "¿Confirmas estos 2 trámites?\n1. Acta (por cotizar)\n2. CURP (por cotizar)\nResponde sí o no."

    twilio_client.send_buttons(PHONE, cuerpo, list(CONFIRM_BUTTONS))

    assert json.loads(enviados[0]["ContentVariables"]) == {"1": cuerpo}


# --- respaldo: nunca se queda un cliente sin pregunta -------------------------------------------


@pytest.mark.parametrize("nombre", PREGUNTAS)
def test_sin_credencial_la_pregunta_sale_como_texto_numerado(enviados, nombre):
    """Sin SID configurado (el estado de producción hoy) nada cambia: texto numerado."""
    botones, _ = PREGUNTAS[nombre]

    twilio_client.send_buttons(PHONE, "¿Pregunta?", list(botones))

    assert "ContentSid" not in enviados[0]
    assert "1) " in enviados[0]["Body"] and "2) " in enviados[0]["Body"]


def test_solo_la_pregunta_con_plantilla_sale_con_botones(monkeypatch, enviados):
    """Las plantillas son independientes: configurar la del caso no obliga a tener las otras."""
    monkeypatch.setattr(settings, "twilio_content_sid_caso", SID_CASO)

    twilio_client.send_buttons(PHONE, "¿Caso?", list(CASO_BOTONES))
    twilio_client.send_buttons(PHONE, "¿Folio?", list(FOLIO_BOTONES))

    assert enviados[0]["ContentSid"] == SID_CASO
    assert "ContentSid" not in enviados[1] and "1) " in enviados[1]["Body"]


@pytest.mark.parametrize("nombre", PREGUNTAS)
def test_si_la_plantilla_falla_el_cliente_recibe_el_texto_numerado(plantillas, monkeypatch, nombre):
    """SID equivocado o plantilla borrada: vale más una pregunta numerada que ninguna."""
    intentos: list[dict] = []

    def _post(to, data):
        intentos.append(data)
        return None if "ContentSid" in data else "SMtexto"

    monkeypatch.setattr(twilio_client, "_post_message", _post)
    botones, _ = PREGUNTAS[nombre]

    sid = twilio_client.send_buttons(PHONE, "¿Pregunta?", list(botones))

    assert sid == "SMtexto"
    assert "ContentSid" in intentos[0] and "ContentSid" not in intentos[1]
    assert "1) " in intentos[1]["Body"]


# --- lo que NO debe cambiar ---------------------------------------------------------------------


def test_la_encuesta_conserva_su_plantilla_explicita(plantillas, enviados):
    """Su SID llega por `content_sid=`; ninguna de las tres nuevas le gana ni se la quita."""
    twilio_client.send_buttons(PHONE, "¿Te sirvió?", FEEDBACK_OPCIONES, content_sid=SID_ENCUESTA)

    assert enviados[0]["ContentSid"] == SID_ENCUESTA


def test_la_encuesta_sin_plantilla_no_toma_la_de_otra_pregunta(plantillas, enviados):
    """Ids «1»/«2»: no hay plantilla para ese par, así que sale numerada, no con la de confirmar."""
    twilio_client.send_buttons(PHONE, "¿Te sirvió?", FEEDBACK_OPCIONES)

    assert "ContentSid" not in enviados[0]


def test_un_sid_explicito_gana_sobre_el_de_la_pregunta(plantillas, enviados):
    twilio_client.send_buttons(PHONE, "¿?", list(CASO_BOTONES), content_sid=SID_ENCUESTA)

    assert enviados[0]["ContentSid"] == SID_ENCUESTA


def test_meta_no_recibe_plantillas_de_twilio(plantillas, monkeypatch):
    """Meta manda botones nativos con su propio formato: ningún ContentSid cruza."""
    from app import whatsapp_client

    capturado: dict = {}
    monkeypatch.setattr(
        whatsapp_client, "_send", lambda to, payload: capturado.update(payload) or "wamid.1"
    )

    whatsapp_client.send_buttons(PHONE, "¿?", list(CASO_BOTONES))

    assert "ContentSid" not in json.dumps(capturado)
    assert [b["reply"]["id"] for b in capturado["interactive"]["action"]["buttons"]] == [
        i for i, _ in CASO_BOTONES
    ]


# --- deriva: la plantilla es la del código -------------------------------------------------------


def test_cada_conjunto_de_botones_del_bot_tiene_su_credencial(plantillas):
    """Si alguien cambia los ids de un botón y no el mapa, la pregunta pierde su plantilla sin avisar."""
    for botones, sid in PREGUNTAS.values():
        ids = tuple(i for i, _ in botones)
        assert twilio_client._sid_de_plantilla(ids) == sid, f"los botones {ids} no tienen plantilla"
    assert twilio_client._sid_de_plantilla(tuple(i for i, _ in FEEDBACK_OPCIONES)) is None


@pytest.mark.parametrize("nombre", PREGUNTAS)
def test_la_definicion_de_la_plantilla_sale_de_las_constantes_del_bot(nombre):
    """Lo que se sube a Twilio es lo que el bot espera: mismos ids, mismos títulos, cuerpo = {{1}}."""
    botones, _ = PREGUNTAS[nombre]

    d = twilio_client.definicion_plantilla(nombre, list(botones))

    qr = d["types"]["twilio/quick-reply"]
    assert qr["body"] == "{{1}}" and d["variables"] == {"1": "Pregunta de ejemplo"}
    assert [(a["id"], a["title"]) for a in qr["actions"]] == list(botones)
    assert d["types"]["twilio/text"]["body"] == "{{1}}", "RCS/SMS necesitan el respaldo de texto"
    assert d["language"] == "es_MX" and d["friendly_name"].startswith("tramites_")


@pytest.mark.parametrize("nombre", PREGUNTAS)
def test_los_botones_caben_en_los_limites_de_twilio(nombre):
    """Título ≤ 20, id ≤ 200 y ≤ 3 botones sin aprobación (documentación de twilio/quick-reply)."""
    botones, _ = PREGUNTAS[nombre]
    assert len(botones) <= 3
    for opcion_id, titulo in botones:
        assert len(titulo) <= 20, titulo
        assert len(opcion_id) <= 200


# --- de punta a punta ---------------------------------------------------------------------------


@pytest.fixture
def twilio_listo(monkeypatch):
    monkeypatch.setattr(settings, "twilio_account_sid", "ACtest")
    monkeypatch.setattr(settings, "twilio_auth_token", "tok")
    monkeypatch.setattr(settings, "twilio_whatsapp_from", "+15550000000")


def test_la_pregunta_real_del_caso_sale_con_su_plantilla_por_el_canal_de_twilio(
    session, monkeypatch, request, plantillas, twilio_listo, enviados
):
    """Parte de lo que `presentation` EMITE y lo entrega por `webhook._deliver` como lo hace el bot."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    request.getfixturevalue("catalogo_vivo")
    monkeypatch.setattr(settings, "contexto_folio_enabled", True)
    monkeypatch.setattr(settings, "revision_enabled", True)
    session.add(Conversation(phone=PHONE, state="menu_categoria", context_json="{}"))
    session.commit()
    replies = handle_incoming(session, PHONE, "tengo una situación con mi pensión, qué se puede hacer")
    item = presentation(session, PHONE, replies)[-1]
    assert isinstance(item, dict) and item["buttons"] == list(CASO_BOTONES)

    wamid = webhook._deliver(PHONE, item, PROVIDER_TWILIO)

    assert wamid == "SMok"
    assert enviados[0]["ContentSid"] == SID_CASO
    assert json.loads(enviados[0]["ContentVariables"]) == {"1": item["text"]}


def test_el_toque_del_boton_llega_como_el_id_que_el_bot_entiende(session, monkeypatch, request):
    """Twilio devuelve el `id` de la plantilla en ButtonPayload y el título en Body: gana el id."""
    form = {"From": "whatsapp:+5215540000001", "To": "whatsapp:+15550000000",
            "Body": "🔎 Revisión", "ButtonPayload": "revision", "MessageSid": "SM1"}

    mensaje = twilio_client.parse_inbound(form)

    assert mensaje["text"] == "revision"
