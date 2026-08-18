# MEYAR — Status

## Current phase
Slice 2 (Job Criteria) implemented and verified. Not yet committed.

## Completed
- Preflight (hardware, tool versions) — DECISIONS.md D-001.
- Fast documentation pass and Claude Code harness.
- Backend scaffold: FastAPI + SQLAlchemy 2.0 async + Alembic + PostgreSQL.
- Slice 1 (Tenant + API Auth) — committed as `f5b4ec3`.
- Slice 2 (Job Criteria): `Job` + `JobCriteriaVersion` models (immutable,
  monotonically versioned per job) + migration; Pydantic criteria schema
  (`SKILL`/`EXPERIENCE`/`CERTIFICATION`/`EDUCATION`/`LANGUAGE`,
  `MUST_HAVE`/`PREFERRED`, weight, evidence_required,
  manual_review_required) with kind-specific field validation, duplicate-id
  rejection, and a sensitive/prohibited-attribute denylist validator (422 on
  gender/age/ethnicity/religion/marital/health/etc. terms in label or
  value). Endpoints: `POST /v1/jobs`, `GET /v1/jobs/{id}`, `POST
  /v1/jobs/{id}/criteria`, `GET /v1/jobs/{id}/criteria`, `GET
  /v1/jobs/{id}/criteria/{version}` — all tenant-scoped (404, not 403, on
  cross-tenant access), scoped by `jobs:read`/`jobs:write`. Audit events
  recorded on job creation and each new criteria version.

## Tests
19/19 passing (9 from Slice 1 + 10 new: job creation, auth requirement,
empty-criteria rejection, duplicate-id rejection, prohibited-criterion
rejection, EXPERIENCE-requires-min_years validation, version-never-mutated
+ current-resolves-to-latest + full-history-listing, unknown
job/version → 404, cross-tenant job read/write/list → 404). `ruff` and
`mypy` clean. Live smoke flow verified manually: create job v1 → create
v2 (adds a certification) → current resolves to v2 → v1 still retrievable
unmutated → full history `[1, 2]` → prohibited criterion → 422.

## In progress
Nothing in flight.

## Blockers
None blocking. Same open items as before: real Apple Silicon Mac not yet
available (D-001); Auto Mode script still dry-run only, owner must run
`--apply` explicitly.

## Next slice
Slice 3 — Candidate Upload (secure storage, document parsing into a
canonical representation). Slice 2 work is implemented/tested but
intentionally left uncommitted per current instructions.
