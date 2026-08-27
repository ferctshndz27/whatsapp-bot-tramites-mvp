# Análisis de brechas ERP/CRM + plan de desarrollo

**Fecha:** 2026-08-24 · **Estado:** borrador para revisión

Compara nuestro bot + dashboard contra lo que la categoría (plataformas de CRM/helpdesk sobre
WhatsApp) considera funcionalidad estándar, y propone un plan por fases.

---

## 0. Alcance y limitaciones — léelo antes de confiar en el resto

Verificado en este documento (segunda pasada de investigación, 2026-08-24):

- **Inventario propio**: leído directamente del repo (rutas, modelos, campos, jobs).
- **Benchmark contra Chatwoot** (36.1k ⭐, la referencia open source de la categoría): modelos y
  esquema de tablas leídos de su repositorio público vía API de GitHub. Reproducible.
- **Política y tarifación de Meta**: contrastada contra la documentación oficial
  (`developers.facebook.com`). Costó **tres pasadas**. La segunda dio por refutado un cambio de
  precios que sí es real, porque leyó únicamente la página general de `pricing`; el dato vivía en la
  página dedicada a mensajes no-plantilla. La corrección — y la trampa metodológica que la causó —
  está en §2.5. Verificar tampoco es leer una sola página.
- **Vendors, verticales LATAM y dolores de operadores**: fuentes secundarias (docs de producto,
  G2/Capterra/Trustpilot, Hacker News). Son contexto, no decisiones, y se marcan como tales.

Única fuente pedida que quedó fuera: **Reddit** — `reddit.com` bloquea el user agent (HTTP 400 a
nivel de plataforma, permanente). Se sustituyó con Hacker News y reseñas verificadas.

---

## 1. Inventario actual (verificado)

**~75 rutas HTTP** y **30 modelos**. El producto está más maduro de lo que sugiere el README.

### Ya resuelto — no re-planear

- **Multi-canal real**: Meta Cloud API + Twilio, con `provider` en `Conversation` y `Ticket`.
- **Gestión de plantillas dual-canal**: `Plantilla` con `meta_template_name`, `twilio_content_sid`,
  `aprobada_meta`, `aprobada_twilio`. Muchas plataformas no modelan la aprobación por canal.
- **Cobranza Stripe end-to-end**: cobros, intentos, reconciliación, conciliación de montos, payouts,
  voucher, alertas y barrido nocturno (`app/cobranza_jobs.py: barrer_todo()`).
- **CSAT básico**: `Feedback(ticket_id, phone, rating)`.
- **Alertas operativas**: `Alert(code, level, metric_value, active, resolved_at)`.
- **Follow-ups con vencimiento**: `FollowUp(due_at, done_at)` + `/my/followups`.
- **Asignación manual completa**: claim / release / reassign / status / finish.
- **Catálogo en DB con auditoría por campo** (`CatalogAudit`) y grid administrable.
- **Bandeja** con bloqueo de contacto y apagado del bot por conversación (`bot-on`).
- **Ciclo de aprendizaje con humano en el centro**: `SynonymProposal` en `pendiente` → `/unmatched`.
- **6 exports CSV** y **espejo MSSQL** de ~22 tablas con conteos comparados en `/system`.

### Modelos clave, tal como están hoy

| Modelo | Campos |
|---|---|
| `Conversation` | `phone, state, context_json, failed_attempts, updated_at, habilitado, provider` |
| `Ticket` | `id, phone, client_name, category, service_id, service_name, price_info, status, escalado, notes, requirements_json, assigned_to, agent_active, created_at, updated_at, habilitado, provider` |
| `Client` | `phone, name, created_at, habilitado` |

---

## 2. Benchmark verificado: Chatwoot

Leído de `chatwoot/chatwoot` (`app/models`, `db/schema.rb`).

### `conversations` — lo que ellos guardan y nosotros no

`first_reply_created_at` · `waiting_since` · `priority` · `sla_policy_id` · `snoozed_until` ·
`cached_label_list` · `team_id` · `custom_attributes` · `additional_attributes` ·
`status_changed_at` · `contact_last_seen_at` · `agent_last_seen_at` · `assignee_last_seen_at` ·
`campaign_id`

Nuestra `Conversation` guarda **estado de la máquina conversacional** y nada de **gestión del
trabajo**. Esa es la diferencia estructural de fondo.

### `contacts` — lo que ellos guardan y nosotros no

`email` · `custom_attributes` · `additional_attributes` · `company_id` · `blocked` · `location` ·
`country_code` · `last_activity_at` · `contact_type` · `identifier`

Con índices únicos `uniq_email_per_account_contact` y `uniq_identifier_per_account_contact` — es
decir, **deduplicación garantizada por la base de datos**.

### Modelos que existen allá y no acá

`label` · `team` + `team_member` · `assignment_policy` + `inbox_assignment_policy` · `working_hour` ·
`campaign` · `csat_survey_response` · `automation_rule` (+ `automation_rule_pending_execution`) ·
`macro` · `custom_attribute_definition` · `custom_filter` · `reporting_event` (+
`reporting_events_rollup`) · `data_import` (+ `data_import_mapping`, `_item`, `_error`) · `webhook` ·
`conversation_participant` · `agent_bot` (+ `agent_bot_inbox`) · `inbox` + `inbox_member` ·
`portal` / `article` / `category` / `folder` (KB pública) · `mention` · `notification_*`

### Panorama de proyectos (consultado en vivo)

| Proyecto | ⭐ | Licencia | Aporte al benchmark |
|---|---|---|---|
| `twentyhq/twenty` | 55.4k | NOASSERTION | CRM moderno "diseñado para IA"; objetos personalizables |
| `chatwoot/chatwoot` | 36.1k | NOASSERTION | **La referencia**: helpdesk omnicanal, esquema completo |
| `botpress/botpress` | 14.9k | MIT | Constructor de agentes LLM |
| `baptisteArno/typebot.io` | 10.3k | NOASSERTION | Constructor visual de flujos conversacionales |
| `evolution-foundation/evolution-api` | 9.4k | NOASSERTION | Integración WhatsApp (no oficial) |
| `frappe/crm` | 3.4k | AGPL-3.0 | CRM completo |
| `espocrm/espocrm` | 3.3k | AGPL-3.0 | CRM clásico |

> Licencias: AGPL-3.0 es **contagiosa para servicios en red** — no portar código de `frappe/crm` ni
> `espocrm` a este repo. En Chatwoot y Typebot, `NOASSERTION` significa que GitHub no pudo clasificar
> la licencia: hay que leer su archivo `LICENSE` antes de reutilizar una sola línea.
> **Las ideas y los esquemas se estudian libremente; el código no se copia.**

---

## 2.5 Política de Meta y economía de mensajería (verificado 2026-08-24)

Fuentes primarias: `developers.facebook.com/documentation/business-messaging/whatsapp/pricing`,
`developers.facebook.com/documentation/business-messaging/whatsapp/pricing/non-template-messages`
y páginas hermanas (`messaging-limits`, `getting-opt-in`). Lo que viene de BSPs/terceros se marca.

> **Trampa metodológica — léela antes de citar esta sección.** Las dos páginas de precios de Meta no
> dicen lo mismo. La **general** (`/pricing`) seguía afirmando el 2026-08-24 que *"Utility templates
> delivered within an open customer service window are free"* y **no menciona** el cambio del
> 1-oct-2026. La **dedicada** (`/pricing/non-template-messages`) es la única que anuncia el cobro
> por mensaje de los mensajes de servicio. Quien verifica solo la página principal concluye lo
> contrario de lo que va a pasar — es exactamente lo que le pasó a la segunda pasada de este
> documento.

### Tarifación — confirmado en doc oficial

- **Por mensaje desde el 1-jul-2025** ("Effective July 1, 2025, Meta charges on a per-message
  basis"). El modelo por conversación está **deprecado**.
- **Hasta el 30-sep-2026** se cobra **solo la entrega de plantillas**, por categoría (MARKETING /
  UTILITY / AUTHENTICATION) y país del destinatario. Desde el 1-oct-2026 se le suman los mensajes
  de servicio (ver más abajo), así que esta frase tiene fecha de vencimiento.
- **Gratis hoy, con fecha de caducidad ya anunciada.** No se cobran los mensajes no-plantilla
  dentro de la ventana de servicio (sin límite desde nov-2024 — el doc oficial NO menciona tope
  mensual; el "1,000 gratis/mes" que circula en blogs no está en la fuente) ni las plantillas
  **utility** respondidas dentro de una ventana abierta (*"have not been charged since July 1,
  2025"*). La tabla de Meta rotula los dos casos como **"Free until October 1, 2026"**.
- **1-oct-2026: se acaba lo gratis dentro de la ventana.** Literal de Meta: *"Effective October 1,
  2026, Meta will charge on a per-message basis for service messages"*. Aplica igual a las
  plantillas de **utilidad** respondidas dentro de la ventana de 24h abierta. Meta publica esas
  tarifas **a más tardar el 1-sep-2026**.
- **Los mensajes de servicio no tienen tramos de volumen** (*"Meta does not offer volume tiers for
  service messages"*); su tarifa por mercado equivale a la de utility/authentication. Su costo
  escala lineal: sin descuento por volumen.
- **La ventana FEP de 72h sigue sin costo de entrega** (click-to-WhatsApp ads). Ese hueco **no** se
  cierra el 1-oct-2026.
- **Meta Business Agent se cobra por token desde el 1-ago-2026.** No lo usamos; queda anotado para
  que nadie lo presupueste con la tarifa por mensaje.
- **Tiers de volumen** para utility/auth: a nivel portfolio, reinician cada mes; webhook
  `VOLUME_BASED_PRICING_TIER_UPDATE` disponible desde oct-2025.
- Los rate cards se actualizan **por trimestre** (hasta 4 veces/año). Referencias de terceros
  (abr-2026): utility/auth desde ~$0.0008 (Colombia) hasta ~$0.055 (Alemania); marketing ~$0.0109
  (Turquía) a ~$0.1597 (Países Bajos). Consultar el rate card vigente de México antes de
  presupuestar — es dato, no constante.
- **Corrección de lo que este documento había dado por refutado (revisado el 2026-08-24).** La
  segunda pasada leyó que "la ventana de servicio gratuita termina el 1-oct-2026 y pasa a cobrarse
  por mensaje" y la marcó **falsa**. **Ese veredicto estaba mal.** Se revisó contra la página
  dedicada (`.../whatsapp/pricing/non-template-messages`) y ahí está, textual: *"Effective October
  1, 2026, Meta will charge on a per-message basis for service messages"*, más las plantillas de
  utilidad respondidas dentro de la ventana abierta. La refutación salió de haber leído solo la
  página general de `pricing`, que a esta fecha sigue sin mencionar el cambio (ver la trampa
  metodológica arriba). No fue un dato de terceros equivocado: fue una verificación incompleta.
  Lo que **sí** era correcto de aquella nota y se conserva: el 1-oct-2026 entra **además** una
  reclasificación tarifaria de países (Bangladesh, Iraq, Nepal, Sri Lanka, Kazajistán, Kuwait,
  Marruecos, Omán, Ucrania), con tarifas finales publicadas a más tardar el 1-sep-2026. Son dos
  cambios distintos que caen el mismo día; el segundo no cancela al primero.

### Ventana de servicio de 24h (consistente en múltiples fuentes)

Cada mensaje entrante del cliente **abre o reinicia** una ventana de 24h. Dentro: mensajes libres
—hoy sin costo, **cobrados uno por uno a partir del 1-oct-2026**—. Fuera: solo plantillas, cobradas
según categoría. Un tap en un anuncio click-to-WhatsApp abre además la FEP de 72h, cuya entrega
sigue sin costo después de esa fecha.

El 1-oct-2026 cambia la **economía**, no la **mecánica**: la ventana seguirá diciendo qué se
*puede* mandar; lo que dejará de decir es que sea gratis. La implementación de esa distinción está
en §6.1 y §6.4.

### Plantillas: rechazo y pausado (fuentes de BSPs, consistentes entre sí)

- Rechazos típicos: **desajuste de categoría** (lenguaje promocional en una utility), formato
  (variables `{{1}}` secuenciales, nunca una variable como primera palabra, valores de ejemplo
  obligatorios), contenido (datos sensibles, urgencia agresiva), duplicados exactos.
- Etiquetar promoción como utility "para ahorrar" → Meta la recategoriza, y la reincidencia trae
  restricciones temporales para crear plantillas utility.
- **Strikes por calidad de plantilla**: 1ª pausa 3h, 2ª pausa 6h, 3ª deshabilitación permanente.
  El webhook `message_template_status_update` avisa en tiempo real.
- Plantillas sin actividad 12 meses → archivadas automáticamente.
- El pausado de plantillas MARKETING hacia números de EE. UU. (desde abr-2025) seguía vigente a
  jul-2026 — irrelevante para MX hoy, relevante si se expande.

### Calidad del número y límites de mensajería

- Los límites aplican **solo a conversaciones iniciadas por el negocio** (plantillas). Responder
  entrantes nunca cuenta ni se bloquea — incluso en estado Restricted se puede contestar.
- Tiers: 250 → 2k → 10k → 100k → ilimitado. Suben solos (evaluación cada 6h) al usar ≥50% del
  límite en 7 días con calidad Media/Alta. Con calidad Baja el número pasa a **Flagged**: si no
  mejora en 7 días, el límite **baja un tier**.
- El quality rating (verde/amarillo/rojo) sale de los últimos 7 días de señales (bloqueos, reportes
  con motivo). En 2026, rojo abre una ventana de corrección en vez de degradar al instante.
- Desde oct-2025 los límites son **por portfolio**: todos los números del Business Manager
  comparten el tier más alto logrado (y añadir números no duplica capacidad).
- Existe un tope por usuario de plantillas marketing recibidas de **todas** las empresas en
  conjunto; Meta no publica la cifra.

### Opt-in (doc oficial "Get opt-in" + BSPs)

- Obligatorio para mensajes iniciados por el negocio. **Un mensaje entrante NO constituye opt-in de
  marketing** — solo abre la ventana de 24h. Un click en anuncio tampoco: consiente la sesión, no
  campañas futuras.
- El opt-in debe fijar la expectativa de qué tipos de mensaje recibirá la persona.
- **Opt-out obligatorio**: vía gratuita y fácil (p.ej. responder "STOP"); Meta añade además un
  control nativo para que el usuario silencie el marketing de un negocio.
- Meta recomienda **guardar evidencia** de cuándo y cómo se dio el consentimiento (auditorías).
  → De ahí que la fase 1 añada `opt_in_at`, `opt_in_source` y `opt_out_at` a `Client`.

### WhatsApp Flows

Formularios nativos dentro del chat (captura de datos, citas, encuestas, intake). Los estáticos se
construyen sin código; los dinámicos (disponibilidad en vivo) requieren endpoint propio. No se
facturan aparte: se paga la plantilla que los entrega. Los BSPs reportan 2-5× la tasa de completado
de un formulario web (cifra de vendor, no de Meta).

---

## 2.6 Mercado: vendors, verticales y dolores de operadores

### El estándar de los grandes (docs de producto + comparativas 2026)

Lo que la integración WhatsApp↔CRM de Zoho/HubSpot/Salesforce/Bitrix24/Odoo da por sentado: bandeja
compartida con **asignación y detección de colisión** (dos agentes en el mismo chat), **notas
internas**, **etiquetado**, enrutamiento por habilidad/idioma, **asignación al dueño del deal** en
el CRM, **escalación por SLA** (p.ej. primera respuesta >15 min → notificar supervisor y
reasignar), colas con salto de fila para VIP, y automatizaciones sin código. Bitrix24 compite por
tarifa plana; Odoo revende los créditos de Meta a tarifa estándar; los grandes exigen la API
oficial.

### Qué critican los usuarios reales de las verticales (G2/Capterra/Trustpilot)

- **Wati**: el patrón #1 es **opacidad de cobro** — markup ~20% sobre Meta, cobro por agente,
  facturas reportadas de 2-5× el precio de lista, y cancelación tortuosa (sin auto-servicio;
  usuarios reportan cargos posteriores a la baja).
- **Respond.io**: caro para equipos chicos ($99+/mes), curva de aprendizaje, aprobación de
  plantillas difícil, y **una conversación cerrada no regresa al agente que la atendía** (queja
  literal — nota directa para nuestra fase 3).
- **Callbell**: latencia y mensajes no entregados, multi-agente débil, bot superficial.
- El markup escondido del BSP es patrón de industria, no defecto de un vendor.

### Dolores de plataforma (Hacker News)

- **#1: suspensión de la cuenta de Meta sin aviso ni apelación efectiva** (hilo de 172 puntos,
  jun-2025) — riesgo existencial para un negocio montado únicamente sobre la Cloud API.
- Markups ocultos de los BSPs (análisis con tracción, jun-2026).

### Lectura para nuestro producto

Dos de los tres dolores dominantes del mercado **ya están resueltos por arquitectura**: (1) sin
markup de BSP en el canal Meta — vamos directo a la Cloud API; (2) el riesgo de suspensión está
mitigado con el doble canal (Twilio) y con datos 100% propios (SQLite + espejo MSSQL). El tercero
(la economía de plantillas) es exactamente la fase 2.

---

## 3. Brechas priorizadas

Impacto = valor para la operación real (triage de trámites con handoff humano).
Esfuerzo = t-shirt, considerando que **no hay Alembic** y que una tabla espejada cuesta 5 sitios.

| # | Brecha | Impacto | Esfuerzo | Fase |
|---|---|---|---|---|
| 1 | `Ticket` sin tiempos (`first_response_at`, `resolved_at`, `status_changed_at`) → no hay SLA ni primera respuesta | **Alto** | S | 1 |
| 2 | Sin etiquetas (`label`) en ticket/cliente → sin segmentación ni filtros transversales | **Alto** | S | 1 |
| 3 | `Client` anémico: sin email, campos personalizados, opt-in, dedup | **Alto** | M | 1 |
| 4 | Sin control de ventana de 24h ni de costo de mensajería | **Alto** | M | 2 |
| 5 | Sin asignación automática (round-robin, carga por agente, reasignación por inactividad) | Medio | M | 3 |
| 6 | Sin horario de atención (`working_hour`) ni ruta de fuera de horario | Medio | S | 3 |
| 7 | Sin motor de reglas (`automation_rule`) | Medio | L | 4 |
| 8 | Sin webhooks salientes (`webhook`) → no hay puerto para un ERP externo | Medio | M | 4 |
| 9 | Sin campañas salientes (`campaign`) | Medio | L | 5 |
| 10 | Reportes limitados a `/activity`: sin embudo por etapa ni productividad por agente | Medio | M | 5 |
| 11 | Sin importación masiva (`data_import`) | Bajo | M | 6 |
| 12 | Sin búsqueda global en el dashboard | Bajo | S | 6 |
| 13 | Sin WhatsApp Flows (formularios nativos) | Bajo* | L | 6 |
| 14 | Sin vistas guardadas (`custom_filter`) ni macros multi-paso | Bajo | M | 6 |
| 15 | Sin equipos (`team`) — hoy la asignación es plana por usuario | Bajo | M | 6 |

\* Flows sube a impacto Alto si el intake de requisitos resulta ser el punto de abandono; hoy no hay
dato que lo respalde. **Medir antes de construir.**

### Lo que deliberadamente NO se propone

- **Que el bot cierre trámites, valide documentos o fije el precio final.** Es la línea que define el
  producto. Ninguna función de este plan la cruza.
- **Auto-aprobación de sinónimos.** El humano en `/unmatched` es un rasgo, no un cuello de botella.
- **Columna `cantidad`.** Se deriva del folio a propósito, para que el espejo MSSQL no se entere.

---

## 4. Roadmap

| Fase | Tema | Contenido | Responsable propuesto | Estado |
|---|---|---|---|---|
| **1** | Medir y clasificar | Tiempos en `Ticket`, etiquetas, `Client` enriquecido | DBA Sr + FullStack Sr | **Implementada** (`cd738cf`) — spec en §5 |
| **2** | Economía de la mensajería | Ventana de 24h y medidor de costo | LLMOps + Backend | **Implementada** — spec en §6 |
| **3a** | Horario y visibilidad | Horario de atención, aviso fuera de horario, carga por asesor — **implementada, §7** | Backend + FullStack | **Implementada** |
| **3b** | Reparto del trabajo | Asignación pegajosa + reparto con tope de carga — **implementada, §7bis** | Backend + FullStack | **Implementada** |
| **4** | Integración y automatización | Webhooks salientes + motor de reglas | Backend + DevOps | Pendiente |
| **5** | Crecimiento y analítica | Campañas + reportes/embudo | AI PM + FullStack | Pendiente |
| **6** | Pulido | Importación, búsqueda global, Flows, vistas, equipos | FullStack + QA | Pendiente |

**La fase 1 va primero porque sin ella no se puede priorizar el resto con datos.** Hoy no sabemos
cuánto tarda una primera respuesta ni qué categoría de trámite se atasca. Construir asignación
automática antes de poder medirla es optimizar a ciegas.

**La fase 2 antes de la 5** porque el envío saliente cuesta dinero y arriesga la calidad del número;
abrir ese grifo sin medidor es imprudente. Ya está implementada (§6) y conviene subrayar qué hizo:
**puso el medidor, no abrió el grifo.** Enviar plantillas fuera de la ventana sigue sin existir en
el código. El calendario le añadió urgencia: el 1-oct-2026 el saliente que hoy es gratis dentro de
la ventana pasa a costar uno por uno (§2.5), y sin la fase 2 ese cambio habría llegado sin ninguna
cifra con qué compararlo.

---

## 5. Especificación de la Fase 1

Objetivo: **poder medir la operación y clasificar el trabajo**, sin tocar la máquina conversacional.

### 5.1 Columnas nuevas en `Ticket`

| Columna | Tipo | Semántica |
|---|---|---|
| `first_response_at` | `datetime \| None` | Primer mensaje **saliente de un humano** en el ticket. No lo llena el bot. |
| `resolved_at` | `datetime \| None` | Cuando `status` pasa a terminal (`finish`). |
| `status_changed_at` | `datetime \| None` | Último cambio de `status`; permite medir tiempo en etapa. |
| `first_response_by` | `str \| None` | `username` del agente que respondió primero. |

Alta de columnas (**no hay Alembic**):

1. Declararlas en la clase `Ticket` de `app/models.py`.
2. Añadirlas al dict de `Ticket` en `_*_COLUMNS_ADDED_AFTER_LAUNCH` de `app/db.py` — las aplica
   `_add_missing_columns()` dentro de `init_db()` vía `ALTER TABLE ... ADD COLUMN`.
3. Añadirlas a `_COLUMNS_ADDED_AFTER_LAUNCH` en `app/archive.py` (la tabla ya está espejada).

> Declararlas solo en el modelo **no hace nada** sobre una base de datos existente.

**Dónde se llenan:**

- `first_response_at` / `first_response_by`: en el envío saliente humano (`/inbox/{phone}/send` y el
  envío desde el detalle de ticket). Condición: `first_response_at is None` **y** el emisor es un
  usuario, no el bot. Una respuesta del bot no cuenta como atención — ese es justamente el número que
  queremos medir.
- `resolved_at`: en `/tickets/{id}/finish`.
- `status_changed_at`: en `/tickets/{id}/status` y en `finish`.

**Retroactivo:** los tickets existentes quedan en `NULL`. No inventar valores desde `created_at`; un
`NULL` honesto es mejor que una métrica falsa. Los reportes excluyen los `NULL` y declaran desde qué
fecha hay datos.

### 5.2 Etiquetas

Dos tablas nuevas → **cinco sitios cada una** en el espejo:

```
Label:       id, nombre (único), color, descripcion, created_at, habilitado
TicketLabel: id, ticket_id, label_id, created_at, created_by
```

Decisión de diseño: tabla puente en vez del `cached_label_list` desnormalizado de Chatwoot. Con
nuestro volumen la unión no duele, y una lista en texto se corrompe sola.

Por cada tabla, las **cinco places** de `app/archive.py` + `app/system.py`:

1. `_SCHEMA_DDL` — el `CREATE TABLE`
2. `archive_label()` / `archive_ticketlabel()` — la función de upsert
3. `_register_listeners()` — enganchar el listener
4. `backfill()` — el bucle idempotente SQLite→MSSQL
5. `MIRROR_TABLES` en `app/system.py` — para que los conteos de `/system` las incluyan

Omitir el paso 5 hace la divergencia **invisible**, que es peor que no espejar.

**UI:** asignar/quitar etiquetas en el detalle de ticket; filtro por etiqueta en el listado. Sin
librerías JS nuevas: `<select multiple>` o checkboxes y un POST. Añadir la columna al export CSV de
tickets.

### 5.3 `Client` enriquecido

| Columna | Tipo | Nota |
|---|---|---|
| `email` | `str \| None` | Normalizar a minúsculas al guardar |
| `notas_internas` | `str \| None` | Ya existe `ClientNote`; este campo es el resumen fijado |
| `origen` | `str \| None` | Canal de adquisición (`meta`, `twilio`, `referido`, `manual`) |
| `opt_in_at` | `datetime \| None` | **Cuándo consintió recibir mensajes.** Prerrequisito de la fase 5 |
| `opt_in_source` | `str \| None` | Cómo se obtuvo el consentimiento — Meta recomienda guardar evidencia para auditorías (§2.5) |
| `opt_out_at` | `datetime \| None` | Cuándo pidió dejar de recibirlos. Se respeta siempre |
| `idioma` | `str` | Default `es-MX` |
| `atributos_json` | `str \| None` | Campos personalizados, sin migración por cada campo nuevo |
| `last_activity_at` | `datetime \| None` | Para ordenar y detectar inactivos |

`atributos_json` es el equivalente pragmático de `custom_attribute_definition` +
`custom_attributes`: un JSON en vez de dos tablas y un motor de definiciones. Se sube a tablas reales
solo si hace falta validar o filtrar por campo — anotar con `# ponytail:` y su vía de mejora.

**`opt_in_at` / `opt_out_at` no son opcionales.** Sin ellos la fase 5 no se puede construir de forma
legítima, y un opt-out ignorado daña la calidad del número. Se registran desde ya, aunque las
campañas lleguen mucho después.

### 5.4 Reporte mínimo que justifica la fase

Una sección nueva en `/activity`, con **SVG generado en el servidor** (`app/activity.py`) — sin
librerías de gráficas en el cliente, esa restricción se mantiene:

- Mediana de primera respuesta, por día (excluyendo `NULL`).
- Tickets por etapa (embudo), con tiempo mediano en cada una.
- Top 10 etiquetas por volumen.

### 5.5 Tests (`pytest`)

Recordar el fixture **autouse** `_isolated_db` y sembrar catálogo propio donde aplique.

- `first_response_at` se llena con el primer envío **humano** y **no** con uno del bot.
- Un segundo mensaje humano **no** sobrescribe `first_response_at`.
- `resolved_at` se llena en `finish`; `status_changed_at` cambia en cada transición.
- `Label` con nombre duplicado se rechaza.
- Quitar una etiqueta borra la fila puente y no el ticket.
- `Client.email` se normaliza a minúsculas.
- `opt_out_at` presente ⇒ el cliente queda excluido de cualquier consulta de audiencia.
- El export CSV de tickets incluye la columna de etiquetas.
- Las tres tablas nuevas aparecen en los conteos de `/system` (protege el paso 5).

Al terminar, `/system` → "Sincronizar ahora" y comparar conteos SQLite vs MSSQL por tabla.

---

## 6. Especificación de la Fase 2 — economía de la mensajería

> **Estado: implementada.** Esta sección documenta código que ya existe, no una propuesta. Objetivo
> de la fase: saber **cuándo se puede escribir** y **cuánto costó lo escrito**, sin cambiar una sola
> decisión de la máquina conversacional. El bot sigue sin fijar precios, validar documentos ni
> cerrar trámites.

### 6.1 `app/ventana.py` — una sola definición de la ventana de 24h

Módulo nuevo, única fuente de verdad de "¿puedo escribirle a este número?":

| Símbolo | Qué contesta |
|---|---|
| `WINDOW_HOURS` | 24. La constante deja de estar copiada en cada módulo |
| `as_utc(dt)` | Normaliza `datetime` naive a UTC antes de restar |
| `abierta(last_in_at)` | `bool` a partir de un instante ya conocido |
| `horas_restantes(last_in_at)` | `float`, 0.0 si cerró o nunca existió |
| `restante_texto(last_in_at)` | `«3 h 12 min»` / `«41 min»` / `«cerrada»`, para la UI |
| `ultimo_entrante(session, phone)` | El último `Message` entrante de ese teléfono |
| `abierta_para(session, phone)` | Atajo para quien solo quiere saber si puede escribir |

**Por qué se DERIVA de `Message` y no hay columna `last_inbound_at`.** El encuadre original de esta
fase (§8) proponía guardar `last_inbound_at` en `Conversation` y leer la ventana de ahí. Se
descartó: es una copia desnormalizada de algo que la tabla `Message` ya contesta, y **una copia que
se puede desincronizar es exactamente el bug que costó el arreglo del espejo en `61ea7c2`** — ahí
los teléfonos crudos que la normalización local nunca le contó al espejo produjeron un conteo
inflado que `/system` seguía llamando "al día". Una columna que se llena en un solo camino de
escritura falla en silencio el día que aparece un segundo camino (un backfill, una importación, un
canal nuevo). El `SELECT` al último entrante no puede mentir: si el mensaje existe, la ventana
existe.

El costo de derivarla es un `SELECT` indexado por pregunta. A este volumen no duele; si algún día
duele, la salida es un índice o una caché de lectura, **no** una columna desnormalizada.

**La ventana es del número, no del folio.** Dos folios del mismo cliente comparten una sola ventana.
Y no cruza canales: cada cuenta —Meta y Twilio— tiene su propio número (`app/messaging.py`), así
que contestar por el otro canal no la reabre.

**Por qué el módulo existe.** La misma pregunta se hacía en cinco lugares con su propio `SELECT`
copiado: bandeja, envío manual, aviso de cierre, aviso de soltado y los dos envíos de cobranza. Un
cambio de criterio tenía que acertarle a los cinco. Ya migrados a este módulo: `app/inbox.py`,
`app/my.py`, `app/dashboard.py` y `app/cobranza_panel.py`.

### 6.2 Las tres columnas de `Message` y la regla de fusión del webhook

| Columna | Tipo | Qué guarda |
|---|---|---|
| `pricing_category` | `str \| None` | `utility`, `marketing`, `authentication`, `authentication-international`, `marketing_lite`, `referral_conversion`, `service` |
| `pricing_type` | `str \| None` | `regular` (**se cobra**), `free_customer_service`, `free_entry_point` |
| `pricing_billable` | `bool \| None` | Bandera que Meta va a deprecar. El veredicto vive en `pricing_type`; esta se guarda mientras la manden, para poder contrastar |

Migración a mano, como manda la casa: las tres entran en
`_MESSAGE_COLUMNS_ADDED_AFTER_LAUNCH` de `app/db.py`, que aplica `_add_missing_columns()` dentro de
`init_db()`. **Nullable y sin `DEFAULT` a propósito** — ver §6.6.

**Quién las llena:** `app/webhook.py::_apply_status`, que copia el objeto `pricing` del webhook de
estado de Meta. `_process_status` se lo pasa entero y sin interpretar: qué se cobra lo decide Meta,
no este código.

**La regla de fusión, que es lo delicado.** `pricing` **no viene en todos los estados**: viaja en el
status `sent` y en **uno** de `delivered`/`read`, nunca en los tres. Por eso `_apply_status` **fusiona
en vez de asignar**:

```python
if pricing:
    row.pricing_category = pricing.get("category") or row.pricing_category
    row.pricing_type = pricing.get("type") or row.pricing_type
    if pricing.get("billable") is not None:
        row.pricing_billable = bool(pricing["billable"])
```

Si asignara directo, el status que llegue después **sin** `pricing` pisaría con `NULL` lo que ya
había guardado el `sent`, y el medidor contaría como "sin dato" un mensaje que Meta sí cobró. El
reporte saldría más barato que la factura, que es la peor dirección posible del error.

Cuando llega un estado cuyo `wa_message_id` no empareja con ninguna fila local, ya no se sale en
silencio: se registra en log. Ese hueco es real y se cuenta (§6.7).

### 6.3 El espejo MSSQL: qué se tocó, y por qué `sync_wa_message` es la línea crítica

Aquí **no aplica** la regla de las cinco places del `CLAUDE.md`: esa es para tablas nuevas.
`wa_messages` ya existía y ya estaba en `MIRROR_TABLES`, así que `app/system.py` no se tocó. Lo que
hubo fue un cambio de **columnas**, en cinco puntos:

1. `_MESSAGE_COLUMNS_ADDED_AFTER_LAUNCH` en `app/db.py` — SQLite.
2. `_SCHEMA_DDL` en `app/archive.py` — el `CREATE TABLE` para instalaciones nuevas.
3. `_COLUMNS_ADDED_AFTER_LAUNCH["wa_messages"]` en `app/archive.py` — el `ALTER TABLE` para la
   instalación ya desplegada. Sin este paso, el punto 2 solo sirve en una base vacía.
4. `archive_wa_message()` — las tres columnas en el `INSERT`.
5. `sync_wa_message()` — las tres columnas en el `UPDATE`. **Esta es la que carga el peso.**

**Por qué el punto 5 no es simetría cosmética.** El precio **nunca** llega en el `INSERT`: cuando la
fila se crea, el mensaje todavía ni se ha enviado (aún no tiene `wa_message_id`). El veredicto de
cobro llega después, en un webhook de estado, es decir **siempre por `UPDATE`**. Sin esa línea el
espejo mostraría `NULL` para siempre mientras SQLite tiene la cifra — y los conteos por tabla de
`/system` seguirían diciendo "al día", porque cuentan **filas**, no columnas. Divergencia invisible:
el modo de falla de `61ea7c2` otra vez, en otra tabla.

### 6.4 `app/medidor.py` — contar lo que Meta dijo, no deducirlo

| Símbolo | Qué hace |
|---|---|
| `RATE_CARD` | El JSON crudo de `app/rate_card.json` |
| `TIPO_COBRABLE` | `"regular"` — el único `pricing_type` que se cobra |
| `tarjeta_vigente(cuando)` | La tarjeta que aplicaba **ese día** |
| `tarifa(categoria, cuando)` | USD por mensaje. `None` = **sin dato**, que no es gratis |
| `resumen(session, desde, hasta)` | Totales del periodo: `salientes`, `con_veredicto`, `sin_dato`, `cobrables`, `gratis`, `por_categoria`, `costo_usd`, `costo_incompleto`, `categorias_sin_tarifa`, `costo_parcial`, `cobertura`, `estimable`, `rate_card`, `rate_cards`, `rate_card_multiple`, `moneda` |
| `serie_diaria(session, desde, hasta)` | Un punto por día: `salientes`, `cobrables`, `sin_dato`. Sale del MISMO recuento que `resumen()` |
| `_sin_veredicto(pricing_type)` | La única definición de «Meta no ha dicho nada»: `pricing_type is None` |

Dos principios sostienen el módulo:

1. **No se adivina lo que Meta ya dijo.** El veredicto se copia y aquí solo se cuenta.
2. **Un total que se lee completo sin estarlo es peor que no tenerlo.** Por eso todo resumen trae
   `sin_dato` al lado, y **dos banderas distintas** para los dos motivos por los que el total puede
   quedarse corto, que no se pueden confundir:
   - `costo_incompleto` — salió algo cobrable de una categoría **sin tarifa** en el rate card. Suma
     a `sin_tarifa` en vez de sumar `0`: sumar cero hace que el total parezca barato justo cuando
     aparece una categoría nueva.
   - `costo_parcial` — hay salientes **sin veredicto** de Meta. Este es hoy el caso normal (todo lo
     anterior al despliegue tiene `pricing` en `NULL`), y antes no encendía ninguna bandera: el
     panel enseñaba un total corto sin una sola advertencia. Va con `cobertura` (qué fracción de
     los salientes trae veredicto) y con `estimable`, que separa «costó cero» de «no se sabe».

   El veredicto es el **tipo**, no la categoría: `_sin_veredicto()` es una sola función compartida
   por `resumen()` y `serie_diaria()`. Cuando la definición vivía duplicada, una fila con categoría
   pero sin tipo caía de lados opuestos —la tabla la contaba como «gratis», la gráfica como «sin
   dato»— y el panel se desmentía a sí mismo.

Detalles de diseño que se sostienen solos: `_conteos()` hace un `GROUP BY` en un round-trip en vez
de traerse decenas de miles de filas; `tarjeta_vigente()` elige **por fecha de entrega** y no "la
última", porque corregir una tarifa de hoy no debe reescribir lo que costó el trimestre pasado.

**Por qué el 1-oct-2026 no exige tocar CÓDIGO — y sí tocar DATO.** El medidor no codifica en ninguna
parte "libre = gratis": pregunta `pricing_type == 'regular'`. Ese día Meta empezará a mandar
`type='regular'` donde hoy manda `free_customer_service`, y el conteo de cobrables sube solo. Eso es
todo lo que el código necesita.

Lo que sí hay que actualizar es el **dato**: la tarjeta con `vigencia_desde: 2026-10-01` de
`app/rate_card.json` trae hoy `"service": null` y `confianza: "pendiente_de_publicacion"`. Mientras
siga así, a partir de esa fecha los mensajes de servicio se contarán **como cobrables sin tarifa**:
entran en `sin_tarifa`, `costo_incompleto` queda en `true` y el reporte lo declara en vez de dar un
total falsamente bajo. Es el comportamiento correcto, pero no es "no hay que hacer nada" — ver §6.5.

### 6.5 El rate card como dato, y lo que el operador tiene que confirmar

Las tarifas viven en `app/rate_card.json`, no en constantes de Python: Meta puede actualizarlas el
primer día de cada trimestre, hasta cuatro veces al año. Cada tarjeta declara `vigencia_desde`,
`pais`, `fuente` y **`confianza`**.

**Hoy `confianza` vale `referencia_sin_confirmar`, y eso hay que leerlo literal: las cifras de
México NO están confirmadas.** Vienen de recopilaciones de BSPs, no del rate card de esta cuenta —
un proveedor consultado publicaba tarifas de India bajo un encabezado de México, y otro seguía
presentándolas "por conversación", un modelo deprecado desde jul-2025. El rate card real se lee en
**WhatsApp Manager** (configuración de la cuenta → facturación / insights). Mientras `confianza` no
diga lo contrario, todo peso se rotula estimado y se dice de dónde salió: igual que el bot nunca le
fija un precio final al cliente, el panel no presenta un costo estimado como si fuera factura.

**Acciones del operador, con fecha:**

1. **Antes de usar cualquier cifra de costo como presupuesto (sin fecha límite, pero es
   prerrequisito):** abrir WhatsApp Manager, copiar el rate card real de MX para `marketing`,
   `utility` y `authentication` a la tarjeta `vigencia_desde: 2025-07-01`, y cambiar `confianza` a
   `confirmado_whatsapp_manager` anotando en `fuente` la fecha de lectura.
2. **1-sep-2026 — fecha límite que Meta se puso a sí misma para publicar las tarifas de octubre:**
   revisar qué publicó, incluida la de `service`, y llenar la tarjeta `vigencia_desde: 2026-10-01`.
   Hasta ese día `service` se queda en `null` a propósito. Recordar que **`service` no tiene tramos
   de volumen** (§2.5): el costo escala lineal con el saliente.
3. **Cada inicio de trimestre:** revisar si Meta movió el rate card. Cambiar una tarifa es editar el
   JSON y desplegar; como el deploy es pull-based, el cambio queda con autor y fecha en git.

`# ponytail:` anotado en el propio archivo: no hay grid de administración para las tarifas. Se
agrega si empiezan a moverse más seguido que por trimestre.

### 6.6 Retroactividad: `NULL` es un dato, no un hueco que rellenar

Los mensajes anteriores al despliegue quedan en `NULL` en las tres columnas. **No se inventan
valores** — ni desde el estado de entrega, ni desde "estaba dentro de la ventana, luego fue gratis".
Es el mismo criterio que la fase 1 aplicó a `first_response_at` (§5.1): un `NULL` honesto vale más
que una métrica falsa.

De ahí que las columnas sean nullable **sin `DEFAULT`**: un `DEFAULT 'free_customer_service'` habría
afirmado, sobre todo el histórico, algo que nadie verificó.

Los reportes excluyen los `NULL` del cálculo **y los declaran**: `sin_dato` viaja al lado del total
en `resumen()` y en cada punto de `serie_diaria()`. Sin ese denominador el reporte no se puede
desmentir, que es exactamente la falla que tenía `/system` cuando llamaba "al día" a un espejo
inflado.

### 6.7 Límites conocidos

Sin maquillar, porque cada uno cambia cómo se lee el número:

- **Twilio no reporta precio.** Ese canal no manda nada equivalente a `pricing`, así que **todo** su
  saliente queda en `sin_dato` para siempre. El costo de Twilio se lee en la consola de Twilio, no
  aquí. Si el reparto entre canales cambia, `sin_dato` crece sin que nada esté roto.
- **No existe `send_template`.** Fuera de la ventana hoy no se puede escribir **en absoluto**: ni
  plantilla ni nada. Lo que el código hace al detectar la ventana cerrada es **no enviar** y
  registrarlo (bandeja, avisos de cierre y de soltado, los dos envíos de cobranza).
- **El emparejamiento de `wamid` es FIFO** (`app/webhook.py::_tag_sent_message`, marcado con
  `# ponytail:`): el estado se pega a la fila saliente sin taggear más vieja de ese teléfono y ese
  proveedor. Con envíos concurrentes al mismo número puede cruzar ids, y un status que no encuentra
  fila se registra y se descarta. Esos mensajes quedan **sin veredicto** y engordan `sin_dato` — que
  es justamente la razón de que ese contador exista.
- **El lector del medidor es la sección «Mensajería y costo» de `/activity`** (`app/activity.py` +
  `templates/activity.html`). La gráfica es **SVG generado en el servidor**, sin librerías JS, como
  el resto del panel. Muestra salientes, cobrables, gratis y `sin dato de cobro` con el mismo peso;
  el costo solo aparece como cifra cuando hay al menos un veredicto —si no, dice «no estimable» en
  vez de un `$0.00` que afirmaría que costó cero—, y va acompañado de la vigencia, el país y la
  confianza declarada del rate card.
- **Cobertura de pruebas:** `tests/test_fase2_medidor.py` cubre la fase; `tests/test_archive.py`
  lleva las tres columnas en el esquema falso de MSSQL. Entre lo cubierto:
  - `abierta()` en el borde exacto de las 24 h, y con `datetime` naive frente a uno con zona.
  - `restante_texto()` en sus cuatro formas (`h + min`, solo `h`, solo `min`, `cerrada`).
  - La fusión de `pricing` en sus tres variantes: un `read` **sin** la llave, un `pricing` **vacío**
    y un `pricing` **parcial** (con `billable` pero sin `category` ni `type`). La tercera es la que
    de verdad ejercita la fusión campo por campo: sin ella, quitar los `or row.pricing_*` dejaba la
    suite entera en verde.
  - `resumen()`: un cobrable sin tarifa suma a `sin_tarifa` y prende `costo_incompleto`, no suma 0.
  - `tarjeta_vigente()` elige por fecha de entrega y no por «la última cargada».
  - Un saliente sin veredicto cae en `sin_dato`, y `sin_dato` cuadra entre el resumen y la gráfica.
  - Un `service` con tipo `regular` —lo que Meta mandará desde el 1-oct-2026— sale cobrable sin
    tocar código.

### 6.8 Qué desbloquea y qué NO

**Desbloquea:**

- Saber, **antes** de escribir, si el chat admite un mensaje libre. Ya está en uso en bandeja, `/my`,
  dashboard y panel de cobranza, con una sola definición detrás.
- Contar el saliente cobrable por categoría y periodo, con su hueco declarado al lado.
- Llegar al 1-oct-2026 con **línea base**: cuánto saliente de servicio hay hoy por día es justamente
  lo que ese día empieza a costar. `serie_diaria()` da esa curva desde el momento en que se desplegó.
- La fase 5 (campañas), cuyo prerrequisito de costo era esta fase.

**NO desbloquea:**

- **Escribir fuera de la ventana.** Sigue faltando `send_template`: catálogo de plantillas aprobadas,
  envío con parámetros, registro de qué plantilla se usó en cada mensaje, y suscripción a
  `message_template_status_update` para enterarse de pausados y rechazos (§2.5).
- **Presupuestar.** Mientras `confianza` diga `referencia_sin_confirmar`, la cifra en dólares es una
  referencia, no un número para meter en un presupuesto (§6.5).
- **Leer nada en pantalla.** No hay panel todavía (§6.7).

**La fase 2 puso el medidor; no abrió el grifo.** Es deliberado: medir primero es lo que permite
decidir con datos si el grifo se abre.

---

## 7. Especificación de la Fase 3a — horario y visibilidad de la carga

La fase 3 se partió en dos entregas porque la mitad de asignación tiene una colisión que hay que
resolver a propósito (§7.5), y la otra mitad no depende de ella ni de tener datos acumulados. Esto
es la **3a**: no toca `Ticket.assigned_to` en absoluto.

### 7.1 El horario vive como dato (`app/horario.json`)

Mismo patrón que `rate_card.json`: es política de negocio, cambia por temporada, y cada cambio debe
quedar con autor y fecha en git. Estructura:

| Llave | Qué es |
|---|---|
| `semana` | `"0"`–`"6"` (0 = lunes, `datetime.weekday()`) → lista de rangos `["HH:MM", "HH:MM"]` |
| `excepciones` | fecha ISO → rangos de ESE día. Lista vacía = cerrado. Manda sobre `semana` |

Varios rangos por día porque la comida es real: `[["09:00","14:00"],["15:00","18:00"]]`. Los rangos
son `[abre, cierra)` — a las 18:00 en punto ya cerró.

Las `excepciones` cargadas son los días de descanso obligatorio de la LFT (art. 74) de lo que resta
de 2026 y de 2027. **No son una afirmación de que el negocio cierre esos días**: hay que revisarlos,
y agregar los propios (puentes, vacaciones, inventario). Un festivo sin cargar hace que el bot
prometa «abrimos a las 9» un 16 de septiembre.

> **ponytail**: sin pantalla de administración. Sube a tabla de SQLite con grid el día que haya que
> cambiar el horario sin desplegar. Hoy el operador ya despliega él mismo y el cron tarda 2 minutos.

### 7.2 `app/horario.py` — una sola definición de «¿hay alguien?»

| Función | Qué contesta |
|---|---|
| `ahora()` | La hora del NEGOCIO. Un solo lugar donde preguntarla |
| `abierto(cuando=None)` | ¿Hay atención humana en ese momento? |
| `proxima_apertura(cuando=None)` | Cuándo vuelve a haber alguien; `None` si no se pudo calcular |
| `aviso(cuando=None)` | Qué decirle al cliente; `None` si está abierto |

La zona horaria se toma de `templating.BUSINESS_TZ`, que ya era la definición única del negocio — no
se declaró una segunda.

**Sin archivo legible, `abierto()` devuelve `True`.** La degradación se eligió a propósito hacia ese
lado: prometer atención que llega tarde molesta; decirle a un cliente que estamos cerrados cuando sí
hay quien lo atienda le hace perder el trámite. Un rango mal escrito se descarta con log y los demás
del día siguen valiendo.

### 7.3 El bot no deja de trabajar fuera de horario — deja de mentir

Fuera de horario el bot sigue recibiendo, deduciendo el trámite y **abriendo el folio igual**. Lo
único que cambia es lo que se promete. Los textos que prometían atención humana lo hacían con la palabra «en breve»:

- `TICKET_ALTA_CIERRE` y `TICKET_ALTA_CIERRE_MULTI` (`app/conversation.py`) — el alta del folio.
- `REQ_CIERRE` — el cierre de la captura de requisitos.
- El `return` de `_escalate()` — «un asesor te contactará en breve», la promesa más explícita.
- `ESCALATED_WAIT_REPLY` — el seguimiento de un cliente ya escalado. **Se me pasó en la primera
  pasada y lo encontró la revisión**: es el punto que más veces se emite de los cinco.

Los cuatro puntos de emisión pasan ahora por `conversation._con_horario()`, que agrega el aviso solo
si está cerrado. Dentro de horario el mensaje queda **byte por byte igual** que antes.

La queja que esto resuelve no es de disponibilidad sino de expectativas: nadie espera que le
contesten a las 3 de la mañana, pero sí espera saber cuándo. Por eso el aviso dice **cuándo** —
«hoy a las 09:00», «el lunes a las 09:00»— y no solo que está cerrado.

### 7.4 Visibilidad de la carga (`activity.carga_abierta`)

Folios vivos (no cerrados, no ocultos) por asesor, en un `GROUP BY`. La llave `None` es el trabajo
sin dueño. Se muestra como columna **«Abiertos»** en la tabla de equipo de `/activity`, con el
conteo de folios sin asesor arriba.

Es la única columna de esa tabla que mira el **presente**: las demás cuentan cierres ya ocurridos.
Y entra en la 3a, no en la 3b, por una razón concreta: hoy «sin asignar» **es** el pendiente y el
equipo lo encuentra con el filtro `?assigned=none` de la lista de folios (`GET /`, no `/tickets`: ahí solo vive el detalle). En cuanto la 3b empiece a repartir
automáticamente, «sin asignar» dejará de ser el pendiente y pasará a ser el desbordamiento del tope
— y el pendiente real será «asignado pero sin tocar», que hoy no tiene vista. Empezar a repartir sin
poder ver el reparto es el punto ciego que esta columna tapa **antes** de abrirlo.

### 7.5 Lo que la 3a NO hace, y la colisión que la 3b tuvo que resolver

> **Resuelta en la 3b (§7bis.6): el dueño es provisional mientras el folio siga en `NUEVO`.**

No se tocó `Ticket.assigned_to`. La asignación sigue 100 % manual: `claim` (atómico), `release`,
`reassign` y `_pause_bot`.

La 3b tiene que decidir **explícitamente** algo que de otro modo rompe en silencio: `_pause_bot`
asigna el folio a quien contesta **solo si `assigned_to is None`** — esa guarda es lo que hace
funcionar la regla «quien responde se queda el chat». Con asignación pegajosa, un cliente que regresa
trae folio con dueño desde el minuto cero, así que la guarda nunca se cumple: si el dueño es A y
contesta B, **todos los reportes le atribuyen el trabajo a A**, incluidos la tabla de equipo y
`first_response_by`. Hay que elegir cuál regla gana y escribirlo en el código, y el folio
auto-asignado necesita su fila en `TicketActivity` diciendo que se asignó solo y por qué — si no, el
histórico muestra un dueño que apareció de la nada.

Segunda decisión de la 3b: un folio auto-asignado nace en `NUEVO` **con dueño**, no en `TOMADO`.
`TOMADO` significa «alguien lo tiene», que sería falso para un folio que nadie ha mirado, y
corrompería el embudo de `operacion_stats`.

### 7.6 Sobre calibrar el tope de carga

El roadmap pide «round-robin con tope de carga». Con ~1 día de datos de la Fase 1, cualquier tope
calibrado sobre esas medianas es ruido: hay que mirar primero la **N** de las tarjetas «Folios con 1ª
respuesta» y «Folios resueltos (con dato)». Lo que sí tiene historial largo es `TicketActivity`.

Cuando se calibre, dos sesgos conocidos de la métrica actual:

1. `primera_respuesta_diaria` agrupa por el día en que **entró** el folio, no en el que se respondió.
   Un folio que llega viernes 23:00 y se atiende lunes 09:00 suma ~4 300 minutos **al viernes**. Eso
   no es un error que haya que tapar: es justamente la señal que dimensiona el horario.
2. `first_response_by` se escribe pero **no lo lee nadie**: la mediana por asesor está a un `GROUP BY`
   de distancia. Igual que `status_changed_at`, que se escribe en tres puntos y cuyo cálculo de
   «tiempo en etapa» todavía no existe.

### 7.7 Pruebas (`tests/test_fase3_horario.py`)

Fija el reloj del negocio con la fixture **autouse** `_horario_de_oficina` del `conftest`: sin ella
el texto esperado del bot dependería de la hora a la que corra la suite —verde a mediodía y rojo de
madrugada— y nadie entendería por qué.

- Bordes: el minuto de apertura cuenta, el de cierre no (`[abre, cierra)`).
- Una `excepción` manda sobre el horario semanal aunque el festivo caiga entre semana.
- `proxima_apertura` salta el fin de semana completo, y salta un festivo.
- Sin archivo legible se asume abierto y no se agrega aviso.
- Un rango corrupto no tumba el resto del día.
- El aviso dice **cuándo**, no solo que está cerrado; y dentro de horario el mensaje no cambia.
- La carga cuenta folios vivos, ignora cerrados y ocultos, y reporta el trabajo sin dueño aparte.
- El panel muestra la columna y los folios sin asesor.

---

## 7bis. Especificación de la Fase 3b — reparto del trabajo

> **Estado: implementada.** Cierra la mitad que la 3a dejó abierta: quién se queda un folio nuevo.
> Sigue sin cruzarse la línea del producto — el bot reparte trabajo entre humanos, no decide
> trámites: no fija precios, no valida documentos ni cierra nada.

Dos reglas y un módulo nuevo, `app/asignacion.py`:

| Símbolo | Qué contesta |
|---|---|
| `elegir(session, phone)` | `(asesor, motivo)` — a quién le toca este folio, o `None` si a nadie |
| `bitacora(session, ticket, motivo)` | Deja la fila de `TicketActivity` que dice que se asignó solo |
| `provisional(ticket)` | ¿El dueño es de reparto y todavía no está ganado? |

Los motivos que devuelve `elegir` —`pegajosa`, `reparto`, `tope`, `fuera-de-horario`,
`sin-asesores`— van al log; no se guardan en columna.

### 7bis.1 Sin esquema nuevo: por qué esta fase no toca el espejo

Ni columna ni tabla. `assignment_policy` sonaba a tabla y es **un número**: una política de una
fila es configuración, no modelo. Y las dos entradas del reparto ya existían:

- **La carga** la cuenta `activity.carga_abierta`, que la 3a construyó exactamente para esto. Un
  segundo contador con su propio criterio de «vivo» es la copia que se desincroniza — el mismo
  argumento que descartó `last_inbound_at` en la fase 2 (§6.1).
- **La pegajosa** se deriva del folio anterior de ese teléfono. «Último asesor del cliente» no
  necesita almacenarse: es un `SELECT ... ORDER BY created_at DESC LIMIT 1`.

Consecuencia directa: cero de los cinco sitios del espejo (§«El MSSQL mirror»). La columna
`assigned_to` ya estaba en el DDL y en el upsert, y como la asignación viaja en el **mismo objeto
ORM** que se commitea al crear el folio, el listener `after_insert` la arrastra sin más. Esto es una
restricción, no una casualidad: si algún día la asignación se hiciera con un `update(Ticket)` de
Core —como ya lo hacen `claim` y `release`— no dispararía el mapper, y `/system` compara **conteos
de filas**, no campos, así que la divergencia sería invisible.

### 7bis.2 La pegajosa manda, y manda incluso saturado

Un cliente que regresa cae con el último asesor que ya lo atendió, mire lo que mire el tope. La
asimetría es deliberada: la queja de los usuarios de la competencia (§2.6) no es esperar, es tener
que volver a contar el caso desde cero. El único filtro es que esa persona siga dada de alta; si ya
no lo está, el cliente cae al reparto normal — no se busca al penúltimo asesor.

Se mira el folio anterior **aunque esté cerrado**: que el trámite pasado haya terminado no borra a
quien lo llevó.

### 7bis.3 El pool de asesores es el que ya existía

`User.habilitado == True`, la misma pregunta que ya contestaba la lista de transferencia del panel.
No se filtra `is_admin` porque **el código no distingue hoy asesor de usuario dado de alta** y
fabricar esa distinción dentro de un módulo de reparto sería una regla nueva escondida. Tampoco se
mira `User.estado`: es autorreportado, tiene un solo escritor y no caduca, así que un «ausente» que
nadie limpia desaparecería del reparto para siempre.

> **ponytail**: si algún día hace falta repartir solo entre quien está de verdad conectado, la señal
> ya existe y es `authlog.active_sessions()` (ventana de 5 minutos, y ya espejada).

### 7bis.4 El reparto solo corre en horario; la pegajosa, siempre

Repartir de madrugada le llena el panel a cada asesor con folios que nadie va a mirar hasta la
mañana y consume el tope con trabajo dormido — y «asignado pero sin tocar» es precisamente el
pendiente que todavía no tiene vista (§7.4). Fuera de horario un cliente sin historial deja su folio
**sin asignar**, que es la cola que el equipo ya sabe mirar con `?assigned=none`.

La pegajosa no tiene ese problema: mandarle a un cliente que regresa el mismo asesor de siempre es
correcto a cualquier hora, y el aviso que la 3a agregó ya le dijo cuándo hay alguien.

Se pregunta `horario.abierto()` en presente —el momento en que entra el folio—, no sobre
`Ticket.created_at`.

### 7bis.5 El tope (`CARGA_MAXIMA_ASESOR`, default 15)

Menos folios vivos gana. Empate: gana quien lleva más tiempo sin recibir trabajo, medido por el
folio más alto que tiene asignado — sin ese desempate el orden sería alfabético y la primera de la
lista cargaría con todo para siempre. Todos al tope ⇒ el folio se queda sin dueño.

> **ponytail**: 15 es un número **elegido, no medido**. §7.6 ya lo advertía: con ~1 día de datos de
> la fase 1 cualquier calibración es ruido. Lo que sí tiene historial largo es `TicketActivity`.
> Barandal 1-200 en `config.py`: un 0 apagaría el reparto en silencio.

Un folio de un mensaje con varios trámites no se reparte por folio: `elegir` se llama **una vez**
antes del bucle y los N folios van al mismo asesor. Eso sobrepasa el tope por N-1 a propósito — un
cliente que pide tres trámites de un jalón es un cliente, no tres.

### 7bis.6 La colisión de §7.5, resuelta: el `status` ya decía quién es provisional

El problema que la 3a dejó anotado: `_pause_bot` asigna el folio a quien contesta **solo si
`assigned_to is None`**, y con folios pre-asignados esa guarda no se cumple nunca. La regla «quien
responde se queda el chat» habría muerto en silencio, acreditándole a un dueño de reparto el trabajo
de quien de verdad contestó — en la tabla de equipo y en `first_response_by`.

La decisión: **el dueño es provisional mientras el folio siga en `NUEVO`.** No hizo falta ninguna
marca nueva porque el `status` ya lo codificaba:

- El folio auto-asignado nace en `NUEVO` **con dueño** (§7.5 lo exigía: `TOMADO` significaría
  «alguien lo tiene», falso para un folio que nadie ha mirado, y corrompería el embudo).
- `claim` y la primera respuesta humana lo pasan a `TOMADO`. A partir de ahí el dueño está **ganado**
  y no se mueve solo.

Así que `_pause_bot` asigna si `assigned_to is None` **o** el folio sigue provisional. Contestar
sigue siendo la forma de quedarse un chat — que además es la única que quedaba: `reassign` exige ser
el dueño y `release` manda un mensaje de cortesía al cliente, así que sin esto un folio pre-asignado
a otra persona no tenía ruta de auto-servicio.

La alternativa que se descartó era «quien responde gana, siempre»: con ella dos asesores que
contestan el mismo chat se lo quitan uno al otro en cada mensaje. Y el despojo de un folio
provisional queda escrito: `tomado-de:{dueño anterior}`.

### 7bis.7 Lo que el cliente ve — y lo que ve el equipo

`STATUS_LABELS[NUEVO]` decía **«recibido, en espera de asignación»**. Con folios que nacen ya con
asesor, eso pasa a ser un enunciado falso enviado por WhatsApp — el bot le miente al cliente sobre
su propio folio. `_status_reply` ahora elige el texto según haya dueño (`recibido, con asesor
asignado`). **Nunca se dice quién**: el nombre del asesor es organización interna.

Del lado del panel, dos textos más estaban a punto de mentirle al equipo:

- El folio ajeno decía **«La está atendiendo ana — solo esa persona puede cambiar el estado»** sobre
  un folio que nadie ha abierto. Ahora, mientras el dueño sea provisional, dice que está repartido,
  que todavía no lo abren y que contestarle al cliente basta para quedárselo — que es justamente la
  ruta de auto-servicio que la fase conserva.
- El histórico de `/activity` imprimía crudos los dos slugs nuevos (`tomado-de:ana` en medio de
  frases en español). `activity._label` los traduce.

### 7bis.8 Lo que la 3b NO hace

- **No arregla `build_conversation_items`** (`app/inbox.py`), cuyo `SELECT` de tickets no filtra
  `habilitado` ni `!= CERRADO` y se queda con el folio más reciente del teléfono. Es un bug
  preexistente que esta fase **amplifica**: de ahí sale el dueño que pinta la insignia de las
  tarjetas y el que llena `/my`, así que con reparto automático empieza a publicar el dueño de
  folios cerrados o borrados. Se deja fuera a propósito para no mezclar, en el mismo entregable, un
  cambio de qué significa la propiedad de un folio con un cambio de qué folios se miran: si algo se
  descompone, no habría forma de saber cuál mitad fue. **Es lo siguiente que hay que tocar.**
- **No reasigna por inactividad** (la brecha #5 lo mencionaba). Sin dato de cuánto tarda hoy una
  primera respuesta por asesor, el umbral sería inventado.
- **No cambia las insignias de dueño** de las cuatro plantillas que lo imprimen. Hoy «sin insignia»
  significaba «nadie lo ha tomado» y ahora significa «no se pudo repartir»; ninguna copy lo dice
  todavía.
- **No toca `claim` ni `release`.** Siguen exactamente igual, con su `UPDATE` atómico.

De `reassign` sí hubo que tocar una línea, y la revisión lo encontró: transferir un folio cambiaba
`assigned_to` **sin tocar el `status`**, así que un folio recién repartido y transferido a propósito
seguía pareciendo provisional y se lo quedaba el primer compañero que le contestara al cliente — el
traspaso deliberado se deshacía con un solo mensaje. Ahora una transferencia saca el folio de
`NUEVO`: transferir **es** trabajo humano, y `TOMADO` ya significa «alguien lo tiene». Queda una
puerta abierta que se deja anotada: `update_ticket_status` acepta regresar un folio a `nuevo`
conservando dueño, y eso lo vuelve provisional otra vez. Para devolver trabajo a la cola la ruta real
es «soltar», que sí limpia `assigned_to`; cerrar el `<select>` es un cambio de flujo que no
corresponde a esta fase.

### 7bis.9 Pruebas (`tests/test_fase3b_asignacion.py`)

El punto de partida fue un hallazgo incómodo: **ninguna prueba del repo verificaba a quién le queda
un folio recién creado.** Las doce afirmaciones sobre `assigned_to` sembraban el folio a mano, y las
dos únicas que lo crean por el flujo real solo miran el `status` — que no cambia. La suite entera
podía quedarse verde mientras producción cambiaba de comportamiento.

- El alta **por `handle_incoming`** deja dueño, nace en `NUEVO`, y deja la fila
  `sistema / asignado-auto:{asesor}`.
- Sin asesores dados de alta, el folio nace sin dueño (degradación segura).
- Dos trámites en un mensaje ⇒ un solo asesor.
- La pegajosa gana aunque el asesor esté saturado; un asesor dado de baja no se queda al cliente.
- El reparto elige al menos cargado; con empate, al que lleva más tiempo sin recibir.
- Todos al tope ⇒ sin asignar, y el desbordamiento sigue contándose bajo la llave `None` de
  `carga_abierta`. Un folio cerrado deja de ocupar lugar.
- Fuera de horario no se reparte, pero la pegajosa sí.
- Quien contesta se queda el folio provisional (`assigned_to`, `first_response_by` y `status`), el
  despojo queda como `tomado-de:ana`, un folio ya trabajado **no** cambia de dueño, y contestar el
  folio propio registra `tomado` sin ensuciar el histórico.
- El bot no dice «en espera de asignación» si ya hay asesor, y no revela su nombre.
- El folio de **escalación** también nace con dueño y con su fila de bitácora.
- Transferir un folio repartido lo saca de la ventana provisional.
- El panel no dice «la está atendiendo» de un folio que nadie ha abierto, y sí lo dice del que ya se
  trabajó.
- Las dos acciones nuevas se leen en español en el histórico.

Ocho mutaciones probadas ponen la suite en rojo: quitar la asignación del alta, devolver
`_pause_bot` a la guarda vieja, ignorar el horario en el reparto, invertir el orden de la pegajosa,
quitar el término de rotación del desempate, dejar que `reassign` no saque de `NUEVO`, quitar la
traducción del histórico y devolver el copy viejo a la vista del folio. Sin esa comprobación, un
archivo de pruebas nuevo no demuestra nada — y de hecho **cuatro de esas ocho mutaciones sobrevivían
a la primera versión de estas pruebas**: la pegajosa se probaba con un solo asesor previo, el
desempate esperaba al ganador que también salía por orden alfabético, y la escalación no se tocaba.

### 7bis.10 Al desplegar

`backfill_closed_tickets` (`app/activity.py`) tiene una salida temprana: **si existe una sola fila
de `TicketActivity`, no vuelve a correr nunca.** Corre al arranque (`app/main.py`), así que en
producción ya se ejecutó hace fases y esto no afecta. Pero en una base nueva, si el primer evento de
la historia fuera un `asignado-auto`, el histórico sintético de cierres previos no se poblaría jamás
y las columnas Hoy/Semana/Mes de la tabla de equipo quedarían mudas para todo lo anterior.

---

## 8. Fases 4-6: notas de encuadre (sin especificar todavía)

> **Fase 2 — ventana de 24h y costo: IMPLEMENTADA. La especificación está en §6.** Del encuadre
> original de esta fase se descartaron dos cosas y conviene que quede escrito por qué: (a) la
> columna `last_inbound_at` en `Conversation` — la ventana se deriva de `Message` (§6.1); (b) el
> supuesto de que "los mensajes libres son gratis dentro de la ventana", que dejará de ser cierto el
> 1-oct-2026 (§2.5). Lo que queda pendiente de aquel encuadre es el envío de plantillas fuera de la
> ventana y la suscripción a `message_template_status_update`, que se mueven a la fase 5.

> **Fase 3b — reparto: IMPLEMENTADA. La especificación está en §7bis.** De este encuadre se
> descartó una cosa y conviene que quede escrito por qué: `assignment_policy` como **tabla**. Una
> política de una fila es configuración (`CARGA_MAXIMA_ASESOR`), no modelo, y una tabla espejada
> cuesta cinco sitios. Lo que queda pendiente de aquel encuadre es la **reasignación por
> inactividad**, que necesita saber primero cuánto tarda hoy una primera respuesta por asesor —
> `first_response_by` se escribe desde la fase 1 y sigue sin que nadie lo lea (§7.6).
- **Fase 4 — webhooks + reglas.** Los webhooks salientes son **el puerto de integración con un ERP
  externo**: entrega firmada, con reintento y backoff, y **nunca** en el camino de la petición (el
  patrón `_safe()` de `app/archive.py` es la referencia). El motor de reglas empieza como
  disparador→condición→acción con un catálogo cerrado de acciones; no un DSL.
- **Fase 5 — campañas. [Política verificada en §2.5.]** Depende de `opt_in_at`/`opt_in_source`
  (fase 1) y del medidor de costo (fase 2). Requisitos duros confirmados: opt-in previo con
  evidencia (un mensaje entrante NO lo es), opt-out tipo "STOP" siempre respetado, plantillas
  MARKETING siempre cobradas, tope por usuario a nivel Meta, y **rampa gradual** — los tiers suben
  solos usando ≥50% del límite con calidad alta, y un blast frío dispara bloqueos que degradan el
  número (el quality rating gatea todo). Hoy solo dos plantillas UTILITY en uso (`cobro_exitoso`,
  `cobro_rechazado`).
  **Ojo con la aritmética de costo a partir del 1-oct-2026 (§2.5):** una campaña ya no cuesta solo
  la plantilla MARKETING. Cada persona que responde abre una ventana de servicio, y desde esa fecha
  **cada mensaje de servicio dentro de la ventana se cobra**, sin tramos de volumen. El costo real
  de una campaña es la plantilla **más la conversación que provoca** — y esa conversación la
  atiende un humano, así que escala con la tasa de respuesta, no con el presupuesto. Antes de
  planear la primera campaña, medir con `medidor.resumen()` cuántos mensajes de servicio consume
  hoy una conversación promedio (§6). Además, esta fase absorbe lo que quedó pendiente de la fase 2:
  `send_template` y la suscripción a `message_template_status_update`.
- **Fase 6 — pulido.** Nada aquí bloquea a nadie; entra cuando haya hueco.

---

## 9. Cómo seguir

1. ~~Completar la investigación pendiente~~ **Hecho (2026-08-24, §2.5-2.6).** Único hueco
   restante: Reddit, bloqueado a nivel de plataforma de forma permanente; se sustituyó con Hacker
   News y reseñas verificadas.
2. ~~Revisar y decidir el alcance de la fase 1~~ **Hecho — fase 1 implementada (`cd738cf`), spec en
   §5.**
3. ~~Fase 2~~ **Implementada, spec en §6.** Lo que queda de ella no es código sino dato y cobertura:
   - **Confirmar el rate card real** de MX en WhatsApp Manager y subir `confianza` (§6.5, acción 1).
     Hasta entonces ninguna cifra en dólares sirve para presupuestar.
   - **Anotar el 1-sep-2026 en el calendario**: es la fecha límite que Meta se puso para publicar las
     tarifas que entran el 1-oct-2026, incluida la de `service` (§6.5, acción 2).
   - **Vigilar la cobertura del medidor**: hoy los salientes de Twilio y los que pierden el
     emparejamiento de `wamid` quedan sin veredicto y engordan `sin dato de cobro`. Si esa fracción
     no baja, el total estimado seguirá siendo parcial y el panel lo dirá (§6.7).
4. ~~Fase 3~~ **Implementada completa: 3a en §7 (`b0fc583`), 3b en §7bis.** Lo que dejó pendiente,
   en orden de urgencia:
   - **Arreglar `build_conversation_items`** (`app/inbox.py`): su `SELECT` de tickets no filtra
     `habilitado` ni `!= CERRADO`, y ahora que casi todo folio tiene dueño eso publica el dueño de
     folios cerrados en las insignias y en `/my` (§7bis.8). Es un cambio de dos líneas que se dejó
     fuera a propósito para no mezclarlo con esta fase.
   - **Calibrar `CARGA_MAXIMA_ASESOR`** contra `TicketActivity` cuando haya semanas de historial;
     hoy el 15 es un número elegido (§7bis.5).
   - **Vigilar el reparto la primera semana**: la columna «Abiertos» de `/activity` es exactamente
     la vista que se construyó para esto (§7.4). Si el trabajo se amontona en una persona, el
     sospechoso es la pegajosa, que ignora el tope a propósito (§7bis.2).
   - **Poner copy a la ausencia de dueño**: «sin insignia» ya no significa «nadie lo ha tomado»
     sino «no se pudo repartir», y ninguna plantilla lo dice.
5. **Siguiente fase a especificar: la 4** (webhooks salientes + motor de reglas). El puerto de
   integración con un ERP externo es lo que desbloquea, y el patrón de entrega ya existe en el
   repo: `_safe()` de `app/archive.py`.
