# MEYAR — Status

## Current phase
**Slice 9 — Natural-Language Search Planner implementation/acceptance.**
Governance PR #1 merged at `16929fd` (**M0 CLOSED**); Slice 6 PR #7
merged at `55fef2d` (**M1 — CV Ingestion & Candidate Library is
CLOSED**, issue #6 closed); Slice 7 PR #9 merged at `24b1d67` (issue #8
closed); Slice 8 PR #11 Squash-merged at `412d978` (issue #10 closed).
Slice 9 issue #12 is implemented on
`feat/natural-language-search-planner` and is pending owner acceptance/
merge. **M2 remains OPEN until Slice 9 is accepted and merged.**

GitHub remote established (`https://github.com/a-r3/meyar.git`, private,
temporary development remote — see D-012, `docs/DECISIONS.md`). `main`
bootstrap-pushed at `f8ac183`, then governance-merged at `16929fd`, then
Slice-6-merged at `55fef2d`. The governance PR included the required
root onboarding README and weekly low-noise Dependabot configuration for
backend `uv` and GitHub Actions dependencies; dependency auto-merge
remains disabled.

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
- Slice 6 (Local CV Library & Folder Indexer) — merged as `55fef2d`
  (PR #7), associated with **M1 — CV Ingestion & Candidate Library**
  (now CLOSED), closed issue #6. New `FolderSource`/`FolderIndexedFile`
  models (Alembic migration `bd1b929cd874`), a symlink-safe recursive
  scanner (`meyar.ingestion.folder_scanner`), and an orchestration
  service (`meyar.services.folder_indexer_service.index_folder`) that
  reuses the existing secure ingestion pipeline unchanged — that
  pipeline itself was extracted into `meyar.services
  .candidate_document_service.ingest_candidate_document` so the
  direct-upload API route and the folder indexer share exactly one code
  path (no parallel ingestion architecture). SHA-256 content hash (never
  mtime) drives NEW/CHANGED/UNCHANGED/retry classification; re-scanning
  an unchanged folder creates zero duplicate Candidate/CandidateDocument/
  index rows (dedicated regression test); a changed file creates a new
  immutable CandidateDocument version under the same Candidate identity;
  a removed file is tombstoned (`MISSING`), never hard-deleted; a
  malformed file never aborts the rest of a scan; a previously FAILED
  file is retried on the next scan when unchanged. New CLI command
  `meyar index-folder --tenant-id --root` (distinct exit codes: 0 clean,
  1 completed-with-failures, 2 invalid source, 3 infra/DB failure),
  PII-safe output (counts/ids only). See D-013, `docs/DECISIONS.md`, for
  the exact removed/changed/retry/parse-failure semantics chosen.
- Slice 7 (Candidate Identity + Local Embeddings / Vector Index) — on
  `feat/candidate-identity-vector-index`, associated with **M2 —
  Candidate Search Intelligence**, closes issue #8. Two independent new
  flows: (1) `CandidateIdentityVersion` — an immutable, versioned,
  evidence-backed local extraction of full_name/email/phone
  (`meyar.extraction.identity_service`), wholly separate from
  `CandidateProfile` and read by no matching/evaluation/search/embedding
  code path; uses a new unredacted `build_identity_document_view`
  (Slice 4's professional view stays redacted) and its own
  `extra="forbid"` schema. (2) `CandidateEmbeddingVersion` — a local,
  pgvector-persisted embedding of deterministic `CandidateProfile`
  content only (`meyar.embedding.serializer
  .build_professional_embedding_text`, fixed field order, no evidence
  quotes, identity structurally unreachable), generated through a new
  `EmbeddingProvider` abstraction (`meyar.embedding.provider`) with a
  local-only `OllamaEmbeddingProvider` (loopback-enforced, shared
  `meyar.llm.loopback.require_loopback_url` helper) and a
  `DEV_INTEGRATION_MODEL` default (`nomic-embed-text` — not an approved
  production model). Idempotent by (profile version, provider, model,
  revision, serializer version, source hash) with a DB unique-constraint
  backstop — a serializer or source-text change never silently reuses
  an older vector; "current" is derived
  from the candidate's current `CandidateProfileVersion`, never a
  fragile flag, so a superseded profile's embedding is correctly STALE;
  a changed profile creates a new embedding without destroying prior
  history; a dimension-agnostic `vector()` column avoids locking in an
  unapproved production dimension. PostgreSQL switched to
  `pgvector/pgvector:pg16` (dev container recreated, volume preserved;
  CI image updated) — Alembic migration `7aae8da26969` enables the
  extension and creates both tables. New CLI commands `meyar
  extract-identity` (PII-safe output) and `meyar embed-candidate`
  (reused/idempotent reporting, never prints the vector). See D-014,
  `docs/DECISIONS.md`, for the exact semantics chosen. **Merged as
  PR #9 at `24b1d67`, closes issue #8.**
- Slice 8 (Hybrid Candidate Search) — on `feat/hybrid-candidate-search`,
  associated with **M2 — Candidate Search Intelligence**, closes issue
  #10. New `meyar.search` package: a strict `CandidateSearchRequest`
  (`STRUCTURED_ONLY`/`SEMANTIC_ONLY`/`HYBRID`, `extra="forbid"`) with
  required filters (hard eligibility gate) separated from preferred
  filters (soft ranking signal — `structured_score` = matched/total,
  0.0 when none configured, never a fabricated advantage). Structured
  filters (skills/certifications/languages/education/
  min_total_experience_years) evaluate the candidate's CURRENT
  `CandidateProfileVersion` only, reusing
  `meyar.evaluation.normalization`'s skill/text normalization rather
  than reimplementing matching; an explicit `as_of_date` is required
  whenever an experience-duration filter is set, so "present/ongoing"
  employment resolves deterministically instead of drifting with the
  wall clock. Semantic retrieval requires an explicit
  `EmbeddingSearchConfig` (provider/model_name/model_revision/
  serializer_version/embedding_dimensions) — only a candidate's current,
  exactly-compatible `CandidateEmbeddingVersion` participates (mirrors
  the Slice 7/D-014 provenance grouping; a stale or incompatible
  embedding is excluded, never mixed into one similarity ranking); the
  query text is embedded locally through the same `EmbeddingProvider`
  instance passed in, with an explicit provider/model/revision match
  check before use. `STRUCTURED_ONLY` never calls the embedding
  provider at all (regression-tested with a provider configured to
  raise if invoked). Hybrid ranking is deterministic (`meyar-search-v1`,
  see D-015): required filters gate eligibility before any semantic
  scoring — a failed required filter can never be overridden by high
  semantic similarity — and the full eligible+compatible set is scored
  before sort/limit, so a lower-semantic/higher-structured candidate can
  still outrank a high-semantic candidate under structured-favoring
  weights even with `limit=1` (both proved by dedicated merge-critical
  regression tests). Semantic similarity is normalized from pgvector's
  `cosine_distance` via `(cosine_similarity + 1) / 2`; results sort by
  relevance descending with candidate UUID ascending as the stable,
  non-PII tie-break. `CandidateIdentity` is never queried anywhere in
  `meyar.search` (regression-tested: identical profiles/embeddings with
  wildly different identity content produce identical rank/relevance).
  `CANDIDATE_SEARCH_EXECUTED` audit events carry only mode/counts/
  policy/embedding-config metadata plus a query SHA-256 — never raw
  query text, identity, CV text, or vector values. No new schema/
  migration — Slice 8 reads existing Slice 4/7 tables via two new
  read-only repository queries. New CLI command `meyar
  search-candidates --tenant-id --request-file <path>` (a JSON
  `CandidateSearchRequest`, no natural-language input), PII-safe output
  (candidate_id/rank/scores only). A small, explicitly documented Slice
  7 hardening: `OllamaEmbeddingProvider.embed` now also rejects an
  all-zero (zero-norm) vector, since cosine similarity is undefined for
  one and a genuine embedding of non-empty text is never all-zero. See
  D-015, `docs/DECISIONS.md`, for the exact ranking policy. **Merged as
  PR #11 at `412d978`, closes issue #10.**
- Slice 9 (Natural-Language Search Planner) — on
  `feat/natural-language-search-planner`, associated with **M2 —
  Candidate Search Intelligence**, closes issue #12. New strict
  `PlannerDraft`/`SearchPlanResult` contracts and deterministic
  `meyar-search-planner-v1` policy convert untrusted English/Azerbaijani
  HR text into the existing Slice 8 `CandidateSearchRequest`. Search mode
  is derived from validated contents; the LLM cannot supply tenant id,
  reference date, embedding provenance, ranking weights, SQL, identity,
  or scores. Existing protected-criterion policy runs before and after
  the LLM; its explicit Azerbaijani root-plus-allowed-suffix handling catches
  common protected inflections without prefix-matching unrelated words such
  as `yaşıl`. Explicit MVP fidelity guards preserve common Azerbaijani
  mandatory forms and reject skill-specific duration, language proficiency,
  identity, custom search weighting, invented numeric
  experience/result counts, and invented structured fields rather than
  weakening or partially executing meaning. Local Ollama remains
  loopback-only with strict JSON parsing and exactly one bounded repair retry.
  `plan_candidate_search`
  reads no candidate/search repository (its DB session is audit-only);
  `plan_and_search_candidates` is a thin plan→accepted Slice 8 delegate,
  with the hard-gate invariant proven end-to-end against real pgvector.
  `SEARCH_PLAN_CREATED`/`REJECTED`/`FAILED` audit events retain only the
  request SHA-256, safe versions/provenance/counts/reason codes — never
  raw request, semantic query, model output, identity, or CV data. New
  CLI: `meyar plan-search --tenant-id ... --query ... --as-of-date
  YYYY-MM-DD [--execute]`. No migration/dependency was added. See D-016.

## Tests
336/336 passing (231 prior + 91 Slice 9 planner/policy/service/CLI tests +
14 shared protected-policy morphology regressions).
Slice 9 coverage includes strict draft parsing, deterministic mode and
draft-to-request conversion, Azerbaijani/English intent, unsupported semantic
weakening and custom weighting, explicit Azerbaijani mandatory/protected
morphology without broad prefix matching, protected-criteria pre/post checks,
bounded repair, provider/result provenance, trusted date/embedding/weight
injection, audit privacy, CLI exits,
structured-only no-embedding execution, and a real-pgvector hybrid hard-gate
integration path. The prior 231 tests include 177 through Slice 7 plus 54
Slice 8 tests — 17 structured + 17 semantic +
10 hybrid + 1 zero-norm-vector hardening regression on
`OllamaEmbeddingProvider` + 9 post-acceptance-audit provenance/source-
hash-freshness regressions, `test_search_semantic_provenance.py`, see
D-015's correction note). Deterministic policy unit tests
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

Slice 7 (`test_candidate_identity.py`, `test_candidate_embedding.py`,
`test_identity_embedding_cli.py`, 50 tests): identity — full_name/email/
phone extraction with real evidence verification, missing field stays
null, strict extra-field rejection, invalid/mismatched evidence
rejected, fabricated-quote rejected, bounded retry (fail-then-succeed
and fail-twice), provider unavailable/timeout handled safely,
re-extraction creates v2 and leaves v1 immutable, tenant isolation,
audit metadata and logs contain no PII, identity view proven unredacted
vs. the professional view proven redacted for the same document.
Embedding — serializer determinism (same content → same text → same
SHA-256) and identity exclusion, first-embed creates a record, identical
rerun is idempotent (provider called exactly once across two calls),
reuse/uniqueness identity independently proven to require all seven
provenance fields — a serializer-version bump, a same-hash-different-
serializer case, and a same-serializer-different-hash case each
correctly produce a distinct embedding and actually re-invoke the
provider, never silently reusing a stale vector (the exact scenario an
acceptance audit reproduced pre-merge — see D-014); DB unique-constraint
backstop independently proven for both the accept and reject sides, new
profile version makes the prior embedding provably STALE (absent from
the current-version lookup) while old history is preserved, different
model/dimension produces distinct non-mixed provenance, provider error
persists no row, tenant isolation, audit metadata contains no vector/
text/PII, plus direct
`OllamaEmbeddingProvider` HTTP-behavior tests via `httpx.MockTransport`
(no real Ollama): loopback rejection/acceptance, valid response, empty/
missing/NaN/non-numeric vector rejected, non-200 and connect-error and
timeout handled. CLI — `extract-identity` PII-safe happy path and
not-found case, `embed-candidate` happy-path-then-reused (provider
called once) and no-profile exit code 2. Plus one Slice 8 hardening
regression added to this file: `OllamaEmbeddingProvider` rejects an
all-zero (zero-norm) embedding vector.

Slice 8 (`test_search_structured.py` 17, `test_search_semantic.py` 17,
`test_search_hybrid.py` 10 — 44 tests, all against real pgvector, no
mocked vector distance): structured — no-filter bounded result, required
skill match/non-match/multiple-required-all-must-match, preferred skill
score affecting rank, certification/language/education filters,
experience-threshold filter, `as_of_date`-deterministic "present"
employment (repeated identical search same result), missing `as_of_date`
rejected when an experience filter is set, protected/sensitive term
rejected in both filter values and semantic query, current-profile-only
search (stale v1 skill no longer matches after v2 supersedes it,
result correctly references v2), `STRUCTURED_ONLY` never invokes the
embedding provider (proven with a provider configured to raise if
called), stable candidate-UUID tie-break, tenant isolation. Semantic —
cosine-similarity ranking order, score normalization bounded [0,1],
top-N limit, stale-profile embedding excluded then re-included once a
compatible v2 embedding exists, incompatible provider/model/model-
revision/serializer/dimension each independently excluded (never mixed
into one ranking), candidate with no compatible embedding excluded,
invalid query-vector dimension rejected, zero-norm query vector
rejected, provider error propagates safely, tenant isolation, vector
values never present in the response, raw query text never present in
the audit event (only a SHA-256), embedding-provider/config mismatch
rejected. Hybrid — the two merge-critical regressions:
**hard-constraint gate never bypassed by semantic similarity** (a
candidate failing a required skill with near-perfect semantic similarity
is excluded entirely; the passing, lower-similarity candidate is
returned) and **no premature semantic top-k** (a candidate with a lower
semantic score but a perfect preferred-structured score correctly
outranks a candidate with a near-perfect semantic score under
structured-favoring weights, even with `limit=1`); plus preferred-score-
affects-rank, weights-must-sum-to-1.0 validation, the exact deterministic
weighted-sum formula reproduced from returned scores, candidate lacking
a compatible embedding excluded from hybrid, stable tie-break, repeated-
identical-search determinism, **`CandidateIdentity` proven not to affect
rank/relevance** (two candidates with identical profiles/embeddings but
wildly different identity content produce identical results), and
explanation components (matched required/preferred filter labels)
proven to match the actual score calculation.

Post-acceptance-audit correction (`test_search_semantic_provenance.py`,
9 tests — see D-015's correction note): an independent acceptance audit
of the initial Slice 8 implementation reproduced two defects before
merge, both fixed on this same branch/PR. (1) The service validated
only the `EmbeddingProvider` object's declared static attributes
against `embedding_config`, never the actual `EmbeddingResult`'s own
provider/model_name/model_revision — fixed, and regression-tested with
a provider whose declared attributes match config but whose `.embed()`
result claims a different provider/model/revision (same dimensions),
each independently rejected as `EMBEDDING_RESULT_PROVENANCE_MISMATCH`;
a matching-provenance case is also tested to prove the fix isn't
over-strict. Two non-finite (NaN/±Inf) query-vector regressions prove
the search boundary itself validates the vector, not just
`OllamaEmbeddingProvider`. (2) `search_compatible_embeddings` selected
among multiple same-profile/same-config embedding rows (differing only
by `source_sha256`, a state Slice 7 explicitly allows) by arbitrary/
unordered SQL row-return order rather than by the current canonical
serialization — independently proven (during the audit) to flip between
the current and a stale embedding purely by reversing insertion order.
Fixed by recomputing each eligible candidate's current canonical
`source_sha256` at search time and requiring an exact
`(candidate_profile_version_id, source_sha256)` match; regression-tested
for insertion-order independence (both orders select only the current
hash, identical scores), the current hash being entirely absent
(candidate excluded, never falls back to a stale hash), and three
coexisting historical hashes (only the current one participates, the
candidate appears exactly once).
`uv run ruff check .` and `uv run mypy src` are clean (95 source files).

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
Slice 9 (`feat/natural-language-search-planner`, closes issue #12) is
implemented and pending owner acceptance/merge. Slice 8 PR #11 merged
(`412d978`, issue #10 closed); M2 intentionally remains open.

## Blockers
None blocking. Same open items as before (D-001 Mac benchmark pending —
now also the blocker for approving a final production embedding model,
D-014, Auto Mode script dry-run only, document encryption-at-rest
deferred, D-009 Ollama upgrade needs root). Git remote is connected but
is a personal/temporary one (D-012) — official bank-owned remote still
pending, migration keeps full history when it arrives.

## Next action
1. **Git Infrastructure** — remote connected (`a-r3/meyar`, private,
   temporary — D-012); governance merged (`16929fd`). May later migrate
   to an official bank-owned remote (history preserved).
2. **Slice 9 — Natural-Language Search Planner** (issue #12, D-016),
   associated with **M2 — Candidate Search Intelligence**: review the
   focused implementation/CI, then owner Squash and merge. Do not close
   M2 or start Slice 10 until the merge is live-verified and local main is
   synchronized.

The previously planned "Slice 6 — External Async Evaluation API" is
CANCELLED (superseded by D-011) — it is not what "Slice 6" now refers to.

## GitHub milestone status

The detailed canonical mapping is in `docs/MVP_PLAN.md`. No milestone has a
due date because the official timeline has not been supplied.

| Milestone | Slice mapping | Current status |
|---|---|---|
| M0 — Project Foundation & Governance | R0 + Git Infrastructure | CLOSED — merged `16929fd`, issue #2 closed |
| M1 — CV Ingestion & Candidate Library | Slice 6 | CLOSED — merged `55fef2d` (PR #7), issue #6 closed |
| M2 — Candidate Search Intelligence | Slices 7–9 | OPEN / IN REVIEW — Slices 7 and 8 merged (PRs #9/#11; issues #8/#10 closed); Slice 9 issue #12 implemented on its task branch, pending acceptance/merge |
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
| Structured extraction | DONE | `CandidateProfileExtraction` schema (Slice 4); `CandidateIdentityExtraction` (full_name/email/phone, separate schema, Slice 7) | — | 4, 7 |
| Strict JSON validation | DONE | Pydantic v2, `extra="forbid"`, bounded retry (Slice 4, Slice 7 identity, Slice 9 planner) | — | 4, 7, 9 |
| Uncertainty handling | DONE | `UNKNOWN` never auto-downgraded (D-010), Slice 5 | — | 5 |
| Candidate DB | DONE | `Candidate`, `CandidateDocument`, `CandidateProfileVersion`, `CandidateIdentityVersion` (Slice 7, D-014) | — | 3, 4, 7 |
| Original file reference | DONE | Opaque storage id + `DocumentStorage` abstraction (Slice 3) | Authorized UI access to original CV | 11 |
| Local CV folder migration/indexing | DONE | Symlink-safe recursive scanner, SHA-256 content-hash incremental/idempotent indexing, existing ingestion pipeline reused, tombstone-not-delete on removal (Slice 6, D-013) | — | 6 |
| Local embeddings / vector storage | DONE | Local `EmbeddingProvider`/`OllamaEmbeddingProvider` (loopback-enforced), pgvector-backed `CandidateEmbeddingVersion` with version/provenance, idempotent, dimension-agnostic column (Slice 7, D-014) | — | 7 |
| Access control | DONE | API-key auth, scopes, tenant isolation (Slice 1) | Extend scopes as new endpoints ship | ongoing |
| JD matching | DONE | Deterministic per-criterion evaluation (Slice 5) | — | 5 |
| 0–100 scoring | NOT STARTED | Fit-band algorithm exists (D-010), no numeric score | Numeric formula on top of existing engine | 10 |
| Batch scoring / ranking | NOT STARTED | Evaluation engine is per-candidate today | Batch-ranking endpoint reusing engine | 10 |
| Structured search | DONE | Deterministic required/preferred filters (skills/certifications/languages/education/min experience) over the current `CandidateProfileVersion`, reusing Slice 5 normalization (Slice 8, D-015) | — | 8 |
| Semantic search | DONE | Local query embedding + pgvector retrieval over current, exactly-compatible embeddings (Slice 8, D-015); strict local natural-language `SearchPlan` conversion delegates to that engine (Slice 9, D-016) | — | 8, 9 |
| Explanations | DONE | Evidence carried through every criterion result (Slice 5); Slice 8 search results carry safe score/filter components; Slice 9 returns a concise structured interpretation summary/reason codes, never chain-of-thought | — | 5, 8, 9 |
| REST API | PARTIAL | Jobs/candidates/health routes live; extraction+evaluation are service+CLI only | Add internal HTTP routes as UI needs them | 11, 12 |
| Swagger / OpenAPI | PARTIAL | FastAPI auto-generates it; not yet reviewed/finalized as a deliverable | Review + README examples | 12 |
| Auth | DONE | API-key + scopes (Slice 1) | — | 1 |
| README examples | NOT STARTED | — | Usage examples once API surface stabilizes | 12 |
| Bad-file testing | DONE | Oversized/malformed/MIME-mismatch tests (Slice 3); malformed-PDF/DOCX isolation + path-traversal/symlink tests for the folder indexer (Slice 6) | — | 6, 13 |
| Scoring consistency | PARTIAL | Policy engine deterministic + unit tested (Slice 5) | Re-verify once numeric score lands | 10, 13 |
| External-network/exfiltration verification | NOT STARTED | Local-only enforced by construction (`OllamaLLMProvider` loopback check) | Explicit verification pass | 13 |
| Data-protection / backup description | PARTIAL | Retention/deletion documented (SECURITY_PRIVACY.md); no backup policy written | Document backup approach | 13 |
| Git branch / PR workflow | PARTIAL | Remote connected (`a-r3/meyar`, private), CI + hooks + PR template merged (`16929fd`, D-012) | Migrate to official bank remote when supplied | Git Infrastructure |

**Official numbered task matrix — 28 items.** Summary: 16 DONE, 5 PARTIAL,
7 NOT STARTED (28 items), independently recounted after Slice 9. Planner
tests cover synthetic Azerbaijani/English HR requests, but this does not
complete the distinct multilingual CV extraction-fixture row. Highest-
priority gap: 0–100 numeric JD scoring (Slice 10). Git/PR infrastructure is PARTIAL only
because the remote is still a personal/temporary one, not blocking.
