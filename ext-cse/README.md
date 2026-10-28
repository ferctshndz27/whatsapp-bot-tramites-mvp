# ext-cse

Extensión de Chrome y Firefox (WebExtension MV3) que muestra el CRM del bot en un panel lateral junto a
WhatsApp Web. No inyecta nada en la página de WhatsApp ni lee sus mensajes.

El plan de desarrollo vive en `02-DOCS/wiki/ftd/ext-cse.md` (local, no versionado).

## Armar

    python3 ext-cse/construir.py --crm https://bot.esaumillan.cloud

Deja `ext-cse/dist/chrome/` y `ext-cse/dist/firefox/` (no se versionan). `--crm` es el único sitio que la
extensión puede tocar: va a `homepage_url` (el origen exacto, del que el código saca sus direcciones) y a
`host_permissions`; el código no conoce otro dominio. En Firefox el permiso va sin puerto, porque Firefox no
admite puertos en los patrones y con puerto no coincidiría con nada. Debe ser https; http solo se acepta
para `localhost`/`127.0.0.1`, para probar contra un bot local.

| Carpeta | Qué es |
|---|---|
| `manifiesto/` | `comun.json` y una capa por navegador. Una clave vive en un solo archivo; el CRM no se escribe a mano. |
| `src/` | El código que viaja. Ningún archivo nombra una URL. |
| `prueba/` | Los jueces de Chrome y Firefox reales. |
| `spikes/` | Pruebas descartables de la fase 1 (S1). Locales: no se versionan ni viajan en el paquete. |

Permisos:

- Chrome: `sidePanel` y `declarativeNetRequest`. El bot prohíbe que lo enmarquen (`X-Frame-Options` y
  `frame-ancestors 'none'`); la regla de `regla-marco.json` lo permite solo en el marco del panel: quita
  X-Frame-Options y pone la CSP del bot —leída de `app/main.py` al armar— con `frame-ancestors` limitado
  al id de la extensión. Fuera del panel el CRM sigue sin poder enmarcarse.
- Firefox: ninguno. Firefox no deja enmarcar el CRM ni con regla (spike S1), así que el panel lateral, tras
  sus comprobaciones, navega a la página del CRM misma. Declara que no transmite datos, lo que exige
  Firefox 140.

## El panel

`panel.html` no abre el CRM a ciegas. Antes comprueba, en este orden:

| Si… | El panel dice | Y vuelve a intentarlo |
|---|---|---|
| no hay red (`navigator.onLine`) | «Sin conexión a internet» | solo, en cuanto vuelve la red |
| la persona le quitó el permiso de sitio | «Falta el permiso para conectarse con el CRM», con el botón «Dar permiso» | solo, en cuanto vuelve el permiso |
| `/health` no contesta | «No se pudo conectar con el CRM» | cada 5 s, o con «Reintentar» |
| `/health` contesta con error (p. ej. 502/503 durante un deploy) | «El CRM no está disponible», con el código | cada 5 s, o con «Reintentar» |

Si todo está en orden, abre `/crm`. Sin sesión, el CRM muestra su propio login. En Chrome, antes de crear
el marco, le pide al fondo que asegure la regla: en el primer arranque el panel puede abrirse antes de que
`onInstalled` la ponga, y un marco que sale antes que la regla queda bloqueado. Si le quitan el permiso con
el CRM ya abierto, el panel vuelve a empezar, porque sin permiso la regla deja de aplicar.

## Probar

    .venv/bin/python -m pytest tests/test_ext_cse_manifiesto.py -q   # manifiestos, regla y construir.py
    node ext-cse/prueba/juez-chrome.mjs                               # Chrome real, sin cabeza
    python3 ext-cse/prueba/juez-firefox.py                            # Firefox real, sin cabeza
    python3 ext-cse/construir.py --crm https://bot.esaumillan.cloud && npx web-ext lint -s ext-cse/dist/firefox

Los dos jueces levantan el bot con una base temporal sembrada (`scripts/qa_crm_seed.py`), instalan la
extensión en un perfil temporal y recorren el panel: el CRM en mantenimiento (503) y luego caído, el CRM
que vuelve (login y, tras entrar, `/crm`), sin red y sin permiso de sitio. El de Chrome comprueba además
la regla del marco (solo deja enmarcar a la extensión, y aguanta dos instalaciones a la vez) y que Chrome
no reporte errores ni advertencias. El de Firefox abre el panel lateral de verdad y lo lee desde el
contexto «chrome» de Marionette: necesita `geckodriver` (el de snap trae uno); con el Firefox de snap, la
extensión se arma dentro de `ext-cse/dist/`, porque snap no lee carpetas ocultas de HOME ni el `/tmp` del
sistema.

Para probarla a mano: Chrome → `chrome://extensions` → modo desarrollador → «Cargar descomprimida» →
`ext-cse/dist/chrome`. Firefox → `about:debugging` → «Este Firefox» → «Cargar complemento temporal» →
`ext-cse/dist/firefox/manifest.json`.
