from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlmodel import select

import app.db as db
from app.activity import (
    BUSINESS_TZ,
    _period_starts_utc,
    activity_view,
    backfill_closed_tickets,
    dispatch_counts,
)
from app.dashboard import claim_ticket, update_ticket_status
from app.models import Ticket, TicketActivity, TicketStatus, User, utcnow


def _request(user: str) -> SimpleNamespace:
    return SimpleNamespace(session={"user": user}, url=SimpleNamespace(path="/activity"))


def _make_ticket(session, **kwargs) -> Ticket:
    ticket = Ticket(
        phone="5215500007777",
        category="Servicios básicos",
        service_id="curp",
        service_name="CURP certificada",
        price_info="$80",
        **kwargs,
    )
    session.add(ticket)
    session.commit()
    session.refresh(ticket)
    return ticket


def _closure(username: str, hours_ago: float) -> TicketActivity:
    return TicketActivity(
        ticket_id=1,
        username=username,
        action="cerrado",
        created_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
    )


def test_claim_and_close_are_logged(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    ticket = _make_ticket(session)

    claim_ticket(_request("ana"), ticket.id)
    update_ticket_status(_request("ana"), ticket.id, status="cerrado")

    actions = [a.action for a in session.exec(select(TicketActivity)).all()]
    assert actions == ["tomado", "cerrado"]


def test_unchanged_status_logs_nothing(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    ticket = _make_ticket(session, assigned_to="ana", status=TicketStatus.TOMADO)

    update_ticket_status(_request("ana"), ticket.id, status="tomado")

    assert session.exec(select(TicketActivity)).all() == []


def test_dispatch_counts_by_period(session):
    starts = _period_starts_utc()
    session.add(_closure("ana", hours_ago=1))      # hoy
    session.add(_closure("ana", hours_ago=24 * 40))  # hace 40 días: solo total
    session.add(_closure("bob", hours_ago=2))
    session.commit()

    counts = dispatch_counts(session, starts)

    assert counts["ana"]["day"] == 1
    assert counts["ana"]["total"] == 2
    assert counts["bob"]["day"] == 1
    assert counts["bob"]["total"] == 1


def test_period_starts_are_ordered():
    starts = _period_starts_utc(datetime(2026, 7, 15, 12, 0, tzinfo=BUSINESS_TZ))
    assert starts["month"] <= starts["week"] <= starts["day"]


def test_backfill_creates_synthetic_closures_once(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _make_ticket(session, assigned_to="ana", status=TicketStatus.CERRADO)
    _make_ticket(session, assigned_to=None, status=TicketStatus.CERRADO)  # sin asignar: no cuenta

    backfill_closed_tickets()
    backfill_closed_tickets()  # idempotente

    acts = session.exec(select(TicketActivity)).all()
    assert len(acts) == 1
    assert acts[0].username == "ana"
    assert acts[0].action == "cerrado"


def test_non_admin_cannot_view_others_history(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    session.add(User(username="colab", password_hash="x", is_admin=False))
    session.add(_closure("otro", hours_ago=1))
    session.commit()

    response = activity_view(_request("colab"), user="otro")

    assert response.context["viewing"] == "colab"  # ignoró el parámetro
    assert response.context["team"] is None


def test_admin_sees_team_and_can_view_others(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    session.add(User(username="jefa", password_hash="x", is_admin=True))
    session.add(User(username="colab", password_hash="x", is_admin=False))
    session.add(_closure("colab", hours_ago=1))
    session.commit()

    response = activity_view(_request("jefa"), user="colab")

    assert response.context["viewing"] == "colab"
    team = {m["username"]: m for m in response.context["team"]}
    assert team["colab"]["day"] == 1
    assert "jefa" in team
