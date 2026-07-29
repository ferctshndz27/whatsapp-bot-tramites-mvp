"""Fase 0 del agente: bitácora nluevent y métricas del panel Agente IA."""

from sqlmodel import select

import app.db as db
import app.nlu as nlu
from app.conversation import handle_incoming
from app.models import NluEvent
from app.synonym_miner import nlu_metrics

PHONE = "5215511112222"


def _keywords_only(monkeypatch):
    monkeypatch.setattr(nlu.settings, "anthropic_api_key", "")


def test_free_text_deduction_is_logged(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _keywords_only(monkeypatch)

    handle_incoming(session, PHONE, "necesito un acta de nacimiento por favor")

    event = session.exec(select(NluEvent)).one()
    assert event.capa == "nombre"  # acertó por el nombre oficial del servicio
    assert event.service_id and "nacimiento" in event.service_id
    assert event.termino_en_ticket is False


def test_confirmed_ticket_marks_event_as_converted(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _keywords_only(monkeypatch)

    handle_incoming(session, PHONE, "necesito un acta de nacimiento")
    handle_incoming(session, PHONE, "si")

    event = session.exec(select(NluEvent)).one()
    assert event.termino_en_ticket is True


def test_not_understood_logs_capa_ninguna(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _keywords_only(monkeypatch)

    handle_incoming(session, PHONE, "zzzz qqqq wwww")

    event = session.exec(select(NluEvent)).one()
    assert event.capa == "ninguna"
    assert event.service_id is None


def test_courtesy_words_are_not_logged(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _keywords_only(monkeypatch)

    handle_incoming(session, PHONE, "hola")     # saludo: no es deducción
    handle_incoming(session, PHONE, "gracias")  # cortesía: no se registra

    assert session.exec(select(NluEvent)).all() == []


def test_saludos_y_despedidas_compuestos_no_son_tramite(monkeypatch, session):
    """El bug del panel: "hola buen dia" caía como 'no entendido' y el panel
    ofrecía convertirlo en sinónimo de un servicio."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    _keywords_only(monkeypatch)

    saludo = handle_incoming(session, PHONE, "hola buen dia")
    despedida = handle_incoming(session, PHONE, "muchas gracias, hasta luego 👋")

    assert session.exec(select(NluEvent)).all() == []  # ninguno es deducción
    assert "hola" in saludo[0].lower()
    assert "gracias a ti" in despedida[0].lower()


def test_saludo_con_tramite_si_deduce_servicio(monkeypatch, session):
    """El recorte de frases sociales no puede tragarse una petición real."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    _keywords_only(monkeypatch)

    handle_incoming(session, PHONE, "hola buen dia, necesito un acta de nacimiento")

    event = session.exec(select(NluEvent)).one()
    assert event.capa == "nombre"
    assert event.service_id and "nacimiento" in event.service_id


def _controlled_catalog(monkeypatch, tmp_path):
    """Catálogo propio del test: capas verificables sin depender de los
    sinónimos que existan en la semilla del repo o en producción."""
    import json

    from app.catalog import Catalog

    seed = {
        "categories": [
            {
                "id": "c",
                "name": "Servicios",
                "services": [
                    {
                        "id": "cfe",
                        "name": "Descarga de Recibo CFE",
                        "price_info": "$10",
                        "synonyms": ["recibo de luz"],
                    },
                    {"id": "acta", "name": "Acta de Nacimiento", "price_info": "$35"},
                ],
            }
        ]
    }
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(seed), encoding="utf-8")
    cat = Catalog(path)
    monkeypatch.setattr(nlu, "catalog", cat)
    return cat


def test_synonym_layer_is_distinguished_from_name(monkeypatch, tmp_path):
    _controlled_catalog(monkeypatch, tmp_path)
    _keywords_only(monkeypatch)

    _, _, capa_syn, _ = nlu.deduce_explained("me pasas mi recibo de luz")
    _, _, capa_nom, _ = nlu.deduce_explained("quiero la descarga de recibo cfe")

    assert capa_syn == "sinonimo"  # acertó por sinónimo aprendido
    assert capa_nom == "nombre"    # acertó por el nombre oficial


def test_fuzzy_layer_reports_confidence(monkeypatch, tmp_path):
    _controlled_catalog(monkeypatch, tmp_path)
    _keywords_only(monkeypatch)

    # Error de dedo ("sfe" por "cfe"): debe ganar la capa difusa con score alto.
    service, _, capa, confianza = nlu.deduce_explained("quiero mi descarga de recibo sfe")

    assert service is not None and service.id == "cfe"
    assert capa == "fuzzy"
    assert confianza is not None and confianza >= 0.85


def test_metrics_summarize_events(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    session.add(NluEvent(phone="p1", text="a", capa="nombre", termino_en_ticket=True))
    session.add(NluEvent(phone="p2", text="b", capa="ninguna"))
    session.commit()

    metrics = nlu_metrics()

    assert metrics["total"] == 2
    assert metrics["entendidos_pct"] == 50
    assert metrics["ticket_pct"] == 50
    assert metrics["donut"]["total"] == 2
    assert {s["user"] for s in metrics["donut"]["segments"]} == {"nombre", "ninguna"}
