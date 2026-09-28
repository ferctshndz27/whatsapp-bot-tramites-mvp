---
name: backend-senior
description: Trabajo de backend del bot de trámites — rutas FastAPI, sesiones SQLModel/SQLite, escrituras ORM contra update() de Core y el espejo MSSQL, ajustes con barandal en app/config.py, migraciones a mano en app/db.py, ciclo de vida del folio y asignación. Úsalo al tocar app/conversation.py, app/dashboard.py, app/inbox.py, app/my.py, app/clients.py, app/db.py, app/models.py, app/config.py o app/asignacion.py.
---

# Backend senior — FastAPI + SQLModel as used here

## Purpose

Ship server-side changes to the trámites bot: routes, the conversational state machine,
SQLite/SQLModel writes, `app/config.py` tunables and the hand-rolled migrations in
`app/db.py` — without silently diverging the MSSQL mirror and without crossing the product
line (the bot never sets a final price, validates documents, confirms a payment, or closes
a trámite).

## When to use

- Editing `app/conversation.py`, `app/dashboard.py`, `app/inbox.py`, `app/my.py`,
  `app/clients.py`, `app/users.py`, `app/asignacion.py`, `app/activity.py`.
- Adding a route, a `Form(...)` POST handler, or a new `SQLModel` column/table.
- Adding a setting to `app/config.py`, or touching `init_db()` / the index block in `app/db.py`.
- Ticket lifecycle: claim, release, assignment, `first_response_at`, `status_changed_at`.

## When NOT to use

- NLU cascade or synonym mining → `app/nlu.py`, `app/synonym_miner.py`. Nothing auto-learns.
- Catalog content → `app/catalog.py` (SQLite-backed; `app/catalog.json` is seed-only).
- Adding a **mirrored table** → that is the five-place checklist in AGENTS.md (`app/archive.py`
  `_SCHEMA_DDL`, `archive_<table>()`, `_register_listeners()`, `backfill()`, `MIRROR_TABLES`
  in `app/system.py`). Follow it there; do not restate it here.
- Jinja templates and the server-side SVG charts built in `app/activity.py` → frontend work.
- Money paths → `app/cobranza*.py`, `app/stripe_client.py`, `app/stripe_webhook.py`.
- Any change that would let the bot quote a final price, validate a document, confirm a
  payment, or close a trámite. That is the product line, not a backend call.

## Responsibilities

- Choose ORM vs Core `update()` per write, and know which one the mirror sees.
- Keep write sessions short: open `with get_session()` in the handler body, commit, close
  **before** any network I/O.
- Give every new `config.py` setting a barandal (min/max clamp) and a comment saying what
  breaks at each end.
- Add new columns in **both** places (`app/db.py` and `app/archive.py`) or the mirror diverges.
- Return `RedirectResponse(..., status_code=303)` from every POST; never render HTML from a POST.
- Guard every route with `_current_user(request)`, and admin sections with `auth.is_admin`.

## Analysis checklist

Run this before editing any route or model:

1. Does this handler **write**? If yes: ORM read-modify-write, or Core `update()`? (see rule below)
2. Does the table have a mapper listener in `app/archive.py:1687-1780`? Ticket, Message,
   Conversation, Client, User, TicketActivity all do.
3. Does the model carry `habilitado` (soft delete, `app/models.py`)? Then the query must filter
   it — `asignacion._elegibles` does (`User.habilitado == True  # noqa: E712`).
4. New column? Add it to the right `_*_COLUMNS_ADDED_AFTER_LAUNCH` dict in `app/db.py:31-89`
   **and** `_COLUMNS_ADDED_AFTER_LAUNCH` in `app/archive.py`. Nullable or with a DEFAULT —
   SQLite rejects `ADD COLUMN NOT NULL` without one on a populated table (`db.py:67-70`).
5. Can this write hit a partial unique index (`ux_cobro_vivo`, `ux_cobro_generacion`,
   `ux_ticketlabel_viva`, `ux_message_wa_message_id`, `ux_intento_vivo` — `db.py:97-161`)?
   Then catch `IntegrityError` (`dashboard.py:9`, `cobranza_panel.py:30`).
6. POST returns 303? Guarded by `_current_user`? Admin-only guarded by `is_admin`?
7. Comparing datetimes? `utcnow()` is aware, SQLite reads back naive — normalize both.

## Technical principles

- **No dependency injection.** There are zero `Depends()` in `app/`. Sessions are opened
  inside the handler body: `with get_session() as session:` (`app/db.py:234`). Do not
  introduce a `Depends(get_session)` dependency — it would fight the autouse test fixture.
- **Session per block, not per request.** A handler may open two: `dashboard.claim_ticket`
  opens one for the atomic UPDATE (`:607`) and a second for the activity log (`:624`).
  `webhook._handle_message` opens one to check `client.habilitado` (`:93`) and another for
  `handle_incoming` (`:99`), then **closes it before `_deliver()`** does network I/O to
  Meta/Twilio (`:105-108`). Never hold a SQLite session across an HTTP call.
- **Core `update()` is reserved for owner races.** It IS used, deliberately, at
  `dashboard.py:610-621` (claim) and `:664-676` (release), gated on `result.rowcount`
  (`:623`, `:678`), so two advisors clicking at once cannot both take a folio. The cost:
  `archive.py:1728-1731` registers `after_insert`/`after_update` at the **mapper** level, and
  a bulk UPDATE never loads instances — so the mirror never sees those two writes until
  someone hits "Sincronizar ahora" (`backfill()`) in `/system`.
  **Everywhere else, ORM read-modify-write.** `inbox.py:414-418` documents the deliberate
  refusal: sealing `first_response_at` uses SELECT+loop and accepts a seconds-wide race,
  because the atomic UPDATE "brincaría los listeners ORM del espejo MSSQL — peor trato".
  Only reach for Core `update()` when a wrong *owner* is the failure mode; then say so in a
  comment, and note the mirror gap.
- **Settings carry barandales.** `config.py:105` `fuzzy_threshold = max(70, min(95, ...))`,
  `:150` `oxxo_dias_vencimiento = max(1, min(3, ...))`, `:163` `carga_maxima_asesor =
  max(1, min(200, ...))`. A new tunable gets a clamp plus a comment naming what breaks at
  each end (a `0` that silently disables a feature is the case these exist for).
- **Some switches are code, not env.** `config.CLAUDE_HABILITADO = False` (`:12`) is a module
  constant on purpose: flipping it is a reviewed, deployed code change, and it blanks
  `anthropic_api_key` centrally instead of at seven call sites (`:38-51`).
- **Reference the engine by attribute, never bind it at import.** Use `get_session()`, or
  `Session(db.engine)` after `from app import db` (`main.py:159`, `rag.py:152`,
  `agente_studio.py:304`). `from app.db import engine` would capture the real `tramites.db`
  and defeat the autouse `_isolated_db` fixture in `tests/conftest.py`.
- **Assignment is decided per service.** `conversation._handle_confirmar` calls
  `asignacion.elegir_por_servicio(session, client.phone, ids)` once, before the loop that creates
  one Ticket per requested service, and each Ticket takes its own `Decision`. The chain —cartera →
  pegajosa → mismo mensaje → reparto— is the pure `asignacion._decidir`; the same advisor keeps
  several folios only if they attend them. `asignacion.bitacora` runs **after** `session.commit()`
  because it needs the folio id.
- **A folio born from auto-assignment stays `NUEVO` with an owner.** `TOMADO` would corrupt the
  funnel; `asignacion.provisional()` is what keeps "whoever replies keeps the chat" alive in
  `inbox._pause_bot` (`inbox.py:308-346`).

## Common mistakes

- Introducing `Depends()` or a repository/ports layer. This repo is flat `app/*.py` modules;
  there is no DI container, no CQRS, no MediatR, and adding one is fashion, not architecture.
- Using Core `update()` on a mirrored table for convenience — the mirror goes stale with no error.
- `from app.db import engine` — tests then write the developer's real DB.
- Calling a helper that opens its OWN session (`auth.is_admin`, `auth.alcance`, `audit.log_admin`)
  while the current session has pending writes. With the suite's single-connection engine its
  `close()` rolls back the outer transaction and the write vanishes silently (the trap documented
  in `asignacion._por_producto`). Resolve those values before the first write, as
  `inbox._pause_bot` does, or after the commit.
- Adding a column to `app/db.py` but forgetting `app/archive.py` (or the reverse).
- A `SQLModel` `sa_column_kwargs={"server_default": text("'meta'")}` that disagrees with the
  `ALTER TABLE ... DEFAULT` string in `db.py` (`models.py:86`, `114`, `171` vs `db.py:41`, `50`, `71`).
- Comparing `utcnow()` (aware, `models.py:8`) against a datetime read from SQLite (naive).
  `conversation._captura_caducada` (`:1483-1496`) shows the normalize-both pattern.
- Returning `200` + HTML from a POST instead of 303 — breaks the F5/back behavior the panel relies on.
- Adding a `self.x = os.environ.get(...)` without grepping the attribute name first:
  `public_base_url` is already assigned twice (`config.py:127` and `:144`).
- Backfilling a new column with an invented value. This repo prefers an honest `NULL`
  (`db.py:42-47`, `:72-77`); a `DEFAULT` would claim old rows had a value they never had.

## Quality bar

`app/asignacion.py` is the style exemplar — match it:

- Module docstring that states what the module **deliberately does not do**, and why.
- Spanish docstrings and comments explaining the *reason*, not the mechanics.
- `# ponytail:` for a conscious simplification, always with its upgrade path
  (`config.py:157-162`, `inbox.py:414-421`).
- `# noqa: E712` on `== True` column comparisons; `# type: ignore[union-attr]` on SQLModel
  column ops like `.is_not(None)` / `.desc()`.
- No new counter or "is it alive" predicate when one exists — `asignacion` reuses
  `activity.carga_abierta` rather than defining a second notion of an open folio.

## Security

- Every route starts with `user = _current_user(request)` → `RedirectResponse("/login", 303)`
  if falsy. Admin sections add `auth.is_admin(user)` (`app/auth.py:7`).
- Log phones through `mask_phone()` (`conversation.py:285`, `webhook.py:96`) — never raw.
- `settings.public_base_url` is required for Twilio signature checks: behind the proxy
  `request.url` reports the internal host, so "sin esto fallan TODAS las firmas, no algunas"
  (`config.py:123-127`). Same var backs Stripe `success_url`/`cancel_url`.
- Without `stripe_webhook_secret` the Stripe webhook 403s everything, on purpose (`config.py:136-139`).
- Do not reorder the four middlewares in `main.py:106-119` without reading the comment:
  Starlette wraps last-added outermost, and `_nav_context` must stay inside `SessionMiddleware`.
  `_nav_context` swallows all exceptions on purpose — a nav badge must never take down a page.
- New settings that are secrets default to `""` and the feature stays inert; never raise at import.

## Performance

- SQLite runs WAL with `synchronous=NORMAL` and `busy_timeout=5000` (`db.py:19-27`) so the
  dashboard reads while the bot writes. That 5 s budget is why a write session must not wrap
  a WhatsApp API call.
- Indexes are justified per query, not sprinkled: `ix_cobro_reconciliacion ON cobro(estado,
  reconciliado_at)` exists because otherwise the nightly reconciliation sweep is a full scan of
  `cobro` (`db.py:112-115`). The partial unique indexes are correctness locks, not speedups.
- `_normalize_phones()` runs on **every** `init_db()` — web startup and each cron run
  (`db.py:163`, `jobs.py`). Keep it idempotent and cheap; it is a no-op on canonical data.
- Aggregate in SQL when the answer is a count: `activity.carga_abierta` (`:328-336`) and
  `asignacion._ultimo_folio_por_asesor` (`:87-92`) use `func.count`/`func.max` + `group_by` and
  return a dict. Reuse them; never count folios in a Python loop over `select(Ticket)`.
- Batch reads when you need the rows: `inbox.build_conversation_items` (`:78-88`) fetches each
  table once into dicts and joins in Python. Its `# ponytail:` (`:79-80`) says it loads *all*
  messages — fine at this volume, move to an aggregate query if history reaches tens of
  thousands. Never add an N+1 inside a template loop.

## Definition of done

- [ ] `pytest -q` green (CI runs only this).
- [ ] Every write classified: ORM (mirror sees it) or Core `update()` (mirror does not — and a
      comment says so).
- [ ] New column present in `app/db.py` **and** `app/archive.py`.
- [ ] New setting clamped, commented, and added to `.env.example`.
- [ ] Every new POST returns 303 and is guarded by `_current_user` / `is_admin`.
- [ ] A test that would fail without the change — seed your own catalog rows, and
      `session.expire_all()` before asserting when the route wrote through its own session.
