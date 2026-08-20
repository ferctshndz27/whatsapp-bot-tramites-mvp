"""Job nocturno del agente (Fase 2): minería de sinónimos → propuestas
pendientes en el panel Agente WhatsApp. Nada se aprueba solo — el admin
decide en /unmatched.

Corre dentro del contenedor vía cron del host (mismo patrón que el deploy):
  30 3 * * * docker exec tramites-bot python -m app.jobs >> /var/log/tramites-jobs.log 2>&1
"""

import logging

from app.config import settings
from app.db import get_session, init_db

logger = logging.getLogger(__name__)


def _evaluate_alerts_safe() -> dict:
    """Salud del agente (Fase 4b). Best-effort: nunca tumba el job nocturno.
    Corre SIEMPRE que el cron ejecuta, aunque no haya minería."""
    from app.alerts import evaluate_alerts

    try:
        return evaluate_alerts()
    except Exception as exc:  # el cron reintenta mañana
        logger.warning("Evaluación de alertas falló: %s", exc)
        return {"error": str(exc)}


def _barrer_cobranza_safe() -> dict:
    """Los cinco barridos de cobranza. Best-effort igual que las alertas: sin
    ellos, "el webhook nunca llegó" se vuelve dinero perdido, pero que fallen
    tampoco puede tumbar el resto del job."""
    from app.cobranza_jobs import barrer_todo

    try:
        return barrer_todo()
    except Exception as exc:  # el cron reintenta mañana
        logger.warning("Barridos de cobranza fallaron: %s", exc)
        return {"error": str(exc)}


def nightly() -> dict:
    init_db()
    from app import synonym_miner
    from app.audit import log_admin

    # La cobranza se barre SIEMPRE y antes que nada: es lo único del job que
    # mueve dinero, y no depende de que haya minería ni API key.
    cobranza = _barrer_cobranza_safe()

    with get_session() as session:
        texts = [m.body for m in synonym_miner.unmatched_messages(session)]

    # Las alertas se evalúan siempre, independientemente de la minería.
    if not texts:
        return {"propuestas": 0, "motivo": "sin mensajes pendientes",
                "cobranza": cobranza, "alertas": _evaluate_alerts_safe()}
    if not (settings.ollama_url or settings.anthropic_api_key):
        return {"propuestas": 0, "motivo": "sin OLLAMA_URL ni ANTHROPIC_API_KEY",
                "cobranza": cobranza, "alertas": _evaluate_alerts_safe()}

    try:
        proposals = synonym_miner.mine_synonyms(texts)
    except Exception as exc:  # best-effort: el cron reintenta mañana
        logger.warning("Minería nocturna falló: %s", exc)
        return {"propuestas": 0, "motivo": f"error: {exc}",
                "cobranza": cobranza, "alertas": _evaluate_alerts_safe()}

    with get_session() as session:
        created = synonym_miner.persist_proposals(session, proposals, origin="mineria-nocturna")
        session.commit()
    log_admin("sistema", "catalogo:mineria-nocturna", f"{len(texts)} mensajes → {created} propuestas")
    logger.info("Minería nocturna: %s propuestas nuevas", created)
    return {"propuestas": created, "cobranza": cobranza,
            "alertas": _evaluate_alerts_safe()}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(nightly())
