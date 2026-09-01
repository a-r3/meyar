# MEYAR

MEYAR is an internal AI Candidate Intelligence & CV Search
Platform. It is intended to help authorized HR staff ingest candidate
documents, extract professional facts locally, search the candidate library,
and evaluate candidates against job requirements with auditable evidence.
MEYAR is internal HR tooling, not an external B2B/SaaS product or a public
candidate-facing service.

The repository contains the working backend foundation through deterministic
JD scoring and batch ranking, the strict local-LLM natural-language planner,
deterministic hybrid search, the server-rendered internal HR browser
interface, and a finalized internal `/api/v1` REST surface with offline
Swagger/OpenAPI documentation (Slice 12). Final security/DoD acceptance
(Slice 13) remains outstanding and must not be treated as complete.

## Project authority

This README is the human onboarding entry point. Detailed requirements and
current status remain canonical in:

- [`docs/PROJECT_VISION.md`](docs/PROJECT_VISION.md) — product direction
- [`docs/MASTER_SPEC.md`](docs/MASTER_SPEC.md) — technical specification
- [`docs/STATUS.md`](docs/STATUS.md) — implemented state and gaps
- [`docs/SECURITY_PRIVACY.md`](docs/SECURITY_PRIVACY.md) — data and AI rules
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — accepted decisions and context

## Current purpose and capabilities

Implemented and tested:

- FastAPI backend with PostgreSQL and Alembic migrations;
- tenant/resource isolation and hashed API-key authentication;
- versioned job criteria;
- validated PDF/DOCX upload, opaque local storage, and canonical parsing;
- local-LLM professional-profile extraction behind `LLMProvider`, with
  redaction, schema validation, evidence validation, and safe failure states;
- deterministic per-criterion evaluation and fit-band policy, exposed through
  an internal service, CLI, and the REST scoring endpoint below;
- internal health, usage, job, candidate, search, and evaluation/ranking API
  routes under `/api/v1`, with offline Swagger UI and OpenAPI security scheme;
- local CV folder scanning/indexing (`meyar index-folder`): symlink-safe
  recursive discovery, SHA-256 content-hash idempotent re-scanning, and
  ingestion through the same secure pipeline as direct upload;
- `CandidateIdentity` (`meyar extract-identity`): local, evidence-backed
  extraction of full_name/email/phone into an immutable, versioned table
  wholly separate from `CandidateProfile` — never read by
  matching/evaluation/search/embedding;
- local candidate embeddings + pgvector storage (`meyar embed-candidate`):
  a local `EmbeddingProvider` abstraction, deterministic professional-only
  serialization (identity excluded by construction), idempotent
  version-traceable persistence with correct current/stale semantics;
- deterministic hybrid candidate search (`meyar search-candidates`):
  structured required/preferred filters over the current professional
  profile, local pgvector semantic retrieval restricted to current and
  exactly provenance-compatible embeddings, and a documented
  `meyar-search-v1` hybrid-ranking formula where a failed required
  filter can never be overridden by semantic similarity — available via
  CLI, the `/ui` chat search, and `POST /api/v1/search`;
- natural-language candidate search planning (`meyar plan-search`): a
  loopback-only local LLM produces a strict `PlannerDraft`; deterministic
  `meyar-search-planner-v1` validation derives the search mode, rejects
  protected or unsupported meaning instead of weakening it, injects trusted
  tenant-independent runtime provenance/date/weights, and produces the exact
  `CandidateSearchRequest` Slice 8 executes. The planner never reads or ranks
  candidates. Its explicit Azerbaijani MVP morphology policy preserves common
  mandatory forms and protected-term inflections without broad prefix
  matching; plan-only and thin plan→search service flows are both tested, and
  the same fail-closed typed outcome contract is exposed via
  `POST /api/v1/search/natural-language`;
- deterministic JD scoring (`meyar evaluate --as-of-date YYYY-MM-DD` or
  `POST /api/v1/jobs/{job_id}/criteria/{version_number}/score`):
  `meyar-policy-v1` criterion/fit evaluation plus `meyar-score-v1` Decimal
  0–100 scoring, exact structured explanation, explicit date provenance, and
  idempotent reuse of the immutable Evaluation for exact repeated inputs;
- deterministic candidate-library ranking (`meyar rank-job` or
  `POST /api/v1/jobs/{job_id}/criteria/{version_number}/rank`): exactly one
  current completed professional profile per active tenant candidate, explicit
  fit tiers before numeric score, and candidate UUID as the stable non-PII
  tie-break. It has no semantic-search, embedding, LLM, or identity dependency;
- internal browser UI (`/ui`): Azerbaijani chat/search, fail-closed typed
  planner outcomes, tenant-scoped CV Library and candidate detail, current
  presentation-only identity, existing-job deterministic ranking, canonical
  score/fit/contribution display, and safe internal error states;
- accountable human identity & dual access (Slice 1, issue #30): `/ui/login`
  authenticates a human with a username/Argon2id-hashed password, never an
  API key, and issues a fresh eight-hour opaque server-side session cookie
  (only its SHA-256 digest is persisted); every request re-derives the
  authenticated user, active tenant membership, and role live from the
  database. Machine REST clients keep authenticating with an API key exactly
  as before — the two paths are fully independent. Authenticated POSTs are
  CSRF-protected;
- repository-packaged Jinja2 templates and local CSS with no Node build,
  frontend package manager, remote asset, analytics, or telemetry dependency;
- synthetic-only automated tests and repository governance.

Planned or in progress:

- **Slice 13** — security and Definition-of-Done acceptance. Implementation
  pass 1 (PR #22) is complete: original-CV access, no-exfiltration formal
  verification, backup/restore acceptance, audit-privacy guard, and
  multilingual evidence. **Remaining**: Target-Mac benchmark execution on the
  owner-confirmed reference hardware (Mac mini M4 Pro) and the resulting
  production-model decision — see `docs/STATUS.md`. Not yet claimed complete.

The superseded External Async Evaluation API version of Slice 6 is cancelled.

## Architecture

MEYAR is a Python modular monolith:

```text
Internal UI / approved internal systems
                    |
              FastAPI API
                    |
        services + deterministic policy
             |                 |
      PostgreSQL          provider boundaries
                               |
                    local Ollama / local embeddings
```

Candidate document content is untrusted data. AI output is schema-validated
before use, and the deterministic policy engine—not the LLM—computes matching
results. Candidate identity is presentation-only and must never become a
search, scoring, or ranking signal.

## Local AI and privacy

Candidate data must remain on local/internal infrastructure. Never send CV
content, candidate profiles, identity data, or embeddings to an external AI,
embedding, analytics, or cloud service. Ollama must remain loopback/internal
only. Real CVs and candidate PII must never be committed; this repository may
contain only clearly synthetic fixtures under `fixtures/synthetic_cvs/`.

`qwen3:0.6b` is only the integration-verified development model for the
current Linux development machine. It is not the approved production model.
Final model selection requires benchmarking on the target Apple Silicon Mac
Mini.

## Technology stack

- Python 3.12, managed with `uv`
- FastAPI, Pydantic v2, Uvicorn
- Jinja2 server-rendered HTML and repository-local CSS (no Node requirement)
- SQLAlchemy 2.0 async, Alembic, PostgreSQL 16
- `pypdf` and `python-docx` for current document parsing
- Ollama through `meyar.llm.LLMProvider`
- Ruff, mypy, pytest, pytest-asyncio
- pgvector and local Ollama-backed LLM/embedding provider abstractions

## Repository structure

```text
backend/                 FastAPI application, migrations, tests, uv lockfile
docs/                    Canonical product, architecture, status, and decisions
fixtures/synthetic_cvs/  Synthetic test documents only
.claude/                 Claude entry/orchestration rules
.githooks/               Repository-local Git safety guards
.github/                 CI, PR template, and Dependabot configuration
scripts/                 Repository setup helpers
AGENTS.md                 Codex project entry point
CLAUDE.md                 Claude project entry point
```

## Prerequisites

- Git
- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)
- Docker Engine with the Docker Compose plugin
- Ollama only when intentionally running live local profile extraction; it is
  not required for the quality gate or ordinary fake-provider tests

After every fresh clone, activate the repository-local safety hooks before
material work (hooks are not transferred by Git):

```bash
scripts/setup-git-governance.sh
git config --get core.hooksPath  # must print .githooks
```

## Local backend setup

From the repository root:

```bash
cd backend
uv sync --locked
cp .env.example .env
cd ..
docker compose up -d postgres
docker compose ps
```

`backend/.env.example` contains non-production local defaults only. Copy it to
the git-ignored `backend/.env` and adjust values locally when necessary. Never
commit `.env`, API keys, credentials, or production configuration.

The checked-in Docker Compose file uses `network_mode: host` and port 55719
only as the documented D-005 workaround for the current development laptop.
It is not CI or production architecture.

Apply database migrations from `backend/`:

```bash
cd backend
uv run alembic upgrade head
```

Slice 11 requires migration `e3b1f7a9c2d4`, which creates only the
`browser_sessions` table. The browser cookie is Secure by default. For local
loopback HTTP development, explicitly set the non-production override in the
git-ignored `backend/.env`:

```bash
MEYAR_UI_COOKIE_SECURE=false
```

Never use that override for production HTTPS deployment. Browser sessions have
a fixed eight-hour lifetime (`MEYAR_UI_SESSION_TTL_HOURS=8` by default), no
remember-me behavior, and no sliding unlimited renewal.

Score one candidate's current profile against a job's current criteria with an
explicit reproducibility date:

```bash
cd backend
uv run meyar evaluate \
  --tenant-id <TENANT_UUID> \
  --candidate-id <CANDIDATE_UUID> \
  --job-id <JOB_UUID> \
  --as-of-date 2026-01-01
```

Rank the tenant's active candidate library against one exact criteria version:

```bash
cd backend
uv run meyar rank-job \
  --tenant-id <TENANT_UUID> \
  --job-criteria-version-id <CRITERIA_VERSION_UUID> \
  --as-of-date 2026-01-01
```

Both commands print only non-PII IDs, policy versions, fit/score values, safe
reason codes, and counts. Exit 2 is invalid/tenant-scoped input, exit 3 is a
scoring-policy failure (including legacy all-zero weights), and exit 4 is an
infrastructure/database failure. A valid empty batch exits successfully.

Run the API:

```bash
uv run uvicorn meyar.main:app --reload
```

Local endpoints:

- Health: `http://127.0.0.1:8000/api/v1/health`
- Internal HR UI: `http://127.0.0.1:8000/ui`
- Swagger UI: `http://127.0.0.1:8000/docs`
- OpenAPI JSON: `http://127.0.0.1:8000/openapi.json`

Protected API routes require a locally issued API key. `/ui/login` is a
separate, human-only path: a username and password, never an API key —
Argon2id-verified against a `User` row, then exchanged for a session cookie
scoped to one active `TenantMembership`. Neither path ever places the API
key, a password, identity, query, or result data in JavaScript or browser
storage. Do not place keys or passwords in this README, source files, shell
history, logs, URLs, or Git.

### Creating a human login

```bash
cd backend
uv run meyar create-user --username "hr.analyst"
uv run meyar add-membership --username "hr.analyst" --tenant-id "<tenant UUID>" --role HR_USER
```

Both commands prompt interactively (never a CLI argument, so a password never
lands in shell history). `set-password`, `disable-user`/`enable-user`, and
`disable-membership`/`enable-membership` round out provisioning — see
`uv run meyar --help`.

## Internal REST API

`/api/v1` is the internal REST surface for approved internal HR clients (the
`/ui` browser interface and other approved internal systems). It is
Bearer-API-key authenticated and fully independent of the `/ui`
BrowserSession/CSRF cookie mechanism — never authenticate a REST client with a
UI session cookie, and never authenticate the UI with an API key header.
`/ui/*` routes are deliberately excluded from the OpenAPI schema
(`include_in_schema=False`); the schema below describes the product REST API
only.

### Creating an API key

API keys are provisioned administratively via the CLI — there is no HTTP
endpoint that issues keys (an unauthenticated key-issuance endpoint would
defeat the auth boundary):

```bash
cd backend
uv run meyar create-tenant --name "Example HR Team"
```

This prints the new tenant's UUID and the plaintext API key **exactly once**.
Store it immediately in a secret manager or local environment variable — it is
never shown again and is stored server-side only as a salted hash. A new key
is seeded with all six current scopes (`jobs:read`, `jobs:write`,
`candidates:read`, `candidates:write`, `evaluations:read`,
`evaluations:write`).

### Authenticating requests

```bash
export MEYAR_API_KEY='<paste the key printed above>'
curl -H "Authorization: Bearer $MEYAR_API_KEY" http://127.0.0.1:8000/api/v1/usage
```

Missing/invalid/expired/revoked keys return `401`. A valid key missing a
required scope returns `403`. The tenant is always derived from the key
server-side — no request may supply its own `tenant_id`.

### Swagger UI / OpenAPI

- `GET /docs` — fully offline Swagger UI (assets are served locally via the
  vendored `swagger-ui-bundle` package; no CDN/internet dependency at
  runtime). Click **Authorize** and paste `<your API key>` (the `Bearer `
  prefix is added for you by the Authorize dialog).
- `GET /openapi.json` — the canonical, FastAPI-generated API spec; also
  requires no network access to generate.

### Endpoint summary

| Method | Path | Scope | Purpose |
|---|---|---|---|
| GET | `/api/v1/health` | none | Liveness only — does not probe DB/Ollama. |
| GET | `/api/v1/usage` | any valid key | Tenant-scoped all-time candidate/evaluation counts. |
| POST | `/api/v1/jobs` | `jobs:write` | Create a job with its first criteria version. |
| GET | `/api/v1/jobs/{job_id}` | `jobs:read` | Job + current criteria version. |
| POST | `/api/v1/jobs/{job_id}/criteria` | `jobs:write` | Create a new immutable criteria version. |
| GET | `/api/v1/jobs/{job_id}/criteria` | `jobs:read` | List all criteria versions. |
| GET | `/api/v1/jobs/{job_id}/criteria/{version_number}` | `jobs:read` | One exact criteria version. |
| POST | `/api/v1/candidates` | `candidates:write` | Create a candidate record. |
| GET | `/api/v1/candidates/{id}` | `candidates:read` | Candidate record only. |
| GET | `/api/v1/candidates/{id}/detail` | `candidates:read` | Identity + current profile + documents + evaluation history. |
| DELETE | `/api/v1/candidates/{id}` | `candidates:write` | Cascade-delete a candidate and its documents. |
| POST | `/api/v1/candidates/{id}/documents` | `candidates:write` | Upload a CV (PDF/DOCX, size/MIME validated). |
| GET | `/api/v1/candidates/{id}/documents` | `candidates:read` | List uploaded documents + parse status. |
| GET | `/api/v1/candidates/{id}/documents/{doc_id}` | `candidates:read` | One document + canonical parse metadata. |
| POST | `/api/v1/search` | `candidates:read` | Structured/semantic/hybrid search (Slice 8). |
| POST | `/api/v1/search/natural-language` | `candidates:read` | Natural-language search planning + execution (Slice 9), typed fail-closed outcomes. |
| POST | `/api/v1/jobs/{job_id}/criteria/{version_number}/score` | `evaluations:write` | Deterministic 0–100 score for one candidate against one criteria version (may persist a new `Evaluation`). |
| POST | `/api/v1/jobs/{job_id}/criteria/{version_number}/rank` | `evaluations:write` | Deterministic batch ranking of the active candidate library. |

### curl examples (synthetic data only)

Structured search:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/search \
  -H "Authorization: Bearer $MEYAR_API_KEY" -H "Content-Type: application/json" \
  -d '{
    "mode": "STRUCTURED_ONLY",
    "required_filters": {"skills": ["Python"]},
    "limit": 10
  }'
```

Natural-language search (Azerbaijani or English query, explicit `as_of_date`
required for reproducibility):

```bash
curl -X POST http://127.0.0.1:8000/api/v1/search/natural-language \
  -H "Authorization: Bearer $MEYAR_API_KEY" -H "Content-Type: application/json" \
  -d '{"query": "5+ il Python təcrübəsi olan namizədlər", "as_of_date": "2026-01-01"}'
```

The response's `outcome` field is always one of `EXECUTABLE`,
`PROHIBITED_REQUEST`, `UNSUPPORTED_SEMANTICS`, `AMBIGUOUS_REQUEST`,
`MALFORMED_MODEL_OUTPUT`, `PLANNER_PROVIDER_FAILURE`, or
`VALIDATION_FAILURE` — `search` is populated only when `outcome ==
"EXECUTABLE"`. A `503` (not a typed outcome) means genuine search/database
infrastructure is unavailable, distinct from `PLANNER_PROVIDER_FAILURE` (a
normal `200` outcome meaning the local planner LLM itself failed/timed out).

Score one candidate:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/jobs/<JOB_UUID>/criteria/1/score \
  -H "Authorization: Bearer $MEYAR_API_KEY" -H "Content-Type: application/json" \
  -d '{"candidate_id": "<CANDIDATE_UUID>", "evaluation_as_of_date": "2026-01-01"}'
```

Batch-rank the library against one criteria version:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/jobs/<JOB_UUID>/criteria/1/rank \
  -H "Authorization: Bearer $MEYAR_API_KEY" -H "Content-Type: application/json" \
  -d '{"evaluation_as_of_date": "2026-01-01"}'
```

`evaluation_as_of_date` is always required and is never defaulted to "today" —
identical inputs (candidate/profile version, criteria version, date) return
the same immutable `evaluation_id` on repeat calls (`reused: true`) rather
than creating a duplicate. `numeric_score` is always a canonical two-decimal
string (e.g. `"75.00"`), never a bare float.

### Local AI dependency map

| Operation | Requires local Ollama? |
|---|---|
| Health, usage, jobs, candidates, candidate detail, documents | No |
| `POST /api/v1/search` (`STRUCTURED_ONLY`) | No |
| `POST /api/v1/search` (`SEMANTIC_ONLY`/`HYBRID`) | Yes — local embedding model only |
| `POST /api/v1/search/natural-language` | Yes — local LLM planner, then local embeddings if the plan needs semantic retrieval |
| Score / batch rank | No — deterministic policy engine only, no LLM call |

No operation ever calls an external/cloud AI, embedding, or telemetry service
— see [Local AI and privacy](#local-ai-and-privacy).

### Common error semantics

`401` missing/invalid/expired/revoked API key. `403` valid key missing a
required scope. `404` resource not found *or* belongs to another tenant
(identical response either way — existence is never leaked cross-tenant).
`422` malformed/invalid request body or an unsupported domain state (e.g. a
candidate with no completed profile). `503` local AI/database infrastructure
genuinely unavailable. Error bodies never include a traceback, SQL text, a
filesystem path, or API-key/tenant internals.

## Quality gate

Run from `backend/`:

```bash
uv run ruff check .
uv run mypy src
uv run pytest -q
```

`mypy src` is the current source gate. Whole-repository `mypy .` has known
pre-existing test-only type debt and is not the CI gate yet.

## Git and dependency workflow

Normal material work uses a task branch (`feat/*`, `fix/*`, `chore/*`,
`docs/*`, or `test/*`), the quality gate, intentional staging, a push, and a
pull request to `main`, the stable integration branch. CI must pass, then the
owner reviews and uses **Squash and merge**. Agents do not automatically merge
normal development PRs. After the owner reports a merge, the agent verifies
the remote state and synchronizes local `main` before creating another task
branch. Do not push directly to `main`.

Dependabot checks backend `uv` and GitHub Actions dependencies weekly.
Routine minor/patch version updates are grouped to reduce noise; major updates
remain separate. Every dependency PR must pass CI and receive owner review.
Auto-merge is intentionally disabled. Dependency changes must include the
updated `backend/uv.lock` when applicable.

## Known current limitations

- The natural-language planner intentionally rejects unsupported language
  proficiency, skill-specific duration, identity, salary/location, and
  custom-weight requests rather than weakening their meaning — this is
  deliberate fail-closed behavior, not a gap.
- Raw CV file download/retrieval is not exposed over `/api/v1` — only
  extracted structured facts and parse metadata there. It is available,
  UI-only and authenticated (`candidates:read`, tenant/candidate/document
  ownership verified server-side), at
  `GET /ui/candidates/{candidate_id}/documents/{document_id}/original`
  (Slice 13) — see `docs/STATUS.md`.
- No rate limiting is implemented yet (`MEYAR_RATE_LIMIT_PER_MINUTE` exists in
  config but is not yet enforced).
- No CORS policy is configured; the current same-origin UI + internal API
  deployment does not require one. A specific internal cross-origin client
  would need an explicit allowlisted-origin decision, not a wildcard.
- OCR fallback for scanned PDFs is not implemented.
- The configured embedding model is a development/integration default
  (`DEV_INTEGRATION_MODEL`), not an approved final production model —
  approval is blocked on the target Mac Mini benchmark. Target reference
  hardware (Mac mini M4 Pro, 12-core CPU/16-core GPU/24GB unified memory/
  512GB SSD) is now owner-confirmed but the benchmark has not yet been
  executed on it — see `docs/TARGET_MAC_BENCHMARK.md`.
- Profile/identity extraction (`extract-profile`, `extract-identity`) and
  folder indexing remain CLI/service-only by design — not part of the
  official REST API surface.
- Application-layer encryption at rest is not implemented; real production
  candidate data requires the approved protected storage environment.
- Server-side branch protection is unavailable on the current private
  repository plan; hooks, PRs, CI, and owner review are the accepted fallback.
- Slice 13 security/Definition-of-Done acceptance, implementation pass 1
  (original CV access, no-exfiltration formal verification, backup/restore
  acceptance, audit-privacy guard, multilingual evidence) is complete
  (PR #22) — see `docs/STATUS.md`. Target-Mac benchmark execution on the
  now owner-confirmed reference hardware has not yet run and remains the
  sole mandatory blocker to MVP closure; no formal penetration test has
  been performed.

See [`docs/STATUS.md`](docs/STATUS.md) for the complete current gap matrix and
the latest next action.
