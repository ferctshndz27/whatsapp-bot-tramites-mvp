"""Perillas de la regla del folio y de la revisión (tarea 18 de casos-analisis, diseño §5.5).

`contexto_folio_enabled` y `revision_enabled` nacen apagadas; `contexto_min_palabras` (40) tiene
barandal 15–200 como `fuzzy_threshold`. Si estas pruebas se ponen rojas, un despliegue encendería
una regla nueva en clientes reales sin que nadie la prendiera desde /agente, o un valor absurdo en
el .env (3 palabras: todo es «informativo»; 1000: nada lo es) entraría sin barandal.
"""


import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

import app.db as db
from app import agent_settings
from app.config import Settings, settings
from app.models import AgentConfig

PERILLAS = ("contexto_folio_enabled", "revision_enabled", "contexto_min_palabras")


def test_nacen_apagadas_y_en_40(monkeypatch):
    for var in ("CONTEXTO_FOLIO_ENABLED", "REVISION_ENABLED", "CONTEXTO_MIN_PALABRAS"):
        monkeypatch.delenv(var, raising=False)
    s = Settings()
    assert s.contexto_folio_enabled is False
    assert s.revision_enabled is False
    assert s.contexto_min_palabras == 40


@pytest.mark.parametrize(("crudo", "esperado"), [("14", 15), ("15", 15), ("120", 120), ("200", 200), ("201", 200)])
def test_contexto_min_palabras_tiene_barandal(monkeypatch, crudo, esperado):
    monkeypatch.setenv("CONTEXTO_MIN_PALABRAS", crudo)
    assert Settings().contexto_min_palabras == esperado


def test_se_encienden_solo_con_1(monkeypatch):
    monkeypatch.setenv("CONTEXTO_FOLIO_ENABLED", "1")
    monkeypatch.setenv("REVISION_ENABLED", "si")
    s = Settings()
    assert s.contexto_folio_enabled is True
    assert s.revision_enabled is False


def test_las_tres_son_editables_con_el_mismo_rango():
    for clave in PERILLAS:
        assert clave in agent_settings.CONFIG_EDITABLE, clave
    p = agent_settings.CONFIG_EDITABLE["contexto_min_palabras"]
    assert (p.tipo, p.minimo, p.maximo) == ("entero", 15, 200)
    assert p.ayuda
    assert agent_settings.CONFIG_EDITABLE["contexto_folio_enabled"].tipo == "bool"
    assert agent_settings.CONFIG_EDITABLE["revision_enabled"].tipo == "bool"


def test_fuera_de_rango_se_rechaza_al_guardar():
    with pytest.raises(ValueError, match="15 a 200"):
        agent_settings._convertir(agent_settings.CONFIG_EDITABLE["contexto_min_palabras"], "14")


def test_override_por_agentconfig(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    for clave, valor in (("contexto_folio_enabled", False), ("revision_enabled", False), ("contexto_min_palabras", 40)):
        monkeypatch.setattr(settings, clave, valor)
    with Session(engine) as s:
        s.add(AgentConfig(key="contexto_folio_enabled", value="1"))
        s.add(AgentConfig(key="revision_enabled", value="1"))
        s.add(AgentConfig(key="contexto_min_palabras", value="60"))
        s.commit()
        agent_settings.aplicar_overrides(s)
    assert settings.contexto_folio_enabled is True
    assert settings.revision_enabled is True
    assert settings.contexto_min_palabras == 60
