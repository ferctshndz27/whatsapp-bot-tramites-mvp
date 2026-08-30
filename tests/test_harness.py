"""El harness de desarrollo, verificado como contrato en vez de como prosa.

`AGENTS.md` rutea cada dominio a una skill de `.claude/skills/`, y cada skill
cita archivos concretos del repo. Todo eso es texto: nada impedía renombrar una
skill, borrarla, o dejar citado un módulo que ya no existe. Este módulo le
aplica al harness el mismo truco que `tests/test_mirror_consistency.py` le
aplica al espejo MSSQL — análisis estático que vuelve ruidosa una divergencia
que de otro modo sería silenciosa.

Lo que deliberadamente NO verifica: que un ancla `archivo.py:132` apunte a la
línea correcta. Los anclas migran con cada refactor —la propia skill
`ai-ml-senior` lo advierte en su texto— y este módulo corre en CI, que gatea el
deploy del bot. Un número corrido no debe frenar producción; un archivo borrado
sí. Por eso el ancla solo se valida "en rango".
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
SKILLS = RAIZ / ".claude" / "skills"
AGENTS_MD = RAIZ / "AGENTS.md"
SETTINGS = RAIZ / ".claude" / "settings.json"

# Filas de la tabla de ruteo: "| lo que toca la tarea | `nombre-skill` |".
_FILA_RUTEO = re.compile(r"^\|.+\|\s*`([a-z0-9-]+)`\s*\|\s*$", re.M)

# Rutas del repo citadas en prosa, con ancla de línea opcional: "app/nlu.py",
# "app/nlu.py:132", "app/models.py:436-441" (del rango solo importa el inicio).
_CITA = re.compile(
    r"\b((?:app|tests|scripts|templates|deploy|docs)/[A-Za-z0-9_./-]+"
    r"\.(?:py|html|sh|json|md|yml))(?::(\d+))?"
)

# Ejemplos genéricos en prosa, no citas de un archivo real.
_MARCADORES = {"tests/test_x.py"}

# Herramientas locales del operador: existen en su máquina pero están fuera del
# repo (`.git/info/exclude`), así que en un clone limpio —y por lo tanto en CI—
# no hay archivo que encontrar. Se citan a propósito; no se exigen en disco.
_LOCALES = {"scripts/ship.sh"}

# Barandales del operador. `scripts/ship.sh` sí corre `git push`, pero lo corre
# el humano en su propia terminal: el deny ata a la herramienta Bash del agente,
# no a él. Que estas entradas sigan ahí es la mitad del valor de la política.
_DENY_OBLIGATORIO = ("git push", "ship.sh", "ssh")


def _skills_en_disco() -> set[str]:
    return {d.name for d in SKILLS.iterdir() if (d / "SKILL.md").is_file()}


def _skills_en_tabla() -> set[str]:
    return set(_FILA_RUTEO.findall(AGENTS_MD.read_text(encoding="utf-8")))


def _documentos() -> list[Path]:
    """Todo lo que forma el harness: la tabla de ruteo y las skills que rutea."""
    return [AGENTS_MD, *sorted(SKILLS.glob("*/SKILL.md"))]


def _citas(doc: Path) -> list[tuple[str, str]]:
    return _CITA.findall(doc.read_text(encoding="utf-8"))


def test_cada_skill_declara_el_nombre_de_su_carpeta():
    """El `name:` del frontmatter es con lo que se invoca la skill. Si no
    coincide con la carpeta, la tabla rutea a un nombre que no existe y la
    skill jamás carga — falla en silencio, que es la peor manera de fallar."""
    for carpeta in sorted(SKILLS.iterdir()):
        skill = carpeta / "SKILL.md"
        if not skill.is_file():
            continue
        declarado = ""
        for linea in skill.read_text(encoding="utf-8").splitlines()[:10]:
            if linea.startswith("name:"):
                declarado = linea.split(":", 1)[1].strip()
                break
        assert declarado == carpeta.name, (
            f"{skill.relative_to(RAIZ)} declara name: {declarado!r} "
            f"pero vive en la carpeta {carpeta.name!r}"
        )


def test_la_tabla_de_ruteo_y_las_skills_en_disco_no_divergen():
    """Bidireccional a propósito: una skill sin fila no la invoca nadie, y una
    fila sin skill manda al agente a cargar un archivo inexistente."""
    disco, tabla = _skills_en_disco(), _skills_en_tabla()
    assert not disco - tabla, (
        "skills en .claude/skills/ que ninguna fila de AGENTS.md rutea "
        f"(nadie las va a invocar): {sorted(disco - tabla)}"
    )
    assert not tabla - disco, (
        "la tabla de ruteo de AGENTS.md manda a skills que no existen en "
        f".claude/skills/: {sorted(tabla - disco)}"
    )


def test_los_archivos_que_cita_el_harness_existen():
    """Una skill que cita `app/foo.py` después de que `app/foo.py` se renombró
    manda al agente a leer humo, y encima lo hace con tono de autoridad."""
    huerfanas = set()
    for doc in _documentos():
        for ruta, _ in _citas(doc):
            if ruta in _MARCADORES or ruta in _LOCALES:
                continue
            if not (RAIZ / ruta).exists():
                huerfanas.add(f"{doc.relative_to(RAIZ)} cita {ruta}")
    assert not huerfanas, "el harness cita archivos que ya no existen:\n  " + "\n  ".join(
        sorted(huerfanas)
    )


def test_los_anclas_de_linea_caen_dentro_del_archivo():
    """Solo "en rango", nunca semántico. Un ancla que apunta más allá del final
    del archivo es prueba de que el archivo se encogió y la cita quedó huérfana;
    un ancla que se corrió veinte líneas es deuda tolerable, no una emergencia
    que deba frenar el deploy del bot."""
    fuera = set()
    for doc in _documentos():
        for ruta, linea in _citas(doc):
            if not linea or ruta in _MARCADORES or ruta in _LOCALES:
                continue
            destino = RAIZ / ruta
            if not destino.exists():
                continue  # ya lo reporta el test de rutas; un solo dueño por falla
            total = len(destino.read_text(encoding="utf-8").splitlines())
            if int(linea) > total:
                fuera.add(
                    f"{doc.relative_to(RAIZ)} cita {ruta}:{linea} "
                    f"y el archivo tiene {total} líneas"
                )
    assert not fuera, "anclas que apuntan fuera del archivo:\n  " + "\n  ".join(sorted(fuera))


def test_la_politica_de_permisos_conserva_los_barandales_del_operador():
    """Mismo candado que `tests/test_agent_tools.py` le pone a
    `PERMISOS["cobrar"] == "deny"`: una regla de seguridad que se puede borrar
    sin que nada se queje no es una regla, es una sugerencia."""
    permisos = json.loads(SETTINGS.read_text(encoding="utf-8")).get("permissions")
    if permisos is None:
        pytest.skip(
            ".claude/settings.json todavía no declara `permissions` — el agente "
            "tiene prohibido escribir su propia política, la pega el operador"
        )
    deny = " ".join(permisos.get("deny", []))
    faltan = [regla for regla in _DENY_OBLIGATORIO if regla not in deny]
    assert not faltan, (
        f"la política de permisos perdió barandales del operador: {faltan}. "
        "El humano corre los push, los ssh y los deploys; el agente no."
    )
