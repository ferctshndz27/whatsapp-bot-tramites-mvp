---
name: ai-ml-senior
description: Úsalo al tocar cualquier parte de IA del bot — la cascada NLU de app/nlu.py (deduce_explained, capas literal/sinonimo/keyword/ollama/llm/fuzzy, umbral difuso), los prompts y el esquema del clasificador, la capa informativa RAG de app/rag.py y sus guardrails, el nodo agéntico con tools de app/agent_tools.py, la minería de sinónimos y su aprobación humana en /unmatched, las perillas del Estudio en app/agent_settings.py, la telemetría NluEvent, o el benchmark scripts/eval_nlu.py.
---

# AI/ML senior — NLU cascade, RAG, agentic node

## Purpose

Own the model-facing layers of this bot: intent deduction, the informative (RAG) layer, the
bounded agentic node, synonym mining, and the telemetry that measures all of it. Every change here
either quotes the right trámite to a real client or the wrong one — there is no third outcome.

## When to use

- Editing `app/nlu.py`, `app/rag.py`, `app/agent_tools.py`, `app/synonym_miner.py`,
  `app/agent_settings.py`, `app/agente_studio.py`, or `scripts/eval_nlu.py`.
- Changing a prompt, a JSON schema, `settings.fuzzy_threshold`, `ollama_model`, `num_predict`,
  `num_ctx`, a timeout, or a guardrail regex.
- Adding a `capa` to `NluEvent`, a tool to `_TOOLS`, or a retriever to `rag._retriever()`.
- Diagnosing "the bot stopped understanding" from the `/unmatched` donut or `/agente`.

## When NOT to use

- Conversation state machine, WhatsApp interactive limits, folio/ticket lifecycle → that is
  `app/conversation.py` + `app/whatsapp_client.py`, not this skill.
- Pricing, charging, Stripe/Twilio, `app/cobranza*.py` — the model never touches money.
- Catalog CRUD through `/catalog`, MSSQL mirror plumbing, schema migrations (see AGENTS.md).

## Responsibilities

1. Keep the cascade order intact: cheapest deterministic layer first, paid/slow layer last.
2. Validate every model output **in code**, never trust the prompt alone.
3. Emit exactly one countable `NluEvent` per inbound message; extra rows go in `CAPAS_EXTRA`.
4. Keep `nothing auto-learns` true end to end.
5. Measure before and after with `scripts/eval_nlu.py`; kill heuristics the numbers don't support.

## Analysis checklist

Before touching the cascade, re-read `nlu.deduce_explained` (app/nlu.py:132) and confirm the order
against the code — line anchors drift:

1. `len(cleaned) < 3` → `"ninguna"`.
2. `_exact_matches()` (app/nlu.py:510) — literal name/synonym as a whole word, else `keyword`.
   **Free, instant, runs BEFORE any LLM.** A single match short-circuits; a tie is real ambiguity
   and deliberately falls through to the LLMs.
3. `es_vago()` (app/nlu.py:121) computed here, used to veto the LLM's `service_id`.
4. Ollama (`settings.ollama_url`) → `_deduce_ollama`, capa `"ollama"`.
5. Claude (`settings.anthropic_api_key`) → `_deduce_llm`, capa `"llm"`.
6. `_deduce_keywords_explained` → literal/keyword again, then `fuzzy`, then `categoria`, then
   `"ninguna"`. Both-null from an LLM is **not** a verdict: these layers see synonyms the miner
   learned that the prompt never carried, and they are free.

Then check: does the change alter `capa` names? `CAPA_COLORS` (app/synonym_miner.py) and the
`NluEvent.capa` comment block (app/models.py:436-441) must be updated too, or the new layer paints
grey `#64748b` in the donut and nobody knows what it is.

## Technical principles

- **`ollama_chat()` (app/nlu.py) is the single funnel** for every open-model call: classifier, code
  typing, RAG, agentic node, miner — whether the model is the self-hosted Ollama or OpenRouter
  (`settings.modelo_abierto` picks `_chat_ollama` / `_chat_openrouter`; the same JSON schemas travel
  as `response_format`, `strict` only when the schema closes its properties; callers always get
  Ollama's message shape). Code typing (`clasificar_codigos`) stays Ollama-only: raw codes never
  leave to a third party. `temperature: 0`, `think: false` (a WhatsApp client feels every
  second). Failures set the `_FALLO_OLLAMA` ContextVar — a ContextVar, not a global, because sync
  FastAPI endpoints run in a threadpool and a global would cross-attribute one client's outage to
  another. `conversation._log_fallo_ollama` consumes it, so one outage never logs twice.
- **Structured outputs everywhere**: `_SCHEMA`, `_CODIGOS_SCHEMA`, `rag._FORMATO_RESPUESTA`,
  `_MINE_SCHEMA`. Ollama's `format` and Claude's `output_config`/schema constrain decoding so
  chatter cannot exist outside the field.
- **`_validated()` (app/nlu.py:295) is the pattern**: any id the model returns is looked up in the
  catalog and silently dropped if invented. Same shape in `clasificar_codigos` (only codes actually
  sent back, only `_TIPOS_CODIGO`), `mine_synonyms`, and `agent_tools._cotizar_servicio`.
- **Prompt caching on the stable prefix**: `cache_control: {"type": "ephemeral"}` on the catalog
  block in `nlu._deduce_llm`, `rag.answer_explained`, `agent_tools._loop_anthropic`,
  `synonym_miner.mine_synonyms`. `nlu.catalog_listing()` is the single source of that listing.
- **`num_ctx` is not optional for long prompts**: without it Ollama silently truncates past 4096.
  `rag.OLLAMA_NUM_CTX = 8192` is used by the agentic node; RAG deliberately does **not** set it
  because `TopKLexical` already bounds the prompt and each distinct `num_ctx` forces Ollama to
  reload a different runner when alternating with the classifier.
- **`rag._docs()` is the privacy boundary** — the only origin of model context: catalog name +
  category + `requisitos_texto` (never `price_info`), enabled `InfoDoc` rows, `INFO_DOCS_PATH`.
  Adding a source there requires answering "could any client already know this?".
- **Fuzzy is last and single-winner**: terms shorter than 3 chars are excluded (`partial_ratio("ur",
  "seguro") == 100`), and it only wins when exactly one service sits within `top_score - 2` of the
  top. `settings.fuzzy_threshold` is clamped 70–95 in `app/config.py:105`.
- **Runtime knobs live in `agent_settings.CONFIG_EDITABLE`** — one `Perilla` per key, and `/agente`
  draws its slider from that descriptor. Ranges used to live twice (Python + `agente.html` min/max)
  and drifted. Secrets and URLs never go here; they stay in `.env`.

## Common mistakes

- Putting a new layer *before* `_exact_matches`. A local CPU classification costs ~5 s; charging it
  to "quiero mi acta de nacimiento" is paying to guess what was already said.
- Matching synonyms by substring. The production catalog ships the synonym `"Ur"`, which lives
  inside `urge`, `seguro` and `buro` — that is why `_contains_term` uses `(?<![a-z0-9])…(?![a-z0-9])`.
- Trusting the prompt. `qwen3:4b` ignores "máximo 30 palabras" and ignores the category rule; that
  is why `rag._evaluar_respuesta` and `nlu.es_vago` exist as **code**, not as prompt lines.
- Letting RAG validate money by "the figure exists in the corpus". With 58 services in one blob,
  *another* service's price also exists. `_DINERO_RE` drops the whole answer instead.
- Auto-approving a mined synonym. `persist_proposals` writes `status="pendiente"`; only
  `/unmatched/proposals/{id}/approve` calls `catalog.add_synonym` (which writes a `CatalogAudit`
  row and `log_admin`). A mis-learned synonym mis-quotes a real client.
- Logging tool calls through `conversation._log_nlu`. It drops greetings and courtesies by design
  (`_social_kind`); `agent_tools._registrar_uso` writes `NluEvent` directly for that reason.
- Adding a `capa` without adding it to `CAPAS_EXTRA` when it is an *extra* row of the same message —
  it would inflate the denominator in `synonym_miner.nlu_metrics` and `alerts._no_entendidos_stats`
  and silence the "no entendidos" alert exactly when it should fire.

## Quality bar

- `tests/test_nlu_regression.py` runs against the **real production seed catalog** (`SEED_PATH`, 58
  services), not a toy one, and enforces `MIN_ACCURACY = 95.0` on *direct* resolution. `Caso.tipo`
  is `servicio | categoria | vaga | ruido`; a `vaga` case must return `(None, None)` plus a
  mini-menu containing the right option, and a `ruido` case must produce `capa == "ninguna"` with
  **zero** suggestions.
- `scripts/eval_nlu.py` is the only way to get real numbers: full cascade against the labs Ollama,
  per-capa accuracy and p50/p90 latency, `--sin-ollama` for the baseline, `--json` for the detail.
  Never in CI (external service, minutes, non-deterministic).
- Measured baseline to beat (2026-08-19/20, corpus of 88 cases): capa `ollama` 47.6% → **76.2%**
  after the `es_vago` guardrail; direct resolution **98.44%** (63/64); p90 **5.2 s**. Compare
  against `data/eval-guardrail.json`.
- **Kill what the data doesn't support.** The ambiguity-by-`suggestions()` rule was discarded: it
  broke 3 working cases ("aktta de matrimonio porfa", "recibo de cfe", "constancia de antecedentes
  no penales") to fix 2. Do not re-propose it without a fresh measurement.

## Security

- `agent_tools.PERMISOS` is the floor: `"cobrar": "deny"`, and no tool that moves money exists.
  `tools_expuestos()` filters to `allow`; `_encendida()` adds the operational flag *on top* —
  turning a permitted tool off is reversible from `/agente`, turning a denied one on is impossible
  by any path. `tests/test_agent_tools.py:76` locks this.
- `consultar_folios` takes **no arguments on purpose**: the phone is injected by the server from the
  conversation, so a client dictating another number cannot read someone else's folios.
- `agent_tools._respuesta_valida`: every digit run in the final answer must exist as a *complete*
  number in some `tool_result` (`_MILES_RE` strips thousand separators so `"500"` cannot ride inside
  `"1,500"`), and any money mention requires a figure that came from `cotizar_servicio`. "cuesta
  ochocientos pesos" written in words is dropped whole.
- `rag._evaluar_respuesta` drop reasons: `meta` (leaked `qwen3` chain-of-thought in English),
  `larga` (over `MAX_RESPONSE_WORDS = 45` with no complete sentence to keep), `pii` (`_CURP_RE` /
  `_TELEFONO_RE`), `dinero`. `GUARDRAIL_MOTIVOS` counts those as fires; a `NO_SE` is the model
  declining, not a guardrail — do not conflate them in metrics.
- `agente_studio` applies the *same* content rule when an admin **saves** an `InfoDoc` paragraph: a
  corpus born clean cannot leak what it does not contain.
- Client text sent to a model is truncated to `MAX_INPUT_CHARS = 500` (app/nlu.py:65, app/rag.py:46).

## Performance

- Free layers first is the whole cost strategy: ~90% of traffic is resolved by the deterministic
  cascade at p50 1–2 ms and zero API spend. Ollama runs on own infra (no API bill); Claude is the
  fallback with `timeout=8.0, max_retries=1` in `nlu._get_client` — a WhatsApp webhook cannot wait.
- Long-prompt layers set their own floor: `rag._timeout_prompt_largo()` = `max(45.0,
  settings.ollama_timeout)`, because `OLLAMA_TIMEOUT` (default 12 s) is calibrated for the short
  classifier prompt and the CPU at labs needs longer to prefill.
- `TopKLexical` (`MAX_CHARS = 4000`, 5-letter prefixes, `settings.rag_top_k`) exists because the
  full ~4-5k-token corpus took over a minute of prefill and the layer died on timeout. Zero-overlap
  documents don't travel: empty corpus → `NO_SE` beats truncating.
- `num_predict=90` in RAG (~10 tok/s on that CPU): every extra token is a second the client waits.
- `agent_tools.responder` calls `session.commit()` **before** the tool loop: the webhook session
  arrives with the `NluEvent` INSERT pending, and holding the SQLite write lock across ~16 s of
  network calls makes every other writer fail with "database is locked".
- Known, accepted waste: the ~13 corpus cases that end in `"ninguna"` still spend ~4.2 s asking
  Ollama for nothing. Documented, not yet worth fixing.

## Definition of done

- [ ] The cascade still resolves cheap-before-expensive, and a tie still falls through instead of
      being resolved by the cheap layer.
- [ ] Every model output is validated in code against the catalog / a regex, never only by prompt.
- [ ] `pytest tests/test_nlu.py tests/test_nlu_regression.py tests/test_guardrails.py
      tests/test_agent_tools.py tests/test_learning.py tests/test_observability.py -q` passes, with
      `MIN_ACCURACY` still ≥ 95.0.
- [ ] `scripts/eval_nlu.py --json` run locally against the real Ollama, compared to
      `data/eval-guardrail.json`; per-capa accuracy and p90 reported in the PR body — no regression
      accepted without a number that justifies it.
- [ ] New `capa` values added to the `NluEvent` comment, `CAPA_COLORS`, and `CAPAS_EXTRA` if the row
      is extra rather than the message's own verdict.
- [ ] New runtime knob added as one `Perilla` in `CONFIG_EDITABLE` (never a hardcoded range in the
      template, never a secret).
- [ ] Nothing new learns by itself: any catalog mutation still goes through a human approval in
      `/unmatched` and leaves a `CatalogAudit` + `log_admin` trail.
