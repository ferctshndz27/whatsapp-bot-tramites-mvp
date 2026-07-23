from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlmodel import select

import app.db as db
from app.activity import (
    BUSINESS_TZ,
    _period_starts_utc,
    activity_view,
    backfill_closed_tickets,
    daily_dispatches,
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
    # hours_ago=0 (ahora mismo): siempre cae en "hoy" sin importar la hora local
    # — con hours_ago=1 el test fallaba si se corría justo después de medianoche.
    session.add(_closure("ana", hours_ago=0))      # hoy
    session.add(_closure("ana", hours_ago=24 * 40))  # hace 40 días: solo total
    session.add(_closure("bob", hours_ago=0))
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


def test_daily_dispatches_buckets_by_business_day(session):
    today = datetime.now(BUSINESS_TZ).date()
    session.add(_closure("ana", hours_ago=0))
    session.add(_closure("bob", hours_ago=0))
    session.add(_closure("ana", hours_ago=24 * 400))  # fuera del rango
    session.commit()

    days = daily_dispatches(session, today - timedelta(days=1), today)

    assert len(days) == 2  # un item por día del rango, sin huecos
    assert days[0]["total"] == 0
    assert days[1]["date"] == today
    assert days[1]["total"] == 2
    assert days[1]["by_user"] == {"ana": 1, "bob": 1}


def test_daily_dispatches_filters_by_user(session):
    today = datetime.now(BUSINESS_TZ).date()
    session.add(_closure("ana", hours_ago=0))
    session.add(_closure("bob", hours_ago=0))
    session.commit()

    days = daily_dispatches(session, today, today, username="ana")

    assert days[0]["by_user"] == {"ana": 1}


def test_chart_defaults_to_today(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    session.add(User(username="jefa", password_hash="x", is_admin=True))
    session.add(_closure("jefa", hours_ago=0))
    session.commit()

    response = activity_view(_request("jefa"))

    today = datetime.now(BUSINESS_TZ).date().isoformat()
    assert response.context["desde"] == today
    assert response.context["hasta"] == today
    assert len(response.context["chart"]) == 1
    assert response.context["chart"][0]["total"] == 1
    assert response.context["chart_colors"] == {"jefa": response.context["chart_colors"]["jefa"]}


def test_chart_respects_filter_and_swaps_inverted_range(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    session.add(User(username="jefa", password_hash="x", is_admin=True))
    session.commit()

    response = activity_view(_request("jefa"), desde="2026-07-20", hasta="2026-07-10")

    assert response.context["desde"] == "2026-07-10"  # rango invertido se corrige
    assert response.context["hasta"] == "2026-07-20"
    assert len(response.context["chart"]) == 11
    assert "Aplicar filtro" in response.body.decode()


def test_chart_invalid_date_falls_back_to_today(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    session.add(User(username="colab", password_hash="x", is_admin=False))
    session.commit()

    response = activity_view(_request("colab"), desde="no-es-fecha", hasta="")

    today = datetime.now(BUSINESS_TZ).date().isoformat()
    assert response.context["desde"] == today == response.context["hasta"]


def test_non_admin_chart_only_shows_own_dispatches(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    session.add(User(username="colab", password_hash="x", is_admin=False))
    session.add(_closure("colab", hours_ago=0))
    session.add(_closure("otra_persona", hours_ago=0))
    session.commit()

    response = activity_view(_request("colab"))

    assert response.context["chart"][0]["by_user"] == {"colab": 1}


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
    # hours_ago=0: igual que test_dispatch_counts_by_period — con 1h el test
    # fallaba corriendo justo después de medianoche (hora de negocio).
    session.add(_closure("colab", hours_ago=0))
    session.commit()

    response = activity_view(_request("jefa"), user="colab")

    assert response.context["viewing"] == "colab"
    team = {m["username"]: m for m in response.context["team"]}
    assert team["colab"]["day"] == 1
    assert "jefa" in team
