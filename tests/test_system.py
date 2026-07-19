from types import SimpleNamespace

from sqlmodel import select

import app.db as db
import app.system as system
from app.models import AdminAudit, Ticket, User


def _request(user: str | None, path: str = "/system") -> SimpleNamespace:
    return SimpleNamespace(
        session={"user": user} if user else {}, url=SimpleNamespace(path=path),
        query_params={},
    )


def _make_admin(session, username: str = "jefa") -> None:
    session.add(User(username=username, password_hash="x", is_admin=True))
    session.commit()


def _make_ticket(session) -> Ticket:
    ticket = Ticket(
        phone="5215500001111", category="Servicios básicos", service_id="curp",
        service_name="CURP certificada", price_info="$80", client_name="María",
    )
    session.add(ticket)
    session.commit()
    session.refresh(ticket)
    return ticket


def test_mirror_status_reports_desync_when_mssql_off(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _make_admin(session)

    status = {row["table"]: row for row in system.mirror_status()}

    assert status["app_users"]["sqlite"] == 1
    assert status["app_users"]["mssql"] is None  # sin conexión en tests
    assert status["app_users"]["ok"] is False


def test_sync_runs_backfill_and_audits(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _make_admin(session)
    called = []
    monkeypatch.setattr(system.archive, "backfill", lambda: called.append(True))

    response = system.system_sync(_request("jefa"))

    assert called == [True]
    assert response.headers["location"] == "/system?synced=1"
    audit = session.exec(select(AdminAudit)).one()
    assert audit.action == "espejo:sync"
    assert audit.admin == "jefa"


def test_audit_view_lists_entries_newest_first(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _make_admin(session)
    session.add(AdminAudit(admin="jefa", action="usuario:creado", target="a"))
    session.add(AdminAudit(admin="jefa", action="usuario:deshabilitado", target="b"))
    session.commit()

    response = system.audit_view(_request("jefa", "/audit"))

    actions = [e.action for e in response.context["entries"]]
    assert len(actions) == 2


def test_export_tickets_csv_has_bom_and_rows(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _make_admin(session)
    _make_ticket(session)

    response = system.export_tickets(_request("jefa"))
    body = response.body.decode("utf-8")

    assert body.startswith("﻿")  # BOM UTF-8 para Excel
    assert "CURP certificada" in body
    assert "María" in body


def test_export_redirects_non_admin(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    session.add(User(username="colab", password_hash="x", is_admin=False))
    session.commit()

    response = system.export_tickets(_request("colab"))

    assert response.headers["location"] == "/"
