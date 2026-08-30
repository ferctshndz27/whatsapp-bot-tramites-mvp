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
sería silenciosa. Cinco asserts: el `name:` del frontmatter coincide con su carpeta; la tabla y el
disco se corresponden **en ambas direcciones**; toda ruta citada existe; todo ancla `archivo.py:132`
cae dentro del archivo; y la política de permisos conserva sus barandales.

Dos decisiones deliberadas dentro de ese test:

- **El ancla se valida en rango, nunca semánticamente.** El test vive en `tests/`, así que CI lo
  corre y un rojo frena el `promote` y por lo tanto el deploy del bot. Un archivo borrado debe
  frenar; un `:132` que se corrió a `:140` no. Las skills mismas advierten que los anclas migran.
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

## No-objetivos

- **No se replica la estructura de byo.** No hay `internal/provider/` ni `CompactionStrategy` porque
  este harness no necesita ninguno de los dos: hay dos proveedores y cero historial que compactar.
- **No se reviven los agentes de `.claude/agents-archivados/`.** Son 25+ roles de otra forma de
  proyecto —casi todos de diseño— irrelevantes para un bot de trámites.
- **El harness no valida la semántica de una skill.** Que una skill diga la verdad sobre el código lo
  garantiza quien la escribe; el test solo garantiza que los archivos que cita existan.
- **Esto no reemplaza a `AGENTS.md`.** Ese archivo son las reglas; este es el porqué.

## Cómo se mantiene vivo

Agregar una skill son **tres lugares**: el archivo, su fila en la tabla de ruteo, y
`tests/test_harness.py` en verde **en el mismo commit**. Es la misma forma de la regla de los cinco
lugares del espejo MSSQL, y por la misma razón: un contrato que solo vive en prosa se rompe en
silencio, y el silencio es la peor manera de fallar.
