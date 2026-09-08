# SPEC — Multi-oficina (Fase 7)

> Grano verificado contra el código real del repo: `app/config.py:17-20,122-130,143-147` (el inventario
> de secretos), `app/webhook.py:22-31` (handshake GET contra `settings.verify_token`), `:33-38` (firma
> HMAC sobre el cuerpo CRUDO **antes** de `request.json()`), `:62-67` (`compare_digest`, fail-closed),
> `:215-233` (firma de Twilio sobre la URL reconstruida desde `PUBLIC_BASE_URL`), `app/stripe_webhook.py:39-64`
> (verificación de firma de Stripe con tolerancia de 300 s), `app/auth.py:7-12` (`is_admin` lee la fila
> del `User`), `app/db.py:31-90` (los dicts `_*_COLUMNS_ADDED_AFTER_LAUNCH`), `:127-134` (orden de
> `init_db`: `create_all` y luego los `ALTER TABLE`), `:226-232` (`_add_missing_columns`),
> `app/models.py:36,66,137,186,343,352,530` (`Client`, `Conversation`, `Ticket`, `User`,
> `CatalogCategory`, `CatalogService`, `Cobro`), `app/main.py:120-136,154-161` (routers y arranque),
> `app/archive.py` (los cinco lugares del espejo). Prior art de Chatwoot extraído el 2026-09-01 de
> `db/schema.rb` y `app/models/channel/whatsapp.rb` en su repo público.

Este documento es un **plan**, no código. Responde a: "el ERP se repartirá por varias oficinas en
distintas ciudades; el admin debe poder configurar cada oficina, sus usuarios y sus llaves de Twilio,
Meta y Stripe (agregar, editar, eliminar); cada oficina debe poder acceder a sus llaves; catálogo,
corpus y sinónimos también por oficina". La configuración que hoy opera en producción pertenece a la
oficina **Tlaxcala**.

---

## 0. Las cuatro decisiones que fijan el diseño

Se preguntaron porque cambian la arquitectura, no el detalle. Respuestas del operador:

| Decisión | Elegido | Lo que obliga |
|---|---|---|
| Meta | **Una App, varios números** | `app_secret` y `verify_token` son COMPARTIDOS. La firma HMAC no distingue oficina. El ruteo sale de `metadata.phone_number_id` del payload. |
| Twilio | **Una cuenta, varios números** | Un solo `account_sid`/`auth_token`. La oficina sale del campo `To` del form, ya con la firma validada. |
| Stripe | **Una cuenta POR oficina** | `stripe_secret_key` y `stripe_webhook_secret` sí son por oficina. Un endpoint de webhook por oficina en el panel de Stripe. |
| Llaves en el panel | **Todas, solo rol admin** | El valor real vive en la base, no solo el nombre de la variable. `is_admin` es la única puerta. |

La cuarta revierte una decisión anterior ("el panel solo mapea, el secreto sigue en `.env`"). Se
revierte porque era incompatible con lo pedido: un mapeo de nombres de variable no permite *agregar*
una llave desde el panel — dar de alta una oficina seguiría requiriendo SSH del operador. Y con Stripe
por oficina el `.env` deja de escalar: cada alta metería dos variables más a mano en el VPS.

---

## 1. Prior art: qué hace Chatwoot (el mismo benchmark de §2 del análisis de brechas)

No es opinión, es su `db/schema.rb`:

- `channel_twilio_sms` (línea 680): `t.string "auth_token", null: false` y `t.string "account_sid", null: false`.
  Columnas normales, **texto plano**.
- `channel_whatsapp` (línea 733): `t.text "business_management_token"`, `t.string "phone_number", null: false`,
  `t.string "provider"`, `t.jsonb "provider_config", default: {}` — el `provider_config` guarda el token
  de la Cloud API en plano.
- `app/models/channel/whatsapp.rb`: `encrypts :business_management_token if Chatwoot.encryption_configured?`
  — cifra **un** campo y **solo si** hay clave configurada; `after_update_commit :log_credentials_transfer`
  audita el cambio; `before_validation :ensure_webhook_verify_token` genera un verify token por canal.

Lecturas que se toman de ahí:

1. Guardar credenciales de proveedor por inquilino **en la base** es el patrón establecido, no una
   excepción. La decisión 4 no inventa nada.
2. Auditar el cambio de credencial es parte del patrón, no un extra.
3. Chatwoot cifra a medias. Aquí se cifra completo, y la razón es concreta: este SQLite ya guarda CURP,
   RFC y NSS de clientes reales. Sumarle `sk_live_…` sube el volcado de la base de "fuga de PII" a
   "puede cobrar tarjetas a nombre del negocio".

---

## 2. Modelo de datos

### 2.1 `office`

```
office
  id            int  PK          -- 1 = Tlaxcala (garantizado, ver §6)
  nombre        str  unique      -- "Tlaxcala"
  slug          str  unique      -- "tlaxcala" (para URLs de webhook de Stripe)
  ciudad        str
  habilitado    bool default 1   -- soft delete, como el resto del repo
  created_at    datetime
```

`zona_horaria` se retiró del modelo tras el PR 3: nunca alimentó `templating.BUSINESS_TZ`
(sigue siendo un valor global fijo, no por oficina), así que era un dato mostrado sin lógica
detrás. La columna sigue existiendo físicamente en la base ya desplegada del VPS (este repo
no hace `DROP COLUMN`, solo `ADD`) como huérfana inofensiva.

### 2.2 `office_credential`

```
office_credential
  id          int PK
  office_id   int  -- apunta a office.id; SIN FK declarada, como el resto del repo
  provider    str            -- meta | twilio | stripe
  clave       str            -- WHATSAPP_TOKEN, TWILIO_WHATSAPP_FROM, STRIPE_SECRET_KEY, ...
  valor_cif   bytes          -- Fernet(valor). NUNCA se devuelve al navegador.
  pista       str            -- ultimos 4 caracteres, para que el admin reconozca cuál es
  es_secreto  bool           -- False para phone_number_id / whatsapp_from (identificadores, no secretos)
  habilitado  bool default 1
  updated_by  str
  updated_at  datetime
```

Índice único parcial `(office_id, provider, clave) WHERE habilitado = 1` — mismo patrón que
`ux_ticketlabel_viva` en `db.py:155-160`. Eliminar una llave es apagar `habilitado`, no borrar la fila:
así el audit conserva quién la puso y cuándo.

**`office_credential` NO se replica al espejo MSSQL.** El espejo es opt-in — una tabla que no está en
los cinco lugares simplemente no se replica, y eso cuesta cero. Sacar secretos hacia un segundo motor
duplicaría el radio de fuga a cambio de nada: nadie va a reportar sobre llaves de API.

> Decisiones fijadas en el PR 1: la tabla física se llama `officecredential` (SQLModel sin `__tablename__`, igual que `ticketlabel`; el índice es `ux_officecredential_viva`); `clave` se guarda en MAYÚSCULAS con el nombre de la variable de entorno (unifica §2.2 con §7 paso 4); `office` tampoco se espeja por ahora — la spec solo excluía `office_credential`; espejarla serían los cinco lugares completos y se decide en el PR 3, cuando el panel cree oficinas.

### 2.3 `office_id` en las tablas existentes

Aislamiento total de datos. La regla para no ponerle `office_id` a las 35 tablas:

> Lleva `office_id` la tabla que el dashboard **filtra directamente**. La que solo se alcanza por join
> hereda la oficina de su padre.

| Lleva `office_id` | Por qué | La hereda por join |
|---|---|---|
| `client`, `conversation`, `ticket` | son la raíz de toda consulta del panel | `message` (por `conversation`), `ticketlabel`, `ticketactivity`, `clientnote` |
| `user` | define a qué oficina pertenece el colaborador | `loginevent`, `websession` |
| `teammessage` | el chat interno es por oficina | — |
| `cobro` | Stripe es por oficina; el cobro debe saber con qué cuenta se generó | `intentocobro`, `eventopasarela`, `plancobro`, `paymentmethod` |
| `catalogcategory`, `catalogservice` | catálogos independientes por oficina | `catalogaudit` |
| `synonymproposal`, `nluevent`, `infodoc` | corpus, sinónimos y RAG por oficina | — |
| `label`, `savedreply`, `plantilla`, `agentconfig` | configuración operativa por oficina | — |

Todas con el mismo DDL, que ya tiene precedente exacto en `db.py:41` (`provider VARCHAR NOT NULL DEFAULT 'meta'`):

```python
# office_id: DEFAULT 1 porque TODO lo que existe hoy es de Tlaxcala — es la única
# oficina que ha operado. Dejarlo NULL dejaría el histórico sin oficina y cada
# reporte por oficina saldría mal para siempre.
_OFFICE_COLUMN = {"office_id": "INTEGER NOT NULL DEFAULT 1"}
```

Sin Alembic: **un solo dict compartido** aplicado en bucle sobre la tupla de las 15 tablas, igual que
`_HABILITADO_COLUMN` / `_TABLES_NEEDING_HABILITADO` — no un dict por tabla. Y —para las que sí se
espejan, que son las 15— la misma columna entra a `_COLUMNS_ADDED_AFTER_LAUNCH` en `app/archive.py`,
donde el DDL se deriva de la misma constante `OFICINA_TLAXCALA_ID` para que los dos motores no se
desfasen.

---

## 3. Resolución de credenciales: herencia, no duplicación

Con una sola App de Meta y una sola cuenta de Twilio, copiar el mismo `WHATSAPP_TOKEN` en cinco filas
sería crear cinco lugares donde rotarlo. Una función, con herencia.

**IMPLEMENTADO (PR 2, `app/credenciales.py`).** Esta es la firma real, no un esbozo — los PR 3/4/5 la
leen como contrato:

```python
def credencial(clave: str, *, office_id: int, session: "Session | None" = None) -> str:
```

Tres diferencias con el pseudocódigo original, cada una con su motivo:

- **`provider` sale de la firma**, pero **NO sale del `WHERE`**: se deriva de `config.CREDENCIALES`.
  Pasarlo a mano era un segundo lugar donde equivocarse, y equivocarse ahí no da error — la fila no
  casa y se hereda en silencio la llave del `.env`. El `WHERE` lo sigue usando, así que el índice
  parcial `ux_officecredential_viva` se aprovecha igual.
- **`session` es keyword y opcional.** Se pasa solo donde ya está en la mano (`cobranza.py`,
  `dashboard.py`); obligatoria está prohibida porque `stripe_client` declara que no conoce SQLModel y
  `stripe_webhook` lee antes de que exista sesión.
- **`_ATRIBUTO_DE_SETTINGS` se reemplaza por `config.CREDENCIALES`**: `dict[str, Credencial]` con
  `provider`, `atributo`, `es_secreto` y `compartida`. Sigue siendo un mapa explícito y no un
  `clave.lower()`: dos de las llaves reales no siguen esa regla (`WHATSAPP_VERIFY_TOKEN` →
  `verify_token`, `WHATSAPP_APP_SECRET` → `app_secret`), y adivinarlo devolvería `""` para el secreto
  que valida la firma de los webhooks de Meta — un fail-closed que descartaría todo el tráfico
  entrante sin decir por qué. Una `clave` fuera del inventario levanta `KeyError`, nunca `""`.

**Invariante, y es lo que el PR 3 tiene que respetar: una fila viva de `officecredential` que casa
`(office_id, provider, clave)` SIEMPRE gana.** No hay atajo por tipo de llave: una llave marcada
`compartida` con fila propia se resuelve por la fila, no por el `.env` (§5). El campo `compartida` es
metadata de render del panel, no una regla del resolvedor.

**Sin caché: un `SELECT` por lectura (184 µs medidos).** El fallback `getattr(settings, ...)` se
reevalúa en CADA llamada, así que una llave editada desde el panel se ve en el siguiente request sin
reiniciar. Si algún día se cachea, solo puede cachearse **la fila** (o su ausencia), nunca el valor
efectivo.

**Fallos, con la asimetría deliberada:**

| Situación | Qué hace |
|---|---|
| Sin fila (el 100 % de producción hoy) | Hereda de `settings`. Devuelve `str`, nunca `None`. |
| Tabla `officecredential` ausente o `SELECT` roto | Loguea `warning` y **hereda**: sin fila conocida, heredar es correcto y hoy ninguna lectura de configuración puede fallar. |
| Hay fila y `CREDENTIALS_KEY` está ausente, malformada o ya no es la que cifró (o el `valor_cif` está corrupto) | `CredencialIlegible` (subclase de `RuntimeError`). **JAMÁS hereda ni devuelve `""`**: heredar sería firmar o cobrar con el secreto de otra oficina, y `""` lo leerían `webhook.py` / `stripe_webhook.py` como «no configurado» y darían 403 a TODO el tráfico con un diagnóstico falso. El mensaje lleva oficina/proveedor/clave y nunca el valor. |

El `try/except` cubre **solo** el `SELECT`, nunca el descifrado. Con `officecredential` vacía
`_descifrar()` no se ejecuta nunca, así que una `CREDENTIALS_KEY` ausente es **inobservable** el día
del despliegue del PR 2 — se vuelve obligatoria el día que el PR 3 guarde la primera llave.

### 3.1 Cifrado

`cryptography` (Fernet) con una clave maestra `CREDENTIALS_KEY` en el `.env`.

**Esto es una dependencia nueva real, no gratis.** `cryptography` **no** está en `requirements.txt` y
ninguna de las que sí están lo arrastra: el `import cryptography` que funciona en la laptop viene de
herramientas ajenas al proyecto instaladas en ese venv. En el contenedor de producción no existe. Hay
que agregar la línea `cryptography==45.0.7` y reconstruir la imagen. **No 42.x**: toda esa rama (42.0.0
–44.0.0) cae en el rango de CVE-2024-12797, corregido en 44.0.1. El bug es de la ruta TLS del OpenSSL
empotrado en el wheel y no toca a Fernet, pero pinear una versión con advisory abierto para *endurecer*
el almacenamiento de llaves se contradice sola, y cualquier escaneo de la imagen la marcaría.

Qué compra y qué no, sin adornos:

- **Compra:** un volcado de `data/*.db` —un respaldo, una copia extraviada, un `docker cp`— no sirve
  para nada sin el `.env`. El respaldo sigue siendo un pendiente abierto de este proyecto; es
  exactamente la ruta de fuga que esto cierra.
- **No compra:** nada contra un host comprometido. Quien lea el `.env` lee todo. No se pretende otra cosa.

**Decisión tomada: se agrega.** `cryptography==45.0.7` entra en el PR 1 de §8, no queda a criterio del
que lo implemente. La alternativa —texto plano, lo que hace Chatwoot— se descarta porque esta misma
base ya guarda CURP/RFC/NSS y el respaldo abierto es la ruta de fuga concreta. Revertirla después es
una decisión de riesgo explícita del operador, no un cambio de ingeniería: `valor_cif` guarda texto y
`_descifrar` se vuelve la identidad.

### 3.2 Reglas de manejo del valor

- El valor **nunca** se devuelve al navegador. El formulario es de solo escritura; la lista muestra
  `presente/ausente` y los últimos 4 caracteres (`pista`).
- Todo alta, edición y baja escribe una fila de auditoría con usuario y timestamp, con el valor
  **omitido**. Ya existe el patrón en `AdminAudit` / `CatalogAudit`.
- Un `POST` con el campo vacío **no borra**: significa "no lo cambies". Borrar es un botón aparte.

---

## 4. Ruteo de entrada — autenticar y resolver son dos cosas distintas

Confundirlas es el error que este diseño evita. La firma dice *si el mensaje es legítimo*. La oficina
sale del contenido, **después** de que la firma pasó.

### 4.1 Meta

Con una sola App, el `app_secret` es el mismo para todas las oficinas: la firma no puede decir de
cuál es. `app/webhook.py:33-38` se queda **igual** — misma ruta `/webhook`, misma verificación contra
`settings.app_secret`, mismo handshake GET contra `settings.verify_token`. Lo único nuevo es leer el
`metadata` que hoy se ignora:

```python
value = change.get("value", {})
phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
office_id = resolver_oficina_meta(phone_number_id)
if office_id is None:
    logger.warning("Webhook de Meta con phone_number_id desconocido: %s", phone_number_id)
    continue  # fail-closed: NUNCA cae a Tlaxcala por omisión
```

**Fail-closed explícito**: un `phone_number_id` que no mapea a ninguna oficina se descarta y se
registra. Caer a la oficina por defecto metería mensajes de un número nuevo en la bandeja de Tlaxcala
sin que nadie se entere.

Esto **borra el riesgo #1 del borrador anterior**. Aquel diseño ruteaba por slug en la URL
(`/webhook/{slug}`) con un `app_secret` por oficina, lo que exigía reconfigurar el webhook en el panel
de Meta y dejaba la ruta legada como un caso especial permanente. Aquí no hay ruta nueva, no hay
reconfiguración en Meta, y dar de alta una oficina es insertar una fila con su `phone_number_id`.

### 4.2 Twilio

Mismo patrón. La firma se valida con el único `auth_token` sobre la URL reconstruida desde
`PUBLIC_BASE_URL` (`app/webhook.py:215-233`, sin cambios). La oficina sale del campo `To` del form
—`whatsapp:+52…`, el número de la oficina— ya verificado. Fail-closed igual: `To` desconocido se
descarta y se registra.

### 4.3 Stripe — el que sí necesita ruta por oficina

Stripe es el caso distinto y hay que decirlo claro: **el evento de Stripe no trae identificador de
oficina**. Con una cuenta por oficina hay un `stripe_webhook_secret` por oficina, y dos formas de
resolver:

- **Ruta por oficina** (elegida): `POST /webhooks/stripe/{slug}`. Se busca la oficina por slug, se
  verifica la firma con **su** secreto, y listo. Un endpoint por oficina en el panel de Stripe, que de
  todos modos hay que dar de alta porque son cuentas distintas.
- Probar todos los secretos vivos hasta que uno valide. Funciona, pero cuesta N verificaciones HMAC por
  evento y hace ruidoso el log. Se descarta: la ruta por slug no cuesta trabajo extra aquí, porque
  configurar el endpoint en Stripe ya es parte del alta de la cuenta.

La ruta legada `/webhooks/stripe` se conserva y queda amarrada a Tlaxcala, para no tocar el endpoint
que hoy está configurado y funcionando en producción.

Además, la oficina del cobro **no** se deduce de la llave: sale del `Cobro` que el evento referencia.
La llave solo autentica.

---

## 5. Panel `/oficinas` y permisos

Router nuevo `app/offices.py`, registrado en `app/main.py` junto a los demás.

| Ruta | Quién | Qué |
|---|---|---|
| `GET /oficinas` | admin | Lista de oficinas con conteo de usuarios y estado de sus llaves |
| `POST /oficinas` | admin | Alta de oficina (nombre, ciudad, slug) |
| `GET /oficinas/{id}` | admin | Detalle: usuarios, llaves (presente/ausente + pista), catálogo |
| `POST /oficinas/{id}/llaves` | admin | Agregar / editar una llave |
| `POST /oficinas/{id}/llaves/{cid}/baja` | admin | Eliminar (soft delete) |
| `POST /oficinas/{id}/usuarios` | admin | Mover un colaborador a esta oficina |

**Solo `is_admin`.** No hay rol intermedio en este alcance: `app/auth.py:7-12` ya resuelve `is_admin`
leyendo la fila del `User`, y el resto del panel ya gatea secciones con él (`/unmatched`). Reusar esa
puerta es cero código nuevo de autorización, que es el código de autorización que menos errores tiene.

Consecuencia asumida y dicha en voz alta: **cualquier admin puede leer la pista y rotar la llave de
Stripe de cualquier oficina.** Con "admin global + admin de oficina" eso se acotaría, pero es un
segundo sistema de permisos y no es lo que se pidió aquí. Si más adelante hace falta, el corte natural
es `user.office_id` y un flag `is_admin_global`; queda anotado, no construido.

**La lista de llaves no deja huecos.** Cada llave del inventario `config.CREDENCIALES` aparece siempre
en el detalle de la oficina, con uno de **cuatro** estados: `propia` (hay fila en `officecredential`, se
muestra la pista), `heredada del global (.env)` (no hay fila, se muestra la pista del valor que
efectivamente se usa), `ausente` (ni fila ni `.env`) o **`ilegible`** (hay fila y `credencial()` levanta
`CredencialIlegible` — `CREDENTIALS_KEY` ausente o cambiada; §3). Un renglón en blanco no distingue "no
configurada" de "compartida con las demás oficinas", y esa es justo la diferencia que el admin necesita
ver para saber si rotar algo afecta a una oficina o a todas. El cuarto estado tampoco puede colapsarse
en "ausente": son dos acciones distintas del operador — pegar la llave contra restaurar
`CREDENTIALS_KEY`.

**Dos constricciones que el PR 2 le hereda al PR 3, y que hay que decidir a la vista, no descubrir:**

1. **`compartida = True` significa "renderizar de solo lectura como *heredada del global*", NO "el
   resolvedor la ignora".** El PR 2 rechazó el atajo que devolvía `settings` sin tocar la base: una
   fila viva de una llave compartida **sí gana**, incluso para la oficina 1. Si el panel la deja
   editar, la edición **funciona** — y solo para la oficina que la escribió, que es exactamente lo que
   el paso 4 de §7 quiere evitar. La alternativa (ignorarla) era peor: el panel mostraría una llave
   que nadie usa y la rotación mentiría en silencio.
2. **El panel DEBE escribir `provider` desde el inventario**, nunca a mano ni desde el formulario. Una
   fila con `provider` distinto al de `config.CREDENCIALES` no casa con el `WHERE` de `credencial()` y
   **hereda en silencio**: la llave se guarda, el panel la muestra como `propia` y el sistema sigue
   usando la del `.env`. No hay error que verlo.

Aislamiento del resto del panel: cada consulta del dashboard filtra por `office_id` del usuario en
sesión. Un admin ve todas; un colaborador ve la suya. El punto único donde se aplica es el helper de
sesión, no 40 vistas.

---

## 6. Catálogo, corpus y sinónimos por oficina

- `CatalogCategory` / `CatalogService` con `office_id`. El seed desde `catalog.json` (que ya solo corre
  cuando las tablas están vacías) siembra la oficina 1. Una oficina nueva arranca con catálogo vacío y
  un botón **"Copiar catálogo de…"** — clonar filas es más barato y más honesto que un catálogo global
  con excepciones por oficina.
- `SynonymProposal` con `office_id`: un sinónimo aprobado en Tlaxcala no aplica en otra ciudad, donde
  el mismo trámite puede llamarse distinto. **La regla de oro no cambia**: nada se auto-aprende, el
  humano aprueba en `/unmatched`, ahora dentro de su oficina.
- `InfoDoc` (corpus del RAG) con `office_id`: la recuperación filtra por oficina. Requisitos y precios
  varían por estado; mezclarlos es exactamente la respuesta equivocada a un cliente real.
- `NluEvent` con `office_id`: el benchmark del NLU deja de ser un promedio de ciudades distintas.

**¿Hace falta Postgres o Redis?** No. Un SQLite en WAL con un solo proceso uvicorn aguanta de sobra
el tráfico de un puñado de oficinas; el cuello de botella medido de este sistema es la latencia del
clasificador (~5 s), no la base. Cambiar de motor ahora sería pagar una migración por un problema que
no existe todavía. El disparador para reconsiderarlo es concreto: más de un worker de uvicorn, o
escrituras que empiecen a chocar con el lock de WAL.

---

## 7. Migración de Tlaxcala — el orden importa

`init_db()` corre `create_all()` y **luego** los `_add_missing_columns` (`db.py:127-134`). El alta de
Tlaxcala tiene que ir en medio:

1. `create_all()` crea `office` y `office_credential` vacías.
2. **Sembrar Tlaxcala como `id = 1`** si no existe la fila `id = 1` — no «si la tabla está vacía»: lo
   que cada `office_id DEFAULT 1` necesita es esa fila concreta, y comprobar por id evita además
   resucitar una Tlaxcala dada de baja. Va aquí, no después: el resto de la migración asume que existe.
3. `_add_missing_columns(...)` agrega `office_id INTEGER NOT NULL DEFAULT 1` a cada tabla. SQLite
   rellena todas las filas existentes con 1 en el mismo `ALTER`. **No hay backfill que escribir.**
4. Las **seis** llaves **compartidas** (`WHATSAPP_TOKEN`, `WHATSAPP_APP_SECRET`,
   `WHATSAPP_VERIFY_TOKEN`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_CONTENT_SID_FEEDBACK`)
   **no se copian** a la tabla: son una sola llave para todas las oficinas y
   duplicarlas sería crear N lugares donde rotar una. Por la herencia de §3 resuelven al `.env` actual
   — el comportamiento de producción no cambia ni un byte el día del despliegue— y el panel las muestra
   como `heredada del global` (§5), no como ausentes.

   `TWILIO_CONTENT_SID_FEEDBACK` no estaba clasificada en ninguna de las dos listas; se marca
   **compartida** porque el ContentSid pertenece a la cuenta de Twilio y §0 fijó una sola cuenta.
   Es un juicio, no un dato de la spec original: revertirlo es una línea de `config.CREDENCIALES`, y
   con el atajo del resolvedor rechazado (§3) la clasificación **solo** afecta cómo lo renderiza el
   panel.

   **No se copian ≠ el resolvedor las ignora.** Si alguien inserta una fila de una llave compartida
   para la oficina 1, esa fila gana (§3, §5). El paso 4 es una instrucción de migración —qué NO
   sembrar— no una regla de resolución.
5. Las llaves que **sí son por oficina** se siembran como filas de Tlaxcala:
   - `WHATSAPP_PHONE_NUMBER_ID` (Meta) y `TWILIO_WHATSAPP_FROM` (Twilio), `es_secreto = False`. Sin
     ellas §4.1 no puede resolver la oficina y el fail-closed descartaría todo el tráfico actual.
   - `STRIPE_SECRET_KEY` y `STRIPE_WEBHOOK_SECRET`, `es_secreto = True`. Stripe es cuenta por oficina
     por la decisión de §0; dejarlas heredadas del `.env` contradiría esa decisión desde el día uno.

   `clave` es siempre el nombre EXACTO de la variable de entorno, en mayúsculas — que es como el
   operador ya las conoce del `.env`. Ojo en el PR 2: dos de ellas no se derivan del atributo de
   `settings` por `clave.lower()` (`WHATSAPP_VERIFY_TOKEN` → `settings.verify_token`,
   `WHATSAPP_APP_SECRET` → `settings.app_secret`), así que la herencia de §3 necesita un mapa
   explícito, no `getattr(settings, clave.lower())`.

   Con esto la única oficina que hoy opera queda con **todas sus llaves accesibles desde el panel** —
   las propias como fila, las compartidas como heredadas— que es literalmente lo pedido, sin duplicar
   un token que es uno solo.

Seis compartidas + cuatro por oficina = **las diez** entradas de `config.CREDENCIALES`. El inventario
no tiene huecos y una prueba de arnés (`tests/test_credenciales_inventario.py`) lo amarra en las dos
direcciones contra `app/config.py`.

**`PUBLIC_BASE_URL` queda FUERA del inventario a propósito**, aunque tenga forma de configuración por
despliegue: es infra del contenedor (una URL pública por servicio), no una credencial, y equivocarla
hace fallar **todas** las firmas de Twilio, no algunas. El riesgo real es que alguien la meta "por
simetría" en un PR posterior: si §4.3 llega a exigirla por oficina, entra entonces y con ese motivo
escrito, no antes.

~~El paso 5 es el único que **debe** hacerse antes de que el ruteo por `phone_number_id` entre en
vigor. Por eso va en su propio PR, antes que el del ruteo.~~

**CORREGIDO AL IMPLEMENTAR EL PR 4.** El paso 5 **no** es un requisito previo, y sembrar esas dos
filas empeoraría el sistema. El resolvedor de `app/ruteo.py` compara contra el valor **resuelto** de
cada oficina —`credencial()`, con la herencia del PR 2— así que con `officecredential` vacía la
oficina 1 hereda del `.env` y casa: el ruteo funciona ANTES de sembrar nada. Eso elimina el día de
bandera que este párrafo temía, en el que desplegar el ruteo antes de correr un script descartaría
todo el tráfico entrante.

Y al revés: **sembrarlas cifradas ataría el webhook a `CREDENTIALS_KEY`**. Hoy una llave maestra
perdida rompe el panel y los envíos; con las filas de ruteo sembradas rompería también la ENTRADA —
`_descifrar` levanta, la oficina no resuelve y el fail-closed descarta todo—, convirtiendo un
problema de configuración en un apagón. Heredando, el peor caso deja los mensajes entrando a la
bandeja correcta.

Queda entonces: `WHATSAPP_PHONE_NUMBER_ID` y `TWILIO_WHATSAPP_FROM` **se dejan heredadas** salvo que
una segunda oficina obligue a distinguirlas (y entonces se siembran desde el panel, que es su lugar).
`STRIPE_SECRET_KEY` y `STRIPE_WEBHOOK_SECRET` siguen siendo por oficina —§0— y se cargan desde
`/oficinas/{id}` cuando exista la segunda cuenta; no las necesita el ruteo de entrada.

---

## 8. Plan de implementación

Cinco PRs. Cada uno despliega solo y deja producción funcionando igual que antes.

| PR | Qué | Riesgo | Prueba que lo cierra |
|---|---|---|---|
| 1 | `office` + `office_credential` + siembra de Tlaxcala + `office_id` en las tablas de §2.3 + `cryptography` en `requirements.txt` + `office_id` en el espejo MSSQL (dict de `archive.py` + 15 upserts) | Bajo — nada lo lee todavía | Arrancar sobre una copia de la base de producción y verificar que toda fila quedó en `office_id = 1` |
| 2 | `credencial()` con herencia + los 9 archivos que hoy leen `settings.*` pasan a usarla | **Medio — es el PR delicado** | El suite completo verde sin una sola fila en `office_credential`: prueba que la herencia reproduce el comportamiento actual |
| 3 | Panel `/oficinas`: alta, usuarios, CRUD de llaves, auditoría | Bajo | Alta de una oficina de prueba, poner y quitar una llave, verificar que el valor nunca aparece en el HTML |
| 4 | Ruteo por `phone_number_id` y por `To`, fail-closed. **SIN sembrar filas**: el resolvedor compara contra el valor resuelto por `credencial()`, así que la oficina 1 hereda del `.env` y el ruteo funciona con la tabla vacía — sembrarlas cifradas ataría la ENTRADA a `CREDENTIALS_KEY` (ver la corrección al final de §7). Cuando haga falta sembrar una llave `compartida` —las tres de Meta que lo son, más las de Twilio— va por script con `credenciales.guardar()`: el panel del PR 3 no las deja crear. | **Alto — toca el webhook** | Test de webhook con `phone_number_id` desconocido que debe descartar y registrar, no procesar |
| 5 | Stripe por oficina: `/webhooks/stripe/{slug}` + `auth.alcance()` y el filtro donde se puede observar (ver la corrección abajo) | Medio | **Folio** de la oficina 2 que no aparece para un colaborador de la oficina 1, ni por el listado ni tecleando su id |

El PR 2 es el que hay que revisar con lupa: cambia cómo se obtiene **toda** credencial en los 9
archivos que hoy leen `settings` directamente (`cobranza.py`, `cobranza_panel.py`, `dashboard.py`,
`stripe_client.py`, `stripe_webhook.py`, `system.py`, `twilio_client.py`, `webhook.py`,
`whatsapp_client.py`). La prueba de que no rompió nada es el suite verde con la tabla vacía.

---

## 9. Lo que deliberadamente NO se hace

- **Stripe Connect.** Es el modelo que Stripe recomienda para varios negocios bajo un techo, pero
  cambia el flujo de cobro completo, no solo dónde vive la llave. Es una fase propia, no un detalle
  de ésta.
- **Migrar a Postgres o Redis.** §6: no hay medición que lo justifique.
- **Espejar `office_credential` a MSSQL.** §2.2.
- **Un segundo sistema de roles (admin global / admin de oficina).** §5: se pidió `is_admin` y eso es
  lo que se construye. El corte queda anotado por si hace falta.
- **Cruzar la línea del producto.** Nada aquí hace que el bot fije precio final, valide documentos o
  cierre un trámite. Una oficina nueva hereda esa línea intacta.

---

## 10. Riesgos

| Riesgo | Mitigación |
|---|---|
| Una llave de Stripe viva pegada en un formulario web queda al alcance de cualquier admin | Cifrado en reposo, valor nunca devuelto al navegador, auditoría de cada cambio. El riesgo residual es real y fue aceptado explícitamente en §0. |
| `CREDENTIALS_KEY` perdida = todas las llaves ilegibles | La clave va al `.env` del VPS junto a las demás y entra al mismo procedimiento de respaldo. Si se pierde, se vuelven a pegar las llaves desde el panel: son recuperables desde Meta/Twilio/Stripe, no son datos. |
| Una consulta del panel olvida el filtro `office_id` y una oficina ve datos de otra | El filtro se aplica en el helper de sesión, un solo lugar. Test que recorre las vistas de lista con dos oficinas sembradas y verifica que ninguna filtra de más. |
| `cryptography` rompe el build de la imagen Docker | Se pinea versión exacta y se prueba el build antes de promover a `production`. |
| El fail-closed de §4.1 descarta tráfico legítimo si falta la fila de Tlaxcala | Por eso el PR 4 va después del que inserta esa fila, y el descarte se registra con `logger.warning` — un `grep` al log lo delata en minutos. |


---

## 11. Correcciones hechas al implementar

Dos veces la implementación contradijo a la spec y ganó la implementación, con la evidencia medida.
Queda escrito aquí para que el que venga no lo redescubra como bug.

### 11.1 El paso 5 de §7 no era un requisito previo (PR 4)

Está corregido en el propio §7. Resumen: el resolvedor compara contra el valor **resuelto** por
`credencial()`, así que la oficina 1 hereda del `.env` y el ruteo funciona con `officecredential`
vacía. Sembrar esas filas cifradas ataría la ENTRADA a `CREDENTIALS_KEY` y convertiría una llave
maestra perdida en un apagón.

### 11.2 La prueba de cierre del PR 5 era imposible como estaba escrita

§8 pedía «cobro de prueba en la oficina 2 que no aparece en el panel de la oficina 1». **No se puede
observar**: `/cobros` exige `is_admin` (`_admin_or_redirect`), y §5 decidió —al descartar el rol de
admin-de-oficina— que **cualquier admin ve todas**. Nadie que llegue a esa pantalla está acotado
nunca, así que el filtro ahí es inerte por construcción, no solo por tener una sola oficina.

§5 manda sobre la fila de §8: es la decisión de roles sobre la que el PR 3 ya construyó, mientras que
la fila era una sugerencia de prueba. Las vistas donde «un colaborador ve la suya» significa algo son
las que un colaborador alcanza: `/`, `/tickets/{id}`, `/clients`, `/inbox/{phone}`, `/my`, `/team`,
`/kb`, `/replies`. La prueba de cierre se mudó a `/`, la lista de folios donde vive todo el día.

El filtro de `cobranza_panel.py` se conserva —es correcto y `ve_todas=True` lo vuelve un no-op— con
un comentario que dice que hoy es inerte, para que nadie lo lea como comportamiento probado.

**Y un hallazgo que la spec no anticipaba:** acotar el LISTADO no cierra `/tickets/{id}`, cuyo id
viene de la URL. Un colaborador tecleando el id de un folio de otra oficina lo abriría entero —
teléfono del cliente, historial, notas—. Eso sí es un agujero de autorización, no preparación
inerte, y se cierra en el detalle además del listado.

### 11.3 §6: cerrado el corpus del RAG, diferido el resto

De los cuatro puntos de §6 se cerró **uno**, el que puede darle una respuesta equivocada a un cliente
hoy: `rag._docs()` acota el corpus curado (`InfoDoc`) por oficina, el bot le pasa la del cliente
(`conversation._try_informative`) y el Estudio la del admin que prueba, que además es la oficina en
la que nacen los párrafos nuevos —sin eso, el admin de una oficina nueva escribiría documentos que su
propio bot no podría leer—.

El **catálogo sigue global a propósito**, no por olvido: acotarlo son 61 puntos de uso de su API más
el botón «Copiar catálogo de…», o sea un PR entero. Y lo que aporta al corpus son nombres y
requisitos de servicio, que es justo lo que el flujo determinista ya le dice a cualquier cliente.

La **medición del NLU también se cerró**: `alerts._no_entendidos_stats` y las tres métricas del
Estudio aceptan `office_id`, con `None` = todas como default. Lo que §6 pide ahí es poder DESGLOSAR
—«el benchmark deja de ser un promedio de ciudades distintas»— no restringir: quien mira esas
pantallas es siempre un admin, y un admin ve todas por §5. La prueba lo enseña con números: 50 % en
Tlaxcala y 0 % en Puebla promedian un 25 % que no describe a ninguna de las dos.

**La ALERTA sigue siendo una sola y combinada**, y es deliberado: partirla por oficina exige decidir
a quién se le manda cada una, y ese ruteo no existe. Un porcentaje combinado que dispara de más es un
aviso de sobra; uno por oficina que nadie recibe es un aviso perdido. Lo amarra una prueba.

Quedan dos, aseverados en `tests/test_alcance_oficina.py` (`SEIS_DIFERIDO`):

| Punto de §6 | Estado |
|---|---|
| Catálogo por oficina + «Copiar catálogo de…» | **Decisión vigente: compartido.** No es deuda — ver abajo |
| Sinónimos aprobados dentro de su oficina | Bloqueado por el anterior, no por tamaño |

**El catálogo compartido es una decisión, no una simplificación pendiente** (confirmado con el
operador el 2026-09-03): hoy los trámites y sus requisitos son los mismos en todas las oficinas, así
que un solo catálogo es la respuesta correcta. El **disparador** para separarlo es concreto: el día
que una oficina necesite un precio o un requisito distinto. Entonces son 61 puntos de uso de la API
del catálogo más el botón «Copiar catálogo de…», y detrás va el de los sinónimos.

El de los sinónimos merece decirse completo, porque parece pequeño y no lo es. Son 3 consultas, pero
`catalog.add_synonym` escribe `CatalogService.synonyms_json`, que es **global**. Acotar solo la lista
de propuestas construiría justo la ilusión que §6 quiere evitar, y en peor forma que hoy: ahora todo
admin ve todas las propuestas y sabe que aprobar tiene efecto global; con la lista acotada, el admin
de Puebla aprobaría algo que se ve suyo y cambiaría el NLU de Tlaxcala **en silencio** — un trámite
mal cotizado a un cliente real, que es exactamente la falla que la regla de oro («nada se
auto-aprende») existe para impedir. El estado actual es más seguro que la media solución.

Ninguno de los dos es observable hasta que una segunda oficina opere. Por eso la lista es una prueba
y no una nota: cerrar uno obliga a actualizarla en el mismo commit.

### 11.4 Lo que queda sin acotar en el panel, aseverado

Con una sola oficina operando, un filtro que falta no se nota en ninguna pantalla. Por eso la lista
de módulos del panel todavía sin acotar vive como un candado en `tests/test_alcance_oficina.py`
(`SIN_ACOTAR`) y no como una nota: agregar un módulo de panel nuevo rompe la prueba. `users.py` está
ahí **a propósito** —acotarlo contradiría a §5— y `offices.py` también, porque ver todas las oficinas
es su función entera.
