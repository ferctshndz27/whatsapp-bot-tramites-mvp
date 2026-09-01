# SPEC — App Android del cliente (Fase 8)

> Grano verificado contra el código real del repo: `app/main.py:120-136` (routers, todos admin/webhook),
> `app/webhook.py:62-67` (HMAC `compare_digest`, fail-closed), `app/auth.py` (`is_admin`, cookie de staff),
> `app/config.py:26` (`secure_cookies`), `app/ventana.py:32,47-55` (ventana de 24 h de Meta),
> `app/cobranza_panel.py:1-12` (regla de oro: el bot nunca genera un cobro), `app/models.py:530`
> (`class Cobro`), `:576-578` (`llave_cobro_vivo` / `llave_generacion`), `:581` (`token_verificacion`),
> `:590,592` (`url_pago` / `voucher_url`), `app/archive.py:28,317,337-390,500,782,794,875` (los cinco
> lugares del espejo + `_safe` + `backfill`), `app/system.py` (`MIRROR_TABLES`), `app/catalog.py:36`
> (`PAGE_SIZE = 8`). Fuentes externas (Play, Stripe, Meta, Firebase, GitHub) citadas en el anexo, extraídas
> el 2026-09-01.

Este documento es un **plan**, no código. No implementa nada. Responde a: "app Android para el cliente,
que busque ejemplos en GitHub, que acceda a los contactos para cobranza, y que sugiera qué más debería
hacer el cliente desde la app". La respuesta a "acceso a contactos para cobranza" es §1 y no es la que se
pidió — se explica por qué, con evidencia técnica y legal, y con la alternativa que sí publica.

---

## 0. Las decisiones duras (léelas antes que la arquitectura)

| # | Contradicción / tentación | Decisión | Por qué |
|---|---|---|---|
| 1 | "La app lee la libreta de contactos para la estrategia de cobranza" | **NO se construye.** Cero `READ_CONTACTS` en el manifiesto. La única lectura de contactos permitida es el **Contact Picker** del sistema (elige 1, sin permiso), y **solo** para un uso legítimo (referir un conocido / registrar una referencia con su propio consentimiento), **nunca** para contactar a terceros por el adeudo. | Es el patrón "montadeudas": Play lo rechaza (y prohíbe de plano en el vertical financiero), y en México procesar los contactos de un cliente sin su consentimiento viola la ley de datos y puede tipificar extorsión, perseguible de oficio desde 2025. §1. |
| 2 | ¿Cuál es el caso de negocio de la app entonces? | **El push gratis.** Meta cobra por mensaje desde jul-2025 y la ventana de 24 h (`ventana.py:32`) limita cuándo puedes escribirle al cliente por WhatsApp. Un push FCM es **gratis, ilimitado y sin ventana**. Esa es la verdadera "estrategia de cobranza": recordatorio de pago a costo cero, no hostigar contactos. | §5. Más barato, más efectivo y sin exposición legal. |
| 3 | ¿Cómo entra el cliente? Hoy no hay ninguna superficie de cliente. | **Frontera de confianza nueva: teléfono + OTP.** Hoy solo existen dos: HMAC de Meta (`webhook.py:62-67`) y cookie de staff (`auth.py`). Un cliente no es ninguna de las dos. El OTP viaja por **WhatsApp** (plantilla AUTHENTICATION), no SMS, sobre el pipe que ya se paga. | §2. Esta es la parte difícil del plan, no la UI. |
| 4 | ¿Stripe SDK / PaymentSheet dentro de la app? | **NO.** Se abre la `url_pago` **existente** del cobro en un **Chrome Custom Tab**. El SDK no puede consumir una Checkout Session ya creada: obligaría a crear un `PaymentIntent` paralelo = dos objetos cobrables para el mismo folio = el doble cargo que el backend prohíbe por diseño. | §4. Reusar el link es correcto **por construcción**, no por disciplina. |
| 5 | ¿La app puede tener un botón "Pagar ahora" que genere el cobro? | **NO.** La regla de oro se extiende a la app: **el bot y la app NUNCA generan un cobro.** La app solo **muestra** cobros que un humano creó desde el panel y abre su link. | `cobranza_panel.py:1-12`, `models.py:576-578`. Un dev móvil añadirá ese botón sin pensarlo; el spec lo prohíbe explícito. |
| 6 | ¿Confiar en que el cliente "regrese" del pago para marcar pagado? | **NO.** La verdad del pago sigue siendo el **webhook de Stripe** (`checkout.session.completed` + `async_payment_succeeded/failed`). La app, al volver, **consulta el estado al backend**, no asume éxito. | §4. OXXO paga días después; el retorno del cliente no es señal. |
| 7 | ¿Cuántas tablas nuevas? Cada tabla cuesta **cinco lugares** en el espejo MSSQL. | **Una en el MVP: `devicetoken`.** El cliente ya se identifica por `phone` en `Ticket`/`Cobro` (no hace falta tabla de clientes). El OTP se delega a **Twilio Verify** (Twilio guarda el código → cero tabla). La sesión de app es un **JWT firmado** stateless (cero tabla). | §7. Minimizar el espejo es una decisión de diseño, no pereza. |
| 8 | ¿Android nativo, o bastaría una PWA? | **Se construye Android nativo porque se pidió**, pero el spec lo dice en voz alta: este producto es WhatsApp-native porque la audiencia no instala apps. Cada función de la app debe ser algo que **WhatsApp no pueda hacer** (cámara de documentos, push fuera de la ventana, bóveda biométrica, historial de recibos). Lo que no cruce esa barra, una PWA lo cubre sin revisión de Play. | §9. Es la pregunta que decide si esto vale el esfuerzo. |

---

## 1. Lo que NO se construye: contactos para cobranza

La petición dice "acceder a los contactos del cliente para las estrategias de cobranza". Hay dos lecturas;
una publica y una no. No se asume que se quiso la predatoria — se ofrece la que sí funciona.

### 1.1 El argumento técnico (el que manda)

Toda versión legítima de esto envía con **cero permiso de contactos**. El **Android Contact Picker**
(`ActivityResultContracts.PickContact`, o `ACTION_PICK` sobre `CommonDataKinds.Phone.CONTENT_TYPE`)
devuelve **el contacto que el usuario eligió** con un permiso temporal de lectura, **sin** declarar
`READ_CONTACTS`:

> "The response grants your app temporary permissions to read that contact using the Contacts Provider
> API, **even if your app doesn't include the READ_CONTACTS permission**." — Android, *Common Intents*.

- "El cliente registra una referencia / a quién más avisar de su trámite" → Contact Picker o un campo de
  texto. Sin permiso, sin riesgo de revisión, se envía hoy.
- "Referir un conocido al servicio" → Contact Picker o el share sheet del sistema. Igual.
- "Leer la libreta para presionar cuando se atrase" → necesita `READ_CONTACTS`, reprueba la prueba de
  permisos sensibles, y **es lo que hace que retiren la app**.

### 1.2 El argumento de Google Play

- **Permisos sensibles**: "You may only request permissions … that are necessary to implement current
  features … promoted in your Google Play listing." Leer la libreta "para cobranza" no es función núcleo
  visible al usuario; hay alternativa de alcance mínimo (el picker) → no pasa la prueba.
- **Financial Services** (si Play clasifica la app en el vertical de préstamo/cobranza): `READ_CONTACTS`
  está **prohibido, sin excepción por declaración** — "Personal loan apps … are prohibited from accessing
  sensitive data, such as photos and contacts." Nació justo por el abuso de raspar la libreta.
- **User Data**: "We don't allow unauthorized publishing or disclosure of people's non-public contacts."
- **Nueva política de Contactos** (efectiva **27-ene-2027**, apps target API 37+): el picker será
  obligatorio salvo declaración aprobada en Play Console.

Riesgo real: rechazo en revisión y posible **suspensión de la cuenta de desarrollador**, no solo de la app.

### 1.3 El argumento legal (México) — se sostiene solo, sin Google

Los contactos del cliente son **titulares distintos** cuyo consentimiento nunca se obtuvo y a quienes
jamás se dio aviso de privacidad. El permiso que da el cliente en Android es una autorización técnica del
SO, **no** un consentimiento jurídico de esos terceros.

- **Nueva Ley de datos personales** (DOF 20-mar-2025, sustituye a la LFPDPPP de 2010). El INAI quedó
  extinto; la autoridad garante es la **Secretaría Anticorrupción y Buen Gobierno**. El texto sustantivo
  (consentimiento, aviso de privacidad, finalidad, proporcionalidad) se mantiene: recabar/tratar datos de
  terceros sin su consentimiento es infracción, con multas duplicables si hay datos sensibles.
- **Ley General de Extorsión (2025)**: extorsión perseguible **de oficio**, penas 15-25 años, **agravante
  por comisión mediante plataformas o medios electrónicos** (art. 17 fr. V) — el supuesto exacto de las
  montadeudas. Antes, cobranza ilegítima en CDMX (Código Penal, art. 209 Bis).
- **Precedente operativo**: la FGJ-CDMX ha cateado call centers de cobranza (>900 empleados) y vinculado a
  proceso a 23 personas desde 2022. *Precisión honesta: no consta en fuentes abiertas una **sentencia
  condenatoria firme** específica contra montadeudas — lo defendible es "detenciones, cateos y
  vinculaciones a proceso".*

### 1.4 Coherencia con este sistema

La cobranza del proyecto ya es humano-en-el-loop, forense y no predatoria: **el bot nunca genera un
cobro** (`cobranza_panel.py:1-12`), toda comunicación de cobro va **solo al deudor** (plantillas
`cobro_exitoso` / `cobro_rechazado`, un único número). Cosechar contactos rompería exactamente ese diseño.

**Regla del spec:** ni el bot ni la app solicitan, reciben, almacenan o derivan la libreta del cliente; y
ningún mensaje de cobranza se envía a un número distinto al del titular del adeudo. Si algún día se aceptan
"referencias" de terceros, se exige base legal propia (aviso de privacidad al tercero en el primer
contacto y registro de esa entrega).

---

## 2. La frontera de confianza nueva: teléfono + OTP

Hoy hay **dos** fronteras y el cliente no es ninguna:

| Frontera | Quién | Mecanismo | Dónde |
|---|---|---|---|
| Meta webhook | el propio Meta | HMAC-SHA256 `compare_digest`, fail-closed | `webhook.py:62-67` |
| Panel/ERP | el colaborador (staff) | cookie de sesión, `is_admin` | `auth.py`, `config.py:26` |
| **App cliente (nueva)** | **el cliente, por su teléfono** | **OTP por WhatsApp → JWT firmado** | §2.1-2.3 |

### 2.1 Alta / login

1. La app pide el teléfono (E.164). El backend genera un OTP de 6 dígitos con expiración corta (5-10 min).
2. El OTP viaja por **WhatsApp** (plantilla de categoría **AUTHENTICATION** — Meta lo **obliga** para
   códigos: cuerpo fijo, código <15 caracteres, botón copy-code / one-tap). No SMS por defecto.
3. La app verifica el código contra el backend. Éxito → el backend emite un **JWT firmado** (corto, p. ej.
   15 min) + refresh token; la identidad es el `phone`, que ya llavea `Ticket`/`Cobro`.

**Decisión dura — cómo se manda el OTP:**

| Opción | Ventaja | Costo |
|---|---|---|
| **Twilio Verify `channel=whatsapp`** *(recomendada para MVP)* | Twilio genera/expira/rate-limita el código y da fallback a SMS → **cero tabla nueva**. Usa el sender propio ya activo (obligatorio desde 1-mar-2024). | Sin one-tap ni plantilla propia. |
| Meta Cloud API, plantilla AUTHENTICATION directa | **One-tap autofill** (requiere `package_name` + `signature_hash` de la APK). | Necesita tabla `otpchallenge` (+5 lugares del espejo) y manejar expiración/rate-limit en casa. |

Recomendación: **Twilio Verify** en el MVP (menos lugares en el espejo, fallback SMS incluido); one-tap con
Meta directo es un upgrade posterior. En ambos casos, **fallback SMS explícito**: el OTP por WhatsApp solo
se entrega al dispositivo primario y falla para quien no tenga WhatsApp activo.

### 2.2 La sesión de app es un JWT, no una tabla

JWT firmado (secreto en variable de entorno, como `app_secret`) → **stateless, sin tabla, sin espejo**.
Revocación en el MVP = expiración corta + rotación de refresh. Si más adelante se necesita revocación
inmediata (robo de dispositivo), se añade un `token_version` por teléfono; no antes. *(ponytail: expiración
corta ahora, lista de revocación si el robo de dispositivo se vuelve un caso real.)*

La cookie de staff y su `Secure`/`SessionMiddleware` **no se tocan**: la app usa `Authorization: Bearer`,
un esquema aparte. El middleware de headers de seguridad (`main.py`, CSP/HSTS/noindex) aplica igual a
`/api/*`.

### 2.3 Autorización

Cada endpoint `/api/*` resuelve el `phone` del JWT y **filtra por ese teléfono**. Un cliente solo ve sus
folios, sus cobros, sus documentos. Nunca `is_admin`. Nunca un teléfono ajeno en la query.

---

## 3. La API JSON del cliente (capa de interfaz nueva)

Hoy **no existe ninguna API JSON para clientes**: todo es Jinja server-rendered (`main.py:120-136`; los
`JSONResponse` de `agente_studio.py` son AJAX de admin). La app necesita una **capa de interfaz nueva**,
versionada, no "un endpoint o dos".

- Prefijo **`/api/v1`**, router propio (`app/api_cliente.py`), registrado en `main.py`.
- **Envelope consistente** (regla de patterns del proyecto): `{ ok, data, error, meta }`.
- Autenticación `Bearer` (§2), filtrado por `phone` en cada handler.
- **Solo lectura del dominio de cobro**: la app muestra, no crea. Escrituras permitidas: subir documento,
  registrar device token, marcar leído. Nada que genere dinero.

| Método | Ruta | Qué devuelve / hace | Fuente en el repo |
|---|---|---|---|
| `POST` | `/api/v1/auth/start` | dispara OTP | Twilio Verify / Meta |
| `POST` | `/api/v1/auth/verify` | verifica OTP → JWT + refresh | §2 |
| `GET` | `/api/v1/folios` | folios del teléfono y su estado | `Ticket`, `models.py` |
| `GET` | `/api/v1/folios/{id}` | detalle, requisitos, historial | `Ticket`, `codigos.py` |
| `GET` | `/api/v1/catalogo` | categorías/servicios y precio informado (estimado) | `catalog.py` (`PAGE_SIZE` es de WhatsApp; la app pagina distinto) |
| `GET` | `/api/v1/cobros` | cobros del teléfono (estado, monto, link) — **read-only** | `Cobro`, `models.py:530` |
| `GET` | `/api/v1/cobros/{id}/pago` | la `url_pago`/`voucher_url` **existente** para abrir en Custom Tab | `models.py:590,592` |
| `POST` | `/api/v1/documentos` | sube un documento (JPEG/PDF) a un folio | `media_store.py` |
| `POST` | `/api/v1/devices` | upsert del token FCM (autenticado) | §5, tabla `devicetoken` |

`GET /api/v1/cobros/{id}/pago` **jamás** crea una sesión de Stripe: devuelve el link que ya vive en la fila,
o `409 link_vencido` si expiró (la app muestra "contacta a tu asesor", no regenera nada). Los candados
`llave_cobro_vivo` / `llave_generacion` (`models.py:576-578`) siguen siendo la garantía anti-doble-cargo.

---

## 4. Pagar desde la app — Chrome Custom Tab sobre el link existente

**Decisión (dura):** abrir la `url_pago` (Stripe Checkout hosted) en un **Chrome Custom Tab**
(`androidx.browser`). **No** se adopta el Stripe Android SDK / PaymentSheet.

Por qué, en las tres dimensiones:

1. **Reusa el link vivo por construcción.** Desde la API `2025-03-31.basil`, "Each Checkout Session
   generates a PaymentIntent **upon confirmation**": una sesión ya creada **no** expone un `client_secret`
   de PaymentIntent que PaymentSheet pueda consumir. Meter PaymentSheet obligaría a crear un PaymentIntent
   paralelo → dos objetos cobrables para el mismo cobro → el doble cargo que la decisión #7 de
   `DISENO-COBRANZA-STRIPE.md` existe para impedir. El Custom Tab abre el objeto que ya existe y **no crea
   nada**.
2. **PCI mínima.** Página alojada por Stripe (SAQ A pre-llenado); la app ni siquiera renderiza campos de
   tarjeta — es un navegador apuntando a `checkout.stripe.com`. PaymentSheet mete la captura dentro de la
   app y añade `com.stripe:stripe-android` como dependencia a mantener.
3. **Cero cambios de backend.** El endpoint que ya entrega `url_pago` y el webhook existente son la única
   fuente de verdad.

**Flujo en la app:** botón "Pagar" → `CustomTabsIntent` con la `url_pago` servida por el backend → al
retorno (App Link del `success_url`, o simplemente `onResume`) **consultar `GET /api/v1/cobros/{id}`** y
pintar `pagado / pendiente / vencido` según lo que el **webhook** haya registrado. El deep link **no**
dispara lógica de negocio (evita spoofing del estado de pago): solo un refresh contra el backend.

Requisito para el backend: que todo cobro pagable desde el móvil exista como **Checkout Session con
`url_pago`** (si hay cobros PaymentIntent-only, se unifican en el servidor antes de exponer el móvil).

---

## 5. Push (la razón de existir de la app)

FCM es **gratis, ilimitado y no depende de la ventana de 24 h** — contraste directo con la plantilla de
WhatsApp, que se cobra por entrega. Push primero; WhatsApp solo cuando aporte (cliente sin la app abierta y
el mensaje amerita el canal).

**Patrón (samples oficiales, §8):**
- Android: `FirebaseMessagingService.onNewToken()` + `getToken()` al arranque → `POST /api/v1/devices`.
- Backend: tabla **`devicetoken`** (`token` único, `phone`, `platform`, `app_version`, `updated_at`),
  upsert con timestamp en cada llamada; refresco mensual desde la app; **borrar la fila ante `UNREGISTERED`
  / 404**. Envío con **`firebase-admin` `messaging.send()` (HTTP v1)** — la API legacy (`fcm/send` +
  server key) fue apagada en 2024; la credencial es un service account por variable de entorno, **nunca**
  en el repo.

**Seguridad del canal:** el push es notificación, **no** autenticación. **Nunca** viaja el OTP ni PII
(CURP/RFC/NSS) en el payload. El endpoint de registro exige sesión autenticada (que un tercero no asocie su
dispositivo al teléfono de otro cliente).

**Costo en el espejo:** `devicetoken` es tabla mirrorada → los **cinco lugares** en el mismo commit
(`archive.py`: `_SCHEMA_DDL`, `archive_devicetoken()`, `_register_listeners`, `backfill`; `system.py`:
`MIRROR_TABLES`). Es la **única** tabla nueva del MVP.

---

## 6. Qué debe hacer el cliente desde la app (la parte "sugiere otras opciones")

Filtro rector (decisión #8): cada función debe ser algo que **WhatsApp no hace bien**. Ordenadas por valor.

| Función | Por qué en la app (no en WhatsApp) | Pieza técnica |
|---|---|---|
| **Estado de sus folios en vivo** | Consulta cuando quiera, sin gastar un mensaje ni abrir la ventana | `GET /folios` |
| **Recordatorios de pago push** | Gratis, fuera de la ventana de 24 h; el caso de negocio | §5 |
| **Pagar** (tarjeta/MSI/OXXO) | Un tap → Custom Tab sobre el link existente; ver voucher OXXO | §4 |
| **Subir documentos con la cámara** | ML Kit Document Scanner: detecta bordes, recorta, multipágina, PDF — imposible de igualar mandando fotos por WhatsApp | §8, `media_store.py` |
| **Bóveda de documentos con biometría** | CURP/acta/comprobantes guardados y **cifrados**, desbloqueo con huella; WhatsApp no ofrece un vault | §8 (Keystore + BiometricPrompt) |
| **Historial y recibos** | Estado de cuenta y comprobantes descargables; la conversación de WhatsApp se pierde en el scroll | `GET /cobros` |
| **Catálogo y cotización estimada** | Explorar servicios y ver el precio informado **siempre como estimado** (el final lo fija el asesor) | `catalog.py`, regla de oro |
| **Continuar en WhatsApp** | Botón "hablar con mi asesor" que abre el chat de WhatsApp: la app **complementa**, no reemplaza al humano | deep link `wa.me` |
| **Referir a un conocido** *(opt-in)* | **Contact Picker** (sin permiso) o share sheet; con divulgación y consentimiento; **jamás** para cobranza | §1.1 |
| **Perfil y avisos** | Aviso de privacidad enlazado in-app y en Play Console, y ejercicio de derechos (acceso/rectificación/cancelación/oposición) | User Data / ley MX |

Lo que la app **no** hace, por decisión: fijar precio final, validar documentos, confirmar pagos, cerrar el
trámite, o generar un cobro. Esa línea es load-bearing en todo el sistema (`AGENTS.md`).

---

## 7. Costo de datos: tablas nuevas y los cinco lugares del espejo

| Necesidad | Solución | ¿Tabla nueva? | Lugares del espejo |
|---|---|---|---|
| Identidad del cliente | `phone` (ya llavea `Ticket`/`Cobro`) | **No** | 0 |
| OTP | Twilio Verify (Twilio guarda el código) | **No** | 0 |
| Sesión de app | JWT firmado stateless | **No** | 0 |
| Token de push | **`devicetoken`** | **Sí** | **5** (`archive.py` ×4 + `system.py`) |

MVP = **una** tabla mirrorada. Si se opta por Meta directo para one-tap (§2.1), se suma `otpchallenge`
(+5). Cualquier columna nueva en tabla existente sigue el proceso manual sin Alembic: `db.py`
(`_*_COLUMNS_ADDED_AFTER_LAUNCH`) + `archive.py` (`_COLUMNS_ADDED_AFTER_LAUNCH`).

---

## 8. El stack y los repos de GitHub (la parte "busca ejemplos funcionales")

**Stack mínimo:** Kotlin + **Jetpack Compose (Material 3)** + **Retrofit/OkHttp** con `kotlinx.serialization`
e **interceptor de token** + **Hilt** (DI). Todos los repos verificados activos (push jul-sep 2026, ninguno
archivado) al 2026-09-01.

| Pieza de la app | Repo (oficial) | Para qué exactamente | Nota |
|---|---|---|---|
| Estructura Compose + REST | **android/nowinandroid** | Plantilla de `core/network` (Retrofit+OkHttp+serialization), `core/data`, un `feature/*`, `build-logic`, Hilt | No copiar entero (usa navigation3, es grande); tomar solo los `core/*`. **No trae auth por token**: el interceptor `Authorization` (~20 líneas) es código propio |
| Registro FCM | **firebase/quickstart-android** → `messaging/` | `onNewToken()` → backend, intent-filter `MESSAGING_EVENT`, permiso `POST_NOTIFICATIONS` (Android 13+), canal de notificación | Usar la ruta `kotlin/`, no `java/` |
| Custom Tabs (pago) | **GoogleChrome/android-browser-helper** | Abrir el checkout hosted; demos `custom-tabs-session` (warm-up) y `custom-tabs-oauth` (retorno por App Link = idéntico al `success_url` de Stripe) | El viejo `GoogleChrome/custom-tabs-client` está **archivado (ene-2023)** — no usar |
| Captura de documentos | **googlesamples/mlkit** → `android/documentscanner` | UI lista: bordes, recorte, filtro, multipágina, JPEG/PDF, **sin CameraX propio ni permiso de cámara** | Depende de Google Play services (no en dispositivos sin GMS → fallback CameraX) |
| Bóveda biométrica | **android/security-samples** → `BiometricLoginKotlin` | `BiometricPrompt` + `CryptoObject`: la clave que cifra la bóveda vive en Android Keystore y solo se libera tras biometría (no un `if` de éxito) | Fallback a credencial del dispositivo |

Evitar: `chrisbanes/tivi` (referencia clásica de OAuth/token, **archivado nov-2024**).

---

## 9. La pregunta estratégica: ¿Android nativo o PWA?

Se pidió Android, así que el plan es Android. Pero hay que decirlo en voz alta: **este producto es
WhatsApp-native porque la audiencia — clientes de trámites — no suele instalar apps.** El riesgo real del
proyecto no es técnico, es de adopción: una app con 200 instalaciones no mueve la cobranza.

Prueba de fuego para justificar el nativo: **cámara de documentos (ML Kit), push fuera de la ventana, y
bóveda biométrica** son las tres cosas que una PWA no cubre bien. El resto (ver folios, catálogo, abrir el
link de pago, recibos) una **PWA** lo hace sin revisión de Play y sin instalación. Recomendación honesta:
si el MVP se mide por adopción, considerar una **PWA primero** para validar demanda, y el nativo cuando la
cámara/vault/push demuestren su valor. Decidirlo antes de escribir código, no después.

---

## 10. Fases de entrega

| Fase | Alcance | Depende de |
|---|---|---|
| **8.0 — Cimiento backend** | `/api/v1` + envelope + JWT + `GET /folios`, `/catalogo`, `/cobros` (read-only, filtrado por `phone`) | §2, §3 |
| **8.1 — Login por OTP** | Twilio Verify `channel=whatsapp` + fallback SMS; app: pantalla de teléfono/código | §2.1 |
| **8.2 — App MVP (lectura)** | Compose + Retrofit + interceptor; folios, catálogo, cobros, "hablar con asesor" | §8 |
| **8.3 — Pagar** | `GET /cobros/{id}/pago` + Custom Tab + refresh por webhook | §4 |
| **8.4 — Push** | `devicetoken` (5 lugares) + `firebase-admin` + recordatorios de pago | §5, §7 |
| **8.5 — Documentos + bóveda** | ML Kit scanner → `POST /documentos`; bóveda cifrada con Keystore + biometría | §6, §8 |
| **8.6 — Referidos (opt-in)** | Contact Picker + divulgación/consentimiento | §1.1 |

El MVP con valor mínimo real es **8.0-8.4** (login, ver, pagar, push). 8.5+ es lo que diferencia del bot.

---

## 11. Riesgos y lo que falta verificar antes de implementar

- **Revalidar las políticas de Play** (permisos sensibles, User Data, Financial Services, Contactos 2027)
  antes de publicar: Google las actualiza varias veces al año; las citas son del 2026-09-01.
- **Tarifa MX vigente** de la plantilla AUTHENTICATION de WhatsApp (Meta revisa por trimestre) — presupuestar
  el costo del login por OTP; el push es gratis pero el OTP no.
- **Clasificación de Play**: si Play cataloga la app como financiera, el diseño ya cumple (cero
  `READ_CONTACTS`), pero conviene un aviso de privacidad y sección Data safety impecables desde el día uno.
- **App Links verificados** (`assetlinks.json` en el dominio del `success_url`) para el retorno del pago;
  sin eso el cliente queda en el navegador.
- **one-tap** depende del `signature_hash` de la firma; con Play App Signing el hash es el de Google, no el
  de la keystore local — un hash equivocado rompe el autofill en silencio.
- **Sin sentencia firme documentada** contra montadeudas: la sección legal afirma "detenciones y
  vinculaciones a proceso", no condenas.

---

## Anexo — fuentes

**Google Play** · Permisos sensibles: https://support.google.com/googleplay/android-developer/answer/16558241 ·
Contactos (efectiva 27-ene-2027): https://support.google.com/googleplay/android-developer/answer/16909972 ·
User Data: https://support.google.com/googleplay/android-developer/answer/10144311 ·
Financial Services: https://support.google.com/googleplay/android-developer/answer/9876821 ·
Contact Picker sin permiso: https://developer.android.com/guide/components/intents-common

**México — datos y extorsión** · INAI (ene-2023): https://www.proceso.com.mx/nacional/2023/1/22/inai-pide-denunciar-apps-de-montadeudas-por-violar-ley-de-proteccion-de-datos-personales-300788.html ·
Nueva ley de datos (2025): https://www.littler.com/es/news-analysis/asap/mexico-tiene-nueva-ley-en-materia-de-proteccion-de-datos-personales ·
CONDUSEF: https://expansion.mx/finanzas-personales/2026/07/01/apps-de-prestamos-montadeudas-advertencias-condusef ·
SSPC: https://www.gob.mx/sspc/prensa/la-sspc-alerta-a-la-ciudadania-a-tomar-precauciones-para-evitar-ser-victima-de-prestamos-montadeudas-en-aplicaciones-moviles ·
FGJ-CDMX (vinculación a proceso): https://aristeguinoticias.com/2108/mexico/cdmx-vinculan-a-proceso-a-23-montadeudas-pero-libran-la-prision/ ·
Ley General de Extorsión: https://www.diputados.gob.mx/LeyesBiblio/pdf/LGPISDME.pdf

**Stripe** · Pagos móviles: https://docs.stripe.com/payments/mobile/accept-payment-embedded ·
Accept a payment (Android): https://docs.stripe.com/payments/accept-a-payment?platform=android ·
Fulfillment por webhook: https://docs.stripe.com/payments/checkout/fulfill-orders ·
Seguridad/PCI: https://docs.stripe.com/security/guide

**Meta / Twilio / Firebase** · Plantillas AUTHENTICATION: https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/authentication-templates/authentication-templates ·
Twilio Verify WhatsApp: https://www.twilio.com/docs/verify/whatsapp ·
FCM registro de token: https://firebase.google.com/docs/cloud-messaging/android/get-started ·
FCM gestión de tokens: https://firebase.google.com/docs/cloud-messaging/manage-tokens ·
FCM HTTP v1: https://firebase.google.com/docs/cloud-messaging/send/v1-api

**GitHub** · https://github.com/android/nowinandroid ·
https://github.com/firebase/quickstart-android/tree/master/messaging ·
https://github.com/GoogleChrome/android-browser-helper ·
https://github.com/googlesamples/mlkit/tree/master/android/documentscanner ·
https://github.com/android/security-samples
