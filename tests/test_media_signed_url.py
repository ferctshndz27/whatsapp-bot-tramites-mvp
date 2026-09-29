"""Adjuntos por URL firmada — Fase 4.

REGLA DE NEGOCIO: Twilio no acepta subir el archivo; exige una `MediaUrl` de la
que él lo descarga. Pero MEDIA_DIR guarda DOCUMENTOS DE CLIENTES —actas,
constancias fiscales, identificaciones— así que publicarlo abierto sería una
fuga permanente: con el nombre del archivo cualquiera lo leería para siempre.

`/media/publico/{name}` es la ÚNICA ruta del panel sin sesión. La firma y la
expiración son lo único que la separa de internet abierto, así que aquí se
prueba más que en cualquier otra vista.
"""

import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

import app.db as db
from app import media_store, twilio_client
from app.config import settings
from app.main import app
from app.models import PROVIDER_META, PROVIDER_TWILIO, Client, Conversation, Direction, Message

SECRETO = "secreto-de-prueba-largo"
BASE = "https://tramites.example.mx"
CANONICO = "525500000000"


@pytest.fixture(autouse=True)
def _entorno(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "session_secret", SECRETO)
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    monkeypatch.setattr(settings, "public_base_url", BASE)
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "geoip_enabled", False)
    yield


@pytest.fixture()
def adjunto(tmp_path):
    """Un documento de cliente ya guardado, con el nombre que genera el panel."""
    nombre = f"{uuid.uuid4().hex}.pdf"
    (tmp_path / nombre).write_bytes(b"%PDF-1.4 acta de nacimiento")
    return nombre


def _partes(ruta: str) -> tuple[str, str]:
    return ruta.split("exp=")[1].split("&")[0], ruta.split("sig=")[1]


# ---------------------------------------------------------------------------
# La firma
# ---------------------------------------------------------------------------


def test_una_url_recien_firmada_es_valida(adjunto):
    ruta = media_store.signed_media_path(adjunto)

    assert ruta is not None
    exp, sig = _partes(ruta)
    assert media_store.verify_media_token(adjunto, exp, sig) is True


def test_una_firma_alterada_se_rechaza(adjunto):
    exp, _ = _partes(media_store.signed_media_path(adjunto))

    assert media_store.verify_media_token(adjunto, exp, "0" * 64) is False


def test_no_se_puede_estirar_la_vigencia_sin_volver_a_firmar(adjunto):
    # REGLA: si `exp` no estuviera dentro de lo firmado, cualquiera alargaría el
    # enlace a un año y el documento quedaría público de forma indefinida.
    exp, sig = _partes(media_store.signed_media_path(adjunto))

    assert media_store.verify_media_token(adjunto, str(int(exp) + 86400), sig) is False


def test_una_url_vencida_se_rechaza(adjunto, monkeypatch):
    # REGLA: Twilio descarga en segundos. Un enlace que no caduca es un
    # documento de cliente publicado para siempre.
    exp, sig = _partes(media_store.signed_media_path(adjunto, ttl=1))
    ahora = time.time()
    monkeypatch.setattr(media_store.time, "time", lambda: ahora + 3600)

    assert media_store.verify_media_token(adjunto, exp, sig) is False


def test_la_firma_de_un_archivo_no_sirve_para_otro(adjunto, tmp_path):
    # REGLA: el nombre va dentro de lo firmado. Si no, con un enlace legítimo se
    # leería CUALQUIER archivo de MEDIA_DIR — el acta de otro cliente incluida.
    otro = f"{uuid.uuid4().hex}.pdf"
    (tmp_path / otro).write_bytes(b"%PDF-1.4 documento de otra persona")
    exp, sig = _partes(media_store.signed_media_path(adjunto))

    assert media_store.verify_media_token(otro, exp, sig) is False


@pytest.mark.parametrize(
    "nombre", ["../../etc/passwd", "..%2Ftramites.db", "archivo.sh", "no-es-un-uuid.pdf", ""]
)
def test_solo_se_firman_nombres_con_el_formato_del_panel(nombre):
    # REGLA: MEDIA_NAME_RE (uuid de 32 hex + extensión permitida) es lo que
    # impide construir una ruta hacia afuera de MEDIA_DIR.
    assert media_store.signed_media_path(nombre) is None
    assert media_store.verify_media_token(nombre, str(int(time.time()) + 60), "x" * 64) is False


def test_sin_secreto_no_se_firma_ni_se_valida(adjunto, monkeypatch):
    # REGLA: sin SESSION_SECRET la firma no sería verificable. Se prefiere NO
    # mandar el adjunto antes que publicarlo sin protección.
    monkeypatch.setattr(settings, "session_secret", "")

    assert media_store.signed_media_path(adjunto) is None
    assert media_store.verify_media_token(adjunto, str(int(time.time()) + 60), "x") is False


# ---------------------------------------------------------------------------
# La ruta pública
# ---------------------------------------------------------------------------


def test_con_firma_valida_el_archivo_se_sirve_sin_sesion(adjunto):
    ruta = media_store.signed_media_path(adjunto)

    with TestClient(app) as http:
        respuesta = http.get(ruta)

    assert respuesta.status_code == 200
    assert respuesta.content == b"%PDF-1.4 acta de nacimiento"


@pytest.mark.parametrize("query", ["", "?exp=99999999999&sig=falsa"])
def test_sin_firma_valida_la_ruta_publica_no_entrega_nada(adjunto, query):
    with TestClient(app) as http:
        respuesta = http.get(f"/media/publico/{adjunto}{query}")

    assert respuesta.status_code == 404
    assert b"acta de nacimiento" not in respuesta.content


def test_la_respuesta_no_revela_si_el_archivo_existe(adjunto):
    # REGLA: distinguir "firma inválida" de "no existe" le permitiría a un
    # extraño enumerar qué documentos hay guardados.
    inexistente = f"{uuid.uuid4().hex}.pdf"

    with TestClient(app) as http:
        con_firma_mala = http.get(f"/media/publico/{adjunto}?exp=9999999999&sig=mala")
        inexistente_resp = http.get(f"/media/publico/{inexistente}?exp=9999999999&sig=mala")

    assert con_firma_mala.status_code == inexistente_resp.status_code == 404


def test_la_ruta_con_sesion_sigue_exigiendo_sesion(adjunto):
    # REGLA: la ruta firmada es una PUERTA ADICIONAL, no un reemplazo. La de
    # siempre no puede haberse aflojado.
    with TestClient(app) as http:
        respuesta = http.get(f"/media/{adjunto}", follow_redirects=False)

    assert respuesta.headers.get("location") == "/login"


# ---------------------------------------------------------------------------
# El envío por Twilio
# ---------------------------------------------------------------------------


def test_twilio_manda_el_adjunto_desde_memoria_por_url_efimera(monkeypatch):
    # Sin disco (regla del operador, 2026-09-28): Twilio descarga el archivo de
    # una URL que sirve la RAM unos minutos, y la referencia «msg:<sid>» es lo
    # único que queda para volver a abrirlo desde Twilio.
    enviados = {}
    monkeypatch.setattr(
        twilio_client, "_post_message", lambda to, data: enviados.update(data) or "SM1"
    )

    sid, ref = twilio_client.send_media(
        CANONICO, b"%PDF-1.4 acta", "application/pdf", "tu acta", "acta.pdf"
    )

    assert (sid, ref) == ("SM1", "msg:SM1")
    assert enviados["MediaUrl"].startswith(f"{BASE}/media/efimero/")
    assert enviados["Body"] == "tu acta"
    ruta = enviados["MediaUrl"].removeprefix(BASE)
    respuesta = TestClient(app, base_url=BASE).get(ruta)
    assert respuesta.status_code == 200
    assert respuesta.content == b"%PDF-1.4 acta"
    assert respuesta.headers["content-type"].startswith("application/pdf")


def test_la_url_efimera_no_sirve_con_un_token_inventado_ni_vencida(monkeypatch):
    token = media_store.publicar_efimero(b"%PDF-1.4", "application/pdf")
    cliente = TestClient(app, base_url=BASE)
    assert cliente.get("/media/efimero/inventado").status_code == 404
    assert cliente.get(f"/media/efimero/{token}").status_code == 200
    monkeypatch.setattr(time, "time", lambda: 10**12)
    assert cliente.get(f"/media/efimero/{token}").status_code == 404


def test_sin_url_publica_el_adjunto_no_se_manda(monkeypatch):
    # REGLA: sin PUBLIC_BASE_URL la MediaUrl apuntaría a un host que Twilio no
    # puede alcanzar. Mejor no enviar y dejarlo en el log que mandar un mensaje
    # roto al cliente.
    monkeypatch.setattr(settings, "public_base_url", "")
    monkeypatch.setattr(twilio_client, "_post_message", lambda *a, **k: "NO-DEBIO-LLAMARSE")

    assert twilio_client.send_media(CANONICO, b"%PDF-1.4", "application/pdf") == (None, None)


# ---------------------------------------------------------------------------
# Ruteo estricto del panel
# ---------------------------------------------------------------------------


def test_el_composer_responde_por_el_canal_de_la_conversacion(monkeypatch):
    # REGLA: el asesor escribe desde el panel sin elegir canal. Si la respuesta
    # saliera por Meta a un cliente de Twilio, llegaría de un número que no
    # reconoce — y fuera de la ventana de ESE número, no llegaría.
    from app import messaging, whatsapp_client

    with Session(db.engine) as s:
        s.add(Client(phone=CANONICO, name="Ana"))
        s.add(Conversation(phone=CANONICO, provider=PROVIDER_TWILIO))
        s.commit()

    por_meta, por_twilio = [], []
    monkeypatch.setattr(whatsapp_client, "send_text", lambda p, b: por_meta.append(b))
    monkeypatch.setattr(twilio_client, "send_text", lambda p, b: por_twilio.append(b) or "SM9")

    messaging.send_text(CANONICO, "aquí va tu cotización")

    assert por_twilio == ["aquí va tu cotización"]
    assert por_meta == [], "la respuesta del asesor salió por la cuenta equivocada"


def test_la_bandeja_marca_el_canal_de_cada_conversacion():
    # REGLA: la ventana de 24 h se cuenta por número, así que el asesor tiene
    # que ver en qué cuenta está parado antes de decidir si aún puede escribir.
    from app.inbox import build_conversation_items
    from app.auth import Alcance

    with Session(db.engine) as s:
        s.add(Client(phone=CANONICO, name="Ana"))
        s.add(Conversation(phone=CANONICO, provider=PROVIDER_TWILIO))
        s.add(Message(phone=CANONICO, direction=Direction.IN, body="hola"))
        s.add(Client(phone="525511112222", name="Beto"))
        s.add(Conversation(phone="525511112222", provider=PROVIDER_META))
        s.add(Message(phone="525511112222", direction=Direction.IN, body="hola"))
        s.commit()
        items = {i["phone"]: i for i in build_conversation_items(s, Alcance(office_id=1, ve_todas=True))}

    assert items[CANONICO]["provider"] == PROVIDER_TWILIO
    assert items["525511112222"]["provider"] == PROVIDER_META


def test_una_conversacion_sin_registro_se_muestra_como_meta():
    # REGLA: no puede quedar en blanco. Meta es la cuenta que existía primero.
    from app.inbox import build_conversation_items
    from app.auth import Alcance

    with Session(db.engine) as s:
        s.add(Client(phone=CANONICO, name="Ana"))
        s.add(Message(phone=CANONICO, direction=Direction.IN, body="hola"))
        s.commit()
        items = build_conversation_items(s, Alcance(office_id=1, ve_todas=True))

    assert items[0]["provider"] == PROVIDER_META
