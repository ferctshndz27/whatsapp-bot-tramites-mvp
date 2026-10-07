"""El CI local de Jenkins (`Jenkinsfile` + `ci/`) sigue cumpliendo lo que promete.

El Jenkinsfile complementa a GitHub Actions: en el Jenkins del equipo (job «tramites») corre shellcheck,
pytest, la imagen de producción y el juez del CRM en cada commit. Tres cosas se rompen en silencio si
nadie las vigila:

- `ci/Dockerfile` deja de empezar como el `Dockerfile` de producción. pytest seguiría verde, pero sobre
  otra base u otras dependencias que las del VPS (lo mismo que la skill devops-senior exige de los dos
  pines de Python), y la imagen de producción perdería la capa de pip que comparte con la de CI.
- Un agente en contenedor pierde el tope de recursos (el equipo corre un modelo local) o gana permisos.
- El hook que avisa a Jenkins pisa un post-commit ajeno o, peor, hace fallar un commit.
"""

import os
import re
import subprocess
import time
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
DOCKERFILE = RAIZ / "Dockerfile"
DOCKERFILE_CI = RAIZ / "ci" / "Dockerfile"
JENKINSFILE = RAIZ / "Jenkinsfile"
INSTALADOR = RAIZ / "ci" / "instalar-hook.sh"

# Un agente en contenedor del Jenkinsfile: `docker { ... }` o `dockerfile { ... }` (sin llaves dentro).
_AGENTE = re.compile(r"\b(docker|dockerfile)\s*\{([^{}]*)\}")


def _instrucciones(dockerfile: Path) -> list[str]:
    """Las instrucciones, sin comentarios, con las líneas continuadas unidas y el espacio normalizado."""
    lineas = dockerfile.read_text(encoding="utf-8").splitlines()
    texto = "\n".join(l for l in lineas if not l.lstrip().startswith("#")).replace("\\\n", " ")
    return [" ".join(l.split()) for l in texto.splitlines() if l.strip()]


def _jenkinsfile_sin_comentarios() -> str:
    """Las reglas viven en los comentarios («Sin --privileged…»): no deben contar como uso."""
    lineas = JENKINSFILE.read_text(encoding="utf-8").splitlines()
    return "\n".join(re.sub(r"\s+//.*$", "", l) for l in lineas if not l.lstrip().startswith("//"))


def test_la_imagen_de_ci_empieza_como_la_de_produccion():
    prod, ci = _instrucciones(DOCKERFILE), _instrucciones(DOCKERFILE_CI)
    fin = next(i for i, l in enumerate(prod) if l.startswith("RUN pip install"))
    prefijo = prod[: fin + 1]
    inicio = ci[: len(prefijo)]
    inicio[0] = re.sub(r"\s+AS\s+\S+$", "", inicio[0], flags=re.I)
    assert inicio == prefijo, (
        "ci/Dockerfile debe empezar, a la letra, como el Dockerfile hasta el `pip install` (mismo "
        "digest de Python, mismas dependencias):\n"
        f"  Dockerfile:    {prefijo}\n  ci/Dockerfile: {inicio}"
    )


def test_las_imagenes_de_ci_estan_fijadas_por_digest():
    etapas = set()
    for linea in _instrucciones(DOCKERFILE_CI):
        ref = re.match(r"(?:FROM\s+|COPY\s+--from=)(\S+)", linea)
        if ref and ref.group(1) not in etapas:
            assert "@sha256:" in ref.group(1), f"ci/Dockerfile usa {ref.group(1)} sin digest"
        etapa = re.search(r"\sAS\s+(\S+)$", linea, re.I)
        if linea.startswith("FROM") and etapa:
            etapas.add(etapa.group(1))
    for imagen in re.findall(r"\bimage\s+'([^']+)'", _jenkinsfile_sin_comentarios()):
        assert "@sha256:" in imagen, f"el Jenkinsfile usa {imagen} sin digest"


def test_los_agentes_del_jenkinsfile_llevan_tope_y_ningun_permiso_de_mas():
    codigo = _jenkinsfile_sin_comentarios()
    agentes = _AGENTE.findall(codigo)
    assert len(agentes) >= 3, "no encontré los agentes en contenedor del Jenkinsfile: ¿cambió su forma?"
    for tipo, cuerpo in agentes:
        args = re.search(r"\bargs\s+'([^']*)'", cuerpo)
        assert args and args.group(1).startswith("--cpus=4 -m 4g"), (
            f"agente {tipo} sin args '--cpus=4 -m 4g …' (el equipo corre un modelo local):{cuerpo}"
        )
    for prohibido in ("--privileged", "docker.sock", ":latest"):
        assert prohibido not in codigo, f"el Jenkinsfile no debe usar {prohibido}"


def test_los_scripts_que_orquesta_el_jenkinsfile_existen_y_son_ejecutables():
    citados = set(re.findall(r"\bci/[\w.-]+\.sh\b", JENKINSFILE.read_text(encoding="utf-8")))
    assert {"ci/shellcheck.sh", "ci/pytest.sh", "ci/imagen.sh", "ci/navegador.sh"} <= citados
    for script in sorted(citados):
        ruta = RAIZ / script
        assert ruta.is_file() and os.access(ruta, os.X_OK), (
            f"{script}: Jenkins lo ejecuta directo, así que tiene que existir con bit de ejecución"
        )


# ── El hook post-commit (ci/instalar-hook.sh) ─────────────────────────────────────────────────


@pytest.fixture()
def clon(tmp_path):
    """Un repo con un commit, aislado de la configuración git de quien corre la prueba, y un
    jenkinsctl de mentira que anota con qué argumentos lo llamaron."""
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "inicio"], cwd=repo, env=env, check=True)
    llamadas = tmp_path / "llamadas"
    ctl = tmp_path / "bin" / "jenkinsctl"
    ctl.parent.mkdir()
    ctl.write_text(f'#!/bin/sh\necho "$*" >> "{llamadas}"\n')
    ctl.chmod(0o755)
    return {"repo": repo, "env": env, "ctl": ctl, "llamadas": llamadas, "tmp": tmp_path}


def _instalar(clon, cwd=None):
    return subprocess.run(["bash", str(INSTALADOR), str(clon["ctl"])], cwd=cwd or clon["repo"],
                          env=clon["env"], capture_output=True, text=True)


def _commit(clon, cwd=None):
    return subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "otro"], cwd=cwd or clon["repo"],
                          env=clon["env"], capture_output=True, text=True)


def _llamadas(clon, segundos=10.0) -> str:
    """El hook llama a jenkinsctl en segundo plano: hay que esperarlo, no leerlo de inmediato."""
    fin = time.monotonic() + segundos
    while time.monotonic() < fin:
        if clon["llamadas"].is_file() and clon["llamadas"].read_text().strip():
            return clon["llamadas"].read_text()
        time.sleep(0.05)
    return ""


def test_el_hook_avisa_a_jenkins_en_cada_commit(clon):
    r = _instalar(clon)
    assert r.returncode == 0, r.stderr
    assert _commit(clon).returncode == 0
    assert _llamadas(clon).strip() == "scan tramites --quiet"


def test_el_hook_vale_para_los_worktrees_del_clon(clon):
    """Los worktrees comparten .git/hooks: instalar desde uno deja el hook donde todos lo ven."""
    wt = clon["tmp"] / "wt"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "otra", str(wt)], cwd=clon["repo"],
                   env=clon["env"], check=True)
    assert _instalar(clon, cwd=wt).returncode == 0
    assert (clon["repo"] / ".git" / "hooks" / "post-commit").is_file()
    assert _commit(clon, cwd=wt).returncode == 0
    assert "scan tramites" in _llamadas(clon)


def test_reinstalar_no_cambia_nada_y_un_hook_ajeno_no_se_pisa(clon):
    hook = clon["repo"] / ".git" / "hooks" / "post-commit"
    assert _instalar(clon).returncode == 0
    primero = hook.read_text()
    assert _instalar(clon).returncode == 0
    assert hook.read_text() == primero

    hook.write_text("#!/bin/sh\necho hook de otro\n")
    r = _instalar(clon)
    assert r.returncode == 1 and "no lo toco" in r.stderr
    assert hook.read_text() == "#!/bin/sh\necho hook de otro\n"


def test_con_core_hookspath_no_instala_a_ciegas(clon):
    """Con core.hooksPath (a veces global) el hook iría a parar fuera de este clon."""
    subprocess.run(["git", "config", "core.hooksPath", "ganchos"], cwd=clon["repo"], env=clon["env"], check=True)
    r = _instalar(clon)
    assert r.returncode == 1 and "core.hooksPath" in r.stderr
    assert not (clon["repo"] / "ganchos" / "post-commit").exists()


def test_sin_jenkins_el_commit_no_falla(clon):
    """Jenkins apagado (jenkinsctl falla) o jenkinsctl borrado: el commit pasa igual."""
    assert _instalar(clon).returncode == 0
    clon["ctl"].write_text("#!/bin/sh\nexit 1\n")
    assert _commit(clon).returncode == 0
    clon["ctl"].unlink()
    assert _commit(clon).returncode == 0
