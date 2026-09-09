"""El workflow de CI, leído como contrato.

CI es lo único que se interpone entre un commit y `production`: `promote` mueve
la rama que el VPS sigue por cron. Este archivo vigila las dos condiciones que
tienen que cumplirse para que ese gate signifique algo, porque las dos se pueden
aflojar con una línea y sin que nada se queje.

Se lee el YAML con regex y no con `import yaml` a propósito: pyyaml solo está
disponible como dependencia transitiva de `uvicorn[standard]`, y una prueba del
harness no debería depender de que eso siga siendo cierto. Mismo criterio que
tests/test_harness.py y tests/test_dependencias.py — leer archivos del repo
como datos.
"""

import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
WORKFLOW = RAIZ / ".github" / "workflows" / "ci-cd.yml"


def _texto() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_ci_construye_la_imagen():
    """REGLA: `pytest` verde NO significa que la imagen construya.

    Durante mucho tiempo el workflow no mencionaba docker ni una vez: instalaba
    requirements en 3.12 y corría las pruebas. Un `COPY` que falta, un cambio en
    el Dockerfile o una dependencia que necesite compilador —`ubuntu-latest`
    trae gcc y compila el sdist; `python:3.12-slim` no— pasaban verdes hasta el
    VPS. Precedente real: el commit que sacó el CSS a `static/` necesitó añadir
    su `COPY static/` en el mismo commit, y lo atrapó el autor, no CI.
    """
    assert re.search(r"^\s*run:\s*docker build\b", _texto(), re.M), (
        "el workflow ya no construye la imagen: un Dockerfile roto llegaría a "
        "production y el VPS entraría en un bucle de rollback cada 2 minutos"
    )


def test_promote_espera_al_build_y_a_las_pruebas():
    """REGLA: construir la imagen no sirve de nada si `promote` no la espera.

    El job puede seguir ahí, ponerse rojo, y `production` avanzar igual — un
    verde decorativo. Decisión tomada y consciente (septiembre de 2026): el
    build BLOQUEA. El precio aceptado es que una caída de Docker Hub detenga los
    deploys; a cambio, un build roto no llega nunca a la rama que el VPS sigue.
    """
    bloque = re.search(r"^\s*promote:\s*$(.*?)(?=^\s{2}\w+:\s*$|\Z)", _texto(), re.M | re.S)
    assert bloque, "no se encontró el job `promote` en el workflow"

    needs = re.search(r"needs:\s*(.+)", bloque.group(1))
    assert needs, "`promote` ya no declara `needs`: correría en paralelo con las pruebas"

    declarado = needs.group(1)
    assert "test" in declarado, "`promote` dejó de esperar a las pruebas"
    assert "build" in declarado, (
        f"`promote` ya no espera al build (needs: {declarado.strip()}). Si fue a propósito, "
        "es una decisión de seguridad del deploy: cámbiala aquí también y di por qué."
    )


def test_promote_no_fuerza_la_rama_production():
    """`git push origin HEAD:production` sin `--force` es lo que hace que un
    push a mano sobre `production` rompa el promote de forma VISIBLE en vez de
    ser sobrescrito en silencio. Está documentado en devops-senior como una de
    las cosas que no se tocan."""
    empuja = re.search(r"git push origin HEAD:production(.*)", _texto())
    assert empuja, "el paso que promueve a production cambió de forma"
    assert "--force" not in empuja.group(1) and "-f " not in empuja.group(1), (
        "promote empuja con --force: un push manual a production dejaría de ser detectable"
    )
