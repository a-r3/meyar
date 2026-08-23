# MEYAR

MEYAR is an internal AI Candidate Intelligence & CV Search
Platform. It is intended to help authorized HR staff ingest candidate
documents, extract professional facts locally, search the candidate library,
and evaluate candidates against job requirements with auditable evidence.
MEYAR is internal HR tooling, not an external B2B/SaaS product or a public
candidate-facing service.

The repository contains the working backend foundation through deterministic
JD scoring and batch ranking, the strict local-LLM natural-language planner,
deterministic hybrid search, and the first server-rendered internal HR browser
interface. The finalized REST/OpenAPI surface remains Slice 12 work and must
not be treated as complete.

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
  an internal service and CLI rather than a finalized evaluation HTTP API;
- internal health, job, and candidate API routes;
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
  filter can never be overridden by semantic similarity — service + CLI
  only, no REST endpoint yet;
- natural-language candidate search planning (`meyar plan-search`): a
  loopback-only local LLM produces a strict `PlannerDraft`; deterministic
  `meyar-search-planner-v1` validation derives the search mode, rejects
  protected or unsupported meaning instead of weakening it, injects trusted
  tenant-independent runtime provenance/date/weights, and produces the exact
  `CandidateSearchRequest` Slice 8 executes. The planner never reads or ranks
  candidates. Its explicit Azerbaijani MVP morphology policy preserves common
  mandatory forms and protected-term inflections without broad prefix
  matching; plan-only and thin plan→search service flows are both tested;
- deterministic JD scoring (`meyar evaluate --as-of-date YYYY-MM-DD`):
  `meyar-policy-v1` criterion/fit evaluation plus `meyar-score-v1` Decimal
  0–100 scoring, exact structured explanation, explicit date provenance, and
  idempotent reuse of the immutable Evaluation for exact repeated inputs;
- deterministic candidate-library ranking (`meyar rank-job`): exactly one
  current completed professional profile per active tenant candidate, explicit
  fit tiers before numeric score, and candidate UUID as the stable non-PII
  tie-break. It has no semantic-search, embedding, LLM, or identity dependency;
- internal browser UI (`/ui`): Azerbaijani chat/search, fail-closed typed
  planner outcomes, tenant-scoped CV Library and candidate detail, current
  presentation-only identity, existing-job deterministic ranking, canonical
  score/fit/contribution display, and safe internal error states;
- server-side browser-session bridge: API key is posted once to login and
  exchanged for a fresh eight-hour opaque session cookie; only its SHA-256
  digest is persisted, and every request derives tenant/scopes from the live
  API-key row. Authenticated POSTs are CSRF-protected;
- repository-packaged Jinja2 templates and local CSS with no Node build,
  frontend package manager, remote asset, analytics, or telemetry dependency;
- synthetic-only automated tests and repository governance.

Planned or in progress:

- Slice 11 independent acceptance and owner merge;
- **Next after acceptance: Slice 12** — finalized internal API/Swagger and
  README API examples;
- Slice 13 full security and target-Mac acceptance.

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

Protected API routes require a locally issued API key. The browser login accepts
that key only in the `/ui/login` POST body, validates it through the same API-key
authority, then stores only a session cookie in the browser—never the API key,
identity, query, or result data in JavaScript or browser storage. Do not place
keys in this README, source files, shell history, logs, URLs, or Git.

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

- Structured/semantic/hybrid candidate search and strict natural-language
  planning exist (Slices 7–9, service + CLI plus the Slice 11 HTML UI; no
  finalized REST endpoint yet). The planner intentionally rejects unsupported language proficiency,
  skill-specific duration, identity, salary/location, and custom-weight
  requests rather than weakening their meaning.
- Deterministic 0–100 scoring and batch ranking are service/CLI capabilities
  rendered by the Slice 11 HTML UI; no finalized scoring/ranking REST endpoint
  exists yet.
- OCR fallback for scanned PDFs is not implemented.
- The configured embedding model is a development/integration default
  (`DEV_INTEGRATION_MODEL`), not an approved final production model —
  approval is blocked on the target Mac Mini benchmark.
- Profile/identity extraction and evaluation are service/CLI flows, not
  finalized HTTP endpoints.
- Application-layer encryption at rest is not implemented; real production
  candidate data requires the approved protected storage environment.
- Server-side branch protection is unavailable on the current private
  repository plan; hooks, PRs, CI, and owner review are the accepted fallback.

See [`docs/STATUS.md`](docs/STATUS.md) for the complete current gap matrix and
the latest next action.
