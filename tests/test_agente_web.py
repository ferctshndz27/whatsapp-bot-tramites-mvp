"""Agente en el WhatsApp Web del colaborador: `POST /crm/api/agente/entrante` (app/agente_web.py).

Se afirma lo que no debe fallar: sin sesión no contesta; el mensaje entra por el cerebro del bot
con el canal «web» y en la oficina de quien pregunta; un reintento (mismo id) no se duplica ni
se vuelve a contestar; nada sale por Meta ni por Twilio; y los botones salen como texto numerado.
"""

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

import app.db as db
from app import messaging, twilio_client, whatsapp_client
from app.agente_web import a_texto
from app.config import settings
from app.main import app
from app.models import PROVIDER_WEB, Client, Conversation, Direction, Message, Office, User

CLAVE = "clave-de-prueba"
HASH = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()
CLIENTE = "525500000077"


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "geoip_enabled", False)
    with Session(engine) as s:
        s.add(User(username="colab1", password_hash=HASH, office_id=1))
        s.commit()
    salidas = []
    for modulo in (whatsapp_client, twilio_client):
        monkeypatch.setattr(modulo, "send_text", lambda *a, _m=modulo, **k: salidas.append((_m.__name__, a)))
    yield salidas


def _cliente():
    c = TestClient(app, base_url="https://testserver")
    assert c.post("/login", data={"username": "colab1", "password": CLAVE}, follow_redirects=False).status_code == 303
    return c


def test_sin_sesion_no_contesta():
    r = TestClient(app).post("/crm/api/agente/entrante", json={"phone": CLIENTE, "text": "hola"})
    assert r.status_code == 403


@pytest.mark.parametrize("cuerpo", [
    {}, {"phone": CLIENTE}, {"phone": CLIENTE, "text": "  "}, {"phone": "abc", "text": "hola"},
    {"phone": CLIENTE, "text": "x" * 2001}, {"phone": CLIENTE, "text": "hola", "wa_id": "i" * 129},
])
def test_rechaza_lo_que_no_es_un_mensaje(cuerpo):
    assert _cliente().post("/crm/api/agente/entrante", json=cuerpo).status_code == 400


def test_contesta_por_el_cerebro_del_bot_con_canal_web(_base):
    r = _cliente().post("/crm/api/agente/entrante",
                        json={"phone": CLIENTE, "text": "hola", "wa_id": "3EB0AAA"})
    assert r.status_code == 200
    respuestas = r.json()["respuestas"]
    assert respuestas and all(isinstance(t, str) and t for t in respuestas)
    with Session(db.engine) as s:
        assert s.get(Conversation, CLIENTE).provider == PROVIDER_WEB
        filas = s.exec(select(Message).where(Message.phone == CLIENTE)).all()
        assert [m.direction for m in filas][0] == Direction.IN
        assert filas[0].wa_message_id == "web:3EB0AAA"
        assert any(m.direction == Direction.OUT for m in filas)
    assert _base == []  # el servidor no mandó nada por ningún proveedor


def test_un_reintento_no_se_duplica_ni_se_vuelve_a_contestar():
    c = _cliente()
    cuerpo = {"phone": CLIENTE, "text": "hola", "wa_id": "3EB0BBB"}
    assert c.post("/crm/api/agente/entrante", json=cuerpo).json()["respuestas"]
    assert c.post("/crm/api/agente/entrante", json=cuerpo).json()["respuestas"] == []
    with Session(db.engine) as s:
        entrantes = s.exec(select(Message).where(Message.phone == CLIENTE, Message.direction == Direction.IN)).all()
        assert len(entrantes) == 1


def test_un_envio_del_servidor_a_un_chat_web_no_sale_por_meta_ni_twilio(_base):
    c = _cliente()
    c.post("/crm/api/agente/entrante", json={"phone": CLIENTE, "text": "hola"})
    assert messaging.send_text(CLIENTE, "recordatorio") is None
    assert _base == []


def test_los_botones_salen_como_texto_numerado():
    texto = a_texto({"text": "¿Confirmas?", "buttons": [("si", "Sí"), ("no", "No")]})
    assert texto.startswith("¿Confirmas?") and "1) Sí" in texto and "2) No" in texto
    assert a_texto({"text": "Menú", "list": [("a", "A")], "button": "Ver"}) == "Menú"
    assert a_texto("plano") == "plano"


def test_no_contesta_a_un_cliente_de_otra_oficina():
    with Session(db.engine) as s:
        s.add(Office(id=2, nombre="Puebla", slug="puebla", ciudad="Puebla"))
        s.add(Client(phone=CLIENTE, name="Ajeno", office_id=2))
        s.commit()
    r = _cliente().post("/crm/api/agente/entrante", json={"phone": CLIENTE, "text": "hola"})
    assert r.status_code == 200 and r.json() == {"respuestas": []}
    with Session(db.engine) as s:
        assert s.exec(select(Message).where(Message.phone == CLIENTE)).all() == []
