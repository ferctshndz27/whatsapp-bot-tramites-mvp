"""De punta a punta: lo que el cliente mandó ANTES de contar su caso lo ve el asesor en la ficha
(operador, 2026-10-12).

Es el pedido completo: «si el cliente manda archivos o fotos el agente debe procurar a qué folio
pertenecen para que el asesor sepa y los vea en la ficha del ticket». Junta dos piezas que se
prueban por separado —el bot liga la racha de archivos al abrir el caso, la ficha lista los
archivos del folio— para que ninguna de las dos se pueda romper sin que esta lo note.

Si se pone roja: el cliente mandó sus fotos, el bot le dijo que las recibió, y el asesor abre la
ficha de su caso y no ve nada.
"""

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

import app.db as db
from app.catalog import catalog
from app.config import settings
from app.conversation import handle_incoming
from app.main import app
from app.models import Client, Conversation, Message, Ticket, User

CLAVE = "clave-de-prueba-1234"
HASH = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()
PHONE = "5215533334444"
SITUACION = "tengo una situación con mi pensión, qué se puede hacer"


@pytest.fixture(autouse=True)
def entorno(_isolated_db, monkeypatch, request):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    request.getfixturevalue("catalogo_vivo")
    for k, v in (("mssql_host", ""), ("anthropic_api_key", ""), ("geoip_enabled", False),
                 ("ollama_url", ""), ("rag_enabled", False), ("contexto_folio_enabled", True),
                 ("revision_enabled", True), ("contexto_min_palabras", 40)):
        monkeypatch.setattr(settings, k, v)
    with Session(db.engine) as s:
        s.add(User(username="admin", password_hash=HASH, is_admin=True))
        s.add(Client(phone=PHONE, name="Gestor", office_id=1))
        s.add(Conversation(phone=PHONE, state="menu_categoria", context_json="{}"))
        s.commit()
    yield
    catalog._refresh()


def _archivo(n: int, nombre: str, mime: str = "application/pdf") -> dict:
    return {"ref": f"media-{n}", "mime": mime, "nombre": nombre, "caption": ""}


def test_el_asesor_ve_en_la_ficha_del_caso_los_archivos_que_el_cliente_mando_antes_de_contarlo():
    with Session(db.engine) as s:
        handle_incoming(s, PHONE, "", is_media=True, adjunto=_archivo(1, "ine-del-trabajador.jpg", "image/jpeg"))
        handle_incoming(s, PHONE, "", is_media=True, adjunto=_archivo(2, "constancia-semanas.pdf"))
        handle_incoming(s, PHONE, SITUACION)
        handle_incoming(s, PHONE, "", is_media=True, adjunto=_archivo(3, "carta-del-patron.pdf"))
        respuesta = handle_incoming(s, PHONE, "revision")
        caso = s.exec(select(Ticket).where(Ticket.phone == PHONE, Ticket.service_id == "analisis")).one()
        ids = {n: s.exec(select(Message.id).where(Message.media_ref == f"media-{n}")).one() for n in (1, 2, 3)}

    assert f"#{caso.id}" in respuesta[0], "el cliente no se entera de a qué folio pertenece su caso"

    web = TestClient(app, base_url="https://testserver")
    assert web.post("/login", data={"username": "admin", "password": CLAVE}, follow_redirects=False).status_code == 303
    ficha = web.get(f"/tickets/{caso.id}")
    assert ficha.status_code == 200
    html = ficha.text

    assert "Archivos del cliente" in html
    for nombre in ("ine-del-trabajador.jpg", "constancia-semanas.pdf", "carta-del-patron.pdf"):
        assert nombre in html, f"la ficha del caso no muestra {nombre}"
    for n in (1, 2, 3):
        assert f'href="/crm/api/adjunto/{ids[n]}"' in html
