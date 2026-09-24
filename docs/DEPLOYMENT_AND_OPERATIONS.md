# MEYAR — Deployment & Operations Runbook

## 1. Purpose and authority

This document defines the operational lifecycle of MEYAR from an accepted
repository revision to a running bank-controlled deployment: provisioning a
host, deploying an update, migrating a database, backing up and restoring,
moving development to a new workstation, and handing the repository over to
a new owner.

Issue #60 adds Pillow to the locked backend runtime for local candidate
photo extraction. The later issue #35 offline Mac package must include a
compatible Apple Silicon Pillow wheel and verify it on the target host;
this branch does not build that package. PDF raster decoding has a worker
timeout and byte/pixel caps. Its Linux address-space limit is not a
verified macOS production memory guarantee, so the target-Mac runtime
rehearsal must validate resource behavior before deployment acceptance.

Authority model:

- The canonical application source is the approved Git repository
  (`https://github.com/a-r3/meyar.git` at present — see §14 on handover).
  Accepted `main` is the deployment source of truth. A feature/task branch,
  an open PR, or an uncommitted working tree is never a deployment source.
- Developer workstations (current Linux workstation, a future macOS
  workstation, etc.) are development environments, not
  production/source-of-truth environments. Nothing on a workstation is
  authoritative except what has been pushed, reviewed, and merged.
- Environment-specific secrets and configuration are never committed to the
  repository. `backend/.env.example` is a template; the real `backend/.env`
  is host-local and git-ignored.
- Deployment acceptance is a separate concern from software development.
  Merging a PR is not deployment; deploying a host is not a code review.
  Each has its own checklist below.

This document is operational glue. It intentionally does not duplicate the
canonical references it points to — read this alongside, not instead of:

- [`README.md`](../README.md) — architecture, local dev setup, REST API
  reference, quality gate.
- [`docs/SECURITY_PRIVACY.md`](SECURITY_PRIVACY.md) — threat model, tenant
  isolation, PII handling, local-only AI boundary.
- [`docs/BACKUP_RESTORE.md`](BACKUP_RESTORE.md) — backup/restore mechanism
  and the executed synthetic acceptance proof.
- [`docs/TARGET_MAC_BENCHMARK.md`](TARGET_MAC_BENCHMARK.md) — reference
  hardware, benchmark harness, model-approval gate.
- [`docs/MEYAR_OPS.md`](MEYAR_OPS.md) — the `meyar-ops` operator CLI
  (issue #35): local preflight/status/readiness checks and release-
  artifact verification. PR1 foundation only — see its own #35/#46
  boundary section; it does not yet replace any manual step in this
  runbook.
- [`docs/DECISIONS.md`](DECISIONS.md) — the full decision log, including
  D-020 (tested security/acceptance boundary) and D-066 (`meyar-ops`
  foundation).
- [`docs/STATUS.md`](STATUS.md) — current slice/milestone/matrix status.

## 2. Environment roles

### A. Developer workstation

Examples: the current Linux workstation; a future macOS development
workstation.

Responsibility: feature branches, local development, running the quality
gate, commits, PR preparation. A workstation never receives production
secrets or real candidate data (see §5, §17).

### B. Canonical Git repository / CI

Responsibility: version history, accepted `main`, the PR/review boundary,
automated quality gates (`ruff`, `mypy`, `pytest`, the Alembic migration-chain
check — see `.github/workflows/ci.yml`). No deployment host should ever treat
anything other than a merged commit on `main` as canonical.

### C. Deployment / reference host

Current MVP reference hardware (owner-confirmed 2026-08-28, see
`docs/DECISIONS.md` D-020 and `docs/TARGET_MAC_BENCHMARK.md`):

- Mac mini M4 Pro
- 12-core CPU
- 16-core GPU
- 24 GB unified memory
- 512 GB SSD

This host runs PostgreSQL (with `pgvector`), the MEYAR application runtime,
Ollama, and the approved local models, and is where target-environment
validation (the Target-Mac benchmark) is performed.

**This is validated reference hardware, not a permanent platform lock.**
See §12 and §19.

## 3. High-level delivery flow

```
developer workstation
  -> task branch (feat/*, fix/*, chore/*, docs/*, test/*)
  -> local quality gate (ruff, mypy, pytest)
  -> push
  -> pull request into main
  -> CI + owner review
  -> owner Squash and merge
  -> deployment host pulls accepted main
  -> dependency sync (uv sync --locked)
  -> database migrations (alembic upgrade head)
  -> application restart
  -> post-deployment verification (§16)
```

Never deploy arbitrary unreviewed developer working-tree state as the
canonical release. A deployment always corresponds to a specific,
identifiable commit on `main` that went through PR review and CI.

## 4. Fresh host provisioning

The `git clone`/`git pull` sequence below is the current, manual,
source-checkout-based runbook — it is not the final intended bank
production mechanism. Issue #35's target production path is an immutable,
owner-accepted release artifact (built, verified, installed, and updated
by `meyar-ops` tooling); `meyar-ops verify-release` (PR1, D-066) exists
today, but the corresponding artifact build/install/update tooling is not
yet implemented and lands in later #35 PRs. Until then, this
source-checkout runbook is the accurate description of how a host is
actually provisioned.

Prerequisites (from `README.md` §Prerequisites, unchanged here):

- Git
- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)
- Docker Engine with the Docker Compose plugin, or an equivalent native
  PostgreSQL 16 + `pgvector` installation
- Ollama, bound to `127.0.0.1` only — see §5 and §11

Sequence:

```bash
# 1. Clone and check out the accepted revision
git clone https://github.com/a-r3/meyar.git
cd meyar
git checkout main
git log -1 --oneline   # record this commit — see §7 step 8

# 2. Activate repository-local safety hooks (not transferred by git clone)
scripts/setup-git-governance.sh
git config --get core.hooksPath   # must print .githooks

# 3. Install locked dependencies
cd backend
uv sync --locked

# 4. Host-local configuration
cp .env.example .env
# edit backend/.env: set MEYAR_DATABASE_URL, MEYAR_OLLAMA_BASE_URL, and any
# other host-specific values — see §5. Never commit this file.

# 5. Database
cd ..
docker compose up -d postgres
docker compose ps
```

The checked-in `docker-compose.yml` `postgres` service uses
`network_mode: host` and port 55719 as a documented workaround for a
specific shared development machine (`docs/DECISIONS.md` D-005) — it is not
mandated production architecture. On a dedicated deployment host it is
reasonable to run the same `pgvector/pgvector:pg16` image with standard
bridge networking on the default port instead; no application code depends
on the networking mode (D-005 is explicit on this point). Which to use on
a given deployment host is an operator decision, not something this
document prescribes.

```bash
# 6. Migrations
cd backend
uv run alembic upgrade head

# 7. Confirm exactly one migration head
uv run alembic heads

# 8. Model availability — see §11 before pulling any model
ollama list

# 9. Start the application — see §6
uv run uvicorn meyar.main:app --host 127.0.0.1 --port 8000

# 10. Verify — see §16
curl http://127.0.0.1:8000/api/v1/health
curl -o /dev/null -s -w "%{http_code}\n" http://127.0.0.1:8000/docs
```

Do not skip step 2 (`setup-git-governance.sh`) on any host that will make
commits — the pre-commit/pre-push hooks are the local guard against
committing secrets, real CVs, or runtime state (see `.githooks/`).

## 5. Configuration and secrets

- `backend/.env.example` is the authoritative template. It contains only
  non-production local defaults and is committed. It is never a source of
  real secrets.
- The real `backend/.env` is host-local, git-ignored, and must never be
  committed. `.githooks/pre-commit` and `scripts/scan-tracked-tree.sh`
  both reject a staged/tracked `.env` file (except the `.example`/
  `.sample`/`.template` variants).
- Production secrets (database credentials, any future signing/session
  secret) are provisioned on the deployment host through the bank's
  approved operator/secrets process — this document does not define that
  process, only that it must exist before a real deployment.
- `MEYAR_DATABASE_URL` and `MEYAR_OLLAMA_BASE_URL` are deployment
  configuration, set per host in `backend/.env`, never hardcoded.
- **Current inference policy is loopback-only.** `meyar.llm.loopback`
  (`require_loopback_url`) rejects any non-`127.0.0.1`/`localhost`/`::1`
  base URL for both the LLM and embedding providers at construction time.
  **MEYAR and Ollama are expected on the same host for the current MVP
  security model.** A future deployment topology that places Ollama on a
  separate host from the MEYAR application is a split-inference
  architecture change and requires its own approved security/architecture
  decision before implementation — it is explicitly not implemented now
  (see `docs/TARGET_MAC_BENCHMARK.md`, `docs/DECISIONS.md` D-020 item 2).

## 6. Application start / stop / health

Start (foreground, current verified method):

```bash
cd backend
uv run uvicorn meyar.main:app --host 127.0.0.1 --port 8000
```

Drop `--reload` (a development-only convenience) for anything other than a
developer workstation.

Stop: standard process termination (`Ctrl-C` in foreground, or the normal
signal-based stop for however the process is supervised — see below).

Health / validation:

- `GET /api/v1/health` — liveness only; it does **not** probe the database
  or Ollama (see `README.md` endpoint summary). A `200` proves the process
  is up and routing requests, nothing more.
- `GET /ui/login` — confirms the UI/templating stack is serving.
- `GET /docs` — confirms the offline Swagger UI is serving (no CDN
  dependency; see `docs/SECURITY_PRIVACY.md`).
- `GET /openapi.json` — confirms the API schema is generating.

A meaningful post-start check therefore also needs at least one
DB-touching call (e.g. `GET /api/v1/usage` with a valid key, or the
smoke sequence in §16) — liveness alone does not prove database
connectivity.

Expected failure behavior: a missing/unreachable database at startup
surfaces as connection errors on the first DB-touching request, not at
process boot (SQLAlchemy's async engine connects lazily). A missing/
unreachable Ollama surfaces only when an LLM- or embedding-dependent
operation is invoked (see the Local AI dependency map in `README.md`) —
health, structured search, and scoring/ranking all continue to work
without it.

Logs: standard `uvicorn`/application stdout/stderr. Structured log calls
carry only ids/enums/durations (`docs/SECURITY_PRIVACY.md`) — never raw CV
text or PII, so application logs are safe to forward to normal log
aggregation without additional redaction.

**Production service supervision (systemd, launchd, a process manager, or
container orchestration) is not yet standardized in this repository.**
No such configuration exists in-tree. Selecting and configuring one is an
explicit deployment/operator decision for the target environment, not
something fabricated here — whatever is chosen must still result in the
same `uv run uvicorn meyar.main:app` invocation shown above, with
`backend/.env` supplying configuration.

## 7. Deploying an update

1. Confirm a recent backup exists where the update includes a schema
   migration or other consequential change (§9).
2. On the deployment host: `git fetch origin`.
3. Verify the exact commit/release being deployed —
   `git log -1 --oneline origin/main` — against the intended PR/release.
4. Fast-forward only: `git switch main && git pull --ff-only origin main`.
   Never merge or rebase on the deployment host.
5. `cd backend && uv sync --locked` — synchronize locked dependencies
   exactly; never run `uv sync` without `--locked` on a deployment host.
6. Inspect what migrations, if any, this update adds
   (`uv run alembic history` / `git log` on `backend/alembic/versions/`),
   then `uv run alembic upgrade head` (§8).
7. Restart the application process (§6).
8. Perform post-deployment verification (§16).
9. Record the deployed revision (the commit SHA from step 3) — e.g. in
   the operator's own deployment log; this repository does not currently
   maintain one.

A feature/task branch or an unmerged PR is never a deployment source —
only step 2–4's fast-forward pull of `main` is.

## 8. Database migrations

- Alembic is authoritative for schema. `backend/alembic/versions/` is the
  single source of truth for schema history.
- Exactly one migration head is expected at all times. CI enforces this
  (`.github/workflows/ci.yml`'s "Alembic migration chain" step runs
  `alembic upgrade head` against a fresh database and asserts a single
  head — see `docs/STATUS.md`, Slice 13).
- Take a database backup before any consequential production migration
  (§9) — a migration is not itself a backup mechanism.
- **Rollback is not equivalent to blindly running `alembic downgrade`.**
  Downgrade paths exist in the migration files but are not verified as
  safe/lossless for every revision in this repository's current test
  coverage; do not treat "downgrade exists" as "downgrade is production-
  safe" without deployment-specific verification. Where a migration must
  be undone in production, prefer restoring from a pre-migration backup
  (§9, §15) over an untested downgrade unless the specific downgrade path
  has been explicitly verified for that migration.

## 9. Backup / restore

Full mechanism, runbook, and the executed synthetic acceptance proof are in
[`docs/BACKUP_RESTORE.md`](BACKUP_RESTORE.md) — not duplicated here.

Operational summary:

- PostgreSQL (`pg_dump`/`pg_restore`) and document storage (`tar` over
  `MEYAR_STORAGE_ROOT`) must be backed up **together**, from the same
  point in time — one without the other leaves either orphaned files or
  dangling metadata.
- Restore into an isolated destination and validate before treating a
  restore as complete (row counts, relationships, original-CV bytes,
  a repeat score request reusing the same `Evaluation` — see
  `docs/BACKUP_RESTORE.md` §Validation).
- Retention and backup-scheduling *policy* (how often, how long kept,
  where stored) remain an explicit, undecided deployment/business
  decision (`docs/SECURITY_PRIVACY.md`) — this document does not set one.

## 10. Document storage

- All document access goes through the `DocumentStorage` abstraction
  (`meyar.storage.base`) — application code never touches the filesystem
  directly, and no route ever returns a raw storage key or filesystem path
  to a client (see `docs/SECURITY_PRIVACY.md`, and the original-CV route's
  security review in `docs/DECISIONS.md` D-020 item 3).
- The current deployment implementation is local filesystem storage under
  `MEYAR_STORAGE_ROOT` (`LocalFilesystemStorage`).
- A future shared/internal storage implementation (e.g. bank-internal
  object storage) could replace this behind the same abstraction if
  deployment architecture requires it, without changing calling code —
  but no such implementation exists today. Do not represent NAS, NFS, or
  object-storage support as available; it is not implemented.

## 11. Local AI / Ollama operations

- Ollama is the local inference runtime for both the LLM and the embedding
  provider — both accessed only through `meyar.llm.LLMProvider` /
  `meyar.embedding` abstractions, never called directly from application
  code (see `.claude/rules/architecture.md`).
- Currently configured **development** models (`backend/.env.example`):
  LLM `qwen3:0.6b`, embedding `nomic-embed-text`. **Neither is approved
  as a production model** — approval is blocked on the Target-Mac
  benchmark (§12; `docs/DECISIONS.md` D-020 item 15; `docs/STATUS.md`
  matrix row 1). Do not treat either as production-selected based on this
  document.
- Model name and revision must be recorded whenever a model is pulled for
  a deployment host (`ollama list` / `ollama show <model>`), consistent
  with the provenance-recording pattern the benchmark harness follows
  (`backend/scripts/target_mac_benchmark.py`).
- Any model change (different model, different tag/revision) requires
  re-validation on the target host before being treated as the
  deployment's active model — the same benchmark harness and, once
  approved, a recorded decision in `docs/DECISIONS.md`.
- Local inference must preserve the no-public-exfiltration boundary at all
  times: `require_loopback_url` rejects any non-loopback `base_url` at
  construction; this has a formal, executable proof
  (`backend/tests/test_no_exfiltration.py`, `docs/DECISIONS.md` D-020
  item 2). Do not point `MEYAR_OLLAMA_BASE_URL` at anything but a loopback
  address on this host.

## 12. Target-hardware validation

Full detail: [`docs/TARGET_MAC_BENCHMARK.md`](TARGET_MAC_BENCHMARK.md).

- New deployment hardware does not require rewriting MEYAR — the
  application, storage, and provider abstractions are not hardware-
  specific.
- Each materially different hardware/model profile still requires its own
  dependency, performance, and model validation before being treated as a
  production-ready configuration. "It runs" is not the same as "it is
  validated."
- The Mac mini M4 Pro configuration in §2C is the current **reference**
  hardware, confirmed by the owner for MVP acceptance — not the only
  hardware MEYAR can ever run on.
- Another Mac, a Mac Studio, a Linux/NVIDIA server, or other
  bank-controlled infrastructure may be used for a future deployment
  after the same class of validation (§7 of `docs/TARGET_MAC_BENCHMARK.md`
  — run the benchmark harness, record hardware/model/timings, make an
  explicit model-approval decision). Do not assume automatic support for
  every platform without that step.

## 13. Developer workstation migration

Moving development from one workstation to another (e.g. the current Linux
workstation to a future macOS workstation):

**Source code:**

```bash
git clone https://github.com/a-r3/meyar.git
cd meyar
git checkout main   # or the in-progress task branch, if migrating mid-task
```

Do not copy a stale local working directory and treat it as authoritative
— the canonical source is always what is in the Git repository (§1).

**Recreate on the new workstation:**

- Toolchain: Git, Python 3.12, `uv`, Docker (or native Postgres),
  Ollama — same prerequisites as §4.
- Locked dependencies: `uv sync --locked` (from `backend/`).
- Local configuration: a fresh `backend/.env` from `.env.example` — never
  copy an old host's `.env` verbatim without reviewing every value.
- SSH/Git credentials: reconfigure independently on the new workstation
  (SSH keys, `gh auth login`, commit signing if used) — these are
  workstation-local, not part of the repository.
- A local development database: fresh `docker compose up -d postgres` +
  `uv run alembic upgrade head` (§4) — do not attempt to physically copy a
  running Postgres data directory between machines as a routine step;
  treat it as a backup/restore operation if data must move (§9).
- Local Ollama models where needed for live-model development: `ollama
  pull <model>` for whatever models local development requires — see §11
  on recording what was pulled.

Host-specific untracked secrets or data (a workstation's own `.env`, any
locally exported dumps, locally pulled models) must be migrated
separately and securely if genuinely needed on the new workstation — never
by committing them to the repository.

**A developer workstation change must not affect canonical project
history** — no force-pushes, no history rewrites, no branch deletion, to
"clean up" a workstation move. The repository's history is unaffected by
where development happens.

## 14. Repository ownership / handover

Repository ownership is not an application architecture dependency —
nothing in MEYAR's code or configuration hardcodes the current GitHub
organization/owner (`a-r3`); `CLAUDE.md` and `.claude/rules/git-workflow.md`
already describe the current remote as personal/temporary, pending
migration to an official bank-owned remote.

Two supported handover approaches:

**Preferred — GitHub-native ownership/organization transfer**, where GitHub
and organizational policy permit it. This preserves PR history, issues,
milestones, and other GitHub-native metadata most completely.

**Alternative — migrate to a new organization-owned repository** while
preserving full Git history (a standard `git clone --mirror` +
`git push --mirror` to the new remote, or equivalent), followed by explicit
validation/reconstruction of GitHub-specific governance metadata (issues,
milestones, branch protection, Actions secrets, Dependabot config) as
needed — these do not travel with a plain history mirror.

This document does not name a specific future bank GitHub organization —
that is an infrastructure/ownership decision made at handover time, not a
detail to fix in advance.

After any repository handover, on every developer workstation and
deployment host:

1. Verify the canonical remote: `git remote -v` should point at the newly
   designated repository.
2. Update `origin` where it doesn't: `git remote set-url origin <new-url>`.
3. Verify CI runs on the new remote (`.github/workflows/ci.yml` and any
   required-status-check configuration).
4. Verify repository permissions/access for the actual operators who need
   them.
5. Verify branch/merge governance (protected `main`, required reviews,
   Squash-and-merge-only policy) is reconfigured on the new remote — it
   does not automatically carry over from a plain mirror.
6. Verify secrets were **not** transferred incorrectly — Actions
   secrets/environment variables must be re-provisioned deliberately on
   the new remote, never assumed to have migrated.
7. Point every deployment host's `git fetch`/`git pull` (§7) at the newly
   designated canonical repository — a deployment host pulling from a
   stale remote after handover is a real operational risk.

## 15. Rollback / failed deployment

Rollback is not one operation — distinguish:

**A. Application-code rollback** — redeploy a previously known-good
commit on `main` (§7, steps 2–7, targeting the prior commit instead of the
latest). Safe when no schema-incompatible migration sits between the two
commits.

**B. Database-schema rollback** — only when the failed deployment included
a migration. Per §8, do not assume `alembic downgrade` is safe for a given
revision without having verified that specific downgrade path. Where it
has not been verified, restoring from the pre-migration backup (C) is the
conservative option.

**C. Data restore** — required when a failed deployment corrupted data or
when B is not confidently reversible; follow `docs/BACKUP_RESTORE.md`,
restoring to a point before the failed deployment.

In all cases:

1. Identify the last known-good, accepted revision (a specific commit on
   `main`, per §7 step 9's recorded deployment log).
2. Bring the database to a schema state compatible with that revision (B
   or C above, as required — not always necessary for a pure code
   rollback).
3. Redeploy that revision (§7).
4. Run post-deployment verification (§16) before considering the rollback
   complete — a rollback that hasn't been verified is not yet a rollback,
   it's a second unverified deployment.

Never use `git reset --hard` or an unverified `alembic downgrade` as a
default/universal production rollback mechanism.

## 16. Post-deployment verification

`uv run meyar-ops status` and `uv run meyar-ops readiness` (see
`docs/MEYAR_OPS.md`) give a machine-readable, read-only snapshot covering
several of the items below (database/Alembic-head/storage/Ollama/model
reachability) in one call — useful as a quick local aid, but they do not
yet replace this manual checklist; run both.

Compact checklist, grounded in MEYAR's actual surfaces:

- [ ] Application process starts and stays up.
- [ ] Database reachable (a DB-touching call succeeds — see §6; `/health`
      alone is not sufficient).
- [ ] `uv run alembic heads` shows exactly one head, matching the deployed
      revision's expected head.
- [ ] `GET /api/v1/health` returns `200`.
- [ ] `GET /docs` returns `200` and renders (offline Swagger).
- [ ] `GET /ui/login` returns `200`.
- [ ] Candidate library / UI browsing works for an authorized test session.
- [ ] A synthetic/approved smoke test passes where one is authorized to
      run against this environment (e.g. the fresh-deployment smoke
      pattern in `backend/scripts/fresh_deployment_smoke.py`, run against
      a disposable database/storage root — never against production data).
- [ ] If this deployment depends on a specific local model, confirm it is
      pulled and available (`ollama list`) — see §11.
- [ ] No unexpected public-network dependency was introduced (the
      no-exfiltration guard and its test remain the standing acceptance
      evidence — `backend/tests/test_no_exfiltration.py`).
- [ ] The deployed commit SHA is recorded (§7 step 9).

Never use real candidate data for generic smoke testing — synthetic
fixtures only (`fixtures/synthetic_cvs/`, `docs/SECURITY_PRIVACY.md`).

## 17. Operational security

Full threat model: [`docs/SECURITY_PRIVACY.md`](SECURITY_PRIVACY.md).
Operational summary:

- Least privilege: API keys are scoped (`jobs:read/write`,
  `candidates:read/write`, `evaluations:read/write`); provision only the
  scopes a given integration actually needs.
- Secrets are host-local (§5) — never committed, never logged (API keys
  are logged only by their safe prefix, per `docs/SECURITY_PRIVACY.md`).
- Tenant isolation is enforced at the data-access layer on every
  tenant-owned query — this is unchanged by deployment topology.
- The candidate-content AI path is local-only by construction and
  formally verified (§11).
- Secure remote administration (§18), firewalling, and VPN configuration
  are infrastructure/operator responsibilities — this document does not
  configure or claim to have tested them.
- Disk/volume encryption is a deployment responsibility, not an
  application-layer feature in MVP (`docs/SECURITY_PRIVACY.md`,
  `docs/DECISIONS.md` D-020 item 10) — `LocalFilesystemStorage` relies on
  host/disk-level protection.
- Real CV content and real PII must never enter source control or test
  fixtures — synthetic fixtures only, enforced by `.githooks/pre-commit`
  and `scripts/scan-tracked-tree.sh`.
- Logs and audit-event metadata must never contain raw sensitive content
  — enforced by a behavioral guard in `record_event`
  (`backend/src/meyar/services/audit_repo.py`,
  `backend/tests/test_audit_privacy_guard.py`).

## 18. Remote administration

The reference Mac mini may operate headlessly in bank infrastructure.
Authorized remote-administration mechanisms may include, depending on
bank IT policy:

- bank-approved SSH access,
- bank-approved remote desktop / screen sharing,
- VPN or internal-network access controls.

**Actual remote-access configuration (which mechanism, credential/key
management, network exposure) is infrastructure/IT policy — it is not
hardcoded into MEYAR and is not defined by this document.** Do not assume
public SSH exposure or any other specific configuration; whatever is
chosen must be consistent with the loopback-only inference boundary (§5,
§11) and the tenant-isolation/auth model (§17) — it does not change
either.

## 19. Platform portability

**MEYAR is not architecturally tied to a specific Mac model.** The stack
(Python/FastAPI/SQLAlchemy/PostgreSQL+pgvector, the `DocumentStorage` and
`LLMProvider`/embedding-provider abstractions) and the current reference
hardware in §2C are separate facts. Portability is enabled by those
abstraction boundaries, but **a new deployment topology or hardware
combination must be revalidated** (§12), not assumed to work.

One distinction matters specifically for security posture:

- **Same-host application + inference** (current MVP model, §5, §11): the
  loopback-only guard is sufficient by construction — there is no network
  boundary between MEYAR and Ollama to secure.
- **Split application/inference hosts** (a future topology where Ollama
  runs on a different host than the MEYAR application): this is
  **explicitly not implemented**. It requires a new, separately approved
  security/architecture decision — network transport security between the
  hosts, authentication for the inference endpoint, and a revised
  no-exfiltration boundary all become live design questions the moment
  Ollama is not loopback-local to the application. Do not deploy this
  topology on the basis of this document.

## 20. Folder ingestion & reconciliation scheduling

Slice 14 (`docs/DECISIONS.md` D-021) adds one operator command,
`meyar reconcile-folder --tenant-id <id> --root <path> [--limit N]`, that
serves both the initial bulk import of an approved bank-controlled CV
folder and its repeatable reconciliation afterward. It reuses the Slice 6
folder scanner/indexer unchanged for discovery/ingestion, then drives
whichever candidates are not yet fully processed through profile
extraction, identity extraction, and embedding — so a newly added or
changed PDF/DOCX becomes searchable without a separate manual per-candidate
command.

- **Initial import:** run the command once against the approved source
  directory. Safe to interrupt and re-run — every step is idempotent.
- **Continuous reconciliation is periodic, not event-driven.** MEYAR does
  not ship or require a filesystem watcher (see D-021: restart safety,
  network/shared-filesystem portability, and avoiding an OS-specific
  dependency all favor a repeatable scan over an event stream). Scheduling
  the command to run periodically is a deployment-infrastructure
  responsibility, not an application one — MEYAR owns the command, not the
  timer.
- **Do not commit a bank-specific source path or a bank-specific scheduler
  configuration to this repository** — those are per-deployment operator
  configuration, same rule as `backend/.env` (§5).
- **Run at most one active `reconcile-folder` invocation per
  `(tenant, source root)` at a time.** This is the supported MVP
  operational model, not something MEYAR enforces at the database level —
  overlapping concurrent reconcilers against the same source can race on
  the exact-content dedup check and each mint a separate candidate for
  identical content (see `docs/DECISIONS.md` D-021 item 9). Do not
  configure two overlapping scheduled jobs (e.g. two different timers, or
  a manual run overlapping a scheduled one) against the same source.
  Distributed locking is not implemented.

Generic scheduler examples (illustrative only — adapt for the actual
deployment host, do not commit the result):

macOS (reference host, `launchd`) — a per-user LaunchAgent plist invoking
`uv run meyar reconcile-folder --tenant-id <id> --root <path>` from
`backend/` on an interval, with `StandardOutPath`/`StandardErrorPath`
pointed at a log location the operator controls.

Linux (future server) — either a `systemd` timer unit paired with a
oneshot service unit running the same command, or a `cron` entry; either
approach is equivalent from the application's perspective.

Operational notes:

- The command's exit code distinguishes clean success (`0`) from
  completed-with-failures (`1`, at least one ingestion or downstream
  processing failure — safe to simply re-run on the next scheduled
  invocation, per-candidate work is retried automatically), an invalid
  source folder (`2`), and an infrastructure/database failure (`3`) — treat
  `0`/`1` as "ran," and `2`/`3` as needing operator attention before the
  next scheduled run. Exit `1`/"Failed/pending retry" in the printed
  summary means that candidate's downstream processing did not fully
  complete this run (profile, identity, or embedding) — it does not by
  itself mean the candidate is unsearchable: a candidate whose profile
  and embedding already succeeded but whose identity extraction failed
  still remains fully searchable by professional profile (identity is
  presentation-only, never a search/ranking input).
- `--limit` bounds how many not-yet-ready candidates are processed in one
  invocation, so a large initial backlog does not force one unbounded
  sequential local-Ollama run — a large backlog is worked off across
  several scheduled invocations instead. Discovery/ingestion itself is
  never bounded by `--limit`; only downstream profile/identity/embedding
  processing is. Within the bound, a candidate that has never been
  attempted is always processed before one that already has a recorded
  failed attempt, so a persistently-failing candidate cannot permanently
  starve a candidate that has not been tried yet — later scheduled
  invocations always make progress on genuinely new work first.
- Output is PII-safe (ids and counts only), so command output is safe to
  forward to normal log aggregation, same as application logs (§6).
- `MEYAR_FOLDER_STABILITY_SECONDS` (default 60) controls the file-stability
  window — a file modified more recently than this is skipped for that run
  and picked up on the next one, so a slow/partial copy onto the source
  folder is never ingested mid-write.

## 21. Operator checklist

**Fresh deployment** — §4, §16.

**Update deployment** — §7, §16.

**Post-update verification** — §16.

**Workstation migration** — §13.

**Repository handover** — §14.

**Rollback** — §15, then §16.

**Folder reconciliation scheduling** — §20.

---

*This document defines process and references authoritative code/config.
It intentionally does not restate the full backup/restore mechanism
(`docs/BACKUP_RESTORE.md`), the full security threat model
(`docs/SECURITY_PRIVACY.md`), the full target-hardware benchmark procedure
(`docs/TARGET_MAC_BENCHMARK.md`), or the REST API reference (`README.md`)
— consult those directly for detail beyond an operator's day-to-day
sequence of commands.*
