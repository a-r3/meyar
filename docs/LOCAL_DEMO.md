# MEYAR — Local Demo Runbook

Operator/developer runbook for standing up MEYAR on a local workstation with a
realistic, entirely synthetic dataset and inspecting the product UI. Not a
beginner tutorial — see `README.md` for full setup detail and `docs/
DEPLOYMENT_AND_OPERATIONS.md` for the deployment-host runbook. This document
does not replace either.

Chore-level, presentation-readiness work only (issue #25, and the
`scripts/demo-up.sh`/`scripts/demo-down.sh` one-command wrapper below) — it
changes no search/matching/scoring behavior and adds no production code
path. See `meyar.services.demo_seed_service` for the exact seeding
implementation.

Issue #60 adds nine deterministic geometric portrait PNGs to freshly
generated synthetic DOCX source files. `seed-demo` processes them through
the real post-ingestion photo service after the documents commit; photo
bytes are never sent to a model. The existing demo tenant is idempotent:
re-running `seed-demo` there rotates credentials but does not replace
older CV bytes with photo-enabled versions. Use a **fresh isolated
database and storage root** for photo visual review; never reset an
owner's existing demo tenant to preview this feature. See the issue #60
review command in the PR description.

## 0. One command (recommended)

```bash
./scripts/demo-up.sh
```

This wraps the exact manual flow in §1–§8 below: prerequisite checks,
`backend/.env` bootstrap, `uv sync --locked`, `docker compose up -d
postgres` with a bounded health-wait, `alembic upgrade head` (+ a
single-head check), `uv run meyar seed-demo`, a best-effort read-only
Ollama status check, and a loopback-only Uvicorn server — then prints a
concise summary and keeps the server running in the foreground:

```
MEYAR LOCAL DEMO READY

Login:
  http://127.0.0.1:8000/ui/login

Username:
  demo.hr

Temporary password (shown once):
  <fresh password printed by seed-demo>

Candidate Library:
  http://127.0.0.1:8000/ui/library
...
Ollama:
  AVAILABLE
  (or: NOT AVAILABLE — structured/demo screens still work)

Stop:
  Ctrl+C
```

Notes:

- **The password is rotated every run** (`seed-demo` behavior, unchanged —
  see §5) and **shown once**; the wrapper never persists it to a file, and
  never writes it to `backend/.env` or anywhere else on disk.
- The API key printed by `seed-demo` is a separate **machine** credential
  for REST API/Swagger testing — it is not the UI password and is not
  repeated in the wrapper's final summary.
- If the wrapper cannot safely isolate just the human password from
  `seed-demo`'s output (e.g. its format changes unexpectedly), it falls
  back to printing `seed-demo`'s full authoritative credential block
  verbatim rather than guessing — never silently degraded.
- `backend/.env` is created from `backend/.env.example` only if missing;
  an existing `backend/.env` (including any owner customization) is never
  touched.
- Ollama is optional. If unavailable, the wrapper prints a truthful
  warning and continues — every screen listed in §9 below still works.
- **Ctrl+C** stops exactly the Uvicorn process `demo-up.sh` started (no
  broad process kill). Docker/Postgres are deliberately left running —
  data persists between demo sessions.
- If MEYAR is already running and healthy on `127.0.0.1:8000`, the
  wrapper detects and reuses it instead of starting a duplicate. If an
  *unrelated* process holds the port, it fails with a clear message
  rather than guessing or killing anything.
- `./scripts/demo-up.sh --skip-sync` skips `uv sync --locked` for a
  faster repeat run when dependencies haven't changed.
- This is a **local presentation convenience only** — not a production
  deployment path. It never binds beyond loopback and never runs
  `--reload`.

To stop Postgres afterward (non-destructive — no volume deletion, no data
reset):

```bash
./scripts/demo-down.sh
```

If MEYAR itself is still running (e.g. a previous `demo-up.sh` foreground
session you haven't Ctrl+C'd yet), `demo-down.sh` reports that rather than
guessing which process to stop — see §12 for why.

The rest of this document is the manual, step-by-step fallback — useful
for troubleshooting, or when you want to run/inspect an individual step
(e.g. re-running migrations only).

## 1. Prerequisites

Same as `README.md` §Prerequisites: Git, Python 3.12, `uv`, Docker Engine with
the Compose plugin. **Ollama is not required for this runbook.** A live local
Ollama daemon is only needed for the specific screens called out in §9 below.

## 2. Start PostgreSQL

From the repository root:

```bash
docker compose up -d postgres
docker compose ps
```

## 3. Install dependencies and configure

```bash
cd backend
uv sync --locked
cp .env.example .env
```

`backend/.env.example` is the canonical, non-secret settings template — every
active `MEYAR_*` setting the application reads is listed there with a safe
local default. Adjust `backend/.env` (git-ignored) only if a value doesn't
match your local setup (e.g. a non-default Postgres port).

## 4. Apply database migrations

```bash
uv run alembic upgrade head
uv run alembic heads   # expect exactly one head
```

## 5. Seed the synthetic demo dataset

```bash
uv run meyar seed-demo
```

This creates one clearly-marked, isolated tenant (`MEYAR Demo (Synthetic)`)
containing 9 synthetic candidates, their documents, deterministic profile/
identity/embedding data, 2 jobs with criteria, and real evaluations computed
by MEYAR's actual deterministic scoring engine. **No live Ollama connection
is required** — see §9/§10 for exactly what that means.

The command prints the demo tenant id and an API key **shown once** — copy
it now. Safe to re-run: `seed-demo` is idempotent and reuses the same
tenant/data if it already exists. Every run — first or repeat — always
hands you a usable key: a repeat run **rotates** the demo credential
(revokes any previously-active demo API key and mints exactly one fresh
one), since a previous run's plaintext can never be recovered. There is
never more than one active demo API key at a time.

To wipe and regenerate the demo dataset from scratch:

```bash
uv run meyar seed-demo --reset
```

`--reset` deletes only the *positively identified* demo tenant — display
name alone (`MEYAR Demo (Synthetic)`) is never sufficient proof; the tenant
must also carry the bootstrap marker `seed-demo` itself writes when it
first creates it. If an unrelated tenant happens to share the exact same
name (however that happened), `--reset` **refuses outright** rather than
guessing — it exits with a clear error and touches nothing. There is no
generic database-reset command, and no path that accepts an arbitrary
tenant id.

## 6. Start the application

```bash
uv run uvicorn meyar.main:app --reload
```

## 7. Log in

Open `http://127.0.0.1:8000/ui/login` and sign in with the demo **human**
username/password printed by `seed-demo` (Slice 1 — Human Identity & Dual
Access, issue #30): username `demo.hr`, plus the temporary password shown
once under "Human/UI login" in the command's output. The normal `/ui/login`
screen no longer accepts an API key — that machine credential (also printed
by `seed-demo`, under "Machine/API credential") is for REST API/Swagger
testing only, via `Authorization: Bearer <key>`.

## 8. Browser URLs

| Screen | URL |
|---|---|
| Login | `http://127.0.0.1:8000/ui/login` |
| Search / chat home | `http://127.0.0.1:8000/ui` |
| Candidate Library | `http://127.0.0.1:8000/ui/library` |
| Candidate detail | linked from the Library — `http://127.0.0.1:8000/ui/candidates/{id}` |
| CV preview (in-app readable view) | linked from candidate detail — `http://127.0.0.1:8000/ui/candidates/{id}/documents/{document_id}/preview` |
| Jobs | `http://127.0.0.1:8000/ui/jobs` |
| Health (liveness only) | `http://127.0.0.1:8000/api/v1/health` |
| Swagger UI (fully offline) | `http://127.0.0.1:8000/docs` |
| OpenAPI schema | `http://127.0.0.1:8000/openapi.json` |

## 9. Works without Ollama

| Feature | Works now |
|---|---|
| Application startup, health, Swagger | Yes |
| Login | Yes |
| Candidate Library (browse/filter/paginate) | Yes |
| Candidate detail (professional facts, presentation-only identity) | Yes — pre-seeded synthetic data |
| Original CV open | Yes |
| Jobs / criteria view | Yes |
| Structured search (skills/certifications/languages/education/experience filters) | Yes — evaluates the pre-seeded profile data directly |
| Score / evidence explanation (`meyar evaluate`, or via the API) | Yes — pre-seeded evaluations already exist; re-running the deterministic evaluator needs no AI |
| Batch ranking (`meyar rank-job`, or via the API) | Yes |

## 10. Requires a live local Ollama

| Feature | Requires |
|---|---|
| Semantic search | A reachable local `LLMProvider`/`EmbeddingProvider` to embed the query text — the pre-seeded candidate embeddings exist, but a semantic query still needs to be embedded at request time |
| Hybrid search | Same as semantic — the structured half works standalone, the semantic half does not |
| Natural-language search (chat-style query → `SearchPlan`) | A reachable local LLM to interpret the request |
| Extracting/embedding a **new** or **changed** real CV (`meyar reconcile-folder`, `extract-profile`, `extract-identity`, `embed-candidate`) | A reachable local LLM/embedding provider — this is unrelated to the demo dataset, which is pre-seeded with synthetic extraction results and never calls Ollama |

If Ollama is not running, an AI-dependent action fails safely with a
handled, non-crashing error — MEYAR never silently pretends a result came
from a model that didn't run.

## 11. Recommended 8–10 minute technical walkthrough

1. **Login** — `/ui/login`, the demo human username/password.
2. **Candidate Library** — `/ui/library`. Point out the variety: Java/Python/
   AML/DevOps/frontend/business-analyst candidates, all synthetic.
3. **Candidate detail** — open one candidate (e.g. a Java backend profile).
   Show skills/employment/education with evidence, and that identity
   (name/contact) is clearly separated from the professional facts used for
   matching.
4. **Original CV** — from candidate detail, open the original synthetic
   document.
5. **Structured search** — search by a required skill (e.g. `Java`) and show
   the matching candidates only.
6. **Jobs / criteria** — `/ui/jobs`, open "Senior Backend Engineer" or
   "AML / Compliance Specialist" and show the MUST_HAVE/PREFERRED criteria.
7. **Score / evidence** — open a scored candidate for that job and walk
   through the exact per-criterion evidence backing the 0–100 result.
8. **Ranking** — show the job's ranked candidate list, and point out that a
   MUST_HAVE gate always dominates the numeric score (a high-scoring but
   MUST_HAVE-failing candidate never outranks a passing one).
9. **Swagger / API** — `/docs`, show the same capabilities are available as
   an authenticated internal REST API, fully offline (no CDN dependency).
10. **NL/semantic search boundary** — if Ollama is not running locally,
    explain honestly (per §10) that natural-language and semantic search
    require a reachable local model, and that MEYAR never fabricates a
    result when one isn't available — structured search and everything
    above already demonstrate the full deterministic product surface
    without it.

## 12. Cleanup

After a demo/presentation session, rotate both demo credentials so
whatever was visible on screen (or in terminal scrollback) stops working:

```bash
uv run meyar seed-demo
```

This revokes the previously-active demo API key and mints a fresh one, and
sets a fresh temporary password for the demo human login (`demo.hr`) —
copy whichever you'll need again, or just leave them unused since the demo
tenant is isolated and harmless to leave in place.

To remove the demo tenant entirely:

```bash
uv run meyar seed-demo --reset
```

This does not affect any other tenant, does not require a database reset,
and there is no broader "wipe everything" command.
