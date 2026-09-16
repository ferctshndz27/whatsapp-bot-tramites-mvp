@AGENTS.md

<!-- Gemini CLI reads this file the way Claude Code reads CLAUDE.md: both are a
one-line import of AGENTS.md, so the rules have exactly one source. Antigravity
(`agy`) does not need this file — it reads AGENTS.md natively. Only what differs
under Gemini CLI goes below; verified against 0.58.0, and kept true by
tests/test_harness.py. -->

## Gemini CLI specifics

- **Trust the folder first.** Gemini ignores project settings, hooks, skills and
  this file in an untrusted workspace, and says so in one easy-to-miss line. The
  `/permissions` command only exists when folder trust is enabled.
- **Skills** are the same fourteen, linked from `.agents/skills/` — the
  customization root Antigravity and Gemini CLI both scan, so one set of links
  serves both. The routing table in AGENTS.md applies unchanged; Gemini picks a
  skill by its `description:`, and `/skills list` shows them. Skills of the
  operator's own live in that directory too and are none of this repo's business.
- **Guardrails** are the deny list of `.claude/settings.json`, enforced here by
  the `BeforeTool` hook in `.gemini/settings.json` (`.gemini/hooks/guardrails.py`):
  no `git push`, `scripts/ship.sh`, `ssh`, `scp`, `rm -rf`; no reading `.env`;
  no writing `data/`, `tramites.db`, `.env`, the operator's untracked
  directories, or the harness's own policy files. The operator runs pushes and
  deploys in their own terminal — prepare the command, don't run it.
  `.gemini/policies/guardrails.toml` carries the same list for the Policy
  Engine; how to enable it is in `docs/HARNESS.md`.
- **Which instructions win**: your global `~/.gemini/GEMINI.md` (nodeterm fills
  it with canvas instructions) is "the user's own global rules" in the order
  AGENTS.md gives. The parent `Projects/CLAUDE.md` never loads here.
