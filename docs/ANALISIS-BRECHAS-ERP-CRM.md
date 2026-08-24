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
  (`developers.facebook.com`). Una afirmación de terceros que circulaba resultó **falsa** y quedó
  refutada en §2.5 — la verificación no fue teatro.
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

Fuente primaria: `developers.facebook.com/documentation/business-messaging/whatsapp/pricing` y
páginas hermanas (`messaging-limits`, `getting-opt-in`). Lo que viene de BSPs/terceros se marca.

### Tarifación — confirmado en doc oficial

- **Por mensaje desde el 1-jul-2025** ("Effective July 1, 2025, Meta charges on a per-message
  basis"). El modelo por conversación está **deprecado**.
- Se cobra **solo la entrega de plantillas**, por categoría (MARKETING / UTILITY / AUTHENTICATION)
  y país del destinatario.
- **Gratis**: todo mensaje no-plantilla (conversaciones de servicio sin límite desde nov-2024 — el
  doc oficial NO menciona tope mensual; el "1,000 gratis/mes" que circula en blogs no está en la
  fuente), plantillas utility dentro de una ventana de servicio abierta, y todo mensaje dentro de
  una **ventana de punto de entrada gratuito (FEP) de 72h** (click-to-WhatsApp ads).
- **Tiers de volumen** para utility/auth: a nivel portfolio, reinician cada mes; webhook
  `VOLUME_BASED_PRICING_TIER_UPDATE` disponible desde oct-2025.
- Los rate cards se actualizan **por trimestre** (hasta 4 veces/año). Referencias de terceros
  (abr-2026): utility/auth desde ~$0.0008 (Colombia) hasta ~$0.055 (Alemania); marketing ~$0.0109
  (Turquía) a ~$0.1597 (Países Bajos). Consultar el rate card vigente de México antes de
  presupuestar — es dato, no constante.
- **Afirmación refutada**: circulaba que la ventana de servicio gratuita "termina el 1-oct-2026 y
  pasa a cobrarse por mensaje". **Falso** — la doc oficial solo anuncia para esa fecha la
  reclasificación tarifaria de países (Bangladesh, Iraq, Nepal, Sri Lanka, Kazajistán, Kuwait,
  Marruecos, Omán, Ucrania; tarifas finales a más tardar el 1-sep-2026). Ningún cobro de la ventana
  ni de los mensajes de servicio.

### Ventana de servicio de 24h (consistente en múltiples fuentes)

Cada mensaje entrante del cliente **abre o reinicia** una ventana de 24h. Dentro: mensajes libres,
gratis. Fuera: solo plantillas, cobradas según categoría. Un tap en un anuncio click-to-WhatsApp
abre además la FEP de 72h, toda gratis.

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

| Fase | Tema | Contenido | Responsable propuesto |
|---|---|---|---|
| **1** | Medir y clasificar | Tiempos en `Ticket`, etiquetas, `Client` enriquecido | DBA Sr + FullStack Sr |
| **2** | Economía de la mensajería | Ventana de 24h y costo | LLMOps + Backend |
| **3** | Distribución del trabajo | Asignación automática + horario de atención | Backend + FullStack |
| **4** | Integración y automatización | Webhooks salientes + motor de reglas | Backend + DevOps |
| **5** | Crecimiento y analítica | Campañas + reportes/embudo | AI PM + FullStack |
| **6** | Pulido | Importación, búsqueda global, Flows, vistas, equipos | FullStack + QA |

**La fase 1 va primero porque sin ella no se puede priorizar el resto con datos.** Hoy no sabemos
cuánto tarda una primera respuesta ni qué categoría de trámite se atasca. Construir asignación
automática antes de poder medirla es optimizar a ciegas.

**La fase 2 antes de la 5** porque el envío saliente cuesta dinero y arriesga la calidad del número;
abrir ese grifo sin medidor es imprudente.

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

## 6. Fases 2-6: notas de encuadre (sin especificar todavía)

- **Fase 2 — ventana de 24h y costo. [DESBLOQUEADA — política verificada en §2.5.]** El modelo
  cuenta **plantillas entregadas** por categoría y país; los mensajes libres son gratis dentro de la
  ventana. Diseño: `last_inbound_at` en `Conversation` deriva la ventana (24h desde el último
  entrante); un registro de plantillas enviadas con su categoría y costo estimado según el rate
  card vigente (que cambia por trimestre — es dato, no constante). El dashboard le muestra al
  agente si el chat está dentro de la ventana **antes** de escribir, porque de eso depende si el
  mensaje necesita plantilla. Suscribirse a `message_template_status_update` (pausados/rechazos)
  entra aquí o en la fase 5.
- **Fase 3 — asignación y horario.** `assignment_policy` mínima: round-robin entre agentes activos
  con tope de carga. `working_hour` por día de la semana. Fuera de horario el bot debe decirlo, no
  callar. **Asignación pegajosa**: un cliente que regresa cae con su último agente — su ausencia es
  queja literal de usuarios de Respond.io (§2.6) y aquí sale casi gratis (`Ticket.assigned_to` ya
  existe).
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
- **Fase 6 — pulido.** Nada aquí bloquea a nadie; entra cuando haya hueco.

---

## 7. Cómo seguir

1. ~~Completar la investigación pendiente~~ **Hecho (2026-08-24, §2.5-2.6).** Único hueco
   restante: Reddit, bloqueado a nivel de plataforma de forma permanente; se sustituyó con Hacker
   News y reseñas verificadas.
2. **Revisar la fase 1 con los agentes responsables**: `DBA Sr` (columnas y las 5 places),
   `FullStack Developer Sr` (rutas, templates, SVG), `QA Engineer Sr` (batería de pruebas),
   `AI Product Manager` (validar la priorización contra el negocio).
3. **Decidir el alcance de la fase 1**: las tres piezas (tiempos, etiquetas, cliente) son
   independientes y se pueden partir en tres PRs. Los tiempos son el de mayor retorno por línea
   escrita.
