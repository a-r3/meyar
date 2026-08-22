# MEYAR — Status

## Current phase
**R0 — Requirement Re-baseline.** The official task specification
(`AI-PROJ-CV-01` v1.0) and owner clarifications arrived after Slice 5;
product direction changed materially (internal Candidate Intelligence
platform, not external B2B API). All product docs and Claude instructions
were audited and updated to match (see D-011, `docs/DECISIONS.md`, and
`docs/PROJECT_VISION.md`). This pass is documentation-only — no new
feature code.

**Slice 6 implementation has NOT started.**

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

## Tests
105/105 passing (65 prior + 40 new). Deterministic policy unit tests
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
`ruff` and `mypy` clean.

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
Nothing in flight. Documentation re-baseline (R0) is complete and
awaiting owner review before commit.

## Blockers
None blocking. Same open items as before (D-001 Mac benchmark pending,
Auto Mode script dry-run only, document encryption-at-rest deferred,
D-009 Ollama upgrade needs root). New: Git remote/PR infrastructure not
yet set up — owner/bank-supplied.

## Next action
1. **Git Infrastructure** — set up bank-approved remote repository
   hosting + task-branch/PR workflow (see `docs/MVP_PLAN.md` § Git
   Infrastructure). Not started; requires owner/bank environment.
2. **Slice 6 — Local CV Library & Folder Indexer** (see
   `docs/MVP_PLAN.md`). **Not started.**

The previously planned "Slice 6 — External Async Evaluation API" is
CANCELLED (superseded by D-011) — it is not what "Slice 6" now refers to.

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
| Multilingual CV tests | PARTIAL | Synthetic fixtures include AZ/RU/EN cases (SECURITY_PRIVACY.md Test data) | Broader multilingual extraction-quality coverage | ongoing |
| Structured extraction | DONE | `CandidateProfileExtraction` schema, Slice 4 | Add `CandidateIdentity` fields | 7 |
| Strict JSON validation | DONE | Pydantic v2, `extra="forbid"`, bounded retry (Slice 4) | — | 4 |
| Uncertainty handling | DONE | `UNKNOWN` never auto-downgraded (D-010), Slice 5 | — | 5 |
| Candidate DB | DONE | `Candidate`, `CandidateDocument`, `CandidateProfileVersion` | Add `CandidateIdentity` | 7 |
| Original file reference | DONE | Opaque storage id + `DocumentStorage` abstraction (Slice 3) | Authorized UI access to original CV | 11 |
| Local CV folder migration/indexing | NOT STARTED | — | Folder scanner, hash-based incremental indexing | 6 |
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
| Bad-file testing | DONE | Oversized/malformed/MIME-mismatch tests (Slice 3) | Extend to folder-scanner input | 6, 13 |
| Scoring consistency | PARTIAL | Policy engine deterministic + unit tested (Slice 5) | Re-verify once numeric score lands | 10, 13 |
| External-network/exfiltration verification | NOT STARTED | Local-only enforced by construction (`OllamaLLMProvider` loopback check) | Explicit verification pass | 13 |
| Data-protection / backup description | PARTIAL | Retention/deletion documented (SECURITY_PRIVACY.md); no backup policy written | Document backup approach | 13 |
| Git branch / PR workflow | NOT STARTED | No remote configured | Set up bank Git provider + PR gate | Git Infrastructure |

**Summary:** 12 DONE, 8 PARTIAL, 9 NOT STARTED (29 items). Highest-priority
gaps: local folder indexing (Slice 6, next), local embeddings/semantic
search (Slice 7–8), 0–100 numeric scoring (Slice 10), and Git/PR
infrastructure (blocks a clean task-branch workflow for everything
after).
