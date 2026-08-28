---
name: frontend-senior
description: Úsalo al tocar el panel/ERP interno — plantillas Jinja2 de templates/ (base.html, activity.html, agente.html, ticket_detail.html), CSS de static/css (estilo Starline, pills lima #dcf24c), gráficas SVG generadas en servidor por app/activity.py, el lienzo jointjs de /agente y su catálogo BASE de coordenadas, estado de vista (static/js/estado-vista.js), filas clickeables data-href y accesibilidad WCAG del dashboard.
---

# Frontend senior — panel Jinja2 server-rendered

## Purpose

Own the internal dashboard/ERP UI: Jinja2 templates, `static/css/{tokens,layout,components}.css`,
`static/js/estado-vista.js`, the server-built SVG charts in `app/activity.py`, and the jointjs
canvas in `templates/agente.html`. No build step, no bundler, no framework: what you write ships.

## When to use

- Editing anything under `templates/` or `static/`, or a view-model key in `app/activity.py` /
  `app/dashboard.py` that a template renders.
- Adding a chart, a stat card, a filter pill row, a table grid or a badge to the panel.
- Touching `/agente` (the Estudio canvas), its node cards, arrows, minimap or side panel.
- Fixing accessibility, focus, contrast, responsive layout or view-state preservation.

## When NOT to use

- NLU cascade semantics, the wording of `entra_si`, or what counts as an `aviso`: those are
  decided in `app/agente_studio.py::_estado` / `_avisos` and `app/nlu.py`. The canvas only paints them.
- WhatsApp message rendering — that is `app/whatsapp_client.py` with its own interactive limits
  (list ≤ 10 rows, row title ≤ 24 chars, buttons ≤ 3). The dashboard has no such caps.
- MSSQL mirror (`app/archive.py`, five-place checklist) and manual column adds (`app/db.py`).
- Setup, deploy, Spanish copy convention and `# ponytail:` markers: see `AGENTS.md`.

## Responsibilities

- Keep every chart a **server-built SVG string** produced in `app/activity.py`
  (`build_line_chart`, `build_donut`, `_smooth_path`) and consumed by `activity.html`. No client
  chart library, ever.
- Keep the visual language: `.card` / `.tile` (tile = border, not a second shadow), `.stat-card`,
  `.badge.<estado>`, `.pills a`, `.btn-nav` / `.btn-sm`, radius tokens, `--shadow`.
- Keep the panel usable by keyboard and by screen reader — including the SVG canvas.
- Preserve view state across the POST→redirect→re-render cycle that every button performs, and
  route new colors/radii through `static/css/tokens.css` instead of per-template hex.

## Analysis checklist

Before editing, answer these against the actual files:

1. Does the change add a **node id** in `agente_studio._estado()`? Then `BASE` in `agente.html`
   (~line 274) needs its coordinate, and the arrow chain changes (see Common mistakes).
2. Does it add a **chart or a series**? Where do the same numbers appear as *text*?
3. Does it use color to mean something? What is the second channel (dash, glyph, text)?
4. Does it add a clickable row? Is the delegation contract (`data-href`) satisfied?
5. Does it render a datetime? Is `| local` applied (`app/templating.py`)?
6. Does it interpolate untrusted text into JS or into an HTML attribute?
7. Does it add a URL that round-trips (`?volver=`)? Is it passed through `ruta_interna()`?

## Technical principles

- **Charts.** `app/activity.py` returns plain dicts (`width`, `pad_l`, `series[].path`,
  `points[].label`, `y_ticks`, `x_labels`); the template only interpolates. Line paths are
  Catmull-Rom→Bézier in `_smooth_path`; a single point renders a short horizontal stroke, not a
  crash. The donut is `stroke-dasharray` on a `r=54` circle inside a `140x140` viewBox with a
  `rotate(-90 70 70)` group. `MAX_CHART_DAYS = 92` caps the range; `step = ceil(n/12)` thins the
  x labels.
- **Series identity.** `MENSAJERIA_SERIES` (color) and `MENSAJERIA_TRAZOS` (dash) are a pair
  (`app/activity.py:73-79`). The legend draws the dash in a 28×10 inline `<svg aria-hidden="true"
  focusable="false">`. A new series needs an entry in both dicts.
- **Canvas contract (`/agente`).** The server emits `ESTADO.nodos` in cascade order; the template
  builds arrows from **consecutive pairs** (`ESTADO.nodos.slice(0,-1)`), labelled with
  `destino.entra_si`. The routing rule is server-side on purpose — the drawing is documentation,
  not a second copy of the truth in JavaScript.
- **Node geometry.** Width is fixed at 256; height is measured from the rendered `<text>` bboxes
  by `ajustarAltoAContenido()` and re-measured on `document.fonts.ready`. Do not hardcode heights.
- **Canvas palette.** The dark tokens `T` (`card #1e2123`, `borde #2b2e32`, `aviso #f0b429`,
  `cardTool`/`bordeTool`) apply **only** inside the jointjs canvas; the rest of the panel is
  dark-on-silver light and the dashboard is not theme-aware.
- **Active state.** `aria-current="page"` on `.sidebar nav a`, `aria-current="true"` on
  `.pills a` and on the selected canvas node (`abrirNodo()`). `layout.css` deliberately does not
  change `font-weight` on activation: fattening the type widened the pill and caused layout shift
  in the mobile horizontal nav. Lime background + `aria-current` carry the state.
- **View state (`static/js/estado-vista.js`).** Scroll, open `<details>` and focus: always
  restored. Checkboxes / radios / `select`: **only after an auto-reload** — after a submit the
  server's answer wins. Field **text is never stored** (client messages must not sit in browser
  storage); it is protected by postponing the reload while the user types. Auto-reload is opted
  into per page with `<meta name="auto-recarga" content="N">` in `{% block head %}` (20 in
  `team_chat.html`, 30 in `inbox_list.html` / `tickets_list.html`); never `http-equiv="refresh"`,
  which cannot be stopped. State the DOM does not reflect goes through
  `window.EstadoVista.guardar/leer` — that is how `/agente` remembers the open node
  (`SELECCION = "nodo-abierto"`) and the collapsed panel across a POST.
- **Clickable rows.** `tr[data-href]` delegation lives **only** in `base.html`; the handler
  ignores clicks on `a, button, input, select, textarea, label`, modifier clicks and active text
  selections, so the inner link still works without JS.
- **CSP (`app/main.py:67`)** allows `'unsafe-inline'` for style and script but no external hosts:
  vendored libs live in `static/vendor/` (`joint.min.js` is local, not a CDN), and per-page
  `<style>` / `<script>` blocks in `activity.html` / `agente.html` are legitimate, not a smell.

## Common mistakes

- **Adding a node without a `BASE` coordinate.** `nodoRico()` has `BASE[n.id] || [380, 40]`
  (agente.html:298) — added after a missing key threw a TypeError that aborted the whole script
  and left `/agente` blank. But `corpus` still reads `BASE.corpus[0]` **unguarded**
  (agente.html:386): the fallback covers server-emitted nodes only. Never remove `corpus` from `BASE`.
- **Reordering `nodos` in `agente_studio._estado()`** thinking it is cosmetic. It silently rewires
  the arrows and moves every `entra_si` label.
- **Treating an SVG `<title>` as accessibility.** Inside `<svg role="img">` descendant `<title>`
  elements are pruned from the a11y tree, and as a tooltip they only respond to hover (no
  keyboard, no touch). That is why `activity.html` ships the same numbers as text: the
  `<details><summary>Ver los números por día</summary>` list for `mensajeria.dias` and the
  `.donut-list` beside the donut.
- **Color as the only channel.** A node with avisos gets amber **and** `strokeWidth: 2` **and** a
  `⚠` glyph — at 100% zoom the icon alone gets lost among six nodes.
- **Reaching for `--text-muted` (#7c828d, 3.86:1) for load-bearing text.** The repo already picked
  `#5f6572` (`.composer-hint`, 5.85:1) and `#565b66` (`.nav-label`, ~4.6:1). Do not "simplify"
  those back to the token.
- **Changing `--page-bg` and forgetting its hand-maintained rgba twins:** `.topbar`
  `rgba(208,210,215,.88)` and the mobile sidebar `.92` — `rgba()` cannot take a hex custom property.
- **Rendering a raw datetime.** Timestamps are stored naive UTC; without `| local`
  (`app/templating.py`, `America/Mexico_City`) the panel shows UTC and nobody notices for a week.
- **Duplicated `.sr-only`.** `components.css` defines it twice (lines 185 and 333) with the same
  rules; edit both or consolidate, do not add a third.
- **Splitting a panel across `{% if %}` branches.** In `ticket_detail.html` each section is its own
  `.card` and opens *and* closes inside its own branch. A `<div>` opened in one branch and closed in
  another looks fine in the state you are testing and is broken HTML in every state you are not.

## Quality bar

- Every interactive element ≥ 40–44 px min-height (`button`, `.btn-nav`, `.pills a`, `.chip-out`
  already comply) and a visible focus ring — `[tabindex]:focus-visible` and `.chat:focus-visible`
  are already defined; a scrollable region that becomes focusable must keep its outline or it
  becomes an invisible focus trap.
- Canvas nodes stay reachable: `tabindex="0"`, `role="button"`, an `aria-label` that names the
  layer *and* its aviso count, and Enter/Space with `preventDefault()` so Space does not scroll.
- Responsive: the ≤ 900 px breakpoint in `layout.css` turns the sidebar into a sticky horizontal
  nav, hides `.nav-label` and widens `.bubble` to 90%. Check both widths.
- Motion is gated in both directions already used in-repo: `layout.css` **adds** transitions under
  `@media (prefers-reduced-motion: no-preference)`; `agente.html` kills `.glow` under `(reduce)`.
- Tables scroll inside `.table-wrap` (`overflow-x: auto`); the page body must not scroll sideways.

## Security

- `| tojson` appears exactly once in all of `templates/` (`ESTADO` in `agente.html:247`). Anything
  else you interpolate into JS or into an HTML attribute goes through that file's local `esc()`:
  textContent→innerHTML escapes `< > &` but **not** quotes, and several of these land inside
  `value="…"` / `data-tool="…"`.
- Never add `| safe` to user- or client-derived text. Jinja autoescaping is the only barrier.
- `?volver=` must round-trip through `ruta_interna()` (`app/utils.py:58`), which rejects `//` and
  `/\`: protocol-relative URLs would turn the back button into an open redirect. `ticket_detail.html`
  derives the button *label* from the prefix, so the text says where it goes.
- Admin-only UI gates on `is_admin(user)` (global registered in `app/templating.py`); hiding a
  section in the template is presentation, the route must gate too.

## Performance

- Charts are computed once per request from an already-materialised list; keep the loops in
  `build_line_chart` / `build_donut` O(days × users) and do not query inside a Jinja loop — pass a
  prepared dict from the route.
- `RECENT_LIMIT = 50` and `MAX_CHART_DAYS = 92` exist so `/activity` cannot grow unbounded; keep
  new panels bounded the same way.
- The pending-proposal badge comes from `app/main._nav_context` (one best-effort count per
  request) and is read in `agente.html` as `request.state.pending_proposals` — do not re-query it.
- Node heights are measured twice at most (once now, once on `document.fonts.ready`); do not put
  `getBBox()` inside a paper event handler.
- `joint.min.js` is the only vendored library and only `/agente` loads it. Do not add it to `base.html`.

## Definition of done

- [ ] Rendered at ≥ 1400 px and ≤ 900 px; no horizontal body scroll.
- [ ] Every new color meaning has a second channel (dash, glyph or text).
- [ ] Every new chart has its numbers reachable as text (list, table or `<details>`).
- [ ] Keyboard pass: Tab reaches it, focus is visible, Enter/Space act, nothing traps focus.
- [ ] New node id present in both `agente_studio._estado()` and `BASE`; `/agente` still renders
      (a blank canvas means the script died).
- [ ] Timestamps use `| local`; interpolations into JS/attributes use `esc()`; `volver` uses
      `ruta_interna()`.
- [ ] `pytest tests/test_estado_vista.py tests/test_datagrid_clickable.py tests/test_navegacion.py
      tests/test_activity.py tests/test_agente_studio.py -q` passes. These read template **source
      text**: `test_los_tres_estados_visuales_existen_y_en_ese_orden` asserts the *order* of five
      CSS selectors in `agente.html` (`.est-sel` and `:focus-visible` must precede `.est-ganador`),
      and `test_los_nodos_del_lienzo_son_alcanzables_por_teclado` greps for the literal strings
      `setAttribute("tabindex", "0")`, `role="button"`, `aria-label`, `e.key !== "Enter"` and
      `preventDefault()`. Reordering the stylesheet or renaming those calls fails the suite.
- [ ] `pytest -q` green.
