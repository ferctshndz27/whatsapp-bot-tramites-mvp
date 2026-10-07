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
demonio de docker, así que corre igual en CI. El «contenedor» del docker falso
es una carpeta (`FAKE_APP`, por omisión el propio repo): ahí corre el `sh` que
calcula la huella del código.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
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
# La forma del Dockerfile y del compose reales, en chico: lo que la imagen copia
# y un servicio que no es el bot.
DOCKERFILE = "FROM python:3.12-slim\nWORKDIR /app\nCOPY requirements.txt .\nCOPY app/ app/\nCMD [\"uvicorn\"]\n"
COMPOSE = textwrap.dedent("""\
    services:
      bot:
        build: .
        container_name: tramites-bot
        environment:
          - TZ=America/Mexico_City
      ollama:
        image: ollama/ollama:0.34.3
        container_name: tramites-ollama
    """)


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture()
def banco(tmp_path):
    """Repo con un commit y deploy-info apuntándolo; `data/tramites.db` real
    (para que el respaldo tenga algo íntegro que copiar); fakes en `bin/`."""
    repo, binarios = tmp_path / "repo", tmp_path / "bin"
    (repo / "data").mkdir(parents=True)
    (repo / "app").mkdir()
    binarios.mkdir()
    _git(repo, "init", "-q", "--initial-branch=production", ".")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "requirements.txt").write_text("# pin\nfastapi==0.136.3\nstarlette==1.6.0\nuvicorn[standard]==0.34.0\nrapidfuzz>=3.0\n")
    (repo / "Dockerfile").write_text(DOCKERFILE)
    (repo / "docker-compose.yml").write_text(COMPOSE)
    (repo / "app" / "main.py").write_text("print('v1')\n")
    _git(repo, "add", "requirements.txt", "Dockerfile", "docker-compose.yml", "app")
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
              *Status*)    if [ "$4" = tramites-ollama ]; then
                             # Como docker 29: renglón vacío en stdout y exit 1.
                             [ "${{FAKE_OLLAMA:-running}}" = ausente ] && {{ echo; exit 1; }}
                             echo "${{FAKE_OLLAMA:-running}}"
                           else echo "${{FAKE_STATUS:-running}}"; fi;; esac;;
          "exec -i")    sed 's#/data/#{repo}/data/#g' | "{sys.executable}" - ;;
          "exec tramites-bot") shift 2
              if [ "$1 $2" = "pip freeze" ]; then
                grep -v '^#' "{repo}/requirements.txt" | sed 's/\\[standard\\]//; s/>=.*//' | grep '==' \\
                  | sed "s/^starlette==.*/starlette==${{FAKE_STARLETTE:-1.6.0}}/"
              elif [ "$1" = sh ]; then
                cd "${{FAKE_APP:-{repo}}}" && exec "$@"
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


# Las perillas del script que el banco no fija, y los FAKE_ de los dobles: si
# quien corre pytest las tiene exportadas (tras un ensayo contra un contenedor
# real, por ejemplo), el script le hablaría a otro contenedor y el banco fallaría
# por una razón ajena. Pasó escribiendo la huella: 13 rojos falsos.
_PERILLAS = ("BRANCH", "CONTENEDOR", "SALUD", "LOGIN", "ESPERA_MAX_SEGUNDOS", "DISCO_MINIMO_GB")


def _entorno(banco, **extra):
    base = {k: v for k, v in os.environ.items() if k not in _PERILLAS and not k.startswith("FAKE_")}
    return {**base, "PATH": f"{banco['bin']}:{os.environ['PATH']}", "REPO_DIR": str(banco["repo"]), **extra}


def _correr(banco, *args, entorno=None, timeout=90):
    env = _entorno(banco, **(entorno or {}))
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=timeout)


def _desplegar(banco, archivos, mensaje="v2"):
    """Un commit que el cron ya desplegó: escribe los archivos, los commitea y
    deja deploy-info apuntándolo. Devuelve el sha."""
    repo = banco["repo"]
    for ruta, texto in archivos.items():
        (repo / ruta).parent.mkdir(parents=True, exist_ok=True)
        (repo / ruta).write_text(texto)
    _git(repo, "add", *archivos)
    _git(repo, "commit", "-qm", mensaje)
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "data" / "deploy-info").write_text(f"{sha} 2026-09-21T11:00:00-06:00\n")
    return sha


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
    _correr(banco, "antes")  # nota de estado: el commit y el arranque de antes del deploy
    sha = _desplegar(banco, {"app/main.py": "print('v2')\n"})
    r = _correr(banco, "despues", sha[:7], entorno={"FAKE_STARTED": "2026-09-21T11:00:00Z"})
    assert r.returncode == 0, r.stdout + r.stderr
    for linea in (f"el contenedor corre el código de {sha[:7]}", "el contenedor se reinició",
                  "cambia app/main.py", "los pines de requirements.txt son los que corren",
                  "contraseña vacía → 200", "100 000 campos → 400"):
        assert linea in r.stdout, linea


_DOCKERFILE_SIN_CAMBIO_REAL = DOCKERFILE.replace("WORKDIR /app\n", "# un comentario nuevo\nWORKDIR /app\n\n")


@pytest.mark.parametrize(
    "cambio",
    [
        {"app/main.py": "print('v2')\n"},
        {"Dockerfile": DOCKERFILE + "EXPOSE 8000\n"},
        {"docker-compose.yml": COMPOSE.replace("TZ=America/Mexico_City", "TZ=America/Monterrey")},
    ],
    ids=["lo-que-la-imagen-copia", "una-instruccion-del-dockerfile", "el-servicio-bot"],
)
def test_despues_exige_el_reinicio_cuando_el_deploy_cambia_la_imagen(banco, cambio):
    """La trampa que la skill de devops documenta: deploy-info puede decir el
    SHA nuevo con el contenedor viejo corriendo. Con una instrucción del
    Dockerfile o el servicio del compose la huella del código cuadra igual, así
    que el reinicio es lo único que lo ve. (Con lo que la imagen copia, en la
    vida real la huella también caería; aquí el «contenedor» es el repo, para
    aislar esta regla.)"""
    _correr(banco, "antes")
    sha = _desplegar(banco, cambio)
    r = _correr(banco, "despues", sha[:7])  # mismo arranque que en «antes»: no se reinició
    assert r.returncode == 1, r.stdout
    assert "NO se reinició" in r.stdout


@pytest.mark.parametrize(
    "cambio",
    [
        {"ci/etapa.sh": "#!/bin/sh\necho ci\n"},
        {"Dockerfile": _DOCKERFILE_SIN_CAMBIO_REAL},
        {"docker-compose.yml": COMPOSE.replace("ollama/ollama:0.34.3", "ollama/ollama:0.35.0")},
        {".dockerignore": ".venv/\n"},
    ],
    ids=["fuera-de-la-imagen", "comentario-del-dockerfile", "otro-servicio-del-compose", "dockerignore"],
)
def test_despues_no_exige_el_reinicio_si_el_deploy_no_cambia_la_imagen(banco, cambio):
    """El ❌ falso de ba8dfab (2026-10-06): un PR de CI que no tocaba la imagen
    salió entero de la caché, compose dejó el contenedor corriendo —con razón— y
    el script dijo «sirve el código viejo». Un ❌ que sale en cada PR así enseña
    a ignorar los ❌."""
    _correr(banco, "antes")
    sha = _desplegar(banco, cambio)
    r = _correr(banco, "despues", sha[:7])
    assert r.returncode == 0, r.stdout
    assert "no tenía por qué reiniciarse" in r.stdout


def test_una_nota_de_antes_tomada_despues_del_deploy_es_aviso_y_no_veredicto(banco):
    """Si «antes» corrió con el commit nuevo ya servido, la nota no sirve para
    juzgar el reinicio: decir ❌ o ✅ ahí sería inventar. La huella sí juzga."""
    _correr(banco, "antes")
    r = _correr(banco, "despues", banco["sha"][:7])
    assert r.returncode == 0, r.stdout
    assert "la nota de «antes» ya es de" in r.stdout
    assert "NO se reinició" not in r.stdout and "el contenedor corre el código de" in r.stdout


def test_una_nota_de_un_commit_que_no_conozco_exige_el_reinicio_como_siempre(banco):
    """Sin poder comparar, la regla vieja (reinicio siempre) es la segura."""
    (banco["repo"] / "data" / "verificar-deploy.estado").write_text(
        f"HEAD={SHA_VIEJO}\nARRANQUE=2026-09-21T10:00:00Z\n"
    )
    r = _correr(banco, "despues", banco["sha"][:7])
    assert r.returncode == 1, r.stdout
    assert "NO se reinició" in r.stdout and "no conozco 0000000" in r.stdout


def _contenedor(banco, destino):
    """Una copia de lo que la imagen copia, para hacer de /app del contenedor."""
    shutil.copytree(banco["repo"] / "app", destino / "app", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(banco["repo"] / "requirements.txt", destino)
    return destino


def test_despues_ve_que_el_contenedor_corre_otro_codigo_y_dice_que_archivo(banco, tmp_path):
    """Lo que ni deploy-info ni el reinicio prueban: qué código corre. Y si no
    cuadra, el operador necesita saber dónde mirar, no solo que no cuadra."""
    contenedor = _contenedor(banco, tmp_path / "contenedor")
    (contenedor / "app" / "main.py").write_text("print('v0')\n")
    r = _correr(banco, "despues", banco["sha"][:7], entorno={"FAKE_APP": str(contenedor)})
    assert r.returncode == 1, r.stdout
    assert "no es el del checkout" in r.stdout
    assert "checkout:   app/main.py" in r.stdout and "contenedor: app/main.py" in r.stdout

    r = _correr(banco, "despues", banco["sha"][:7], entorno={"FAKE_APP": str(tmp_path / "no-existe")})
    assert r.returncode == 1 and "no pude leer el código dentro de tramites-bot" in r.stdout


def test_la_huella_no_cuenta_pycache_de_ningun_lado(banco, tmp_path):
    """Un `python -m app.algo` en el checkout del VPS deja __pycache__; la imagen
    no lo trae (.dockerignore). Contarlo sería un ❌ falso, y no es de git."""
    pyc = banco["repo"] / "app" / "__pycache__" / "main.cpython-312.pyc"
    pyc.parent.mkdir()
    pyc.write_bytes(b"bytecode")
    contenedor = _contenedor(banco, tmp_path / "contenedor")
    r = _correr(banco, "despues", banco["sha"][:7], entorno={"FAKE_APP": str(contenedor)})
    assert "el contenedor corre el código de" in r.stdout, r.stdout


def test_despues_ve_en_el_checkout_un_archivo_que_no_es_del_commit(banco):
    """Alguien editó el servidor: el build toma el árbol, no el commit, así que
    contenedor y checkout cuadran y aun así no corre el commit que se dice."""
    (banco["repo"] / "app" / "parche.py").write_text("x = 1\n")
    r = _correr(banco, "despues", banco["sha"][:7])
    assert r.returncode == 1, r.stdout
    assert "archivos que no son de" in r.stdout and "app/parche.py" in r.stdout


def test_el_dockerfile_real_se_entiende_y_su_huella_cuadra(banco):
    """Si un cambio le da al Dockerfile una forma que el script no sabe leer (un
    COPY con banderas, otra etapa), la huella se apaga con un aviso y el deploy
    se queda sin prueba de qué código sirve. Esto lo ve en CI, antes del deploy."""
    sha = _desplegar(banco, {
        "Dockerfile": (RAIZ / "Dockerfile").read_text(encoding="utf-8"),
        **{f"{carpeta}/x.txt": carpeta for carpeta in ("templates", "static", "landing")},
    })
    r = _correr(banco, "despues", sha[:7])
    assert f"el contenedor corre el código de {sha[:7]}" in r.stdout, r.stdout
    assert "no sé comparar" not in r.stdout


@pytest.mark.parametrize(
    "dockerfile",
    [
        "FROM python\nWORKDIR /app\nCOPY --chown=app:app app/ app/\n",
        "FROM python\nWORKDIR /app\nCOPY app/ .\n",
        "FROM python\nWORKDIR /app\nCOPY . .\n",
        "FROM python\nWORKDIR /app\nADD app.tar.gz .\n",
        "FROM python AS base\nFROM base\nWORKDIR /app\nCOPY app/ app/\n",
        "FROM python\nWORKDIR /app\nCOPY app/ app/\nWORKDIR /srv\n",
    ],
    ids=["copy-con-banderas", "carpeta-a-la-raiz", "todo-el-contexto", "add", "dos-etapas", "workdir-despues"],
)
def test_un_dockerfile_que_no_sabe_leer_da_aviso_y_el_reinicio_se_exige_como_siempre(banco, dockerfile):
    """Comparar rutas que no son las mismas daría un ❌ falso, y adivinar, un ✅
    falso: el script dice que no sabe. Y sin saber qué copia la imagen, exige el
    reinicio como antes de la huella."""
    _correr(banco, "antes")
    sha = _desplegar(banco, {"Dockerfile": dockerfile})
    r = _correr(banco, "despues", sha[:7])
    assert "no sé comparar el código con este Dockerfile" in r.stdout, r.stdout
    assert "corre el código de" not in r.stdout and "no es el del checkout" not in r.stdout
    assert r.returncode == 1 and "NO se reinició" in r.stdout and "no sé leer el Dockerfile" in r.stdout


def test_el_dockerignore_no_saca_nada_de_lo_que_la_imagen_copia():
    """La huella cuenta lo que el Dockerfile copia tal como está en el checkout,
    y el reinicio no mira el .dockerignore. Las dos cosas valen mientras el
    .dockerignore solo saque lo que la imagen no copia (y __pycache__, que la
    huella también salta). Si un día saca algo de dentro de app/, templates/,
    static/ o landing/, el checkout lo tendría y el contenedor no: un ❌ falso
    en cada deploy. Este test lo ve antes."""
    fuentes = [
        renglon.split()[1].rstrip("/")
        for renglon in (RAIZ / "Dockerfile").read_text(encoding="utf-8").splitlines()
        if renglon.split()[:1] == ["COPY"]
    ]
    assert fuentes, "no encontré ningún COPY en el Dockerfile"
    invasores = []
    for patron in (RAIZ / ".dockerignore").read_text(encoding="utf-8").splitlines():
        patron = patron.strip()
        if not patron or patron.startswith("#"):
            continue
        limpio = patron.rstrip("/")
        if limpio in ("__pycache__", "**/__pycache__"):
            continue
        if patron.startswith("!") or "**" in limpio or any(
            fnmatch.fnmatchcase(fuente, limpio.split("/")[0]) for fuente in fuentes
        ):
            invasores.append(patron)
    assert not invasores, (
        f"el .dockerignore saca de lo que la imagen copia ({', '.join(fuentes)}): {invasores}. "
        "Enséñale a la huella de deploy/verificar-deploy.sh a saltarlo, o el próximo deploy da ❌ falso."
    )


def test_sin_argumentos_explica_las_tres_fases():
    """La ayuda se leía por número de renglón; la directiva de shellcheck del
    encabezado los recorrió y la ayuda dejó de decir cómo se llama cada fase."""
    r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 2
    for fase in ("verificar-deploy.sh antes", "verificar-deploy.sh durante <sha>", "verificar-deploy.sh despues <sha>"):
        assert fase in r.stdout, r.stdout
    assert "shellcheck" not in r.stdout


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
        env=_entorno(banco, ESPERA_MAX_SEGUNDOS="60"),
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
        env=_entorno(banco, ESPERA_MAX_SEGUNDOS="60", FAKE_STARTED="2026-09-21T11:00:00Z"),
    )
    time.sleep(2)
    (banco["repo"] / "data" / "deploy-info").write_text(f"{banco['sha']} 2026-09-21T12:00:00\n")
    salida, _ = proceso.communicate(timeout=60)
    assert proceso.returncode == 0, salida
    assert "está sirviendo" in salida and "== despues" in salida and "Sin fallos" in salida

def test_antes_exige_que_data_sea_del_uid_del_contenedor_cuando_el_dockerfile_fija_usuario(banco):
    """INFRA-5: el contenedor deja de ser root y data/ es un bind mount. Si el
    host no hizo el chown, el deploy entra en rollback cada dos minutos; el
    script lo dice ANTES. Sin USER en el Dockerfile, no opina."""
    r = _correr(banco, "antes")
    assert "pertenece al uid" not in r.stdout and "chown" not in r.stdout, "sin USER no debía opinar"

    (banco["repo"] / "Dockerfile").write_text("FROM python\nRUN useradd --uid 10001 app\nUSER app\n")
    r = _correr(banco, "antes")
    assert r.returncode == 1 and "chown -R 10001:10001 data/" in r.stdout, r.stdout


def test_ollama_presente_y_corriendo_es_ok(banco):
    """El servicio ollama del compose es opcional; si está, se reporta como los demás."""
    r = _correr(banco, "antes")
    assert "contenedor tramites-ollama: running" in r.stdout
    assert "Sin fallos" in r.stdout


def test_ollama_ausente_es_aviso_no_fallo(banco):
    """Sin COMPOSE_PROFILES=ollama el contenedor no existe: aviso, y el deploy
    sigue siendo válido (0 fallos). Presente pero caído sí es fallo: restart
    debería haberlo levantado."""
    r = _correr(banco, "antes", entorno={"FAKE_OLLAMA": "ausente"})
    assert "tramites-ollama: ausente" in r.stdout
    assert "Sin fallos" in r.stdout
    r = _correr(banco, "antes", entorno={"FAKE_OLLAMA": "exited"})
    assert "contenedor tramites-ollama: exited" in r.stdout
    assert "fallo(s)" in r.stdout
