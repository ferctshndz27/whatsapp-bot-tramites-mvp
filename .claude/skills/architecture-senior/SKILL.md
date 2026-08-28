---
name: architecture-senior
description: Úsalo para decidir DÓNDE vive una regla en el bot de trámites de WhatsApp — límites entre webhook/conversation/nlu/catalog/rag/asignacion, si algo debe espejarse a MSSQL (cuesta 5 sitios), si toca esquema sin Alembic, si el cambio es una fase del roadmap ERP/CRM o un parche, cuándo NO abstraer (marcadores ponytail) y si la línea del producto (el bot nunca fija precio final, valida documentos ni cierra folios) sigue intacta.
---

# Architecture — system boundaries and roadmap calls

## Purpose

Decide where a rule belongs before writing it, and whether the change is one more roadmap phase or a
patch. Few layers, all load-bearing: a wrong boundary is how the MSSQL mirror silently diverges, how
the NLU quotes the wrong trámite, and how the bot starts crossing the product line.

## When to use

- A new rule, state, layer, table, column, middleware, or config knob is on the table; or you are
  deciding between "add it to the current phase" and "add a §3 row to the gaps doc".
- Something is read/written from two modules and you want to duplicate it; a change touches
  `app/archive.py`, `app/db.py`, middleware order, or `_dispatch`; a `# ponytail:` may be due.

## When NOT to use

- Fixing a bug inside one function with no boundary change — go straight to the fix.
- Copy/UI wording, WhatsApp interactive limits, dashboard styling; prompt tuning or threshold
  calibration inside a layer that already exists; deploy/git mechanics (see AGENTS.md).

## Responsibilities

Who decides what. Do not move a decision across these lines.

| Module | Decides | Never |
|---|---|---|
| `app/webhook.py` | transport: HMAC (`_valid_signature`:62) / Twilio signature (`_form_verificado`:215), phone normalization at the edge (:75), unwrapping `list_reply`/`button_reply` to its row id (`_extract_text`:111), which `provider` the answer leaves by (`_deliver`:124) | trámite logic |
| `app/conversation.py` | conversational state, escalation, ticket creation (`_handle_confirmar`:721) | which service the text means |
| `app/nlu.py` | which service/category a text means, and which layer said so (`deduce_explained`:132) | what to reply, or touching state |
| `app/catalog.py` | the only source of price, name, synonyms, requirements | conversational rules |
| `app/codigos.py` | the CURP/RFC/NSS/email patterns and `cuantos_tramites`:108 | fixing a final price |
| `app/rag.py` / `app/agent_tools.py` | informative answers and read-only tools, hanging off `_not_understood`:1245 | entering the cascade |
| `app/asignacion.py` | which human gets the folio (`elegir`, once per message) | what the client is told |
| `app/archive.py` | MSSQL mirror, always through `_safe`:1380 | raising into the request path |

Single trunk: `webhook.handle_inbound`:82 → `conversation.handle_incoming`:262. Both providers arrive
already translated to the canonical shape; nothing provider-specific may live past that line.

## Analysis checklist

1. **Does it cross the product line?** No final price, no document validation, no payment confirmed,
   no trámite closed by the bot. `_estimado`:1367 always says "sujeto a confirmación del asesor" and
   refuses to multiply a non-numeric catalog price; media replies "un asesor te indicará cómo
   enviarlos" (:289). Nothing auto-learns: `SynonymProposal` stays `pendiente` until `/unmatched`.
2. **Which layer owns it?** Use the table above. If two want it, one of them is wrong.
3. **Does it need a new table?** It costs five sites (AGENTS.md lists them; `dba-senior` has the
   detail). Try a number in `config.py` or a derivation first — §7bis.2 did that with
   `CARGA_MAXIMA_ASESOR`, load from
   `activity.carga_abierta`, and the sticky advisor from `ORDER BY created_at DESC LIMIT 1`.
4. **New column?** `_*_COLUMNS_ADDED_AFTER_LAUNCH` in `app/db.py` AND `_COLUMNS_ADDED_AFTER_LAUNCH`
   in `app/archive.py`:559. Nullable unless history really has the value.
5. **Is it a new conversational state?** New key in the `handlers` dict (`_dispatch`:457-465), never
   an `if` branch inside another handler. **Is it phase or patch?** See below.
6. **Switch instead of deploy?** If it changes who sees work or how the agent behaves in production,
   it is a `Perilla` in `agent_settings.CONFIG_EDITABLE`:85 — default off, reversible in one click,
   audited in `AdminAudit`.

## Technical principles

**The NLU cascade order is a cost/risk decision, not a preference.** Real order in
`deduce_explained`:136-183 — (1) literal exact match by whole word (`_exact_matches`:510 /
`_contains_term`:497), the ONLY layer trusted enough to skip the LLMs because it is exact, free and
instant (a local classification costs ~5 s); a tie is real ambiguity and is deliberately NOT resolved
there; (2) `ollama`; (3) `llm` (Claude); (4) `_deduce_keywords_explained`:543 → literal → fuzzy →
category by name → `ninguna`. Fuzzy is last on purpose (:550-554): a false positive there quotes the
wrong trámite to a real client. An LLM saying "no sé" does not stop the chain (:162,177) — the layers
below see synonyms mined after the prompt was built. `es_vago`:121 downgrades a service to its
category rather than invent a trámite.

**The catalog lives in SQLite because the NLU reads it live.** `_service_terms`:489 rebuilds terms
from `catalog.categories` on every deduction, so an approved synonym applies on the next message with
no deploy; `app/catalog.json` is a one-time seed only. `PAGE_SIZE = 8` (catalog.py:36) is a Meta
constraint (8 + "Ver más" + "Volver" = the 10-row cap), not taste.

**No `cantidad` column, on purpose.** Derived from the folio: `cuantos_tramites` counts only codes of
the type the requirement discriminates, and the classification is cached in the `confirmar` context
(`_pendiente`:500) so the quote the client saw and the folio's match and the "sí" turn costs zero. A
column would be a sixth mirror site and a second truth against `requirements_json`.

**A mirrored table costs five sites** — AGENTS.md lists them and `dba-senior` carries the detail
(line refs, `_upsert`, what `backfill()` skips). The architectural consequence is the part that
belongs here: `/system` compares **row counts, not fields**, so a field written with a Core
`update(Ticket)` never fires the mapper and the divergence is invisible (§7bis.2). Default to
mutating through the ORM object being committed. The **one sanctioned exception** is an
owner-race guard — `dashboard.py` claim/release use Core `update()` + `result.rowcount` on
purpose, and accept the mirror gap in writing; see `backend-senior` before copying that shape.

**Middleware registration order is inverted at runtime** (main.py:106-119, Starlette wraps last-added
outermost): anything reading `request.session` must be registered *before*
`add_middleware(SessionMiddleware)` — added after that line it silently sees an empty session.
`_security_headers` is registered last so it seals every response.

**Roadmap vs patch** (`docs/ANALISIS-BRECHAS-ERP-CRM.md`):
- A gap already in the §3 table → it is a phase and gets a spec section (§5, §6, §7, §7bis are
  exactly that) reviewed before code. Phases 1, 2, 3a, 3b are implemented; **4 (outbound webhooks +
  rules engine) is next to spec**. Effort must count the five mirror sites and no Alembic (§3).
- Order rule from §4: *phase 1 first because without it the rest cannot be prioritized with data.*
  Never build the automation before its meter.
- Anything new and off the runway → a new §3 row with impact/effort, never smuggled into the current
  phase; §7bis deliberately left the `build_conversation_items` fix out for exactly that reason.
- §3 "Lo que deliberadamente NO se propone" is a closed refusal list (bot closing trámites /
  validating documents / setting final price; synonym auto-approval; a `cantidad` column). Reopening
  one is a product decision, not an implementation detail.

**When NOT to abstract.** `# ponytail:` marks a deliberate simplification *with* its upgrade path and
the condition that triggers it: catalog.py:180 (full reload per access — ~60 rows), asignacion.py:26
(pool = every enabled user, admins included), config.py:157 (15 is chosen, not measured), db.py:29
(no Alembic), dashboard.py:414 (in-memory pagination). Do not "fix" one until its stated condition
holds; leave a marker with its trigger when you accept a simplification; never convert a ponytail
into an abstraction because it looks cleaner.

## Common mistakes

- Adding a table/column and forgetting site 5 (`MIRROR_TABLES`) — `/system` then calls the mirror
  healthy while a whole table diverges. It already happened to `ClientNote`, `FollowUp`, `SavedReply`
  (see the comment in system.py).
- Writing an assignment (or any mirrored field) with a Core `update()` instead of the ORM object
  *for convenience*: `after_insert`/`after_update` never fire and row counts still match. (Not a
  mistake when a wrong **owner** is the failure mode — then it is the documented exception above.)
- `ALTER TABLE ... ADD COLUMN NOT NULL` without DEFAULT — SQLite rejects it over a populated table
  and startup dies on the VPS (db.py:67-70). Converse mistake: inventing a DEFAULT where NULL is the
  honest answer (`pricing_*` db.py:42-47, `first_response_at` db.py:72-74).
- Making `handle_incoming` async or calling it from the event loop: uvicorn runs ONE worker and the
  chain (NLU, HTTP to Meta/Twilio, SQLite) is sync — hence `run_in_threadpool` (webhook.py:50,241).
- A substring shortcut in `_dispatch` that ignores `capturando` (:430): in `pidiendo_requisitos` the
  client's free text legitimately contains "folio", "lista", "precios".
- Re-defining CURP/RFC/NSS patterns in `conversation.py` — they live in `app/codigos.py`, and
  `rag._CURP_RE` is separate **on purpose** (laxer leak guard), so do not unify them either.
- Calling `asignacion.elegir` inside the ticket loop: once per message (:740) — a client asking for
  three trámites is one client, not three.
- Logging a `rag`/`tools` row without excluding it from denominators (`models.CAPAS_EXTRA`), which
  inflates the "not understood" rate; turning a one-row policy into a table (§7bis.2).

## Quality bar

- Every boundary decision leaves a comment saying *why the other side was rejected* — house style,
  see webhook.py:138-146, nlu.py:550-554, db.py:38-47. A phase lands with its spec section in
  `docs/ANALISIS-BRECHAS-ERP-CRM.md` and its §4 status row, including what was discarded and why.
- For anything server-rendered whose JS holds a parallel catalog, add the test that reads the
  template and asserts every server-emitted id exists client-side: `/agente` returned 200 while
  rendering blank because `BASE` lacked one node (§7bis.1). Route-status tests cannot see that.

## Security

- Both webhooks reject before parsing: HMAC-SHA256 with `hmac.compare_digest` (webhook.py:62), Twilio
  over the **public** URL rebuilt from `PUBLIC_BASE_URL` (:215) — behind the proxy `request.url` is
  the internal host. That is also the cross-provider firewall: a Meta payload posted to the Twilio
  route is rejected whole, not half-processed.
- Secrets and infra URLs never enter `CONFIG_EDITABLE` (agent_settings.py:8-9); free-text knobs use
  an allowlist regex, not a denylist (`_MODELO_RE`:24). Admin surfaces (`/agente`, `/unmatched`,
  `/catalog`) gate on `is_admin` and write `AdminAudit`.
- `SECURITY_HEADERS` (main.py:60-73) is the single place for CSP/HSTS/noindex; the CSP allows inline
  style/script and nothing cross-origin — do not add a CDN.
- Soft deletes are semantic: `_latest_open_ticket`:384 filters `habilitado == True` — otherwise
  escalation marks an invisible folio and a deleted `agent_active=False` silences the bot forever.

## Performance

- The mirror never blocks a request (`_safe`:1380); that pattern is the required reference for the
  phase-4 outbound webhooks (§8). Pragmas are set once on connect (db.py:19-27): WAL so the dashboard
  reads while the bot writes, plus `busy_timeout=5000`.
- The literal shortcut before the LLMs is a latency budget, not a micro-optimization (~5 s of local
  classification avoided on the most common phrasing).
- The partial unique indexes on `cobro` (db.py:92-111, `ux_cobro_vivo`) are the double-charge guard —
  never weaken one to "make an insert pass". N+1 loops accepted at current volume carry a ponytail
  marker with their trigger: read it before optimizing, and add one if you accept a new loop.

## Definition of done

- [ ] Product line intact: no final price, no document validation, no payment confirmed, no trámite
      closed by the bot; nothing auto-learns.
- [ ] The rule lives in exactly one module, and the rejected alternative is written down.
- [ ] Mirrored table added → all five sites, verified against `/system` row counts. Column added →
      both the `db.py` and `archive.py` dicts, nullable-or-justified.
- [ ] State added → a key in the `handlers` dict, with `capturando` behavior considered.
- [ ] If it is a phase: §3/§4 updated, spec section written, discards recorded.
- [ ] Ponytail markers respected, or upgraded with their condition met and the marker removed;
      `pytest -q` green.
