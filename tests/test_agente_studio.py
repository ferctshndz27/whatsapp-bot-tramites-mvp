"""Estudio del Agente (/agente): permisos, configuración en caliente y corpus.

Reglas protegidas: solo el admin entra y muta; la configuración guardada pisa
settings al instante y sobrevive al "reinicio" (aplicar_overrides); un párrafo
del corpus con dinero/CURP/teléfono se RECHAZA al guardar (misma regla que la
salida de la capa RAG); lo guardado aparece en el corpus de rag._docs; y el
borrado es suave para no divergir del espejo MSSQL.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

import app.db as db
from app import agent_settings, rag
from app.config import settings
from app.main import app
from app.models import AgentConfig, InfoDoc, User

CLAVE = "clave-de-prueba-1234"
HASH = None  # se calcula una vez en la fixture


@pytest.fixture(autouse=True)
def entorno(_isolated_db, monkeypatch):
    global HASH
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "geoip_enabled", False)
    if HASH is None:
        import bcrypt

        HASH = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()
    with Session(db.engine) as s:
        s.add(User(username="admin", password_hash=HASH, is_admin=True))
        s.add(User(username="colab", password_hash=HASH, is_admin=False))
        s.commit()
    yield


def _sesion_de(username: str) -> TestClient:
    cliente = TestClient(app, base_url="https://testserver")
    r = cliente.post(
        "/login", data={"username": username, "password": CLAVE}, follow_redirects=False
    )
    assert r.status_code == 303
    return cliente


# ---------------------------------------------------------------------------
# Permisos
# ---------------------------------------------------------------------------


def test_sin_sesion_redirige_a_login():
    r = TestClient(app, base_url="https://testserver").get("/agente", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_colaborador_no_entra_ni_muta():
    c = _sesion_de("colab")
    assert c.get("/agente", follow_redirects=False).status_code == 303
    assert c.post("/agente/config", json={"key": "rag_enabled", "value": "1"}).status_code == 403
    assert c.post("/agente/corpus", json={"texto": "hola"}).status_code == 403


# ---------------------------------------------------------------------------
# Configuración en caliente
# ---------------------------------------------------------------------------


def test_config_valida_se_aplica_al_instante_y_persiste(monkeypatch):
    monkeypatch.setattr(settings, "rag_enabled", False)
    c = _sesion_de("admin")

    r = c.post("/agente/config", json={"key": "rag_enabled", "value": "1"})

    assert r.status_code == 200
    assert settings.rag_enabled is True  # hot-reload, sin recrear contenedor
    # Y sobrevive a un "arranque": aplicar_overrides la vuelve a pisar.
    monkeypatch.setattr(settings, "rag_enabled", False)
    with Session(db.engine) as s:
        agent_settings.aplicar_overrides(s)
    assert settings.rag_enabled is True


def test_config_invalida_se_rechaza_sin_tocar_settings(monkeypatch):
    monkeypatch.setattr(settings, "fuzzy_threshold", 85)
    c = _sesion_de("admin")

    assert c.post("/agente/config", json={"key": "fuzzy_threshold", "value": "40"}).status_code == 400
    assert c.post("/agente/config", json={"key": "anthropic_api_key", "value": "x"}).status_code == 400
    assert settings.fuzzy_threshold == 85


def test_secretos_jamas_son_configurables():
    # Candado: la allowlist no puede crecer hacia secretos/infra sin que
    # alguien borre esta prueba a sabiendas.
    for prohibida in ("anthropic_api_key", "ollama_url", "ollama_token", "mssql_password"):
        assert prohibida not in agent_settings.CONFIG_EDITABLE


def test_override_corrupto_no_tumba_el_arranque(monkeypatch):
    monkeypatch.setattr(settings, "fuzzy_threshold", 85)
    with Session(db.engine) as s:
        s.add(AgentConfig(key="fuzzy_threshold", value="basura"))
        s.commit()
        agent_settings.aplicar_overrides(s)  # no lanza
    assert settings.fuzzy_threshold == 85


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------


def test_parrafo_bueno_entra_al_corpus_de_rag():
    c = _sesion_de("admin")
    r = c.post("/agente/corpus", json={"texto": "Atendemos de lunes a viernes."})
    assert r.status_code == 200

    assert "Atendemos de lunes a viernes." in rag._docs()


def test_parrafo_con_dinero_curp_o_telefono_se_rechaza():
    c = _sesion_de("admin")
    for texto in (
        "El acta cuesta $500",
        "Tu CURP es MAHJ850315HDFRRN09",
        "Marca al 55-1234-5678",
    ):
        assert c.post("/agente/corpus", json={"texto": texto}).status_code == 400, texto
    assert rag._docs()  # el corpus del catálogo sigue vivo, sin lo rechazado


def test_borrado_es_suave_y_sale_del_corpus():
    c = _sesion_de("admin")
    doc_id = c.post("/agente/corpus", json={"texto": "Párrafo temporal."}).json()["id"]

    assert c.post(f"/agente/corpus/{doc_id}", json={"borrar": True}).status_code == 200

    with Session(db.engine) as s:
        fila = s.get(InfoDoc, doc_id)
    assert fila is not None and fila.habilitado is False  # sigue en la base (espejo)
    assert "Párrafo temporal." not in rag._docs()
