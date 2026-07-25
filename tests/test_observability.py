"""Fase 4a: ventana temporal en las métricas, observabilidad de /system y
umbral fuzzy configurable."""

from datetime import timedelta

import app.db as db
from app.config import Settings
from app.models import NluEvent, utcnow
from app.synonym_miner import cluster_unmatched, nlu_metrics
from app.system import _backup_info, _disk_free


def test_metrics_ignore_events_outside_window(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    session.add(NluEvent(phone="p", text="acta reciente", capa="nombre"))
    # Fuera de la ventana de 14 días: el WHERE en SQL no debe traerlo.
    session.add(
        NluEvent(
            phone="p", text="acta vieja", capa="nombre",
            created_at=utcnow() - timedelta(days=40),
        )
    )
    session.commit()

    metrics = nlu_metrics(days=14)

    assert metrics["total"] == 1  # solo el reciente


def test_cluster_ignores_events_outside_window(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    session.add(NluEvent(phone="p", text="constancia semanas cotizadas", capa="ninguna"))
    session.add(
        NluEvent(
            phone="p", text="constancia semanas cotizadas", capa="ninguna",
            created_at=utcnow() - timedelta(days=40),
        )
    )
    session.commit()

    clusters = cluster_unmatched(days=14)

    assert clusters and clusters[0]["count"] == 1  # el viejo no suma


def test_backup_info_reports_missing_when_no_backups(monkeypatch, tmp_path):
    from app.config import settings

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "tramites.db"))

    info = _backup_info()

    assert info["exists"] is False
    assert info["stale"] is True


def test_backup_info_reports_age_when_present(monkeypatch, tmp_path):
    from app.config import settings

    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "dump.db").write_text("x")
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "tramites.db"))

    info = _backup_info()

    assert info["exists"] is True
    assert info["age_hours"] is not None
    assert info["stale"] is False  # recién creado


def test_disk_free_structure(monkeypatch, tmp_path):
    from app.config import settings

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "tramites.db"))

    disk = _disk_free()

    assert 0 <= disk["free_pct"] <= 100
    assert isinstance(disk["low"], bool)


def test_fuzzy_threshold_clamped_to_safe_range(monkeypatch):
    monkeypatch.setenv("FUZZY_THRESHOLD", "40")  # demasiado bajo: cotizaría mal
    assert Settings().fuzzy_threshold == 70

    monkeypatch.setenv("FUZZY_THRESHOLD", "99")
    assert Settings().fuzzy_threshold == 95

    monkeypatch.setenv("FUZZY_THRESHOLD", "88")
    assert Settings().fuzzy_threshold == 88


def test_local_dt_converts_utc_to_cdmx():
    from datetime import datetime

    from app.templating import local_dt

    assert local_dt(datetime(2026, 7, 25, 0, 32)) == "2026-07-24 18:32"
    assert local_dt(None) == ""
