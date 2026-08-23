# MEYAR — Status

## Current phase
**Slice 6 — Local CV Library & Folder Indexer.** Governance PR #1
(`chore/git-governance`) merged into `main` at `16929fd`; milestone
**M0 — Project Foundation & Governance is CLOSED** (issue #2 closed,
zero open M0 work). Slice 6 is implemented on `feat/cv-folder-indexing`
and its PR is open for review — **not yet merged.**

GitHub remote established (`https://github.com/a-r3/meyar.git`, private,
temporary development remote — see D-012, `docs/DECISIONS.md`). `main`
bootstrap-pushed at `f8ac183`, then governance-merged at `16929fd`. The
same governance PR included the required root onboarding README and
weekly low-noise Dependabot configuration for backend `uv` and
GitHub Actions dependencies; dependency auto-merge remains disabled.

## Completed
- Preflight, fast docs pass, Claude Code harness.
- Backend scaffold: FastAPI + SQLAlchemy 2.0 async + Alembic + PostgreSQL.
- Slice 1 (Tenant + API Auth) — committed as `f5b4ec3`.
- Slice 2 (Job Criteria) — committed as `9ef3273`.
- Slice 3 (Candidate Upload) — committed as `56aca03`.
- Slice 4 (Profile Extraction) — committed as `c14a7ac`.
- Slice 5 (Evaluation Engine) — committed as `9d71cb6`. Fully
  deterministic, no LLM.
  `meyar.evaluation.evaluators` implements SKILL/CERTIFICATION/EDUCATION/
  LANGUAGE/EXPERIENCE per-criterion policy (exact normalized-string match
  + a small curated alias table — no fuzzy/embedding matching; deterministic
  year-extraction + overlap detection for EXPERIENCE, never LLM date math).
  `meyar.evaluation.policy` computes the overall fit band
  (`meyar-policy-v1` — see D-010 for the exact binding algorithm).
  Immutable `Evaluation` model (never updated in place — a new profile
  version, criteria version, or policy version always produces a new
  row) with exact provenance (candidate_profile_version_id,
  job_criteria_version_id, policy_engine_version). Tenant isolation
  enforced at the resolution step: `get_profile_version_by_id`/
  `get_criteria_version_by_id` are tenant-scoped lookups, so a
  cross-tenant id simply fails to resolve — `EvaluationInputError` before
  any row is created. Internal service only (`meyar.evaluation.service.
  evaluate_candidate`) + CLI (`meyar evaluate`) for Slice 5, per the
  "don't freeze an API contract Slice 6 will replace" guidance — no
  `POST /v1/evaluations` endpoint yet. `CriterionIn` gained an additive,
  optional `required_level` field for LANGUAGE proficiency requirements
  (backward-compatible — old stored criteria rows just default to
  `None`).
- Governance (M0) — `chore/git-governance` merged as `16929fd`.
  CI/hooks/PR-template/onboarding-README/Dependabot landed; organization
  branding removed from tracked docs; startup-protocol path checks made
  environment-neutral (`git rev-parse --show-toplevel`, no hard-coded
  absolute path). Issue #2 closed, milestone M0 closed.
- Slice 6 (Local CV Library & Folder Indexer) — on `feat/cv-folder-indexing`,
  associated with **M1 — CV Ingestion & Candidate Library**, closes
  issue #6. New `FolderSource`/`FolderIndexedFile` models (Alembic
  migration `bd1b929cd874`), a symlink-safe recursive scanner
  (`meyar.ingestion.folder_scanner`), and an orchestration service
  (`meyar.services.folder_indexer_service.index_folder`) that reuses the
  existing secure ingestion pipeline unchanged — that pipeline itself was
  extracted into `meyar.services.candidate_document_service
  .ingest_candidate_document` so the direct-upload API route and the
  folder indexer share exactly one code path (no parallel ingestion
  architecture). SHA-256 content hash (never mtime) drives
  NEW/CHANGED/UNCHANGED/retry classification; re-scanning an unchanged
  folder creates zero duplicate Candidate/CandidateDocument/index rows
  (dedicated regression test); a changed file creates a new immutable
  CandidateDocument version under the same Candidate identity; a removed
  file is tombstoned (`MISSING`), never hard-deleted; a malformed file
  never aborts the rest of a scan; a previously FAILED file is retried on
  the next scan when unchanged. New CLI command `meyar index-folder
  --tenant-id --root` (distinct exit codes: 0 clean, 1 completed-with-
  failures, 2 invalid source, 3 infra/DB failure), PII-safe output
  (counts/ids only). See D-013, `docs/DECISIONS.md`, for the exact
  removed/changed/retry/parse-failure semantics chosen.

## Tests
127/127 passing (105 prior + 22 new, Slice 6). Deterministic policy unit tests
(no DB, no LLM — `test_evaluation_policy.py`): skill match/absent-is-
unknown/case-normalization/Java-never-equals-JavaScript/alias
normalization, certification match/absent, education match/unsupported,
language explicit-level-match/present-without-level-is-partial/absent/
no-level-required, experience sufficient/insufficient/ambiguous-dates-
require-review/no-history-is-unknown/overlapping-dates-conflict/correct-
summation, configured manual-review flag forces review even on MATCH,
evidence carried through unmodified, policy deterministic on repeated
identical input, overall-result algorithm (all 4 bands + manual-review/
conflict precedence + preferred-ratio threshold), no hidden criteria.
DB-integration tests (`test_evaluation_engine.py`): exact version
provenance persisted, immutability (new profile version → new
evaluation, old one unchanged), new criteria version → new evaluation,
cross-tenant candidate+job mixing rejected (`CRITERIA_VERSION_NOT_FOUND`),
tenant B cannot retrieve tenant A's evaluation, wrong-tenant profile id
rejected even though it exists, profile belonging to a different
candidate rejected, criterion-result count matches configured criteria
exactly (no hidden criteria), repeated identical evaluation
deterministic, no identity/protected fields reach the policy engine.
`uv run ruff check .` and `uv run mypy src` are clean. Whole-repository
`uv run mypy .` retains known test-only type debt.

Slice 6 (`test_folder_indexer.py`, `test_folder_indexer_cli.py`, 22
tests): empty folder, valid PDF/DOCX discovery+import, nested-folder
relative-path normalization, unsupported extension ignored, case-
insensitive extension, unchanged-rescan idempotency (zero duplicate
Candidate/CandidateDocument/index rows), changed-file new-version-same-
candidate with prior evidence preserved, removed-file MISSING then
unchanged-reappearance reactivation, malformed-PDF (parse-stage) and
malformed-DOCX (validation-stage) isolation without aborting the scan,
retry-of-previously-FAILED-file then fixed, SHA-256 correctness, source-
root-escape/symlink prevention (file and directory symlinks), invalid/
non-directory source root, cross-tenant isolation (same folder path
scanned by two tenants never shares rows), PII-safe audit metadata, and
CLI happy-path/invalid-root/exit-code-on-failure with no filename in
output.

## Live synthetic smoke
**PASS.** Per Slice 5 spec §25, no live Ollama call required (Slice 4
already verified that integration) — used a synthetically constructed
CandidateProfileVersion (bypassing the LLM entirely) against a 3-criterion
JobCriteriaVersion (2 MUST_HAVE: Python skill + 3yr min experience; 1
PREFERRED: AWS certification). Result: both MUST_HAVE criteria MATCH
(skill found; experience computed as 4 years from "2021-2025",
correctly exceeding the 3-year requirement) with evidence correctly
carried through from the profile; PREFERRED certification correctly
UNKNOWN (absent, not penalized as failure); overall result
`POTENTIAL_MATCH` (all must-haves satisfied, 0% preferred coverage <
50% threshold) — exactly matching the documented D-010 algorithm.
Evaluation's persisted `candidate_profile_version_id`/
`job_criteria_version_id` verified to equal the exact input versions.

## In progress
Slice 6 PR (`feat/cv-folder-indexing` → `main`, closes issue #6) is open
and awaiting owner review/merge. Governance PR #1 merged (`16929fd`);
M0 closed.

## Blockers
None blocking. Same open items as before (D-001 Mac benchmark pending,
Auto Mode script dry-run only, document encryption-at-rest deferred,
D-009 Ollama upgrade needs root). Git remote is connected but is a
personal/temporary one (D-012) — official bank-owned remote still
pending, migration keeps full history when it arrives.

## Next action
1. **Git Infrastructure** — remote connected (`a-r3/meyar`, private,
   temporary — D-012); governance merged (`16929fd`). May later migrate
   to an official bank-owned remote (history preserved).
2. **Slice 6 — Local CV Library & Folder Indexer** (see
   `docs/MVP_PLAN.md`, D-013), associated with **M1 — CV Ingestion &
   Candidate Library**: implemented on `feat/cv-folder-indexing`, PR
   open, **awaiting owner review/merge** — not yet started: Slice 7
   (CandidateIdentity, local embeddings/pgvector).

The previously planned "Slice 6 — External Async Evaluation API" is
CANCELLED (superseded by D-011) — it is not what "Slice 6" now refers to.

## GitHub milestone status

The detailed canonical mapping is in `docs/MVP_PLAN.md`. No milestone has a
due date because the official timeline has not been supplied.

| Milestone | Slice mapping | Current status |
|---|---|---|
| M0 — Project Foundation & Governance | R0 + Git Infrastructure | CLOSED — merged `16929fd`, issue #2 closed |
| M1 — CV Ingestion & Candidate Library | Slice 6 | IN REVIEW — issue #6 / PR open on `feat/cv-folder-indexing` |
| M2 — Candidate Search Intelligence | Slices 7–9 | NOT STARTED |
| M3 — JD Matching & Ranking | Slice 10 | NOT STARTED |
| M4 — Internal Product Interface & API | Slices 11–12 | NOT STARTED |
| M5 — Security, Target-Mac Validation & MVP Acceptance | Slice 13 + target-Mac benchmark | NOT STARTED |

## Official requirement gap matrix

Against `AI-PROJ-CV-01` + owner clarifications. DONE = implemented and
tested; PARTIAL = foundation exists, capability incomplete; NOT STARTED =
no code yet.

| Requirement | Status | Evidence | Remaining work | Slice |
|---|---|---|---|---|
| Mac Mini / model benchmark | NOT STARTED | D-001 dev-machine substitute only | Benchmark on real Apple Silicon hardware | — (blocked on hardware) |
| PDF parsing | DONE | `pypdf`-based `LocalTextParser` (Slice 3) | — | 3 |
| DOCX parsing | DONE | `python-docx`-based parsing (Slice 3) | — | 3 |
| Scanned PDF detection / OCR | NOT STARTED | Digital-PDF-only parsing (D-007) | Local OCR fallback | future |
| Multilingual CV tests | NOT STARTED | No tracked Azerbaijani/Cyrillic synthetic CV tests currently prove this requirement | Add genuine AZ/RU/EN synthetic fixtures and extraction tests | future |
| Structured extraction | DONE | `CandidateProfileExtraction` schema, Slice 4 | Add `CandidateIdentity` fields | 7 |
| Strict JSON validation | DONE | Pydantic v2, `extra="forbid"`, bounded retry (Slice 4) | — | 4 |
| Uncertainty handling | DONE | `UNKNOWN` never auto-downgraded (D-010), Slice 5 | — | 5 |
| Candidate DB | DONE | `Candidate`, `CandidateDocument`, `CandidateProfileVersion` | Add `CandidateIdentity` | 7 |
| Original file reference | DONE | Opaque storage id + `DocumentStorage` abstraction (Slice 3) | Authorized UI access to original CV | 11 |
| Local CV folder migration/indexing | DONE | Symlink-safe recursive scanner, SHA-256 content-hash incremental/idempotent indexing, existing ingestion pipeline reused, tombstone-not-delete on removal (Slice 6, D-013) | — | 6 |
| Local embeddings / vector storage | NOT STARTED | — | Local embedding provider + pgvector | 7 |
| Access control | DONE | API-key auth, scopes, tenant isolation (Slice 1) | Extend scopes as new endpoints ship | ongoing |
| JD matching | DONE | Deterministic per-criterion evaluation (Slice 5) | — | 5 |
| 0–100 scoring | NOT STARTED | Fit-band algorithm exists (D-010), no numeric score | Numeric formula on top of existing engine | 10 |
| Batch scoring / ranking | NOT STARTED | Evaluation engine is per-candidate today | Batch-ranking endpoint reusing engine | 10 |
| Structured search | NOT STARTED | — | Filter-based search over `CandidateProfile` | 8 |
| Semantic search | NOT STARTED | — | Local embeddings + pgvector retrieval | 7, 8 |
| Explanations | DONE (for evaluation) | Evidence carried through every criterion result (Slice 5) | Extend to search results | 8 |
| REST API | PARTIAL | Jobs/candidates/health routes live; extraction+evaluation are service+CLI only | Add internal HTTP routes as UI needs them | 11, 12 |
| Swagger / OpenAPI | PARTIAL | FastAPI auto-generates it; not yet reviewed/finalized as a deliverable | Review + README examples | 12 |
| Auth | DONE | API-key + scopes (Slice 1) | — | 1 |
| README examples | NOT STARTED | — | Usage examples once API surface stabilizes | 12 |
| Bad-file testing | DONE | Oversized/malformed/MIME-mismatch tests (Slice 3); malformed-PDF/DOCX isolation + path-traversal/symlink tests for the folder indexer (Slice 6) | — | 6, 13 |
| Scoring consistency | PARTIAL | Policy engine deterministic + unit tested (Slice 5) | Re-verify once numeric score lands | 10, 13 |
| External-network/exfiltration verification | NOT STARTED | Local-only enforced by construction (`OllamaLLMProvider` loopback check) | Explicit verification pass | 13 |
| Data-protection / backup description | PARTIAL | Retention/deletion documented (SECURITY_PRIVACY.md); no backup policy written | Document backup approach | 13 |
| Git branch / PR workflow | PARTIAL | Remote connected (`a-r3/meyar`, private), CI + hooks + PR template merged (`16929fd`, D-012) | Migrate to official bank remote when supplied | Git Infrastructure |

**Official numbered task matrix — 28 items.** Summary: 13 DONE, 5 PARTIAL,
10 NOT STARTED (28 items). Multilingual AZ/RU/EN CV fixtures and extraction
tests remain future work; no current evidence is claimed. Highest-priority
gap: local embeddings/semantic search (Slice 7–8), then 0–100 numeric
scoring (Slice 10). Git/PR infrastructure is PARTIAL only because the
remote is still a personal/temporary one, not blocking.
