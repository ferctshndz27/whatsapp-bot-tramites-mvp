"""Segunda cuenta de WhatsApp (Twilio) — Fase 3: entrada.

REGLA DE NEGOCIO: dos cuentas independientes que comparten este CRM. Twilio
habla otro idioma en TODO —teléfonos con prefijo, form-urlencoded en vez de
JSON, firma HMAC-SHA1 sobre la URL, otro vocabulario de estados— y ese idioma no
puede filtrarse más allá del adaptador.

No hace falta cuenta de Twilio para verificar nada de esto: el algoritmo de
firma es determinista, así que las pruebas generan una firma válida con un
AuthToken sintético y comprueban que el validador la acepte y que rechace
cualquier alteración.
"""

import base64
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

import app.db as db
from app import twilio_client
from app.config import settings
from app.main import app
from app.models import (
    OFICINA_TLAXCALA_ID,
    PROVIDER_META,
    PROVIDER_TWILIO,
    Client,
    Conversation,
    Direction,
    Message,
)

TOKEN = "token-de-prueba"
BASE = "https://tramites.example.mx"
RUTA = "/webhook/twilio"
RUTA_ESTADO = "/webhook/twilio/status"

CRUDO_TWILIO = "whatsapp:+5215500000000"
# El número de LA OFICINA (el `To` del form). Twilio siempre lo manda; desde el
# PR 4 es lo que resuelve la oficina, y sin él el fail-closed de §4.2 descarta.
TO_OFICINA = "whatsapp:+5215599999999"
CANONICO = "525500000000"
SID = "SM00000000000000000000000000000001"


def _firmar(url: str, params: dict[str, str]) -> str:
    """La firma tal como la calcula Twilio: URL completa + params ordenados,
    concatenados sin separador, HMAC-SHA1 con el AuthToken, en base64."""
    firmado = url + "".join(clave + params[clave] for clave in sorted(params))
    return base64.b64encode(
        hmac.new(TOKEN.encode(), firmado.encode("utf-8"), hashlib.sha1).digest()
    ).decode()


@pytest.fixture(autouse=True)
def _entorno(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "twilio_account_sid", "ACtest")
    monkeypatch.setattr(settings, "twilio_auth_token", TOKEN)
    monkeypatch.setattr(settings, "twilio_whatsapp_from", TO_OFICINA)
    monkeypatch.setattr(settings, "public_base_url", BASE)
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "geoip_enabled", False)
    # Cortafuegos de red: ninguna prueba puede llamar a Twilio de verdad.
    # Cada envío devuelve un sid DISTINTO, como el proveedor real: repetirlo
    # chocaría contra el índice único de wa_message_id y ensuciaría el log con
    # un IntegrityError que no tiene que ver con lo que se está probando.
    contador = iter(range(1000))
    monkeypatch.setattr(
        twilio_client, "_post_message", lambda *a, **k: f"SM{next(contador):032d}"
    )
    yield


def _post(http, ruta: str, form: dict[str, str], firma: str | None = None):
    return http.post(
        ruta,
        data=form,
        headers={"X-Twilio-Signature": firma if firma is not None else _firmar(BASE + ruta, form)},
    )


# ---------------------------------------------------------------------------
# Firma
# ---------------------------------------------------------------------------


def test_una_firma_valida_se_acepta():
    form = {"From": CRUDO_TWILIO, "To": TO_OFICINA, "Body": "hola", "MessageSid": SID}
    url = BASE + RUTA

    assert twilio_client.verify_signature(url, form, _firmar(url, form)) is True


@pytest.mark.parametrize(
    "mutacion",
    [
        {"Body": "otro texto"},  # cambiaron el mensaje
        {"From": "whatsapp:+5215511112222"},  # suplantaron al remitente
        {"Extra": "campo inyectado"},  # agregaron un parámetro
    ],
)
def test_alterar_cualquier_parametro_invalida_la_firma(mutacion):
    # REGLA: la firma cubre TODOS los parámetros. Si solo cubriera algunos, se
    # podría cambiar el remitente y hacer avanzar la conversación de otra
    # persona con un payload firmado legítimamente.
    form = {"From": CRUDO_TWILIO, "To": TO_OFICINA, "Body": "hola", "MessageSid": SID}
    url = BASE + RUTA
    firma = _firmar(url, form)

    assert twilio_client.verify_signature(url, {**form, **mutacion}, firma) is False


def test_la_url_es_parte_de_lo_firmado():
    # REGLA: por esto PUBLIC_BASE_URL no es opcional. Detrás del proxy la app ve
    # el host interno y http://; si se firmara con esa URL fallarían TODAS las
    # firmas — no algunas, lo que lo haría parecer un problema intermitente.
    form = {"From": CRUDO_TWILIO, "To": TO_OFICINA, "Body": "hola"}
    firma = _firmar(BASE + RUTA, form)

    assert twilio_client.verify_signature("http://interno:8000" + RUTA, form, firma) is False


def test_sin_auth_token_no_se_acepta_nada(monkeypatch):
    # REGLA: sin token no hay forma de verificar. Aceptar "porque no está
    # configurado" dejaría el bot abierto a que cualquiera inyecte mensajes.
    monkeypatch.setattr(settings, "twilio_auth_token", "")
    form = {"From": CRUDO_TWILIO, "To": TO_OFICINA, "Body": "hola"}

    assert twilio_client.verify_signature(BASE + RUTA, form, _firmar(BASE + RUTA, form)) is False


# ---------------------------------------------------------------------------
# Parseo
# ---------------------------------------------------------------------------


def test_el_telefono_se_normaliza_al_canonico():
    # REGLA: el formato de Twilio no entra a la base. Si entrara, el mismo
    # humano sería dos clientes distintos (la PK de `client` es el teléfono).
    datos = twilio_client.parse_inbound({"From": CRUDO_TWILIO, "To": TO_OFICINA, "Body": "hola"})

    assert datos["phone"] == CANONICO


def test_una_respuesta_de_lista_llega_como_su_id_no_como_su_titulo():
    # REGLA: el id de cada opción ES el texto que el cliente habría tecleado
    # ("1"). Tomar el Body ("Servicios básicos") rompería la máquina de estados,
    # que espera el número.
    datos = twilio_client.parse_inbound(
        {"From": CRUDO_TWILIO, "To": TO_OFICINA, "Body": "Servicios básicos", "ListId": "1"}
    )

    assert datos["text"] == "1"


def test_un_boton_llega_como_su_payload():
    datos = twilio_client.parse_inbound(
        {"From": CRUDO_TWILIO, "To": TO_OFICINA, "Body": "Sí, confirmo", "ButtonPayload": "si"}
    )

    assert datos["text"] == "si"


def test_un_adjunto_se_marca_como_media():
    datos = twilio_client.parse_inbound(
        {"From": CRUDO_TWILIO, "NumMedia": "1", "MediaUrl0": "https://api.twilio.com/x"}
    )

    assert datos["is_media"] is True


def test_sin_remitente_no_hay_nada_que_procesar():
    assert twilio_client.parse_inbound({"Body": "hola"}) is None


# ---------------------------------------------------------------------------
# Estados de entrega
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "twilio_estado,esperado",
    [
        ("queued", "sent"),
        ("sent", "sent"),
        ("delivered", "delivered"),
        ("read", "read"),
        ("failed", "failed"),
        ("undelivered", "failed"),  # Meta no tiene "undelivered"
    ],
)
def test_los_estados_se_traducen_al_vocabulario_de_la_bandeja(twilio_estado, esperado):
    # REGLA: la bandeja muestra un solo vocabulario. Guardar el crudo de Twilio
    # haría que "undelivered" no pinte como fallo y el asesor creyera que el
    # mensaje llegó.
    assert twilio_client.map_status(twilio_estado) == esperado


def test_un_estado_desconocido_no_se_inventa():
    # REGLA: mejor no mostrar nada que mostrar algo falso sobre una entrega.
    assert twilio_client.map_status("estado_que_no_existe") is None


# ---------------------------------------------------------------------------
# El webhook completo
# ---------------------------------------------------------------------------


def test_un_mensaje_de_twilio_crea_cliente_conversacion_y_mensaje_en_su_canal():
    with TestClient(app) as http:
        respuesta = _post(http, RUTA, {"From": CRUDO_TWILIO, "To": TO_OFICINA, "Body": "hola", "MessageSid": SID})

    assert respuesta.status_code == 204
    with Session(db.engine) as s:
        assert [c.phone for c in s.exec(select(Client)).all()] == [CANONICO]
        assert s.get(Conversation, CANONICO).provider == PROVIDER_TWILIO
        entrantes = s.exec(select(Message).where(Message.direction == Direction.IN)).all()
        assert [m.provider for m in entrantes] == [PROVIDER_TWILIO]


def test_las_respuestas_tambien_quedan_marcadas_en_el_canal_de_twilio():
    # REGLA: si un saliente quedara marcado como Meta, el estado de entrega de
    # Twilio se aplicaría a la fila equivocada (ver _tag_sent_message).
    with TestClient(app) as http:
        _post(http, RUTA, {"From": CRUDO_TWILIO, "To": TO_OFICINA, "Body": "hola", "MessageSid": SID})

    with Session(db.engine) as s:
        salientes = s.exec(select(Message).where(Message.direction == Direction.OUT)).all()
    assert salientes, "el bot no contestó"
    assert all(m.provider == PROVIDER_TWILIO for m in salientes)


def test_una_firma_invalida_se_rechaza_y_no_deja_rastro():
    with TestClient(app) as http:
        respuesta = _post(
            http, RUTA, {"From": CRUDO_TWILIO, "To": TO_OFICINA, "Body": "hola"}, firma="firma-inventada"
        )

    assert respuesta.status_code == 403
    with Session(db.engine) as s:
        assert s.exec(select(Client)).all() == []
        assert s.exec(select(Message)).all() == []


def test_un_payload_de_meta_enviado_a_la_ruta_de_twilio_se_rechaza_entero():
    # REGLA: el cortafuegos entre proveedores. Procesar "lo que se entienda" de
    # un payload ajeno crearía clientes y conversaciones a medias, sin firma que
    # respalde nada.
    with TestClient(app) as http:
        respuesta = http.post(
            RUTA,
            json={"entry": [{"changes": [{"value": {"messages": [{"from": CANONICO}]}}]}]},
        )

    assert respuesta.status_code == 403
    with Session(db.engine) as s:
        assert s.exec(select(Client)).all() == []


def test_el_mismo_sid_repetido_no_duplica_el_mensaje():
    # REGLA: Twilio reintenta si tarda la respuesta. Sin este candado, un
    # reintento avanzaría la máquina de estados dos veces.
    with TestClient(app) as http:
        form = {"From": CRUDO_TWILIO, "To": TO_OFICINA, "Body": "hola", "MessageSid": SID}
        _post(http, RUTA, form)
        _post(http, RUTA, form)

    with Session(db.engine) as s:
        entrantes = s.exec(select(Message).where(Message.direction == Direction.IN)).all()
    assert len(entrantes) == 1, f"el reintento duplicó el mensaje: {len(entrantes)} filas"


def test_el_callback_de_estado_actualiza_el_mensaje_correcto():
    with Session(db.engine) as s:
        s.add(Client(phone=CANONICO, name="Ana"))
        s.add(
            Message(
                phone=CANONICO,
                direction=Direction.OUT,
                body="respuesta",
                wa_message_id=SID,
                provider=PROVIDER_TWILIO,
            )
        )
        s.commit()

    with TestClient(app) as http:
        respuesta = _post(http, RUTA_ESTADO, {"MessageSid": SID, "MessageStatus": "delivered"})

    assert respuesta.status_code == 204
    with Session(db.engine) as s:
        fila = s.exec(select(Message).where(Message.wa_message_id == SID)).one()
    assert fila.wa_status == "delivered"


def test_el_callback_de_estado_tambien_exige_firma():
    with Session(db.engine) as s:
        s.add(
            Message(
                phone=CANONICO,
                direction=Direction.OUT,
                body="respuesta",
                wa_message_id=SID,
                provider=PROVIDER_TWILIO,
            )
        )
        s.commit()

    with TestClient(app) as http:
        respuesta = _post(
            http, RUTA_ESTADO, {"MessageSid": SID, "MessageStatus": "read"}, firma="no"
        )

    assert respuesta.status_code == 403
    with Session(db.engine) as s:
        assert s.exec(select(Message).where(Message.wa_message_id == SID)).one().wa_status is None


# ---------------------------------------------------------------------------
# Identidad compartida entre las dos cuentas
# ---------------------------------------------------------------------------


def test_el_mismo_humano_por_las_dos_cuentas_es_un_solo_cliente(monkeypatch):
    # REGLA: LA prueba de todo el proyecto. Escribir a un número y luego al otro
    # no puede partir a la persona en dos clientes, dos historiales y —cuando
    # entre cobranza— dos calendarios sobre la misma tarjeta.
    from app import whatsapp_client

    monkeypatch.setattr(whatsapp_client, "_send", lambda *a, **k: "wamid.META1")
    monkeypatch.setattr(settings, "app_secret", "")  # firma de Meta desactivada

    with TestClient(app) as http:
        _post(http, RUTA, {"From": CRUDO_TWILIO, "To": TO_OFICINA, "Body": "hola", "MessageSid": SID})
        from app import webhook

        webhook._process_message(
            {
                "from": "5215500000000",
                "id": "wamid.X1",
                "type": "text",
                "text": {"body": "sigo aquí"},
            },
            OFICINA_TLAXCALA_ID,
        )

    with Session(db.engine) as s:
        clientes = s.exec(select(Client)).all()
        conversaciones = s.exec(select(Conversation)).all()
    assert len(clientes) == 1, f"las dos cuentas crearon {len(clientes)} clientes"
    assert len(conversaciones) == 1


def test_escribir_al_otro_numero_mueve_el_canal_sin_reiniciar_el_tramite():
    # REGLA: una persona = una conversación (opción A). Cambiar de número cambia
    # por dónde se le contesta, NO borra su avance.
    #
    # Se prueba directo sobre _get_or_create_conversation y no por HTTP a
    # propósito: por HTTP, el contenido del mensaje puede reiniciar el flujo por
    # su cuenta (un "hola" es un saludo, y la máquina de estados vuelve al
    # inicio con razón). Eso taparía lo único que aquí importa — que el cambio
    # de CANAL, por sí solo, no toca el avance.
    from app import conversation

    with Session(db.engine) as s:
        s.add(
            Conversation(
                phone=CANONICO,
                state="menu_servicio",
                context_json='{"categoria": "actas"}',
                failed_attempts=1,
                provider=PROVIDER_META,
            )
        )
        s.commit()

        conv = conversation._get_or_create_conversation(s, CANONICO, PROVIDER_TWILIO)

    assert conv.provider == PROVIDER_TWILIO, "no se movió el canal de respuesta"
    assert conv.state == "menu_servicio", "se reinició el estado del trámite"
    assert conv.context_json == '{"categoria": "actas"}', "se perdió el avance del trámite"
    assert conv.failed_attempts == 1


# ---------------------------------------------------------------------------
# Degradación de interactivos
# ---------------------------------------------------------------------------


def test_una_lista_no_duplica_el_menu_del_cuerpo(monkeypatch):
    # REGLA: el body de un menú YA trae las opciones numeradas ("1. Acta…",
    # "0. Volver…"). Re-anexar las filas degradadas mostraba el menú dos veces
    # en el chat (bug reportado con captura el 2026-08-18).
    enviados = []
    monkeypatch.setattr(
        twilio_client, "send_text", lambda phone, body: enviados.append(body) or "SM1"
    )
    body = (
        "Servicios de Actas — responde con el número:\n"
        "1. Acta de nacimiento\n0. Volver al menú anterior"
    )

    twilio_client.send_list(
        "5215512345678", body, "Ver opciones",
        [("1", "Acta de nacimiento"), ("0", "⬅ Volver al menú")],
    )

    assert enviados == [body]  # tal cual, sin filas re-anexadas


def test_el_degradado_numerado_conserva_los_ids():
    # REGLA: _numerar (hoy usada por los botones sin plantilla) tiene que
    # conservar el id de cada opción, porque es lo que el cliente va a responder
    # y lo que la máquina de estados espera.
    texto = twilio_client._numerar("Elige:", [("1", "Actas"), ("2", "SAT")])

    assert "1) Actas" in texto
    assert "2) SAT" in texto


def test_la_lista_degradada_respeta_el_tope_de_opciones_de_whatsapp():
    opciones = [(str(i), f"Opción {i}") for i in range(1, 15)]

    texto = twilio_client._numerar("Elige:", opciones)

    assert "10) Opción 10" in texto
    assert "11) Opción 11" not in texto, "se pasó del tope de 10 opciones de WhatsApp"


# ---------------------------------------------------------------------------
# Botones nativos (plantilla de la Content API)
# ---------------------------------------------------------------------------

SID_PLANTILLA = "HX00000000000000000000000000000001"
OPCIONES_FB = [("1", "👍 Sí, excelente"), ("2", "👎 Puede mejorar")]


def test_con_plantilla_se_mandan_botones_nativos_y_no_texto(monkeypatch):
    # REGLA: Twilio solo manda botones por ContentSid. El cuerpo viaja como
    # variable {{1}} para que el texto siga saliendo del código y no de la
    # plantilla.
    enviado: dict = {}
    monkeypatch.setattr(
        twilio_client, "_post_message",
        lambda to, data: enviado.update(to=to, data=data) or "SM1",
    )

    sid = twilio_client.send_buttons(
        "525500000000", "¿Te sirvió?", OPCIONES_FB, content_sid=SID_PLANTILLA
    )

    assert sid == "SM1"
    assert enviado["data"]["ContentSid"] == SID_PLANTILLA
    assert json.loads(enviado["data"]["ContentVariables"]) == {"1": "¿Te sirvió?"}
    assert "Body" not in enviado["data"], "con plantilla no se manda el texto degradado"


def test_sin_plantilla_se_sigue_degradando_a_texto(monkeypatch):
    # REGLA: sin TWILIO_CONTENT_SID_FEEDBACK la encuesta tiene que salir igual,
    # como texto numerado y contestable tecleando.
    enviado: dict = {}
    monkeypatch.setattr(
        twilio_client, "_post_message",
        lambda to, data: enviado.update(data=data) or "SM2",
    )

    twilio_client.send_buttons("525500000000", "¿Te sirvió?", OPCIONES_FB)

    assert "ContentSid" not in enviado["data"]
    assert "1) 👍 Sí, excelente" in enviado["data"]["Body"]


def test_si_la_plantilla_falla_el_cliente_recibe_el_texto(monkeypatch):
    # REGLA: un SID equivocado o una plantilla borrada dejarían al cliente sin
    # encuesta y sin error visible. Vale más una encuesta fea que ninguna.
    intentos: list[dict] = []

    def _post(to, data):
        intentos.append(data)
        return None if "ContentSid" in data else "SM3"

    monkeypatch.setattr(twilio_client, "_post_message", _post)

    sid = twilio_client.send_buttons(
        "525500000000", "¿Te sirvió?", OPCIONES_FB, content_sid=SID_PLANTILLA
    )

    assert sid == "SM3", "no se cayó al texto cuando la plantilla falló"
    assert "1) 👍 Sí, excelente" in intentos[-1]["Body"]
