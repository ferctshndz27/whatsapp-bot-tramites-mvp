# Graph Report - .  (2026-07-30)

## Corpus Check
- 117 files · ~123,203 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1975 nodes · 6930 edges · 25 communities detected
- Extraction: 47% EXTRACTED · 53% INFERRED · 0% AMBIGUOUS · INFERRED: 3674 edges (avg confidence: 0.63)
- Token cost: 600,543 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Modelo de datos|Modelo de datos]]
- [[_COMMUNITY_Dominio de cobranza|Dominio de cobranza]]
- [[_COMMUNITY_NLU y catalogo de servicios|NLU y catalogo de servicios]]
- [[_COMMUNITY_Diseno y documentacion|Diseno y documentacion]]
- [[_COMMUNITY_Panel y bandeja de mensajes|Panel y bandeja de mensajes]]
- [[_COMMUNITY_Conversacion del bot|Conversacion del bot]]
- [[_COMMUNITY_Plantillas de cobranza|Plantillas de cobranza]]
- [[_COMMUNITY_Webhook y limites de WhatsApp|Webhook y limites de WhatsApp]]
- [[_COMMUNITY_Pruebas de concurrencia y aplicador|Pruebas de concurrencia y aplicador]]
- [[_COMMUNITY_Plantillas de administracion|Plantillas de administracion]]
- [[_COMMUNITY_Espejo MSSQL|Espejo MSSQL]]
- [[_COMMUNITY_Alertas y salud del sistema|Alertas y salud del sistema]]
- [[_COMMUNITY_Cliente HTTP de Stripe|Cliente HTTP de Stripe]]
- [[_COMMUNITY_Bitacora de accesos|Bitacora de accesos]]
- [[_COMMUNITY_Pruebas end-to-end|Pruebas end-to-end]]
- [[_COMMUNITY_Actividad y metricas|Actividad y metricas]]
- [[_COMMUNITY_Consistencia del espejo|Consistencia del espejo]]
- [[_COMMUNITY_Migraciones de esquema|Migraciones de esquema]]
- [[_COMMUNITY_Gestion de usuarios|Gestion de usuarios]]
- [[_COMMUNITY_Identidad de marca CSE|Identidad de marca CSE]]
- [[_COMMUNITY_Aislamiento de pruebas|Aislamiento de pruebas]]
- [[_COMMUNITY_Dependencia Anthropic SDK|Dependencia: Anthropic SDK]]
- [[_COMMUNITY_Dependencia RapidFuzz|Dependencia: RapidFuzz]]
- [[_COMMUNITY_Dependencia bcrypt|Dependencia: bcrypt]]
- [[_COMMUNITY_Dependencia itsdangerous|Dependencia: itsdangerous]]

## God Nodes (most connected - your core abstractions)
1. `Ticket` - 216 edges
2. `Message` - 183 edges
3. `TicketStatus` - 168 edges
4. `Cobro` - 157 edges
5. `Direction` - 146 edges
6. `User` - 142 edges
7. `Conversation` - 120 edges
8. `Client` - 116 edges
9. `get_session()` - 113 edges
10. `TicketActivity` - 99 edges

## Surprising Connections (you probably didn't know these)
- `_sinonimos_de_curp()` --calls--> `get_session()`  [INFERRED]
  tests/test_authz.py → app/db.py
- `test_duplicate_webhook_message_ignored()` --calls--> `handle_incoming()`  [INFERRED]
  tests/test_claim.py → app/conversation.py
- `Consistencia estructural del espejo MSSQL: los CINCO lugares.  REGLA DE NEGOCIO:` --uses--> `User`  [INFERRED]
  tests/test_mirror_consistency.py → app/models.py
- `(tabla protegida por el guard, tabla que el bloque realmente crea).` --uses--> `User`  [INFERRED]
  tests/test_mirror_consistency.py → app/models.py
- `Tablas del espejo nombradas en algún literal de cadena del nodo.      Cubre tant` --uses--> `User`  [INFERRED]
  tests/test_mirror_consistency.py → app/models.py

## Hyperedges (group relationships)
- **Defensa en profundidad contra el cobro duplicado** — diseno_cobranza_stripe_llave_cobro_vivo, diseno_cobranza_stripe_ux_cobro_vivo, diseno_cobranza_stripe_llave_generacion, diseno_cobranza_stripe_clave_idempotencia, diseno_cobranza_stripe_metadata_dos_veces, diseno_cobranza_stripe_ux_intento_vivo, diseno_cobranza_stripe_intentos_huerfanos [EXTRACTED 1.00]
- **Ciclo del pago diferido de OXXO, de la ficha al cobro** — diseno_cobranza_stripe_oxxo, diseno_cobranza_stripe_estado_pendiente, diseno_cobranza_stripe_absorber_datos, diseno_cobranza_stripe_completar_voucher, diseno_cobranza_stripe_voucher_expira_at, diseno_cobranza_stripe_reconciliar_pendientes, diseno_cobranza_stripe_estado_pagado [EXTRACTED 1.00]
- **Webhook fail-closed de Stripe** — diseno_cobranza_stripe_stripe_webhook_secret, diseno_cobranza_stripe_verificacion_de_firma_fail_closed, diseno_cobranza_stripe_tolerancia_temporal_300s, diseno_cobranza_stripe_ramificacion_200_503, diseno_cobranza_stripe_stripe_event_id, diseno_cobranza_stripe_alerta_cobro_desfase_reloj, diseno_cobranza_stripe_reloj_ntp_vps [EXTRACTED 1.00]
- **Cobranza dentro del folio** — ticket_detail_detalle_de_solicitud, cobro_parcial_de_cobranza, cobro_modo_lectura, cobro_modo_formulario, cobro_post_tickets_id_cobro, ticket_detail_avisos_err_ok [EXTRACTED 1.00]
- **Conciliacion del dinero cobrado** — cobros_pagina_cobros, payouts_pagina_depositos_del_banco, cobros_export_conciliacion_csv, payouts_barrido_nocturno, cobros_columna_neto, payouts_sin_conciliar [INFERRED 0.85]
- **El cliente paga fuera del panel** — cobro_retorno_pagina_de_retorno, cobro_retorno_no_afirma_pago_aplicado, cobro_formas_de_pago, cobro_copiar_link_de_pago, cobro_post_cobros_id_enviar [INFERRED 0.80]
- **Administración de accesos y colaboradores** — users_page, invitations_page, logins_page, audit_page [INFERRED 0.82]
- **Ciclo de aprendizaje de sinónimos del agente** — unmatched_clusters_frecuencia, unmatched_mineria_claude, unmatched_aprobar_propuesta, unmatched_agregar_sinonimo, catalog_sinonimos_servicio [INFERRED 0.80]
- **Gráficas SVG renderizadas en el servidor** — activity_grafica_linea, activity_donut_colaboradores, unmatched_grafica_linea, unmatched_donut_capas [INFERRED 0.85]

## Communities

### Community 0 - "Modelo de datos"
Cohesion: 0.05
Nodes (243): Histórico de actividad por colaborador.  Cada acción sobre solicitudes (tomar, c, Cierres por día (horario de negocio) entre start y end inclusive.      Devuelve, Curva suave (Catmull-Rom → Bézier) por los puntos de la serie., Geometría SVG de la gráfica de líneas (una serie por colaborador)., Dona de distribución por colaborador en el rango (como la referencia)., {colaborador: {"up": n, "down": n}} — opiniones 👍/👎 de clientes sobre     ticket, Puebla el histórico una sola vez con los cierres previos a esta función.      Lo, Inicios de hoy/semana/mes en horario de negocio, convertidos a UTC naive     (co (+235 more)

### Community 1 - "Dominio de cobranza"
Cohesion: 0.03
Nodes (202): absorber_datos(), _actividad(), alerta(), _contar(), Reglas de salud de la cobranza, para `app.alerts.current_rules`.  TODAS las regl, (code, level, title, detail, metric_value) por cada regla que dispara., reglas_cobranza(), _aplicar_a_cobro() (+194 more)

### Community 2 - "NLU y catalogo de servicios"
Cohesion: 0.02
Nodes (158): log_admin(), Registro de acciones administrativas en base de datos.  Módulo mínimo y sin depe, _admin_or_redirect(), _apply(), catalog_view(), category_create(), category_rename(), category_toggle() (+150 more)

### Community 3 - "Diseno y documentacion"
Cohesion: 0.02
Nodes (170): absorber_datos (absorcion != transicion), Alerta cobro:cancelacion-pendiente, Alerta cobro:confirmado-sin-stripe, Alerta cobro:desfase-reloj, Alerta cobro:disputa, Alerta cobro:doble-pago, Alerta cobro:eventos-sin-procesar, Alerta cobro:intento-irresoluble (+162 more)

### Community 4 - "Panel y bandeja de mensajes"
Cohesion: 0.04
Nodes (140): is_admin(), close_session(), add_client_note(), client_profile(), _current_user(), delete_client_note(), add_ticket_note(), _append_note() (+132 more)

### Community 5 - "Conversacion del bot"
Cohesion: 0.04
Nodes (124): requisitos_del_ticket(), _already_processed(), _asks_info(), _asks_status(), _bump_or_escalate(), _captura_caducada(), _confirm_message(), _confirm_multi_message() (+116 more)

### Community 6 - "Plantillas de cobranza"
Cohesion: 0.02
Nodes (122): Badge de propuestas por revisar, Barra lateral de navegacion, block content (area privada), block content_public (sin sesion), Hojas de estilo del panel (tokens, layout, components), is_admin(user), /catalog (Catalogo), /logout (+114 more)

### Community 7 - "Webhook y limites de WhatsApp"
Cohesion: 0.04
Nodes (93): _menu_rows(), presentation(), Valida y guarda un archivo subido.      Devuelve (ruta_web, ruta_disco, None) o, save_upload(), _current_user(), team_chat(), team_delete_message(), team_send() (+85 more)

### Community 8 - "Pruebas de concurrencia y aplicador"
Cohesion: 0.06
Nodes (101): aplicar_evento(), Session, _alertas(), _cobro(), _evento(), _meta(), Monotonía: expired (created=100) llegando DESPUÉS de succeeded     (created=90), test_20_evento_sin_ids_no_engancha_la_fila_equivocada() (+93 more)

### Community 9 - "Plantillas de administracion"
Cohesion: 0.04
Nodes (79): Donut de distribución por colaborador, Filtro GET /activity por rango desde/hasta, Gráfica de líneas SVG de despachadas por día, Contexto recent con el histórico de acciones, Página Actividad, Ruta /activity?user= (histórico por colaborador), Ruta /tickets/<id> (detalle de solicitud), Contexto stats: día, semana, mes y total (+71 more)

### Community 10 - "Espejo MSSQL"
Cohesion: 0.08
Nodes (62): archive_activity(), archive_admin_audit(), archive_alert(), archive_catalog_audit(), archive_catalog_category(), archive_catalog_service(), archive_client_note(), archive_cobro() (+54 more)

### Community 11 - "Alertas y salud del sistema"
Cohesion: 0.05
Nodes (58): active_alerts(), current_rules(), evaluate_alerts(), health_level(), _no_entendidos_stats(), _pending_proposals(), Fase 4b — Salud del agente.  Evalúa reglas simples sobre el estado del sistema y, Corre las reglas y sincroniza la tabla Alert: upsert de las que disparan,     re (+50 more)

### Community 12 - "Cliente HTTP de Stripe"
Cohesion: 0.06
Nodes (49): _fetch_geo(), conciliar_montos(), _consultar_instrumento(), buscar_payment_intents_por_metadata(), buscar_sesiones_por_ventana(), cancelar_payment_intent(), crear_checkout_session(), crear_customer() (+41 more)

### Community 13 - "Bitacora de accesos"
Cohesion: 0.09
Nodes (49): active_sessions(), _apply_geo(), _as_utc(), _cached_geo(), client_ip(), export_logins(), geo_hint(), _is_public_ip() (+41 more)

### Community 14 - "Pruebas end-to-end"
Cohesion: 0.18
Nodes (36): _button_reply(), _conv(), _db(), _envelope(), _list_reply(), _media_msg(), _messages(), _next_in_id() (+28 more)

### Community 15 - "Actividad y metricas"
Cohesion: 0.13
Nodes (33): activity_view(), _as_naive_utc(), backfill_closed_tickets(), build_donut(), build_line_chart(), daily_dispatches(), dispatch_counts(), feedback_counts() (+25 more)

### Community 16 - "Consistencia del espejo"
Cohesion: 0.07
Nodes (31): _bloques_ddl(), _listeners(), _llamadas(), Consistencia estructural del espejo MSSQL: los CINCO lugares.  REGLA DE NEGOCIO:, Modelo SQLModel -> funciones que dispara su listener en _register_listeners()., REGLA: el guard IF OBJECT_ID debe nombrar la misma tabla del CREATE TABLE., REGLA: una lista blanca que apunta a tablas fantasma deja de proteger.      Si a, REGLA: sin archive_<algo>() nadie escribe esa tabla; queda vacía para siempre. (+23 more)

### Community 17 - "Migraciones de esquema"
Cohesion: 0.1
Nodes (20): _add_missing_columns(), init_db(), bd_de_lanzamiento(), bd_migrada(), _columnas(), _engine_de_archivo(), _indices(), Migración manual de columnas e idempotencia de init_db().  Este repo NO usa Alem (+12 more)

### Community 18 - "Gestion de usuarios"
Cohesion: 0.23
Nodes (22): _admin_or_redirect(), CRUD de colaboradores — accesible solo para administradores.  Alta, reset de con, Devuelve el username admin, o una redirección si no hay sesión/rol., _render_list(), users_create(), users_list(), users_reset_password(), users_toggle_enabled() (+14 more)

### Community 19 - "Identidad de marca CSE"
Cohesion: 0.17
Nodes (20): Formato de banner ancho apaisado propio de imagen Open Graph para redes sociales, Logotipo CSE: siglas en mayusculas dorado ambar, sans-serif bold, alineado a la izquierda, AMBIGUO: el dominio esaumillan.cloud no coincide con las siglas CSE; relacion marca-dominio no verificable desde la imagen, Razon social desplegada: CONSULTORIA Y SOLUCIONES / EMPRESARIALES, Fondo degradado casi negro con vineteado sutil, Dominio del sitio mostrado: esaumillan.cloud, Barra de acento dorada en degradado al borde inferior de la imagen, Paleta dorado/ambar como color de acento unico sobre fondo oscuro (+12 more)

### Community 20 - "Aislamiento de pruebas"
Cohesion: 0.33
Nodes (4): _isolated_db(), Ninguna prueba puede depender del .env del desarrollador.      `app.config` hace, Cada test corre contra su propia base en memoria: el catálogo ahora     vive en, _stripe_en_modo_prueba()

### Community 24 - "Dependencia: Anthropic SDK"
Cohesion: 1.0
Nodes (1): anthropic>=0.92.0

### Community 25 - "Dependencia: RapidFuzz"
Cohesion: 1.0
Nodes (1): rapidfuzz>=3.0

### Community 26 - "Dependencia: bcrypt"
Cohesion: 1.0
Nodes (1): bcrypt==4.2.1

### Community 27 - "Dependencia: itsdangerous"
Cohesion: 1.0
Nodes (1): itsdangerous==2.2.0

## Ambiguous Edges - Review These
- `Regla de oro: el bot nunca genera un cobro` → `Cotizaciones y pagos`  [AMBIGUOUS]
  landing/terminos.html · relation: conceptually_related_to
- `Filtro todos=1 (ver los dos modos)` → `Badge modo prueba (livemode false)`  [AMBIGUOUS]
  templates/_cobro.html · relation: conceptually_related_to
- `Contenido parametrizado (titulo, icono, mensaje, nota)` → `Avisos de resultado (err / ok)`  [AMBIGUOUS]
  templates/cobro_retorno.html · relation: conceptually_related_to
- `Código de invitación de un solo uso` → `Registro en /register con el código`  [AMBIGUOUS]
  templates/invitations.html · relation: references
- `Logotipo CSE: siglas en mayusculas dorado ambar, sans-serif bold, alineado a la izquierda` → `AMBIGUO: el dominio esaumillan.cloud no coincide con las siglas CSE; relacion marca-dominio no verificable desde la imagen`  [AMBIGUOUS]
  landing/og-image.png · relation: conceptually_related_to
- `Linea de contacto: WhatsApp +52 56 5861 1307` → `AMBIGUO: agrupacion de digitos del telefono (+52 56 5861 1307) dificil de segmentar con certeza a partir del render`  [AMBIGUOUS]
  landing/og-image.png · relation: conceptually_related_to
- `Dominio del sitio mostrado: esaumillan.cloud` → `AMBIGUO: el dominio esaumillan.cloud no coincide con las siglas CSE; relacion marca-dominio no verificable desde la imagen`  [AMBIGUOUS]
  landing/og-image.png · relation: conceptually_related_to

## Knowledge Gaps
- **179 isolated node(s):** `Red de no-degradación del NLU contra el CATÁLOGO REAL de producción.  Este archi`, `Una frase del corpus y lo que el negocio espera de ella.      tipo:            "`, `id legible para pytest: al romperse, el reporte dice qué frase se degradó.`, `Cascada 100% determinista y offline.      - anthropic_api_key vacía: si el opera`, `Fuerza la siembra del catálogo real.      El autouse _isolated_db de conftest de` (+174 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Dependencia: Anthropic SDK`** (1 nodes): `anthropic>=0.92.0`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Dependencia: RapidFuzz`** (1 nodes): `rapidfuzz>=3.0`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Dependencia: bcrypt`** (1 nodes): `bcrypt==4.2.1`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Dependencia: itsdangerous`** (1 nodes): `itsdangerous==2.2.0`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `Regla de oro: el bot nunca genera un cobro` and `Cotizaciones y pagos`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **What is the exact relationship between `Filtro todos=1 (ver los dos modos)` and `Badge modo prueba (livemode false)`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **What is the exact relationship between `Contenido parametrizado (titulo, icono, mensaje, nota)` and `Avisos de resultado (err / ok)`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **What is the exact relationship between `Código de invitación de un solo uso` and `Registro en /register con el código`?**
  _Edge tagged AMBIGUOUS (relation: references) - confidence is low._
- **What is the exact relationship between `Logotipo CSE: siglas en mayusculas dorado ambar, sans-serif bold, alineado a la izquierda` and `AMBIGUO: el dominio esaumillan.cloud no coincide con las siglas CSE; relacion marca-dominio no verificable desde la imagen`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **What is the exact relationship between `Linea de contacto: WhatsApp +52 56 5861 1307` and `AMBIGUO: agrupacion de digitos del telefono (+52 56 5861 1307) dificil de segmentar con certeza a partir del render`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **What is the exact relationship between `Dominio del sitio mostrado: esaumillan.cloud` and `AMBIGUO: el dominio esaumillan.cloud no coincide con las siglas CSE; relacion marca-dominio no verificable desde la imagen`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._