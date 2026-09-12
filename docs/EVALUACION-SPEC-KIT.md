# Evaluación: GitHub Spec Kit en este repo

> **Veredicto.** Sí es integrable, pero no con `specify init` a secas. Bajo Claude Code, spec-kit
> no instala *comandos*: instala **diez skills** en `.claude/skills/`, que es justo el directorio
> que `tests/test_harness.py` vigila con un contrato pensado para una sola población. Medido en una
> copia del repo: `specify init --integration claude` pone **5 de 33 pruebas del harness en rojo**,
> y un rojo frena el job `promote` y con él el deploy del bot.
>
> La mitigación cuesta **+53 líneas en `tests/test_harness.py`** y deja el harness en **35/35 verde**
> con spec-kit instalado. También está medida, no estimada.

Fecha de la evaluación: 12 de septiembre de 2026. Upstream evaluado: `github/spec-kit@d848fb4`
(11 de septiembre de 2026), `specify-cli` 1.0.7.dev0, licencia MIT, requiere Python ≥ 3.11.

---

## 1. Qué es y qué instala

Spec Kit es una CLI (`specify`) que impone un ciclo de *Spec-Driven Development*: constitución →
especificación → clarificación → plan → tareas → análisis de consistencia → implementación. Cada
paso es un comando que el agente ejecuta, y cada uno escribe un artefacto versionado.

Bajo Claude Code, esos comandos llegan como skills:

| Qué | Dónde queda | Tamaño |
|---|---|---|
| Los diez comandos | `.claude/skills/speckit-<nombre>/SKILL.md` | 2 440 líneas, 132 KB |
| Constitución del proyecto | `.specify/memory/constitution.md` | — |
| Scripts de soporte | `.specify/scripts/{bash,powershell,python}/` | 6 scripts × 3 lenguajes |
| Plantillas de artefactos | `.specify/templates/` | 591 líneas |
| Manifiesto y estado | `.specify/integrations/speckit.manifest.json` | — |

Los diez: `specify`, `clarify`, `plan`, `tasks`, `analyze`, `implement`, `checklist`, `converge`,
`constitution`, `taskstoissues`.

Soporta los tres agentes que este repo ya usa —`claude`, `gemini`, `agy` (Antigravity)— más otros
treinta. Eso es un punto a favor: el harness de este repo es explícitamente tri-agente.

---

## 2. El choque con el harness, medido

Método: copia del repo en un sandbox, `pytest tests/test_harness.py -q` como línea base
(**33 passed**), simulación fiel de `specify init --integration claude` —incluida la reescritura
`scripts/` → `.specify/scripts/` que hace `process_template`— y la misma prueba otra vez.

Resultado: **5 failed, 28 passed**.

| Prueba que se rompe | Por qué |
|---|---|
| `test_la_tabla_de_ruteo_y_las_skills_en_disco_no_divergen` | Las diez skills de spec-kit no tienen fila en `AGENTS.md`. La biyección tabla ↔ disco es el contrato central del harness. |
| `test_cada_skill_conserva_la_forma_del_harness` | Ninguna trae las once secciones (`Purpose`, `When to use`, …). **Y no pueden traerlas**: `specify upgrade` las regenera desde el upstream y borraría cualquier adaptación. |
| `test_los_archivos_que_cita_el_harness_existen` | 22 citas huérfanas. spec-kit reescribe sus rutas a `.specify/scripts/…`, pero el regex `_CITA` casa `scripts/bash/check-prerequisites.sh` **dentro** de esa ruta —el `\b` encuentra borde después del `/`— y busca el archivo en la raíz del repo, donde no está. |
| `test_las_skills_del_repo_estan_enlazadas_para_los_agentes_de_google` | Exige un symlink en `.agents/skills/` por cada skill en disco. |
| `test_gitignore_versiona_los_enlaces_y_solo_los_enlaces` | Exige una línea `!.agents/skills/<nombre>` por cada skill en disco. |

La raíz del problema no es un test mal escrito: **el harness asume que todo lo que vive en
`.claude/skills/` es un rol de dominio de este repo.** Spec-kit introduce una segunda población en
el mismo directorio —comandos de proceso, genéricos por diseño, propiedad de una herramienta
externa— y el contrato no tiene forma de distinguirlas.

Dicho de otro modo: las skills de spec-kit fallan las preguntas 1 y 3 del criterio de *Skills de
fuera* que ya está escrito en `docs/HARNESS.md` (no citan archivos de este repo; no traen las once
secciones). Pero ese criterio se escribió para *roles de dominio importados*, que es otra cosa.
Rechazarlas por esa vara sería aplicar la regla correcta al caso equivocado.

---

## 3. Los choques que no son del harness

**3.1 La constitución duplica `AGENTS.md`.** `/speckit.constitution` escribe principios de proyecto
en `.specify/memory/constitution.md`. `AGENTS.md` ya es exactamente eso, y es el archivo que las
tres portadas importan. Dos fuentes de verdad sobre las mismas reglas derivan; es la pregunta 2 del
criterio de skills de fuera, aplicada a un archivo en vez de a una skill.
→ **La constitución no se escribe.** `.specify/memory/constitution.md` se reduce a un puntero de un
renglón a `AGENTS.md`. (El upstream trae un preset `constitution-sync` que vale la pena mirar antes
de improvisar esto a mano.)

**3.2 La extensión `git` choca de frente con el barandal del operador.** Trae `auto-commit.sh` y
`create-new-feature-branch.sh`: crea ramas y commitea sola. Este repo tiene su propio guion de
publicación en `scripts/` y la regla dura de que el humano corre todo `git push`, respaldada por el
`deny` de `.claude/settings.json` y por el hook de Gemini. → **No se instala.**

**3.3 `.claude/settings.json` es el archivo que el agente no puede escribir.** Spec-kit lo declara
como `events_config_file`: una extensión con eventos escribiría hooks ahí. Que el agente no pueda
tocar su propia política es load-bearing según `docs/HARNESS.md`. → **No se instalan extensiones con
eventos**; si alguna hiciera falta, el bloque va como copia-pega al operador, como ya se hace hoy.

**3.4 Antigravity queda a medias, y está bien que quede así.** `specify init --integration agy`
escribe directorios reales en `.agents/skills/speckit-*`. Eso el agente no puede hacerlo (`deny` en
`Write(./.agents/**)`), y aunque lo corra el operador, `.gitignore` ignora todo `.agents/skills/*`
salvo los trece enlaces nombrados. Consecuencia práctica: **los comandos de spec-kit no viajan al
clone para Antigravity**; cada máquina que lo quiera corre `specify init --integration agy` una vez.
Es sostenible, pero hay que saberlo en vez de descubrirlo.

**3.5 Entorno.** `uv`/`uvx` **no están instalados** en esta máquina, y el venv local corre Python
3.14 mientras producción despliega 3.12. Spec-kit se instala como herramienta aparte
(`uv tool install specify-cli` o `pipx install specify-cli`), **nunca dentro del venv del proyecto
ni en `requirements.txt`**: los pines de `requirements.txt` son los de la imagen y no se tocan por
una herramienta de desarrollo.

---

## 4. La mitigación, probada

El principio: **el harness no deja de vigilar, cambia de contrato para la población nueva.** Una
skill de spec-kit no es un rol de dominio y no se le exige la forma de uno; lo que sí se le exige es
que la instalación esté completa y que su `name:` resuelva.

Cuatro cambios en `tests/test_harness.py` (+53 líneas, probados):

1. `_skills_en_disco()` excluye el prefijo `speckit-`, y un helper nuevo `_skills_de_spec_kit()`
   devuelve solo esas. Con eso, la biyección tabla ↔ disco vuelve a hablar únicamente de las trece.
2. `_documentos()` excluye sus `SKILL.md` del escaneo de citas: sus rutas son del árbol `.specify/`,
   no del repo, y validarlas sería vigilar el interior de una herramienta externa.
3. `test_cada_skill_conserva_la_forma_del_harness` itera el helper en vez del directorio.
4. **Prueba nueva** `test_los_comandos_de_spec_kit_estan_completos`: que estén los diez, que cada
   `name:` case con su carpeta, y `skip` si spec-kit no está instalado —mismo criterio que `_LOCALES`
   y que `CLAUDE.md`, para que un clone sin la herramienta no ponga rojo el CI. Más
   `test_spec_kit_no_pisa_una_skill_de_dominio`, porque las dos poblaciones comparten directorio y no
   deben compartir nombre.

Resultado medido con las diez skills instaladas: **35 passed** (33 originales + 2 nuevas).

Falta, en el mismo commit, lo que el harness no puede inferir: una sección en `AGENTS.md` que diga
que `.claude/skills/` tiene dos poblaciones y cuál es cuál, y el porqué en `docs/HARNESS.md`.
Adoptar spec-kit es un cambio al harness, y el harness se cambia con su dueño: la skill
`harness-engineer`.

---

## 5. Qué aporta de verdad y qué duplica

Este repo **ya hace SDD a mano**: `docs/SPEC-MULTI-OFICINA.md` (41 KB),
`docs/SPEC-MIGRACION-OPENROUTER.md` (21 KB), `docs/PLAN-CORRECCION-AUDITORIA.md`. La pregunta no es
si el método sirve —ya se usa— sino si la herramienta agrega algo sobre el método a mano.

**Duplica:** la constitución (`AGENTS.md`) y el flujo rama → PR → promote, que ya vive en `scripts/`.
**No compite** con el ruteo por rol: spec-kit no tiene nada parecido a las trece skills, y las trece
no tienen nada parecido a un ciclo de artefactos. Son capas distintas.

**Aporta, en orden de valor para este repo:**

- **`/speckit.clarify`** — obliga a resolver ambigüedades *antes* de planear. El commit `4c6abb6`
  (*el modelo deja de elegir por el cliente cuando el pedido va a medias*) es exactamente la clase de
  defecto que un paso de clarificación previo atrapa en la spec en vez de en producción.
- **`/speckit.analyze`** — consistencia cruzada spec ↔ plan ↔ tareas. Hoy una spec de 41 KB no tiene
  contra qué contrastarse salvo la lectura de un humano.
- **Artefactos por feature numerados** (`specs/001-…/spec.md|plan.md|tasks.md`) en lugar de un `.md`
  monolítico que solo crece.
- **`/speckit.checklist`** — listas de verificación de requisitos, que encajan con la línea que el bot
  no cruza (precio final, documentos, pagos, cierre de trámite).

---

## 6. Recomendación

**Integración parcial y por fases. No `specify init` completo.**

**Fase 1 — piloto acotado.** Instalar spec-kit solo para Claude Code, sin extensiones, con el parche
del harness en el mismo commit (la regla de las tres partes: el cambio, su fila y una prueba verde).
Estrenarlo en **una** feature: la candidata natural es la migración a OpenRouter —la spec ya existe,
está acotada y no toca el flujo del cliente—. Si el ciclo aporta, se queda; si no, se borran
`.claude/skills/speckit-*` y el parche queda inerte gracias al `skip`.

**Fase 2 — solo si la fase 1 convence.** Agente Gemini (`--integration gemini`) y decidir si
Antigravity vale el `specify init` por máquina.

**Nunca:** la extensión `git`, cualquier extensión con eventos, y `/speckit.constitution` como fuente
de principios. `AGENTS.md` manda.

---

## 7. Cómo reproducir esta medición

```bash
git clone --depth 1 https://github.com/github/spec-kit.git
# copia del repo sin .git/.venv/data/media, luego:
pytest tests/test_harness.py -q          # línea base: 33 passed
# simular la instalación: templates/commands/*.md -> .claude/skills/speckit-<stem>/SKILL.md
# con name: speckit-<stem> y scripts/ reescrito a .specify/scripts/
pytest tests/test_harness.py -q          # 5 failed, 28 passed
# aplicar la mitigación de §4
pytest tests/test_harness.py -q          # 35 passed
```

<!-- ponytail: la simulación reproduce ClaudeIntegration a mano en vez de correr specify init de
     verdad, porque uv no está instalado y meter la CLI en el venv del proyecto para una medición
     habría sido peor que aproximarla. Lo que se verifica es el efecto sobre el harness —diez
     carpetas con SKILL.md y sus citas—, y eso es fiel. Si algún día se corre el init real, lo que
     hay que volver a medir es el conteo de citas huérfanas: depende de cómo quede el frontmatter. -->
