# DISEÑO FINAL — Cobranza con Stripe (Fase 1: pago único por folio)

> Esqueleto base: **Diseño 3** (log de eventos como fuente de verdad + monotonía por rango + barridos de recuperación).
> Injertos: **de D2** la separación pedido/cobrado/liquidado, `livemode`, MSI real, comisión y neto; **de D1** la disciplina de superficie mínima, el `moneda='mxn'` en minúsculas y el no-op silencioso del cliente HTTP.
> Los 24 fallos de los revisores están cerrados abajo, cada uno con su mecanismo nombrado.

Todo lo que sigue está verificado contra el código real del repo: `app/db.py:59-77` (índice parcial a mano), `app/archive.py:28,317,337-390,500,782,794,875` (los cinco lugares + `_safe` + backfill), `app/system.py` (`MIRROR_TABLES`, `_csv_response`), `app/whatsapp_client.py:29-63` (patrón de reintentos y no-op), `app/webhook.py:31-60` (raw body antes del json + firma fail-closed), `app/dashboard.py:372-417,494-588,665-668,721-748`, `app/inbox.py:56`, `app/activity.py:39-47`, `app/conversation.py:304-310,556-566,964`, `tests/test_mirror_consistency.py`, `tests/test_db_migrations.py`, `tests/test_authz.py:478`, `tests/test_archive.py:20`.

---

## 0. Las siete decisiones duras (léelas antes que el esquema)

| # | Contradicción entre diseños | Decisión | Por qué |
|---|---|---|---|
| 1 | ¿Cancelar libera el candado? (D1/D2/D3 sí; el revisor dice que eso duplica cobros) | **NO.** El candado no depende de `estado` sino de la columna `llave_cobro_vivo`, y esa solo se pone en NULL cuando **Stripe confirma** que el instrumento murió. Si Stripe rechaza la cancelación, el cobro queda en `cancelacion_solicitada` y el candado sigue puesto. | Expirar la sesión NO mata el voucher OXXO. Lo que lo mata es `POST /v1/payment_intents/{pi}/cancel`. Mientras no haya confirmación, existe un instrumento cobrable y el folio no puede recibir un segundo link. |
| 2 | ¿Quién escribe `vencido`? (D1: el reloj del cron; D2: Stripe consultado; D3: solo Stripe) | **Solo Stripe.** Ningún reloj local escribe un estado terminal. El cron consulta y escribe el resultado como **evento sintético** que pasa por el mismo aplicador. | La sesión expira a 24 h y el voucher vive 72 h. Vencer por `expires_at` mata cobros vivos y luego descarta el pago real. |
| 3 | ¿El pago puede llegar a un cobro no-`pendiente`? (D1: no; D2: solo desde `vencido`; D3: monotonía por rango) | **Monotonía por rango.** `pagado` (rango 4) gana sobre `cancelado`/`vencido`/`fallido` (rango 3) y sobre `cancelacion_solicitada` (2), con `discrepancia=True` + Alert. | El dinero real siempre gana sobre el estado local. Rechazar un pago verdadero es el fallo más caro de la lista. |
| 4 | ¿`recomputar_estado()` es fiel? (D3 lo promete pero cancelar no es evento) | **Toda transición que no viene de Stripe se escribe también en `eventopasarela`** con id sintético (`panel:cancel:…`, `cron:recon:…`, `panel:crear:…`). El replay es completo y `recomputar_estado()` reconstruye también `llave_cobro_vivo` y los campos absorbidos. | Sin esto el replay resucita cobros cancelados y produce dos links vivos en el mismo folio. |
| 5 | Recuperar un intento huérfano >24 h "buscando por metadata" (D2/D3) | **No existe filtro por metadata en Checkout Sessions.** Se busca por **ventana de `created`** sobre `/v1/checkout/sessions` filtrando la metadata del lado nuestro, y como respaldo `/v1/payment_intents/search` (que sí soporta metadata, y por eso la metadata se manda **dos veces**: a nivel sesión y en `payment_intent_data[metadata]`). Búsqueda vacía **nunca** autoriza crear si el intento tiene <5 min; tras 3 barridos sin resolver → Alert y cero creación. | Reintentar el POST con clave caducada crea una segunda sesión: es el único camino real a un doble cargo. |
| 6 | ¿El candado es por folio? (los tres) | **No: por `(teléfono, servicio)`.** `llave_cobro_vivo` nunca es NULL mientras el cobro vive. El bloque del panel lista los cobros vivos **del teléfono**, no del folio. | El folio no es estable 72 h: `_latest_open_ticket` (`app/conversation.py:304`) excluye los CERRADO, así que el bot fabrica folio nuevo y el candado por `ticket_id` no ve nada. Amarrar solo al teléfono bloquearía dos trámites legítimos en paralelo; amarrar a `(teléfono, servicio)` bloquea exactamente el vector de duplicado. |
| 7 | ¿200 siempre al webhook? (D1/D2 sí; D3 no) | **200 solo si la fila del evento quedó COMMITEADA.** `IntegrityError` = duplicado → 200. Cualquier otra excepción al insertar (`OperationalError: database is locked`, que este repo ya tiene pineado en `tests/test_concurrency.py:811`) → **503**, para que Stripe reintente. Fallar *aplicando* sí devuelve 200: el payload ya es nuestro y lo reprocesa el barrido. | Confundir "duplicado" con "no pude escribir" pierde eventos de OXXO para siempre. |

**Regla de oro heredada del proyecto, escrita en el código:** el bot **nunca** genera un cobro. El link siempre lo dispara un humano desde el panel. Va como comentario en la cabecera de `app/cobranza_panel.py`.

**La regla es sobre COBRAR, no sobre COTIZAR** *(precisión de 2026-07-30; la redacción anterior decía que `app/conversation.py` "sigue diciendo `(A cotizar)`" y con el catálogo de producción eso dejó de ser cierto)*. El bot sí dice precios: `_confirm_message` le lee `service.price_info` al cliente y 51 de los 58 servicios traen número; solo 7 siguen en "A cotizar". Eso es exactamente lo que promete `landing/terminos.html` §2 — *"los precios informados por el asistente provienen de nuestro catálogo vigente"* — y por eso el número cotizado se congela en `monto_cotizado_centavos`: es el compromiso contra el que se mide el override (regla 14 de alertas).

**Alcance de métodos en PR1:** tarjeta + MSI + OXXO. **SPEI se pospone** (PR1.5, ~50 líneas): en Stripe MX no es un `payment_method_types` plano sino `customer_balance` con `bank_transfer[type]=mx_bank_transfer`, exige un Customer en la sesión y un paso extra para aplicar el saldo. El esquema ya lo soporta (`metodos_ofrecidos` acepta `spei`, `stripe_customer_id` existe, `payment_intent.partially_funded` ya está en el mapa de eventos). *Verificar los nombres exactos contra la doc vigente de Stripe al implementar: es la parte de su API que más ha cambiado.*

---

## 1. Tablas finales

Cinco tablas (las acordadas). En PR1 **`plancobro` y `paymentmethod` nacen inertes**: DDL, espejo y snapshot de pruebas completos, **cero escrituras y cero rutas**. Se declaran ahora para no pagar dos veces los cinco lugares del espejo cuando llegue el recurrente.

Nombres SQLite = clase en minúsculas pegada (convención de SQLModel en este repo). Sin `ForeignKey` en ninguna columna: es la convención del repo (`FollowUp.ticket_id`, `Feedback.ticket_id`) y además simplifica el orden del backfill.

### 1.1 `cobro` → MSSQL `cobros` — **mutable** (read model reconstruible)

Es el documento de cobro. Su `estado` es un **read model**: siempre recomputable con replay de `eventopasarela` ordenado por `(stripe_created, id)`.

| Campo | Tipo | Nota |
|---|---|---|
| `id` | `int \| None` PK | autoincremental |
| `ticket_id` | `int \| None` idx | nullable: el cobro sobrevive al cierre del folio y hay cobros sin folio |
| `phone` | `str` idx | congelado al crear; el webhook no debe cargar el ticket para avisar |
| `client_name` | `str \| None` | congelado, para cuadrar contra el estado de cuenta |
| `service_id` | `str \| None` | congelado; NULL o `'escalado'` en folios escalados |
| `concepto` | `str` | lo que ve el cliente en Stripe y en WhatsApp; inmutable |
| **`moneda`** | `str = 'mxn'` | **minúsculas**, como lo exige Stripe. El sufijo "MXN" lo agrega la vista |
| **`monto_centavos`** | `int` | lo **pedido**. Entero, jamás float ni Decimal |
| `monto_cotizado_centavos` | `int \| None` | lo que el bot le **cotizó** al cliente (parseado de `ticket.price_info`; NULL si "A cotizar"). Es la línea base del override |
| `monto_catalogo_centavos` | `int \| None` | precio de lista vigente al generar. Hoy siempre NULL |
| `monto_origen` | `str` | `cotizado` \| `override` \| `manual` |
| **`monto_pagado_centavos`** | `int \| None` | lo **cobrado** (`amount_total`/`amount_received`). Nunca pisa `monto_centavos` |
| `moneda_pagada` | `str \| None` | minúsculas, como llega |
| **`monto_reembolsado_centavos`** | `int = 0` | acumulado desde `amount_refunded` del charge (no sumando eventos) |
| `comision_centavos` | `int \| None` | `fee` de la balance transaction; **leído, nunca calculado** |
| `neto_centavos` | `int \| None` | `net` de la balance transaction, menos reembolsos (en MX la comisión no se recupera) |
| `moneda_liquidacion` | `str \| None` | puede diferir de la cobrada |
| `balance_transaction_id` | `str \| None` | `txn_…`; liga con el payout |
| `metodos_ofrecidos` | `str = 'card'` | CSV: `card,oxxo` (convención de `NluEvent.service_id`) |
| `metodo_usado` | `str \| None` | lo que el cliente realmente usó |
| `msi_meses_ofrecidos` | `str \| None` | CSV de plazos: `'3,6,9,12'`. NULL = sin MSI |
| `msi_meses_usado` | `int \| None` | plazo elegido por el cliente; **cambia la comisión** |
| **`estado`** | `str = 'borrador'` idx | ver máquina de estados (§1.6) |
| `estado_motivo` | `str \| None` | el porqué en palabras, ≤500 chars |
| **`llave_cobro_vivo`** | `str \| None` UNIQUE parcial | `"{phone}:svc:{service_id}"` \| `"{phone}:folio:{ticket_id}"` \| `"{phone}:libre"`. **NUNCA NULL mientras exista instrumento cobrable**. Solo pasa a NULL con confirmación de Stripe |
| **`llave_generacion`** | `str \| None` UNIQUE parcial | `"form:{uuid del render}"` \| `"plan:{id}:{periodo}"`. Nunca se libera: mata el F5/doble submit y vuelve exactamente-una-vez al generador de planes |
| **`token_verificacion`** | `str` UNIQUE idx | `uuid4().hex` inmutable. Viaja en la metadata y es el **segundo factor** al resolver el cobro (contra reciclado de ids tras restaurar respaldo) |
| `stripe_checkout_session_id` | `str \| None` UNIQUE parcial | `cs_…` |
| `stripe_payment_intent_id` | `str \| None` UNIQUE parcial | `pi_…`; su presencia = **hay instrumento emitido** |
| `stripe_charge_id` | `str \| None` idx | `ch_…`; es la llave del CSV de pagos de Stripe |
| `stripe_customer_id` | `str \| None` | semilla del recurrente y requisito de SPEI |
| `url_pago` | `str \| None` | el link; vive aquí para poder reenviarlo |
| `url_expira_at` | `datetime \| None` | `expires_at` de la sesión (**no** es el vencimiento del voucher) |
| `voucher_url` | `str \| None` | `hosted_voucher_url`; lo único reenviable si perdió la ficha |
| **`voucher_expira_at`** | `datetime \| None` | corte real del voucher OXXO / CLABE. **Este es el reloj que importa** |
| `detalle_pendiente_at` | `datetime \| None` | marca "falta traerme el detalle del PI (voucher)". Cola del barrido: el aplicador **no hace red** |
| `livemode` | `bool \| None` | tomado del evento/respuesta, **no** del prefijo de la llave en memoria |
| `discrepancia` | `bool = False` | monto distinto, pago tras cancelar, segundo pago |
| `disputado` | `bool = False` | `charge.dispute.created`; no cambia `estado` |
| `ultimo_evento_id` | `str \| None` | `evt_…` que dejó el estado actual |
| `ultimo_evento_created` | `int \| None` | epoch de Stripe; desempate dentro del mismo rango |
| `plan_id` | `int \| None` idx | bisagra del recurrente. Hoy siempre NULL |
| `origen` | `str` | `panel` \| `plan` |
| `creado_por` | `str` idx | username; `'sistema'` si lo generó un plan. **Es dueño permanente del cobro** aunque el folio se reasigne |
| `cancelado_por` | `str \| None` | |
| `enviado_at`, `pagado_at`, `cancelado_at` | `datetime \| None` | |
| `aviso_pago_enviado_at` | `datetime \| None` | el aviso al cliente sale FUERA del webhook y es reintentable |
| `reconciliado_at` | `datetime \| None` | última vez que se le preguntó a Stripe |
| `created_at` idx, `updated_at` | `datetime` | |
| `habilitado` | `bool = True` | soft delete. **No** entra en el WHERE del índice del candado: borrar no libera un instrumento cobrable |

### 1.2 `intentocobro` → MSSQL `intentos_cobro` — **append-only**

Una fila por **cada llamada** a la pasarela, escrita **antes** del POST. Es el write-ahead log de "el proceso murió y no sé si Stripe lo creó".

`id` PK · `cobro_id` idx · `numero` (1,2,3… dentro del cobro) · **`clave_idempotencia`** `str` unique+idx (`"cobro:{cobro_id}:{numero}"`, NOT NULL → basta `unique=True` en el modelo, precedente `Invitation.token`) · `operacion` (`crear_checkout`\|`expirar_sesion`\|`cancelar_pi`\|`consultar`) · `metodos_solicitados` · `msi_meses` · `monto_centavos` (copia congelada) · `moneda` · `estado` idx (`enviando`\|`exitoso`\|`rechazado`\|`error_red`\|`abandonado`) · `stripe_object` (`checkout.session`\|`payment_intent`) · `stripe_id` idx · **`stripe_request_id`** (`req_…`, lo primero que pide el soporte de Stripe) · `http_status` · `respuesta_json` (**recortado a 8 KB**) · `error_codigo` (`card_declined`, `sin_credenciales`, …) · `error_mensaje` · `reintentos` · `origen` (`panel`\|`cron`\|`webhook`) · `creado_por` · `wa_message_id` (`int \| None`, id de la fila `message` con la que se envió el link: liga dinero con conversación y permite probar entrega vía `Message.wa_status`) · `livemode` · `solicitado_at` (antes del POST) · `respondido_at` (NULL con estado `enviando` = literalmente "no sé") · `cerrado_at` · `created_at` idx · `habilitado`.

### 1.3 `eventopasarela` → MSSQL `eventos_pasarela` — **append-only**

Bitácora forense y **única fuente de verdad local**. Guarda **todo** evento con firma válida, incluidos los que no sabe aplicar.

`id` PK · **`stripe_event_id`** `str` unique+idx (`evt_…` o sintético: `panel:cancel:{cobro_id}:{iso}`, `cron:recon:{cobro_id}:{iso}`) · `tipo` idx · `api_version` · `livemode` · **`stripe_created`** `int` idx (epoch de Stripe: el reloj del orden) · `recibido_at` idx (nuestro reloj; la diferencia mide el desfase y el retraso real de OXXO) · `objeto_id` idx (`cs_`/`pi_`/`ch_`) · `monto_centavos` / `moneda` (para conciliar sin abrir el JSON) · `cobro_id` idx (NULL = huérfano, **nunca se descarta**) · `intento_id` idx · **`payload_json`** (crudo, **recortado a 32 KB**) · `procesado_at` idx (NULL = cola de trabajo del barrido) · `intentos_proceso` · `resultado` idx (`aplicado`\|`duplicado`\|`ignorado`\|`huerfano`\|`error`) · `detalle` · `habilitado`.

### 1.4 `plancobro` → `planes_cobro` — *inerte en PR1*
`id` · `phone` idx · `ticket_id \| None` idx · `service_id \| None` · `concepto` · `moneda='mxn'` · `monto_centavos` · `periodicidad` (`mensual`\|`quincenal`\|`anual`) · `dia_cobro \| None` (**1-28**, nunca 29-31) · `paymentmethod_id \| None` · `proxima_ejecucion_at \| None` idx · `ultima_ejecucion_at \| None` · `ultimo_periodo_generado \| None` (`'2026-08'`, memoria explícita) · `cobros_generados=0` · `intentos_fallidos_seguidos=0` · `estado` idx (`activo`\|`pausado`\|`cancelado`) · `fin_at \| None` · `stripe_customer_id \| None` · `stripe_subscription_id \| None` · `creado_por` · `created_at` · `habilitado`.
**La suscripción no cobra: GENERA cobros** con `plan_id` y `llave_generacion='plan:{id}:{periodo}'`.

### 1.5 `paymentmethod` → `payment_methods` — *inerte en PR1*
`id` · `phone` idx · `stripe_customer_id` idx · `stripe_payment_method_id` unique+idx · `tipo` · `marca` · `ultimos4` · `exp_mes` · `exp_anio` · `funding` (determina si aplica MSI) · `predeterminado=False` · `autorizado_por` / `autorizado_at` (consentimiento para cobrar sin presencia del cliente) · `estado` (`activo`\|`vencido`\|`revocado`) · `created_at` · `habilitado`.
**Nunca PAN, nunca CVC.** Solo el `pm_` y los cuatro últimos.

### 1.6 Máquina de estados (rangos)

```
0 borrador
1 pendiente                 (link vivo o voucher vivo)
2 cancelacion_solicitada    (le pedimos morir a Stripe y no confirmó)
3 cancelado | vencido | fallido
4 pagado
5 reembolsado               (SOLO reembolso total)
```
* El estado **solo avanza a rango mayor**. `ultimo_evento_created` desempata dentro del mismo rango.
* **Reembolso parcial NO es un estado**: `estado` sigue `pagado` y el panel pinta "Pagado · reembolso parcial $200". Estado derivado: `monto_reembolsado == monto_pagado` → `reembolsado`.
* **Disputa tampoco**: `disputado=True` + Alert.
* **Absorción ≠ transición**: `absorber_datos()` corre **siempre** que el evento sea más nuevo (escribe `pi`, `voucher_url`, `voucher_expira_at`, `monto_pagado_centavos`, `livemode`, `msi_meses_usado`…), aunque el rango no suba. Sin esto se pierden justo los tres datos que valen oro en OXXO.

### 1.7 Índices (a mano al final de `init_db()`, patrón `ux_message_wa_message_id`, `app/db.py:69-74`)

```sql
CREATE UNIQUE INDEX IF NOT EXISTS ux_cobro_vivo
  ON cobro(llave_cobro_vivo)            WHERE llave_cobro_vivo IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_cobro_generacion
  ON cobro(llave_generacion)            WHERE llave_generacion IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_cobro_payment_intent
  ON cobro(stripe_payment_intent_id)    WHERE stripe_payment_intent_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_cobro_session
  ON cobro(stripe_checkout_session_id)  WHERE stripe_checkout_session_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_intento_vivo
  ON intentocobro(cobro_id)             WHERE estado IN ('enviando','abierto');
```
`clave_idempotencia` y `stripe_event_id` son NOT NULL → `unique=True, index=True` en el modelo, sin índice a mano.

**Migraciones:** ninguna columna nueva en tablas existentes ⇒ **cero dicts nuevos** en `app/db.py` y cero entradas nuevas en `_DICTS_DE_MIGRACION`. `create_all()` crea las cinco tablas completas. Solo hay que sumar **5 entradas a `_ESQUEMA_DESPLEGADO`** (`tests/test_db_migrations.py`).

---

## 2. Archivos nuevos y responsabilidad

| Archivo | Responsabilidad | No hace |
|---|---|---|
| **`app/dinero.py`** (~90) | Fuente única de la representación del dinero: `parse_monto(txt) -> int` (Decimal, rechaza >2 decimales, ≤0, notación científica, vacío), `formato_monto(centavos, moneda) -> "$1,234.50 MXN"`, `MONEDA_DEFAULT='mxn'`, `MONTO_MIN_CENTAVOS=1000`, `MONTO_MAX_CENTAVOS` (de config), `OXXO_MAX_CENTAVOS=1_000_000`. Se registra como global de Jinja en `app/templating.py`. | Nada del dominio. Sin dependencias del proyecto |
| **`app/stripe_client.py`** (~220) | Único módulo que habla con `api.stripe.com`, con **httpx** (ya en `requirements.txt`; no se agrega el SDK `stripe`). Copia el patrón de `app/whatsapp_client.py:29-63`: `MAX_RETRIES=3`, `REQUEST_TIMEOUT_SECONDS=10`, backoff exponencial, cuerpo del error truncado a 500. **`RETRYABLE_STATUS={409,429,500,502,503,504}`** — el 409 `idempotency_key_in_use` significa "la petición original sigue en vuelo" y es reintentable por definición. Cuerpo **form-urlencoded** con corchetes (`line_items[0][price_data][unit_amount]`). Devuelve siempre `RespuestaStripe(ok, http_status, request_id, objeto, error_codigo, error_mensaje, cuerpo_recortado, indeterminado)` — con dinero, "sin credenciales", "rechazado" y "no sé" son tres cosas distintas. Funciones: `crear_checkout_session`, `expirar_sesion`, `cancelar_payment_intent`, `obtener_session`, `obtener_payment_intent`, `obtener_balance_transaction`, `buscar_sesiones_por_ventana`, `buscar_payment_intents_por_metadata`. | No conoce SQLModel ni FastAPI |
| **`app/cobranza.py`** (~380) | Dominio puro, testeable con la fixture autouse de `tests/conftest.py`. **Único lugar autorizado a mutar `cobro.estado`.** `RANGO`, `crear_cobro`, `nuevo_intento`, `aplicar_respuesta_stripe`, `absorber_datos`, `transicionar`, `aplicar_evento`, `registrar_evento_sintetico`, `resolver_cobro(evento)` (dos factores), `recomputar_estado(session, cobro_id)`, `llave_vivo(phone, service_id, ticket_id)`, `puede_cobrar(user, ticket, cobro)`, `MENSAJE_LINK` / `MENSAJE_LINK_REEMPLAZO` / `MENSAJE_PAGADO`. | Sin HTTP, sin FastAPI, sin abrir transacciones propias |
| **`app/stripe_webhook.py`** (~150) | `POST /webhooks/stripe` (el path `/webhook` ya es de Meta, GET y POST). `_verificar_firma(raw, header, secreto, tolerancia=300)`: **fail-closed** sin `STRIPE_WEBHOOK_SECRET`, `raw = await request.body()` antes de cualquier `json()`, parsea `t=`/`v1=`, `hmac.compare_digest` contra **todas** las firmas `v1` (Stripe manda varias durante la rotación). Persiste y delega. Sin ningún guard de sesión. Archivo separado del panel a propósito: su autenticación es HMAC, no cookie — el repo ya separa `app/webhook.py` de `app/dashboard.py` por la misma razón | No aplica lógica de negocio ni hace red |
| **`app/cobranza_panel.py`** (~260) | Router del panel. Todo por `Form(...)`, todo termina en `RedirectResponse(..., 303)` (patrón `update_ticket_status`, `app/dashboard.py:721-748`). Rutas: `POST /tickets/{id}/cobro`, `POST /cobros/{id}/enviar`, `POST /cobros/{id}/cancelar`, `POST /cobros/{id}/reconciliar`, `POST /cobros/{id}/avisar-pago`, `GET /cobros` (admin), `GET /export/cobros.csv` (admin), `GET /export/conciliacion.csv` (admin) | |
| **`app/cobranza_jobs.py`** (~260) | Los **cinco barridos** que hacen que nada requiera un humano (§4.5). Idempotentes, sin orden entre sí | |
| **`app/cobranza_alertas.py`** (~90) | Reglas que `app/alerts.py::current_rules` consume (la tabla `Alert` ya existe, una fila activa por `code`, se pinta en `/unmatched`) | |
| **`templates/_cobro.html`** (~90) | Parcial incluido **dos veces** en `ticket_detail.html`: bloque de estado read-only tras la línea 74 (visible para cualquiera) y el formulario dentro de la rama `{% elif ticket.assigned_to == user %}` (entre "Agregar nota", línea 120, y `<h2>Seguimiento</h2>`, línea 122) para heredar la regla de propiedad sin escribir una condición nueva. Reusa `badge tomado`/`cerrado`/`caducada`, `btn-sm`, `btn-danger` de `static/css/components.css`. **El badge siempre lleva texto además de color** | |
| **`templates/cobros.html`** (~120) | Listado/conciliación admin, totales por estado, filtro por `livemode` | |

**Tocados (sin archivo nuevo):** `app/models.py` (5 clases al final, forma de `SynonymProposal`) · `app/db.py` (los 5 índices parciales al final de `init_db()`) · `app/archive.py` (los CINCO lugares × 5 tablas, §7) · `app/system.py` (5 entradas en `MIRROR_TABLES`) · `app/main.py` (2 `include_router` + `cobranza_jobs.eventos_pendientes()` en el startup) · `app/config.py` + `.env.example` (§10) · `app/activity.py` (`ACTION_LABELS`: `cobro:generado`, `cobro:enviado`, `cobro:voucher`, `cobro:pagado`, `cobro:fallido`, `cobro:vencido`, `cobro:cancelado`, `cobro:cancelacion-pendiente`, `cobro:cierre-con-pago-pendiente` — sin esto `/activity` muestra el slug crudo) · `app/dashboard.py` (`ticket_detail` acepta `err`/`ok` y agrega `cobros`/`cobros_vivos_del_telefono` al contexto; `finish_ticket` con la confirmación de §4.6) · `app/jobs.py` (`nightly()` llama los cinco barridos, best-effort como `_evaluate_alerts_safe`) · `app/alerts.py` (`current_rules` suma las reglas de cobranza) · `docker-compose.yml` (healthcheck de `mssql` + `depends_on: condition: service_healthy`).

---

## 3. Flujo del pago único, paso a paso

Folio #42, `ticket.assigned_to == 'ana'`.

1. **GET `/tickets/42`.** `ticket_detail` agrega al contexto `cobros` (del folio), `cobros_vivos_del_telefono` (de otros folios del mismo teléfono — cierra el fallo del folio que rota) y `err`/`ok`. El bloque read-only se pinta para cualquiera.
2. **El formulario** solo se renderiza en la rama `{% elif ticket.assigned_to == user %}`. Campos: `monto` prellenado con **`formato_monto(monto_cotizado)`** — lo que el bot le dijo al cliente, no el catálogo vivo —, checkboxes `metodo` (Tarjeta / OXXO), select `msi`, `concepto` prellenado `"{{ ticket.service_name }} (folio #{{ ticket.id }})"`, y un hidden **`token_form=uuid4` generado en el render**. Si el catálogo tuviera precio y difiriera de lo cotizado, se pinta el delta explícito ("cotizado $800 · catálogo hoy $950"). `onsubmit="return confirm(...)"` con el monto formateado.
3. **POST `/tickets/42/cobro`.** Guard: `_current_user` → sin sesión, 303 a `/login`. Luego `cobranza.puede_cobrar(user, ticket, None)` = `ticket.assigned_to == user or is_admin(user)`; si no, `logger.warning` + 303 al mismo folio (no-op silencioso).
4. `dinero.parse_monto("1,250.00")` → `125000`. `ValueError` → `?err=monto`. Barandales `MONTO_MIN/MAX`; si `oxxo` está entre los métodos y el monto pasa `OXXO_MAX_CENTAVOS` → `?err=monto-oxxo` **antes** de llamar a Stripe.
5. `if not settings.stripe_secret_key:` → 303 `?err=sin-stripe`, **sin escribir nada**. Matiz: el no-op silencioso es del cliente HTTP; la UI sí le dice al asesor que no está configurado — un cobro fantasma es peor.
6. **TRANSACCIÓN 1 (corta, sin red dentro).** INSERT `cobro` (`estado='borrador'`, `llave_cobro_vivo=cobranza.llave_vivo(...)`, `llave_generacion=f'form:{token_form}'`, `token_verificacion=uuid4().hex`, `monto_centavos`, `monto_cotizado_centavos`, `monto_origen`, `concepto`, `phone`, `creado_por='ana'`, `moneda='mxn'`) + INSERT `intentocobro` (`numero=1`, `clave_idempotencia='cobro:{id}:1'`, `estado='enviando'`, `solicitado_at=utcnow()`) + `TicketActivity('cobro:generando')` + evento sintético `panel:crear:{cobro_id}`. **COMMIT.** La clave de idempotencia está en disco antes de que exista una sola llamada de red.
   * `IntegrityError` sobre `ux_cobro_vivo` → rollback, se lee el cobro vivo y 303 `?err=cobro-vivo` (mostrando el folio donde está). Nunca un 500.
   * `IntegrityError` sobre `ux_cobro_generacion` → es el F5/doble submit → 303 al cobro que ya existe, **sin error**.
7. **FUERA de toda transacción SQLite** (con WAL y `busy_timeout=5000`, sostener el escritor 10 s de HTTP tumba el panel con "database is locked"): `POST /v1/checkout/sessions`, `auth=(secret,'')`, form-urlencoded:
   `mode=payment`, `currency=mxn`, `line_items[0][price_data][unit_amount]=125000`, `…[product_data][name]=<concepto>`, `payment_method_types[0]=card`, `payment_method_types[1]=oxxo`, `payment_method_options[oxxo][expires_after_days]=3`, `payment_method_options[card][installments][enabled]=true` (MSI), `expires_at`, `success_url`/`cancel_url` a páginas estáticas propias, y **la metadata DOS veces**:
   `metadata[cobro_id]`, `metadata[intento_id]`, `metadata[token]=<token_verificacion>`, `metadata[llave_generacion]`
   **`payment_intent_data[metadata][cobro_id]`, `[intento_id]`, `[token]`** ← sin esto, el único objeto que Stripe sabe buscar por metadata queda sin marcar.
   Cabecera **`Idempotency-Key: cobro:{id}:1`**.
8. **TRANSACCIÓN 2.** Éxito → `intentocobro(estado='exitoso', http_status, stripe_request_id, stripe_id=cs_…, respondido_at, livemode)` y `cobro(estado='pendiente', stripe_checkout_session_id, stripe_payment_intent_id si vino, stripe_customer_id, url_pago, url_expira_at, livemode)`. Rechazo 4xx → intento `rechazado` con `error_codigo`, el cobro **se queda en `borrador`** (reintentable) y el asesor ve `?err=stripe&detalle=<mensaje real>`. Red/5xx agotados → intento `error_red`, cobro en `borrador`, y **queda para el barrido de huérfanos** (no sabemos si Stripe la creó; por eso fueron la clave y la metadata).
   **Invariante:** nunca queda un cobro `pendiente` sin `url_pago`.
9. **Envío por WhatsApp, best-effort.** Se lee el último `Message` IN y se evalúa `_window_open` (`app/inbox.py:56`). Abierta → `whatsapp_client.send_text(phone, MENSAJE_LINK.format(concepto, formato_monto(...), url))` + fila `Message(direction=OUT, sent_by='ana', wa_message_id=wamid)` para que el link quede en la bandeja y en el historial → `cobro.enviado_at`, `intentocobro.wa_message_id`. Cerrada → `?err=ventana`; el panel muestra la URL con botón **Copiar**. **Que el envío falle nunca cambia el estado del cobro.**
10. **Bitácora.** `_log_activity(session, 42, 'ana', 'cobro:generado')` y `'cobro:enviado'`. Si `monto_origen='override'` → `log_admin(user, 'cobro:monto-override', f'#42 {cotizado}->{monto}')`. Si el monto supera el umbral → `log_admin(user, 'cobro:monto-alto', ...)`.
11. **303 a `/tickets/42?ok=cobro`.** El bloque read-only muestra `<span class="badge tomado">Pendiente de pago</span>`, monto, métodos, link, "vence el …" y los botones Reenviar / Cancelar / Reconciliar ahora.
12. **El cliente paga con tarjeta.** Stripe manda `checkout.session.completed` con `payment_status='paid'`.
13. **POST `/webhooks/stripe`.** (a) `raw = await request.body()`; (b) firma fail-closed, tolerancia 300 s → 403 sin escribir nada; (c) **transacción cortísima**: INSERT `eventopasarela(stripe_event_id, tipo, stripe_created, api_version, livemode, objeto_id, monto_centavos, payload_json[:32768], procesado_at=NULL)`, COMMIT. `IntegrityError` → replay → **200 inmediato**. Cualquier otra excepción al insertar → **503** (Stripe reintenta).
14. **Aplicación, en su propia transacción con try/except.** `resolver_cobro` (§5C) → `absorber_datos` → `transicionar` por rango. Verificación de dinero: `amount_total` vs `monto_centavos` y `currency.casefold()` vs `moneda.casefold()` (por eso la columna guarda `'mxn'`: comparar `'MXN'` contra lo que manda Stripe encendería la Alert en el **100%** de los pagos legítimos). Difieren → se aplica igual (el dinero real manda), se escribe `monto_pagado_centavos`, `estado_motivo="monto distinto: esperado X, cobrado Y"`, `discrepancia=True` y Alert. Se escriben `pagado_at`, `metodo_usado`, `msi_meses_usado`, `stripe_charge_id`, `ultimo_evento_*`, y **`llave_cobro_vivo=None`** (el instrumento se consumió). `procesado_at`, `resultado='aplicado'`. Si revienta: `procesado_at` sigue NULL, `intentos_proceso+=1`, `error=…` y **aun así 200** — el payload ya es nuestro y el barrido lo reintenta; depender del reintento de Stripe (3 días y se acabó) es peor.
15. **Efectos secundarios FUERA del webhook** (barrido o `BackgroundTask`): `TicketActivity('cobro:pagado')` con usuario `'sistema'` (precedente: `log_admin('sistema', …)` en `jobs.py`); aviso al cliente si la ventana está abierta → `aviso_pago_enviado_at`; si el folio ya estaba cerrado, Alert `cobro:pago-tras-cierre` + `FollowUp(due_at=ahora)` para `creado_por`.
16. **Espejo MSSQL** por listener dentro del flush, todo vía `_safe`: MSSQL caído nunca hace que Stripe reciba un 5xx (§7.6).
17. **Conciliación (cron, no en el request path):** `obtener_balance_transaction(charge)` llena `comision_centavos`, `neto_centavos`, `moneda_liquidacion`, `balance_transaction_id`. `/export/conciliacion.csv` cruza por `stripe_charge_id`; la suma de `neto_centavos` de un periodo cuadra con el payout **sumando enteros**.

**CANCELAR** (`POST /cobros/{id}/cancelar`):
1. Permiso (§5D).
2. Si hay `stripe_payment_intent_id` → **`POST /v1/payment_intents/{pi}/cancel`** (esto, y no `/expire`, es lo que mata un voucher OXXO en `requires_action`). Si no lo hay → `POST /v1/checkout/sessions/{cs}/expire`.
3. **Solo si Stripe confirma** (`pi.status=='canceled'` o `session.status=='expired'`) se escribe el evento sintético `panel:cancel:{id}:{iso}` → rango 3 `cancelado`, `cancelado_por`, y **`llave_cobro_vivo=None`**.
4. **Si Stripe rechaza o no responde**: `estado='cancelacion_solicitada'` (rango 2), **el candado NO se libera**, `estado_motivo="no se pudo cancelar el voucher: sigue cobrable hasta el <fecha>"`, y el barrido reintenta cada noche. El panel muestra la fecha exacta en que se liberará solo.
5. Al generar el reemplazo, el mensaje al cliente es `MENSAJE_LINK_REEMPLAZO`: **"Este link reemplaza al anterior; el anterior ya no sirve."**

---

## 4. OXXO / SPEI y qué pasa si el folio ya se cerró

**Principio:** con pago diferido no hay ruta síncrona. El link se genera hoy, el cliente paga en horas o días, y la única fuente de verdad es el webhook (o nuestra reconciliación contra Stripe). Nada en el panel consulta "¿ya pagó?" en el request path.

### 4.1 El ciclo
1. Sesión creada con `payment_method_types[]=oxxo` y `payment_method_options[oxxo][expires_after_days]=3`. El cliente recibe el mismo `<a href>` (la CSP no se toca: no hay `js.stripe.com`, ni Elements, ni iframes — ver §4.7).
2. **Minutos después llega `checkout.session.completed` con `payment_status='unpaid'`. ESTE EVENTO NO ES UN PAGO.** Confundirlo es *el* bug clásico de OXXO. `aplicar_evento`: el estado se queda en `pendiente` (rango 1, no sube) pero **`absorber_datos` sí corre** y escribe `stripe_payment_intent_id`, `metodo_usado='oxxo'` y `detalle_pendiente_at=utcnow()`. `TicketActivity('cobro:voucher', 'sistema')`.
3. El barrido `completar_voucher()` hace `GET /v1/payment_intents/{pi}` (fuera del webhook: **el aplicador no hace red**) y escribe `voucher_url` (`next_action.oxxo_display_details.hosted_voucher_url`) y **`voucher_expira_at`** (`…expires_after`), limpiando `detalle_pendiente_at`. Badge: "Esperando pago en OXXO — vence el 5 ago".
4. **Confirmación (1 h a 3 días):** `checkout.session.async_payment_succeeded` o `payment_intent.succeeded` → exactamente la misma ruta que la tarjeta. `recibido_at - stripe_created` deja medido el retraso real.
5. **Fallo:** `async_payment_failed` / `payment_intent.payment_failed` → `fallido` (rango 3), `llave_cobro_vivo=None`. El cobro sigue vivo comercialmente: el asesor genera uno nuevo sin perder el historial.
6. **`checkout.session.expired`:** se **IGNORA** si el cobro ya tiene `stripe_payment_intent_id` (voucher emitido y todavía pagable) → `resultado='ignorado'`, `detalle='sesión expirada con voucher vivo'`. Si no lo tiene, sí es palabra de Stripe → `vencido`.
7. **Vencimiento del voucher:** lo escribe **únicamente** Stripe o una reconciliación contra Stripe. El panel puede pintar "venció el 2 de agosto (sin confirmar)" derivando de `voucher_expira_at`, pero la columna `estado` no miente. Un pago que entra en el último minuto no debe encontrarse la fila ya marcada como muerta.
8. **Pago tardío:** un `*_succeeded` sobre un cobro en `vencido`/`fallido`/`cancelado` (rango 3) o `cancelacion_solicitada` (2) **gana**: pasa a `pagado`, `discrepancia=True`, `estado_motivo='pago tardío aceptado'` y Alert. Nunca se rechaza un pago verdadero.
9. **Pago parcial SPEI** (PR1.5): `payment_intent.partially_funded` → estado `pendiente`, `monto_pagado_centavos` parcial, `estado_motivo="transferencia parcial: recibidos X de Y"`, Alert. Nada se marca pagado sin un `succeeded`. **Sobrepago:** el excedente queda en el customer balance de Stripe; se registra en `estado_motivo` + Alert, jamás se inventa un reembolso.

### 4.2 Si el folio ya se cerró
El cobro **no depende del ticket** (`ticket_id` es nullable) y el webhook **nunca** rechaza un pago por el estado del folio. El dinero entró: es un hecho que se registra. Se marca `pagado`, se escribe `TicketActivity('cobro:pagado','sistema')`, se levanta `Alert('cobro:pago-tras-cierre')` y se crea un `FollowUp(due_at=ahora, username=cobro.creado_por)`. **El folio no se reabre solo** — eso lo decide un humano, con el aviso enfrente.

### 4.3 Prevención: no cerrar a ciegas con cobro vivo
`finish_ticket` (`app/dashboard.py:504`) hoy no consulta nada de cobranza y dispara `_notify_finish` con `"✅ …quedó terminada. ¡Gracias por tu confianza!"`, que es literalmente el argumento del cliente para no pagar la ficha. Cambio: si existe un cobro vivo del teléfono, **no se bloquea el cierre** (a veces es legítimo) pero se exige `confirm=1` en el form, se escribe `TicketActivity('cobro:cierre-con-pago-pendiente')` y **el texto al cliente cambia** a `FINISH_NOTICE_CON_COBRO`: *"Tu trámite quedó listo. Recuerda que tu ficha de pago vence el {fecha}."*

### 4.4 Aviso al cliente cuando paga
A los 3 días la ventana de 24 h casi siempre está cerrada, así que el aviso sale **fuera del webhook**, es best-effort y **reintentable** (`aviso_pago_enviado_at`). No se inventa una plantilla HSM (no hay ninguna aprobada). El panel marca "Pagado · sin avisar al cliente" y ofrece el botón de un clic. **Que el aviso no salga nunca afecta el estado del cobro ni el 200 a Stripe.**

### 4.5 Los cinco barridos (`app/cobranza_jobs.py`) — la red de seguridad
Todos idempotentes, sin orden entre sí, invocados por `nightly()` con el mismo patrón best-effort de `_evaluate_alerts_safe`, y `eventos_pendientes()` **también en el startup de `app/main.py` y al abrir `GET /cobros`** (el cron vive en el host y puede no estar configurado en el VPS).

1. **`eventos_pendientes()`** — `eventopasarela` con `procesado_at IS NULL`, por `(stripe_created, id)`.
2. **`intentos_huerfanos()`** — `intentocobro` en `enviando` con `solicitado_at` > 2 min.
   * **< 24 h**: reintenta el MISMO POST con la MISMA `clave_idempotencia` → Stripe devuelve el objeto original.
   * **> 24 h**: la clave ya caducó y reusarla **crearía un objeto nuevo**. NO se reintenta el POST. Se busca: `GET /v1/checkout/sessions?created[gte]=solicitado_at-60&created[lte]=solicitado_at+300&limit=100` filtrando `metadata[token]` **del lado nuestro** (determinista a este volumen), y como respaldo `GET /v1/payment_intents/search?query=metadata['cobro_id']:'77'`.
   * **Resultado vacío no autoriza crear** si el intento tiene <5 min (la Search API es eventualmente consistente, hasta ~1 min). Tras **3 barridos** sin resolver: Alert `cobro:intento-irresoluble` y se deja para un humano. **Crear a ciegas es exactamente el cobro duplicado.**
3. **`completar_voucher()`** — cobros con `detalle_pendiente_at` no nulo → `GET` del PI → `voucher_url` + `voucher_expira_at`.
4. **`reconciliar_pendientes()`** — cobros en `pendiente`/`cancelacion_solicitada` con >2 h sin evento, o con `voucher_expira_at` pasado: `GET` a Stripe y el resultado entra como **fila sintética** `cron:recon:{cobro_id}:{iso}` que pasa por el MISMO aplicador. Reintenta también la cancelación pendiente. Botón manual "Reconciliar ahora" por cobro. Con esto, "el webhook nunca llegó" se degrada de pérdida de dinero a un retraso de una noche, **sin que nadie toque la base**.
5. **`conciliar_montos()`** — cobros `pagado` sin `neto_centavos`: `GET /v1/balance_transactions`.

### 4.6 Alertas (`app/cobranza_alertas.py`, tabla `Alert` ya existente)
`cobro:sin-eventos` (hay pendientes y **cero** filas nuevas en `eventopasarela` en 24 h = el webhook está roto o rotaron el secreto) · `cobro:eventos-sin-procesar` · `cobro:intentos-huerfanos` · `cobro:intento-irresoluble` · **`cobro:desfase-reloj`** (`|recibido_at − stripe_created| > 120 s`: si el VPS se desfasa, la firma rechaza **todo** con 403 y el sistema se ve sano) · `cobro:doble-pago` · `cobro:monto-distinto` · `cobro:pago-tras-cierre` · `cobro:cancelacion-pendiente` · `cobro:modo-no-coincide` · `cobro:disputa` · `cobro:confirmado-sin-stripe` · `cobro:evento-huerfano` · **`cobro:monto-sobre-cotizado`** (el link pide más de lo que el bot le cotizó al cliente por WhatsApp; `landing/terminos.html` §2 promete la cotización por adelantado, así que el override hacia arriba es la única desviación de dinero que el cliente puede reclamar por escrito).

### 4.7 El voucher y la CSP
`app/main.py:53-67` fija `default-src 'self'; img-src 'self' data:` sin `frame-src`. Eso mata cualquier cosa embebida de Stripe. Este diseño **no toca la CSP**: el voucher se muestra como `<a href>` (la navegación por enlace no está restringida). Si algún día hay que embeberlo, la salida es proxearlo por el servidor con el precedente de `GET /media/{name}` (`app/inbox.py:343`) — **jamás** aflojar `default-src`, que rompe `tests/test_security.py:22-26`.

---

## 5. Idempotencia, con nombres de campo

### A) SALIDA — no cobrar dos veces
| Mecanismo | Campo / índice | Qué fallo corta |
|---|---|---|
| Un instrumento cobrable por cliente-trámite | `cobro.llave_cobro_vivo` + `ux_cobro_vivo` | Doble clic, dos asesores a la vez, folio que rota (el bot fabrica folio nuevo con el voucher vivo), y **cancelar sin confirmación de Stripe** (no libera) |
| Doble submit del navegador (F5 / botón atrás) | `cobro.llave_generacion` + `ux_cobro_generacion` | Reenvío del formulario aunque el cobro ya haya terminado |
| Exactamente-una-vez del generador de planes | `llave_generacion='plan:{id}:{periodo}'` | Cron que corre dos veces o muere antes de avanzar `proxima_ejecucion_at` |
| Reintento HTTP dentro de 24 h | `intentocobro.clave_idempotencia` → cabecera `Idempotency-Key` | Timeout/502: Stripe devuelve la MISMA sesión. **La clave se escribe y COMMITEA antes de la primera llamada de red** y nunca se regenera para el mismo intento |
| Reintento >24 h | metadata (`cobro_id`, `intento_id`, `token`) **a nivel sesión Y en `payment_intent_data[metadata]`** | La clave ya caducó; la metadata es la llave de recuperación (§4.5.2) |
| Dos instrumentos vivos para el mismo cobro | `ux_intento_vivo` | Voucher OXXO pendiente + link de tarjeta nuevo = dos formas simultáneas de pagar lo mismo |
| Un objeto de Stripe reclamado por dos cobros | `ux_cobro_payment_intent`, `ux_cobro_session` | |
| `409 idempotency_key_in_use` | en `RETRYABLE_STATUS` | Significa "la original sigue en vuelo": se reintenta con backoff y **no** se marca el cobro `fallido` |

### B) ENTRADA — no procesar dos veces
* **`eventopasarela.stripe_event_id`** `unique=True, index=True`. Se **INSERTA antes de aplicar nada**: el `IntegrityError` ES la detección (no hay ventana de carrera como con SELECT-then-INSERT). Mismo patrón que `ux_message_wa_message_id` contra los webhooks duplicados de Meta. Stripe reentrega 3 días.
* **Solo se persisten eventos con firma válida.** Los rechazos van al log con contador, no a la tabla: si no, cualquiera desde internet la llena.
* **Tolerancia temporal de 300 s** sobre la `t=` de `Stripe-Signature`, contra replay de un cuerpo viejo con su firma vieja.
* **Ramificación por tipo de excepción**: `IntegrityError` → 200; cualquier otra (incluido `OperationalError: database is locked`) → **503**.

### C) APLICACIÓN — no aplicar dos veces el mismo efecto
* **Resolución de dos factores.** `resolver_cobro(evento)` prueba en orden `metadata[cobro_id]` → `stripe_payment_intent_id` → `stripe_checkout_session_id` → `stripe_charge_id`, y **acepta la fila solo si `cobro.token_verificacion == metadata[token]`**. Cierra el reciclado de ids tras restaurar un respaldo (el evento viejo con `cobro_id=137` no toca al 137 nuevo).
* **Nunca consultar con `None`.** Verificado en el venv de este repo: `where(Cobro.stripe_session_id == None)` compila a `IS NULL` y `.first()` engancha la fila equivocada. Cada eslabón lleva `if not valor: continue` **y** `.where(Col.is_not(None))` como red.
* Sin resolución → `resultado='huerfano'`, `cobro_id=NULL`, Alert, **200**. El evento no se pierde: es reprocesable desde el ledger.
* **Monotonía por rango** (§1.6) + `ultimo_evento_created` para desempatar. Un `expired` que llega después de un `succeeded` no desmarca el pago; un `succeeded` que llega antes del `completed` gana y el segundo no lo degrada.
* **Segundo pago real ≠ replay.** Si el estado ya es `pagado` y el `payment_intent` del evento **coincide** → replay → no-op. Si **difiere** → es un segundo cobro real: no se pisa nada, `resultado='ignorado'`, `detalle='segundo pago sobre cobro pagado'`, `discrepancia=True`, **Alert `cobro:doble-pago`**. Nunca se calla.
* **`livemode`.** Si `settings.stripe_secret_key` empieza con `sk_live` y el evento trae `livemode=false` (o al revés) → se persiste con `resultado='ignorado'`, `detalle='modo no coincide'`, Alert, y **no se mueve ni un estado**. Todo listado, total y export filtra por `livemode` verdadero por defecto.
* **`recomputar_estado(session, cobro_id)`** reconstruye `estado`, los campos absorbidos **y `llave_cobro_vivo`** desde el log crudo, en una sola transacción. Si al restaurar el candado choca con `ux_cobro_vivo`, se deja el cobro terminal y se levanta Alert: **nunca duplicar**. Único camino de escritura de `estado`: `app/cobranza.transicionar`.

### D) Autorización de las rutas de cobranza
`cobranza.puede_cobrar(user, ticket, cobro)` = `ticket.assigned_to == user` **or** `user == cobro.creado_por` **or** `is_admin(user)`. Cobrar es acto de **asesor**, no de admin, pero un cobro de 3 días sobrevive a `release_ticket` (`app/dashboard.py:463` deja `assigned_to=None`) y sin la ampliación **nadie** podría cancelar ni reenviar el voucher.
**Nota metodológica honesta:** el predicado vive en `app/cobranza.py`, así que la fuente del *endpoint* no contiene `is_admin(` y `test_la_tabla_cubre_toda_ruta_con_guard_de_admin` (`tests/test_authz.py:478`) no lo detecta. Eso **no** es una evasión: se compensa con (a) filas reales en `RUTAS` para las tres rutas que sí son puerta de admin (`GET /cobros`, los dos CSV) y (b) un archivo nuevo `tests/test_authz_cobranza.py` que cubre cada ruta × cada rol con sonda de estado. Si se prefiere no tocar `fuera_de_tabla`, la alternativa es meter `is_admin(` en los endpoints y escribir filas en `RUTAS` cuyo control positivo sea "el asignado SÍ puede" — semántica opuesta a la del resto de la tabla. Elegimos el archivo aparte.

---

## 6. Representación del dinero (decidido)

* **Entero en centavos, siempre.** `monto_centavos: int` → `INTEGER` en SQLite, `BIGINT` en MSSQL. **Nunca float, nunca Decimal**: verificado en este entorno, un `Decimal(max_digits=12, decimal_places=2)` genera `NUMERIC(12,2)` en el DDL pero SQLite lo guarda físicamente como `REAL` (`typeof='real'`). Además mapea 1:1 al `unit_amount` de Stripe: cero oportunidad de perder un centavo redondeando.
* **Moneda:** columna `moneda: str = 'mxn'`, **en minúsculas**, como la exige Stripe y como la devuelve. Es dato de la fila, no constante hardcodeada en el cliente HTTP. Toda comparación con `.casefold()`. El sufijo visible "MXN" lo agrega la vista.
* **Dos conversiones en todo el sistema, ambas en `app/dinero.py`:**
  * `parse_monto(texto) -> int`: quita `$`, espacios y comas; parsea con `decimal.Decimal` (**nunca** `int(float(txt)*100)`: 19.99 → 1998); exige ≤2 decimales; `int(d*100)` sobre un Decimal cuantizado. Rechaza vacío, negativo, cero, `'1250.005'`, `'abc'`, notación científica.
  * `formato_monto(centavos, moneda) -> "$1,250.50 MXN"`. **Única** función que formatea dinero: la usan la plantilla, el mensaje de WhatsApp y la línea de actividad, así que el cliente, el asesor y la bitácora ven el mismo string.
* **Redondeo: no existe.** Más de 2 decimales es un error de captura, no algo que redondear.
* **Prohibido reconstruir un monto parseando un texto ya formateado.** Excepción declarada y acotada: `monto_cotizado_centavos` se obtiene aplicando `parse_monto` a `ticket.price_info`, que es **texto humano en la frontera del sistema** (no salida de nuestro formateador) y devuelve NULL ante cualquier ambigüedad ("A cotizar" → NULL).
* **Tres números distintos, nunca confundidos:** `monto_centavos` (pedido) · `monto_pagado_centavos` (cobrado) · `neto_centavos` (liquidado). Un cobro que se "corrige" solo es un cobro que no se puede auditar.
* **Reembolsos:** `monto_reembolsado_centavos` acumulado desde `amount_refunded` del charge (no sumando eventos, que se reentregan). `neto = neto_del_cargo − reembolsado` — **en México Stripe no devuelve la comisión al reembolsar**, así que se resta, nunca se relee el neto original.
* **MSI:** no altera `monto_centavos`; el plazo lo **elige el cliente** en el checkout, por eso `msi_meses_ofrecidos` ≠ `msi_meses_usado`. **Prohibido calcular la comisión con una tarifa hardcodeada:** con MSI no es plana. `comision_centavos` y `neto_centavos` se **leen** de la balance transaction.
* **Barandales** (constantes con comentario de restricción): `MONTO_MIN_CENTAVOS = 1000` ($10, abajo Stripe MX rechaza y OXXO no emite voucher) · `MONTO_MAX_CENTAVOS` (env, default `10_000_000` = $100,000; el asesor teclea el monto a mano y un cero de más es el riesgo operativo #1) · `OXXO_MAX_CENTAVOS = 1_000_000` (~$10,000 por voucher; se rechaza antes de llamar a Stripe con `?err=monto-oxxo`).
* **CSV: nunca dinero formateado.** Los exports llevan `monto_centavos`, `monto_pagado_centavos`, `neto_centavos` como **enteros crudos** y `moneda` en su propia columna. `'$1,234.56 MXN'` entra a Excel es-MX como texto, `SUMA()` devuelve 0, y el separador de millares por coma dentro de un CSV separado por comas depende de que el quoting nunca se rompa. `formato_monto` queda restringido a plantillas y al mensaje de WhatsApp.
* **Catálogo:** en PR1 **no** se agrega `precio_centavos` a `catalogservice`. El precio ya vive en `price_info` como texto humano (51 de 58 con número desde el catálogo de producción, 7 en "A cotizar"), y duplicarlo en una columna entera cuesta ~12 puntos de propagación sin comprar nada que `parse_monto` no dé hoy. Cuando el dueño capture los montos, `cobro.monto_centavos` no cambia: solo cambia el valor por defecto del input. **Riesgo que hay que resolver ANTES de ese PR2:** convivir `price_info='$800'` (lo que el cliente lee en `_confirm_message`) con `precio_centavos=75000` sin regla que los ligue = el cliente ve $800 y Stripe le cobra $750. Este diseño ya deja el mecanismo puesto: la línea base del override es **lo cotizado** (`monto_cotizado_centavos`), no el catálogo vivo.

---

## 7. Los cinco lugares del espejo MSSQL, por tabla

`tests/test_mirror_consistency.py` es **DDL-driven**: si se declaran los modelos y se olvida `_SCHEMA_DDL` por completo, `TABLAS_DDL` no cambia y **ninguna** aserción de ese archivo falla. **Orden de trabajo obligatorio: primero el DDL, tabla por tabla, corriendo el test después de cada una.**

| SQLite | MSSQL | 1. `_SCHEMA_DDL` (archive.py:28) | 2. upsert | 3. listener | 4. `backfill()` | 5. `MIRROR_TABLES` (system.py) |
|---|---|---|---|---|---|---|
| `cobro` | `cobros` | bloque `IF OBJECT_ID('cobros','U') IS NULL CREATE TABLE cobros (…)` | `archive_cobro(c)` → `_upsert("cobros","source_id",c.id,{…})` | `@event.listens_for(Cobro,"after_insert"/"after_update")` | `for c in session.exec(select(Cobro)).all(): archive_cobro(c)` | `(Cobro, "cobros")` |
| `intentocobro` | `intentos_cobro` | ídem | `archive_intento_cobro(i)` | ídem `IntentoCobro` | ídem | `(IntentoCobro, "intentos_cobro")` |
| `eventopasarela` | `eventos_pasarela` | ídem | `archive_evento_pasarela(e)` | ídem `EventoPasarela` | ídem | `(EventoPasarela, "eventos_pasarela")` |
| `plancobro` | `planes_cobro` | ídem | `archive_plan_cobro(p)` | ídem `PlanCobro` | ídem | `(PlanCobro, "planes_cobro")` |
| `paymentmethod` | `payment_methods` | ídem | `archive_payment_method(p)` | ídem `PaymentMethod` | ídem | `(PaymentMethod, "payment_methods")` |

### 7.1 Convenciones del DDL
* Clave del espejo: `source_id BIGINT NOT NULL PRIMARY KEY` (patrón `tickets`, `followups`), porque `_upsert` es UPDATE-luego-INSERT idempotente y así el backfill no duplica.
* **Sin FKs** en estas cinco tablas (mismo criterio ya escrito en `app/archive.py:139`: es un archivo de solo lectura y las FKs complicarían el orden del backfill).
* Dinero: `BIGINT NULL/NOT NULL`. Texto largo: `NVARCHAR(MAX)` (`payload_json`, `respuesta_json`, `estado_motivo`). Ids de Stripe: `NVARCHAR(128)`. Fechas: `DATETIME2` pasadas por `_naive_utc`.
* `habilitado BIT NOT NULL DEFAULT 1` en las cinco.
* El `CREATE TABLE` original queda **congelado**: columnas futuras van en `_COLUMNS_ADDED_AFTER_LAUNCH` (archive.py:317).

### 7.2 Dos puntos ciegos léxicos que hay que respetar al escribir el código
1. `_tablas_en_literales` recolecta identificadores dentro de **cualquier** literal de cadena. **No escribas la cadena `'cobros'` dentro de `archive_intento_cobro` ni de `archive_evento_pasarela`** (ni en un comentario SQL, ni en una FK): el test creería que esa función ya escribe `cobros` y taparía un lugar 2 faltante. Nombres como `cobro_id` o `cobros_generados` son un solo token y son seguros.
2. `backfill()` se excluye a propósito del chequeo del lugar 2; el lugar 4 se verifica aparte. Hay que agregar los 5 `import` y los 5 bucles.

### 7.3 Candados de prueba que se pondrán rojos y son parte del entregable
`_ESQUEMA_DESPLEGADO` (`tests/test_db_migrations.py`): 5 entradas nuevas · `ARCHIVE_DDL` (`tests/test_archive.py:20`): 5 `CREATE TABLE` en dialecto SQLite · `MIRROR_TABLES`: 5 filas · `RUTAS` (`tests/test_authz.py`): 3 filas (las rutas admin) · `ACTION_LABELS`: 9 claves.

### 7.4 El espejo dentro del request del webhook
`_safe` (archive.py:782) hace `if not _ready: init()` en **cada** escritura, e `init()` abre conexión a `master`, hace `CREATE DATABASE` y recorre todo `_SCHEMA_DDL`. Con `docker-compose.yml` levantando `mssql` sin healthcheck y SQL Server 2022 Express tardando decenas de segundos en aceptar logins tras un reinicio del VPS, un `async_payment_succeeded` que llegue en esa ventana dispara **tres** `init()` completos contra un MSSQL que no responde, **sin timeout**. Stripe corta a los 30 s, reintenta, y el reintento choca con el lock de escritura de SQLite. Cuatro arreglos, todos en este PR:
1. `login_timeout=5&timeout=10` explícitos en la URL de `pymssql` (`_get_engine`, archive.py:341).
2. `_safe` no reintenta `init()` en cada escritura: `_PROXIMO_INIT` (marca de tiempo, mínimo 60 s entre intentos); mientras tanto se salta el espejo. El backfill del arranque repara lo que falte.
3. `healthcheck` de `mssql` en `docker-compose.yml` + `depends_on: condition: service_healthy`.
4. Se mantiene el espejo síncrono (regla del proyecto: se espeja toda la data) porque `_safe` degrada cualquier excepción a `logger.warning` y MSSQL caído nunca hace que el endpoint devuelva 5xx. La salida si el volumen crece ya está anotada en `_register_listeners`.

### 7.5 Privacidad
`payload_json` puede traer correo y nombre del cliente y se espeja como `NVARCHAR(MAX)`. Es consistente con la decisión de espejar toda la data, pero queda escrito: **`eventos_pasarela` nunca se expone completa en el panel** y **`paymentmethod` jamás guarda PAN ni CVC**.

---

## 8. Qué se implementa AHORA y qué queda para después

### AHORA (PR1 — pago único)
* Las **5 tablas** con sus 5 índices parciales y los 5 lugares del espejo. `plancobro` y `paymentmethod` **inertes**: DDL + espejo + snapshot, **cero rutas y cero escrituras**.
* **Tarjeta + MSI + OXXO.** Generar link desde el folio, enviarlo por WhatsApp (con su fila `Message`), reenviar, cancelar (con confirmación de Stripe), reconciliar a mano.
* Webhook con firma fail-closed, ledger append-only, aplicador con monotonía por rango + absorción, resolución de dos factores.
* Los **cinco barridos** + `eventos_pendientes()` también en el startup.
* Las **11 alertas** de cobranza sobre la tabla `Alert` existente.
* `GET /cobros` + `/export/cobros.csv` + `/export/conciliacion.csv` (admin), con `comision`/`neto` leídos de la balance transaction.
* Aviso de pago al cliente best-effort y reintentable, fuera del webhook.
* Cambio en `finish_ticket` (confirmación + texto distinto con cobro vivo).
* Endurecimiento del espejo (§7.4).

### DESPUÉS
| Cuándo | Qué | Costo estimado |
|---|---|---|
| **PR1.5** | **SPEI** (`customer_balance` + `bank_transfer[type]=mx_bank_transfer` + Customer + `partially_funded`) | ~50 líneas; el esquema ya lo soporta |
| **PR2** | `catalogservice.precio_centavos` (nullable) + prellenado del formulario. **Requiere decidir antes cuál es la fuente de verdad frente a `price_info`** | ~12 puntos de propagación |
| **PR3** | **Recurrente**: activar `plancobro` (generador en el cron, `llave_generacion='plan:{id}:{periodo}'`) y `paymentmethod` (Checkout con `setup_future_usage`, cargos off-session, dunning con `intentos_fallidos_seguidos`). **Sin tocar el esquema.** El webhook, el panel, el espejo y el flujo de OXXO no cambian una línea | lógica pura |
| **PR4** | Reembolsos y disputas **operables** desde el panel. En PR1 ya se **registran** (`monto_reembolsado_centavos`, `disputado`, estado derivado, Alert), pero no hay botón para reembolsar | |
| **Fuera de alcance, decisión del dueño** | **CFDI / conciliación fiscal.** Stripe cobra, no factura en México. Este módulo termina en "el cliente pagó"; la factura es un proceso humano aparte. Para una gestoría mexicana **esta pregunta sale en la primera semana** | |

---

## 9. Pruebas a escribir (cada una con la entrada exacta que reproduce el fallo)

**Dinero — `tests/test_dinero.py`**
1. `parse_monto("1,250.00") == 125000`; `parse_monto("$1250") == 125000`; `parse_monto("19.99") == 1999` — *previene `int(float("19.99")*100) == 1998`.*
2. `parse_monto` lanza `ValueError` con: `""`, `"0"`, `"-5"`, `"1250.005"`, `"abc"`, `"1e3"`, `"1.234,56"`.
3. Insertar `Cobro(monto_centavos=125000)`, releer y afirmar `typeof(monto_centavos)=='integer'` vía SQL crudo — *previene el Decimal→REAL.*
4. `formato_monto(125050,'mxn') == "$1,250.50 MXN"`; el CSV de `/export/cobros.csv` contiene la celda `125050` y **no** contiene `"$1,250.50"`.

**Esquema y migraciones — `tests/test_db_migrations.py`**
5. Base **vieja con datos** + `init_db()`: las 5 tablas existen completas y ninguna fila previa se perdió.
6. `PRAGMA index_list('cobro')` → `ux_cobro_vivo` con **`row[2] == 1`** (bandera de unicidad, como el test del índice de Meta en las líneas 549-572), y el segundo INSERT con la misma `llave_cobro_vivo` lanza `IntegrityError`.
7. Lo mismo para `ux_cobro_generacion`, `ux_cobro_payment_intent`, `ux_cobro_session`, `ux_intento_vivo`.
8. `init_db()` dos veces seguidas no revienta (idempotencia).

**Idempotencia de salida — `tests/test_cobranza_idempotencia.py`**
9. Dos POST a `/tickets/42/cobro` con el **mismo `token_form`** → 1 sola fila `cobro`, 303 al cobro existente, **sin `?err`**, y `stripe_client.crear_checkout_session` llamado **una vez** (doble); *previene el F5.*
10. Dos POST con **tokens distintos** para el mismo `(phone, service_id)` → `IntegrityError` traducido a `303 ?err=cobro-vivo`, **nunca 500**, y cero llamadas a Stripe la segunda vez.
11. Folio #42 **cerrado** con voucher vivo + folio #57 nuevo del **mismo `service_id`** y mismo teléfono → el segundo cobro es rechazado por `ux_cobro_vivo`; *previene el folio que rota.* Y con **distinto `service_id`** sí se permite.
12. `crear_cobro` con `stripe_client` que lanza al ser llamado: la fila `cobro` **y** el `intentocobro` con su `clave_idempotencia` ya están COMMITEADOS antes de la excepción.
13. Doble `crear_checkout_session` con la misma clave: el doble de httpx afirma que la cabecera `Idempotency-Key` es **idéntica** en ambas llamadas.
14. Respuesta `409 idempotency_key_in_use` → se reintenta con backoff y el cobro **no** queda `fallido`.

**Idempotencia de entrada — `tests/test_stripe_webhook.py`**
15. `_verificar_firma` con `STRIPE_WEBHOOK_SECRET=""` → `False` (fail-closed), espejo exacto de `test_missing_app_secret_rejects_everything` (`tests/test_webhook.py:30-34`).
16. Firma con `t = now-400` → 403 (tolerancia 300 s). Cabecera con **dos** `v1=`, la segunda válida → aceptada (rotación de secreto).
17. Mismo `evt_1` dos veces → 1 fila en `eventopasarela`, 200 ambas, y `aplicar_evento` invocado **una** vez.
18. El INSERT del evento lanza `OperationalError("database is locked")` (monkeypatch) → respuesta **503**, no 200; *previene el evento perdido.*
19. Cuerpo válido pero `aplicar_evento` lanza → **200**, `procesado_at IS NULL`, `intentos_proceso==1`; luego `eventos_pendientes()` lo aplica.

**Resolución y máquina de estados — `tests/test_cobranza_aplicar.py`**
20. Dos cobros **sin** `stripe_payment_intent_id` en la base; evento sin `payment_intent` → `resolver_cobro` devuelve `None` (no la fila más vieja) y `resultado='huerfano'`; *previene el `== None` → `IS NULL`.*
21. Evento con `metadata[cobro_id]=137` y `metadata[token]='xxx'` sobre un cobro 137 cuyo `token_verificacion='yyy'` → no aplica, `detalle='metadata no coincide'`, Alert; *previene el id reciclado tras restaurar respaldo.*
22. `checkout.session.completed` con **`payment_status='unpaid'`** → `estado` sigue `pendiente`, `stripe_payment_intent_id` **sí** se escribe, `detalle_pendiente_at` no nulo; *previene dar por cobrado lo que nadie pagó **y** perder los datos del voucher por la monotonía.*
23. Cobro en `pendiente` con `pi` presente + `checkout.session.expired` → `resultado='ignorado'`, sigue `pendiente`.
24. Cobro en `vencido` + `async_payment_succeeded` → **`pagado`**, `discrepancia=True`, `estado_motivo='pago tardío aceptado'`; y lo mismo desde `cancelado`.
25. `expired` (stripe_created=100) **después** de `succeeded` (stripe_created=90), aplicados en ese orden → el cobro queda `pagado`.
26. Cobro `pagado` con `pi_A`; llega otro `succeeded` con `pi_B` → nada se pisa, `resultado='ignorado'`, Alert `cobro:doble-pago`; *previene el doble cargo invisible.*
27. Evento con `currency='mxn'` sobre cobro con `moneda='mxn'` → **no** escribe `estado_motivo` ni levanta Alert; *previene la alerta de moneda encendida en el 100% de los pagos.*
28. `amount_total=120000` sobre `monto_centavos=125000` → se aplica, `monto_pagado_centavos=120000`, `monto_centavos` **intacto**, `discrepancia=True`, Alert.
29. `sk_live` + evento `livemode=false` → no aplica, Alert `cobro:modo-no-coincide`.
30. `charge.refunded` con `amount_refunded=20000` sobre `monto_pagado=200000` → `monto_reembolsado_centavos=20000`, `estado` sigue `pagado`; con `amount_refunded=200000` → `reembolsado`. Reentrega del mismo evento no duplica el acumulado.
31. **Replay completo**: crear cobro → `completed(paid)` → cancelar desde el panel → `recomputar_estado()` devuelve exactamente lo que está en la fila **y** `llave_cobro_vivo` sigue en NULL; *previene la resurrección del cancelado.*

**Cancelación y candado — `tests/test_cobranza_cancelar.py`**
32. Cobro con `pi` en `requires_action`; `cancelar_payment_intent` devuelve error → `estado='cancelacion_solicitada'`, **`llave_cobro_vivo` intacta**, y un segundo POST a `/tickets/42/cobro` da `?err=cobro-vivo`; *previene los dos instrumentos vivos.*
33. Mismo caso pero Stripe confirma `pi.status='canceled'` → `cancelado`, `llave_cobro_vivo IS NULL`, y ya se puede generar otro.
34. Cancelar un cobro **sin** `pi` llama a `/expire` y **no** a `/payment_intents/…/cancel`.

**Barridos — `tests/test_cobranza_jobs.py`**
35. `intentocobro` en `enviando` con `solicitado_at = now-5min` → reintenta el POST con la **misma** clave (afirmado sobre el doble) y no crea una segunda fila.
36. Mismo intento con `solicitado_at = now-30h` → **no** hace POST de creación; llama a la búsqueda por ventana/metadata.
37. Búsqueda vacía y `solicitado_at = now-3min` → **no crea nada** (eventual consistency); tras 3 barridos sin resolver → Alert, **cero creación**.
38. Cobro `pendiente` con `voucher_expira_at` pasado: `reconciliar_pendientes` con un doble que devuelve `pi.status='requires_action'` → **sigue `pendiente`**; con `pi.status='canceled'` → `vencido` vía fila sintética en `eventopasarela`; *previene que el reloj local mate un voucher vivo.*
39. Cobro con `detalle_pendiente_at` → `completar_voucher` escribe `voucher_url` y `voucher_expira_at` y lo limpia.
40. `nightly()` con `stripe_client` que lanza excepción → devuelve dict con el error y **no** tumba el job (patrón de `_evaluate_alerts_safe`).

**Panel, folio y avisos**
41. Colaborador que **no** es el asignado ni admin ni `creado_por` → 303 sin efecto (sonda: conteo de `cobro` no cambia). Asignado → sí. `creado_por` con el folio ya soltado (`assigned_to=None`) → sí; *previene el cobro huérfano de 3 días.*
42. `GET /cobros` y los dos CSV con colaborador → 303 a `/`; con admin → 200 (filas en `RUTAS`).
43. `finish_ticket` con cobro vivo y sin `confirm` → no cierra; con `confirm=1` → cierra, escribe `cobro:cierre-con-pago-pendiente` y el texto enviado contiene "vence el", **no** "quedó terminada".
44. Pago que entra con el folio **cerrado** → `pagado`, Alert `cobro:pago-tras-cierre`, `FollowUp` creado, ticket **no** reabierto.
45. Ventana de 24 h cerrada al generar → el cobro existe con `url_pago`, `enviado_at IS NULL`, `?err=ventana`, y `send_text` **no** fue llamado.
46. Cada clave nueva de `ACTION_LABELS` produce texto legible en `/activity` (ninguna cae al `return action` crudo de `app/activity.py:57`).

**Espejo — `tests/test_mirror_consistency.py` + `tests/test_archive.py`**
47. Los tests existentes ya cubren los cinco lugares una vez que las 5 tablas están en `_SCHEMA_DDL`. Añadir: `archive_cobro` sobre el mirror SQLite escribe y **reescribe** (idempotencia del upsert), y `archive_evento_pasarela` con un `payload_json` de 40 KB lo guarda recortado a 32 KB.
48. `_safe` con `_ready=False` y un `init()` que lanza: **no** vuelve a llamar `init()` en la segunda escritura dentro de los 60 s; *previene la tormenta de DDL en el request del webhook.*

---

## 10. Qué se necesita exactamente del dueño

> **Nunca pegues un secreto en este chat ni en un ticket.** Los valores se escriben directo en el `.env` del VPS (`nano /opt/tramites/.env`) y se aplican con `docker compose up -d`. El repo solo recibe los **nombres** en `.env.example`.

### 10.1 Variables de entorno (nuevas en `app/config.py` y `.env.example`)

| Variable | De dónde sale | Sin ella |
|---|---|---|
| `STRIPE_SECRET_KEY` | Dashboard de Stripe → **Developers → API keys → Secret key** (`sk_test_…` para probar, `sk_live_…` para producción) | El panel muestra "Stripe no configurado" y **no** se escribe ningún cobro (no-op del cliente, aviso explícito en la UI) |
| `STRIPE_WEBHOOK_SECRET` | Dashboard → **Developers → Webhooks → tu endpoint → Signing secret** (`whsec_…`). Es **distinto** en modo prueba y en modo real | El webhook rechaza **todo** con 403 (fail-closed deliberado) y ningún pago se confirma |
| `STRIPE_API_VERSION` | La versión que muestre tu dashboard el día del deploy (p.ej. `2025-xx-xx`). Se pinea para que un cambio de shape de Stripe no rompa el aplicador | Se usa la default de la cuenta y el histórico deja de ser reproducible |
| `PUBLIC_BASE_URL` | La URL pública del panel, p.ej. `https://tramites.tudominio.mx`. Se usa para `success_url`/`cancel_url`. *(Una versión previa de esta tabla decía `STRIPE_PUBLIC_BASE_URL` — nombre que el código NUNCA leyó; la fuente única es `.env.example`)* | El panel rechaza generar con `err=sin-url-publica` antes de escribir nada |
| `COBRO_MONTO_MAXIMO_CENTAVOS` | Decisión del dueño. Sugerido `10000000` ($100,000) | Default $100,000 |
| `OXXO_DIAS_VENCIMIENTO` | 1-3. Sugerido `3` | Default 3 |

### 10.2 Configuración en el dashboard de Stripe (lo hace el dueño, una vez por modo)
1. **Activar OXXO**: Settings → Payment methods → **OXXO** → Turn on. Hay que activarlo **en modo prueba y en modo real por separado**. Sin esto, la creación de la sesión falla con un error críptico.
2. **Activar MSI (installments)**: Settings → Payment methods → **Cards → Installments (México)**. Confirmar qué plazos quedan habilitados (3/6/9/12) — el plazo lo elige el cliente y **la comisión la absorbe el negocio**, así que conviene decidir el tope.
3. **Registrar el webhook**: Developers → Webhooks → **Add endpoint** → `https://<tu-dominio>/webhooks/stripe`, con **estos eventos y no otros**:
   `checkout.session.completed`, `checkout.session.async_payment_succeeded`, `checkout.session.async_payment_failed`, `checkout.session.expired`, `payment_intent.succeeded`, `payment_intent.payment_failed`, `payment_intent.canceled`, `payment_intent.partially_funded`, `charge.refunded`, `charge.refund.updated`, `charge.dispute.created`, `charge.dispute.closed`.
   > `charge.dispute.closed` es el doceavo y se agregó después: sin él la
   > bandera `disputado` no se apaga nunca, la alerta crítica queda encendida
   > de por vida y la disputa SIGUIENTE solo actualiza la misma fila — nadie
   > ve que hay una nueva.
4. **Copiar el `whsec_` al `.env` del servidor** (nunca al chat, nunca al repo) y reiniciar el contenedor.
5. **Un solo modo a la vez.** No dejar el endpoint registrado en modo prueba mientras corre producción, y **no** correr `stripe listen --forward-to` contra el dominio de producción: aunque `livemode` está guardado y el aplicador rechaza el modo que no coincide, la regla operativa evita el susto.

### 10.3 En el VPS
* **Reloj sincronizado** (`timedatectl set-ntp true`). Un desfase >300 s hace que la verificación de firma rechace el **100%** de los webhooks con 403 y el sistema se ve sano. La alerta `cobro:desfase-reloj` lo canta, pero mejor no llegar ahí.
* **Cron nocturno**: la línea que ya existe (`30 3 * * * docker exec tramites-bot python -m app.jobs`) basta — `nightly()` llama los cinco barridos. **Confirmar que está dada de alta**: sin ella, la única red de seguridad es `eventos_pendientes()` en el startup.
* **HTTPS válido** en el dominio del webhook (Stripe no entrega a certificados inválidos).

### 10.4 Dos decisiones de producto que el dueño tiene que tomar (no son código)
1. **Aviso de pago fuera de la ventana de 24 h.** Con OXXO la confirmación llega a los 2-3 días con la ventana de WhatsApp cerrada. Sin una **plantilla HSM aprobada por Meta**, el cliente **no** recibe aviso automático de "ya cayó tu pago": el panel se lo marca al asesor para que lo mande a mano. Si se quiere el aviso automático, hay que dar de alta la plantilla en Meta.
2. **CFDI.** Stripe cobra, no factura en México. Hay que decidir quién y cómo emite la factura, porque es la primera pregunta que va a llegar.
---

# ADENDA — Decisiones del dueño (2026-07-29)

Cuatro preguntas abiertas quedaron resueltas. Dos cambian el alcance de PR1.

## A.1 Revisión manual de OXXO: **consultar y, si no, marcar**

El asesor tendrá dos acciones en el folio, en este orden:

1. **«Consultar a Stripe ahora»** — `GET /v1/payment_intents/{id}` bajo demanda; el
   resultado entra como evento sintético `panel:consulta:{cobro_id}:{iso}` y pasa por el
   mismo aplicador que el webhook. Cero riesgo: Stripe sigue siendo la única verdad.
   Es la reconciliación manual que ya estaba en §8, ahora con botón propio.
2. **«Confirmado por asesor»** — solo si Stripe todavía no sabe nada. **No** escribe
   `estado='pagado'`: escribe una bandera paralela. El rango 4 sigue siendo exclusivo de
   Stripe y la monotonía de §1.6 queda intacta.

Campos nuevos en `cobro` (ninguno altera el índice del candado):

| Campo | Tipo | Nota |
|---|---|---|
| `confirmado_por_asesor` | `bool = False` | desbloquea el trabajo; **no** es "pagado" |
| `confirmado_por` | `str \| None` | username de quien puso la bandera |
| `confirmado_at` | `datetime \| None` | |
| `referencia_recibo` | `str \| None` | la referencia tecleada del recibo de OXXO/SPEI; es lo único que amarra el comprobante a ESTE cobro |

Reglas:
* El panel y `/export/conciliacion.csv` distinguen «Pagado» de «Confirmado por asesor
  (sin confirmar por Stripe)». Nunca se suman como lo mismo.
* Alerta `cobro:confirmado-sin-stripe` si pasan más de N horas con la bandera puesta y
  Stripe sigue sin confirmar.
* Cuando Stripe confirma, `estado` sube a `pagado` normalmente y los dos registros se
  cruzan por `referencia_recibo` / `stripe_charge_id`. La bandera se conserva como
  rastro de quién se adelantó.

## A.2 El depósito bancario **no** sirve para confirmar un folio

El dueño reporta que ven el depósito «en la cuenta bancaria del negocio». Eso es el
**payout de Stripe**: llega días después y **agrupado** — un solo depósito contiene
decenas de cobros, ya neteados de comisiones. No existe correspondencia 1:1 entre un
depósito y un folio, así que ese camino nunca va a confirmar un cobro individual.

Consecuencia para el diseño: `/export/conciliacion.csv` sube de «bonito de tener» a
**pieza central**, y se le agrega la dimensión que faltaba:

* Guardar `payout_id` en `cobro` (viene de la balance transaction).
* Vista `/cobros/payouts`: por cada depósito bancario, qué cobros lo componen, cuánto
  fue comisión y cuánto neto. Es lo que permite cuadrar el estado de cuenta del banco
  contra los folios sin hacerlo a mano.

## A.3 SPEI **entra en PR1**

Sube de PR1.5 a PR1 por decisión del dueño: confirma en minutos u horas (no días como
OXXO) y su comisión es menor que tarjeta, así que es la respuesta directa a la queja de
lentitud. Implica `customer_balance` con `bank_transfer[type]=mx_bank_transfer`, crear
Customer en la sesión y manejar `payment_intent.partially_funded`.
**Verificar los nombres exactos contra la doc vigente de Stripe al implementar.**

Métodos de PR1: **tarjeta + MSI + OXXO + SPEI.**

## A.4 Sin plantilla HSM: avisa el panel, decide el asesor

No se da de alta ninguna plantilla UTILITY con Meta. Cuando un pago se confirma fuera de
la ventana de 24 h, el bot **no** le escribe al cliente: el folio se marca en el tablero
del asesor y él decide si contacta. Se conserva `aviso_pago_enviado_at` en el esquema
para no migrar si algún día se aprueba la plantilla.

## A.5 CFDI: fuera de alcance

El dueño indica que no se emite factura porque no cobran IVA sobre el costo de
operación. El módulo termina en «el cliente pagó». *(No es asesoría fiscal: conviene
confirmarlo con su contador, porque no cobrar IVA y no estar obligado a emitir CFDI son
cosas distintas.)*

---

# ESTADO DE IMPLEMENTACIÓN (rama `claude/stripe-cobranza-5c3392`)

PR1 **completo, con la suite en verde: 787 passed, 18 xfailed.**

| Pieza | Archivo | Estado |
|---|---|---|
| 5 tablas + 5 índices parciales | `app/models.py`, `app/db.py` | ✅ |
| Espejo MSSQL (los 5 lugares × 5 tablas) | `app/archive.py`, `app/system.py` | ✅ |
| Endurecimiento del espejo (§7.4) | `app/archive.py`, `docker-compose.yml` | ✅ |
| Dinero (centavos enteros) | `app/dinero.py` | ✅ |
| Cliente HTTP | `app/stripe_client.py` | ✅ |
| Dominio / aplicador / replay | `app/cobranza.py` | ✅ |
| Webhook fail-closed 200/403/503 | `app/stripe_webhook.py` | ✅ |
| Los 5 barridos | `app/cobranza_jobs.py` | ✅ |
| Alertas | `app/cobranza_alertas.py` | ✅ 19 reglas |
| Panel + CSV + payouts | `app/cobranza_panel.py`, `templates/` | ✅ |
| Humo con Stripe vivo | crear · pagar · conciliar | ✅ modo **prueba** completo |
| Humo en modo **real** | crear 4/4 · cancelar ×2 | ✅ · falta solo el **cargo** |
| Panel visto en navegador | `/cobros`, `/unmatched` | ✅ escritorio y 375 px |

`plancobro` y `paymentmethod` quedaron **inertes** como se acordó: DDL, espejo y
snapshot completos, cero rutas y cero escrituras.

## Desviaciones del diseño, con su razón

1. **Las alertas se derivan del estado, no del evento que pasó.**
   `evaluate_alerts()` resuelve (`active=False`) toda alerta activa cuyo código
   no vuelva a disparar, así que una levantada solo ad-hoc desde el aplicador
   se habría apagado sola en el barrido nocturno **con el problema aún vivo**.
   El aplicador las sigue levantando al vuelo para que el panel las vea en el
   minuto; los códigos coinciden a propósito para que sea la misma fila.
2. **`cobro.vencido` es un tipo de evento propio.** Un PI que Stripe cancela
   por su cuenta (`cancellation_reason` `automatic`/`abandoned`) es un voucher
   **vencido**, no una cancelación del asesor: mismo rango, distinta palabra
   para el cliente y para el panel.
3. **Un evento sintético usa su propio `cobro_id`** en vez de re-resolverse por
   metadata: lo escribimos nosotros y no hay ambigüedad que resolver. La
   resolución de dos factores queda intacta para todo lo que llega de fuera.
4. **`absorber_datos` nunca reemplaza un `payment_intent` ya escrito.** Un PI
   distinto es un segundo pago (lo trata `_aplicar_pago`), no una corrección.
5. **Se detecta la COLUMNA, no el índice, en el `IntegrityError`.** SQLite
   reporta `UNIQUE constraint failed: cobro.llave_generacion`; buscar
   `ux_cobro_generacion` nunca acertaba y el F5 devolvía `err=interno`. Lo
   encontraron las pruebas del panel.
6. **Páginas de retorno propias** (`/cobros/gracias`, `/cobros/cancelado`):
   públicas y sin `base.html`. Las abre el cliente desde su celular y ninguna
   afirma que el pago se aplicó — con OXXO/SPEI llega ahí *antes* de pagar.
7. **SPEI hace un POST previo a `/v1/customers`.** El diseño suponía que
   `customer_creation: always` bastaba; la API viva responde
   `400 The payment method 'customer_balance' requires 'customer' … to be set`.
   El Customer se crea con la clave de idempotencia derivada (`…:cus`), así el
   reintento del barrido reusa el mismo en vez de sembrar uno por intento, y
   `customer_creation` desaparece del cuerpo porque es excluyente con
   `customer`. Lo encontró la prueba de humo contra la cuenta real; ningún
   doble podía verlo.

8. **Cancelar sin `pi` ni `cs` NO cierra si hay un intento en `enviando`.** El
   diseño tenía dos ramas (Stripe confirma / Stripe no confirma) y deducía
   "nunca existió instrumento" de la ausencia de ids. Es falso mientras un POST
   siga en vuelo: el barrido de huérfanos recuperaba después la sesión que
   Stripe **sí** creó y la pegaba a un cobro ya cancelado, con el asesor
   habiendo generado ya el reemplazo. Dos instrumentos cobrables para el mismo
   (teléfono, servicio) — el escenario que el candado existe para impedir.
9. **La cola de reconciliación filtra en SQL y rota por `reconciliado_at`.** El
   `.limit(200)` corría **antes** del filtro de antigüedad: SQLite devuelve las
   primeras filas por rowid (las más viejas) y el filtro se aplicaba en Python
   sobre lo ya truncado. Con 200 zombis —SPEI en `requires_action`, que no
   tiene reloj de vencimiento propio— el cobro de hoy con el webhook perdido no
   entraba al lote nunca. `reconciliado_at` se sella también en el camino
   "sin respuesta"; si no, lo inalcanzable se pega al frente del `ORDER BY`.
   Índice nuevo `ix_cobro_reconciliacion` sobre `cobro(estado, reconciliado_at)`.
10. **`charge.refund.updated` entrega un objeto `refund`, no un charge.** No
    trae `amount_refunded`, así que el `or 0` escribía **cero** sobre el
    acumulado de un cobro ya reembolsado, y el replay lo repetía en cada
    recomputación. El acumulado absoluto solo se lee del `charge`. Su
    `balance_transaction` es además otra distinta y negativa: escribirla pisaba
    la del cargo, dejando el neto en rojo y el `payout_id` equivocado. Ahora la
    BT solo se absorbe cuando el objeto es un `charge`.
11. **Un reembolso invalida el neto ya conciliado.** `conciliar_montos` filtra
    por `neto_centavos IS NULL`, así que sin invalidar, el neto quedaba inflado
    para siempre y `/export/conciliacion.csv` reportaba de más. En el caso del
    objeto `refund` se borra también `balance_transaction_id`: es lo que fuerza
    la relectura del charge para traer `amount_refunded` fresco. En México
    Stripe no devuelve la comisión al reembolsar, y el neto ya la resta.
12. **Un evento viejo rellena lo que falta, pero no pisa lo que hay.** Antes se
    descartaba entero. Cualquier sintético local (cancelar, enviar, `cron:recon`)
    estampa el reloj del VPS en `ultimo_evento_created`, así que el
    `payment_intent.succeeded` **real** que llegara después era "más viejo" y se
    tiraba: el cobro quedaba `pagado` sin `stripe_charge_id`, y `conciliar_montos`
    filtra por esa columna — pago real fuera del circuito de conciliación para
    siempre, sin comisión, sin neto y sin `payout_id`. `transicionar` tampoco
    baja ya la marca de tiempo (`max`), para no reabrir la puerta a otros más
    viejos.
13. **El timeout de MSSQL queda por debajo del `busy_timeout` de SQLite** (3 s
    contra 5 s). §7.4 justificaba el espejo síncrono con que `_safe` degrada
    cualquier excepción a `warning`; eso cubre MSSQL **caído**, no MSSQL
    **lento**, donde no hay excepción que degradar. `timeout` en pymssql es por
    statement y el listener corre dentro del flush, reteniendo el escritor: el
    INSERT de `eventopasarela` del webhook moría con `database is locked` y
    devolvía 503, quemando reintentos de Stripe. La forma definitiva es mover el
    espejo a un listener `after_commit`, fuera de la transacción; queda pendiente.

14. **El espejo de las tres tablas de dinero corre en `after_commit`.** Es la
    forma definitiva que la desviación 13 dejaba pendiente: los listeners de
    `Cobro`, `IntentoCobro` y `EventoPasarela` encolan en `session.info` y un
    listener de sesión drena la cola **fuera** de la transacción de SQLite. Se
    encola una instantánea (`SimpleNamespace` con las columnas), no el objeto:
    después del commit los atributos están expirados y leerlos dispararía un
    SELECT por fila. Las demás tablas siguen síncronas — su volumen es bajo y
    no están en el camino del webhook. Lo que se pierda si el proceso muere
    entre el commit y el espejo lo repara `backfill()` al arrancar, igual que
    cuando MSSQL está caído. `tests/test_mirror_consistency.py` reconoce ahora
    `_diferir` además de `_safe` como LUGAR 3 de los cinco.
15. **`/cobros` pagina de 100 y agrega los totales en SQL.** Antes materializaba
    la tabla completa como objetos SQLModel y sumaba en Python: a 18,000 filas
    son ~150 MB de heap en un VPS de 2 GB, con dos admins refrescando a la vez.
    El filtro de `livemode` se fue al WHERE, los totales salen de un `GROUP BY`
    sobre TODA la cartera (no sobre la página), y `eventos_pendientes()` —hasta
    200 transacciones— pasó a `BackgroundTasks`, fuera del camino del render.
    Los dos CSV se emiten con `StreamingResponse` sobre `yield_per(500)`, así
    que ya no se arma el archivo entero en memoria antes del primer byte.
    `/cobros/payouts` lleva ventana de 90 días y cuenta con `COUNT` en vez de
    `len(...all())`.
16. **Lo PEDIDO y lo RECIBIDO dejaron de ser el mismo número.** El total por
    estado usaba `monto_pagado or monto_centavos` incluso en estados vivos, y
    `payment_intent.partially_funded` escribe `monto_pagado` dejando el cobro
    en `pendiente`: un SPEI de $50,000 con $10,000 transferidos reportaba
    $10,000 por cobrar en vez de $50,000 — la cartera se desplomaba justo
    mientras crecía. Ahora los estados vivos suman lo pedido, los cobrados
    suman lo recibido, y arriba hay dos cifras que no existían: **en la calle**
    y **recuperado**.
17. **`/export/conciliacion.csv` incluye a los confirmados por asesor** y trae
    `telefono`, `cliente`, `concepto` y `creado_por`. Filtrar solo por estado
    dejaba fuera justo a la población que la adenda A.1 manda distinguir ahí,
    y volvía letra muerta a la columna `confirmado_por_asesor`. Se distinguen
    por la columna `estado`, que ahora también va en el CSV; **nunca se suman
    como lo mismo**.
18. **Las reglas de alerta que no se apagaban solas ya lo hacen.** `doble-pago`
    y `pago-tras-cierre` llevan la misma ventana de 7 días que las reglas 10 y
    13; la primera además filtra por `resultado` (indexado) antes de comparar
    `detalle`, que no lo está y vive detrás de un `payload_json` de hasta 32 KB.
    Una alerta permanentemente encendida no se lee, y `evaluate_alerts()`
    reutiliza la fila: el caso número 21 no producía nada visible.
19. **Regla 14, `cobro:monto-sobre-cotizado`.** El sistema ya guardaba
    `monto_cotizado_centavos`, ya clasificaba la diferencia como
    `monto_origen = "override"` y ya se la enseñaba al asesor en el formulario
    — pero **solo al que la tecleaba**. No la miraba ninguna alerta ni ningún
    barrido: se podía pedir más de lo cotizado por WhatsApp y nadie se enteraba,
    contra un compromiso público escrito en `landing/terminos.html` §2. La
    regla compara la **columna** (`monto_centavos > monto_cotizado_centavos`),
    no `monto_origen`, porque el origen se congela al crear y el monto se puede
    editar después; y lleva ventana de 7 días porque `monto_cotizado_centavos`
    no cambia nunca — sin ella, un override legítimo del año pasado dejaría la
    alerta encendida para siempre. Solo dispara hacia **arriba** y solo en
    estados cobrables: pagar de menos no es una queja, y un cobro cancelado
    nunca le costó nada al cliente.
20. **Auditoría pre-deploy (2026-08-01): 11 hallazgos confirmados, todos
    corregidos.** Los dos que importan al dinero: (a) un **declive de
    tarjeta** (`payment_intent.payment_failed`) se trataba como terminal y
    soltaba el candado con la Checkout Session AÚN ABIERTA — el cliente podía
    reintentar en el link viejo mientras el asesor generaba otro: dos
    instrumentos cobrables; ahora solo `checkout.session.async_payment_failed`
    (OXXO/SPEI) es terminal, y el declive deja el cobro pendiente con el
    candado puesto. (b) el deploy hacía `git reset --hard` ANTES del
    `compose up`: si el up fallaba, el siguiente tick veía LOCAL==REMOTE y no
    reintentaba nunca — bot caído hasta entrar por SSH; ahora hay `rollback()`
    al commit anterior en ambos caminos de fallo. Además: guard de
    `PUBLIC_BASE_URL` vacía (antes: 100% de los cobros fallaban con un error
    genérico), el cierre de folio filtra por (teléfono, servicio) — el mismo
    criterio del candado — en vez de solo teléfono, los cinco códigos ad-hoc
    del aplicador ganaron regla derivada (15-19: sin ella `evaluate_alerts()`
    los apagaba la misma noche), un rechazo 4xx de Stripe suelta el candado
    (antes: borrador bloqueado para siempre), `avisar-pago` y el envío del
    link ya no se marcan como enviados cuando Meta rechaza,
    `referencia_recibo` se recorta a los 100 del espejo, y el drenado de
    eventos reclama el escritor antes de leer (cron y panel corren el mismo
    barrido desde procesos distintos). Cada arreglo trae su prueba en rojo
    verificada — salvo el reclamo del drenado, cuya carrera no es
    reproducible de forma determinista en un test.

## Lo que falta para cobrarle a un cliente de verdad

Nada de código. Falta lo que solo puede hacer el dueño (§10):

1. `STRIPE_SECRET_KEY` y `STRIPE_WEBHOOK_SECRET` en el `.env` del VPS. Sin la
   primera el panel dice «Stripe no configurado» y no escribe ningún cobro; sin
   la segunda el webhook rechaza **todo** con 403, a propósito.
2. **Métodos: verificados en modo REAL (2026-07-31).** Se crearon Checkout
   Sessions live (`livemode=true`) y se expiraron enseguida — crear no cobra.
   Tarjeta ✅ · OXXO ✅ (`oxxo: {expires_after_days: 3}`) · MSI ✅
   (`card: {installments: {enabled: true}}`) · SPEI ✅
   (`customer_balance: {bank_transfer: {type: mx_bank_transfer}}`).

   **SPEI estaba apagado en producción y este chequeo lo encontró**: la API
   devolvía `The payment method type provided: customer_balance is invalid.
   Please ensure the provided type is activated in your dashboard`. Se activó
   ("Transferencias bancarias" en Settings → Payment methods, con *Test mode*
   apagado) y volvió a probarse: pasa. Los interruptores de prueba y de
   producción son **distintos**; que algo funcione en `sk_test_` no dice nada
   del modo real.
3. Registrar el endpoint `https://<dominio>/webhooks/stripe` con los 12 eventos
   de §10.2.3.
4. `timedatectl set-ntp true` en el VPS: un desfase >300 s hace que la
   verificación de firma rechace el 100% de los webhooks **y el sistema se ve
   perfectamente sano**.
5. **Humo de creación: hecho** (2026-07-30, `sk_test_`, cuenta
   `acct_1TycbaQUq2XAVvdQ`). Cuatro Checkout Sessions reales — tarjeta,
   tarjeta+OXXO, tarjeta+MSI, tarjeta+OXXO+SPEI — 4/4 aceptadas por la API.
   De ahí salió la desviación 7.
6. **Humo de PAGO punta a punta: hecho** (2026-07-31, modo prueba, misma
   cuenta, con `stripe listen` reenviando al webhook local). Cobro generado
   desde el panel por $800.00 (monto prellenado con lo cotizado, `monto_origen
   = "cotizado"`), pagado con `4242 4242 4242 4242`. Lo que quedó probado y
   ningún doble podía probar:
   * La **firma HMAC se verifica contra Stripe real** y el 403 fail-closed se
     levanta al poner el `whsec_`. Antes de pagar se dispararon cuatro eventos
     firmados ajenos: los cuatro entraron y se resolvieron `huerfano` — el
     aplicador clasifica lo que no le pertenece en vez de tronar o inventar.
   * **La idempotencia aguanta el orden real de Stripe.** Llegaron cinco
     eventos del pago; `payment_intent.succeeded` aplicó, y
     `checkout.session.completed` —que llegó DESPUÉS— se marcó `duplicado
     (pago ya aplicado)`. Ese es el escenario que duplica ingresos en las
     implementaciones ingenuas. `charge.succeeded`, `payment_intent.created` y
     `charge.updated` se ignoraron limpiamente por tipo no manejado.
   * **La conciliación lee dinero real, no una tarifa hardcodeada.**
     `conciliar_montos()` trajo la balance transaction: pagado 80000,
     comisión 4153, neto 75847 (cuadra exacto; 5.19% para tarjeta MX).
   Falta el mismo humo **en modo real**, que es otra llave y otros
   interruptores.
7. Un guardarraíl que apareció al hacerlo: el formulario de generar lleva un
   `confirm()` nativo con el monto en el texto. Detiene el envío hasta que un
   humano lea la cifra — el riesgo operativo #1 es el cero de más.

---

# ADENDA — La cartera del colaborador (2026-09-08)

El panel de cobranza nació admin-only: `/cobros` es la vista de la jefa y el
asesor solo veía cobros de a uno, entrando por el folio. Faltaba la pregunta
que un asesor se hace todos los días —**«¿a quién le cobro hoy?»**— y la que
se hace quien lo supervisa —**«¿quién trae dinero en la calle?»**.

## C.1 Es una cola de trabajo, NO una frontera de acceso

Decisión del dueño, y hay que tenerla presente antes de "endurecerla":
`/my/cobros` filtra por dueño para que el asesor tenga una lista corta y suya,
no para esconderle nada a nadie. La razón es que el repo ya decidió lo
contrario en otras dos partes, y fingir lo opuesto sería seguridad de teatro:

* `POST /tickets/{id}/claim` es **autoservicio**: cualquiera se asigna
  cualquier folio libre de su oficina. Una columna que uno mismo se pone no
  puede ser un permiso.
* `GET /tickets/{id}` valida **solo `office_id`**. La oficina es el libro
  compartido, por diseño.

La frontera dura sigue siendo `alcance()` —la oficina— y las acciones sobre un
cobro las sigue gateando `cobranza.puede_cobrar`. La cartera se compone con la
primera, no la reemplaza.

## C.2 «Suyo» son dos puertas, y por eso hay UN predicado

Decisión del dueño: un cobro es del asesor si **lo generó él** (`creado_por`)
**o** si el folio está asignado a él (`assigned_to`). Las dos hacen falta:
sin `creado_por`, soltar el folio le borra de la vista un voucher que todavía
debe perseguir; sin `assigned_to`, quien hereda un folio no ve la deuda que
hereda con él.

Vive en `app/cobranza.py` junto a `puede_cobrar`, como su gemelo en SQL
(`mios()`), para que la lista y el permiso no puedan divergir: el día que uno
cambie de criterio, el otro se mueve con él o la prueba se pone roja.

El JOIN con `ticket` es **OUTER**, y es la única forma correcta: `ticket_id`
es nullable porque el cobro sobrevive al folio (§4), así que un INNER se comía
en silencio justo los vouchers de OXXO, los más urgentes de la lista.

## C.3 El corte por asesor se agrupa por `creado_por`, no por el asignado

`assigned_to` se suelta (queda NULL) y se reasigna: un corte por él pierde
filas en un cubo «sin asesor» y **le cambia el pasado a un mes ya cerrado**.
`creado_por` es permanente y NOT NULL, así que cada cobro cae en exactamente
un asesor y la suma cuadra.

Y cuadra de verdad, no de palabra: el total global se **pliega de las mismas
filas** del corte por asesor (`_por_estado`), así que `Σ(en la calle por
asesor) == en_la_calle` por construcción y no porque dos SELECT parecidos
coincidan. De paso `/cobros` hace un agregado menos que antes.

## C.4 Lo confirmado por el asesor se reporta aparte, no se descuenta

`confirmar_por_asesor` **no** escribe `estado='pagado'` —el rango 4 es
exclusivo de Stripe (§0)— así que ese cobro sigue `pendiente` y seguía sumando
en «En la calle» hasta que el link vencía. La cartera le reclamaba al asesor
dinero que él ya había visto entrar, que es exactamente como una lista
operativa deja de creerse a la segunda semana.

Ni descontarlo (sería fingir que Stripe lo confirmó) ni esconderlo: se agrega
`confirmado_por_asesor` al GROUP BY y se muestra como cifra propia, rotulada
«incluido en en la calle».

## C.5 Dónde vive

| Qué | Dónde |
|---|---|
| El predicado en SQL | `app/cobranza.py` → `mios()`, junto a `puede_cobrar` |
| La vista personal | `app/cobranza_panel.py` → `GET /my/cobros` (NO admin-only) |
| El número para la tarjeta de `/my` | `app/cobranza_panel.py` → `cartera_de()` |
| El corte por asesor | dentro de `GET /cobros`, misma consulta agregada |
| La tabla, compartida por las dos vistas | `templates/_cobros_tabla.html` |
| Las pruebas, con sus mutaciones | `tests/test_cobranza_cartera.py` |
