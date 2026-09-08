---
name: security-senior
description: Úsalo al tocar cualquier frontera de confianza del bot de trámites — firma HMAC de los webhooks de Meta, Twilio o Stripe, idempotencia de mensajes y eventos, cookie de sesión y guards del panel, cabeceras CSP/HSTS/noindex, URLs firmadas de adjuntos, secretos del .env, permisos de herramientas del agente, o datos personales de clientes mexicanos (CURP, RFC, NSS, teléfono) en logs, corpus RAG o respuestas del bot.
---

# Security review — trust boundaries of this repo

## Purpose

Keep the five real trust boundaries of this app intact: the three inbound webhook signatures,
the panel session, and the signed media URL. Everything else in this file exists to stop a
change from silently opening one of them.

## When to use

- Touching `app/webhook.py`, `app/stripe_webhook.py`, `app/twilio_client.py`, `app/media_store.py`.
- Adding or editing any route, middleware or `SECURITY_HEADERS` entry in `app/main.py`.
- Adding a knob to `agent_settings.CONFIG_EDITABLE` or a tool to `agent_tools.PERMISOS`.
- Any code that logs, stores, mirrors or renders a phone, CURP, RFC, NSS or a message body.
- Any change to `app/rag.py` `_evaluar_respuesta` or the `codigos.py` patterns.

## When NOT to use

- Pure NLU accuracy work (`nlu.py` cascade, fuzzy threshold, synonym mining) — no boundary moves.
- Dashboard layout/Jinja/SVG chart work that adds no route and no new header.
- MSSQL mirror plumbing (`archive.py`) — the five-place checklist there is a correctness task,
  not a security one; `_safe()` already swallows its errors outside the request path.

## Responsibilities

1. Name which boundary the change touches, using the map in **Security** below.
2. Verify the verifier still fails **closed** when its secret is absent.
3. Verify idempotency still holds for anything a provider can retry.
4. Verify no client PII widened its blast radius (log, corpus, response, mirror, template).
5. Verify the change did not add a secret to the panel or a tool to `allow`.
6. Add the test that locks the new behaviour (see **Quality bar**) — CI runs only `pytest`.

## Analysis checklist

- [ ] Is the signature computed over the **raw bytes** read before `request.json()` /
      `request.form()`? (`webhook.py:35`, `stripe_webhook.py:70`)
- [ ] Does every comparison use `hmac.compare_digest`? The single `==` on a secret in the repo is
      `verify_webhook` (`webhook.py:28`, Meta's `hub.verify_token` handshake) — do not copy it.
- [ ] Missing secret → `False` / `None`, never a pass-through.
- [ ] New Twilio route: is its exact path string passed to `_form_verificado(request, "<path>")`?
- [ ] New provider retry surface: is there a unique constraint, not just a SELECT-then-INSERT?
- [ ] New panel route: does the handler itself read `request.session.get("user")` and, if admin-only,
      `auth.is_admin(user)`? There is **no** global dependency and no default-deny.
- [ ] Is the route `POST` if it mutates state? There is no CSRF token in this repo.
- [ ] Does any new log line carry a raw phone / CURP / RFC / message body?
- [ ] Does a new middleware that reads `request.session` get registered **before** line 112 of
      `main.py`?

## Technical principles

- **Verify before parse.** `receive_webhook` reads `await request.body()` and calls
  `_valid_signature` (`webhook.py:62-67`, HMAC-SHA256 + `compare_digest`) before `request.json()`.
  Re-serializing the parsed payload to hash it produces different bytes and every signature fails.
- **Twilio signs the public URL.** `twilio_client.verify_signature` (`twilio_client.py:80-102`,
  HMAC-SHA1 → base64 over `url + sorted(params)` concatenated) is fed
  `twilio_client.callback_url(path)`, built from `PUBLIC_BASE_URL` — never `request.url`, which
  behind the proxy is the internal `http://` host and matches no signature.
- **`_form_verificado` is the cross-provider firewall** (`webhook.py:215-230`): a Meta payload POSTed
  to `/webhook/twilio` carries no valid `X-Twilio-Signature` and is rejected whole, not half-processed.
- **Stripe's response contract is money** (`stripe_webhook.py:1-16`): bad signature → 403 with nothing
  written (rejections go to the log, or anyone on the internet fills `eventopasarela`);
  `IntegrityError` on the unique `stripe_event_id` → 200 `duplicado`; **any other** insert failure →
  503 so Stripe retries. Collapsing those two branches loses OXXO payments permanently. A failure
  while *applying* still returns 200 — the row is ours and the sweep reprocesses it.
- **Two layers of idempotency, on purpose.** `conversation._already_processed`
  (`conversation.py:362-370`) covers the normal retry; the partial unique index
  `ux_message_wa_message_id` (`db.py:141-146`, `WHERE wa_message_id IS NOT NULL`) covers two
  concurrent retries. Keep both.
- **Middleware order is load-bearing** (`main.py:106-119`). Starlette wraps the last-added
  outermost, so `_security_headers` seals every response and `session_activity` / `_nav_context`
  sit *inside* `SessionMiddleware` because they read `request.session`.
- **Headers are applied with `setdefault`** (`main.py:76-80`) — a route *can* override one. Never
  override CSP or `X-Frame-Options`. The CSP is `default-src 'self'` with `'unsafe-inline'` for
  style and script only; charts are server-rendered SVG (`activity.py`) precisely so no external
  host is ever needed.
- **Session cookie** (`main.py:112-118`): `https_only=settings.secure_cookies` (default on,
  `SECURE_COOKIES=0` only for local HTTP), `same_site="lax"`, `max_age=SESSION_MAX_AGE` = 12 h.
  SameSite=Lax + POST is the *entire* CSRF defense; `/logout` (`dashboard.py:130`) is the one
  existing GET mutation — do not add a second.
- **Signed media** (`media_store.py:57-89`): `MEDIA_NAME_RE` = `^[a-f0-9]{32}\.(jpg|png|webp|pdf|docx|xlsx)$`,
  HMAC-SHA256 over `name|expira` with `SESSION_SECRET`, `FIRMA_TTL_SEGUNDOS` = 15 min.
  `/media/publico/{name}` (`inbox.py:506-522`) is the only unauthenticated panel route and answers
  **404 for every failure** — bad signature, expired, missing file — so it never confirms what exists.
- **Login** (`dashboard.py:93-127`): `bcrypt.checkpw`; the browser always gets the same generic
  error, while the precise reason (`REASON_NO_USER` / `REASON_DISABLED` / `REASON_BAD_PASSWORD`)
  goes only to `authlog`. Lockout is 5 attempts / 300 s, in-memory per process (`dashboard.py:40-45`).

## Common mistakes
- **Assuming the middlewares authenticate.** The four in `app/main.py` add session state, nav
  context and security headers — **none of them is a guard**. Anything FastAPI mounts on its own
  is therefore public: `/docs`, `/redoc` and `/openapi.json` served the ERP's full route map (98
  paths, `/oficinas`, `/agente/config`, `/system`) to anyone until they were turned off in the
  constructor. Every new mount inherits that: it is public unless its own handler checks the
  session. `openapi_url=None` alone closes all three — both UIs are served *from* the schema — but
  keep the other two explicit so re-enabling the schema does not quietly reopen them.

- Calling `await request.json()` first and hashing a re-serialized body.
- Building the Twilio signature base from `request.url` instead of `callback_url(path)`.
- Assuming a new route inherits a guard from its router siblings. It does not — it is public.
- Registering a session-reading middleware below `add_middleware(SessionMiddleware, ...)`.
- Returning 200 from the Stripe handler when the INSERT failed for a reason other than
  `IntegrityError`.
- "Fixing" `rag._CURP_RE` (`rag.py:78`) to match `codigos.CURP_RE`. It is deliberately laxer: it is a
  leak guard, so it must over-match. Same for `_TELEFONO_RE` = `\d{10,}` applied *after*
  `_SEPARADORES_RE` strips `-`, `.`, `()` and spaces (`rag.py:79-82`), so `55-1234-5678` can't slip by.
- Redefining CURP/RFC/NSS/email patterns inside `conversation.py`. They live once in
  `codigos.py:21-32`.
- Adding a `*_token`, `*_key`, `*_secret` or `*_url` entry to `CONFIG_EDITABLE` so ops can rotate it
  from the panel. Secrets are the VPS `.env`, full stop (`agent_settings.py:8-9`).
- Giving `cobrar` a flag in `FLAG_POR_HERRAMIENTA`. `PERMISOS` is the reviewed-code floor;
  the panel map only *turns off* something already `allow` (`agent_tools.py:133-156`).
- Adding a CDN script or webfont to a template. The CSP blocks it, and widening the CSP is not the fix.

## Quality bar

- Every new guarded route gets a case in `tests/test_authz.py`, which asserts three tiers over real
  HTTP: collaborator is redirected, the mutated state did **not** change, and the same request by an
  admin **does** take effect (the positive control is what catches a typo'd path passing as a 404).
- Every new signed boundary gets the four tests `tests/test_media_signed_url.py` already models:
  valid signature passes, tampered signature rejected, expiry/replay rejected, and **absent secret
  fails closed**.
- `tests/test_security.py` asserts the security headers on `/health` without the lifespan running;
  extend it when adding a header.
- `pytest -q` green before handing the change over.

## Security

Boundary map — verifier, secret, and what a failure returns:

| Boundary | Verifier | Secret | On failure |
|---|---|---|---|
| `POST /webhook` (Meta) | `webhook._valid_signature` (`webhook.py:62`) | `WHATSAPP_APP_SECRET` | 403 |
| `GET /webhook` (Meta handshake) | `webhook.verify_webhook` (`webhook.py:22`) | `WHATSAPP_VERIFY_TOKEN` | 403 |
| `POST /webhook/twilio[/status]` | `webhook._form_verificado` → `twilio_client.verify_signature` | `TWILIO_AUTH_TOKEN` | 403 |
| `POST /webhooks/stripe` | `stripe_webhook._verificar_firma` (`stripe_webhook.py:39`), all `v1=` compared, `t=` within 300 s | `STRIPE_WEBHOOK_SECRET` | 403, nothing written |
| `GET /media/publico/{name}` | `media_store.verify_media_token` (`media_store.py:79`) | `SESSION_SECRET` | 404 (always) |
| Every other panel route | per-handler `request.session.get("user")` + `auth.is_admin` (`auth.py:7`) | `SESSION_SECRET` cookie | 303 → `/login` |

- **`SESSION_SECRET` absent is an outage, not a warning.** `main.py:48-55` falls back to a random
  per-process secret: panel sessions die on every restart *and* `signed_media_path` returns `None`
  (`media_store.py:70-74`), so Twilio attachments stop being sent entirely.
- **`config.CLAUDE_HABILITADO = False`** (`config.py:12`) is a code constant, not an env var, so the
  Claude path cannot be switched on by editing `.env` on the server; while off, `ANTHROPIC_API_KEY`
  is blanked with a warning (`config.py:47-51`).
- **Client PII.** `MEDIA_DIR` holds real client documents — actas, constancias fiscales
  (`media_store.py:51-53`). Logs mask phones with `utils.mask_phone` → `***last4`
  (`webhook.py:96`, `conversation.py:285`). Never log a raw phone, CURP, RFC, NSS or message body.
- **The RAG guardrail order matters** (`rag.py:405-434`): strip `<think>` → `meta` → length trim →
  `pii` → `dinero`. The pii and money checks run on the *trimmed* text, i.e. on exactly what ships
  (`rag.py:418-424`). Put any new guard after the trim for the same reason. `_DINERO_RE` drops any
  money mention because this layer never quotes — that is the product line in code.
- **Open redirect** is guarded once, in `utils.ruta_interna` (`utils.py:58-67`), which rejects `//`
  and `/\`. Route it through there, don't re-implement the check.
- Inbound abuse limits: blocked clients are dropped before the state machine
  (`webhook.py:93-97`, `client.habilitado`); `_is_rate_limited` allows 20 messages / 60 s per phone
  (`conversation.py:181-182, 373`).

## Performance

- The webhook path runs on **one** uvicorn worker. Everything after the signature check is pushed
  through `run_in_threadpool` (`webhook.py:50, 55, 241`) because NLU + provider HTTP + SQLite are
  synchronous and would block the loop for up to ~8 s per message. A new security check that does
  I/O (a network lookup, a DB read) belongs inside that threadpool, never in the `async` handler.
- HMAC runs over bytes already in memory. Do not re-read the body or re-serialize to verify.
- `auth.is_admin` opens a session and queries `user` on **every** call (`auth.py:10-12`). Resolve it
  once per request and pass the boolean into the template; never call it inside a loop.
- `_already_processed` filters `message` on `wa_message_id`; the partial unique index
  `ux_message_wa_message_id` (`db.py:143`) is both the concurrency backstop and the index that keeps
  that lookup cheap as `message` grows. Do not drop it when adding columns by hand.

## Definition of done

- [ ] The touched boundary still fails closed with its secret unset, proven by a test.
- [ ] Signatures compare with `compare_digest` over raw bytes.
- [ ] Retryable inbound events are deduped by a unique constraint, not only by a SELECT.
- [ ] New panel routes carry their own session/admin guard and a `tests/test_authz.py` case.
- [ ] No secret reachable from the panel; `CONFIG_EDITABLE` and `PERMISOS` unchanged, or the change
      is argued explicitly in the PR body.
- [ ] No raw phone, CURP, RFC, NSS or message body added to a log line or a mirrored column.
- [ ] `pytest -q` green.
