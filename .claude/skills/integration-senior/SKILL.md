---
name: integration-senior
description: Úsalo al tocar cualquier integración externa de este repo — webhook de Meta Cloud API o de Twilio, verificación de firma HMAC, idempotencia de mensajes entrantes, interactivos de WhatsApp (listas, botones y sus límites), el puerto app/messaging.py, cobros y webhook firmado de Stripe, o las llamadas a Ollama en labs — y al agregar un proveedor, reintentos, timeouts o degradación.
---

# Integration Senior

## Purpose

Own the four live outbound/inbound integrations — Meta Cloud API, Twilio (a **second, independent**
WhatsApp account), Stripe, and the Ollama box in labs — so that no external service can break the
request path, duplicate a charge, or leave a client without an answer.

## When to use

- Touching `app/whatsapp_client.py`, `app/twilio_client.py`, `app/messaging.py`, `app/webhook.py`,
  `app/stripe_client.py`, `app/stripe_webhook.py`, `app/cobranza_jobs.py`, `nlu.ollama_chat`.
- Adding a provider, an interactive message shape, a retry/timeout policy, or a signature check.
- Diagnosing "no reply", "the status landed on the wrong message", "the charge was created
  twice", "Ollama went down and the bot stopped understanding".

## When NOT to use

- Conversation state machine logic (`app/conversation.py`) or catalog/NLU ranking (`app/nlu.py`
  cascade order) — those are other domains; here you only own the transport layer they call.
- Charge/state rules: `app/cobranza.py` is the **only** module allowed to mutate `cobro.estado`.
- The MSSQL mirror (`app/archive.py`) — it is not an integration, it is a replica; see AGENTS.md.
- Queues, brokers or device protocols: this repo has none.

## Responsibilities

1. Every outbound call goes through the single port `app/messaging.py`. Callers say **to whom**,
   never **by which channel**; a new provider is one branch in `messaging._adaptador()`.
2. Every inbound call is authenticated **before** anything is read or written.
3. Every inbound call is idempotent — providers retry, and a retry must not re-run the flow.
4. Every external failure has a named degradation: next layer, plain-text fallback, or a queued
   row that a sweep (`cobranza_jobs.barrer_todo`) resolves. Never a 500 into the request path.
5. Retry policy is uniform and bounded: `MAX_RETRIES = 3`, `delay *= 2`, `REQUEST_TIMEOUT_SECONDS = 10`.

## Analysis checklist

Before editing an integration, answer these against the code:

- **Which port?** Does the caller use `messaging.send_*` (correct) or import `whatsapp_client` /
  `twilio_client` directly (wrong, except inside `messaging._adaptador`)?
- **Which channel?** `messaging.canal_de(phone)` reads `Conversation.provider`, defaulting to
  `PROVIDER_META`. Is `provider` threaded explicitly from `webhook.handle_inbound` down to delivery?
- **Signature.** Meta: HMAC-SHA256 over raw bytes, header `x-hub-signature-256` with `sha256=`
  prefix (`webhook._valid_signature`). Twilio: HMAC-SHA1 base64 over the **full public URL** plus
  POST params sorted alphabetically, name+value concatenated (`twilio_client.verify_signature`,
  URL from `twilio_client.callback_url` → `PUBLIC_BASE_URL`). Stripe: HMAC-SHA256 over
  `f"{t}." + raw`, 300 s tolerance, **all** `v1` candidates compared
  (`stripe_webhook._verificar_firma`). All three fail closed with no secret.
- **Idempotency — three distinct mechanisms, do not blur them:**
  - Inbound WhatsApp: `conversation._already_processed()` — a SELECT on `Message.wa_message_id`
    with `direction == IN`. Not a DB constraint.
  - Inbound Stripe: the INSERT against the unique on `EventoPasarela.stripe_event_id` **is** the
    dedup ("sin ventana de carrera"), inside a deliberately tiny transaction.
  - Outbound Stripe: `IntentoCobro.clave_idempotencia = f"cobro:{cobro.id}:{numero}"`, written
    ahead — the row must be **committed before the first network call**.
- **Interactive limits** (constants in `whatsapp_client`, re-declared on purpose in
  `twilio_client`): `MAX_LIST_ROWS = 10`, `LIST_TITLE_MAX = 24`, `LIST_DESC_MAX = 72`,
  `LIST_BODY_MAX = 1024`, `MAX_BUTTONS = 3`, `BUTTON_TITLE_MAX = 20`, `CAPTION_MAX = 1024`.
  A title longer than 24 is truncated and the full name spills into `description`.
- **Degradation.** What answers if this service times out? Name it.
- **Test lock.** Is there a test that pins the behaviour you are about to change?

## Technical principles

- **Two accounts, no failover.** Meta and Twilio are independent WhatsApp accounts sharing this
  CRM. You always reply through the number the message came in on: the 24 h service window is
  **per number** (`app/ventana.py`, `WINDOW_HOURS = 24`), and replying via the other account puts
  the business out of window with a sender the client does not recognise.
- **Adapters absorb provider shape.** `twilio_client` alone converts `whatsapp:+52…` ↔ `52…`,
  maps `MessageStatus` to Meta's vocabulary through the explicit `STATUS_MAP`, and degrades
  interactives to numbered text. Nothing above the adapter learns a provider format —
  `webhook._process_message` and `twilio_client.parse_inbound` both normalise at the edge because
  `client`'s PK is the phone.
- **Two independent fallback layers.** (1) `webhook._deliver`: if `messaging.send_list` /
  `send_buttons` returns falsy, it re-sends `item["text"]` as plain text. (2) inside
  `twilio_client.send_buttons`: if the Content API template (`ContentSid`) fails, it falls back to
  `send_text(_numerar(...))`. Keep both.
- **Retryable status sets differ on purpose.** Meta/Twilio: `{429, 500, 502, 503, 504}`. Stripe
  **adds 409** (`idempotency_key_in_use`) — with the same key Stripe returns the original object,
  never a second one. `whatsapp_client.send_media` has **no** retries (marked `ponytail`).
- **Stripe answers three things, not two.** `RespuestaStripe.indeterminado=True` means "the request
  may have landed" (network error, 5xx, exhausted 409) — it is not a rejection. The intento stays
  `enviando` with `respondido_at` NULL, `ux_intento_vivo` blocks a second POST, and only
  `cobranza_jobs.intentos_huerfanos()` resolves it.
- **Idempotency params must be time-free.** `cobranza._parametros_creacion` derives everything from
  the two rows; `expires_at` comes from `intento.solicitado_at`, never from "now". Reusing a key
  with different params makes Stripe error instead of returning the original — which is exactly
  what the sweep's retry needs.
- **Ollama has one call site**: `nlu.ollama_chat()` (used by the classifier, `clasificar_codigos`,
  the informative layer and `agent_tools`). Bearer from `OLLAMA_TOKEN` behind nginx, `think: False`,
  `temperature: 0`, `connect=2.0`, `OLLAMA_TIMEOUT` default 12 s. Transport failures land in the
  `_FALLO_OLLAMA` **ContextVar** (not a global: sync FastAPI endpoints run in a thread pool),
  consumed once by `limpiar_fallo_ollama()` and logged as layer `ollama_error`.
- **Degradation ladder is the product.** Ollama down → the next NLU layer answers.
  `nlu.clasificar_codigos` returns `{}` → **the folio is created anyway** and a human validates.

## Common mistakes

- Treating `indeterminado` as "rejected" and POSTing again. *Crear a ciegas ES el cobro duplicado.*
- Rewriting `stripe_client.modo_real()` as `startswith("sk_live")`. It is `"_live_" in key` because
  restricted keys (`rk_live_…`) exist; misclassifying one made the applier discard **real** payment
  events as "modo no coincide". Found against a live account.
- Dropping the `provider` filter in `webhook._tag_sent_message`. With two accounts writing to the
  same client, a Meta wamid attaches to a Twilio outbound row and the delivery status lands on the
  wrong message.
- Assigning instead of merging `pricing` in `webhook._apply_status`. Meta sends it on `sent` and on
  **one** of delivered/read, never all three; assigning erases it and `app/medidor.py` counts the
  message as "sin dato".
- Adding `_numerar` to `twilio_client.send_list`. It is omitted deliberately: menu bodies are
  already numbered by `conversation.presentation` (MENU_MARKER), so re-appending duplicated the
  whole menu in the chat. `send_buttons` **does** need it — its body lists nothing.
- Building the Twilio signature from `request.url` instead of `twilio_client.callback_url()`.
  Behind the proxy the app sees the internal host and `http://`, so **every** signature fails.
- Returning TwiML from `/webhook/twilio`. The reply goes out through the API like Meta's; TwiML
  would duplicate the message. The route answers 204.
- Returning 200 when the Stripe event could not be **persisted**. Only `IntegrityError` (replay) is
  200; any other write failure is **503** so Stripe retries — confusing "duplicate" with "could not
  write" loses OXXO events forever. Failing while *applying* still returns 200: the sweep reprocesses.
- Calling `ollama_chat` with a long prompt and no `num_ctx`. The server default (4096) truncates
  silently and the model answers without having seen the start of the prompt.
- Doing network I/O in the async webhook body. Both routes wrap the sync chain in
  `run_in_threadpool` — uvicorn runs a single worker and would block the whole process ~8 s/message.
- Reading secrets from `os.environ` in a client module. Everything comes from `app.config.settings`
  (note: `public_base_url` is already assigned twice there, in the Twilio and Stripe blocks — same
  value; do not add a third).

## Quality bar

- No `raise` escapes an outbound client: on failure they return `None` / a `RespuestaStripe`.
- Errors log the diagnosable payload: Meta's body (it carries codes like `131047` = out of the
  24 h window), Stripe's `Request-Id` (`req_…`), Twilio's `MessageSid` — with `utils.mask_phone`
  on every phone number.
- A missing credential makes the integration **inert**, never half-working:
  `twilio_client.enabled()` is False, `stripe_client._request` returns `sin_credenciales`,
  `whatsapp_client._send` returns None.
- Provider constants come from `app/models.py` (`PROVIDER_META`, `PROVIDER_TWILIO`), never literals.

## Security

- Never accept an unverified inbound. `verify_signature` returning False without a token is
  deliberate: accepting would let anyone inject messages and advance other people's conversations.
- Compare digests with `hmac.compare_digest`, always; read the **raw body** before any `json()`.
- Rejected Stripe signatures go to the log, never to `eventopasarela` — otherwise anyone on the
  internet fills the table.
- `agent_tools.PERMISOS` is the single source of tool permissions: `"cobrar": "deny"` and **no
  money-moving tool exists**. The client's phone is injected server-side from the conversation and
  is never a tool argument; model arguments are validated against the catalog; the final answer is
  validated in code by `_respuesta_valida`; `MAX_TURNOS = 3`.
- `twilio_client.send_media` returns None **without sending** when `PUBLIC_BASE_URL` or
  `SESSION_SECRET` are missing: Twilio downloads from a URL, and `MEDIA_DIR` holds client
  documents. Short-lived signed URLs come from `media_store.signed_media_path` — never publish the
  directory.
- LLM output is never an identifier: `nlu._validated` / `clasificar_codigos` drop anything absent
  from the catalog or from the requested strings.

## Performance

- One round trip per message where possible; `messaging.canal_de` is a PK SELECT per send (marked
  `ponytail`: pass `provider` explicitly when you already hold the conversation).
- Timeouts are the budget: 10 s per HTTP call, 12 s Ollama with `connect=2.0`, 8 s + 1 retry for
  the Anthropic client (`nlu._get_client`). The webhook cannot wait longer than the client will.
- Keep the Stripe persist transaction tiny and the apply transaction separate — a long write on
  SQLite WAL turns into `database is locked` and a 503 storm.
- Do not add a fourth LLM layer: ~90 % of traffic is resolved by the free deterministic path.

## Definition of done

- [ ] Signature, idempotency and degradation each named explicitly in the diff or its comments.
- [ ] Interactive payloads still respect the 10/24/72/1024/3/20 limits, truncating defensively.
- [ ] `pytest tests/test_messaging_port.py tests/test_provider_channel.py tests/test_interactive.py
      tests/test_stripe_webhook.py tests/test_stripe_client.py tests/test_cobranza_jobs.py
      tests/test_media_signed_url.py -q` passes.
- [ ] `tests/test_messaging_port.py::test_sin_credenciales_de_twilio_nadie_sale_por_ese_canal`
      still passes — it is the lock that keeps an unconfigured provider inert.
- [ ] A new mirrored column/table also went through the five places listed in AGENTS.md.
