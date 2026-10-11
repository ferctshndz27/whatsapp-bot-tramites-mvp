"""Los lugares que resuelven `Ticket.service_id` fuera del catálogo toleran "analisis" (PR5, tarea 29).

El «caso para análisis» (diseño §5.3) es un folio sin producto, como el de la escalada: su
`service_id` no existe en el catálogo. Auditoría sobre `feat/regla-del-folio@58d2d51`:

| Lugar | Qué hace con un id fuera del catálogo | Cambio |
|---|---|---|
| `conversation._status_reply` | lista el folio con su `service_name` | ninguno |
| `dashboard.ticket_detail` (`catalog.service_by_id` → None) | la plantilla ya lo tolera | ninguno |
| `dashboard._notify_finish` | «Tu solicitud de 'Caso para análisis'…» | genérico, como "escalado" |
| `dashboard._notify_release` | «Tu solicitud de 'Caso para análisis'…» | genérico, como "escalado" |
| `agent_tools._cotizar_servicio` | error: no existe | ninguno (no se cotiza un caso) |
| `nlu._validated` | lo descarta como id alucinado | ninguno |
| `synonym_miner.mine_synonyms` y `persist_proposals` | descartan la propuesta | ninguno |

Si una de estas pruebas se pone roja, a un cliente con un caso le llega un aviso que nombra un
«trámite» que nunca pidió, el bot le cotiza o le aprende sinónimos a algo que no es un servicio, o
el asesor no puede abrir el folio.
"""

import json

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine, select
from sqlmodel.pool import StaticPool

import app.db as db
from app import agent_tools, dashboard, nlu, synonym_miner
from app.config import settings
from app.conversation import _status_reply
from app.main import app
from app.models import SERVICIOS_SIN_CATALOGO, SynonymProposal, Ticket, TicketStatus, User

CLAVE = "clave-de-prueba"
HASH = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()
PHONE = "525599990001"


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "geoip_enabled", False)


def _caso(**extra) -> int:
    with Session(db.engine) as s:
        s.add(User(username="asesora", password_hash=HASH))
        t = Ticket(phone=PHONE, service_id="analisis", service_name="Caso para análisis",
                   category="(análisis)", price_info="N/A", escalado=True, assigned_to="asesora",
                   status=TicketStatus.TOMADO, notes="Caso para análisis. Mensaje del cliente:\nhola",
                   **extra)
        s.add(t)
        s.commit()
        return t.id


def _enviados(monkeypatch) -> list[str]:
    enviados: list[str] = []
    monkeypatch.setattr(dashboard.ventana, "abierta_para", lambda session, phone: True)
    monkeypatch.setattr(dashboard.messaging, "send_text",
                        lambda phone, body: enviados.append(body) or "wamid.X")
    return enviados


def test_analisis_y_escalado_son_los_folios_sin_catalogo():
    assert SERVICIOS_SIN_CATALOGO == {"escalado", "analisis"}


def test_el_estatus_lista_el_caso_con_su_nombre():
    folio = _caso()
    with Session(db.engine) as s:
        assert f"Caso para análisis (folio #{folio})" in _status_reply(s, PHONE)


def test_el_asesor_abre_el_folio_del_caso():
    folio = _caso()
    with TestClient(app, base_url="https://testserver") as cliente:
        r = cliente.post("/login", data={"username": "asesora", "password": CLAVE}, follow_redirects=False)
        assert r.status_code == 303, "el login de la prueba dejó de funcionar"
        r = cliente.get(f"/tickets/{folio}")
    assert r.status_code == 200
    assert "Caso para análisis" in r.text


def test_el_aviso_de_cierre_de_un_caso_es_el_generico(monkeypatch):
    """Mutación: sin "analisis" en la tupla, el cliente lee «Tu solicitud de 'Caso para análisis'»."""
    enviados = _enviados(monkeypatch)
    dashboard._notify_finish(_caso())
    assert enviados == [dashboard.FINISH_NOTICE_GENERIC]


def test_el_aviso_de_soltado_de_un_caso_es_el_generico(monkeypatch):
    enviados = _enviados(monkeypatch)
    dashboard._notify_release(_caso())
    assert enviados == [dashboard.RELEASE_NOTICE_GENERIC]


def test_un_caso_no_se_cotiza():
    texto, es_error = agent_tools._cotizar_servicio("analisis")
    assert es_error and "No existe" in texto


def test_el_nlu_descarta_analisis_como_id_alucinado():
    assert nlu._validated({"service_id": "analisis"}) == (None, None)


def test_el_minero_no_propone_sinonimos_para_analisis(monkeypatch):
    monkeypatch.setattr(settings, "ollama_url", "http://ollama.invalido")
    respuesta = {"proposals": [{"service_id": "analisis", "synonym": "revisar mi caso"}]}
    monkeypatch.setattr(nlu, "ollama_chat", lambda *a, **k: {"content": json.dumps(respuesta)})
    assert synonym_miner.mine_synonyms(["me revisas mi caso"]) == []


def test_persistir_una_propuesta_de_analisis_no_crea_nada():
    with Session(db.engine) as s:
        creadas = synonym_miner.persist_proposals(
            s, [{"service_id": "analisis", "synonym": "revisar mi caso"}], origin="prueba",
        )
        assert creadas == 0
        assert s.exec(select(SynonymProposal)).all() == []
