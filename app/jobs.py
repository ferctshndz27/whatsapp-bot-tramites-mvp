"""Job nocturno del agente (Fase 2): minería de sinónimos → propuestas
pendientes en el panel Agente WhatsApp. Nada se aprueba solo — el admin
decide en /unmatched.

Corre dentro del contenedor vía cron del host (mismo patrón que el deploy):
  30 3 * * * docker exec tramites-bot python -m app.jobs >> /var/log/tramites-jobs.log 2>&1

Cada pieza es best-effort y ninguna tumba a las demás: que falle la minería no
puede impedir el barrido de cobranza. Pero seguir tras un fallo y ADEMÁS
reportar éxito son dos cosas distintas, y esa segunda es la que deja un job
roto pareciendo sano durante semanas: la salida se redirige al log, así que el
código de salida es lo único que el cron sabe leer. Por eso `__main__` sale con
1 cuando algo falló, sin cambiar nada de lo que el job hace ni de lo que
`nightly()` devuelve.
"""

import logging
import sys

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
    from app import agent_settings, synonym_miner
    from app.audit import log_admin

    # El cron corre en OTRO proceso: sin esto ignoraría lo configurado desde
    # /agente y usaría solo el .env.
    try:
        with get_session() as _s:
            agent_settings.aplicar_overrides(_s)
    except Exception:
        logger.warning("Overrides del agente ilegibles; el cron usa el .env")

    # La cobranza se barre SIEMPRE y antes que nada: es lo único del job que
    # mueve dinero, y no depende de que haya minería ni API key.
    cobranza = _barrer_cobranza_safe()

    with get_session() as session:
        texts = [m.body for m in synonym_miner.unmatched_messages(session)]

    # Las alertas se evalúan siempre, independientemente de la minería.
    if not texts:
        return {"propuestas": 0, "motivo": "sin mensajes pendientes",
                "cobranza": cobranza, "alertas": _evaluate_alerts_safe()}
    if not (settings.modelo_abierto or settings.anthropic_api_key):
        return {"propuestas": 0, "motivo": "sin OPENROUTER_API_KEY, OLLAMA_URL ni ANTHROPIC_API_KEY",
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


# Contador de fallos POR ELEMENTO. Los cinco barridos de cobranza no revientan
# cuando un evento falla: registran `logger.exception`, suman aquí y siguen con
# el siguiente (app/cobranza_jobs.py:111-115, :281-286, :403-408, :474-478).
# Sin esta clave, una noche con 200 eventos de Stripe pendientes y 200 fallidos
# no tiene ninguna clave "error" y salía como noche sana: justo el caso que más
# importa, porque es dinero.
_CONTADOR_DE_FALLOS = "fallidos"


def errores(resultado: dict, _ruta: str = "") -> list[str]:
    """Qué falló dentro del resultado del job, por nombre. Vacía = todo bien.

    No inspecciona excepciones: para cuando esta función corre, cada pieza ya
    las convirtió en dato. Y las convierte de DOS formas distintas, que es lo
    que hace falta mirar por separado:

    1. `{"error": str(exc)}` cuando revienta la pieza ENTERA — la usan
       `_evaluate_alerts_safe`, `_barrer_cobranza_safe` y `barrer_todo`
       (`app/cobranza_jobs.py:502`) cuando muere un barrido completo. Se busca
       a cualquier profundidad, así que una pieza nueva que siga la convención
       queda cubierta sola.
    2. `{"fallidos": n}` cuando falla ELEMENTO a elemento y el barrido sigue
       adelante, que es el caso normal y el que de verdad ocurre: el barrido
       termina bien, devuelve su resumen, y los doscientos eventos que no se
       aplicaron viven solo en un contador.

    La minería es aparte: reporta en `motivo`, donde "no había nada que minar"
    y "reventó" comparten campo. Solo el prefijo `error:` es un fallo; "sin
    mensajes pendientes" y "sin OLLAMA_URL ni ANTHROPIC_API_KEY" son noches
    normales, no incidentes.

    Sabe lo que no ve, y son dos huecos deliberados:

    - El fallo al aplicar los overrides del Estudio queda solo en el log
      (arriba, `nightly`): su consecuencia es caer al `.env` —el comportamiento
      anterior— y no una corrida incompleta.
    - `sin_resolver` del barrido de huérfanos (`app/cobranza_jobs.py:225-231`)
      **no** se cuenta, aunque a veces sí sea un fallo: esa rama mezcla la
      excepción con el «todavía no empareja», que es una noche perfectamente
      normal. Contarlo alarmaría cada madrugada, y un job que alarma siempre se
      silencia y deja de avisar de nada. Si algún día ese barrido distingue las
      dos causas, esto se vuelve una clave más aquí. `irresolubles` tampoco: es
      estado terminal de negocio y ya tiene su alerta `critical` propia en
      `app/cobranza_alertas.py`.
    """
    fallos = []
    for clave, valor in resultado.items():
        ruta = f"{_ruta}.{clave}" if _ruta else clave
        if clave == "error":
            fallos.append(_ruta or "job")
        elif clave == _CONTADOR_DE_FALLOS and isinstance(valor, int) and valor > 0:
            fallos.append(_ruta or "job")
        elif isinstance(valor, dict):
            fallos.extend(errores(valor, ruta))
        elif clave == "motivo" and isinstance(valor, str) and valor.startswith("error:"):
            fallos.append(_ruta or "mineria")
    return sorted(set(fallos))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    resultado = nightly()
    print(resultado)
    # El trabajo que sí se hizo ya quedó guardado; esto solo decide qué le
    # dice el proceso al cron. Con exit 0 pase lo que pase, un barrido de
    # cobranza caído —dinero— se veía igual que una noche sana.
    fallos = errores(resultado)
    if fallos:
        logger.error("Job nocturno con %d fallo(s): %s", len(fallos), ", ".join(fallos))
    sys.exit(1 if fallos else 0)
