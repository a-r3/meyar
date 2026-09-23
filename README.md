# MEYAR

MEYAR is an **internal AI Candidate Intelligence & CV Search Platform** for
bank HR. It ingests candidate CVs, extracts professional facts locally,
lets HR search/browse the candidate library, and evaluates candidates
against job requirements — with deterministic scoring and auditable
evidence throughout.

MEYAR is internal HR tooling: no external customers, no public surface, no
billing. It is not a hiring-decision engine — the LLM interprets and
explains; a deterministic policy engine, not the LLM, decides scores and
match status.

## Product principles

> AI understands. Database remembers. Search retrieves. Deterministic
> policy evaluates. Evidence explains. Humans decide.

- The LLM's output is an **interpretation**, never scoring authority. It
  never computes the final numeric score, never decides a hiring outcome,
  and never becomes "true" simply because it is schema-shaped.
- `CandidateIdentity` (name/contact) is presentation-only. It may be shown
  to authorized HR users but is structurally excluded from search,
  matching, scoring, and embeddings — those read only `CandidateProfile`
  (professional facts).
- Sensitive/irrelevant attributes (gender, photo, DOB, ethnicity, religion,
  marital status, political opinion, health) never enter `CandidateProfile`
  and never influence matching.
- Missing or unsupported evidence becomes `UNKNOWN` / a human-review state
  — never invented certainty, and `UNKNOWN` is never silently downgraded to
  a negative result.
- All candidate/JD-content AI runs on local Ollama only. No candidate data
  or embedding ever reaches an external/cloud AI service.

## Project authority

This README is the human onboarding entry point; it summarizes current
reality but does not replace canonical detail:

- [`docs/PROJECT_VISION.md`](docs/PROJECT_VISION.md) — product direction
- [`docs/MASTER_SPEC.md`](docs/MASTER_SPEC.md) — technical specification
- [`docs/STATUS.md`](docs/STATUS.md) — implementation status and gaps (the
  detailed authority — this README does not restate its history)
- [`docs/SECURITY_PRIVACY.md`](docs/SECURITY_PRIVACY.md) — data and AI rules
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — accepted decisions and context

**Freshness rule:** this README describes *merged* product truth only. When
a material capability or phase is accepted and merged, this file should be
reviewed for stale capability/roadmap claims. `docs/STATUS.md` remains the
detailed implementation-status authority; planned/unmerged work must never
be described here as implemented.

## What HR can do today

### Secure CV ingestion

- Direct upload and local-folder bulk import/reconciliation share one
  validated ingestion pipeline: MIME sniffing (not filename), max size,
  opaque storage id — a local file is never trusted more than an upload.
- SHA-256 content-hash change detection: unchanged files are never
  reprocessed; a changed file creates a new immutable document version;
  byte-identical content at a different path is deduplicated instead of
  creating a duplicate candidate.
- Authenticated original-document access
  (`GET /ui/candidates/{candidate_id}/documents/{document_id}/original`)
  for authorized HR users only.

### Candidate understanding

- `CandidateIdentity` (name/email/phone) is extracted and stored separately
  from `CandidateProfile` (skills, employment, education, certifications,
  languages, projects) — never read by any matching/search/ranking code
  path.
- Extraction is local, schema-validated, and evidence-backed: every fact
  must be attributable to a real, verbatim quote in the source document,
  not just schema-shaped output. Facts a source document doesn't literally
  support fail closed to `FAILED`/unavailable rather than becoming
  positive evidence.
- Local embeddings (pgvector-backed) are generated from professional
  content only, with version/provenance tracking so a superseded profile's
  embedding is correctly excluded.

### Candidate search

- Structured, semantic, and hybrid search over the current professional
  profile, with a documented deterministic ranking formula
  (`meyar-search-v1`) — a failed required filter can never be overridden by
  semantic similarity.
- Natural-language search accepts Azerbaijani and English (including safe
  mixed professional terminology). Requests are parsed through
  server-owned, source-bound semantic interpretation before any criterion
  is built — see "Agent authority pipeline" below.
- Typed, fail-closed outcomes: a request that can't be safely interpreted
  is honestly declined (`UNSUPPORTED_SEMANTICS`, `AMBIGUOUS_REQUEST`, etc.),
  never silently weakened into a different, easier search.
- Protected/personal attributes (gender, age, nationality/citizenship,
  health, disability) can never become a scoring or search criterion,
  regardless of phrasing, language, or which model produced a draft.

### MEYAR AI — the agent workflow

MEYAR AI (`/ui/agent`) is the primary post-login HR surface. It has three
distinct behaviors:

1. **Ordinary candidate search.** HR asks for candidates matching some
   constraint in plain language. No `Job`/`CriteriaVersion` is created
   merely to answer a search.
2. **Vacancy analysis.** HR pastes/describes a job requirement. MEYAR
   segments and interprets it into server-authorized typed requirements,
   which HR reviews. Only requirements that pass server-side validation
   (subject, kind, modality, duration/level) may cross into a confirmable
   state; unsupported or ambiguous items are disclosed, never silently
   dropped or silently scored. This step is interpretation and review — it
   is **not** ranking.
3. **Confirmation → deterministic evaluation/ranking.** Confirming a
   reviewed draft is a dedicated, idempotent server operation, distinct
   from ordinary vacancy creation, that locks in the exact confirmed
   requirement set. Only after that commit does deterministic evaluation
   run: `meyar-policy-v2` per-criterion status, `meyar-score-v1` 0–100
   Decimal scoring, and fit-tier/score-ordered ranking with per-candidate
   evidence.

The agent can also answer a candidate profile/evidence lookup for a
candidate already surfaced by a prior search in the same turn. Its
free-text output is otherwise structurally bounded: `FINAL_ANSWER`/
`CLARIFY` carry only a closed response code the server maps to fixed copy —
there is no channel for the model to assert an unsupported factual claim.

### Candidate library and detail

- Server-rendered CV Library (`/ui/library`) and candidate detail with
  evidence/explanation and authorized original-CV access.
- Normal HR screens show no raw UUIDs, parser internals, or pipeline-status
  detail — those remain developer-facing only.

## Agent authority pipeline

```text
HR natural language (Azerbaijani / English)
        |
Local LLM interpretation (Ollama, loopback only)
        |
Server-owned source/span authority
  (occurrence-exact requirement spans; the model references
   a span id — its own restated text is never authority)
        |
Validated typed search/requirement intent
  (subject, kind, modality, duration/level — protected
   attributes rejected before and after interpretation)
        |
Structured / semantic / hybrid retrieval
        |
Accepted CandidateProfile facts + evidence
  (claim-specific, quote-attributed — never CandidateIdentity)
        |
Deterministic evaluation / ranking (meyar-policy-v2, meyar-score-v1)
        |
Evidence-backed explanation
        |
Human HR decision
```

Key invariants:

- Source spans, subject identity, and confirmation authority are
  server-owned — a model-authored field is a hint, never authority.
- Workflow controls (e.g. "show me 10 candidates") are parsed as separate,
  bounded metadata and can never become a scoring criterion.
- Ambiguous or unsupported material fails closed to a review/UNSUPPORTED
  state rather than guessing.
- Confirmation is enforced server-side (tenant/session-bound, idempotent,
  tamper-checked) — never only by what the browser UI happens to show.

## What's implemented vs. planned

**Implemented and merged:** ingestion (upload + folder import/
reconciliation), identity/profile extraction with claim-level evidence
attribution, local embeddings, structured/semantic/hybrid search, the AZ/EN
natural-language search and JD-requirement interpretation described above,
deterministic evaluation/scoring/ranking, the MEYAR AI conversational
workflow (search, JD drafting, review, confirmation, ranking), the CV
Library/candidate detail UI, and the internal REST API below.

**Not yet implemented** (see "Roadmap" and "Known current limitations"):
agentless deployment tooling completion, the real target-hardware model
benchmark, conversational follow-up on a prior result set, evidence-backed
candidate Q&A/comparison, and full API-first parity for every agent
workflow step.

## Architecture

```text
PDF / DOCX (upload or local-folder import)
        |
Secure ingestion (MIME sniff, size cap, opaque id) -> CanonicalDocument
        |
Local LLM extraction -> CandidateIdentity  +  CandidateProfile
                          (presentation-only)   (search/scoring input)
                                                     |
                                          Local embeddings -> PostgreSQL + pgvector
                                                     |
                          Structured / semantic / hybrid retrieval
                                                     |
                          Server-authorized natural-language intent
                                                     |
                          Deterministic evaluation / ranking
                                                     |
                          Evidence-backed UI / API response
                                                     |
                                       Human HR decision
```

MEYAR is a Python modular monolith: one FastAPI app, one PostgreSQL
database, no Kafka/Kubernetes/microservices. `CandidateIdentity` is a
separate branch from the moment extraction happens — it never merges back
into anything the matching/search/ranking/embedding code reads.

## Local AI and privacy

Candidate data must remain on local/internal infrastructure:

- Every model call (extraction, identity, embeddings, search planning, and
  the agent) goes through `meyar.llm.LLMProvider` / the local embedding
  provider — never a direct Ollama import elsewhere in the codebase.
- Ollama is loopback-only in every environment; the HTTP client rejects a
  non-loopback base URL at construction and disables environment-derived
  proxy routing (`trust_env=False`), so a process-environment proxy
  variable cannot redirect a request off-machine.
- Real CVs and candidate PII must never be committed; this repository may
  contain only clearly synthetic fixtures under `fixtures/synthetic_cvs/`.
- No formal penetration test has been performed, and no security
  validation has been run on target deployment hardware yet.

### Model status

Three distinct things, not one:

| | Model | Status |
|---|---|---|
| Source/default (dev, tests, integration default) | `qwen3:0.6b` | Intentional lightweight development setting — **not** a production approval |
| Recent acceptance/browser verification override | `qwen3:1.7b` | Used for stronger local acceptance testing during recent development — **not** a production approval |
| Final production model | **TBD** | Selected only via the real target-hardware benchmark (issue #36) on the actual Mac mini M4 Pro — that measurement is authority, not either model above |

## Technology stack

- Python 3.12, managed with `uv`
- FastAPI, Pydantic v2, Uvicorn
- Jinja2 server-rendered HTML and repository-local CSS (no Node requirement)
- SQLAlchemy 2.0 async, Alembic, PostgreSQL 16 + pgvector
- `pypdf` and `python-docx` for document parsing
- Ollama through `meyar.llm.LLMProvider` and a matching local embedding
  provider abstraction
- Ruff, mypy, pytest, pytest-asyncio

## Repository structure

```text
backend/                 FastAPI application, migrations, tests, uv lockfile
docs/                    Canonical product, architecture, status, and decisions
fixtures/synthetic_cvs/  Synthetic test documents only
.claude/                 Claude entry/orchestration rules
.githooks/               Repository-local Git safety guards
.github/                 CI, PR template, and Dependabot configuration
scripts/                 Repository setup helpers
AGENTS.md                Codex project entry point
CLAUDE.md                Claude project entry point
```

## Prerequisites

- Git
- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)
- Docker Engine with the Docker Compose plugin
- Ollama only when intentionally running live local profile extraction; it
  is not required for the quality gate or ordinary fake-provider tests

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

`backend/.env.example` contains non-production local defaults only. Copy it
to the git-ignored `backend/.env` and adjust values locally when necessary.
Never commit `.env`, API keys, credentials, or production configuration.

The checked-in Docker Compose file uses `network_mode: host` and a
non-default local port as a documented development-laptop workaround (see
`docs/DECISIONS.md` D-005). It is not CI or production architecture.

Apply database migrations from `backend/`:

```bash
cd backend
uv run alembic upgrade head
```

The browser cookie is Secure by default. For local loopback HTTP
development, explicitly set the non-production override in the git-ignored
`backend/.env`:

```bash
MEYAR_UI_COOKIE_SECURE=false
```

Never use that override for production HTTPS deployment. Browser sessions
have a fixed eight-hour lifetime (`MEYAR_UI_SESSION_TTL_HOURS=8` by
default), no remember-me behavior, and no sliding unlimited renewal.

Score one candidate's current profile against a job's current criteria with
an explicit reproducibility date:

```bash
cd backend
uv run meyar evaluate \
  --tenant-id <TENANT_UUID> \
  --candidate-id <CANDIDATE_UUID> \
  --job-id <JOB_UUID> \
  --as-of-date 2026-01-01
```

Rank the tenant's active candidate library against one exact criteria
version:

```bash
cd backend
uv run meyar rank-job \
  --tenant-id <TENANT_UUID> \
  --job-criteria-version-id <CRITERIA_VERSION_UUID> \
  --as-of-date 2026-01-01
```

Both commands print only non-PII IDs, policy versions, fit/score values,
safe reason codes, and counts. Exit 2 is invalid/tenant-scoped input, exit 3
is a scoring-policy failure, and exit 4 is an infrastructure/database
failure. A valid empty batch exits successfully.

Run the API:

```bash
uv run uvicorn meyar.main:app --reload
```

Local endpoints:

- Health: `http://127.0.0.1:8000/api/v1/health`
- Internal HR UI: `http://127.0.0.1:8000/ui`
- MEYAR AI agent workspace: `http://127.0.0.1:8000/ui/agent`
- CV Library: `http://127.0.0.1:8000/ui/library`
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

Both commands prompt interactively (never a CLI argument, so a password
never lands in shell history). `set-password`, `disable-user`/
`enable-user`, and `disable-membership`/`enable-membership` round out
provisioning — see `uv run meyar --help`.

## Internal REST API

`/api/v1` is the current internal REST surface for approved internal HR
clients (the `/ui` browser interface and other approved internal systems).
It is Bearer-API-key authenticated and fully independent of the `/ui`
BrowserSession/CSRF cookie mechanism — never authenticate a REST client
with a UI session cookie, and never authenticate the UI with an API key
header. `/ui/*` routes are deliberately excluded from the OpenAPI schema
(`include_in_schema=False`); the schema below describes the product REST
API only.

This surface covers the deterministic search/scoring/ranking workflow
end-to-end. Full API-first parity for every step of the agent's
conversational workflow (JD drafting, review, confirmation) is tracked
separately and not yet complete — see issue #45.

### Creating an API key

API keys are provisioned administratively via the CLI — there is no HTTP
endpoint that issues keys (an unauthenticated key-issuance endpoint would
defeat the auth boundary):

```bash
cd backend
uv run meyar create-tenant --name "Example HR Team"
```

This prints the new tenant's UUID and the plaintext API key **exactly
once**. Store it immediately in a secret manager or local environment
variable — it is never shown again and is stored server-side only as a
salted hash. A new key is seeded with all six current scopes
(`jobs:read`, `jobs:write`, `candidates:read`, `candidates:write`,
`evaluations:read`, `evaluations:write`).

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
| POST | `/api/v1/search` | `candidates:read` | Structured/semantic/hybrid search. |
| POST | `/api/v1/search/natural-language` | `candidates:read` | Natural-language search planning + execution, typed fail-closed outcomes. |
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

`evaluation_as_of_date` is always required and is never defaulted to
"today" — identical inputs (candidate/profile version, criteria version,
date) return the same immutable `evaluation_id` on repeat calls (`reused:
true`) rather than creating a duplicate. `numeric_score` is always a
canonical two-decimal string (e.g. `"75.00"`), never a bare float.

### Local AI dependency map

| Operation | Requires local Ollama? |
|---|---|
| Health, usage, jobs, candidates, candidate detail, documents | No |
| `POST /api/v1/search` (`STRUCTURED_ONLY`) | No |
| `POST /api/v1/search` (`SEMANTIC_ONLY`/`HYBRID`) | Yes — local embedding model only |
| `POST /api/v1/search/natural-language` | Yes — local LLM planner, then local embeddings if the plan needs semantic retrieval |
| Score / batch rank | No — deterministic policy engine only, no LLM call |

No operation ever calls an external/cloud AI, embedding, or telemetry
service — see [Local AI and privacy](#local-ai-and-privacy).

### Common error semantics

`401` missing/invalid/expired/revoked API key. `403` valid key missing a
required scope. `404` resource not found *or* belongs to another tenant
(identical response either way — existence is never leaked cross-tenant).
`422` malformed/invalid request body or an unsupported domain state (e.g. a
candidate with no completed profile). `503` local AI/database
infrastructure genuinely unavailable. Error bodies never include a
traceback, SQL text, a filesystem path, or API-key/tenant internals.

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
pull request to `main`, the stable integration branch. CI must pass, then
the owner reviews and uses **Squash and merge**. Agents do not
automatically merge normal development PRs. After the owner reports a
merge, the agent verifies the remote state and synchronizes local `main`
before creating another task branch. Do not push directly to `main`.

Dependabot checks backend `uv` and GitHub Actions dependencies weekly.
Routine minor/patch version updates are grouped to reduce noise; major
updates remain separate. Every dependency PR must pass CI and receive
owner review. Auto-merge is intentionally disabled. Dependency changes
must include the updated `backend/uv.lock` when applicable.

## Current phase and roadmap

**Active: Agentless Mac Deployment Readiness (issue #35).** Goal: MEYAR is
deployable and operable on the target host by operations staff with no
Claude Code/Codex/AI-coding-agent dependency of any kind:

```text
provision -> configure -> PostgreSQL/pgvector -> migrations
  -> Ollama/model setup -> service lifecycle -> healthcheck
  -> synthetic smoke -> backup/restore -> update/rollback
  -> support/log collection
```

Final deployment must be agentless: Claude Code/Codex availability on the
bank Mac is not assumed, and no AI agent edits live production code there.
A problem discovered on the bank Mac is diagnosed from logs/a support
bundle, fixed and tested on a development machine, then released and
redeployed — never patched in place by an agent on the production host.

**Next: Real Target-Mac Model Selection & Benchmark (issue #36).** Executes
the benchmark suite on the actual confirmed reference hardware — Mac mini
M4 Pro, 12-core CPU, 16-core GPU, 24 GB unified memory, 512 GB SSD — to make
the final production LLM/embedding model decision. This benchmark **has not
been executed yet**; nothing in this README should be read as claiming it
has.

**Other open tracked work:** issue #45 (API-first parity for the full agent
workflow) and issue #46 (pre-deployment runtime/ingestion/recovery
hardening) — see `docs/STATUS.md` for detail.

### Post-presentation capability backlog (planned, not implemented)

Two further capabilities are scoped but **not implemented**:

- **Issue #49 — server-owned search result context and conversational
  follow-ups.** Lets HR refer back to a previous result set ("bunlardan",
  "ikinci namizəd", "ilk üçü"). Result membership and ordering must remain
  server-owned — the LLM interprets that HR *means* the prior result set,
  but never guesses who was in it or which candidate was "second."
- **Issue #50 — evidence-backed candidate Q&A and deterministic
  comparison.** Depends on #49. The intended architecture is a
  **structured / evidence-aware RAG**, explicitly not "raw CV chunks →
  vector search → LLM → trust answer":

  ```text
  authorized candidate reference (via #49's result set)
    -> structured CandidateProfile facts
    -> accepted evidence / relevant canonical CV spans
    -> deterministic calculations where needed (e.g. duration)
    -> local LLM explanation
    -> evidence-backed answer
  ```

  No LLM hiring winner, no LLM-authored numeric hiring score, and
  `UNKNOWN` whenever evidence is insufficient — same invariants as every
  other MEYAR AI capability today.

## Known current limitations

- The production LLM/embedding model is not selected — that decision is
  blocked on the real target-hardware benchmark (issue #36), which has not
  yet run.
- Agentless deployment tooling (issue #35) is in progress; some
  operational procedures (provisioning, rollback, diagnostics) remain
  runbook prose rather than executable automation — see
  `docs/DEPLOYMENT_AND_OPERATIONS.md`.
- No formal penetration test has been performed, and no security
  validation has occurred on target deployment hardware.
- OCR fallback for scanned PDFs is not implemented — digital PDFs only.
- No rate limiting is enforced yet (`MEYAR_RATE_LIMIT_PER_MINUTE` exists in
  config but is not yet wired into request handling).
- No CORS policy is configured; the current same-origin UI + internal API
  deployment does not require one.
- Application-layer encryption at rest is not implemented; real production
  candidate data requires the approved protected storage environment.
- Full API-first parity for every step of the agent's conversational
  workflow (JD drafting, review, confirmation) is tracked separately and
  incomplete — see issue #45.
- Additional pre-deployment runtime/ingestion/recovery hardening is tracked
  in issue #46.
- Conversational follow-up on a prior search result and evidence-backed
  candidate Q&A/comparison are planned, not implemented — see "Post-
  presentation capability backlog" above (issues #49, #50).

See [`docs/STATUS.md`](docs/STATUS.md) for the complete current gap matrix
and the latest next action.
