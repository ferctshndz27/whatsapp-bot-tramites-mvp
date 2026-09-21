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

import importlib.util
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
SKILLS = RAIZ / ".claude" / "skills"
AGENTS_MD = RAIZ / "AGENTS.md"
SETTINGS = RAIZ / ".claude" / "settings.json"

# Desde el 2026-09-21 la capa de proceso es rsc-harness. `.rsc.json` es su
# declaración versionada: qué asistentes, qué versión del catálogo y QUÉ SKILLS
# gestiona. Esas skills comparten `.claude/skills/` con las del repo pero son
# enlaces hacia `.rsc/`, que git ignora: en la máquina del operador existen y en
# un clone limpio —o sea, en CI— no. Un assert que las cuente sale verde en CI y
# rojo en local, que es la peor combinación: el candado que gatea el deploy diría
# que todo va bien justo donde no mira. Por eso todo lo de abajo habla de skills
# PROPIAS, y la frontera la pone el archivo versionado, no la forma del enlace
# (en Windows rsc copia en vez de enlazar).
RSC_JSON = RAIZ / ".rsc.json"
RSC_BOOTSTRAP = RAIZ / ".claude" / "rsc-bootstrap.mjs"

# El mismo harness visto desde los agentes de Google. Antigravity (`agy`, el
# que usa el operador) y Gemini CLI escanean `.agents/skills/` —Antigravity
# NO mira `.gemini/skills/`, de ahí que los enlaces vivan en el primero, que
# las dos herramientas sí leen—. `GEMINI.md` importa `AGENTS.md` igual que
# `CLAUDE.md`, y el hook de `.gemini/` carga el mismo deny que
# `.claude/settings.json` (solo Gemini CLI; los permisos de Antigravity viven
# en el home del operador, fuera del repo). Ninguno de los dos tiene el candado
# de plataforma que impide al agente reescribir su propia política, así que
# aquí el candado es este archivo.
CLAUDE_MD = RAIZ / "CLAUDE.md"
GEMINI_MD = RAIZ / "GEMINI.md"
AGENTS_SKILLS = RAIZ / ".agents" / "skills"
GITIGNORE = RAIZ / ".gitignore"
GEMINI_SETTINGS = RAIZ / ".gemini" / "settings.json"
GEMINI_HOOK = RAIZ / ".gemini" / "hooks" / "guardrails.py"
GEMINI_POLICY = RAIZ / ".gemini" / "policies" / "guardrails.toml"

# Filas de la tabla de ruteo: "| lo que toca la tarea | `nombre-skill` |".
_FILA_RUTEO = re.compile(r"^\|.+\|\s*`([a-z0-9-]+)`\s*\|\s*$", re.M)

# Rutas del repo citadas en prosa, con ancla de línea opcional: "app/nlu.py",
# "app/nlu.py:132", "app/models.py:436-441" (del rango solo importa el inicio).
# Los directorios con punto (.claude/, .gemini/) van en su propia alternativa
# porque `\b` no reconoce el borde entre un backtick y un punto —y en estas
# skills toda ruta va entre backticks—, así que con el primer grupo jamás
# casarían y sus citas quedarían sin vigilar. El lookbehind excluye los
# caracteres que forman una ruta, no cualquier no-espacio: si excluyera el
# backtick, que es justo lo que las precede, no casaría ninguna.
_CITA = re.compile(
    r"((?:\b(?:app|tests|scripts|templates|deploy|docs)"
    r"|(?<![A-Za-z0-9_./-])\.(?:claude|gemini))"
    r"/[A-Za-z0-9_./-]+\.(?:py|html|sh|json|md|yml|toml|mjs))(?::(\d+))?"
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

# Las once secciones que comparten hoy las catorce skills, en este orden. No es un
# template estético: `When to use` / `When NOT to use` son lo que evita que una
# tarea de dos líneas cargue cuatro roles, y `Definition of done` es la única
# condición de salida escrita. La forma ya existía de facto; lo que faltaba era
# el candado, así que la skill número catorce podía nacer sin ella y nadie se
# enteraba hasta ver al agente cargar un rol de más. Una skill puede agregar
# secciones propias: lo que se exige es que estas once estén y se lean en este
# orden. Si alguna necesitara una excepción, va nombrada aquí como `_LOCALES`.
_SECCIONES_SKILL = (
    "Purpose",
    "When to use",
    "When NOT to use",
    "Responsibilities",
    "Analysis checklist",
    "Technical principles",
    "Common mistakes",
    "Quality bar",
    "Security",
    "Performance",
    "Definition of done",
)

_H2 = re.compile(r"^##\s+(.+?)\s*$", re.M)


def _rsc() -> dict:
    """La declaración de rsc, o {} si el repo todavía no la tiene: sin ella no
    hay skills gestionadas y todo lo de `.claude/skills/` es del repo, que es
    exactamente como se comportaba este módulo antes de rsc."""
    return json.loads(RSC_JSON.read_text(encoding="utf-8")) if RSC_JSON.is_file() else {}


def _skills_gestionadas() -> set[str]:
    return set(_rsc().get("skills", []))


def _carpetas_propias() -> list[Path]:
    gestionadas = _skills_gestionadas()
    return sorted(
        d for d in SKILLS.iterdir() if (d / "SKILL.md").is_file() and d.name not in gestionadas
    )


def _skills_en_disco() -> set[str]:
    """Las skills PROPIAS: las que viven en `.claude/skills/` y rsc no declara."""
    return {d.name for d in _carpetas_propias()}


def _skills_en_tabla() -> set[str]:
    return set(_FILA_RUTEO.findall(AGENTS_MD.read_text(encoding="utf-8")))


def _documentos() -> list[Path]:
    """Todo lo que forma el harness: la tabla de ruteo, las skills que rutea y
    las portadas de un renglón que la importan.

    `CLAUDE.md` va condicionado porque `.gitignore` lo excluye: existe en la
    máquina de quien trabaja y NO en un clone limpio, así que exigirlo pondría
    rojo el CI —y con él el deploy del bot— por un archivo que el repo decidió
    no versionar. Mismo criterio que `_LOCALES` con `scripts/ship.sh`."""
    portadas = [p for p in (CLAUDE_MD, GEMINI_MD) if p.is_file()]
    return [AGENTS_MD, *portadas, *(d / "SKILL.md" for d in _carpetas_propias())]


def _citas(doc: Path) -> list[tuple[str, str]]:
    return _CITA.findall(doc.read_text(encoding="utf-8"))


def test_cada_skill_declara_el_nombre_de_su_carpeta():
    """El `name:` del frontmatter es con lo que se invoca la skill. Si no
    coincide con la carpeta, la tabla rutea a un nombre que no existe y la
    skill jamás carga — falla en silencio, que es la peor manera de fallar."""
    for carpeta in _carpetas_propias():
        skill = carpeta / "SKILL.md"
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


def test_cada_skill_conserva_la_forma_del_harness():
    """`description:` es el texto con el que el modelo decide si una skill aplica,
    y las secciones son el esqueleto que vuelve comparables a catorce roles
    distintos. Una skill sin `description:` existe pero no la elige nadie; una sin
    `When NOT to use` deja que una tarea de dos líneas cargue cuatro roles. Las dos
    fallan tan calladas como una skill mal nombrada, así que se verifican igual.

    Solo las propias: las de rsc traen la forma de SU catálogo, y exigirles las
    once secciones sería vigilar un archivo que ni es nuestro ni está en el repo."""
    for carpeta in _carpetas_propias():
        skill = carpeta / "SKILL.md"
        texto = skill.read_text(encoding="utf-8")

        descripcion = ""
        for linea in texto.splitlines()[:10]:
            if linea.startswith("description:"):
                descripcion = linea.split(":", 1)[1].strip()
                break
        assert descripcion, (
            f"{skill.relative_to(RAIZ)} no declara `description:` en el frontmatter: "
            "es lo que el modelo lee para decidir si la skill aplica, y sin eso la "
            "skill se puede rutear pero nunca se elige sola"
        )

        secciones = _H2.findall(texto)
        faltan = [s for s in _SECCIONES_SKILL if s not in secciones]
        assert not faltan, (
            f"{skill.relative_to(RAIZ)} no tiene la forma común del harness, le "
            f"faltan secciones: {faltan}"
        )
        propias = [s for s in secciones if s in _SECCIONES_SKILL]
        assert propias == list(_SECCIONES_SKILL), (
            f"{skill.relative_to(RAIZ)} tiene las secciones comunes desordenadas o "
            f"repetidas: se leen {propias}, se esperaban {list(_SECCIONES_SKILL)}"
        )


# ---------------------------------------------------------------------------
# La capa de proceso: rsc-harness
# ---------------------------------------------------------------------------
#
# De rsc no se vigila el contenido —es un catálogo ajeno, fijado por versión— sino
# las tres cosas que un clone necesita para no romperse en silencio: que la
# declaración siga diciendo lo que decidió el operador, que lo gestionado no
# entre al repo ni le pise el nombre a una skill propia, y que los hooks
# versionados apunten a algo que el clone sí trae.


def _gitignore() -> set[str]:
    """Las reglas de `.gitignore`, normalizadas como las compara git: `.rsc`,
    `/.rsc` y `.rsc/` son la misma regla."""
    lineas = GITIGNORE.read_text(encoding="utf-8").splitlines()
    return {l.strip().lstrip("/").rstrip("/") for l in lineas if l.strip() and not l.startswith("#")}


def test_rsc_json_sigue_diciendo_lo_que_decidio_el_operador():
    """`.rsc.json` es la decisión que viaja por git. Quien clona reconstruye el
    harness con `npx @ericrisco/rsc@<catalogVersion> sync`, así que una versión
    sin fijar le daría a cada máquina el catálogo del día en que clonó.

    El target es solo `claude` a propósito: `AGENTS.md` les dice a Gemini CLI y a
    Antigravity que no tienen capa de rsc. Si esto se pone rojo porque se agregó
    un target, lo que sigue no es relajar el assert: es reescribir esa sección."""
    rsc = _rsc()
    assert rsc, (
        "falta .rsc.json: el harness de proceso es rsc-harness y sin su declaración "
        "un clone no sabe qué reconstruir"
    )
    assert rsc.get("targets") == ["claude"], (
        f".rsc.json declara targets {rsc.get('targets')!r}; el operador decidió solo 'claude' "
        "y AGENTS.md lo afirma en su sección de proceso"
    )
    version = str(rsc.get("catalogVersion") or "")
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), (
        f".rsc.json no fija una versión exacta del catálogo (catalogVersion={version!r})"
    )


def test_lo_que_gestiona_rsc_no_entra_al_repo_ni_pisa_a_las_propias():
    """Una skill gestionada es un enlace hacia `.rsc/`, que git ignora: si el
    enlace se versionara, cada clone recibiría un enlace roto. rsc agrega la
    línea de `.gitignore` por cada una; este assert nota el día que falte.

    Y el choque de nombres: rsc se niega a instalar una skill del catálogo que
    se llame igual que una propia, pero esa defensa vive en SU instalador y en la
    máquina del operador. Aquí se afirma desde el repo: un mismo nombre en la
    tabla de ruteo y en `.rsc.json` es una fila que ya no se sabe a cuál de las
    dos manda."""
    gestionadas = _skills_gestionadas()
    assert gestionadas, ".rsc.json no declara ninguna skill gestionada"
    ignoradas = _gitignore()
    assert ".rsc" in ignoradas, ".gitignore dejó de ignorar .rsc/ (estado local de la máquina)"
    sin_ignorar = sorted(g for g in gestionadas if f".claude/skills/{g}" not in ignoradas)
    assert not sin_ignorar, (
        "skills gestionadas por rsc que .gitignore no excluye —un `git add .` versionaría "
        f"enlaces hacia un directorio ignorado—: {sin_ignorar}"
    )
    pisadas = sorted(gestionadas & _skills_en_tabla())
    assert not pisadas, (
        f"la tabla de ruteo de AGENTS.md usa nombres que rsc gestiona: {pisadas}. "
        "Renombra la skill propia: la fila no puede mandar a dos skills distintas"
    )
    enlazadas = sorted(d.name for d in AGENTS_SKILLS.iterdir() if d.name in gestionadas)
    assert not enlazadas, (
        f".agents/skills/ enlaza skills gestionadas por rsc: {enlazadas}. El operador instaló "
        "rsc solo para Claude; para los agentes de Google serían enlaces rotos en cada clone"
    )


def test_el_cerebro_de_rsc_se_queda_local():
    """rsc recomienda versionar `02-DOCS/` y `01-TOOLS/`; el operador decidió que
    no —nada de documentos se sube, igual que `docs/`—. Ahí viven su perfil, el
    plan aceptado, la constitución y los documentos de FTD en curso."""
    faltan = sorted({"02-DOCS", "01-TOOLS"} - _gitignore())
    assert not faltan, f".gitignore dejó de ignorar lo que el operador decidió mantener local: {faltan}"


def test_los_hooks_de_rsc_pasan_por_el_bootstrap_versionado():
    """Los scripts de los hooks viven en `.rsc/`, que un clone no trae. Si
    `settings.json` los llamara directo, cada sesión en un clone abriría con el
    stack trace del cargador de módulos en vez de un hook. Por eso pasan por
    `.claude/rsc-bootstrap.mjs`, el único archivo del harness que sí se versiona:
    delega cuando `.rsc/` está montado y avisa una vez cuando no.

    Y sin rutas absolutas: un `settings.json` versionado con la carpeta de esta
    máquina horneada está roto en cualquier otro checkout."""
    hooks = json.loads(SETTINGS.read_text(encoding="utf-8")).get("hooks", {})
    de_rsc = [
        h.get("command", "")
        for entradas in hooks.values()
        for entrada in entradas
        for h in entrada.get("hooks", [])
        if h.get("type") == "command" and ".rsc/" in h.get("command", "")
    ]
    assert de_rsc, (
        ".rsc.json declara el target claude pero .claude/settings.json no registra ningún "
        "hook de rsc: el operador corre `npx @ericrisco/rsc@<catalogVersion> sync`"
    )
    directos = [c for c in de_rsc if ".claude/rsc-bootstrap.mjs" not in c]
    assert not directos, f"hooks de rsc que no pasan por el bootstrap versionado: {directos}"
    absolutos = [c for c in de_rsc if "${CLAUDE_PROJECT_DIR}" not in c or str(RAIZ) in c]
    assert not absolutos, f"hooks de rsc con la ruta de esta máquina horneada: {absolutos}"
    assert RSC_BOOTSTRAP.is_file(), (
        ".claude/settings.json llama a .claude/rsc-bootstrap.mjs y el archivo no está: "
        "se versiona junto con .rsc.json y settings.json, los tres o ninguno"
    )


# ---------------------------------------------------------------------------
# El mismo harness desde Gemini CLI
# ---------------------------------------------------------------------------


def _hook_gemini():
    """El módulo del hook, cargado por ruta: es un script suelto, no un paquete,
    y el test quiere sus tablas (PREFIJOS_SHELL, ...) además de ejecutarlo."""
    spec = importlib.util.spec_from_file_location("guardrails_gemini", GEMINI_HOOK)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _regla_claude_a_ruta(regla: str) -> str:
    """"Read(./.env)" -> ".env"; "Edit(./data/**)" -> "data/"; "Bash(git push:*)" -> "git push"."""
    interior = regla[regla.index("(") + 1 : -1]
    if interior.endswith(":*"):
        interior = interior[:-2]
    if interior.startswith("./"):
        interior = interior[2:]
    if interior.endswith("/**"):
        interior = interior[:-2]
    return interior


def _deny_de_claude() -> list[str] | None:
    permisos = json.loads(SETTINGS.read_text(encoding="utf-8")).get("permissions")
    return None if permisos is None else permisos.get("deny", [])


def test_cada_portada_presente_importa_el_mismo_agents_md():
    """Una portada por agente, un solo contenido: si alguna dejara de importar
    AGENTS.md, ese agente trabajaría sin reglas ni tabla de ruteo y nada se lo
    diría. `GEMINI.md` se exige siempre —está versionado—; `CLAUDE.md` solo si
    está presente, porque `.gitignore` lo excluye del repo."""
    assert GEMINI_MD.is_file(), "falta GEMINI.md: Gemini CLI no leería AGENTS.md"
    for portada in (p for p in (CLAUDE_MD, GEMINI_MD) if p.is_file()):
        lineas = portada.read_text(encoding="utf-8").splitlines()
        assert "@AGENTS.md" in lineas, f"{portada.name} no importa AGENTS.md"
    # Gemini toma como import cualquier `@ruta` al inicio de línea o tras un
    # espacio, fuera de código. Un `@` suelto en GEMINI.md sería un import
    # fallido metido en el contexto del modelo.
    fuera_de_codigo = re.sub(r"`[^`]*`", "", GEMINI_MD.read_text(encoding="utf-8"))
    extras = set(re.findall(r"(?:^|\s)@(\S+)", fuera_de_codigo)) - {"AGENTS.md"}
    assert not extras, f"GEMINI.md tiene tokens que Gemini intentaría importar: {sorted(extras)}"


def test_las_skills_del_repo_estan_enlazadas_para_los_agentes_de_google():
    """Las catorce, por enlace y en `.agents/skills/`.

    Bidireccional para lo que es del repo: una skill sin enlace no existe para
    Antigravity ni para Gemini CLI, y un enlace sin skill apunta a la nada. Lo
    que NO es enlace se ignora a propósito: ese directorio es la raíz de
    personalización del operador y sus propias skills viven ahí (hoy
    `tidyfactor-styler`), fuera del repo y fuera de este contrato.
    """
    del_repo = _skills_en_disco()
    enlaces = {d.name for d in AGENTS_SKILLS.iterdir() if d.is_symlink()}
    assert del_repo <= enlaces, (
        "skills de .claude/skills/ sin enlace en .agents/skills/ —Antigravity y Gemini "
        f"no las ven—: {sorted(del_repo - enlaces)}. Créalos con:\n  " + "\n  ".join(
            f"ln -s ../../.claude/skills/{n} .agents/skills/{n}" for n in sorted(del_repo - enlaces)
        )
    )
    assert enlaces <= del_repo, (
        f"enlaces en .agents/skills/ que no corresponden a ninguna skill: {sorted(enlaces - del_repo)}"
    )
    for nombre in sorted(enlaces):
        enlace = AGENTS_SKILLS / nombre
        assert not os.path.isabs(os.readlink(enlace)), (
            f".agents/skills/{nombre} es un enlace absoluto: se rompe en cualquier otro clone"
        )
        assert enlace.resolve() == (SKILLS / nombre).resolve(), (
            f".agents/skills/{nombre} apunta a {os.readlink(enlace)}, no a .claude/skills/{nombre}"
        )
        assert (enlace / "SKILL.md").is_file()


def test_gitignore_versiona_los_enlaces_y_solo_los_enlaces():
    """`.agents/` es del operador: se versionan los catorce enlaces y nada más.

    Re-incluir `.agents/skills/*` entero metería al repo la siguiente skill que
    el operador guarde ahí, y un `git add .` bastaría. Por eso van uno por uno,
    y por eso esta lista tiene que seguir el paso de las skills en disco: sin
    este assert, una skill nueva se enlaza, el enlace queda ignorado en
    silencio y el clone siguiente se queda sin ella."""
    reincluidas = set(
        re.findall(r"^!\.agents/skills/([A-Za-z0-9_-]+)\s*$", GITIGNORE.read_text(encoding="utf-8"), re.M)
    )
    faltan = _skills_en_disco() - reincluidas
    assert not faltan, (
        f"skills cuyo enlace .gitignore deja fuera del repo: {sorted(faltan)}. "
        "Agrega en .gitignore:\n  " + "\n  ".join(f"!.agents/skills/{n}" for n in sorted(faltan))
    )
    sobran = reincluidas - _skills_en_disco()
    assert not sobran, f".gitignore re-incluye enlaces de skills que ya no existen: {sorted(sobran)}"


def test_gemini_registra_el_hook_de_barandales():
    """Sin la entrada en .gemini/settings.json el script existe pero nadie lo
    llama — el equivalente exacto de una skill sin fila de ruteo."""
    ajustes = json.loads(GEMINI_SETTINGS.read_text(encoding="utf-8"))
    comandos = [
        h.get("command", "")
        for entrada in ajustes.get("hooks", {}).get("BeforeTool", [])
        for h in entrada.get("hooks", [])
        if h.get("type") == "command"
    ]
    assert any(".gemini/hooks/guardrails.py" in c and "$GEMINI_PROJECT_DIR" in c for c in comandos), (
        ".gemini/settings.json no registra .gemini/hooks/guardrails.py como hook BeforeTool "
        "(con $GEMINI_PROJECT_DIR, para que valga desde cualquier cwd)"
    )
    assert GEMINI_HOOK.is_file()


def test_los_barandales_de_gemini_dicen_lo_mismo_que_los_de_claude():
    """Paridad entre las tres copias: el deny de Claude, el hook y el TOML del
    Policy Engine. Una regla que falte en una es un barandal que depende de
    qué agente esté sentado."""
    hook = _hook_gemini()
    for regla in _DENY_OBLIGATORIO:
        assert any(regla in p for p in hook.PREFIJOS_SHELL), f"el hook de Gemini perdió {regla!r}"

    politica = tomllib.loads(GEMINI_POLICY.read_text(encoding="utf-8"))
    reglas = [r for r in politica.get("rule", []) if r.get("decision") == "deny"]
    prefijos_toml = {
        p
        for r in reglas
        if r.get("toolName") == "run_shell_command"
        for p in (r.get("commandPrefix") or [])
    }
    assert prefijos_toml == set(hook.PREFIJOS_SHELL), (
        f"el TOML y el hook no coinciden en shell: {prefijos_toml ^ set(hook.PREFIJOS_SHELL)}"
    )

    def _regex_de(herramienta: str) -> re.Pattern:
        for r in reglas:
            nombres = r.get("toolName")
            nombres = [nombres] if isinstance(nombres, str) else nombres
            if herramienta in nombres and r.get("argsPattern"):
                return re.compile(r["argsPattern"])
        raise AssertionError(f"el TOML no tiene regla deny con argsPattern para {herramienta}")

    lectura, escritura = _regex_de("read_file"), _regex_de("write_file")
    for ruta in hook.LECTURA_PROHIBIDA:
        assert lectura.search(f'{{"file_path":"/repo/{ruta}"}}'), f"el TOML no bloquea leer {ruta}"
    assert not lectura.search('{"file_path":"/repo/.env.example"}'), "el TOML bloquea .env.example"
    for patron in hook.ESCRITURA_PROHIBIDA:
        ejemplo = patron + "x.txt" if patron.endswith("/") else patron
        assert escritura.search(f'{{"content":"","file_path":"/repo/{ejemplo}"}}'), (
            f"el TOML no bloquea escribir {ejemplo}"
        )
    assert not escritura.search('{"content":"","file_path":"/repo/app/main.py"}')

    deny = _deny_de_claude()
    if deny is None:
        pytest.skip(".claude/settings.json todavía no declara `permissions`")
    for regla in deny:
        ruta = _regla_claude_a_ruta(regla)
        if regla.startswith("Bash("):
            assert ruta in hook.PREFIJOS_SHELL, f"Claude niega {regla} y el hook de Gemini no"
        elif regla.startswith("Read("):
            assert ruta in hook.LECTURA_PROHIBIDA, f"Claude niega {regla} y el hook de Gemini no"
        else:
            assert ruta in hook.ESCRITURA_PROHIBIDA, f"Claude niega {regla} y el hook de Gemini no"


@pytest.mark.parametrize(
    ("herramienta", "entrada", "bloquea"),
    [
        ("run_shell_command", {"command": "git push origin main"}, True),
        ("run_shell_command", {"command": "git  push"}, True),
        ("run_shell_command", {"command": "git status && git push"}, True),
        ("run_shell_command", {"command": "./scripts/ship.sh app/x.py"}, True),
        ("run_shell_command", {"command": "ssh vps 'docker compose logs'"}, True),
        ("run_shell_command", {"command": "rm -rf data"}, True),
        ("run_shell_command", {"command": "git status"}, False),
        ("run_shell_command", {"command": "git pushup"}, False),
        ("run_shell_command", {"command": ".venv/bin/pytest -q"}, False),
        ("read_file", {"file_path": "{raiz}/.env"}, True),
        ("read_file", {"absolute_path": "{raiz}/.env.old"}, True),
        ("read_many_files", {"paths": ["{raiz}/app/main.py", "{raiz}/.env"]}, True),
        ("read_file", {"file_path": "{raiz}/.env.example"}, False),
        ("write_file", {"file_path": "{raiz}/data/tramites.db", "content": ""}, True),
        ("replace", {"file_path": "{raiz}/tramites.db", "old_string": "", "new_string": ""}, True),
        ("write_file", {"file_path": "{raiz}/.gemini/settings.json", "content": ""}, True),
        ("replace", {"file_path": "{raiz}/.claude/settings.json", "old_string": "", "new_string": ""}, True),
        ("write_file", {"file_path": "{raiz}/HTTP", "content": ""}, True),
        ("write_file", {"file_path": "{raiz}/app/main.py", "content": ""}, False),
        ("write_file", {"file_path": "{raiz}/.claude/skills/qa-senior/SKILL.md", "content": ""}, False),
        ("glob", {"pattern": "**/*.py"}, False),
    ],
)
def test_el_hook_de_gemini_decide_como_gemini_lo_llama(tmp_path, herramienta, entrada, bloquea):
    """El contrato de verdad: el script como proceso, JSON por stdin, veredicto
    por stdout. Importar `evaluar()` probaría la función; esto prueba lo que
    Gemini ejecuta."""
    raiz = str(tmp_path)
    entrada = json.loads(json.dumps(entrada).replace("{raiz}", raiz))
    evento = {"hook_event_name": "BeforeTool", "cwd": raiz, "tool_name": herramienta, "tool_input": entrada}
    proceso = subprocess.run(
        [sys.executable, str(GEMINI_HOOK)],
        input=json.dumps(evento),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proceso.returncode == 0, proceso.stderr
    if bloquea:
        veredicto = json.loads(proceso.stdout)
        assert veredicto["decision"] == "deny" and veredicto["reason"], proceso.stdout
    else:
        assert proceso.stdout.strip() == "", f"el hook bloqueó algo permitido: {proceso.stdout}"


def test_el_hook_de_gemini_no_revienta_con_basura():
    """Un hook que truena con un JSON raro deja a Gemini sin poder llamar ninguna
    herramienta; ante lo ilegible, deja pasar y calla."""
    for basura in ("", "no es json", "[1, 2]", '{"tool_name": "write_file", "tool_input": "x"}'):
        proceso = subprocess.run(
            [sys.executable, str(GEMINI_HOOK)],
            input=basura,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert proceso.returncode == 0 and proceso.stdout.strip() == "", (
            basura, proceso.stdout, proceso.stderr,
        )
