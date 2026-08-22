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


def test_admin_ve_la_pagina_con_el_pipeline():
    c = _sesion_de("admin")
    r = c.get("/agente")
    assert r.status_code == 200
    # El estado viaja embebido: nodos del pipeline y la librería vendoreada.
    assert "Estudio del Agente" in r.text
    assert "Capa informativa (RAG)" in r.text
    assert "/static/vendor/joint.min.js" in r.text


# ---------------------------------------------------------------------------
# Probador (el "Run" del estudio)
# ---------------------------------------------------------------------------


def test_probar_corre_la_cascada_en_seco_sin_dejar_rastro():
    from sqlmodel import select

    from app.models import NluEvent

    c = _sesion_de("admin")
    r = c.post("/agente/probar", json={"texto": "quiero mi acta de nacimiento"})

    assert r.status_code == 200
    datos = r.json()
    assert datos["capa"] in ("nombre", "sinonimo", "keyword")
    assert datos["servicio"] is not None
    # Banco de pruebas, no tráfico: cero filas en la bitácora del agente.
    with Session(db.engine) as s:
        assert s.exec(select(NluEvent)).first() is None


def test_probar_es_solo_para_admins():
    c = _sesion_de("colab")
    assert c.post("/agente/probar", json={"texto": "hola"}).status_code == 403


# ---------------------------------------------------------------------------
# Match literal: sinónimos del catálogo
#
# La capa literal no tiene flags — la alimenta el catálogo. Lo que se protege
# aquí: solo el admin los toca, el cambio pasa por app.catalog (con su
# auditoría y su caché refrescada) y el NLU empata con el sinónimo nuevo.
# ---------------------------------------------------------------------------


def _primer_servicio():
    from app.catalog import catalog

    return catalog.categories[0].services[0]


def test_colaborador_no_ve_ni_toca_sinonimos():
    c = _sesion_de("colab")
    assert c.get("/agente/sinonimos").status_code == 403
    assert c.post(
        "/agente/sinonimos", json={"service_id": _primer_servicio().id, "texto": "x"}
    ).status_code == 403


def test_listado_trae_servicios_con_sus_sinonimos():
    servicio = _primer_servicio()
    datos = _sesion_de("admin").get("/agente/sinonimos").json()
    fila = next(s for s in datos["servicios"] if s["id"] == servicio.id)
    assert fila["nombre"] == servicio.name and fila["sinonimos"] == list(servicio.synonyms)


def test_sinonimo_agregado_lo_empata_el_nlu():
    from app import nlu

    servicio = _primer_servicio()
    c = _sesion_de("admin")

    r = c.post("/agente/sinonimos", json={"service_id": servicio.id, "texto": "papelito magico"})

    assert r.status_code == 200
    encontrado, _, capa, _ = nlu.deduce_explained("necesito el papelito magico")
    assert encontrado is not None and encontrado.id == servicio.id and capa == "sinonimo"


def test_sinonimo_quitado_deja_de_empatar():
    from app import nlu

    servicio = _primer_servicio()
    c = _sesion_de("admin")
    c.post("/agente/sinonimos", json={"service_id": servicio.id, "texto": "papelito magico"})

    r = c.post(
        "/agente/sinonimos",
        json={"service_id": servicio.id, "texto": "papelito magico", "quitar": True},
    )

    assert r.status_code == 200
    encontrado, _, _, _ = nlu.deduce_explained("necesito el papelito magico")
    assert encontrado is None


def test_el_cambio_de_sinonimos_queda_auditado():
    from app.models import CatalogAudit

    servicio = _primer_servicio()
    _sesion_de("admin").post(
        "/agente/sinonimos", json={"service_id": servicio.id, "texto": "papelito magico"}
    )

    with Session(db.engine) as s:
        from sqlmodel import select

        filas = s.exec(select(CatalogAudit).where(CatalogAudit.field == "synonyms")).all()
    assert filas and "papelito magico" in filas[-1].new_value


def test_sinonimo_invalido_o_servicio_inexistente_es_400():
    c = _sesion_de("admin")
    assert c.post(
        "/agente/sinonimos", json={"service_id": _primer_servicio().id, "texto": "  "}
    ).status_code == 400
    assert c.post(
        "/agente/sinonimos", json={"service_id": "no-existe", "texto": "algo"}
    ).status_code == 400


# ---------------------------------------------------------------------------
# Capa Claude apagada (decisión del negocio: todo corre sobre Ollama)
# ---------------------------------------------------------------------------


def test_la_llave_de_anthropic_se_ignora_aunque_este_en_el_entorno(monkeypatch):
    # El apagador vive donde NACE la llave: así los siete gates que ya existen
    # (`if settings.anthropic_api_key`) quedan cerrados sin repetir condiciones.
    # Sin esto, poner la llave en el .env del VPS haría que el bot empezara a
    # pagar API sin que nadie lo decidiera.
    from app import config

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-de-mentiras")
    assert config.CLAUDE_HABILITADO is False, "el apagador de la capa Claude cambió"
    assert config.Settings().anthropic_api_key == ""


def test_el_lienzo_ya_no_dibuja_el_nodo_de_claude():
    datos = _sesion_de("admin").get("/agente")

    assert datos.status_code == 200
    with Session(db.engine) as s:
        from app.agente_studio import _estado

        nodos = {n["id"] for n in _estado(s)["nodos"]}
    assert "llm" not in nodos
    # Y el resto de la cascada sigue completa: quitar un nodo no puede borrar
    # los demás por accidente.
    assert {"literal", "ollama", "fuzzy", "rag", "tools", "escalada"} <= nodos
