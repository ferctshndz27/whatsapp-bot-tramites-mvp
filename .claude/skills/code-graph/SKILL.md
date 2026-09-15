---
name: code-graph
description: Usar antes de editar o renombrar código que no conoces — quién llama a una función, qué toca, qué rompe un cambio, por qué capas pasa una petición. Se le pregunta al grafo local de graphify (explain, affected, path), construido solo desde el código, sin modelo y sin red. No sirve para datos, plantillas ni documentos, y no reemplaza leer el archivo antes de editarlo.
---

# Code graph — quién llama a quién, preguntado a un grafo local en vez de a grep

## Purpose

Answer structural questions about this repo — who calls `normalize_phone()`, what
`handle_inbound()` reaches, how many call sites a change to `ruta_interna()` touches — from a call
graph instead of a chain of greps and file reads. graphify builds the graph from the code alone
(tree-sitter AST, no model, no network) into `graphify-out/`, which git ignores and each machine
rebuilds in seconds.

It is a map, not the territory. It tells you where to look; the file tells you what is there. Every
edit still starts by reading the lines the graph pointed at.

## When to use

- Before editing a function you have not read this session: `graphify affected "<name>"` lists its
  callers with file and line — the blast radius. For `normalize_phone` that is the mirror, the client
  directory, the DB migration, the router, both inbound adapters and its own tests.
- Tracing a request through the layers: `graphify explain "handle_inbound"` shows the shared trunk
  of `app/webhook.py` — it calls `handle_incoming()`, `presentation()` and `_deliver()`, and is
  called from `_process_message()` (Meta) and `receive_twilio()`.
- Before a rename, a signature change, or deleting something that looks unused.
- Finding the tests that exercise a function: callers under `tests/` appear in `affected`.
- Orienting in an area you have never touched: `graphify god-nodes` names the hubs —
  `get_session()`, `Ticket`, `handle_incoming()`, `User`, `Conversation`, `Cobro`.

## When NOT to use

- **Data flow through the database.** The graph has calls and imports, not rows.
  `graphify path "handle_inbound" "inbox_send"` finds no path, and that is correct: the webhook
  writes `Message` rows and the inbox reads them; neither function calls the other. "Does screen X
  see what the bot received?" is answered by the models and a test, not by the graph.
- **Templates, CSS and docs.** Only code is parsed: `app/`, `tests/`, the JavaScript in `static/`,
  the tracked scripts, `deploy/` and the Gemini hook. Jinja templates, stylesheets and `docs/` are not
  in the graph — search them directly.
- **Production state**: counts, what a client wrote, what the MSSQL mirror holds. That is `/system`,
  the database or the domain skill.
- **The file you are about to edit.** Read it. The graph is only as fresh as its last update.
- **A design decision** — where a rule belongs, whether a table must be mirrored. That is the domain
  skill; this one only finds the code the decision is about.

## Responsibilities

1. Keep the graph fresh before trusting it: `graphify update .` after any code change in the
   session, before the next query.
2. Keep it code-only and local. Nothing in this workflow sends code, docs or node names to a model.
3. Verify every graph claim that drives an edit against the file and line it cites.
4. Keep `.graphifyignore` honest. It excludes what is not the bot — the Flask prototype, the
   landing page, terraform and vendored JavaScript — and nothing the bot runs.
5. Say so when the graph and the code disagree, instead of trusting the graph.

## Analysis checklist

1. **Is there a graph?** Check for `graphify-out/graph.json`. If it is missing, build it:
   `graphify extract . --code-only`, then `graphify cluster-only . --no-label --no-viz` for the
   report.
2. **Is it fresh?** `GRAPH_REPORT.md` names the commit it was built from. If code changed since —
   yours or anyone's — run `graphify update .` (no model involved).
3. **Ask for one exact symbol.** `explain` for a node, `affected` for its callers, `path` between
   two. A bare name defined more than once comes back as `Ambiguous` with every id:
   `_current_user` exists in eight modules (`app_inbox_current_user`, `app_clients_current_user`, …).
   Retry with `app/inbox.py::_current_user` or the id.
4. **Check the edge kind.** `EXTRACTED` edges come from the AST. `INFERRED` ones (about 11 % here)
   are resolved by name — `handle_inbound()` shows an inferred `session()` call that points at the
   test fixture in `tests/conftest.py`. Confirm inferred edges in the file before relying on them.
5. **Read the cited lines, then edit.** Then `graphify update .` so the next question sees it.

## Technical principles

- **What the graph is.** One node per module, class, function and rationale comment; edges for
  `calls`, `imports`, `uses`, `references` and `contains`. Docstrings and `# WHY:` comments become
  `rationale` nodes linked to the code they explain. A fresh build of this repo (2026-09-15,
  graphify 0.9.62) had 145 files, about 4,200 nodes and 12,100 edges, 89 % of them extracted.
- **Scope follows git.** graphify reads `.gitignore`, `.git/info/exclude` and `.graphifyignore`, so
  `.env`, `data/`, `tramites.db`, `docs/` and the operator's private folders never enter. Tracked
  files under an excluded folder still do: the tracked scripts are in, the local ones are not.
- **Ids are path-qualified** (`app_webhook_handle_inbound`). Use them, or `path::symbol`, whenever a
  name is ambiguous.
- **`query` is a breadth-first walk with a token budget.** A natural-language question starts from
  several nodes and truncates at 2,000 tokens; one test question returned 92 of 1,119 reached nodes.
  Prefer `explain`, `affected` and `path`; use `query` with a narrow term, and `--budget`, only to
  discover names.
- **Communities are named without a model.** `update` names them after their hub node, and
  `cluster-only --no-label` leaves numbered placeholders. Neither calls a backend.
- **The graph is per machine and out of git.** CI and the Docker image never see it, and
  `tests/test_harness.py` does not require it: it only checks cited paths under known prefixes, so
  a clean clone without `graphify-out/` stays green.
- **graphify is an operator tool, not a bot dependency.** It lives in a virtualenv under
  `~/.local/share/graphify/` with its command linked into `~/.local/bin/`. It is not in
  `requirements.txt` and must not be: the image would carry 26 tree-sitter packages for nothing.

## Common mistakes

- **Running graphify's installers.** `graphify claude|agents|gemini install` append a generic
  section to `CLAUDE.md`, `AGENTS.md` or `GEMINI.md`, and the Claude and Gemini ones register hooks
  in `.claude/settings.json` or `.gemini/settings.json`. `graphify antigravity install` writes rules
  into the operator's `.agents/`, and `graphify install` copies graphify's own skill into the home
  config. That rewrites the harness contract and a policy file the agent may not edit.
  `docs/HARNESS.md` records the decision: adopt the tool, not its installers.
- **`graphify hook install`.** Besides git hooks it creates a `.gitattributes` in the repo root and
  registers a merge driver in git config, both for a `graph.json` this repo never commits.
- **Naming a skill `graphify` under `.claude/skills/`.** A project-scoped graphify install writes
  its own skill there and would overwrite it. This one is `code-graph` on purpose.
- **Trusting a bare name.** `_current_user` resolves to eight different functions; `session` is a
  pytest fixture and a SQLModel session.
- **Reading `GRAPH_REPORT.md` whole** — tens of kilobytes — to answer a question about one function.
- **Reading "no path" as "not connected".** Rows, `fetch` calls from `static/` to routes, and the
  SQLAlchemy listeners in `app/archive.py` never appear as call edges.
- **Committing `graphify-out/`.** graphify recommends it for teams; here it is megabytes of churn per
  rebuild for a single operator, and it stays in `.gitignore`.

## Quality bar

- Every claim like "X is only called from Y" in a plan or a review names the command that produced
  it and was checked against the file.
- The graph was updated after the session's last code edit before it was queried again.
- `git status` shows no graphify output besides the versioned `.graphifyignore`.

## Security

- **Offline commands, the only ones used here:** `extract --code-only`, `update`,
  `cluster-only --no-label`, `explain`, `affected`, `path`, `query` and `god-nodes`.
- **Off-limits, because they send content to a model:** the `/graphify` skill, `graphify extract`
  without `--code-only`, `graphify label`, `cluster-only` without `--no-label`, `graphify add`,
  `--dedup-llm` and `graphify prs --triage`. Without an explicit backend graphify uses the first API
  key it finds in the environment, Gemini first, and its Kimi backend routes to servers in China.
  `docs/` is local-only by the operator's decision, and no part of the repo may leave the machine.
- **Queries are logged** to `~/.cache/graphify-queries.log` (the question, not the answer). Ask about
  code symbols, never about a client: no phones, CURP, RFC or names in a query.
  `GRAPHIFY_QUERY_LOG_DISABLE=1` turns the log off.
- **Never `--no-gitignore`.** It would pull `.env`, `data/` and `tramites.db` into the extraction.

## Performance

- A fresh code-only build takes about 4 s plus 2.5 s for the report. `update` takes about 6 s even
  with nothing changed, because it rewrites `graph.json`, `graph.html` and the report.
- `explain`, `affected`, `path` and `god-nodes` answer in 0.3–0.5 s, most of it loading a graph of
  about 6 MB. One `affected` replaces the grep-and-read loop that finds every caller of a helper.
- `query` spends its 2,000-token default on whatever the question matches first. Narrow the term
  before raising `--budget`.

## Definition of done

- [ ] The graph was fresh — `graphify update .` after the last code edit — when it was queried.
- [ ] Every graph claim that drove an edit was verified in the cited file.
- [ ] Nothing left the machine: no command from the off-limits list was run.
- [ ] No graphify installer or hook was run: `git status` shows no `.gitattributes`, no
      `graphify-out/` and no new section in `AGENTS.md`, `CLAUDE.md` or `GEMINI.md`.
