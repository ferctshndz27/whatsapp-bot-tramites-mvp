import json
from types import SimpleNamespace

from sqlmodel import select

import app.catalog_admin as catalog_admin
import app.db as db
from app.catalog import Catalog
from app.models import AdminAudit, User

SEED = {
    "categories": [
        {
            "id": "basicos",
            "name": "Servicios básicos",
            "services": [
                {"id": "curp", "name": "CURP certificada", "price_info": "$80"},
                {"id": "rfc", "name": "Constancia RFC", "price_info": "$100"},
            ],
        }
    ]
}


def _make_catalog(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(SEED), encoding="utf-8")
    return Catalog(path), path


def _request(user: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        session={"user": user} if user else {}, url=SimpleNamespace(path="/catalog"),
        query_params={},
    )


def test_hidden_service_disappears_from_bot_menu(tmp_path):
    cat, _ = _make_catalog(tmp_path)

    assert cat.toggle_service("curp") is None

    menu = cat.format_service_menu("basicos")
    assert "CURP" not in menu
    assert "Constancia RFC" in menu
    assert cat.service_by_id("curp") is None  # el bot ya no lo puede elegir


def test_disabled_category_disappears_from_menu(tmp_path):
    cat, _ = _make_catalog(tmp_path)

    cat.toggle_category("basicos")

    assert "Servicios básicos" not in cat.format_category_menu()


def test_price_update_is_hot_reloaded_from_disk(tmp_path):
    cat, path = _make_catalog(tmp_path)
    other = Catalog(path)  # otra instancia (otro proceso hipotético)

    cat.update_service("curp", "CURP certificada", "$120")

    assert other.service_by_id("curp").price_info == "$120"  # recarga por mtime


def test_add_duplicate_service_rejected(tmp_path):
    cat, _ = _make_catalog(tmp_path)

    assert cat.add_service("basicos", "Acta de nacimiento", "$90") is None
    error = cat.add_service("basicos", "Acta de nacimiento", "$99")  # mismo slug

    assert error is not None


def test_add_category_and_service(tmp_path):
    cat, _ = _make_catalog(tmp_path)

    assert cat.add_category("Trámites de auto") is None
    assert cat.add_service("tramites_de_auto", "Tenencia", "$450") is None

    assert cat.service_by_id("tenencia").category_name == "Trámites de auto"


def test_non_admin_route_redirected(monkeypatch, session, tmp_path):
    monkeypatch.setattr(db, "engine", session.get_bind())
    cat, _ = _make_catalog(tmp_path)
    monkeypatch.setattr(catalog_admin, "catalog", cat)
    session.add(User(username="colab", password_hash="x", is_admin=False))
    session.commit()

    response = catalog_admin.service_update(
        _request("colab"), "curp", name="Hack", price_info="$0", synonyms="", requirements=""
    )

    assert response.headers["location"] == "/"
    assert cat.service_by_id("curp").name == "CURP certificada"  # sin cambios


def test_admin_update_persists_and_audits(monkeypatch, session, tmp_path):
    monkeypatch.setattr(db, "engine", session.get_bind())
    cat, path = _make_catalog(tmp_path)
    monkeypatch.setattr(catalog_admin, "catalog", cat)
    session.add(User(username="jefa", password_hash="x", is_admin=True))
    session.commit()

    response = catalog_admin.service_update(
        _request("jefa"), "curp", name="CURP exprés", price_info="$150", synonyms="", requirements="INE vigente"
    )

    assert response.status_code == 303
    saved = json.loads(path.read_text(encoding="utf-8"))
    svc = saved["categories"][0]["services"][0]
    assert (svc["name"], svc["price_info"]) == ("CURP exprés", "$150")
    audit = session.exec(select(AdminAudit)).one()
    assert audit.action == "catalogo:servicio_editado"
    assert audit.target == "curp"


def test_synonyms_persist_via_admin_route(monkeypatch, session, tmp_path):
    monkeypatch.setattr(db, "engine", session.get_bind())
    cat, path = _make_catalog(tmp_path)
    monkeypatch.setattr(catalog_admin, "catalog", cat)
    session.add(User(username="jefa", password_hash="x", is_admin=True))
    session.commit()

    catalog_admin.service_update(
        _request("jefa"), "curp", name="CURP certificada", price_info="$80",
        synonyms="clave única, curp certificada , ", requirements="",
    )

    saved = json.loads(path.read_text(encoding="utf-8"))
    svc = saved["categories"][0]["services"][0]
    assert svc["synonyms"] == ["clave única", "curp certificada"]  # limpia vacíos/espacios
    assert cat.service_by_id("curp").synonyms == ("clave única", "curp certificada")
