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


def nightly() -> dict:
    init_db()
    from app import synonym_miner
    from app.audit import log_admin

    with get_session() as session:
        texts = [m.body for m in synonym_miner.unmatched_messages(session)]
    if not texts:
        return {"propuestas": 0, "motivo": "sin mensajes pendientes"}
    if not settings.anthropic_api_key:
        return {"propuestas": 0, "motivo": "sin ANTHROPIC_API_KEY"}

    try:
        proposals = synonym_miner.mine_synonyms(texts)
    except Exception as exc:  # best-effort: el cron reintenta mañana
        logger.warning("Minería nocturna falló: %s", exc)
        return {"propuestas": 0, "motivo": f"error: {exc}"}

    with get_session() as session:
        created = synonym_miner.persist_proposals(session, proposals, origin="claude-nocturno")
        session.commit()
    log_admin("sistema", "catalogo:mineria-nocturna", f"{len(texts)} mensajes → {created} propuestas")
    logger.info("Minería nocturna: %s propuestas nuevas", created)
    return {"propuestas": created}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(nightly())
