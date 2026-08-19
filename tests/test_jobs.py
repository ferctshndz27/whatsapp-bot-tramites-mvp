"""Job nocturno (app.jobs) y las dos reglas de salud que leen la infraestructura.

Cubre lo que nadie más ejercita:

* `nightly()` completo — el camino feliz con Claude MOCKEADO (jamás la red), la
  rama sin ANTHROPIC_API_KEY, el try/except que convierte un fallo de minería
  en `{"propuestas": 0, "motivo": "error: ..."}` y el que convierte un fallo de
  alertas en `{"error": ...}`. Es código que corre desatendido de madrugada:
  la garantía operativa es que **el cron nunca revienta**.
* La REGLA DE ORO en tiempo de ejecución: la minería nocturna deja las
  propuestas en "pendiente" y NO llama a `catalog.add_synonym`. Nada auto-aprende.
* `app.alerts`: las reglas `respaldo_viejo` y `disco_bajo` — las únicas dos que
  ningún test dispara, porque el helper de tests/test_alerts.py neutraliza
  `_backup_info`/`_disk_free` en todos sus casos.

No repite lo de tests/test_alerts.py (upsert/resolución de `no_entendidos_alto`,
`propuestas_pendientes`, orden y vista admin), tests/test_synonym_miner.py
(derivación de no-entendidos y validación de `mine_synonyms` contra el catálogo)
ni tests/test_learning.py (persistencia/aprobación/rechazo desde el panel).
"""

import json
import logging
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect
from sqlmodel import Session, create_engine, select

import app.alerts as alerts
import app.db as db
import app.nlu as nlu
import app.system as system
from app.catalog import catalog
from app.config import settings
from app.conversation import NOT_RECOGNIZED_HINT
from app.jobs import nightly
from app.models import AdminAudit, Direction, Message, SynonymProposal

PHONE = "5215500000000"
# Texto que el motor determinista NO relaciona con ningún servicio del catálogo
# (verificado: nlu._deduce_keywords devuelve (None, None)), así que
# unmatched_messages lo entrega como pendiente de minar.
TEXTO_NO_ENTENDIDO = "quiero el tramite zorglub imposible"
SINONIMO_PROPUESTO = "papelito magico"


@pytest.fixture()
def infra_sana(monkeypatch):
    """Respaldo y disco leen el sistema de archivos REAL: sin neutralizarlos las
    alertas dependerían de la máquina donde corre la suite. Cada prueba que
    quiera disparar una de esas dos reglas re-parcha solo la que le toca."""
    monkeypatch.setattr(
        system, "_backup_info", lambda: {"exists": True, "age_hours": 1.0, "stale": False}
    )
    monkeypatch.setattr(
        system, "_disk_free", lambda: {"free_pct": 80, "free_gb": 100.0, "low": False}
    )


def _seed_no_entendido(session, text: str = TEXTO_NO_ENTENDIDO, phone: str = PHONE) -> None:
    """Par IN + OUT("No reconocí…") que es como app.synonym_miner deriva los
    mensajes pendientes: no hay tabla propia, se leen de message."""
    session.add(Message(phone=phone, direction=Direction.IN, body=text))
    session.commit()
    session.add(Message(phone=phone, direction=Direction.OUT, body=NOT_RECOGNIZED_HINT + " menú"))
    session.commit()


def _claude_falso(payload: dict):
    """Cliente Anthropic de mentiras: mine_synonyms solo usa .messages.create()."""
    response = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
    )
    return SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: response))


def _claude_caido(exc: Exception):
    def _create(**kw):
        raise exc

    return SimpleNamespace(messages=SimpleNamespace(create=_create))


@pytest.fixture()
def mineria_lista(monkeypatch, infra_sana):
    """Escenario del camino feliz: un mensaje pendiente, API key puesta, Claude
    mockeado proponiendo UN sinónimo nuevo para un servicio real del catálogo, y
    un espía sobre Catalog.add_synonym para poder afirmar que NUNCA se llama."""
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    with Session(db.engine) as session:
        _seed_no_entendido(session)

    servicio = catalog.categories[0].services[0]
    monkeypatch.setattr(
        nlu,
        "_client",
        _claude_falso({"proposals": [{"service_id": servicio.id, "synonym": SINONIMO_PROPUESTO}]}),
    )

    llamadas: list[tuple[str, str]] = []

    def _espia(self, service_id, synonym, admin="sistema"):  # noqa: ANN001
        llamadas.append((service_id, synonym))
        return None

    monkeypatch.setattr(type(catalog), "add_synonym", _espia)
    return SimpleNamespace(servicio=servicio, add_synonym_calls=llamadas)


# --------------------------------------------------------------------------
# app.jobs.nightly() — el cron nocturno
# --------------------------------------------------------------------------


def test_nightly_evalua_alertas_aunque_no_haya_nada_que_minar(monkeypatch, infra_sana):
    """REGLA: la salud del agente se evalúa SIEMPRE que corre el cron, aunque no
    haya mensajes pendientes (si no, un disco lleno pasaría inadvertido)."""
    monkeypatch.setattr(
        system, "_disk_free", lambda: {"free_pct": 2, "free_gb": 0.4, "low": True}
    )

    result = nightly()

    assert result["propuestas"] == 0
    assert result["motivo"] == "sin mensajes pendientes"
    assert result["alertas"] == {"nuevas": 1, "actualizadas": 0, "resueltas": 0}
    with Session(db.engine) as session:
        assert [a.code for a in alerts.active_alerts(session)] == ["disco_bajo"]


def test_nightly_sin_api_key_no_consulta_a_claude(monkeypatch, infra_sana):
    """REGLA: sin ANTHROPIC_API_KEY el cron se detiene ANTES de construir el
    cliente — nunca intenta salir a la red — y reporta el motivo."""
    monkeypatch.setattr(settings, "anthropic_api_key", "")

    def _prohibido():
        raise AssertionError("nightly no debe construir el cliente de Claude sin API key")

    monkeypatch.setattr(nlu, "_get_client", _prohibido)
    with Session(db.engine) as session:
        _seed_no_entendido(session)  # sí hay mensajes: la única razón de parar es la key

    result = nightly()

    assert result["motivo"] == "sin OLLAMA_URL ni ANTHROPIC_API_KEY"
    assert result["propuestas"] == 0
    assert "nuevas" in result["alertas"]  # las alertas se evalúan igual
    with Session(db.engine) as session:
        assert session.exec(select(SynonymProposal)).all() == []


def test_nightly_persiste_propuestas_pendientes_y_deja_rastro_en_la_bitacora(mineria_lista):
    """REGLA: la minería nocturna solo PROPONE. La propuesta nace "pendiente",
    sin decisor, marcada con su origen, y la corrida queda auditada."""
    result = nightly()

    assert result["propuestas"] == 1
    with Session(db.engine) as session:
        prop = session.exec(select(SynonymProposal)).one()
        audit = session.exec(select(AdminAudit)).one()

    assert (prop.service_id, prop.synonym) == (mineria_lista.servicio.id, SINONIMO_PROPUESTO)
    assert prop.status == "pendiente"
    assert prop.origin == "mineria-nocturna"
    assert prop.decided_by is None and prop.decided_at is None
    assert (audit.admin, audit.action) == ("sistema", "catalogo:mineria-nocturna")
    assert result["alertas"]["nuevas"] == 0  # infraestructura sana: ninguna alerta


def test_la_mineria_nocturna_nunca_agrega_sinonimos_al_catalogo(mineria_lista):
    """REGLA DE ORO: nada auto-aprende. Un sinónimo mal aprendido haría que el
    bot cotizara mal a un cliente real, así que el cron jamás toca el catálogo."""
    antes = catalog.service_by_id(mineria_lista.servicio.id).synonyms

    nightly()

    despues = catalog.service_by_id(mineria_lista.servicio.id).synonyms
    assert mineria_lista.add_synonym_calls == []
    assert despues == antes
    assert SINONIMO_PROPUESTO not in [s.lower() for s in despues]


def test_nightly_no_revienta_si_la_mineria_falla(monkeypatch, caplog, mineria_lista):
    """REGLA: si Claude no responde, el cron devuelve el motivo y sigue vivo —
    mañana reintenta. Nunca propaga la excepción (nadie vería el traceback)."""
    monkeypatch.setattr(nlu, "_client", _claude_caido(RuntimeError("connection refused")))

    with caplog.at_level(logging.WARNING, logger="app.jobs"):
        result = nightly()

    assert result["propuestas"] == 0
    assert result["motivo"].startswith("error:")
    assert "connection refused" in result["motivo"]
    assert "nuevas" in result["alertas"]  # las alertas se evalúan pese al fallo
    assert "Minería nocturna falló" in caplog.text  # el log del cron es la única visibilidad
    with Session(db.engine) as session:
        assert session.exec(select(SynonymProposal)).all() == []
        assert session.exec(select(AdminAudit)).all() == []  # no audita lo que no pasó


def test_nightly_no_revienta_si_la_evaluacion_de_alertas_falla(monkeypatch, caplog, infra_sana):
    """REGLA: la salud es best-effort. Un fallo evaluando alertas se reporta en
    el resultado, no tumba el job nocturno."""
    def _boom():
        raise RuntimeError("no such table: alert")

    monkeypatch.setattr(alerts, "evaluate_alerts", _boom)

    with caplog.at_level(logging.WARNING, logger="app.jobs"):
        result = nightly()

    assert result["alertas"] == {"error": "no such table: alert"}
    assert result["propuestas"] == 0
    assert "Evaluación de alertas falló" in caplog.text


def test_nightly_crea_el_esquema_si_la_base_esta_vacia(monkeypatch, infra_sana):
    """REGRESIÓN: el cron (`python -m app.jobs`) es el primero en tocar la base
    en un contenedor recién creado. init_db() tiene que dejar las tablas puestas
    antes de consultar nada; si no, el job falla en silencio de madrugada."""
    vacia = create_engine("sqlite://", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", vacia)  # a propósito SIN create_all

    result = nightly()

    assert result["propuestas"] == 0
    tablas = set(inspect(vacia).get_table_names())
    assert {"message", "nluevent", "synonymproposal", "alert"} <= tablas


# --------------------------------------------------------------------------
# app.alerts — reglas de infraestructura (respaldo y disco)
# --------------------------------------------------------------------------


def _reglas_disparadas(session) -> dict[str, tuple[str, str, float | None]]:
    return {
        code: (level, detail, metric_value)
        for code, level, _title, detail, metric_value in alerts.current_rules(session)
    }


def test_infraestructura_sana_no_dispara_ninguna_regla(infra_sana):
    """Línea base: con la base vacía y la infra sana no hay alertas. Sin esto,
    las afirmaciones de las pruebas de abajo no probarían nada."""
    with Session(db.engine) as session:
        assert _reglas_disparadas(session) == {}


def test_respaldo_viejo_reporta_la_antiguedad_del_ultimo_archivo(monkeypatch, infra_sana):
    """REGLA: si hay respaldo pero está viejo, la alerta dice cuántas horas tiene
    (es el dato que el operador necesita para decidir)."""
    monkeypatch.setattr(
        system, "_backup_info", lambda: {"exists": True, "age_hours": 72.5, "stale": True}
    )

    with Session(db.engine) as session:
        disparadas = _reglas_disparadas(session)

    level, detail, metric_value = disparadas["respaldo_viejo"]
    assert level == "warn"
    assert metric_value == 72.5
    assert "72.5" in detail
    assert "No hay ningún respaldo" not in detail  # esa es la otra rama


def test_respaldo_inexistente_pide_configurarlo_sin_inventar_antiguedad(monkeypatch, infra_sana):
    """REGLA: sin ningún archivo de respaldo la alerta pide configurarlo y no
    reporta antigüedad (no la hay)."""
    monkeypatch.setattr(
        system, "_backup_info", lambda: {"exists": False, "age_hours": None, "stale": True}
    )

    with Session(db.engine) as session:
        disparadas = _reglas_disparadas(session)

    level, detail, metric_value = disparadas["respaldo_viejo"]
    assert level == "warn"
    assert metric_value is None
    assert "No hay ningún respaldo" in detail
    assert "horas" not in detail


def test_respaldo_fresco_no_dispara(monkeypatch, infra_sana):
    """REGLA: la antigüedad la juzga app.system._backup_info; alerts solo respeta
    su veredicto ("stale"). Fresco = sin alerta, aunque el archivo tenga horas."""
    monkeypatch.setattr(
        system, "_backup_info", lambda: {"exists": True, "age_hours": 47.9, "stale": False}
    )

    with Session(db.engine) as session:
        assert "respaldo_viejo" not in _reglas_disparadas(session)


@pytest.mark.parametrize(
    "free_pct, nivel_esperado",
    [
        (80, None),        # holgado
        (16, None),
        (15, None),        # umbral de aviso EXACTO: no dispara (la comparación es <)
        (14, "warn"),
        (6, "warn"),
        (5, "warn"),       # umbral crítico EXACTO: sigue siendo aviso, no crítica
        (4, "critical"),
        (0, "critical"),
    ],
)
def test_disco_bajo_avisa_bajo_15_y_es_critico_bajo_5(
    monkeypatch, infra_sana, free_pct, nivel_esperado
):
    """REGLA (valores del producto, anclados al literal): menos de 15% libre =
    aviso, menos de 5% = crítica. Justo en el umbral todavía no dispara."""
    monkeypatch.setattr(
        system,
        "_disk_free",
        lambda: {"free_pct": free_pct, "free_gb": 3.2, "low": free_pct < 15},
    )

    with Session(db.engine) as session:
        disparadas = _reglas_disparadas(session)

    if nivel_esperado is None:
        assert "disco_bajo" not in disparadas
        return
    level, detail, metric_value = disparadas["disco_bajo"]
    assert level == nivel_esperado
    assert metric_value == float(free_pct)
    assert f"{free_pct}%" in detail and "3.2 GB" in detail  # el detalle da el dato accionable


def test_alertas_de_infraestructura_llegan_a_la_tabla_y_se_resuelven(monkeypatch, infra_sana):
    """REGLA: respaldo y disco son reglas independientes — pueden estar activas a
    la vez — y la salud global la manda la más severa. Al normalizarse la infra,
    ambas se resuelven solas en la siguiente corrida del cron."""
    monkeypatch.setattr(
        system, "_backup_info", lambda: {"exists": False, "age_hours": None, "stale": True}
    )
    monkeypatch.setattr(
        system, "_disk_free", lambda: {"free_pct": 1, "free_gb": 0.2, "low": True}
    )

    resumen = alerts.evaluate_alerts()

    with Session(db.engine) as session:
        activas = alerts.active_alerts(session)
    assert resumen["nuevas"] == 2
    assert {a.code for a in activas} == {"respaldo_viejo", "disco_bajo"}
    assert alerts.health_level(activas) == "critical"  # la crítica del disco manda

    # El operador arregla la infra: respaldo fresco y disco holgado.
    monkeypatch.setattr(
        system, "_backup_info", lambda: {"exists": True, "age_hours": 1.0, "stale": False}
    )
    monkeypatch.setattr(
        system, "_disk_free", lambda: {"free_pct": 80, "free_gb": 100.0, "low": False}
    )
    infra_sana_de_nuevo = alerts.evaluate_alerts()

    with Session(db.engine) as session:
        assert alerts.active_alerts(session) == []
    assert infra_sana_de_nuevo["resueltas"] == 2
