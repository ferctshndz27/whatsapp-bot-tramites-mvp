# ext-cse

Extensión de Chrome y Firefox (WebExtension MV3) que muestra el CRM del bot en un panel lateral junto a
WhatsApp Web. No inyecta nada en la página de WhatsApp ni lee sus mensajes.

El plan de desarrollo vive en `02-DOCS/wiki/ftd/ext-cse.md` (local, no versionado).

## Armar

    python3 ext-cse/construir.py --crm https://bot.esaumillan.cloud

Deja `ext-cse/dist/chrome/` y `ext-cse/dist/firefox/` (no se versionan). `--crm` es el único sitio que la
extensión puede tocar: va a `host_permissions` y de ahí lo lee el código, que no conoce otro dominio. Debe
ser https; http solo se acepta para `localhost`/`127.0.0.1`, para probar contra un bot local.

| Carpeta | Qué es |
|---|---|
| `manifiesto/` | `comun.json` y una capa por navegador. Una clave vive en un solo archivo; el host no se escribe a mano. |
| `src/` | El código que viaja. Ningún archivo nombra una URL. |
| `prueba/` | El juez de Chrome real. |
| `spikes/` | Pruebas descartables de la fase 1 (S1). Locales: no se versionan ni viajan en el paquete. |

Permisos:

- Chrome: `sidePanel` y `declarativeNetRequest`. El bot prohíbe que lo enmarquen (`X-Frame-Options` y
  `frame-ancestors 'none'`); la regla de `regla-marco.json` lo permite solo en el marco del panel: quita
  X-Frame-Options y pone la CSP del bot —leída de `app/main.py` al armar— con `frame-ancestors` limitado
  al id de la extensión. Fuera del panel el CRM sigue sin poder enmarcarse.
- Firefox: ninguno. Firefox no deja enmarcar el CRM ni con regla (spike S1), así que el panel lateral ES
  la página del CRM (`sidebarAction.setPanel`). Declara que no transmite datos, lo que exige Firefox 140.

## Probar

    .venv/bin/python -m pytest tests/test_ext_cse_manifiesto.py -q   # manifiestos, regla y construir.py
    node ext-cse/prueba/juez-chrome.mjs                               # Chrome real, sin cabeza
    python3 ext-cse/construir.py --crm https://bot.esaumillan.cloud && npx web-ext lint -s ext-cse/dist/firefox

El juez levanta el bot con una base temporal sembrada (`scripts/qa_crm_seed.py`), instala la extensión en
un perfil temporal y comprueba que el panel muestra el login y, tras entrar, `/crm`; que la regla solo
deja enmarcar a la extensión, y que Chrome no reporta errores ni advertencias del manifiesto.

Para probarla a mano: Chrome → `chrome://extensions` → modo desarrollador → «Cargar descomprimida» →
`ext-cse/dist/chrome`. Firefox → `about:debugging` → «Este Firefox» → «Cargar complemento temporal» →
`ext-cse/dist/firefox/manifest.json`.
