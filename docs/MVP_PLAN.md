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
| **M6 — Operational CV Intake & Reconciliation** | Slice 14: automatic profile/identity/embedding processing for folder-imported CVs, so a folder-imported candidate becomes searchable without a manual per-candidate command |

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
D-012). Development remote connected
(`https://github.com/a-r3/meyar.git`, private, personal — temporary
until the bank supplies an official repository, full history preserved
on that migration); CI, local Git hooks, and PR template are active. Slice 11's
server-rendered frontend uses the same Python quality gate and adds no Node
toolchain.

### Slice 6 — Local CV Library & Folder Indexer

Scan a configured local folder for PDF/DOCX, hash-based change detection,
ingest new/changed files only (idempotent, safe to re-run), parse through
the existing Slice 3 pipeline, persist indexing state (discovered path,
hash, scan timestamp, parse status, current candidate/document linkage).
Downstream profile extraction (Slice 4), identity extraction (Slice 7),
and embedding (Slice 7) remained a separate, manually-triggered operator
step, same as for direct upload — closed by Slice 14, not by Slice 6.
**DONE** (Slice 6, D-013). GitHub milestone: **M1 — CV Ingestion &
Candidate Library**.

### Slice 7 — Candidate Identity + Local Embeddings / Vector Index

`CandidateIdentity` model (name/contact, presentation-only, never a
scoring input); local embedding provider abstraction; pgvector-backed
professional-profile embeddings with version/provenance. **DONE** (Slice 7,
D-014).

### Slice 8 — Hybrid Candidate Search

Structured filters + vector semantic retrieval → deterministic relevance
combination → ranked results with explanations. **DONE** (Slice 8, D-015).

### Slice 9 — Natural-Language Search Planner

Chat-style request → LLM-produced, schema-validated `SearchPlan` → search
execution → requested candidate count when enough matches exist. **DONE**
(Slice 9, D-016).

### Slice 10 — JD 0–100 Scoring + Batch Ranking

Deterministic `meyar-score-v1` Decimal score layered on the existing
criterion/fit engine, explicit as-of provenance, immutable idempotent
Evaluation persistence, and one JD → tenant candidate library → fit-tier-first
ranked list with reasons. **DONE and merged** (D-017, issue #14, PR #15).

### Slice 11 — Internal Chat UI + CV Library UI

Server-rendered chat/search, candidate results, CV Library, candidate detail,
existing-job JD matching results, and the API-key browser-session bridge.
Implemented on `feat/internal-chat-cv-library-ui`, pending independent
acceptance and owner merge (D-018, issue #16). Arbitrary raw-CV delivery and
final REST/OpenAPI work remain outside this slice.

### Slice 12 — REST API / Swagger / README Completion

Finalize the internal API surface for the above capabilities; OpenAPI/
Swagger docs; README usage examples. Not started.

### Slice 13 — Security + Official Definition-of-Done Acceptance

Full acceptance pass against the official DoD matrix (`docs/STATUS.md`),
including bad-file testing, scoring consistency, and an
external-network/exfiltration verification pass. Not started.

### Slice 14 — CV Folder Import & Continuous Ingestion

Closes the operational/product gap left after Slice 6: a folder-imported
`CandidateDocument` never automatically continued through profile
extraction, identity extraction, or embedding, so a folder-imported
candidate was not searchable without a separate manual per-candidate
command. Reuses the Slice 6 scanner/indexer unchanged; adds a
reconciliation orchestration layer (`meyar.services
.folder_reconciliation_service`), a file-stability window, tenant-scoped
exact-content dedup, and one CLI command
(`meyar reconcile-folder --tenant-id --root [--limit N]`) serving both
initial bulk import and repeatable reconciliation. No new database
migration (readiness is derived from existing Slice 4/7 provenance) and no
new runtime dependency (periodic reconciliation, not a filesystem
watcher). **DONE** — merged as PR #24 at squash SHA `f6e31ff`, closes
issue #23 (D-021). GitHub milestone: **M6 — Operational CV Intake &
Reconciliation**.

## Deferred (still explicitly out of scope)

Billing/payments, complex commercial plans, ATS connectors, email/Drive
ingestion, cloud LLM, autonomous rejection, interview bot, fine-tuning,
mobile app, Kubernetes, microservices, advanced dashboards, complex RBAC,
enterprise SSO, webhook delivery (interface left open, not implemented),
a `develop` branch (unless bank policy explicitly requires one).

Note: RAG/vector search and numeric JD scoring were previously deferred
here — both are now **required** (D-011) and appear as Slices 7–10
above, not in this deferred list.

## Roadmap after product-direction pivot: bounded local-AI agent (D-030/D-031/D-032)

Following completion of the official-task MVP roadmap above (through Slice
14 / M6, plus the in-progress M7 HR UI productization), MEYAR's roadmap
continues with the bounded local-AI agent direction recorded in
`docs/DECISIONS.md` D-030/D-031/D-032. This does not reorganize, rename, or
close any existing milestone (M0–M7 unchanged; M5/#20 explicitly stays open
until its Target-Mac gate is executed — see Slice 7 below).

### M8 — Bounded Local-AI HR Agent Platform

- **Slice 1 — Human Identity & Dual Access** (#30): real human user/session
  → tenant membership → role, alongside the unchanged API-key machine path.
- **Slice 2 — Read-Only Local AI Agent Foundation** (#31): local Ollama
  agent orchestration, typed tool dispatch over existing deterministic
  services, conversation/session state, evidence-grounded responses,
  prompt-injection boundaries, Ollama concurrency control, primary "MEYAR
  AI" workspace. No mutating actions.
- **Slice 3 — Evidence Capability Completion** (#32): close provable
  evidence-model gaps (skill-specific duration, skill↔employment grounding,
  sector/domain experience, recency) — missing evidence stays UNKNOWN,
  never fabricated.
- **Slice 4 — Agent Product UX & JD Matching** (#33): MEYAR AI becomes the
  primary surface; conversational search/refinement, comparison, evidence
  explanations, JD-to-draft-criteria, human review, deterministic ranking;
  de-emphasizes classic search/Vacancies navigation once accepted; evaluates
  and, if accepted, acts on the D-031 fast-path sunset condition.
- **Slice 5 — Confirmed Actions Framework** (#34): propose → validated
  pending action → human confirmation (gated on Slice 1 identity) → typed
  tool execution → audit. No silent mutations.

### M9 — Deployment, Benchmark & Integration Readiness

- **Slice 6 — Agentless Mac Deployment Readiness** (#35): tested,
  executable (not just documented) provisioning/config/migration/Ollama
  setup/startup/healthcheck/backup/restore/update/rollback/diagnostics, no
  Claude Code/Codex dependency on the bank Mac.
- **Slice 7 — Real Target-Mac Model Selection & Benchmark** (#36): executes
  the benchmark on real bank Mac-mini hardware, extending issue #20/M5's
  scope to agent workloads (concurrency, multi-user, agent understanding).
  **#20/M5 remain open and are not superseded or closed by this slice.**
- **Slice 8 — Bank Integrations** (#37): conditional tracking only (SSO,
  calendar/interview scheduling, ATS) — not implemented until the bank's
  actual environment is known; consistent with the existing "Deferred"
  list above (complex RBAC, enterprise SSO remain deferred).

Implementation of these slices is explicitly **not** authorized by this
roadmap entry alone — each proceeds through the normal branch → quality
gate → PR → review workflow when separately started.

## Current status

See `STATUS.md`.
