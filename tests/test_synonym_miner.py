import json
from types import SimpleNamespace

from sqlmodel import select

import app.db as db
import app.synonym_miner as miner
from app import nlu, rag
from app.catalog import Catalog
from app.config import settings
from app.conversation import NOT_RECOGNIZED_HINT
from app.models import AdminAudit, Direction, Message, User

PHONE = "5215500008888"


def _request(user: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        session={"user": user} if user else {}, url=SimpleNamespace(path="/unmatched")
    )


def _seed_unmatched(session, text: str, phone: str = PHONE) -> None:
    session.add(Message(phone=phone, direction=Direction.IN, body=text))
    session.commit()
    session.add(Message(phone=phone, direction=Direction.OUT, body=NOT_RECOGNIZED_HINT + "menú"))
    session.commit()


def test_unmatched_derives_incoming_before_hint(session):
    _seed_unmatched(session, "necesito el tramite zorglub")
    # Conversación entendida: no debe aparecer
    session.add(Message(phone="5215500001111", direction=Direction.IN, body="1"))
    session.commit()

    pending = miner.unmatched_messages(session)

    assert [m.body for m in pending] == ["necesito el tramite zorglub"]


def test_unmatched_dedupes_and_skips_attachments(session):
    _seed_unmatched(session, "tramite zorglub raro")
    _seed_unmatched(session, "tramite zorglub raro")  # duplicado
    _seed_unmatched(session, "[archivo adjunto]")  # adjunto: fuera

    pending = miner.unmatched_messages(session)

    assert len(pending) == 1


def test_unmatched_excludes_texts_the_engine_now_understands(session):
    # "acta de divorcio" ya lo entiende el motor de keywords → no es pendiente
    _seed_unmatched(session, "acta de divorcio")
    _seed_unmatched(session, "tramite zorglub imposible")

    pending = miner.unmatched_messages(session)

    assert [m.body for m in pending] == ["tramite zorglub imposible"]


def _fake_client(payload: dict):
    response = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
    )
    return SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: response))


def test_mine_validates_against_catalog(monkeypatch):
    svc = next(s for c in miner.catalog.categories for s in c.services)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(
        nlu,
        "_client",
        _fake_client(
            {
                "proposals": [
                    {"service_id": svc.id, "synonym": "Papelito Azul"},
                    {"service_id": "alucinado_xyz", "synonym": "no debe pasar"},
                    {"service_id": svc.id, "synonym": "papelito azul"},  # duplicado
                ]
            }
        ),
    )

    proposals = miner.mine_synonyms(["mensaje de prueba"])

    assert proposals == [
        {"service_id": svc.id, "service_name": svc.name, "synonym": "papelito azul"}
    ]


def test_mine_usa_ollama_cuando_esta_configurado(monkeypatch):
    # Sin API de Anthropic el minero corre sobre el Ollama de labs — el ciclo
    # de aprendizaje nocturno no depende de una API de pago.
    svc = next(s for c in miner.catalog.categories for s in c.services)
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "ollama_url", "http://labs.fake")
    monkeypatch.setattr(
        nlu,
        "ollama_chat",
        lambda messages, **kw: {
            "content": json.dumps(
                {"proposals": [{"service_id": svc.id, "synonym": "papelito azul"}]}
            )
        },
    )

    proposals = miner.mine_synonyms(["mensaje de prueba"])

    assert proposals == [
        {"service_id": svc.id, "service_name": svc.name, "synonym": "papelito azul"}
    ]


def test_la_mineria_con_ollama_no_hereda_el_timeout_ni_el_contexto_del_clasificador(monkeypatch):
    """La minería manda el catálogo completo más hasta 100 mensajes: en el peor
    caso ~7,800 tokens contando la salida. Con el contexto default de Ollama
    (4096) ese prompt se TRUNCA en silencio, y con el timeout del clasificador
    (12-25 s) la CPU de labs no alcanza a contestar: el 2026-09-12 los logs de
    Ollama mostraron un 500 a los 25 s justo a las 03:30. Si esto se pone rojo,
    la minería nocturna vuelve a morir cada noche sin una sola propuesta, y
    nadie se entera: el job solo deja un warning."""
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "ollama_url", "http://labs.fake")
    capturado = {}

    def _ollama_falso(messages, **kw):
        capturado.update(kw)
        return {"content": json.dumps({"proposals": []})}

    monkeypatch.setattr(nlu, "ollama_chat", _ollama_falso)

    miner.mine_synonyms(["mensaje de prueba"])

    assert capturado.get("num_ctx") == rag.OLLAMA_NUM_CTX, "sin num_ctx Ollama trunca el prompt"
    assert capturado.get("timeout") == miner.OLLAMA_TIMEOUT_MINERIA, (
        "la minería heredó el timeout corto del clasificador"
    )
    # OLLAMA_TIMEOUT tope 60 s (app/config.py, app/agent_settings.py): la
    # minería no puede depender de que alguien suba esa perilla.
    assert miner.OLLAMA_TIMEOUT_MINERIA > 60.0, "el techo de la minería cabe en la perilla del clasificador"


def test_mine_with_no_texts_skips_api(monkeypatch):
    monkeypatch.setattr(nlu, "_client", None)  # si llamara al API, explotaría
    assert miner.mine_synonyms([]) == []


def _tmp_catalog(tmp_path) -> Catalog:
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps(
            {
                "categories": [
                    {
                        "id": "basicos",
                        "name": "Servicios básicos",
                        "services": [
                            {"id": "curp", "name": "CURP certificada", "price_info": "$80"}
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return Catalog(path)


def test_approve_synonym_persists_and_audits(monkeypatch, session, tmp_path):
    monkeypatch.setattr(db, "engine", session.get_bind())
    cat = _tmp_catalog(tmp_path)
    monkeypatch.setattr(miner, "catalog", cat)
    session.add(User(username="jefa", password_hash="x", is_admin=True))
    session.commit()

    miner.unmatched_add_synonym(_request("jefa"), service_id="curp", synonym="clave única")

    assert cat.service_by_id("curp").synonyms == ("clave única",)
    audit = session.exec(select(AdminAudit)).one()
    assert audit.action == "catalogo:sinonimo"
    assert audit.target == "curp:clave única"

    # duplicado: rechazado con error, sin segunda entrada
    response = miner.unmatched_add_synonym(
        _request("jefa"), service_id="curp", synonym="Clave Única"
    )
    assert "ya existe" in response.context["error"]


def test_non_admin_redirected(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    session.add(User(username="colab", password_hash="x", is_admin=False))
    session.commit()

    response = miner.unmatched_view(_request("colab"))

    assert response.headers["location"] == "/"
