# MEYAR

MEYAR is an internal AI Candidate Intelligence & CV Search
Platform. It is intended to help authorized HR staff ingest candidate
documents, extract professional facts locally, search the candidate library,
and evaluate candidates against job requirements with auditable evidence.
MEYAR is internal HR tooling, not an external B2B/SaaS product or a public
candidate-facing service.

The repository currently contains a working backend foundation through the
deterministic evaluation engine. Folder indexing, semantic search, numeric
scoring, batch ranking, and the internal user interface are planned work and
must not be treated as implemented.

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
- synthetic-only automated tests and repository governance.

Planned or in progress:

- **Next: Slice 6 — Local CV Library & Folder Indexer** (not started):
  incremental hash-based scanning and indexing of a configured local folder;
- `CandidateIdentity`, local embeddings, and pgvector-backed storage;
- structured/semantic hybrid search and a validated natural-language search
  planner;
- deterministic 0–100 JD scoring and batch candidate ranking;
- internal Chat/Search and CV Library user interfaces;
- finalized internal API/Swagger examples and full security acceptance.

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
- SQLAlchemy 2.0 async, Alembic, PostgreSQL 16
- `pypdf` and `python-docx` for current document parsing
- Ollama through `meyar.llm.LLMProvider`
- Ruff, mypy, pytest, pytest-asyncio
- pgvector and a local embedding provider are planned, not installed yet

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

Run the API:

```bash
uv run uvicorn meyar.main:app --reload
```

Local endpoints:

- Health: `http://127.0.0.1:8000/api/v1/health`
- Swagger UI: `http://127.0.0.1:8000/docs`
- OpenAPI JSON: `http://127.0.0.1:8000/openapi.json`

Protected routes require a locally issued API key. Do not place keys in this
README, source files, shell history, logs, or Git.

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

- Slice 6 folder indexing has not started.
- No local embeddings, pgvector search, hybrid search, or natural-language
  search planner exists yet.
- No deterministic 0–100 score, batch ranking, Chat UI, or CV Library UI
  exists yet.
- OCR fallback for scanned PDFs is not implemented.
- `CandidateIdentity` is not implemented.
- Profile extraction and evaluation are service/CLI flows, not finalized HTTP
  endpoints.
- Application-layer encryption at rest is not implemented; real production
  candidate data requires the approved protected storage environment.
- Server-side branch protection is unavailable on the current private
  repository plan; hooks, PRs, CI, and owner review are the accepted fallback.

See [`docs/STATUS.md`](docs/STATUS.md) for the complete current gap matrix and
the latest next action.
