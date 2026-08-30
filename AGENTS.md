
This file provides guidance to ANY agent (claude.ai/code) when working with code in this repository.
It holds the **global rules**. Domain knowledge lives in `.claude/skills/` and loads on demand.

## How to work here

You are a **senior engineering team**, not a single generalist: architect, backend, frontend, DBA,
DevOps, security, QA, performance, integration and AI/ML. **Pick only the roles the task actually
needs** — loading all of them is how a small change turns into a rewrite.

The user never has to say "act as a DBA". Infer it from the task and invoke the matching skill.

### Role routing

| The task touches… | Skill |
|---|---|
| Layer boundaries, a new roadmap phase, "where should this live?", the product line the bot must not cross | `architecture-senior` |
| FastAPI routes, SQLModel, sessions, `app/conversation.py`, settings with guardrails | `backend-senior` |
| Jinja templates, CSS, the server-rendered SVG charts, the `/agente` canvas, accessibility | `frontend-senior` |
| Schema, a new column or table, the MSSQL mirror, indexes, WAL, row-count drift | `dba-senior` |
| Docker, the pull-based deploy, GitHub Actions, the VPS cron, `/health`, secrets on the server | `devops-senior` |
| Webhook signatures, sessions, CSP, authorization, PII (CURP/RFC/NSS), content guardrails | `security-senior` |
| pytest, fixtures, coverage of a real failure, mutation testing | `qa-senior` |
| Something is slow or heavy — **only with a measurement in hand** | `performance-senior` |
| Meta Cloud API, Twilio, Stripe, Ollama, retries, idempotency, WhatsApp interactive limits | `integration-senior` |
| The NLU cascade, RAG, agent tools, synonym mining, prompts, evals, token cost | `ai-ml-senior` |
| Scope of a bot behaviour, what stays with the human advisor, the KPI behind a claim | `product-owner` |
| The routing table itself, a new skill, the permission policy, `tests/test_harness.py` | `harness-engineer` |

**Combine them when the task spans domains** — that is the normal case, not the exception:

- *"This endpoint that queries the mirror is slow"* → `performance-senior` + `dba-senior` + `backend-senior`
- *"Add a table for X"* → `dba-senior` + `architecture-senior` (the five-place rule is a design cost, not a chore)
- *"The bot answered something wrong"* → `ai-ml-senior` + `qa-senior`
- *"New button in the panel"* → `frontend-senior` + `backend-senior`

### Before changing code

Understand → inspect **only the relevant files** → identify the domain → load those skills → plan →
implement → `pytest -q` → review → verify. Don't read the whole repo, and don't edit before you
understand. Both failures cost the same.

### Token discipline

Skills load on demand and cost nothing until invoked — that is the whole point of keeping this file
short. Don't paste framework documentation, don't restate these global rules inside a skill, and
don't open files the task doesn't touch.

### The harness is tested, not just described

This routing table is a contract, not documentation: `tests/test_harness.py` asserts —in both
directions— that every skill on disk has a row and every row points at a skill that exists, that
each `name:` matches its folder, and that every repo path a skill cites still resolves. Adding a
skill is three places: the file, the row, and a green harness test **in the same commit**.

`.claude/settings.json` carries the permission policy the agent works under: the operator runs every
`git push`, `ssh` and deploy, and `deny` now enforces that instead of trusting memory. The agent
cannot edit that file — the platform refuses — so changes to it are handed over as a copy-paste
block. Why the harness looks like this, and how it measures up against a reference harness, is in
`docs/HARNESS.md`.

## What this is

WhatsApp bot (Meta Cloud API) that triages Mexican bureaucratic procedures ("trámites"): it
identifies the service the client wants and opens a request for a human advisor. **The bot never
sets a final price, validates documents, confirms payments, or closes a trámite on its own** — that
line is load-bearing across the whole flow, don't add logic that crosses it.

Stack: FastAPI + SQLModel + SQLite (WAL). A best-effort MSSQL mirror archives every table. The same
process serves the WhatsApp webhook and an internal admin/ERP dashboard (Jinja templates).

## Commands

```bash
# Setup
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

# Run the server (webhook + dashboard) at http://localhost:8000
uvicorn app.main:app --reload --port 8000

# Tests (all)
pytest -q
# Single test file / single test
pytest tests/test_nlu.py -q
pytest tests/test_observability.py::test_fuzzy_threshold_clamped_to_safe_range -q
```

- **Local dashboard login over HTTP fails unless you set `SECURE_COOKIES=0`** in `.env` (the session
  cookie has the `Secure` flag on by default for production HTTPS).
- MSSQL connection errors in test output are harmless noise — there's no local MSSQL and the mirror
  degrades silently by design (`_safe()` wrapper).
- There is no configured linter/formatter in-repo; global rules prefer black/isort/ruff but CI only
  runs `pytest`.

## Deploy (pull-based — no server credentials in GitHub)

Push to `main` → GitHub Actions runs `pytest` → on success the `promote` job fast-forwards the
`production` branch → the VPS cron (`deploy/pull-deploy.sh`, every 2 min, read-only deploy key) does
`git reset --hard origin/production` + `docker compose up -d --build` + `/health` check.
`.env`, `data/`, and `media/` are gitignored and survive the hard reset. `data/deploy-info` holds the
deployed SHA (shown in `/system`).

The human operator runs all `git push` / deploy commands themselves — prepare copy-paste-ready
commands, don't push. See the auto-memory `feedback-comandos-deploy.md` for the exact command format
this operator expects (explicit `git add` file lists, one ref per `git rev-parse`, heredocs for
inline Python).

## Architecture

### Request flow
`app/webhook.py` (HMAC-SHA256 signature check, idempotency) → `app/conversation.py`
`handle_incoming()` (the conversational state machine, ~one file) → replies rendered as WhatsApp
interactive messages by `app/whatsapp_client.py`. Inbound interactive replies (`list_reply` /
`button_reply`) are unwrapped to the row `id` in `webhook._extract_text()`. `_deliver()` sends
interactive messages with an automatic fallback to plain text.

### NLU cascade (`app/nlu.py`)
`deduce_explained(text) -> (service, category, capa, confianza)` is the single entry point;
`deduce()` delegates to it. Layers, **in the order the code runs them** (`nlu.py:132-190`):

1. **literal** `nombre` / `sinonimo` (`_exact_matches`) — exact, free, instant. It runs FIRST
   precisely because a local CPU classification costs ~5 s, and charging that to "quiero mi acta de
   nacimiento" is paying to guess what was already said. **A tie does NOT resolve here** — that is
   real ambiguity and falls through to the LLMs, which do read context.
2. **`ollama`** — local classifier, only if `OLLAMA_URL` is set. A "don't know" does not stop the
   chain: the cheap layers below see synonyms the miner learned that the prompt never carried.
3. **`llm`** (Claude) — only if `ANTHROPIC_API_KEY` is set. Today `config.CLAUDE_HABILITADO` is
   `False`, so this layer is off and the key is blanked centrally.
4. **`keyword`** → **`fuzzy`** (rapidfuzz) — fuzzy goes LAST and accepts a service only at/above
   `settings.fuzzy_threshold` (env `FUZZY_THRESHOLD`, clamped 70–95), because a false positive here
   quotes the wrong trámite to a real client.

One `NluEvent` is logged per inbound message via `conversation._deduce_and_log()`.

### Códigos del cliente (`app/codigos.py`)
Fuente única de los patrones de CURP/RFC/NSS/correo (no los redefinas en `conversation.py`;
`rag._CURP_RE` sí es aparte a propósito — es un guardia de fugas, más laxo). Una cadena con forma
de clave que no cuadra con ningún patrón se guarda como `codigo` y **solo entonces** se le pregunta
a Ollama (`nlu.clasificar_codigos`) de qué tipo es. Si Ollama no está configurado, tarda o alucina,
devuelve `{}` y **el folio se crea igual** — el colaborador valida a mano; nunca bloquea un alta.

Los códigos que vienen en el mismo mensaje que pide el trámite viajan en el contexto `confirmar`
(`_pendiente`), porque el folio no existe hasta que el cliente confirma. De ahí salen **las dos
cosas**: la cantidad que se cotiza y lo que se copia a `requirements_json`. `cuantos_tramites()`
cuenta solo los códigos del tipo que el requisito discrimina (tres correos no son tres actas) y el
estimado es `precio del catálogo × cantidad`, **siempre dicho como estimado** — el precio final lo
sigue fijando el asesor. No hay columna `cantidad`: se deriva del folio, así el espejo MSSQL no se
entera.

**Regla de oro: nothing auto-learns.** Synonym proposals (Claude-mined nightly + clusters of
unmatched messages) are written to `SynonymProposal` as `pendiente` and only take effect after a
human approves them in `/unmatched`. A mis-learned synonym would mis-quote a real client.

### Catalog lives in SQLite, not JSON
`app/catalog.py` is SQLite-backed (`CatalogCategory`, `CatalogService`, with field-level auditing in
`CatalogAudit`). `app/catalog.json` (and `/data/catalog.json` in prod) is **only the one-time seed**
loaded on first access when the tables are empty — editing the JSON does nothing after seeding. Edits
go through the `/catalog` admin grid, which writes audit rows. `PAGE_SIZE = 8` so a category's
services fit one native WhatsApp list (8 rows + "Ver más" + "Volver" ≤ 10-row cap).

### The MSSQL mirror — the main footgun
`app/archive.py` mirrors SQLite → MSSQL via SQLAlchemy `after_insert`/`after_update` listeners.
**Adding a new mirrored table means updating FIVE places or the mirror silently diverges:**
1. `_SCHEMA_DDL` — the `CREATE TABLE`
2. an `archive_<table>()` upsert function
3. `_register_listeners()` — wire the listener
4. `backfill()` — the idempotent SQLite→MSSQL loop
5. `MIRROR_TABLES` in `app/system.py` — so `/system` row counts include it

All mirror writes go through `_safe()` and never raise into the request path. `/system` shows per-table
SQLite-vs-MSSQL counts and a "Sincronizar ahora" button that runs `backfill()`.

### No migration tool — schema changes are manual
No Alembic. New **columns** on existing tables are added by hand:
- SQLite: add to the relevant `_*_COLUMNS_ADDED_AFTER_LAUNCH` dict in `app/db.py` (applied by
  `_add_missing_columns` in `init_db()` via `ALTER TABLE ... ADD COLUMN`).
- MSSQL mirror: also add to `_COLUMNS_ADDED_AFTER_LAUNCH` in `app/archive.py`.

### Dashboard, charts, security
Templates extend `templates/base.html` ("Starline" style: sticky sidebar/topbar, lime `#dcf24c`
active pills, server-rendered). **Charts are server-side SVG** (`app/activity.py` builds line/donut
charts as SVG strings) — no client-side JS chart libraries; keep it that way. Admin-only sections
(e.g. "🤖 Agente WhatsApp" → `/unmatched`) gate on `is_admin`. `app/main.py` registers four
middlewares (order matters — Starlette wraps last-added outermost): session-activity → `_nav_context`
(pending-proposal badge, best-effort) → `SessionMiddleware` → `_security_headers` (CSP, HSTS,
X-Frame-Options, noindex). The dashboard user is auto-seeded at startup from
`DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD`.

## Tests
`tests/conftest.py` has an **autouse** `_isolated_db` fixture that swaps `app.db.engine` for a fresh
in-memory SQLite per test (required now that the catalog lives in the DB — otherwise tests would write
the real local DB). A separate `session` fixture gives a bare `Session`. Tests that exercise catalog
matching seed their own controlled catalog rather than relying on the production seed. When a route
writes via its own session, call `session.expire_all()` before asserting.

## Conventions
- Comments and UI copy are in Spanish (Mexican); match that when editing.
- `# ponytail:` comments mark deliberate simplifications with their upgrade path — respect them.
- WhatsApp interactive limits (enforced in `whatsapp_client.py`): list ≤ 10 rows, row title ≤ 24 chars
  (full name spills into `description`), body ≤ 1024; buttons ≤ 3, title ≤ 20.
