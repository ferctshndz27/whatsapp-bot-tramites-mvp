"""deploy/ollama-entrypoint.sh con un `ollama` falso: levanta el servidor,
espera a que conteste, descarga el modelo de OLLAMA_MODEL, no muere si la
descarga falla, y reenvía SIGTERM al servidor."""
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "ollama-entrypoint.sh"


@pytest.fixture
def ollama_falso(tmp_path):
    """`ollama serve` se queda vivo hasta recibir TERM; `ls` falla las dos
    primeras veces (servidor arrancando) y luego contesta; `pull` anota el
    modelo y sale con FAKE_PULL_EXIT."""
    bitacora = tmp_path / "bitacora"
    bitacora.write_text("")
    contador = tmp_path / "ls_llamadas"
    contador.write_text("0")
    falso = tmp_path / "ollama"
    falso.write_text(f"""#!/bin/bash
case "$1" in
  serve) trap 'echo "serve: TERM recibido" >> "{bitacora}"; exit 0' TERM
         echo "serve: arranca" >> "{bitacora}"
         while :; do sleep 0.1; done;;
  ls)    n=$(cat "{contador}"); n=$((n + 1)); echo "$n" > "{contador}"
         [ "$n" -ge 3 ] && exit 0 || exit 1;;
  pull)  echo "pull: $2" >> "{bitacora}"; exit "${{FAKE_PULL_EXIT:-0}}";;
  *)     echo "ollama falso: $*" >&2; exit 1;;
esac
""")
    falso.chmod(0o755)
    return {"bin": falso, "bitacora": bitacora, "contador": contador}


def _lanzar(ollama_falso, **entorno):
    env = {**os.environ, "OLLAMA_BIN": str(ollama_falso["bin"]), "OLLAMA_ESPERA_ARRANQUE": "10", **entorno}
    return subprocess.Popen(["sh", str(SCRIPT)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _esperar(condicion, segundos=8.0):
    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        if condicion():
            return True
        time.sleep(0.05)
    return False


def test_arranca_espera_descarga_el_modelo_y_reenvia_term(ollama_falso):
    proc = _lanzar(ollama_falso, OLLAMA_MODEL="qwen3:1.7b")
    try:
        assert _esperar(lambda: "pull: qwen3:1.7b" in ollama_falso["bitacora"].read_text()), \
            "no descargó el modelo tras esperar al servidor"
        assert int(ollama_falso["contador"].read_text()) >= 3, "no esperó a que `ls` contestara"
        assert proc.poll() is None, "el script terminó aunque el servidor sigue vivo"
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=8)
        assert "serve: TERM recibido" in ollama_falso["bitacora"].read_text(), "no reenvió SIGTERM al servidor"
    finally:
        if proc.poll() is None:
            proc.kill()


def test_una_descarga_fallida_es_advertencia_y_el_servidor_sigue(ollama_falso):
    proc = _lanzar(ollama_falso, OLLAMA_MODEL="qwen3:1.7b", FAKE_PULL_EXIT="1")
    try:
        assert _esperar(lambda: "pull: qwen3:1.7b" in ollama_falso["bitacora"].read_text())
        time.sleep(0.5)
        assert proc.poll() is None, "una descarga fallida no debe tumbar el servicio"
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=8)
        assert "ADVERTENCIA" in err and "qwen3:1.7b" in err
    finally:
        if proc.poll() is None:
            proc.kill()


def test_sin_modelo_no_descarga_nada(ollama_falso):
    proc = _lanzar(ollama_falso, OLLAMA_MODEL="")
    try:
        assert _esperar(lambda: int(ollama_falso["contador"].read_text()) >= 3)
        time.sleep(0.5)
        assert "pull:" not in ollama_falso["bitacora"].read_text()
        assert proc.poll() is None
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
