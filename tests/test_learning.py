"""Fase 2: propuestas persistidas, clustering de no-entendidos y extractores."""

from types import SimpleNamespace

from sqlmodel import select

import app.db as db
import app.nlu as nlu
import app.synonym_miner as miner
from app.catalog import catalog
from app.conversation import handle_incoming
from app.jobs import nightly
from app.models import ClientNote, NluEvent, SynonymProposal, User

PHONE = "5215511112222"


def _svc():
    return catalog.categories[0].services[0]


def _request(user: str) -> SimpleNamespace:
    # base.html usa request.url.path para marcar el menú activo.
    return SimpleNamespace(session={"user": user}, url=SimpleNamespace(path="/unmatched"))


def test_persist_proposals_dedupes(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    svc = _svc()
    catalog.add_synonym(svc.id, "ya en catalogo")
    proposals = [
        {"service_id": svc.id, "synonym": "propuesta nueva"},
        {"service_id": svc.id, "synonym": "Ya En Catalogo"},   # el catálogo ya la cubre
        {"service_id": svc.id, "synonym": "propuesta nueva"},  # repetida en el lote
        {"service_id": "no-existe", "synonym": "x"},           # id alucinado: fuera
    ]

    created = miner.persist_proposals(session, proposals, origin="claude")
    session.commit()
    created_again = miner.persist_proposals(session, proposals, origin="claude")

    assert created == 1
    assert created_again == 0
    row = session.exec(select(SynonymProposal)).one()
    assert (row.synonym, row.status, row.origin) == ("propuesta nueva", "pendiente", "claude")


def test_approve_proposal_adds_synonym_and_audits_decision(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    session.add(User(username="jefa", password_hash="x", is_admin=True))
    svc = _svc()
    session.add(SynonymProposal(service_id=svc.id, synonym="tramite expres"))
    session.commit()
    prop_id = session.exec(select(SynonymProposal)).one().id

    miner.proposal_approve(_request("jefa"), prop_id)

    session.expire_all()  # la ruta escribió con su propia sesión
    row = session.exec(select(SynonymProposal)).one()
    assert row.status == "aprobada"
    assert row.decided_by == "jefa"
    assert row.decided_at is not None
    assert "tramite expres" in [s.lower() for s in catalog.service_by_id(svc.id).synonyms]


def test_reject_proposal_leaves_catalog_untouched(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    session.add(User(username="jefa", password_hash="x", is_admin=True))
    svc = _svc()
    session.add(SynonymProposal(service_id=svc.id, synonym="no me convence"))
    session.commit()
    prop_id = session.exec(select(SynonymProposal)).one().id

    miner.proposal_reject(_request("jefa"), prop_id)

    session.expire_all()  # la ruta escribió con su propia sesión
    row = session.exec(select(SynonymProposal)).one()
    assert row.status == "rechazada"
    assert row.decided_by == "jefa"
    assert "no me convence" not in [s.lower() for s in catalog.service_by_id(svc.id).synonyms]


def test_cluster_groups_similar_texts(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    for text in [
        "constancia de semanas cotizadas",
        "constancia semanas cotizadas",
        "CONSTANCIA de semanas cotizadas!!",
        "quiero renovar mi pasaporte",
    ]:
        session.add(NluEvent(phone=PHONE, text=text, capa="ninguna"))
    session.add(NluEvent(phone=PHONE, text="acta de nacimiento", capa="nombre"))  # entendido: no cuenta
    session.commit()

    clusters = miner.cluster_unmatched()

    assert clusters[0]["count"] == 3  # las tres variantes se agrupan
    assert "semanas" in clusters[0]["rep"]
    assert any("pasaporte" in c["rep"] for c in clusters)


def test_ignorar_grupo_es_soft_delete_y_sale_de_las_estadisticas(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    session.add(User(username="jefa", password_hash="x", is_admin=True))
    for text in ["hola buen dia", "hola buenos dias", "quiero renovar mi pasaporte"]:
        session.add(NluEvent(phone=PHONE, text=text, capa="ninguna"))
    session.commit()

    miner.cluster_ignore(_request("jefa"), synonym="hola buen dia")

    session.expire_all()  # la ruta escribió con su propia sesión
    eventos = session.exec(select(NluEvent)).all()
    ignorados = [e for e in eventos if not e.habilitado]
    assert len(eventos) == 3            # nada se borró de la base
    assert len(ignorados) == 2          # las dos variantes del saludo
    assert [c["rep"] for c in miner.cluster_unmatched()] == ["quiero renovar mi pasaporte"]
    assert miner.nlu_metrics()["total"] == 1  # los ignorados no cuentan


def test_extracted_data_becomes_client_notes_once(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(nlu.settings, "anthropic_api_key", "")

    handle_incoming(session, PHONE, "mi curp es GOMC900514HJCNRR07 y mi correo juan@mail.com")
    handle_incoming(session, PHONE, "les repito: GOMC900514HJCNRR07 juan@mail.com")

    notes = session.exec(select(ClientNote)).all()
    bodies = " | ".join(n.body for n in notes)
    assert len(notes) == 2  # CURP y Email, sin duplicados en el reenvío
    assert "GOMC900514HJCNRR07" in bodies
    assert "juan@mail.com" in bodies
    assert all(n.author == "agente-ia" for n in notes)


def test_nightly_without_pending_messages_is_noop(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())

    result = nightly()

    assert result["propuestas"] == 0
    assert session.exec(select(SynonymProposal)).all() == []
