"""Fase 4b — Salud del agente: evaluación de reglas, upsert idempotente,
resolución de alertas, nivel de salud y visibilidad admin-only en /unmatched."""

from types import SimpleNamespace

from sqlmodel import select

import app.alerts as alerts
import app.db as db
import app.synonym_miner as miner
import app.system as system
from app.models import Alert, NluEvent, SynonymProposal, User, utcnow


def _healthy_infra(monkeypatch):
    """Neutraliza las reglas de respaldo y disco (leen del sistema de archivos
    real) para que solo la regla bajo prueba pueda disparar."""
    monkeypatch.setattr(system, "_backup_info", lambda: {"exists": True, "age_hours": 1.0, "stale": False})
    monkeypatch.setattr(system, "_disk_free", lambda: {"free_pct": 80, "free_gb": 100.0, "low": False})


def _seed_no_entendidos(session, n: int, capa: str = "ninguna") -> None:
    for i in range(n):
        session.add(NluEvent(phone="p", text=f"tramite raro {i}", capa=capa))
    session.commit()


def _request(user: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        session={"user": user} if user else {}, url=SimpleNamespace(path="/unmatched")
    )


def test_evaluate_creates_alert_when_rule_fires(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _healthy_infra(monkeypatch)
    # 20 eventos, todos "ninguna" → 100% no entendidos ≥ 40 (critical).
    _seed_no_entendidos(session, 20)

    result = alerts.evaluate_alerts()

    session.expire_all()
    rows = session.exec(select(Alert).where(Alert.code == "no_entendidos_alto")).all()
    assert len(rows) == 1
    alert = rows[0]
    assert alert.active is True
    assert alert.level == "critical"  # 100% ≥ 40
    assert alert.metric_value == 100.0
    assert result["nuevas"] == 1


def test_below_min_events_does_not_fire(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _healthy_infra(monkeypatch)
    # 19 eventos < 20: la regla no evalúa aunque el 100% no se entienda.
    _seed_no_entendidos(session, 19)

    alerts.evaluate_alerts()

    session.expire_all()
    assert session.exec(select(Alert)).all() == []


def test_second_run_does_not_duplicate(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _healthy_infra(monkeypatch)
    _seed_no_entendidos(session, 20)

    alerts.evaluate_alerts()
    result = alerts.evaluate_alerts()  # misma condición: actualiza, no duplica

    session.expire_all()
    rows = session.exec(select(Alert).where(Alert.active == True)).all()  # noqa: E712
    assert len(rows) == 1
    assert result["nuevas"] == 0
    assert result["actualizadas"] == 1


def test_alert_resolves_when_condition_disappears(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _healthy_infra(monkeypatch)
    _seed_no_entendidos(session, 20)
    alerts.evaluate_alerts()

    # La condición desaparece: eventos ahora entendidos (capa distinta de "ninguna").
    for e in session.exec(select(NluEvent)).all():
        e.capa = "nombre"
        session.add(e)
    session.commit()

    alerts.evaluate_alerts()

    session.expire_all()
    alert = session.exec(select(Alert).where(Alert.code == "no_entendidos_alto")).one()
    assert alert.active is False
    assert alert.resolved_at is not None


def test_pending_proposals_rule_fires_at_threshold(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _healthy_infra(monkeypatch)
    for i in range(10):
        session.add(SynonymProposal(service_id="svc", synonym=f"s{i}", status="pendiente"))
    session.commit()

    alerts.evaluate_alerts()

    session.expire_all()
    alert = session.exec(select(Alert).where(Alert.code == "propuestas_pendientes")).one()
    assert alert.active is True
    assert alert.level == "warn"
    assert alert.metric_value == 10.0


def test_health_level_prioritizes_critical():
    warn = Alert(code="a", level="warn", title="t", detail="d")
    crit = Alert(code="b", level="critical", title="t", detail="d")
    assert alerts.health_level([warn, crit]) == "critical"
    assert alerts.health_level([warn]) == "warn"
    assert alerts.health_level([]) == "ok"


def test_active_alerts_sorts_critical_first(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    session.add(Alert(code="w", level="warn", title="Aviso", detail="d"))
    session.add(Alert(code="c", level="critical", title="Crítica", detail="d"))
    session.add(Alert(code="old", level="warn", title="Vieja", detail="d", active=False))
    session.commit()

    result = alerts.active_alerts(session)

    assert [a.code for a in result] == ["c", "w"]  # crítica primero; la inactiva no aparece


def test_unmatched_view_shows_health_for_admin(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    session.add(User(username="jefa", password_hash="x", is_admin=True))
    session.add(Alert(code="disco_bajo", level="critical", title="Disco casi lleno", detail="d"))
    session.commit()

    response = miner.unmatched_view(_request("jefa"))

    assert response.status_code == 200
    assert response.context["health"] == "critical"
    assert [a.code for a in response.context["alerts"]] == ["disco_bajo"]


def test_unmatched_view_redirects_non_admin(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    session.add(User(username="colab", password_hash="x", is_admin=False))
    session.commit()

    response = miner.unmatched_view(_request("colab"))

    assert response.headers["location"] == "/"
