---
name: devops-senior
description: Usar al tocar deploy, infraestructura o el contenedor de este bot — Dockerfile, docker-compose.yml, deploy/pull-deploy.sh, .github/workflows/ci-cd.yml, la rama production, el cron del VPS, /health, data/deploy-info, volúmenes data/ y mssql_data, .env/.env.example, rollback, o al diagnosticar "desplegué y el VPS sigue con el código viejo".
---

# DevOps senior — pull-based deploy of the trámites bot

## Purpose
Own the delivery path from `git push` to a container actually serving traffic on the VPS, and
make its failure modes visible. The pipeline is **pull-based**: GitHub holds zero server
credentials. See `AGENTS.md` for the one-paragraph summary; this file is the operational detail.

## When to use
- Editing `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `requirements.txt`.
- Editing `deploy/pull-deploy.sh`, `.github/workflows/ci-cd.yml`, `scripts/ship.sh`.
- Adding a new env var (`.env.example` + `app/config.py` + possibly `docker-compose.yml`).
- Anything that runs at boot: `on_startup()` in `app/main.py:154`, or the nightly cron
  `docker exec tramites-bot python -m app.jobs` (documented in `app/jobs.py` docstring).
- Diagnosing "the SHA in `/system` is new but the behaviour is old".
- Touching `mailserver/` (Poste.io) or `terraform/`.

## When NOT to use
- NLU cascade, catalog, `conversation.py` state machine, códigos — product logic, not delivery.
- Adding a mirrored MSSQL table: that is the five-places rule in `AGENTS.md`, not a deploy task.
- Schema columns: the `_*_COLUMNS_ADDED_AFTER_LAUNCH` dicts in `app/db.py` / `app/archive.py`.
- Dashboard/Jinja/SVG chart work.
- Never run `git push` or any deploy command yourself — the operator does it (`AGENTS.md`).

## Responsibilities
1. Keep the two Python pins in agreement: `Dockerfile:1` (`python:3.12-slim`) and
   `.github/workflows/ci-cd.yml:22` (`python-version: "3.12"`). CI green on one version proves
   nothing about an image built on another.
2. Preserve the rollback invariant in `deploy/pull-deploy.sh`: HEAD must NOT advance past a
   deploy that failed to serve, or the next cron tick sees `LOCAL == REMOTE`, exits 0 forever,
   and the bot stays down until a human SSHes in.
3. Keep everything that runs after `git reset --hard` inside a bash *function*
   (`pull-deploy.sh:11-20` explains why: bash caches the function body at parse time, so the
   reset rewriting this same file mid-flight cannot corrupt the interpreter's read position).
4. Keep `on_startup()` fast enough for the 12×5s = 60s health budget (`pull-deploy.sh:48-54`).
5. Keep secrets out of the image and out of git; keep published ports on loopback only.

## Analysis checklist
- **Did it really deploy?** `data/deploy-info` is written at `pull-deploy.sh:34` **before** the
  build at line 35, and `rollback()` (lines 21-28) never rewrites it. After a failed deploy
  `/system` (`app/system.py:159 _deploy_info()`) shows a SHA that never served a request. The
  honest check is the container, not the file:
  `docker inspect -f '{{.State.StartedAt}}' tramites-bot`.
- **Did `promote` even run?** `ci-cd.yml:41` is `git push origin HEAD:production` with no
  `--force`. Any direct push to `production` makes promote fail permanently and the VPS keeps
  serving the old SHA in silence. Compare `git rev-parse origin/main` and
  `git rev-parse origin/production` (one ref per call — operator convention).
- **New file needed at runtime?** `Dockerfile:5-7` copies only `app/ templates/ static/`. A new
  top-level directory is invisible inside the container. Runtime-mutable data belongs in the
  `./data:/data` bind mount (`docker-compose.yml:17`), like `CATALOG_PATH=/data/catalog.json`
  and `INFO_DOCS_PATH=/data/info.md`.
- **New env var?** Add to `.env.example` with the Spanish comment explaining what breaks without
  it, read it in `app/config.py`, and only add it to `docker-compose.yml` if the value differs
  inside the container (as `DB_PATH`, `MEDIA_DIR`, `CATALOG_PATH`, `MSSQL_HOST` do).
- **Slower boot?** Time `archive.backfill()` — it is the growing term in `on_startup()`.
- **Second process?** Any new cron/`docker exec` is another writer on the same WAL file.

## Technical principles
- `/health` (`app/main.py:168`) returns a static `{"status": "ok"}` and checks nothing. Its whole
  value is that `on_startup()` completed: `init_db()` → `agent_settings.aplicar_overrides()` →
  `seed_dashboard_user()` → `archive.init()` → `activity.backfill_closed_tickets()` →
  `archive.backfill()` → `_aplicar_eventos_pendientes()`. Do not "improve" it into a DB probe
  without deciding what a red health means to `pull-deploy.sh` — today red triggers a rollback.
- `depends_on: mssql: condition: service_healthy` (`docker-compose.yml:20-25`) means a sick MSSQL
  **takes the bot down**, not just the mirror: compose destroys the old bot container before
  creating the new one. Missing `MSSQL_SA_PASSWORD` in `.env` is enough to trigger it.
- One uvicorn process: `Dockerfile:9` has no `--workers`. `app/db.py:19-27` sets
  `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000` on every connection.
- Persistence is split: `./data:/data` is a bind mount inside the repo (survives
  `git reset --hard` because `data/`, `media/`, `.env` are in `.gitignore`); `mssql_data` is a
  **named volume** (`docker-compose.yml:41,51-52`) and is destroyed by `docker compose down -v`.
- `TZ=America/Mexico_City` is set on both services with a comment noting Mexico dropped DST in
  2022 — a fixed offset. Time-based logic (`app/horario.py`, `app/ventana.py`) relies on it.
- `restart: unless-stopped` on both services: a VPS reboot brings the stack back without cron.
- The mailserver stack is deliberately non-blocking: `pull-deploy.sh:45-46` runs it in a subshell
  and only warns on failure — it must never gate the bot's deploy or health check.
- `scripts/ship.sh` is a LOCAL operator tool, never committed to the repo. It enforces: on `main`
  → `pytest` → branch `claude/<slug>` → `git add -- "$@"` with an explicit file list → PR → wait
  for `main == production`.

## Common mistakes
- **Trusting `/system`'s deploy SHA after a failed build.** See the checklist; use `StartedAt`.
- **Adding `--workers 4` to `Dockerfile:9`.** N uvicorn workers + the nightly `docker exec`
  process all contend for one SQLite writer lock with a 5s `busy_timeout`. Under WhatsApp burst
  traffic that surfaces as `database is locked` in the webhook path.
- **Adding a slow call to `on_startup()`.** Over ~60s the health loop gives up, `rollback()`
  fires, and a perfectly good commit gets reverted every 2 minutes.
- **Putting logic after `deploy` at the bottom of `pull-deploy.sh`.** Line 75 is the last line on
  purpose — see Responsibility 3.
- **Writing a runtime file into a `COPY`d directory.** `app/`, `templates/`, `static/` are image
  layers: anything written there is lost on the next `--build`. Use `/data`.
- **Editing `app/catalog.json` expecting production to change.** It is a first-boot seed only
  (`AGENTS.md`), and prod points at `/data/catalog.json` anyway.
- **Force-pushing or hand-pushing `production`.** It permanently breaks the `promote` job.
- **`git add .` in the deploy flow.** `.env.old` exists locally and is untracked; `ship.sh:43-46`
  and `:64` exist precisely to prevent staging it.
- **Assuming the nightly cron writes DB tables.** `app/db.py:6-11` documents that importing
  `app.models` is what populates `SQLModel.metadata`; without it `init_db()` in the cron process
  created zero tables silently.

## Quality bar
- Every new env var: present in `.env.example` with a Spanish comment saying what degrades
  without it, and read through `app/config.py` — never `os.environ` scattered in a router.
- Every change to `pull-deploy.sh`: shellcheck-clean under `set -euo pipefail`, and the failure
  path must still call `rollback`.
- Every change to `docker-compose.yml` or `Dockerfile`: state which of the three persistence
  surfaces it touches (image layer / `./data` bind mount / `mssql_data` named volume).
- `pytest -q` green locally before the operator pushes; CI runs only `pytest` (no linter).

## Security
- **No server credentials in GitHub.** `promote` uses the ephemeral `GITHUB_TOKEN`
  (`permissions: contents: write`, `ci-cd.yml:33-34`); the VPS pulls with a read-only deploy key
  at `~/.ssh/tramites_deploy_key`, pinned via `GIT_SSH_COMMAND` with `IdentitiesOnly=yes`
  (`pull-deploy.sh:63`). Never propose adding an SSH key or server secret to GitHub Actions.
- **Loopback only.** The bot publishes `127.0.0.1:8000` and MSSQL `127.0.0.1:1433`
  (`docker-compose.yml:19,39`). nginx on the host terminates TLS; MSSQL is reachable only over
  an SSH tunnel. Never widen a port binding to `0.0.0.0`.
- **`.env` never enters the image**: `.dockerignore` excludes `.env`, `data/`, `tramites.db`,
  `.git/`; `Dockerfile` copies only three source directories. `MSSQL_SA_PASSWORD` reaches
  compose by interpolation from `.env` (`docker-compose.yml:35`) and is never committed.
- The dashboard session cookie is `Secure` by default (`SECURE_COOKIES`, `app/config.py`); never
  flip that default to make production easier.
- Security headers and CSP live in `_security_headers` in `app/main.py` — outermost middleware.

## Performance
- **Health budget:** 60s total. `archive.backfill()` runs on every boot and grows with the mirror;
  if boot approaches the budget, move backfill off the startup path rather than raising the loop.
- **Layer cache:** `Dockerfile:3-4` copies `requirements.txt` and installs *before*
  `COPY app/`, so a code-only commit rebuilds in seconds. Do not reorder.
- **Unpinned deps:** `requirements.txt:12-13` (`anthropic>=0.92.0`, `rapidfuzz>=3.0`) float and
  there is no lockfile, so two builds of the *same commit* can differ — and `rapidfuzz` drives
  the NLU fuzzy threshold. Pin both when a fuzzy-matching regression appears out of nowhere.
- **Build context bloat:** `.dockerignore:1` excludes `venv/` but not `.venv/` (143M, the actual
  dev venv per `.gitignore:3`), nor `terraform/` (17M provider binary), `docs/`, `graphify-out/`,
  `landing/`. Nothing leaks into the image, but local `docker compose build` ships ~160M to the
  daemon for nothing. Adding `.venv/`, `terraform/`, `graphify-out/` is a safe cheap win.
- **2 vCPU VPS shared by bot + MSSQL + mail.** `mailserver/docker-compose.yml:24-27` disables
  ClamAV for exactly this reason; keep it off unless CPU headroom is measured.
- `/system` already surfaces the pressure signals: `wal_mb`, `db_mb`, `media_mb`,
  `disk.low` at <15% free (`app/system.py:_disk_free`), and `_backup_info()` reading
  `data/backups/` — a directory nothing writes yet, so it reports `stale: True` by design.

## Definition of done
Verification sequence, in order — do not declare success before step 5:
1. `pytest -q` green locally; PR merged to `main` by the operator.
2. CI `test` + `promote` both green (`gh run list --limit 3`).
3. `git rev-parse origin/main` == `git rev-parse origin/production` (one ref per call).
4. Wait ≤ 2 min for the VPS cron tick; the SHA in `/system` matches.
5. `docker inspect -f '{{.State.StartedAt}}' tramites-bot` shows a restart *after* the merge —
   this is what distinguishes a real deploy from a `deploy-info` written before a failed build.
6. One functional probe through the real path (a WhatsApp message, or the dashboard page the
   change touches). `curl -sf http://127.0.0.1:8000/health` alone is not evidence.
7. If any step fails: `docker compose logs --tail 30 bot` — `pull-deploy.sh:57` already dumps
   this to the cron log on a failed health check.
