# MEYAR — Status

## Current phase
Slice 1 (Tenant + API Auth) complete and verified.

## Completed
- Preflight (hardware, tool versions) — DECISIONS.md D-001.
- Fast documentation pass (CLAUDE.md, docs/*) and Claude Code harness
  (rules, agents, skills, hooks, Auto Mode script — not yet applied, see
  below).
- Backend scaffold: FastAPI + SQLAlchemy 2.0 async + Alembic + PostgreSQL,
  managed with `uv`.
- Slice 1: `Tenant`, `ApiKey`, `AuditEvent` models + initial migration;
  `meyar.core.security`/`meyar.core.auth` (SHA-256 key hashing, Bearer auth,
  scope checks); `meyar create-tenant` CLI (mints tenant + first API key,
  shown once); `GET /v1/health` (public), `GET /v1/usage` (protected, proves
  the auth boundary). Verified live: health 200, unauthenticated usage 401,
  valid key 200 with only its own tenant_id.
- Tests: 9/9 passing (auth rejection paths — missing/garbage/revoked/expired
  key, malformed header; cross-tenant isolation). `ruff` and `mypy` clean.
- Fixed a real host-infra issue (Docker bridge-network port publishing
  stalling new connections under memory pressure) — see DECISIONS.md D-005.

## In progress
- Nothing in flight.

## Blockers
- None blocking. Open: real Apple Silicon Mac not yet available for final
  model benchmark (D-001). This dev machine (7.5GB RAM, heavy swap) is
  resource-constrained — expect slower local iteration, not a correctness
  issue.
- Auto Mode script (`scripts/setup-claude-auto-mode.sh`) has only been
  dry-run so far, per policy (never silently alter global config during
  bootstrap). Owner should run `scripts/setup-claude-auto-mode.sh --apply`
  when ready.

## Next slice
Slice 2 — Job Criteria (tenant → create job → versioned criteria).
