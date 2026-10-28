# ext-cse

Extensión de Chrome y Firefox (WebExtension MV3) que muestra el CRM del bot en un panel lateral junto a
WhatsApp Web. No inyecta nada en la página de WhatsApp ni lee sus mensajes.

El plan de desarrollo vive en `02-DOCS/wiki/ftd/ext-cse.md` (local, no versionado).

## Armar

    python3 ext-cse/construir.py --crm https://bot.esaumillan.cloud

Deja `ext-cse/dist/chrome/` y `ext-cse/dist/firefox/`, y junto a ellas sus paquetes (ver «Empaquetar»); nada
de `dist/` se versiona. `--crm` es el único sitio que la
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

## Empaquetar

El mismo `construir.py` deja junto a cada carpeta su paquete, `ext-cse/dist/ext-cse-<navegador>-<versión>.zip`
(la versión es la del manifiesto), e imprime su SHA-256. El zip trae exactamente lo de la carpeta, con
`manifest.json` en la raíz, y el mismo código da el mismo zip byte por byte: el SHA-256 dice si lo que alguien
tiene instalado es lo que se revisó. Al armar se borra el zip de otra versión, para que no se instale por error.

El de Firefox trae los mismos archivos, con los mismos bytes, que `web-ext build -s ext-cse/dist/firefox`
(comparados al cerrar E5; solo cambian la compresión y las fechas), así que no hace falta npm para armarlo.
`web-ext lint` lo revisa igual que a la carpeta.

Los paquetes no se firman ni se publican. Lo que eso significa al instalar:

- Chrome no instala un zip fuera de su tienda: se descomprime en una carpeta que se quede ahí y se carga con
  «Cargar descomprimida» (abajo). Chrome la lee de esa carpeta cada vez que arranca.
- Firefox, en su versión normal, solo la instala como complemento temporal: se va al cerrar Firefox y hay que
  volver a cargarla. Para que se quede hace falta que Mozilla la firme (AMO, aunque sea sin listarla en la
  tienda) o usar Firefox ESR, Developer Edition o Nightly con `xpinstall.signatures.required` en `false`
  ([ayuda de Mozilla](https://support.mozilla.org/kb/add-on-signing-in-firefox)).

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

### Ir al cliente

El campo «Ir al cliente» no es de la extensión: está en la barra del propio `/crm`, así que se ve igual en
el panel de Chrome, en el de Firefox (donde el panel es la página del CRM) y en el CRM web. Se pega o se
teclea el número del cliente tal como se ve en WhatsApp Web (`+52 1 55 1234 5678`, diez dígitos, con
guiones o paréntesis) y «Ir» abre su conversación. Es un GET a `/crm?chat=`: el servidor lleva el número
al formato canónico con `normalize_phone` (`app/crm.py`, `crm_page`). La extensión no lee WhatsApp Web ni
guarda números.

### ¿Folio manual?

El botón «¿Folio manual?» de la misma barra abre una ayuda con los pasos para abrirle un folio a un cliente
que se atendió desde el celular: reenviarle al agente su material desde el celular registrado en el
usuario, esperar la pregunta del número y seguir el diálogo del agente hasta el folio
(`app/alta_manual_barrido.py` y `app/alta_manual_dialogo.py`). La pinta el servidor con el estado real
(`app/crm.py`, `_ayuda_folio_manual`): avisa si la perilla «Alta manual de folios por colaboradores» del
Estudio está apagada o si el usuario no tiene un celular que el agente pueda reconocer, y dice cuántos
segundos esperar. Si el diálogo del agente cambia, la ayuda también.

## Probar

    .venv/bin/python -m pytest tests/test_ext_cse_manifiesto.py -q   # manifiestos, regla, construir.py y zips
    node ext-cse/prueba/juez-chrome.mjs                               # Chrome real, sin cabeza
    python3 ext-cse/prueba/juez-firefox.py                            # Firefox real, sin cabeza
    python3 ext-cse/construir.py --crm https://bot.esaumillan.cloud && npx web-ext lint -s ext-cse/dist/firefox

Los dos jueces levantan el bot con una base temporal sembrada (`scripts/qa_crm_seed.py`), instalan el zip de
la extensión (no la carpeta, que borran antes) en un perfil temporal y recorren el panel: el CRM en
mantenimiento (503) y luego caído, el CRM
que vuelve (login y, tras entrar, `/crm`), «Ir al cliente» con un número pegado, la ayuda «¿Folio manual?»
(modal, cabe en un ancho de panel y Escape la cierra), sin red y sin permiso de
sitio. El de Chrome comprueba además
la regla del marco (solo deja enmarcar a la extensión, y aguanta dos instalaciones a la vez) y que Chrome
no reporte errores ni advertencias. El de Firefox abre el panel lateral de verdad y lo lee desde el
contexto «chrome» de Marionette: necesita `geckodriver` (el de snap trae uno); con el Firefox de snap, la
extensión se arma dentro de `ext-cse/dist/`, porque snap no lee carpetas ocultas de HOME ni el `/tmp` del
sistema.

Para instalarla a mano: Chrome → descomprimir `ext-cse/dist/ext-cse-chrome-<versión>.zip` en una carpeta que se
quede → `chrome://extensions` → modo desarrollador → «Cargar descomprimida» → esa carpeta. Firefox →
`about:debugging` → «Este Firefox» → «Cargar complemento temporal…» → `ext-cse/dist/ext-cse-firefox-<versión>.zip`.
Para probar un cambio sin pasar por el zip sirven también las carpetas `ext-cse/dist/chrome` y
`ext-cse/dist/firefox/manifest.json`.
