"""Fase 4b — Salud del agente.

Evalúa reglas simples sobre el estado del sistema y las persiste como filas
`Alert` (una activa por `code`). El panel /unmatched (admin) muestra un banner
de salud y la lista de alertas activas. **Nada se envía por WhatsApp** — por
decisión del operador las alertas viven solo en el panel.

`evaluate_alerts()` se llama al final del job nocturno (app.jobs.nightly). Es
idempotente: reevaluar sin cambios no duplica filas ni las resucita.

Fuente única: reusa `nlu_metrics`/`cluster` NO, sino los mismos datos crudos —
el % de no entendidos se calcula con su propia ventana de 7 días (distinta de
los 14 días del panel); `_backup_info()` y `_disk_free()` vienen de app.system y
el conteo de propuestas de `SynonymProposal`, sin reimplementar nada.
"""

import logging
from datetime import timedelta, timezone

from sqlmodel import Session, func, select

from app.db import get_session
from app.models import Alert, NluEvent, SynonymProposal, utcnow

logger = logging.getLogger(__name__)

# Ventana y umbrales de la regla de "no entendidos". 7 días como pide el diseño;
# se exige un mínimo de eventos para no disparar con muestras diminutas.
NO_ENTENDIDOS_DAYS = 7
NO_ENTENDIDOS_MIN_EVENTS = 20
NO_ENTENDIDOS_WARN_PCT = 25
NO_ENTENDIDOS_CRIT_PCT = 40

PROPUESTAS_WARN = 10

DISCO_WARN_PCT = 15
DISCO_CRIT_PCT = 5

# Orden de severidad para resolver el nivel global de salud.
_LEVEL_RANK = {"ok": 0, "warn": 1, "critical": 2}


def _no_entendidos_stats(session: Session, days: int = NO_ENTENDIDOS_DAYS) -> tuple[int, float]:
    """(total, pct_no_entendidos) de nluevent en la ventana. El corte fino por
    fecha lo hace SQL con naive-UTC (mismo criterio que synonym_miner). Los
    grupos ignorados a mano (soft delete) no cuentan, igual que en el panel."""
    cutoff = (utcnow() - timedelta(days=days)).astimezone(timezone.utc).replace(tzinfo=None)
    total = session.exec(
        select(func.count())
        .select_from(NluEvent)
        .where(NluEvent.created_at >= cutoff)
        .where(NluEvent.habilitado == True)  # noqa: E712
    ).one()
    if not total:
        return 0, 0.0
    ninguna = session.exec(
        select(func.count())
        .select_from(NluEvent)
        .where(NluEvent.created_at >= cutoff)
        .where(NluEvent.habilitado == True)  # noqa: E712
        .where(NluEvent.capa == "ninguna")
    ).one()
    return total, round(ninguna / total * 100, 1)


def _pending_proposals(session: Session) -> int:
    return session.exec(
        select(func.count())
        .select_from(SynonymProposal)
        .where(SynonymProposal.status == "pendiente")
    ).one()


def current_rules(session: Session) -> list[tuple[str, str, str, str, float | None]]:
    """Evalúa cada regla y devuelve las que disparan como tuplas
    (code, level, title, detail, metric_value). Reusa las utilidades de
    observabilidad de app.system para respaldo y disco (import local para
    evitar un ciclo alerts↔system al arrancar)."""
    from app.system import _backup_info, _disk_free

    fired: list[tuple[str, str, str, str, float | None]] = []

    # 1) % de mensajes no entendidos (capa "ninguna") en 7 días.
    total, pct = _no_entendidos_stats(session)
    if total >= NO_ENTENDIDOS_MIN_EVENTS:
        if pct >= NO_ENTENDIDOS_CRIT_PCT:
            fired.append((
                "no_entendidos_alto", "critical",
                "Muchos mensajes sin entender",
                f"El {pct:g}% de los mensajes de los últimos {NO_ENTENDIDOS_DAYS} días "
                f"no se entendió ({total} en total). Revisa los grupos y agrega sinónimos.",
                pct,
            ))
        elif pct >= NO_ENTENDIDOS_WARN_PCT:
            fired.append((
                "no_entendidos_alto", "warn",
                "Suben los mensajes sin entender",
                f"El {pct:g}% de los mensajes de los últimos {NO_ENTENDIDOS_DAYS} días "
                f"no se entendió ({total} en total). Conviene revisar los grupos.",
                pct,
            ))

    # 2) Propuestas de sinónimo esperando aprobación.
    pendientes = _pending_proposals(session)
    if pendientes >= PROPUESTAS_WARN:
        fired.append((
            "propuestas_pendientes", "warn",
            "Propuestas sin revisar",
            f"Hay {pendientes} propuestas de sinónimo esperando tu aprobación. "
            "Apruébalas o descártalas para que el bot mejore.",
            float(pendientes),
        ))

    # 3) Respaldo de la base viejo o inexistente.
    backup = _backup_info()
    if backup["stale"]:
        if backup["exists"]:
            detail = (
                f"El último respaldo tiene {backup['age_hours']} horas "
                "(más de 48). Verifica el respaldo del servidor."
            )
        else:
            detail = "No hay ningún respaldo registrado. Configura el respaldo del servidor."
        fired.append((
            "respaldo_viejo", "warn", "Respaldo de la base pendiente", detail,
            backup["age_hours"],
        ))

    # 4) Espacio en disco bajo.
    disk = _disk_free()
    free_pct = disk["free_pct"]
    if free_pct < DISCO_CRIT_PCT:
        fired.append((
            "disco_bajo", "critical", "Disco casi lleno",
            f"Queda {free_pct}% de disco libre ({disk['free_gb']} GB). "
            "Libera espacio con urgencia o el bot puede dejar de guardar.",
            float(free_pct),
        ))
    elif free_pct < DISCO_WARN_PCT:
        fired.append((
            "disco_bajo", "warn", "Poco espacio en disco",
            f"Queda {free_pct}% de disco libre ({disk['free_gb']} GB). "
            "Conviene liberar espacio pronto.",
            float(free_pct),
        ))

    return fired


def evaluate_alerts() -> dict:
    """Corre las reglas y sincroniza la tabla Alert: upsert de las que disparan,
    resuelve (active=False) las que ya no. Idempotente. Devuelve un resumen."""
    with get_session() as session:
        fired = current_rules(session)
        fired_by_code = {code: (level, title, detail, mv) for code, level, title, detail, mv in fired}

        active = session.exec(select(Alert).where(Alert.active == True)).all()  # noqa: E712
        active_by_code = {a.code: a for a in active}

        opened = updated = resolved = 0

        for code, (level, title, detail, metric_value) in fired_by_code.items():
            existing = active_by_code.get(code)
            if existing is None:
                session.add(Alert(
                    code=code, level=level, title=title, detail=detail,
                    metric_value=metric_value, active=True,
                ))
                opened += 1
            else:
                existing.level = level
                existing.title = title
                existing.detail = detail
                existing.metric_value = metric_value
                session.add(existing)
                updated += 1

        for code, alert in active_by_code.items():
            if code not in fired_by_code:
                alert.active = False
                alert.resolved_at = utcnow()
                session.add(alert)
                resolved += 1

        session.commit()

    logger.info(
        "Alertas evaluadas: %s nuevas, %s actualizadas, %s resueltas", opened, updated, resolved
    )
    return {"nuevas": opened, "actualizadas": updated, "resueltas": resolved}


def active_alerts(session: Session) -> list[Alert]:
    """Alertas activas: críticas primero, y dentro de cada nivel las más
    recientes arriba."""
    alerts = session.exec(select(Alert).where(Alert.active == True)).all()  # noqa: E712
    return sorted(
        alerts,
        key=lambda a: (_LEVEL_RANK.get(a.level, 0), a.created_at.timestamp()),
        reverse=True,
    )


def health_level(alerts: list[Alert]) -> str:
    """Nivel global: "critical" si alguna crítica, "warn" si solo warns, "ok"
    si no hay alertas activas."""
    if any(a.level == "critical" for a in alerts):
        return "critical"
    if any(a.level == "warn" for a in alerts):
        return "warn"
    return "ok"
