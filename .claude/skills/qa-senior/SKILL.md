---
name: qa-senior
description: Usar al escribir, revisar o depurar pruebas de pytest de este repo — fixtures autouse de tests/conftest.py, aislamiento de base (_isolated_db, los dos engines, StaticPool), la prueba de mutación obligatoria, asserts sobre TicketActivity, session.expire_all(), concurrencia con barreras y el ruido esperado del espejo MSSQL.
---

# QA Senior — pytest in this repo

## Purpose
Write and review tests that go red when production behavior changes. Here a new test file is not
evidence of anything until you mutate the production line it claims to protect and watch it fail.

## When to use
- Adding or changing anything under `tests/` (one file per feature/fase).
- Reviewing tests that are green but suspicious — seeded state doing the work the code should do.
- A test is flaky, order-dependent, or green locally and red in CI (or the reverse).
- Deciding whether an assertion belongs at helper level, through `handle_incoming()`, or via `TestClient`.
- Triaging failure output: telling real failures apart from expected MSSQL noise.

## When NOT to use
- Deciding what the bot is allowed to say or do (final price, closing a trámite): product line in AGENTS.md.
- Adding a column (`app/db.py` + `app/archive.py`) or a mirrored table (five places) — see AGENTS.md.
- NLU quality tuning: that lives in `tests/test_nlu_regression.py` plus the offline benchmark, another loop.
- Deploy/CI mechanics (pull-based promotion) — AGENTS.md owns it.

## Responsibilities
1. Pick the smallest path that cannot lie: pure helper → `handle_incoming()` → `TestClient`.
2. Bind the engine correctly before any code under test opens a session of its own.
3. Run the mutation procedure and write down which mutations were tried and which went red.
4. Shape assertions so they survive rows added by a later fase (`TicketActivity` above all).
5. Cut every outbound dependency (Anthropic, Ollama, Meta, Twilio, Stripe, MSSQL) explicitly.

## Analysis checklist
Before writing a single assertion:
- [ ] Does the behavior exist as a pure function (`horario.abierto`, `horario.proxima_apertura`,
      `asignacion.elegir`)? Test it there, then add ONE end-to-end test for the wiring.
- [ ] Does the code under test call `db.get_session()` internally? → `monkeypatch.setattr(db, "engine",
      session.get_bind())`. 23 test files do exactly this; without it you assert against an empty DB.
- [ ] Is the fact you assert created by the *seed* or by the *code*? If the fixture already set
      `assigned_to`, the test proves nothing about who assigns.
- [ ] Would the expected winner also win by alphabetical or insertion order? Reorder the fixture until
      only the rule under test can produce it (test_fase3b_asignacion.py:173-195).
- [ ] Does the test depend on the UNIQUE index `ux_message_wa_message_id` (app/db.py:143)? It only
      exists after `init_db()`; `_isolated_db` gives you the non-unique `ix_message_wa_message_id`.
- [ ] Does anything cross a thread or a `TestClient` boundary? → StaticPool or a file-backed engine.
- [ ] Which autouse fixtures must you override, and did the tests that measure the switch re-flip it?
- [ ] For a panel route: `TestClient(app, base_url="https://testserver")` and a real `POST /login`.

## Technical principles
1. **The four autouse fixtures in `tests/conftest.py` each came from a real failure, and each is
   designed to be overridden by the tests that need another value:**
   - `_isolated_db` — a fresh in-memory engine per test. The catalog lives in SQLite now, so without it
     tests write the real local `tramites.db`.
   - `_stripe_en_modo_prueba` — `app.config` runs `load_dotenv()` at import; a real restricted
     `rk_live_` key in the dev's `.env` made synthetic `livemode=False` events classify as
     mode-mismatch and half the cobranza module failed for an unrelated reason. "Pasó de verdad."
   - `_nlu_sin_red` — blanks `anthropic_api_key`, `ollama_url`, `ollama_token`. Otherwise the LLM
     layers of the cascade activate by themselves: slow, non-deterministic and billed.
   - `_horario_de_oficina` — pins `horario.ahora` to Mon 2026-08-24 10:00 `BUSINESS_TZ`. Since fase 3
     the bot appends an out-of-hours notice, so unpinned the expected reply text depends on the wall
     clock: green at noon, red at 3 a.m., with nobody understanding why.
2. **There are two engines, and they are not the same one.** `_isolated_db` swaps `db.engine`
   (engine A); the `session` fixture builds its own (engine B). `db.get_session()` is
   `Session(engine)` resolved at call time, so a test using `session` while the route opens its own
   session is quietly split across two databases. Rebind, or drive everything through `get_session()`.
3. **`_isolated_db` calls `SQLModel.metadata.create_all()`, never `init_db()`.** Columns are fine
   (they are declared on the models; `_add_missing_columns` only retrofits an existing file DB), but
   the hand-written `CREATE UNIQUE INDEX ux_message_wa_message_id` (app/db.py:143) is missing — what
   you get from `Field(index=True)` is the NON-unique `ix_message_wa_message_id`, so duplicate
   `wa_message_id` rows are accepted and a webhook-idempotency test passes for the wrong reason.
   Tests that rely on it build their own engine and call `db.init_db()` — see the `engine_produccion`,
   `panel` and `conversacion` fixtures.
4. **Mutation is the acceptance standard.** Suite green → edit the exact line in `app/` the test claims
   to protect → `pytest tests/test_x.py -q`, then the full suite → if still green the test is
   decorative: fix the test, not the mutation → revert, confirm green. Record the mutations tried.
   Calibration: four of the eight fase-3b mutations survived the FIRST version of those tests
   (docs/ANALISIS-BRECHAS-ERP-CRM.md:1022-1028).
5. **Prefer the path that cannot lie.** Twelve `assigned_to` assertions seeded the ticket by hand and
   the only two using the real flow looked at `status`, which does not change — so the whole suite
   stayed green while the alta changed owner semantics (test_fase3b_asignacion.py docstring:7-11).
6. **Cover every call site, not just the helper.** Disconnecting all FIVE emission points of the
   out-of-hours notice left the suite byte-identical; the feature could be switched off silently. The
   fix was tests through `handle_incoming()` (test_fase3_horario.py:287-292, 325-346).
7. **`TicketActivity.action` carries a payload after a colon** (`asignado-auto:ana`,
   `soltado-por-admin:beto`, `tomado-de:ana`, `estado:<valor>`, `reasignado:<destino>`) and the row set
   grows every fase. Assert membership of a `(username, action)` pair or a substring —
   test_fase3b_asignacion.py:130-134 and test_fase3_horario.py:281-284 are the shapes to copy.
8. **`session.expire_all()` before re-querying** when the route wrote through its own session
   (test_conversation.py:96); **`session.refresh(obj)`** when you already hold the instance
   (test_claim.py:43, 54, 64).
9. **A file-level autouse that flips a feature flag must be reversible.** `_reparto_encendido` turns on
   `settings.reparto_automatico` (which ships OFF, an admin enables it from the Estudio); the three
   tests that measure the switch set it back to False, or the file would measure the switch, not the rule.
10. **Expiring canaries are legitimate here.** `test_la_lista_de_festivos_no_se_ha_quedado_corta` goes
    red six months before `app/horario.json` runs out of holidays and its docstring tells the next
    person exactly how to refill it (LFT art. 74). Copy that pattern for any closed list with a shelf life.

## Common mistakes
- Asserting `len(activities) == N` or list equality on `TicketActivity`: fase 3b added a
  `sistema / asignado-auto:{asesor}` row to every alta and broke that shape everywhere.
- Using the `session` fixture with a route (`claim_ticket`, `inbox_send`, `release_ticket`) and
  forgetting `monkeypatch.setattr(db, "engine", session.get_bind())` — the assert reads engine B while
  the route wrote engine A, and the test fails (or passes) for the wrong reason.
- Reading a value the route wrote without `expire_all()`: the identity map hands back the stale object.
- Seeding one prior advisor for a "pegajosa" (sticky-assignment) test: with one row, `desc()` and
  `asc()` are indistinguishable and the mutation survives. Seed two with explicit `created_at`.
- Testing only `horario.abierto()` / `asignacion.elegir()` and calling the feature covered — that is
  exactly the gap mutation #6 exposed.
- Letting a test depend on the production catalog seed. Seed your own, or neutralize requirements with
  `catalog.update_service(..., requirements="")` like `_sin_requisitos` in test_fase3b_asignacion.py:72.
- Treating `pymssql`/MSSQL connection errors in the output as failures. `archive.enabled()` is
  `bool(mssql_host and mssql_password)`, the conftest does NOT clear it, and every write goes through
  `_safe()` which never raises into the request path. Expected noise — unless your test is threaded.
- Writing a new test file and calling it done without mutating. See principle 4.
- Adding a fifth autouse fixture to `conftest.py` for a single file's need. Local fixtures instead:
  `_reparto_encendido`, `engine_produccion`, `panel`, `conversacion` all live in their own file.

## Quality bar
- Every test name is a sentence about behavior in Spanish (`test_el_alta_por_el_flujo_real_deja_el_folio_con_dueno`).
- Every non-obvious test carries a docstring saying **what breaks in the business** if it goes red —
  not what the code does. The existing files are the reference; match that register.
- Every assert carries a message when the failure would otherwise be unreadable
  (`assert tickets[0].assigned_to == "ana", "el folio nació huérfano"`).
- Where a test deliberately does NOT assert something, say so and why (test_concurrency.py:423-429
  explains that PK uniqueness is the schema's job, so no mutation of `app/` could break it).
- Full suite green before and after; no test left order-dependent on another file.

## Security
- A "must never be called" double must escape `except Exception`. `RedProhibida`
  (test_concurrency.py:87-95) inherits `BaseException` on purpose: `nlu.deduce_explained` wraps the LLM
  layer in `except Exception` and falls back to keywords, so a normal exception would make a real
  network leak look exactly like a legitimate fallback.
- Never let a test read a live credential. `_stripe_en_modo_prueba` and `_nlu_sin_red` exist for that;
  if you add a provider, add the same kind of blanking rather than trusting the dev's `.env`.
- Panel tests must use `base_url="https://testserver"`: `SessionMiddleware(https_only=settings.secure_cookies)`
  (app/main.py:115) and `secure_cookies` defaults to True (app/config.py:26). Over plain `http://` the
  cookie is dropped, login silently fails, and an assertion on `.text` is really asserting on the login page.
- Security controls get mutation tests too: test_twilio_channel.py:101-116 parametrizes `mutacion` and
  asserts that altering ANY form parameter invalidates the signature. Do the same for the Meta
  HMAC-SHA256 check and for any authz guard you touch.
- Guardrails on model output are tested as behavior, not as prose: `test_cualquier_monto_se_descarta`
  (test_rag.py:196), `test_el_corpus_no_lleva_precios` (test_rag.py:247),
  `test_inyeccion_que_induce_precio_se_descarta_como_dinero` (test_guardrails.py:98). Any new LLM path
  needs its equivalent — the bot never quotes a final price.

## Performance
- `create_engine("sqlite://")` defaults to `SingletonThreadPool`: a second thread sees an EMPTY
  database and the test passes for the wrong reason. Anything crossing a thread boundary needs
  `poolclass=StaticPool` (test_fase3_horario.py:299-301, test_fase3b_asignacion.py:506-508) or a
  file-backed engine with the real PRAGMA listener (`engine_produccion`, test_concurrency.py:122-138).
- Force overlap with `threading.Barrier`, never `sleep()`. `en_paralelo()` (test_concurrency.py:220-227)
  synchronizes n threads on a barrier; `BARRERA_TIMEOUT = 20.0` is a hang guard, not synchronization.
- Threaded tests must set `settings.mssql_host = ""`: `archive`'s `_engine` / `_ready` globals are not
  thread-safe and each insert would fire an `after_insert` connecting to MSSQL from N threads
  (test_concurrency.py:105-110).
- The `catalog` singleton is module-global and shared by the whole suite. Warm it before starting
  threads (`catalogo_tibio` asserts `len(catalog.categories) == 9`) and restore it on teardown
  (`catalog._refresh()` in `engine_produccion`) if your test mutated it.
- Going through `handle_incoming()` is slower; pay for it only where the real path is the only
  non-lying witness. Everything else belongs at helper level.

## Definition of done
- [ ] New/changed tests are green, and the full suite is green.
- [ ] At least one mutation per new rule was applied, seen red, and reverted; the list is in the PR body.
- [ ] No test writes the real `tramites.db`, opens a socket, or depends on the wall clock.
- [ ] Engine binding is explicit wherever the code under test opens its own session.
- [ ] `TicketActivity` assertions are membership/substring, never counts or list equality.
- [ ] Any expected failure output (MSSQL) is called out so the next reader does not chase it.
- [ ] Docstrings say what breaks in the business, in Spanish, matching the surrounding files.
