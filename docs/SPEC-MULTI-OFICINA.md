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
  zona_horaria  str  default "America/Mexico_City"
  habilitado    bool default 1   -- soft delete, como el resto del repo
  created_at    datetime
```

### 2.2 `office_credential`

```
office_credential
  id          int PK
  office_id   int  -> office.id
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

Sin Alembic: cada tabla entra a su dict `_*_COLUMNS_ADDED_AFTER_LAUNCH` en `app/db.py` y —para las que
sí se espejan— a `_COLUMNS_ADDED_AFTER_LAUNCH` en `app/archive.py`.

---

## 3. Resolución de credenciales: herencia, no duplicación

Con una sola App de Meta y una sola cuenta de Twilio, copiar el mismo `WHATSAPP_TOKEN` en cinco filas
sería crear cinco lugares donde rotarlo. Una función, con herencia:

```python
def credencial(session, office_id: int, provider: str, clave: str) -> str:
    """Valor de la oficina si existe; si no, el global del .env.

    La herencia es lo que hace gratis la migración de Tlaxcala: cero filas en
    office_credential = exactamente el comportamiento de hoy.
    """
    fila = session.exec(
        select(OfficeCredential).where(
            OfficeCredential.office_id == office_id,
            OfficeCredential.provider == provider,
            OfficeCredential.clave == clave,
            OfficeCredential.habilitado == True,  # noqa: E712
        )
    ).first()
    if fila:
        return _descifrar(fila.valor_cif)
    return getattr(settings, clave.lower(), "")
```

### 3.1 Cifrado

`cryptography` (Fernet) con una clave maestra `CREDENTIALS_KEY` en el `.env`.

**Esto es una dependencia nueva real, no gratis.** `cryptography` **no** está en `requirements.txt` y
ninguna de las que sí están lo arrastra: el `import cryptography` que funciona en la laptop viene de
herramientas ajenas al proyecto instaladas en ese venv. En el contenedor de producción no existe. Hay
que agregar la línea `cryptography==42.0.8` y reconstruir la imagen.

Qué compra y qué no, sin adornos:

- **Compra:** un volcado de `data/*.db` —un respaldo, una copia extraviada, un `docker cp`— no sirve
  para nada sin el `.env`. El respaldo sigue siendo un pendiente abierto de este proyecto; es
  exactamente la ruta de fuga que esto cierra.
- **No compra:** nada contra un host comprometido. Quien lea el `.env` lee todo. No se pretende otra cosa.

**Decisión tomada: se agrega.** `cryptography==42.0.8` entra en el PR 1 de §8, no queda a criterio del
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
| `POST /oficinas` | admin | Alta de oficina (nombre, ciudad, slug, zona horaria) |
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

**La lista de llaves no deja huecos.** Cada llave del inventario de `app/config.py` aparece siempre en
el detalle de la oficina, con uno de tres estados: `propia` (hay fila en `office_credential`, se muestra
la pista), `heredada del global (.env)` (no hay fila, se muestra la pista del valor que efectivamente se
usa) o `ausente` (ni fila ni `.env`). Un renglón en blanco no distingue "no configurada" de "compartida
con las demás oficinas", y esa es justo la diferencia que el admin necesita ver para saber si rotar algo
afecta a una oficina o a todas.

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
2. **Sembrar Tlaxcala como `id = 1`** si `office` está vacía. Va aquí, no después: el resto de la
   migración asume que la oficina 1 existe.
3. `_add_missing_columns(...)` agrega `office_id INTEGER NOT NULL DEFAULT 1` a cada tabla. SQLite
   rellena todas las filas existentes con 1 en el mismo `ALTER`. **No hay backfill que escribir.**
4. Las llaves **compartidas** (`whatsapp_token`, `app_secret`, `verify_token`, `twilio_account_sid`,
   `twilio_auth_token`) **no se copian** a la tabla: son una sola llave para todas las oficinas y
   duplicarlas sería crear N lugares donde rotar una. Por la herencia de §3 resuelven al `.env` actual
   — el comportamiento de producción no cambia ni un byte el día del despliegue— y el panel las muestra
   como `heredada del global` (§5), no como ausentes.
5. Las llaves que **sí son por oficina** se siembran como filas de Tlaxcala:
   - `phone_number_id` (Meta) y `whatsapp_from` (Twilio), `es_secreto = False`. Sin ellas §4.1 no puede
     resolver la oficina y el fail-closed descartaría todo el tráfico actual.
   - `stripe_secret_key` y `stripe_webhook_secret`, `es_secreto = True`. Stripe es cuenta por oficina
     por la decisión de §0; dejarlas heredadas del `.env` contradiría esa decisión desde el día uno.

   Con esto la única oficina que hoy opera queda con **todas sus llaves accesibles desde el panel** —
   las propias como fila, las compartidas como heredadas— que es literalmente lo pedido, sin duplicar
   un token que es uno solo.

El paso 5 es el único que **debe** hacerse antes de que el ruteo por `phone_number_id` entre en vigor.
Por eso va en su propio PR, antes que el del ruteo.

---

## 8. Plan de implementación

Cinco PRs. Cada uno despliega solo y deja producción funcionando igual que antes.

| PR | Qué | Riesgo | Prueba que lo cierra |
|---|---|---|---|
| 1 | `office` + `office_credential` + siembra de Tlaxcala + `office_id` en las tablas de §2.3 + `cryptography` en `requirements.txt` | Bajo — nada lo lee todavía | Arrancar sobre una copia de la base de producción y verificar que toda fila quedó en `office_id = 1` |
| 2 | `credencial()` con herencia + los 9 archivos que hoy leen `settings.*` pasan a usarla | **Medio — es el PR delicado** | El suite completo verde sin una sola fila en `office_credential`: prueba que la herencia reproduce el comportamiento actual |
| 3 | Panel `/oficinas`: alta, usuarios, CRUD de llaves, auditoría | Bajo | Alta de una oficina de prueba, poner y quitar una llave, verificar que el valor nunca aparece en el HTML |
| 4 | Filas de Meta/Twilio de Tlaxcala + ruteo por `phone_number_id` y por `To`, fail-closed | **Alto — toca el webhook** | Test de webhook con `phone_number_id` desconocido que debe descartar y registrar, no procesar |
| 5 | Stripe por oficina: `/webhooks/stripe/{slug}` + filtro por `office_id` en todas las vistas del panel | Medio | Cobro de prueba en la oficina 2 que no aparece en el panel de la oficina 1 |

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
