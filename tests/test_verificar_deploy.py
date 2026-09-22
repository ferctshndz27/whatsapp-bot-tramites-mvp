"""`deploy/verificar-deploy.sh`, ejercitado de verdad, como `pull-deploy.sh`.

Es el script que el operador corre en el VPS antes y después de un deploy que
toca dependencias. Si un chequeo pasa por la razón equivocada, el operador da
por bueno un deploy que no lo es; si el script revienta a medias, se queda sin
el resto del cuadro. Las dos cosas ya pasaron mientras se escribía, y el banco
de abajo las cazó antes que un deploy: el cuerpo de 100 000 campos rebasaba
ARG_MAX como argumento de curl, y `grep -c` devolvía `0` Y salía con 1, así que
`|| echo 0` dejaba «0\n0» y la comparación entera moría.

Mismo banco que tests/test_deploy_rollback.py: script REAL, repo git de
mentira, `docker`, `curl` y `crontab` stubbeados por PATH. No hace falta el
demonio de docker, así que corre igual en CI.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
SCRIPT = RAIZ / "deploy" / "verificar-deploy.sh"
SHA_VIEJO = "0" * 40


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture()
def banco(tmp_path):
    """Repo con un commit y deploy-info apuntándolo; `data/tramites.db` real
    (para que el respaldo tenga algo íntegro que copiar); fakes en `bin/`."""
    repo, binarios = tmp_path / "repo", tmp_path / "bin"
    (repo / "data").mkdir(parents=True)
    binarios.mkdir()
    _git(repo, "init", "-q", "--initial-branch=production", ".")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "requirements.txt").write_text("# pin\nfastapi==0.136.3\nstarlette==1.6.0\nuvicorn[standard]==0.34.0\nrapidfuzz>=3.0\n")
    _git(repo, "add", "requirements.txt")
    _git(repo, "commit", "-qm", "v1")
    _git(repo, "remote", "add", "origin", ".")
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "data" / "deploy-info").write_text(f"{sha} 2026-09-21T10:00:00-06:00\n")
    con = sqlite3.connect(repo / "data" / "tramites.db")
    con.execute("create table t(x)")
    con.execute("insert into t values (1)")
    con.commit()
    con.close()
    log = tmp_path / "cron.log"
    log.write_text("")

    # docker falso: lo que el script le pide y nada más. El `exec -i … python -`
    # del respaldo corre el mismo python del banco con /data/ redirigido al repo.
    (binarios / "docker").write_text(textwrap.dedent(f"""\
        #!/bin/bash
        case "$1 $2" in
          "inspect -f") case "$3" in
              *StartedAt*) echo "${{FAKE_STARTED:-2026-09-21T10:00:00Z}}";;
              *Status*)    echo "${{FAKE_STATUS:-running}}";; esac;;
          "exec -i")    sed 's#/data/#{repo}/data/#g' | "{sys.executable}" - ;;
          "exec tramites-bot") shift 2
              if [ "$1 $2" = "pip freeze" ]; then
                grep -v '^#' "{repo}/requirements.txt" | sed 's/\\[standard\\]//; s/>=.*//' | grep '==' \\
                  | sed "s/^starlette==.*/starlette==${{FAKE_STARLETTE:-1.6.0}}/"
              fi;;
          "pull -q")    exit "${{FAKE_PULL_EXIT:-0}}";;
          "compose logs") echo "INFO ok";;
          *) echo "docker falso: $*" >&2; exit 1;;
        esac
        """))
    (binarios / "curl").write_text(textwrap.dedent("""\
        #!/bin/bash
        args="$*"
        if [[ "$args" == *"/health"* ]]; then exit "${FAKE_HEALTH_EXIT:-0}"; fi
        if [[ "$args" == *"--data-binary"* ]]; then printf '%s' "${FAKE_CAMPOS:-400}"; exit 0; fi
        if [[ "$args" == *"password="* ]]; then printf '%s' "${FAKE_VACIO:-200}"; exit 0; fi
        printf '200'
        """))
    (binarios / "crontab").write_text(
        f'#!/bin/bash\necho "*/2 * * * * flock -n /tmp/x /opt/x/deploy/pull-deploy.sh >> {log} 2>&1"\n'
    )
    for f in binarios.iterdir():
        f.chmod(0o755)
    return {"repo": repo, "bin": binarios, "sha": sha, "log": log}


def _correr(banco, *args, entorno=None, timeout=90):
    env = {**os.environ, "PATH": f"{banco['bin']}:{os.environ['PATH']}", "REPO_DIR": str(banco["repo"]),
           **(entorno or {})}
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=timeout)


def test_antes_sano_termina_en_cero_y_deja_un_respaldo_integro(banco):
    """Control positivo del banco: si el escenario sano no da 0, los escenarios
    rotos de abajo no demuestran que el script SABE ver un problema."""
    r = _correr(banco, "antes")
    assert r.returncode == 0, r.stdout + r.stderr
    respaldos = list((banco["repo"] / "data" / "backups").glob("tramites-*.db"))
    assert len(respaldos) == 1, "el respaldo no se escribió"
    con = sqlite3.connect(respaldos[0])
    assert con.execute("pragma integrity_check").fetchone()[0] == "ok"
    assert con.execute("select count(*) from t").fetchone()[0] == 1, "el respaldo no trae los datos"
    con.close()
    assert (banco["repo"] / "data" / "verificar-deploy.estado").read_text().startswith(f"HEAD={banco['sha']}")


def test_antes_ve_un_deploy_fallido_pendiente_y_un_contenedor_caido(banco):
    (banco["repo"] / "data" / "deploy-info").write_text(f"{SHA_VIEJO} viejo\n")
    r = _correr(banco, "antes", entorno={"FAKE_STATUS": "exited", "FAKE_HEALTH_EXIT": "7"})
    assert r.returncode == 1
    assert "deploy fallido pendiente" in r.stdout
    assert "tramites-bot: exited" in r.stdout
    assert "/health responde → falló" in r.stdout
    assert "respaldo íntegro" in r.stdout, "un fallo temprano no debe saltarse el respaldo"


def test_despues_sano_da_cero_y_las_tres_sondas_del_panel_pasan(banco):
    _correr(banco, "antes")  # deja la nota de estado con el arranque viejo
    r = _correr(banco, "despues", banco["sha"][:7], entorno={"FAKE_STARTED": "2026-09-21T11:00:00Z"})
    assert r.returncode == 0, r.stdout + r.stderr
    for linea in ("el contenedor se reinició", "los pines de requirements.txt son los que corren",
                  "contraseña vacía → 200", "100 000 campos → 400"):
        assert linea in r.stdout, linea


def test_despues_distingue_un_contenedor_que_no_se_reinicio(banco):
    """deploy-info puede decir el SHA nuevo con el contenedor viejo corriendo:
    esa es la trampa que la skill de devops documenta, y el script la ve."""
    _correr(banco, "antes")
    r = _correr(banco, "despues", banco["sha"][:7], entorno={"FAKE_STARTED": "2026-09-21T10:00:00Z"})
    assert r.returncode == 1
    assert "NO se reinició" in r.stdout


def test_despues_compara_los_pines_con_lo_que_corre_en_el_contenedor(banco):
    _correr(banco, "antes")
    r = _correr(banco, "despues", banco["sha"][:7],
                entorno={"FAKE_STARTED": "2026-09-21T11:00:00Z", "FAKE_STARLETTE": "0.41.3"})
    assert r.returncode == 1
    assert "pin starlette==1.6.0 pero el contenedor tiene 0.41.3" in r.stdout


def test_despues_ve_el_422_del_framework_y_el_tope_de_campos_ausente(banco):
    _correr(banco, "antes")
    r = _correr(banco, "despues", banco["sha"][:7],
                entorno={"FAKE_STARTED": "2026-09-21T11:00:00Z", "FAKE_VACIO": "422", "FAKE_CAMPOS": "200"})
    assert r.returncode == 1
    assert "contraseña vacía → 422" in r.stdout
    assert "100 000 campos → 200" in r.stdout


@pytest.mark.parametrize(
    "log_previo",
    ["", "2026-09-21T09:00:00 rollback a 1111111\n"],
    ids=["log-sin-rollbacks", "log-con-uno-viejo"],
)
def test_durante_cae_al_primer_rollback_nuevo_e_ignora_los_viejos(banco, log_previo):
    """El `grep -c` que imprime 0 y sale con 1: la primera versión duplicaba el
    cero y moría con «se esperaba un entero» antes de mirar el log. Solo pasa
    con el log SIN rollbacks previos —con uno viejo `grep -c` sale 0 y no hay
    duplicado—, así que ese caso va aparte o la mutación sobrevive (sobrevivió)."""
    banco["log"].write_text(log_previo)
    (banco["repo"] / "data" / "deploy-info").write_text(f"{SHA_VIEJO} viejo\n")
    proceso = subprocess.Popen(
        ["bash", str(SCRIPT), "durante", banco["sha"][:7]], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env={**os.environ, "PATH": f"{banco['bin']}:{os.environ['PATH']}", "REPO_DIR": str(banco["repo"]),
             "ESPERA_MAX_SEGUNDOS": "60"},
    )
    time.sleep(2)
    with banco["log"].open("a") as f:
        f.write("2026-09-21T10:05:00 rollback a 2222222\n")
    salida, _ = proceso.communicate(timeout=60)
    assert proceso.returncode == 1, salida
    assert "hizo rollback" in salida
    assert "se esperaba un entero" not in salida


def test_durante_termina_en_despues_cuando_el_commit_llega_a_servir(banco):
    _correr(banco, "antes")
    (banco["repo"] / "data" / "deploy-info").write_text(f"{SHA_VIEJO} viejo\n")
    proceso = subprocess.Popen(
        ["bash", str(SCRIPT), "durante", banco["sha"][:7]], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env={**os.environ, "PATH": f"{banco['bin']}:{os.environ['PATH']}", "REPO_DIR": str(banco["repo"]),
             "ESPERA_MAX_SEGUNDOS": "60", "FAKE_STARTED": "2026-09-21T11:00:00Z"},
    )
    time.sleep(2)
    (banco["repo"] / "data" / "deploy-info").write_text(f"{banco['sha']} 2026-09-21T12:00:00\n")
    salida, _ = proceso.communicate(timeout=60)
    assert proceso.returncode == 0, salida
    assert "está sirviendo" in salida and "== despues" in salida and "Sin fallos" in salida
