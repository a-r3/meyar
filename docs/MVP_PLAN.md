# MEYAR — MVP Plan

Canonical product direction: `docs/PROJECT_VISION.md`. This document
sequences the implementation slices.

## Completed historical slices (pre-re-baseline)

These slices were planned and implemented under the original framing and
remain valid — see D-011 (`docs/DECISIONS.md`): the re-baseline changed
product direction and roadmap, not this completed work.

| # | Slice | Proves | Depends on | Status |
|---|-------|--------|------------|--------|
| 0 | Bootstrap | preflight, docs, harness, repo scaffold | - | DONE |
| 1 | Tenant + API Auth | API key → authenticate → tenant context → protected endpoint | 0 | DONE (`f5b4ec3`) |
| 2 | Job Criteria | tenant → create job → versioned criteria | 1 | DONE (`9ef3273`) |
| 3 | Candidate Upload | tenant → upload CV → secure storage → parsed canonical doc | 1 | DONE (`56aca03`) |
| 4 | Local AI Profile Extraction | canonical CV → local model → validated structured profile + evidence | 3 | DONE (`c14a7ac`) |
| 5 | Evaluation Engine | candidate + criteria → deterministic criterion analysis → policy engine → immutable stored result | 2, 4 | DONE (`9d71cb6`) |

Acceptance criteria (still binding for regressions):

- **1**: cross-tenant access returns 404/403 (never leaks existence);
  revoked/expired keys rejected; unauthenticated request to a protected
  route rejected; API key secret never persisted or logged in plaintext.
- **2**: criteria versions immutable once created; job always resolves to
  exactly one current criteria version.
- **3**: rejects oversized/malformed/MIME-mismatched files; stores under
  opaque id; original filename never used as a path.
- **4**: LLM output that fails schema validation never reaches
  `CandidateProfile`; sensitive attributes never extracted into the profile.
- **5**: policy engine has LLM-independent unit tests; `UNKNOWN` never
  silently becomes `NOT_MATCHED`; evaluations are immutable/versioned;
  cross-tenant evaluation input resolution is rejected.

## SUPERSEDED — original planned Slice 6/7

The original plan's **Slice 6 — External API Flow** (a scripted external
customer completing the full flow) and **Slice 7 — Security Gate** (a
distinct "external customer" security milestone) assumed an
external/commercial B2B product. This framing is **SUPERSEDED by D-011**
— MEYAR is an internal platform, so there is no external client flow to
script. Tenant-isolation, PII, and prompt-injection testing remain
mandatory (`.claude/rules/testing.md`) but are folded into each new
slice's own acceptance criteria plus the new **Slice 13** below, rather
than a separate "external customer" milestone.

## Roadmap after official-task re-baseline

### Canonical GitHub milestones

No due dates are assigned until the official project timeline is supplied.

| Milestone | Canonical scope |
|---|---|
| **M0 — Project Foundation & Governance** | Requirement re-baseline; GitHub/CI governance; Claude/Codex instructions; README, Dependabot, and repository guards |
| **M1 — CV Ingestion & Candidate Library** | Slice 6 |
| **M2 — Candidate Search Intelligence** | Slices 7–9: `CandidateIdentity`, local embeddings/pgvector, hybrid search, natural-language `SearchPlan` |
| **M3 — JD Matching & Ranking** | Slice 10: deterministic 0–100 score, batch ranking, explanations |
| **M4 — Internal Product Interface & API** | Slices 11–12: Chat UI, CV Library UI, internal REST API, Swagger/README completion |
| **M5 — Security, Target-Mac Validation & MVP Acceptance** | Slice 13, target Mac Mini model benchmark, external-network/data-exfiltration verification, privacy/security acceptance, official Definition of Done |

Material product work uses the applicable approved milestone and, when useful,
one issue per coherent deliverable. Avoid micro-issues for tiny tests or edits.
The normal trace is milestone → issue → task branch → implementation/tests →
PR/CI → owner Squash and merge → issue close → milestone progress update.

### R0 — Requirement / Documentation Re-baseline

This pass: audited and updated all product docs and Claude instructions
against `AI-PROJ-CV-01` + owner clarifications (D-011). No feature code
changed.

### Git Infrastructure

`main` ← PR ← task branch (`feat/*`, `fix/*`, `chore/*`, `docs/*`,
`test/*`), with `ruff` + `mypy src` + `pytest` as a merge gate (see
D-012). **In progress:** development remote connected
(`https://github.com/a-r3/meyar.git`, private, personal — temporary
until the bank supplies an official repository, full history preserved
on that migration); CI, local Git hooks, and PR template landing via
`chore/git-governance`. Frontend checks join the gate once a frontend
exists (Slice 11).

### Slice 6 — Local CV Library & Folder Indexer

Scan a configured local folder for PDF/DOCX, hash-based change detection,
ingest new/changed files only (idempotent, safe to re-run), parse/extract
through the existing Slice 3/4 pipeline, persist indexing state
(discovered path, hash, scan timestamp, parse/extraction status, current
candidate/profile linkage). **Not started.**
GitHub milestone: **M1 — CV Ingestion & Candidate Library**.

### Slice 7 — Candidate Identity + Local Embeddings / Vector Index

`CandidateIdentity` model (name/contact, presentation-only, never a
scoring input); local embedding provider abstraction; pgvector-backed
professional-profile embeddings with version/provenance. Not started.

### Slice 8 — Hybrid Candidate Search

Structured filters + vector semantic retrieval → deterministic relevance
combination → ranked results with explanations. Not started.

### Slice 9 — Natural-Language Search Planner

Chat-style request → LLM-produced, schema-validated `SearchPlan` → search
execution → requested candidate count when enough matches exist. Not
started.

### Slice 10 — JD 0–100 Scoring + Batch Ranking

Deterministic numeric score (formula TBD) layered on the existing
criterion/policy engine (D-010); one JD → many candidates → sorted
ranked list with reasons. Not started.

### Slice 11 — Internal Chat UI + CV Library UI

Chat/search, candidate results, CV Library, candidate detail/original CV
access, JD matching results, minimal admin/status visibility. Not
started.

### Slice 12 — REST API / Swagger / README Completion

Finalize the internal API surface for the above capabilities; OpenAPI/
Swagger docs; README usage examples. Not started.

### Slice 13 — Security + Official Definition-of-Done Acceptance

Full acceptance pass against the official DoD matrix (`docs/STATUS.md`),
including bad-file testing, scoring consistency, and an
external-network/exfiltration verification pass. Not started.

## Deferred (still explicitly out of scope)

Billing/payments, complex commercial plans, ATS connectors, email/Drive
ingestion, cloud LLM, autonomous rejection, interview bot, fine-tuning,
mobile app, Kubernetes, microservices, advanced dashboards, complex RBAC,
enterprise SSO, webhook delivery (interface left open, not implemented),
a `develop` branch (unless bank policy explicitly requires one).

Note: RAG/vector search and numeric JD scoring were previously deferred
here — both are now **required** (D-011) and appear as Slices 7–10
above, not in this deferred list.

## Current status

See `STATUS.md`.
