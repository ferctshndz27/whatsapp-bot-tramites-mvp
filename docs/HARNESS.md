# El harness de este repo

> *"El modelo es el motor. El harness es todo lo demás."*
> — [`betta-tech/byo-coding-agent`](https://github.com/betta-tech/byo-coding-agent)

Ese repo llama **harness engineering** a "la disciplina de construir el andamio alrededor de un LLM
que lo convierte en un agente útil", y sostiene que con un buen harness un modelo mediano rinde bien,
mientras que con uno malo hasta un modelo de frontera "se siente roto". Divide el trabajo en tres
niveles: construir el loop, extenderlo, y **configurarlo con archivos** como `AGENTS.md`.

Este documento usa esa taxonomía como vara de medir. No para copiar su estructura —es un repo
pedagógico en Go de ~1,000 líneas y esto es un bot en producción— sino porque nombra las capas y
obliga a responder, capa por capa, *qué hace este repo y por qué*.

## Aquí hay dos harness, no uno

| | Qué es | Dónde vive |
|---|---|---|
| **Harness del producto** | El andamio alrededor del modelo que atiende clientes | `app/nlu.py`, `app/rag.py`, `app/agent_tools.py` |
| **Harness de desarrollo** | El andamio alrededor del modelo que escribe este repo | `AGENTS.md`, `.claude/skills/`, `.claude/settings.json` |

Se evalúan por separado porque fallan distinto: el del producto le cotiza mal un trámite a un
cliente real; el de desarrollo manda a un agente a leer un archivo que ya no existe, con tono de
autoridad y sin que nada truene.

## Harness del producto, capa por capa

| Capa de byo | Qué hay aquí | Veredicto |
|---|---|---|
| Agent loop | `_loop_ollama` / `_loop_anthropic`, acotados por `settings.agent_max_turnos` | ✅ |
| Tools | registro `_TOOLS` + `tools_expuestos()`, esquema validado en código | ✅ |
| Providers | dos loops casi gemelos | ⚠️ fuga |
| Compaction | el loop arranca `[system, user]` y no carga historial | ✅ no aplica |
| Permisos | `PERMISOS` con piso `deny` + interruptor operativo encima | ✅✅ mejor que byo |
| "Don't crash the loop" | `_safe()`, ContextVar de fallo, degradación silenciosa | ✅✅ mejor que byo |
| Evaluación | `scripts/eval_nlu.py`, `tests/test_nlu_regression.py` con piso de exactitud | ✅✅ byo no tiene |

### Compaction: capa que no se necesita, no capa faltante

byo dedica un paquete entero a moldear el historial, con `SlidingWindow`, `Summarize` y un
`SafeSplitPoint` que evita separar un `tool_use` de su `tool_result` y provocar un 400. Aquí ese
paquete no existe, y está bien: el nodo agéntico arranca cada mensaje con `[system, user]` y **no
arrastra historial**. El estado conversacional vive en la máquina de estados y en la base, no en el
contexto del modelo.

Es la diferencia entre un hueco y una decisión. La versión perezosa —no cargar lo que no se
necesita— resulta ser también la robusta: no hay ventana que desbordar ni resumen que alucine.

### Permisos y degradación: aquí byo se queda corto

En byo la política de permisos es el **ejercicio 3**, una tarea para el lector; el agente terminado
trae un `confirm` hardcodeado. Aquí `PERMISOS` es un piso de seguridad revisado en código: `"cobrar"`
está en `deny` por construcción, solo lo `allow` se expone al modelo, y el interruptor operativo va
*encima* — apagar una herramienta permitida es reversible desde `/agente`, encender una denegada es
imposible por cualquier ruta. `tests/test_agent_tools.py` ancla esa línea con un assert.

Lo mismo con la máxima de byo *"Don't crash the loop"*: allá es una convención de estilo (una
herramienta que falla devuelve texto y una bandera, no un error). Aquí es infraestructura —`_safe()`
en `app/archive.py` hace que el espejo MSSQL nunca reviente el request, y un fallo del clasificador
viaja en un ContextVar en vez de una global, porque los endpoints síncronos corren en un threadpool
y una global le atribuiría la caída de un cliente a otro.

### Evaluación: el hueco confesado de byo

El README de byo dice, textual, *"Nothing's automated yet"*: no tiene tests ni evals, y proponer un
`MockProvider` es el ejercicio 5. Este repo llegó antes: `scripts/eval_nlu.py` corre la cascada
completa contra el Ollama real y reporta exactitud por capa y p50/p90, y
`tests/test_nlu_regression.py` corre contra el catálogo semilla de producción con un piso de
exactitud que un cambio no puede bajar sin que CI lo note.

### El único hallazgo de código: fuga de proveedor

byo confina el formato de cable de cada SDK a un adaptador (`Send(ctx, messages, tools)`) y deja
**un** loop; cambiar de proveedor es una línea. Aquí hay dos loops, y el comentario del propio código
lo admite: *"Mismos permisos, mismas herramientas y misma validación que el bucle de Claude — solo
cambia el protocolo del mensaje"*.

Funciona, y hoy solo uno está activo. El costo es real de todos modos: un guardrail que se arregle
en un loop y no en el otro queda a medias, y nada lo detecta. **No se refactoriza en el PR que
introdujo este documento** — es un cambio que debe preservar comportamiento y merece su propio PR con
pruebas que cubran ambos caminos. Queda anotado aquí para que la decisión sea deliberada y no olvido.

Segundo pendiente conocido, ya documentado en la skill de IA: los ~13 casos del corpus que terminan
en `"ninguna"` gastan de todos modos ~4.2 s preguntándole a Ollama. Desperdicio medido y aceptado.

## Harness de desarrollo: de prosa a contrato

Aquí estaba el hueco real. `AGENTS.md` rutea cada dominio a una skill, y las skills citan archivos
concretos — pero todo eso era **texto**. Se podía renombrar una skill, borrarla, o dejar citado un
módulo que ya no existía, y nada se quejaba: el agente simplemente leía humo.

Lo que se agregó:

**`tests/test_harness.py`** — el mismo truco que `tests/test_mirror_consistency.py` le aplica a la
regla de los cinco lugares del espejo: análisis estático que vuelve ruidosa una divergencia que si no
sería silenciosa. Los asserts originales: el `name:` del frontmatter coincide con su carpeta; la
tabla y el disco se corresponden **en ambas direcciones**; cada skill conserva su `description:` y
las once secciones comunes en orden; toda ruta citada existe; todo ancla `archivo.py:132` cae dentro
del archivo; y la política de permisos conserva sus barandales. Los que se sumaron con Gemini están
más abajo, en *El mismo harness desde otro agente*.

Dos decisiones deliberadas dentro de ese test:

- **El ancla se valida en rango, nunca semánticamente.** El test vive en `tests/`, así que CI lo
  corre y un rojo frena el `promote` y por lo tanto el deploy del bot. Un archivo borrado debe
  frenar; un `:132` que se corrió a `:140` no. Las skills mismas advierten que los anclas migran.
- **La forma de skill se exige, las secciones extra no se prohíben.** Las trece skills ya compartían
  las mismas once secciones; lo que faltaba era el candado. `When to use` / `When NOT to use` son lo
  que evita que una tarea de dos líneas cargue cuatro roles, y `description:` es lo que el modelo lee
  para elegir la skill: una skill sin él se puede rutear a mano pero nunca se elige sola. El assert
  pide que esas once estén y se lean en orden, no que sean las únicas — una skill que necesite una
  sección propia la agrega sin pelear con CI.

- **`_LOCALES` existe por un caso real.** `scripts/ship.sh` es la herramienta local del operador,
  fuera del repo: una skill que lo cita pasa en la máquina de quien escribe y **truena en CI**. La
  exclusión es corta, nombrada y explicada, no un cajón para esconder citas rotas.

**`.claude/settings.json`** — la política de permisos, versionada (el ejercicio 3 de byo, hecho). El
`deny` carga la regla del operador: nada de `git push`, `ssh`, `scp`, `docker` ni `scripts/ship.sh`,
y nada de escribir en `data/`, `tramites.db` o `.env`.

Con una precisión que importa: **el `deny` ata la herramienta Bash del agente, no la terminal del
operador**. `scripts/ship.sh` sí corre `git push -u origin` y `gh pr create`, pero lo corre el humano
en su propia shell, donde estas reglas no existen. Lo que la regla bloquea es que el agente empuje
por su cuenta — la regla que antes solo vivía en la memoria del asistente.

**Dos roles nuevos como skill**: `harness-engineer` (dueño de la tabla, la política y este test, que
antes no tenía dueño) y `product-owner`, migrado desde `.claude/agents/ai-product-manager.md`, que se
borró en el mismo commit: dos copias del mismo rol violan la regla de fuente única.

### Un hallazgo que no se buscaba

El agente **no puede escribir su propia política de permisos**: la plataforma lo rechaza, tanto por
Bash como por la herramienta de escritura. Es correcto y es load-bearing — una política que el agente
puede reescribir no es una política. Por eso el assert de permisos hace `skip` mientras el bloque no
exista y se vuelve estricto en cuanto el operador lo pega, en vez de dejar la suite roja por algo que
el agente tiene prohibido arreglar.

byo no tiene este problema porque su `confirm` está hardcodeado en el binario. Es una diferencia a
favor del harness alojado: la compuerta la sostiene alguien que el agente no puede convencer.

## El mismo harness desde otro agente (Antigravity y Gemini CLI)

El operador trabaja este repo desde un canvas de nodeterm con nodos de Claude, Codex y Gemini. Un
harness que solo funciona bajo Claude Code no es un harness del repo: es una preferencia de
herramienta.

**La primera versión de esta sección apuntó a la herramienta equivocada** y queda escrito porque es
la lección más útil del ejercicio: se implementó todo contra `gemini-cli`, que el operador **no
tiene instalado**. Lo que usa es **Antigravity (`agy`)**, y las dos herramientas no leen los mismos
directorios. El error se descubrió tarde, sondeando el binario que sí está en la máquina.

Todo lo de abajo se verificó **contra los binarios**, no contra la documentación: el bundle de
Gemini CLI 0.58.0 publicado en npm —leído y ejecutado— y las cadenas del ejecutable de Antigravity
1.1.27. Donde el código y la doc no coinciden, gana el código.

| | Claude Code | Antigravity (`agy` 1.1.27) | Gemini CLI 0.58.0 |
|---|---|---|---|
| Lee `AGENTS.md` sin configurar | por `CLAUDE.md` | **sí, nativamente** | no, solo `GEMINI.md` |
| Escanea `.agents/skills/` | — | **sí** | sí |
| Escanea `.gemini/skills/` | — | **no** | sí |
| Dónde vive su política | `.claude/settings.json` (en el repo) | `permissionGrants` en `~/.gemini/config/projects/<uuid>.json` (**fuera del repo**) | `.gemini/settings.json` (en el repo) |

De ahí las tres decisiones:

**Los enlaces viven en `.agents/skills/`, no en `.gemini/skills/`.** Es el único directorio que las
dos herramientas de Google escanean, así que un solo juego de enlaces sirve para ambas. El costo es
que ese directorio es la **raíz de personalización del operador**: sus propias skills viven ahí
(`tidyfactor-styler`) y estaba excluido entero. Por eso `.gitignore` versiona los trece enlaces
**uno por uno y por nombre**, y nada más: re-incluir `.agents/skills/*` entero metería al repo la
siguiente skill que el operador guarde ahí, y un `git add .` bastaría para hacerlo sin que nadie lo
note. El precio de listarlos es un lugar más que tocar al agregar una skill, y por eso el test
compara esa lista con las skills en disco y falla nombrando la línea que falta.

**Las skills se enlazan, no se copian.** `loadSkillsFromDir` busca `SKILL.md` y `*/SKILL.md` con
`fs.stat`, que sigue symlinks: `gemini skills list` dentro del repo lista las trece más la del
operador. Un enlace *relativo* sobrevive a cualquier clone; uno absoluto solo funciona en la máquina
donde se creó, y el test lo rechaza. El precio es Windows sin `core.symlinks`, y se paga a sabiendas:
el operador trabaja en Linux y el repo se despliega en un contenedor Linux.

**`GEMINI.md` existe solo por Gemini CLI.** Antigravity ya lee `AGENTS.md` nativamente. Gemini CLI
busca `GEMINI.md` por defecto (`DEFAULT_CONTEXT_FILENAME` en el bundle) y **no lee `AGENTS.md` solo**.
Se podría apuntar `context.fileName` a `AGENTS.md`, pero eso vive en un settings que cada quien
configura y que el merge de workspace *reemplaza* en vez de concatenar. Un `GEMINI.md` de un renglón
con `@AGENTS.md` no depende de configuración alguna y deja el mismo patrón que ya usa `CLAUDE.md`:
una portada por agente, un solo contenido. Su procesador de imports resuelve `@ruta` relativa al
archivo, valida que no se salga del proyecto y marca en el propio texto lo que no pudo importar.

### Los barandales no viajan igual, y bajo Antigravity no viajan

Bajo Claude Code los sostiene la plataforma; bajo Gemini CLI, el hook de abajo. **Bajo Antigravity
no hay nada en el repo que los imponga**: sus permisos son `permissionGrants` guardados en el home
del operador (`~/.gemini/config/projects/<uuid>.json`, ya con este repo registrado), en formato
`command(...)`, y ese archivo no se puede versionar aquí. La decisión tomada fue **no intentar
automatizarlo y decirlo en voz alta**: `AGENTS.md` advierte que cuando el agente es Antigravity, las
reglas escritas son el único barandal. Es una asimetría real y conviene que esté a la vista en vez de
suponer que el candado existe en las tres herramientas.

**Por qué los barandales son un hook y no un `exclude`.** Se probaron las tres vías:

- `tools.exclude` **no sirve para esto**, y no por gusto: la propia CLI avisa al arrancar que está
  deprecado, y —lo que decide— una entrada como `run_shell_command(git push)` se convierte en una
  regla cuyo `toolName` se compara por **igualdad exacta** contra `run_shell_command`. Nunca casa.
  Habría quedado un `deny` que se lee bien en el JSON y no bloquea nada: la peor forma de fallar,
  que es la que este documento existe para evitar.
- `.gemini/policies/*.toml` es el sucesor oficial y el formato correcto (`commandPrefix`,
  `argsPattern`, `priority`), pero las políticas de *workspace* vienen **apagadas por código** en
  0.58.0 (`disableWorkspacePolicies = true`). El archivo está escrito y el test lo mantiene en
  sincronía con el hook, listo para el día que se enciendan. Mientras tanto el operador puede
  activarlo él: `"policyPaths": ["~/.gemini/policies", "<repo>/.gemini/policies"]` en su
  `~/.gemini/settings.json` —la lista reemplaza al directorio de usuario, por eso va también— o
  `gemini --policy .gemini/policies` por sesión.
- El **hook `BeforeTool`** sí corre: los hooks de proyecto se concatenan con los del usuario, reciben
  el evento por stdin (`tool_name`, `tool_input`, `cwd`) y un `{"decision": "deny"}` en stdout
  bloquea la llamada. Es la única de las tres que hoy detiene un `git push`.

`.gemini/hooks/guardrails.py` es solo biblioteca estándar —corre con el `python3` del sistema, fuera
del venv— y ante un JSON ilegible **deja pasar y calla**: un hook que truena deja a Gemini sin poder
llamar ninguna herramienta, y eso es peor que el riesgo que cubre. Revisa cada tramo de un comando
encadenado por separado, para que `git status && git push` no se cuele por el `&&`.

**El candado, aquí, es el test.** Bajo Claude Code la plataforma impide que el agente reescriba su
propia política — el hallazgo que ya cuenta este documento. Ninguno de los dos agentes de Google
tiene esa compuerta. Por eso `tests/test_harness.py` creció de seis asserts a treinta y tres, y ahí
está el peso: exige **paridad entre las tres copias** de la lista (el `deny` de Claude, el hook y el
TOML), que las skills sean enlaces —no copias, no absolutos—, que `.gitignore` versione esos enlaces
y solo esos, que `.gemini/settings.json` registre el hook, y **ejecuta el hook como proceso** contra
veintiún llamadas reales, permitidas y prohibidas. La regla del repo se mantiene: una regla de
seguridad que se puede borrar sin que nada se queje no es una regla, es una sugerencia.

Tres notas para quien venga:

- **Carpeta no confiable, harness apagado.** Gemini CLI ignora `GEMINI.md`, los hooks y las skills de
  proyecto en un workspace sin confianza, y lo avisa en una línea fácil de pasar por alto. Su comando
  `/permissions` **solo existe si folder trust está activo** (`config.getFolderTrust() ? … : []`), así
  que "usa /permissions" no siempre es un consejo aplicable.
- **`CLAUDE.md` no está versionado** (`.gitignore`), `GEMINI.md` sí. Por eso el test exige el segundo
  siempre y el primero solo si está presente: leerlo sin más pondría rojo el CI —y con él el deploy—
  por un archivo que el repo decidió no guardar. Es el mismo error que `_LOCALES` existe para evitar,
  y se cometió al escribir esta sección antes de que el test lo atrapara.
- **La skill del operador convive con las nuestras.** `.agents/skills/` mezcla los trece enlaces del
  repo con las skills propias del operador. El test solo mira los symlinks; lo demás no es asunto de
  este repo y `deny` sigue prohibiendo escribir ahí.

## Skills de fuera: cómo se evalúan

Van a seguir apareciendo colecciones de skills instalables (`npx skills add …`, catálogos estilo
`agentskills.io`). La pregunta no es si el paquete es popular, sino si lo que trae **sabe algo de
este sistema**. El criterio ya está escrito en la skill `harness-engineer` y aquí solo se le pone
nombre: *si un `SKILL.md` se puede soltar en otro repo sin cambiarle una coma, es documentación
sobre un tema, no una skill sobre este sistema*. El modelo ya sabe FastAPI; lo que no sabe es que el
catálogo vive en SQLite y que el JSON es solo semilla.

Cuatro preguntas, en orden, y la primera que falle cierra el caso:

1. **¿Cita archivos de este repo?** Sin anclas, no hay skill.
2. **¿Choca con una de las trece?** `backend-senior` ya es FastAPI + SQLModel, `qa-senior` ya es
   pytest y fixtures, `architecture-senior` ya es los límites entre capas. Una segunda copia del
   mismo rol es una fuente de verdad que va a derivar.
3. **¿Trae las once secciones en orden?** Si no, `tests/test_harness.py` se pone rojo, y un rojo
   frena el `promote` y con él el deploy del bot. Adaptarla no es cosmética: es reescribirla.
4. **¿Se puede auditar y licenciar?** Se lee el contenido completo antes de copiarlo, no el README.

**Caso resuelto (septiembre 2026): `full-stack-skills/python-skills`, rechazado.** Su README anuncia
tres skills (django, fastapi, flask); el repo trae diecinueve. La de `fastapi` está casi entera en
chino y su sección de "límites" dice que aplica «cuando necesites usar el stack correspondiente a
esta skill». Cuatro de las ocho revisadas traen el **frontmatter duplicado**, con un párrafo de
relleno en medio. Los `examples/` de las diecinueve son el mismo `print("Hello, World!")`, byte por
byte. Hay un `SKILL.md.backup` versionado. Falla las cuatro preguntas; la revisión completa costó
menos que escribir este párrafo, y ese es justamente el punto de dejarlo escrito.

## No-objetivos

- **No se replica la estructura de byo.** No hay `internal/provider/` ni `CompactionStrategy` porque
  este harness no necesita ninguno de los dos: hay dos proveedores y cero historial que compactar.
- **No se reviven los agentes de `.claude/agents-archivados/`.** Son 25+ roles de otra forma de
  proyecto —casi todos de diseño— irrelevantes para un bot de trámites.
- **El harness no valida la semántica de una skill.** Que una skill diga la verdad sobre el código lo
  garantiza quien la escribe; el test solo garantiza que los archivos que cita existan.
- **Esto no reemplaza a `AGENTS.md`.** Ese archivo son las reglas; este es el porqué.

## Cómo se mantiene vivo

Agregar una skill son **cinco lugares**: el archivo, su fila en la tabla de ruteo, el symlink, su
línea en `.gitignore`, y `tests/test_harness.py` en verde **en el mismo commit**:

```bash
ln -s ../../.claude/skills/<nombre> .agents/skills/<nombre>   # relativo, nunca absoluto
echo '!.agents/skills/<nombre>' >> .gitignore                 # o el enlace no entra al repo
```

Es la misma forma de la regla de los cinco lugares del espejo MSSQL, y por la misma razón: un
contrato que solo vive en prosa se rompe en silencio, y el silencio es la peor manera de fallar.

Y el test se comprueba en negativo, no solo en verde: romper un enlace, copiar una skill en vez de
enlazarla, quitarle `git push` al hook, desincronizar el TOML o dejar el hook sin registrar tienen
que poner la suite en rojo. Un test de harness que solo sabe pasar no prueba nada.
