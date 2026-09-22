"""Ollama local en el VPS (2026-09-22): el cupo de generaciones es perilla del
.env (barandal 1-8) y el servicio `ollama` del compose es opcional, acotado en
memoria, relevantable y solo accesible por loopback."""
import importlib
import os
from pathlib import Path

import yaml

from app import config

RAIZ = Path(__file__).resolve().parents[1]


def _settings_con(monkeypatch, valor):
    if valor is None:
        monkeypatch.delenv("OLLAMA_CONCURRENCIA", raising=False)
    else:
        monkeypatch.setenv("OLLAMA_CONCURRENCIA", valor)
    return config.Settings()


def test_cupo_default_cuatro_y_barandal_uno_a_ocho(monkeypatch):
    assert _settings_con(monkeypatch, None).ollama_concurrencia == 4
    assert _settings_con(monkeypatch, "1").ollama_concurrencia == 1
    assert _settings_con(monkeypatch, "0").ollama_concurrencia == 1, "0 apagaría la capa sin decirlo: se sube a 1"
    assert _settings_con(monkeypatch, "50").ollama_concurrencia == 8


def test_nlu_toma_el_cupo_de_settings():
    from app import nlu
    assert nlu.OLLAMA_CONCURRENCIA == config.settings.ollama_concurrencia
    # El semáforo se construye con ese número (BoundedSemaphore expone _value).
    assert nlu._CUPO_OLLAMA._value == nlu.OLLAMA_CONCURRENCIA


def test_servicio_ollama_del_compose_es_opcional_acotado_y_privado():
    compose = yaml.safe_load((RAIZ / "docker-compose.yml").read_text(encoding="utf-8"))
    ollama = compose["services"]["ollama"]
    assert ollama["profiles"] == ["ollama"], "sin el perfil se levantaría en todo despliegue"
    assert ollama["restart"] == "unless-stopped", "si se cae, Docker debe relevantarlo"
    assert ollama["container_name"] == "tramites-ollama"  # lo que revisa verificar-deploy.sh
    assert "mem_limit" in ollama, "sin tope crecería sobre MSSQL"
    assert ollama["image"].startswith("ollama/ollama:") and "@sha256:" in ollama["image"], "por digest, como mssql"
    for puerto in ollama.get("ports", []):
        assert str(puerto).startswith("127.0.0.1:"), f"puerto expuesto a internet: {puerto}"
    entorno = dict(e.split("=", 1) for e in ollama["environment"])
    assert entorno["OLLAMA_NUM_PARALLEL"] == "1" and entorno["OLLAMA_KEEP_ALIVE"] == "-1"
    assert ollama["entrypoint"] == ["/bin/sh", "/deploy/ollama-entrypoint.sh"]
    assert "./deploy/ollama-entrypoint.sh:/deploy/ollama-entrypoint.sh:ro" in ollama["volumes"]
    assert (RAIZ / "deploy" / "ollama-entrypoint.sh").exists()
    assert "ollama_models" in compose["volumes"]
    # El bot NO depende de ollama: sin modelo sigue con las capas baratas.
    assert "ollama" not in (compose["services"]["bot"].get("depends_on") or {})
