---
name: harness-engineer
description: Usar al tocar el harness de desarrollo de este repo — la capa de proceso de rsc-harness (.rsc.json, hooks, sync/repair/add, doctor), la tabla de ruteo de AGENTS.md, las skills propias de .claude/skills/, la política de permisos de .claude/settings.json, el contrato de scripts/ship.sh, o cuando tests/test_harness.py se pone rojo. No es para escribir el contenido de dominio de una skill: eso es del senior del dominio.
---

# Harness engineer — the scaffolding this repo wraps around the model

## Purpose

Own the development harness, which since 2026-09-21 has **two layers with two owners**:

- **Process — rsc-harness** (`@ericrisco/rsc`, pinned in `.rsc.json`, Claude Code only). The lane
  decisor (Answer / FTD / SDD), the SDD chain, `verify`, `review`, `ship`, session memory, the
  subagents and the hooks. A third-party catalog: this repo pins it, never edits it.
- **Domain — this repo.** The routing table in `AGENTS.md`, the fourteen own skills under
  `.claude/skills/`, the `deny` policy in `.claude/settings.json`, the Gemini guardrails, and
  `tests/test_harness.py` — the test that keeps all of it true.

The harness is what turns a general model into someone who knows this repo. When it rots, nothing
crashes: the agent just quietly reads a file that no longer exists and speaks with authority anyway.
That is the failure mode this role exists to make loud.

## When to use

- Adding, renaming, splitting or deleting an **own** skill under `.claude/skills/`.
- Editing the role-routing table in `AGENTS.md`, or its list of where rsc yields to this repo.
- Anything rsc: bumping `catalogVersion`, preparing a `sync` / `repair` / `add` / `onboard` for the
  operator, reading `rsc doctor`, a hook that misbehaves, a managed skill that went missing.
- Changing `.claude/settings.json` — the `allow`/`deny` policy and the rsc hooks wired next to it.
- The harness seen from the Google agents: the `GEMINI.md` cover, the skill symlinks under
  `.agents/skills/` (with their `.gitignore` lines), and everything under `.gemini/`.
- `tests/test_harness.py` is red and you need to know whether the fix is the citation or the code.
- Preparing a ship command for the operator, or changing what `scripts/ship.sh` is asked to do.
- Someone asks "why didn't the agent load the right skill?" or "why did it take that lane?".

## When NOT to use

- Writing the **domain content** of a skill (what a DBA must check, how the NLU cascade works) —
  that belongs to the senior of that domain. This role owns the wiring, not the expertise.
- Editing a **managed** skill. `.claude/skills/ftd` and its thirty-two siblings are symlinks into
  `.rsc/`; the next `rsc sync` overwrites any edit. A disagreement with one goes in `AGENTS.md`.
- Amending the constitution → the `constitution` skill, and the operator ratifies.
- Anything in `app/`. The bot's own agent harness (`app/agent_tools.py`, `app/nlu.py`,
  `app/rag.py`) is a different harness with a different owner → `ai-ml-senior`.
- CI, Docker, the VPS cron or the pull-based deploy → `devops-senior`.
- Webhook signatures, PII, content guardrails → `security-senior`.

## Responsibilities

1. Keep the routing table and the **own** skills on disk in exact correspondence, both directions.
2. Keep the own/managed border where a clone can see it: `.rsc.json`, never the shape of a symlink.
3. Keep every path the harness cites resolvable from a clean clone — not just on this machine.
4. Keep the operator's guardrails in `deny`, and keep them honest about what they actually bind.
5. Keep `AGENTS.md` short, and its list of rsc disagreements current. Depth goes in a skill.
6. Make harness rot fail a test rather than mislead a reader.

## Analysis checklist

**Is the skill own or managed?** Ask `.rsc.json`: an id under `skills` is managed. Managed means a
symlink into `.rsc/skills/`, git-ignored one by one, absent from a clean clone and therefore from
CI. Nothing versioned may cite a file inside one — name the skill in backticks, never its path.

Adding an **own** skill is **five places**, and none of the last three is optional:

1. `.claude/skills/<name>/SKILL.md`, with frontmatter `name:` equal to the directory name — and a
   name no catalog skill uses (`rsc list`; the test rejects a collision with `.rsc.json`).
2. A row in the routing table of `AGENTS.md` — format `| what the task touches | \`skill-name\` |`.
3. `ln -s ../../.claude/skills/<name> .agents/skills/<name>` — **relative**, so it survives a clone.
   That directory, not `.gemini/skills/`, is what Antigravity and Gemini CLI both scan; a copy
   instead of a link is a second source of truth and the test rejects both.
4. `!.agents/skills/<name>` in `.gitignore`, or the link never enters the repo. The rest of
   `.agents/` stays ignored on purpose: it is the operator's.
5. `.venv/bin/pytest tests/test_harness.py -q` green **in the same commit**.

Adding a **catalog** skill is not an edit, it is a command for the operator: `rsc add` rewrites
`.rsc.json`, `.gitignore` and `.claude/settings.json`. Hand it over pinned:
`npx @ericrisco/rsc@<catalogVersion> add <id>`. Same for `sync`, `repair` and `onboard`. Before
recommending one, ask the question `docs/HARNESS.md` asks of any outside skill: does it know
anything about *this* system, or does it restate what the model already knows about FastAPI?

**Anything that writes a `.md`, `.py` or a manifest between an `onboard` preview and its
acceptance changes the plan id.** rsc answers `RSC_PLAN_CHANGED` and writes nothing — safe, but
the preview has to be regenerated. Preview → operator accepts → then edit.

Changing a guardrail is **three places in one commit**: the `deny` of `.claude/settings.json` (which
only the operator can paste), `PREFIJOS_SHELL` / `LECTURA_PROHIBIDA` / `ESCRITURA_PROHIBIDA` in
`.gemini/hooks/guardrails.py`, and `.gemini/policies/guardrails.toml`. The test asserts parity
between the three.

Before citing any repo path inside a SKILL.md, ask whether a fresh clone has it. `scripts/ship.sh`
does not: it is the operator's local tool. Neither does anything under `.rsc/`, `02-DOCS/` or
`01-TOOLS/`. A citation of a local-only file is green here and **red in CI**, which gates the bot's
deploy. That is why `tests/test_harness.py` carries `_LOCALES` — a short, named, deliberate
exclusion list, not a place to hide broken citations.

## Technical principles

- **The table is the contract; the test is the enforcement.** Same move
  `tests/test_mirror_consistency.py` makes for the MSSQL mirror's five-place rule: static analysis
  that turns a silent divergence into a loud one.
- **A check that counts managed skills is green in CI and red on the operator's machine.** That is
  what happened the day rsc was installed: five tests went red locally, and CI —which never sees
  the symlinks— would have stayed green. Every assert speaks of own skills for that reason.
- **rsc is pinned, not trusted by name.** `catalogVersion` is exact; the adoption compared the npm
  tarball against the audited source with `diff -rq` before the operator ran it. A bump repeats
  that: `npm pack @ericrisco/rsc@<new>`, read the diff of `scripts/` and `targets/`, then hand over
  the command. `.claude/rsc-bootstrap.mjs` runs on every session and every shell call — review a
  change to it the way you would review a CI workflow.
- **Where rsc and this repo disagree, the repo wins, and the disagreement is written down.** `ship`
  pushes and opens PRs; here the operator does. `ship` says "Eric"; here it is the operator. rsc
  commits `02-DOCS/`; here it is local. The list lives in `AGENTS.md`, because a managed skill
  cannot be edited and an unwritten exception is one the next session will not know.
- **The agent cannot write its own permission policy.** Editing `.claude/settings.json` is refused
  by the platform. Correct and load-bearing: a policy the agent can rewrite is not a policy. The
  rsc installer merges its hooks into that same file without touching `permissions` — which is
  exactly why every rsc command that rewrites it belongs to the operator.
- **The hooks go through the committed bootstrap.** Their scripts live in `.rsc/`, which a clone
  does not have; wired directly, every session in a clone would open with a module-loader stack
  trace. `.claude/rsc-bootstrap.mjs` delegates when `.rsc/` is mounted and says so once when not.
  `.rsc.json`, `.claude/settings.json` and the bootstrap are versioned together: all three or none.
- **Under the Google agents there is no rsc and no platform lock, so the test is the lock.** The
  operator installed rsc for Claude only. Gemini CLI gets the `BeforeTool` hook; Antigravity
  enforces nothing from the repo (its `permissionGrants` live in the operator's home), so there the
  written rules are the only guardrail. `AGENTS.md` states that asymmetry plainly.
- **Verify against the tool's binary, not its docs.** `tools.exclude` reads like the way to deny
  `run_shell_command(git push)` and in Gemini CLI 0.58.0 it never matches. rsc ships an
  `antigravity` target that writes `.antigravity/`; `strings ~/.gemini/bin/agy` has zero references
  to that path and seven to `.agents/skills`. `docs/HARNESS.md` records what was measured.
- **`deny` binds the agent's own Bash tool, not the operator's terminal.** `scripts/ship.sh` runs
  `git push -u origin` and `gh pr create`; the operator runs it in their own shell.
- **Line anchors are validated in range, never semantically.** A file that got deleted must block
  the deploy; a `:132` that drifted to `:140` must not.
- **A skill earns its length with anchors, named failure modes and runnable commands.** If a
  SKILL.md could be dropped into another repo unchanged, it is documentation about a topic, not a
  skill about this system. That is the whole reason the fourteen survived the move to rsc.

## Common mistakes

- Editing a managed skill to "fix" it for this repo. The edit lives until the next `rsc sync`.
- Running `rsc add`, `sync`, `repair` or `onboard` from the agent's Bash. They rewrite
  `.claude/settings.json`; prepare the pinned command and hand it over.
- Citing a managed skill's path, or anything under `.rsc/`, `02-DOCS/` or `01-TOOLS/`, from a
  versioned file. Green here, red in CI.
- Re-enabling `memory.compactionHint` in `.rsc.json`. In rsc 2.0.5 the hint is not deduplicated:
  past `editThreshold` (20 edits in a session) it rides on **every** hook event, and on `Stop`
  Claude Code re-invokes the agent with it — a loop of turns answering a hook, paid in tokens. It
  is off on purpose; session memory itself stays on, and `rsc sync` preserves the key.
- Handing over `npx @ericrisco/rsc@latest …`. Every machine would get the catalog of the day it ran.
- Adding an own skill and leaving the routing row for "a follow-up commit". The real cost is a skill
  nobody invokes.
- Including `02-DOCS/`, `01-TOOLS/` or `docs/` in a `git add`. They are local by the operator's rule.
- Putting a secret, a token or a server URL in a skill. Those live in `.env` and nowhere else.
- Making the anchor assert semantic "while we're at it". It converts skill-file drift into a
  production deploy freeze for the bot.
- Reviving `.claude/agents-archivados/`. Those are design roles from another project shape; the
  agents this repo uses now are the five rsc installs under `.claude/agents/`.
- Editing `.agents/`, `marketing/` or `HTTP` at the repo root. They are untracked and belong to the
  operator — `deny` covers them, and a commit must never include them.
- Restating in `AGENTS.md` what the rsc hooks already inject. It is paid twice, every session.

## Quality bar

- `.venv/bin/pytest tests/test_harness.py -q` green, and green for the right reason: the permission
  assert **skips** while `.claude/settings.json` has no `permissions` block, and turns strict the
  moment the operator pastes it.
- The negative check is part of the bar, not a nicety: a harness test that only ever passes proves
  nothing. Add a skill directory with no routing row, un-ignore a managed skill, point a hook past
  the bootstrap — each must turn the suite red.
- `rsc doctor` clean: no missing skills, agents or commands, no collisions, the fourteen listed
  under `ownSkills`. It is read-only, so the agent may run it.
- `.venv/bin/pytest -q` full, compared against the recorded baseline — no previously passing test
  changes state.

## Security

- The `deny` list carries the operator's standing rules: `git push`, `scripts/ship.sh`, `ssh`,
  `scp`, `rm -rf`, plus writes to `data/**`, `tramites.db` and `.env`. `tests/test_harness.py`
  anchors the first three, the same way `tests/test_agent_tools.py` anchors
  `PERMISOS["cobrar"] == "deny"` for the product.
- rsc's own `danger-guard` is **off** for this operator: it only guards non-technical profiles, and
  the accepted profile is `technical`. The guardrails of this repo are the `deny`, not that hook.
- The rsc hooks are code that runs on every session. A pull request that edits
  `.claude/rsc-bootstrap.mjs` or the `hooks` block runs on the machine of whoever checks it out.
- rsc's session memory stays local (`.rsc/`, never the network) and stores no prompts, tool output
  or file contents. `rsc memory off` disables it; that, too, is the operator's command.
- `.env` is never read whole and never printed. Presence of a variable is checked with a count.
- Production data (`data/`, `tramites.db`) and the operator's untracked directories are
  write-denied outright.

## Performance

- Token cost is the harness's main running cost, and with rsc it is measured, not guessed:
  `rsc doctor` reports ~7 KB per session start, ~1 KB per user turn and ~12 KB of skill
  descriptions. `AGENTS.md` is loaded always; skills only when invoked.
- The heaviest managed bodies (`specify`, `implement`, `harness`, `verify`, `review`) run 20–30 KB
  each. They cost nothing until invoked — which is the argument for staying in FTD unless the
  operator asks for the chain.
- `tests/test_harness.py` runs in well under two seconds because it is pure static analysis — no
  app imports, no DB, no fixtures. Keep it that way: a slow harness test gets skipped.
- Don't paste framework documentation into a skill. The model already knows FastAPI; what it does
  not know is that this repo's catalog lives in SQLite and the JSON is only a seed.

## Definition of done

- [ ] Every own skill has a routing row in `AGENTS.md`, and every row points at an own skill —
      verified by the test, in the same commit.
- [ ] Every own skill also has its relative symlink in `.agents/skills/` **and** its `!` line in
      `.gitignore`; any guardrail change landed in all three copies (Claude's `deny`, the hook,
      the TOML).
- [ ] No versioned file cites a path inside a managed skill, `.rsc/`, `02-DOCS/` or `01-TOOLS/`.
- [ ] Every repo path cited in a new or edited skill resolves from a clean clone, or is a named,
      justified entry in `_LOCALES` / `_MARCADORES`.
- [ ] `.venv/bin/pytest tests/test_harness.py -q` green, plus one negative check proving it bites.
- [ ] `rsc doctor` clean, and `.venv/bin/pytest -q` full with no previously passing test changing
      state.
- [ ] Anything that rewrites `.claude/settings.json` —a policy change or an rsc command— handed to
      the operator as a copy-paste block pinned to `catalogVersion`, never applied by the agent.
- [ ] `git status --short` free of `.agents/`, `HTTP`, `marketing/`, `02-DOCS/` and `01-TOOLS/`
      before any commit.
