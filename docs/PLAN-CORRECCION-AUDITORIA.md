# Plan de corrección: informe de auditoría + hallazgos de operabilidad

> **Qué es esto.** La respuesta a `docs/INFORME-AUDITORIA-APP.md` después de verificar cada una de
> sus once afirmaciones contra el código, más los hallazgos de una segunda revisión —de alcance
> distinto— sobre CI, deploy, jobs y contratos de configuración. Ordena qué se hace, en qué orden, y
> **qué se rechaza y por qué**, que es la mitad que un plan suele omitir.

---

## 1. Lo primero: el informe no vio lo más grave

`/docs`, `/redoc` y `/openapi.json` responden **200 sin autenticación**, y el OpenAPI publica las
**98 rutas** del ERP interno —`/agente/config`, `/oficinas`, `/system`, `/clients/upsert`— a
cualquiera en internet. Ningún test lo cubre.

El informe pasó al lado: su §2.4 menciona `/docs` solo para decir que "queda sin estructura
organizada" y propone agregarle `tags`. El problema no es que la documentación esté desordenada; es
que está publicada. Es el único hallazgo de este ejercicio con carácter urgente, y es de una línea.

---

## 2. Las dos listas casi no se tocan

El informe declara su alcance: *"Exclusivo para el directorio `app/`"*. Por eso no ve CI, deploy,
jobs, ni tests — que es donde vive la mayor parte de la segunda lista.

| Área | Informe | Segunda lista | ¿Se cruzan? |
|---|---|---|---|
| Arquitectura de `app/` (monolitos, `Depends`, imports) | 4 hallazgos | — | **No** |
| Rendimiento de `app/` (event loop, N+1, httpx) | 5 hallazgos | — | **No** |
| `app/config.py` | §3.7 (migrar a pydantic-settings) | 3 defectos concretos | **Sí, y se complementan** |
| `app/main.py` | §3.1, §3.2 (arranque, middleware) | 1 (orden del logging) | **Sí, mismo archivo** |
| `app/archive.py` | §2.1 (partirlo) | 1 (`backfill` no reporta avance) | Mismo archivo, problema distinto |
| CI / `pull-deploy.sh` / `requirements.txt` | — | 8 hallazgos | **No** |
| Jobs y cobranza | — | 5 hallazgos | **No** |
| Tests | — | 2 hallazgos | **No** |

**Conclusión:** son complementarios, no redundantes. Donde coinciden —`config.py`— el informe pide
el refactor estructural y la segunda lista trae los defectos concretos que lo justificarían; los
defectos se arreglan solos, sin el refactor.

---

## 3. Qué resistió la verificación

Cada afirmación del informe se leyó contra el código. Las once resultaron **ciertas en el hecho y
exageradas en la consecuencia**: ninguna alcanza impacto medio en este despliegue —un proceso
uvicorn, SQLite local, un puñado de colaboradores internos— y **diez de once** chocan con alguna
regla escrita del repo.

Tres errores factuales que conviene registrar:

1. **§2.1 afirma que `conversation.py` contiene "expresiones regulares de CURP/RFC".** Es falso:
   viven en `app/codigos.py:21-24`. Más aún, `architecture-senior` lista *"Re-defining CURP/RFC/NSS
   patterns in conversation.py"* como error a evitar — el informe describe un estado del que el repo
   ya salió.
2. **§2.1 se contradice consigo mismo:** el resumen ejecutivo pone los monolitos como frente de
   deuda #1; su propia matriz de priorización los clasifica **Baja**.
3. **§3.2 llama "Problema Crítico" a `_nav_context`** sin mencionar que tiene guarda
   (`if request.session.get("user")`): el webhook de WhatsApp, que es el camino con presión real,
   nunca lo ejecuta.

> **Sesgo declarado.** La verificación se pidió con instrucciones que invitaban a detectar
> exageración, y eso pudo inclinar los veredictos. Lo que sostiene las conclusiones es la evidencia
> citada —líneas concretas, contadores reales, mediciones— no el veredicto en sí. Tres datos se
> comprobaron a mano y por separado: `/docs` abierto, la guarda de sesión de `_nav_context`, y que
> hoy hay **una** oficina activa.

---

## 4. El plan

### Fase 0 — Cerrar la puerta ✅ HECHA

| # | Qué | Dónde | Estado |
|---|---|---|---|
| 0.1 | `docs_url=None, redoc_url=None, openapi_url=None` | `app/main.py` | ✅ Las tres responden 404; `/health` y las 98 rutas intactas |
| 0.2 | Dos tests: 404 en las tres, y que ninguna respuesta pública liste rutas internas | `tests/test_security.py` | ✅ Verde, y **3 de 3 mutaciones muerden** |

Detalle que se descubrió al mutar y quedó escrito en el código: **`openapi_url=None` apaga los
tres**, porque Swagger y ReDoc se sirven *desde* el esquema y sin él FastAPI ni siquiera registra
sus rutas. Los otros dos parámetros se dejaron explícitos de todos modos, para que reactivar el
esquema mañana no reabra las dos UIs sin que nadie lo note.

### Fase 1 — Defectos verificados, arreglo trivial ✅ HECHA

Ninguno cambia comportamiento observable. Cada uno con su prueba, y cada prueba
**verificada por mutación** — se revierte el arreglo y se comprueba que la suite se pone roja.

| # | Qué | Dónde | Estado |
|---|---|---|---|
| 1.1 | Borrado el `public_base_url` duplicado; el comentario ahora nombra a sus **dos** consumidores | `app/config.py` | ✅ |
| 1.2 | Borrada la segunda declaración de `PUBLIC_BASE_URL`, dejando escrita la trampa | `.env.example` | ✅ |
| 1.3 | Documentadas las claves faltantes — resultaron **10**, no nueve | `.env.example` | ✅ 0 faltantes, 0 duplicadas |
| 1.4 | Guard de `/static/` antes del query del nav | `app/main.py` | ✅ muerde |
| 1.5 | `is_admin` fuera del bucle de notas | `templates/client.html` | ✅ muerde (14 llamadas → 2) |
| 1.6 | `_buscar` presta su sesión a `credencial()` | `app/ruteo.py` | ✅ muerde (6 oficinas: 6 sesiones → 1) |

**Dos tests nacieron flojos y la mutación los delató.** Vale registrarlo porque el modo de fallo se
repite:

- El de `/static/` pasaba **por vacuidad**: `_nav_context` solo consulta si hay sesión, y el cliente
  de prueba era anónimo. Se rehízo con login real (patrón de `tests/test_oficinas.py`).
- El de `is_admin` montaba el escenario equivocado: las notas eran del propio usuario, y como Jinja
  **cortocircuita el `or`** de `n.author == user or is_admin(user)`, la función no se llamaba nunca.
  Con notas ajenas —el caso real: un admin revisando el trabajo del equipo— el conteo salta a 14.
  De paso quedó claro que el costo no es "una consulta por nota" sino **por nota ajena**.
- Y un tercer detalle que costó encontrar: Jinja copia `env.globals` a la plantilla **al cargarla**,
  así que espiar una función global después no alcanza a una plantilla ya cacheada. El test limpia
  `env.cache` para que el espía entre.

### Fase 2 — Con dueño y disparador (cuando se toque el área)

No se agendan solas; se hacen montadas sobre el próximo trabajo que ya toque ese archivo.

| # | Qué | Disparador |
|---|---|---|
| 2.1 | **Test de autorización por enumeración de rutas**: recorrer `app.routes` y exigir 303 a `/login` en toda ruta fuera de una lista blanca explícita | Es el valor real del §2.2 del informe: cierra el riesgo de "endpoint público por omisión" **sin** el refactor a `Depends`. ~30 líneas, no toca `app/` |
| 2.2 | `errores()` del cron: cubrir también `sin_resolver` | Cuando el barrido de huérfanos distinga excepción de "aún no empareja" (hoy los mezcla y contarlo alarmaría cada noche) |
| 2.3 | `backfill()`: reportar hasta dónde llegó | Al agregar la siguiente tabla al espejo. Un `try` único envuelve las ~70 líneas y devuelve `None` |
| 2.4 | Migrar a `lifespan` conservando el nombre `on_startup()` | Al próximo cambio en el arranque. El nombre es carga documental: `devops-senior` lo cita siete veces |
| 2.5 | `inbox`: pedir cuatro columnas en vez de hidratar entidades ORM | Cuando la bandeja se sienta lenta **con una medición en mano** |
| 2.6 | `intentos_proceso` se escribe y nadie lo lee | Al tocar la cobranza: un contador de reintentos que no acota nada |

### Fase 3 — Rechazado, por escrito

Un "no" sin registrar se reabre en la siguiente auditoría. Estos se anotan como `# ponytail:` con su
condición de reapertura, que es el formato que el repo ya usa.

| Propuesta del informe | Por qué no |
|---|---|
| **Partir `archive.py` y `conversation.py`** (§2.1) | La regla de los CINCO lugares del espejo pasaría a ser cinco lugares en **cinco archivos**: el footgun que `AGENTS.md` llama *"the main footgun"* se vuelve más fácil de disparar. Además `tests/test_mirror_consistency.py:66` parsea el AST de **un** archivo: como paquete, revienta al recolectar — y su `_llamadas()` solo reconoce `ast.Name`, así que una reescritura descuidada quedaría **verde en vacío**. El 64% de `archive.py` es DDL declarativo y mapeo columna-a-columna, no lógica |
| **Refactor completo a `Depends`** (§2.2) | El valor está en la cobertura (2.1 de la Fase 2), no en el refactor. `HTTPException(303, headers={"Location": …})` además no es el mecanismo idiomático de redirección |
| **`prefix` en los routers** (§2.4) | Cambia URLs cableadas en plantillas Jinja y en los webhooks ya configurados en Meta, Twilio y Stripe. El beneficio declarado (ordenar `/docs`) desaparece al cerrar `/docs` |
| **`httpx.Client` singleton** (§3.3) | TCP+TLS medidos ~24 ms contra un presupuesto p90 de ~6 s de Ollama, y `keepalive_expiry=5 s` dejaría el pool frío entre mensajes. `performance-senior` exige medición antes de optimizar; la del informe (50-150 ms) no está tomada aquí |
| **`run_in_threadpool` en `_nav_context`** (§3.2) | Contradice una decisión ya medida y escrita: `app/stripe_webhook.py:119-121` documenta que el salto a threadpool cuesta más que el SELECT que ahorra |
| **Migrar a `pydantic-settings`** (§3.7) | `backend-senior:147` manda que un secreto ausente deje la función inerte y **nunca levante al importar**; el snippet del informe hace lo contrario. Además `tests/test_credenciales_inventario.py` lee `config.py` como texto y se rompería |
| **`ROW_NUMBER()` en `/inbox`** (§3.4) | Hay un `# ponytail:` vigente en `dashboard.py:414` sobre paginación en memoria. La versión mínima (Fase 2.5) captura el valor sin reescribir la consulta |
| **Jerarquía estricta de capas** (§2.3) | Los imports diferidos que se revisaron no son parches de ciclo: varios llevan su propio `ponytail` con el techo escrito |

---

## 5. Cómo se verifica cada fase

Lo mismo que el resto del repo: `pytest -q` verde (1637 hoy), y para cada cambio una prueba que
**muerda** — se rompe a propósito el arreglo y se comprueba que la suite se pone roja. Un test que
solo sabe pasar no prueba nada.

Las Fases 0 y 1 no cambian comportamiento observable salvo por diseño (0.1 cierra tres rutas). La
Fase 2 sí, y por eso va montada sobre trabajo que ya toque el área, nunca suelta.
