---
name: harness-engineer
description: Usar al tocar el harness de desarrollo de este repo — la tabla de ruteo de AGENTS.md, las skills de .claude/skills/, la política de permisos de .claude/settings.json, el contrato de scripts/ship.sh, o cuando tests/test_harness.py se pone rojo. No es para escribir el contenido de dominio de una skill: eso es del senior del dominio.
---

# Harness engineer — the scaffolding this repo wraps around the model

## Purpose

Own the development harness: the routing table in `AGENTS.md`, the skills under `.claude/skills/`,
the permission policy in `.claude/settings.json`, and `tests/test_harness.py` — the test that keeps
all of it true. The harness is what turns a general model into someone who knows this repo. When it
rots, nothing crashes: the agent just quietly reads a file that no longer exists and speaks with
authority anyway. That is the failure mode this role exists to make loud.

## When to use

- Adding, renaming, splitting or deleting a skill under `.claude/skills/`.
- Editing the role-routing table in `AGENTS.md`, or deciding which skill owns a new domain.
- Changing `.claude/settings.json` — the `allow`/`deny` policy the agent works under.
- The same harness seen from the Google agents: the `GEMINI.md` cover, the skill symlinks under
  `.agents/skills/` (with their `.gitignore` lines), and everything under `.gemini/` — the
  `BeforeTool` guardrail hook and its Policy Engine twin.
- `tests/test_harness.py` is red and you need to know whether the fix is the citation or the code.
- Preparing a ship command for the operator, or changing what `scripts/ship.sh` is asked to do.
- Someone asks "why didn't the agent load the right skill?".

## When NOT to use

- Writing the **domain content** of a skill (what a DBA must check, how the NLU cascade works) —
  that belongs to the senior of that domain. This role owns the wiring, not the expertise.
- Anything in `app/`. The bot's own agent harness (`app/agent_tools.py`, `app/nlu.py`,
  `app/rag.py`) is a different harness with a different owner → `ai-ml-senior`.
- CI, Docker, the VPS cron or the pull-based deploy → `devops-senior`.
- Webhook signatures, PII, content guardrails → `security-senior`.

## Responsibilities

1. Keep the routing table and the skills on disk in exact correspondence, in **both** directions.
2. Keep every path a skill cites resolvable from a clean clone — not just on this machine.
3. Keep the operator's guardrails in `deny`, and keep them honest about what they actually bind.
4. Keep `AGENTS.md` short. Depth goes in a skill that loads on demand; breadth costs every session.
5. Make harness rot fail a test rather than mislead a reader.

## Analysis checklist

Adding a skill is **five places**, and none of the last three is optional:

1. `.claude/skills/<name>/SKILL.md`, with frontmatter `name:` equal to the directory name.
2. A row in the routing table of `AGENTS.md` — format `| what the task touches | \`skill-name\` |`.
3. `ln -s ../../.claude/skills/<name> .agents/skills/<name>` — **relative**, so it survives a clone.
   That directory, not `.gemini/skills/`, is what Antigravity and Gemini CLI both scan; a copy
   instead of a link is a second source of truth and the test rejects both.
4. `!.agents/skills/<name>` in `.gitignore`, or the link never enters the repo. The rest of
   `.agents/` stays ignored on purpose: it is the operator's, and re-including the whole directory
   would let a `git add .` swallow their own skills.
5. `.venv/bin/pytest tests/test_harness.py -q` green **in the same commit**. The table↔disk assert
   is bidirectional, so a skill added without its row fails, and so does a row without its skill.

Changing a guardrail is **three places in one commit**: the `deny` of `.claude/settings.json` (which
only the operator can paste), `PREFIJOS_SHELL` / `LECTURA_PROHIBIDA` / `ESCRITURA_PROHIBIDA` in
`.gemini/hooks/guardrails.py`, and `.gemini/policies/guardrails.toml`. The test asserts parity
between the three; a rule that lives in only one is a guardrail that depends on which agent is
sitting down.

Before citing any repo path inside a SKILL.md, ask whether a fresh clone has it. `scripts/ship.sh`
does not: it is the operator's local tool, excluded in `.git/info/exclude` and untracked. A citation
of a local-only file is green on your machine and **red in CI**, which gates the bot's deploy. That
is why `tests/test_harness.py` carries `_LOCALES` — a short, named, deliberate exclusion list, not a
place to hide broken citations.

## Technical principles

- **The table is the contract; the test is the enforcement.** Before `tests/test_harness.py` the
  routing table was prose — a skill could be renamed and nothing would notice. This is the same move
  `tests/test_mirror_consistency.py` makes for the MSSQL mirror's five-place rule: static analysis
  that turns a silent divergence into a loud one.
- **The agent cannot write its own permission policy.** Attempting to edit `.claude/settings.json`
  is refused by the platform's classifier, by Bash and by the Write tool alike. This is correct and
  load-bearing: a policy the agent can rewrite is not a policy. Changes to it are prepared as a
  copy-paste block and applied by the operator, exactly like a deploy command.
- **Under the Google agents that lock does not exist, so the test is the lock.** Neither has a
  classifier stopping the agent from editing `.gemini/settings.json` or its own hook. What holds the
  line is `tests/test_harness.py`: it demands parity with the `deny` Claude cannot touch, and runs
  the hook as a process against real allowed and forbidden calls. Weakening that side means going
  red in CI, which gates the bot's deploy.
- **Antigravity enforces nothing from the repo, and the docs must say so.** Its permissions are
  `permissionGrants` in the operator's home (`~/.gemini/config/projects/<uuid>.json`), unversionable
  here. When the agent is `agy`, the written rules are the only guardrail. Do not paper over that
  asymmetry — `AGENTS.md` states it plainly.
- **Verify against the tool's binary, not its docs — and against the tool the operator actually
  runs.** Two findings, both only visible in the code: `tools.exclude` reads like the obvious way to
  deny `run_shell_command(git push)` and the docs suggest it, but in 0.58.0 that entry becomes a rule
  whose `toolName` is compared by exact equality against `run_shell_command` — it never matches; and
  Antigravity does not scan `.gemini/skills/` at all, so a whole mirror was built for a CLI that was
  not installed. The probe: `npm i @google/gemini-cli` in a scratch dir and read the bundle;
  `strings ~/.gemini/bin/agy` for Antigravity. `docs/HARNESS.md` records what was measured.
- **`deny` binds the agent's own Bash tool, not the operator's terminal.** `scripts/ship.sh` runs
  `git push -u origin` and `gh pr create`; the operator runs it in their own shell, outside Claude
  Code, where these rules do not apply. What the rule blocks is the agent pushing on its own — the
  standing operator rule, finally machine-enforced instead of remembered.
- **Line anchors are validated in range, never semantically.** `tests/test_harness.py` lives in
  `tests/`, so CI runs it and a red suite blocks the `promote` job and therefore the deploy. A file
  that got deleted must block; a `:132` that drifted to `:140` must not. The skills themselves warn
  that anchors drift.
- **Skills load on demand and cost nothing until invoked.** That is the whole reason `AGENTS.md`
  stays at ~200 lines while the ten domain skills carry ~1,750. Depth is free; breadth is not.
- **A skill earns its length with anchors, named failure modes and runnable commands.** If a
  SKILL.md could be dropped into another repo unchanged, it is documentation about a topic, not a
  skill about this system — delete it and write the specific one.

## Common mistakes

- Adding a skill and leaving the routing row for "a follow-up commit". The test fails immediately,
  which is the point, but the real cost is a skill nobody invokes.
- Citing a path that only exists locally. See `_LOCALES` above; this fails only in CI, which is the
  worst place to find out.
- Putting a secret, a token or a server URL in a skill. Those live in `.env` and nowhere else.
  Checking whether one is set is `grep -c` on the count, never printing the value.
- Making the anchor assert semantic "while we're at it". It converts skill-file drift into a
  production deploy freeze for the bot.
- Reviving `.claude/agents-archivados/`. Those 25+ agents are mostly design roles from another
  project shape; a trámites bot has no use for a whimsy injector. They stay archived on purpose.
- Editing `.agents/`, `marketing/` or `HTTP` at the repo root. They are untracked and belong to the
  operator, not to this repo's history — `deny` covers them, and a commit must never include them.
- Restating global rules inside a skill. `AGENTS.md` already says them once; a copy is a second
  source of truth that will drift.

## Quality bar

- `.venv/bin/pytest tests/test_harness.py -q` green, and green for the right reason: the permission
  assert **skips** while `.claude/settings.json` has no `permissions` block, and turns strict the
  moment the operator pastes it.
- The negative check is part of the bar, not a nicety: a harness test that only ever passes proves
  nothing. Temporarily add a skill directory with no routing row and confirm the suite goes red.
- `.venv/bin/pytest -q` full, compared against the recorded baseline — no previously passing test
  changes state.

## Security

- The `deny` list carries the operator's standing rules: `git push`, `scripts/ship.sh`, `ssh`,
  `scp`, `docker`, plus writes to `data/**`, `tramites.db` and `.env`. `tests/test_harness.py`
  anchors the first three, the same way `tests/test_agent_tools.py` anchors
  `PERMISOS["cobrar"] == "deny"` for the product. A safety rule nobody can delete accidentally is a
  rule; one that can vanish unnoticed is a suggestion.
- `.env` is never read whole and never printed. Presence of a variable is checked with a count.
- Production data (`data/`, `tramites.db`) and the operator's untracked directories are write-denied
  outright — the agent has no business editing either.
- The MSSQL mirror archives every table, so anything written anywhere eventually lands there. That
  raises the cost of a careless write beyond the local file.

## Performance

- Token cost is the harness's main running cost, and it is paid per session whether or not the
  context gets used. `AGENTS.md` is loaded always; skills only when invoked. Anything that is true
  for one domain belongs in that domain's skill.
- `tests/test_harness.py` runs in well under a second because it is pure static analysis — no app
  imports, no DB, no fixtures. Keep it that way: a slow harness test gets skipped, and a skipped
  test guards nothing.
- Don't paste framework documentation into a skill. The model already knows FastAPI; what it does
  not know is that this repo's catalog lives in SQLite and the JSON is only a seed.

## Definition of done

- [ ] Every skill on disk has a routing row in `AGENTS.md`, and every row points at a skill that
      exists — verified by the test, in the same commit.
- [ ] Every skill also has its relative symlink in `.agents/skills/` **and** its `!` line in
      `.gitignore`, and any guardrail change landed in all three copies (Claude's `deny`, the hook,
      the TOML).
- [ ] Frontmatter `name:` matches the directory name for every skill.
- [ ] Every repo path cited in a new or edited skill resolves from a clean clone, or is a named,
      justified entry in `_LOCALES` / `_MARCADORES`.
- [ ] `.venv/bin/pytest tests/test_harness.py -q` green, plus one negative check proving it bites.
- [ ] `.venv/bin/pytest -q` full suite with no previously passing test changing state.
- [ ] Permission-policy changes handed to the operator as a copy-paste block, never applied by the
      agent.
- [ ] `git status --short` free of `.agents/`, `HTTP` and `marketing/` before any commit.
