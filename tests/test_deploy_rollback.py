"""El rollback del deploy, ejercitado de verdad.

`deploy/pull-deploy.sh` no tiene pruebas y es el único código del repo cuyo
fallo deja al bot CAÍDO sin avisar: si `rollback()` no corre tras un deploy
malo, HEAD se queda en el commit roto, el siguiente tick del cron ve
`LOCAL == REMOTE`, sale 0 para siempre y nadie se entera hasta que alguien
entra por SSH. El propio script lo explica en el comentario de `rollback()`.

Se ejercita el script REAL sobre un repo git de mentira, con `docker`, `curl` y
`sleep` stubbeados. No hace falta el demonio de docker, así que corre igual en
CI (bash y git existen en el runner de ubuntu).

Ojo al montar bancos como este: la primera versión de esta prueba daba VERDE
por la razón equivocada. `deploy()` empieza con `git reset --hard
origin/$BRANCH`, y como el repo de prueba no tenía remoto, el script moría en
esa línea —antes del punto de interés— dejando HEAD intacto; la aserción
"HEAD volvió al commit sano" se cumplía sin que `rollback` hubiera corrido
jamás. De ahí el control positivo de abajo: si el escenario sano no demuestra
que el arnés SABE ver un rollback, el escenario roto no prueba nada.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
SCRIPT = RAIZ / "deploy" / "pull-deploy.sh"


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture()
def banco(tmp_path):
    """Repo con dos commits y un remoto local: v1 es el sano, v2 el que rompe."""
    origen, repo, binarios = tmp_path / "origen", tmp_path / "repo", tmp_path / "bin"
    for d in (origen, repo, binarios):
        d.mkdir()

    _git(origen, "init", "-q", "--bare", "--initial-branch=production", ".")
    trabajo = tmp_path / "trabajo"
    trabajo.mkdir()
    _git(trabajo, "init", "-q", "--initial-branch=production", ".")
    _git(trabajo, "config", "user.email", "t@t")
    _git(trabajo, "config", "user.name", "t")
    (trabajo / "f").write_text("v1")
    _git(trabajo, "add", "f")
    _git(trabajo, "commit", "-qm", "v1 sano")
    sano = _git(trabajo, "rev-parse", "HEAD").stdout.strip()
    (trabajo / "f").write_text("v2")
    _git(trabajo, "add", "f")
    _git(trabajo, "commit", "-qm", "v2 roto")
    malo = _git(trabajo, "rev-parse", "HEAD").stdout.strip()
    _git(trabajo, "remote", "add", "origin", str(origen))
    _git(trabajo, "push", "-q", "origin", "production")

    # El repo del "VPS": clonado y con HEAD en el commit SANO.
    _git(tmp_path, "clone", "-q", str(origen), str(repo))
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "checkout", "-q", sano)
    (repo / "data").mkdir(exist_ok=True)
    (repo / "mailserver").mkdir(exist_ok=True)

    return {"repo": repo, "bin": binarios, "sano": sano, "malo": malo}


def _correr(banco, *, logs_exit: int) -> str:
    """Corre deploy() con el health check SIEMPRE fallando. Devuelve el HEAD final."""
    (banco["bin"] / "docker").write_text(
        "#!/bin/bash\n"
        'for a in "$@"; do\n'
        f'  if [ "$a" = logs ]; then exit {logs_exit}; fi\n'
        "done\n"
        "exit 0\n"
    )
    (banco["bin"] / "curl").write_text("#!/bin/bash\nexit 1\n")   # health nunca responde
    (banco["bin"] / "sleep").write_text("#!/bin/bash\nexit 0\n")  # sin esperar los 60 s
    for f in banco["bin"].iterdir():
        f.chmod(0o755)

    guion = banco["repo"] / "_prueba.sh"
    fuente = SCRIPT.read_text()
    trozos = ["set -euo pipefail", "BRANCH=production",
              f'REMOTE={banco["malo"]}', f'LOCAL={banco["sano"]}']
    for nombre in ("rollback", "deploy"):
        ini = fuente.index(f"{nombre}() {{")
        fin = fuente.index("\n}\n", ini) + 3
        trozos.append(fuente[ini:fin])
    trozos.append("deploy")
    guion.write_text("\n".join(trozos))

    subprocess.run(
        ["bash", str(guion)],
        cwd=banco["repo"], capture_output=True, text=True, timeout=120,
        env={**os.environ, "PATH": f'{banco["bin"]}:{os.environ["PATH"]}'},
    )
    return _git(banco["repo"], "rev-parse", "HEAD").stdout.strip()


def test_control_el_banco_sabe_observar_un_rollback(banco):
    """CONTROL POSITIVO, y no es ceremonia: sin él, el test de abajo pasaría
    aunque el script muriera antes de mover nada. Con `docker compose logs`
    saliendo 0, el rollback corre y HEAD tiene que volver al commit sano."""
    final = _correr(banco, logs_exit=0)

    assert final == banco["sano"], (
        "el arnés no observa el rollback ni en el caso bueno: cualquier verde de "
        "la prueba siguiente sería vacío"
    )


def test_el_rollback_corre_aunque_el_volcado_de_logs_falle(banco):
    """REGRESIÓN: `docker compose logs` es diagnóstico y jamás debe adelantarse
    al rollback.

    Bajo `set -euo pipefail`, sin el `|| true` de esa línea, un `logs` que
    devuelva != 0 mata el script ANTES de `rollback` y HEAD se queda en el
    commit malo — de ahí el cron sale 0 en todos los ticks siguientes y el bot
    queda caído sin alerta. Y falla más de lo que parece: basta que el commit
    malo renombre el servicio `bot` en docker-compose.yml, o que el demonio de
    docker haya muerto en esa ventana de 60 s.
    """
    final = _correr(banco, logs_exit=1)

    assert final == banco["sano"], (
        "HEAD se quedó en el commit malo: el rollback no corrió porque `docker "
        "compose logs` falló y set -e mató el script antes. Falta el `|| true`."
    )


def test_el_script_declara_los_flags_estrictos():
    """El resto de este archivo razona sobre `set -e`; si alguien lo quitara,
    estas pruebas seguirían verdes probando otra cosa."""
    assert "set -euo pipefail" in SCRIPT.read_text()
