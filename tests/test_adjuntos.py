"""Archivos de los chats con clientes sin guardarlos en el VPS (app/adjuntos.py).

Regla del operador (2026-09-28): los archivos que se cruzan clientes y colaboradores viven en
Meta o en Twilio; el VPS guarda solo la referencia y los trae al abrirlos. Datos del proveedor
verificados en su documentación: el media id de un webhook de Meta vence a los 7 días y el de
una subida a los 30 (por eso se renueva); Twilio conserva lo recibido hasta que se borre y exige
HTTP Basic; ninguno acepta WebP como imagen.
"""

from datetime import timedelta

import bcrypt
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

import app.db as db
from app import adjuntos, messaging, twilio_client, whatsapp_client
from app.config import settings
from app.conversation import handle_incoming
from app.main import app
from app.models import (
    PROVIDER_META,
    PROVIDER_TWILIO,
    Client,
    Direction,
    Message,
    Ticket,
    TicketStatus,
    User,
    utcnow,
)

CLAVE = "clave-de-prueba"
HASH = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()
CLI = "525500000051"
PDF = b"%PDF-1.4 constancia"
PNG = b"\x89PNG\r\n\x1a\n imagen"
DOCX = b"PK\x03\x04 word"


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "geoip_enabled", False)
    with Session(engine) as s:
        s.add(User(username="jefa", password_hash=HASH, is_admin=True, office_id=1))
        s.add(User(username="ana", password_hash=HASH, is_admin=False, office_id=1))
        s.add(Client(phone=CLI, name="Cliente", office_id=1))
        s.commit()
    yield


def _mensaje(**campos) -> int:
    datos = dict(phone=CLI, direction=Direction.IN, body="[documento: acta.pdf]",
                 provider=PROVIDER_META, media_ref="mid-1", media_mime="application/pdf",
                 media_nombre="acta.pdf", wa_message_id="wamid.IN1",
                 media_vence_at=utcnow() + timedelta(days=7))
    datos.update(campos)
    with Session(db.engine) as s:
        m = Message(**datos)
        s.add(m)
        s.commit()
        return m.id


def _get(mid: int) -> Message:
    with Session(db.engine) as s:
        return s.get(Message, mid)


def _sesion(username: str) -> TestClient:
    cliente = TestClient(app, base_url="https://testserver")
    r = cliente.post("/login", data={"username": username, "password": CLAVE},
                     follow_redirects=False)
    assert r.status_code == 303
    return cliente


@pytest.fixture
def proveedor(monkeypatch):
    """Doble del proveedor: registra quién pidió qué y devuelve lo configurado."""
    estado = {"descargas": [], "subidas": [], "devolver": (PDF, "application/pdf")}

    def descargar(ref, provider, max_bytes):
        estado["descargas"].append((ref, provider, max_bytes))
        return estado["devolver"]

    def subir(data, mime, nombre=None):
        estado["subidas"].append((data, mime, nombre))
        return "mid-30dias"

    monkeypatch.setattr(messaging, "descargar_media", descargar)
    monkeypatch.setattr(messaging, "subir_media_meta", subir)
    return estado


# --- Entrada de Meta ----------------------------------------------------------


def test_de_meta_lee_imagen_y_documento_y_nada_mas():
    doc = {"type": "document", "document": {"id": "d1", "mime_type": "application/pdf",
                                             "filename": "acta.pdf", "caption": "mi acta"}}
    assert adjuntos.de_meta(doc) == {"ref": "d1", "mime": "application/pdf",
                                      "nombre": "acta.pdf", "caption": "mi acta"}
    img = {"type": "image", "image": {"id": "i1", "mime_type": "image/jpeg"}}
    assert adjuntos.de_meta(img)["ref"] == "i1"
    assert adjuntos.de_meta({"type": "audio", "audio": {"id": "a1"}}) is None
    assert adjuntos.de_meta({"type": "text", "text": {"body": "hola"}}) is None


def test_el_bot_calla_ante_un_archivo_si_alguien_apago_el_agente():
    with Session(db.engine) as s:
        s.add(Ticket(phone=CLI, category="c", service_id="s", service_name="Acta",
                     price_info="$1", status=TicketStatus.TOMADO, assigned_to="ana",
                     office_id=1, agent_active=False))
        s.commit()
        adj = {"ref": "m", "mime": "application/pdf", "nombre": "a.pdf", "caption": ""}
        assert handle_incoming(s, CLI, "", is_media=True, adjunto=adj) == []
        # El archivo igual queda en el chat para quien lo atiende.
        m = s.exec(select(Message).where(Message.direction == Direction.IN)).one()
        assert m.media_ref == "m"


# --- Renovación de Meta (7 → 30 días) -------------------------------------------


def test_la_renovacion_sube_otra_vez_y_la_referencia_dura_30_dias(proveedor):
    mid = _mensaje()
    assert adjuntos.renovar_meta("wamid.IN1") is True
    m = _get(mid)
    assert m.media_ref == "mid-30dias" and m.media_bytes == len(PDF)
    vence = m.media_vence_at.replace(tzinfo=None)
    assert timedelta(days=29) < vence - utcnow().replace(tzinfo=None) <= timedelta(days=30)
    assert proveedor["descargas"] == [("mid-1", PROVIDER_META, 16 * 1024 * 1024)]
    assert proveedor["subidas"] == [(PDF, "application/pdf", "acta.pdf")]


def test_un_reintento_de_meta_no_vuelve_a_renovar(proveedor):
    _mensaje()
    adjuntos.renovar_meta("wamid.IN1")
    assert adjuntos.renovar_meta("wamid.IN1") is False
    assert len(proveedor["descargas"]) == 1


def test_no_se_renueva_lo_que_no_es_lo_que_dice_ser(proveedor):
    mid = _mensaje()
    proveedor["devolver"] = (b"<html>no soy pdf</html>", "application/pdf")
    assert adjuntos.renovar_meta("wamid.IN1") is False
    assert proveedor["subidas"] == []
    assert _get(mid).media_ref == "mid-1"  # queda la referencia de 7 días


def test_twilio_no_se_renueva(proveedor):
    _mensaje(provider=PROVIDER_TWILIO, media_ref="https://api.twilio.com/x", media_vence_at=None)
    assert adjuntos.renovar_meta("wamid.IN1") is False
    assert proveedor["descargas"] == []


# --- Visor del CRM --------------------------------------------------------------


def test_el_visor_sirve_el_archivo_con_cabeceras_seguras(proveedor):
    mid = _mensaje()
    r = _sesion("jefa").get(f"/crm/api/adjunto/{mid}")
    assert r.status_code == 200
    assert r.content == PDF
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.headers["content-disposition"] == "inline; filename*=UTF-8''acta.pdf"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"].startswith("private")


def test_word_se_descarga_en_vez_de_abrirse(proveedor):
    tipo = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    proveedor["devolver"] = (DOCX, tipo)
    mid = _mensaje(media_mime=tipo, media_nombre="carta ñ.docx")
    r = _sesion("jefa").get(f"/crm/api/adjunto/{mid}")
    assert r.headers["content-disposition"] == "attachment; filename*=UTF-8''carta%20%C3%B1.docx"


def test_quien_no_ve_el_chat_no_ve_el_archivo_ni_se_le_pide_al_proveedor(proveedor):
    mid = _mensaje()
    r = _sesion("ana").get(f"/crm/api/adjunto/{mid}")  # sin folio ni cartera de ese cliente
    assert r.status_code == 404
    assert proveedor["descargas"] == []
    assert TestClient(app, base_url="https://testserver").get(
        f"/crm/api/adjunto/{mid}").status_code == 403


def test_un_archivo_vencido_se_dice_y_no_se_pide(proveedor):
    mid = _mensaje(media_vence_at=utcnow() - timedelta(minutes=1))
    r = _sesion("jefa").get(f"/crm/api/adjunto/{mid}")
    assert r.status_code == 410
    assert proveedor["descargas"] == []


@pytest.mark.parametrize("devuelve,status", [(None, 502), ((b"MZ ejecutable", "application/pdf"), 415)])
def test_lo_que_falla_en_el_proveedor_no_se_sirve(proveedor, devuelve, status):
    proveedor["devolver"] = devuelve
    mid = _mensaje()
    assert _sesion("jefa").get(f"/crm/api/adjunto/{mid}").status_code == status


def test_el_archivo_se_pide_al_proveedor_del_mensaje_no_al_canal_de_hoy(proveedor):
    """Un archivo que entró por Twilio vive en Twilio aunque el cliente hoy escriba por Meta."""
    proveedor["devolver"] = (PNG, "image/png")
    mid = _mensaje(provider=PROVIDER_TWILIO, media_ref="https://api.twilio.com/2010-04-01/M",
                   media_mime="image/png", media_vence_at=None)
    assert _sesion("jefa").get(f"/crm/api/adjunto/{mid}").status_code == 200
    assert proveedor["descargas"][0][1] == PROVIDER_TWILIO


def test_el_chat_del_crm_describe_el_adjunto():
    mid = _mensaje(media_vence_at=utcnow() - timedelta(days=1))
    mensajes = _sesion("jefa").get(f"/crm/api/chat/{CLI}").json()["mensajes"]
    adj = next(m for m in mensajes if m["id"] == mid)["adjunto"]
    assert adj["url"] == f"/crm/api/adjunto/{mid}"
    assert (adj["mime"], adj["nombre"], adj["vencido"]) == ("application/pdf", "acta.pdf", True)


# --- Twilio -----------------------------------------------------------------------


def test_la_entrada_de_twilio_guarda_la_url_de_su_api_sin_vencimiento():
    datos = twilio_client.parse_inbound({
        "From": "whatsapp:+525500000051", "NumMedia": "1", "Body": "",
        "MediaUrl0": "https://api.twilio.com/2010-04-01/Accounts/AC1/Messages/MM1/Media/ME1",
        "MediaContentType0": "application/pdf", "MessageSid": "MM1",
    })
    with Session(db.engine) as s:
        handle_incoming(s, CLI, "", is_media=True, wa_message_id="MM1",
                        provider=PROVIDER_TWILIO, adjunto=datos["adjunto"])
        m = s.exec(select(Message).where(Message.direction == Direction.IN)).one()
    assert m.media_ref.startswith("https://api.twilio.com/") and m.media_vence_at is None


def test_twilio_no_manda_su_credencial_a_otro_host(monkeypatch):
    monkeypatch.setattr(twilio_client, "credencial", lambda *a, **k: "valor")
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: pytest.fail("no debió descargar"))
    assert twilio_client.descargar_media("https://evil.example/archivo", 10_000) is None


# --- Topes de descarga de Meta ----------------------------------------------------------


class _Info:
    def __init__(self, datos):
        self._datos = datos

    def raise_for_status(self):
        pass

    def json(self):
        return self._datos


def test_meta_no_descarga_una_url_sin_https_ni_lo_que_pesa_de_mas(monkeypatch):
    monkeypatch.setattr(whatsapp_client, "_credenciales_media", lambda: ("tok", "pnid"))
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: pytest.fail("no debió descargar"))
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Info({"url": "http://x/y"}))
    assert whatsapp_client.descargar_media("m", 1000) is None
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Info({"url": "https://x/y", "file_size": 5000}))
    assert whatsapp_client.descargar_media("m", 1000) is None


# --- Salida desde el CRM ----------------------------------------------------------------


def test_el_crm_no_manda_webp_ni_un_pdf_falso(monkeypatch):
    enviados = []
    monkeypatch.setattr(messaging, "send_media", lambda *a, **k: enviados.append(a) or ("w", "r"))
    with Session(db.engine) as s:
        s.add(Message(phone=CLI, direction=Direction.IN, body="hola"))
        s.commit()
    cliente = _sesion("jefa")
    for nombre, tipo, datos in (
        ("foto.webp", "image/webp", b"RIFF\x00\x00\x00\x00WEBP"),
        ("acta.pdf", "application/pdf", b"<script>no</script>"),
    ):
        r = cliente.post(f"/inbox/{CLI}/send", data={"body": ""},
                         files={"file": (nombre, datos, tipo)}, follow_redirects=False)
        assert r.headers["location"].endswith("?err=tipo")
    assert enviados == []


# --- Hallazgos de las revisiones adversariales (2026-09-29) --------------------------------


def test_el_nombre_de_un_archivo_entrante_cabe_en_el_espejo():
    """Corrección: sin recortar, un filename de más de 255 rompía para siempre la fila del
    espejo (NVARCHAR(255)), y backfill reintentaba el mismo valor."""
    m = Message(phone=CLI, direction=Direction.IN, body="x")
    adjuntos.sellar(m, {"ref": "r", "mime": "application/pdf", "nombre": "a" * 400}, PROVIDER_META)
    assert len(m.media_nombre) == 255


def test_el_almacen_efimero_tiene_tope_y_twilio_no_envia_si_esta_lleno(monkeypatch):
    """Seguridad: sin tope total, envíos seguidos de 16 MB se acumulaban en la RAM del único
    proceso durante 15 minutos cada uno."""
    from app import media_store

    monkeypatch.setattr(media_store, "_EFIMEROS", {})
    monkeypatch.setattr(media_store, "EFIMERO_MAX_BYTES", 10)
    monkeypatch.setattr(settings, "public_base_url", "https://tramites.example.mx")
    monkeypatch.setattr(twilio_client, "_post_message", lambda *a: pytest.fail("no debió enviar"))
    assert media_store.publicar_efimero(b"123456", "application/pdf") is not None
    assert media_store.publicar_efimero(b"123456", "application/pdf") is None  # 12 > 10
    assert twilio_client.send_media(CLI, b"%PDF-1", "application/pdf") == (None, None)
