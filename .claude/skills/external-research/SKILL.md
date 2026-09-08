---
name: external-research
description: Usar antes de afirmar de memoria un detalle de documentación externa — límites de la Meta Cloud API, plantillas de Twilio, webhooks o versión de API de Stripe, y sobre todo los requisitos oficiales de un trámite (SAT, IMSS, RENAPO) que van a parar al catálogo. La herramienta es la skill global `notebooklm`, que responde solo desde documentos cargados y con cita. Regla dura: nada de este repo se sube a un cuaderno.
---

# External research — respuestas con cita, desde documentos que este repo no debe contener

## Purpose

Answer questions about **external** documentation from the documents themselves instead of from
memory: the Meta Cloud API, Twilio, Stripe, Ollama, and — the case that actually matters here — the
official sources behind the trámites catalog. The tool is the globally installed `notebooklm` skill,
which drives a real browser to a NotebookLM notebook and returns an answer constrained to the
documents in it, with citations.

The value is not "one more search". It is that a requirement copied into `requisitos_texto` can be
traced back to the page it came from. Getting a trámite's requirements wrong tells a real client to
bring the wrong papers to a government office.

## When to use

- About to state a version-specific fact from memory: an interactive-message cap, a template
  approval rule, a webhook signature scheme, a pinned API version.
- Curating the catalog's `requisitos_texto` or the informative corpus from official PDFs.
- Two sources disagree and the question is which one the vendor actually publishes.
- A vendor behaviour changed and the local knowledge is older than the change.

## When NOT to use

- Questions about **this** codebase. That is what the domain skills and the repo's own graph tools
  are for; a notebook knows nothing about `app/rag.py`.
- Anything about live state — deployed SHA, row counts, mirror health. That is `/system`.
- In CI or on the VPS. The skill needs a visible Chrome and a human Google login; it is local
  Claude Code only, by design. Under Gemini CLI there is no equivalent: fetch the official page
  directly and cite it.
- When the document is small, public and a direct fetch answers it. A browser round-trip costs
  seconds and burns one of a limited number of daily queries.

## Responsibilities

1. Keep every byte of this repo out of every notebook.
2. Bring back a citation, not a confident paraphrase.
3. Treat the answer as a proposal for a human, never as something to commit silently.
4. Describe a notebook from what it actually contains, not from what its name suggests.

## Analysis checklist

Before uploading **any** document, one question decides it:

> *Could this document be published on a public web page without harming a client or this business?*

If the answer is no, it does not go in — and this is not a matter of taste. The skill's own README
lists it as a requirement: **the notebook must be shared "Anyone with link"** for the automation to
reach it. A link-shared notebook is a public URL. Authenticating with your own Google account does
not make its contents private.

Out, without exception: client data of any kind, CURP/RFC/NSS, `tramites.db`, anything under
`data/`, `.env`, anything from the MSSQL mirror, screenshots of the panel, and catalog prices —
`app/rag.py` already keeps `price_info` out of model context, and this is the same rule one layer
further out.

In, comfortably: vendor documentation that is already public, and official government requirements
for a trámite — the exact material a client is entitled to know.

## Technical principles

- **Invocation goes through the installed skill's own wrapper** (`run.py`, under its `scripts/`
  directory — not this repo's). Calling one of its scripts directly fails: the wrapper creates and
  activates the isolated `.venv` first.
- **It is installed globally, not vendored here.** It carries its own `.venv/` and a `data/`
  directory holding live Google session cookies; neither belongs in this repo's history. That is why
  `tests/test_harness.py` does not see it, and why installing it is an operator step, like the
  permission policy.
- **Every question is stateless.** There is no follow-up context between queries, so each question
  must carry its own premises. The skill's own loop then asks whether anything is missing and fires
  additional queries — budget several per research task against a free-tier daily cap.
- **A cited answer is evidence, not authority.** It is grounded in the documents that were uploaded,
  which is a snapshot someone chose at some point. A requirement that changed last month is not in
  there unless somebody re-uploaded it.
- **Describe notebooks from their contents.** Query the notebook for an overview first, then register
  it with that description. A guessed description makes the library unsearchable later.

## Common mistakes

- Uploading a repo file "just this once, it's only the schema". The notebook is link-shared; there
  is no "just this once".
- Using the primary Google account. The author's own disclaimer says the humanization features
  "can't guarantee Google won't detect or flag automated usage" — use a dedicated account.
- Asking it about this codebase and believing the answer. It will answer anyway, from documents that
  have nothing to do with here.
- Copying anything out of the installed skill's `data/` directory. `browser_state/` is a live
  credential store.
- Taking a researched requirement straight into the catalog. **Nothing auto-learns** — a catalog
  change goes through a human and leaves a `CatalogAudit` row, the same as a mined synonym does.
- Treating a slow answer as a failure and re-asking. Each retry spends quota.

## Quality bar

- Every fact brought back into a PR, spec or catalog entry names the source document it came from.
- Anything that reaches `requisitos_texto` or the informative corpus passed through a human
  approving it, not through an automated copy.
- `git status --short` shows nothing from the installed skill — it lives outside this repo.

## Security

- **The notebook is published.** Link-shared means anyone with the URL reads everything in it. Treat
  every upload as publication, and apply the same admission test the informative corpus already uses
  in `app/agente_studio.py`: only what any client could already know.
- Client identifiers are the bright line. CURP, RFC, NSS and phone numbers are PII; the product's
  whole credibility rests on them never leaving.
- Browser automation of a Google product carries an account-flagging risk the author documents
  openly. A dedicated account limits the blast radius to that account.
- The stored session cookies are a credential. Never commit, copy or paste the contents of the
  skill's `data/` directory.

## Performance

- Each question costs seconds of browser time plus one query from a daily allowance. Group related
  questions into one research pass instead of drip-feeding them across a session.
- It is still cheaper than what it replaces: pasting a large PDF into context every session, paying
  for those tokens every time, and getting an invented API back anyway.

## Definition of done

- [ ] No file, excerpt or screenshot from this repo was uploaded to any notebook.
- [ ] The answer carries citations, and the source document is named where the fact lands.
- [ ] Anything reaching the catalog or the corpus went through human approval.
- [ ] The notebook was registered with a description derived from its actual contents.
- [ ] Nothing from the installed skill's `data/` directory appears in `git status`.
