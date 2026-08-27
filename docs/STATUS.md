# MEYAR — Status

## Current phase
**Slice 13 — Security + Official Definition-of-Done Acceptance: NOT STARTED.**
Governance PR #1 merged at `16929fd` (**M0 CLOSED**); Slice 6 PR #7
merged at `55fef2d` (**M1 — CV Ingestion & Candidate Library is
CLOSED**, issue #6 closed); Slice 7 PR #9 merged at `24b1d67` (issue #8
closed); Slice 8 PR #11 Squash-merged at `412d978` (issue #10 closed).
Slice 9 PR #13 merged at `1be5d51` (issue #12 closed; **M2 CLOSED**).
Slice 10 PR #15 merged at `1c9dbbd` (issue #14 and **M3 — JD Matching &
Ranking CLOSED**). Slice 11 PR #17 squash-merged at `e182bdd` (issue #16
closed). Slice 12 PR #19 squash-merged at `93fa567` (issue #18 closed;
**M4 — Internal Product Interface & API CLOSED**). Local `main` and
`origin/main` currently sit at `93fa567`. **M5 — Security, Target-Mac
Validation & MVP Acceptance is OPEN**, containing only issue #20 (Slice
13 — Security + Official Definition-of-Done Acceptance, a governance/
placeholder issue). Slice 13 implementation has not started.

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
- Slice 10 (JD 0–100 Scoring + Batch Ranking) — on
  `feat/jd-scoring-batch-ranking`, associated with **M3 — JD Matching &
  Ranking**, closes issue #14. Slice 5's wall-clock resolution of current
  employment is replaced by a required, persisted full
  `evaluation_as_of_date`; `meyar-policy-v1` meaning is retained because the
  change controls provenance rather than criterion semantics. New
  `meyar-score-v1` policy uses exhaustive status factors, textual float→
  Decimal conversion, declared weights only, final-only `ROUND_HALF_UP` to
  two decimals, and a typed zero-total failure. Immutable Evaluation rows gain
  nullable date/score/policy/explanation fields; exact scored provenance is
  reused with a PostgreSQL partial unique-index concurrency backstop, while
  historical rows remain NULL and unmodified. Structured explanations contain
  exact decimal strings and location-only evidence references—never identity,
  quotes, CV text, AI output, vectors, or semantic relevance. Batch ranking
  reads the tenant library directly, chooses exactly one current completed
  profile per active candidate, and sorts explicit fit tier → Decimal score →
  UUID integer; MUST_HAVE gating (including zero-weight MUST_HAVE) therefore
  dominates score. New CLI commands are the date-required `meyar evaluate` and
  `meyar rank-job`; no REST/UI surface or Slice 11 code was added. Migration
  `c0a4f2d8e317` (down revision `7aae8da26969`); see D-017.
- Slice 11 (Internal Chat UI + CV Library UI) — implemented on
  `feat/internal-chat-cv-library-ui`, associated with **M4 — Internal Product
  Interface & API**, closes issue #16 when owner-merged. FastAPI now exposes a
  separate server-rendered `/ui` router using explicitly auto-escaped Jinja2
  templates and repository-packaged local CSS (no Node/runtime CDN/analytics).
  Azerbaijani pages cover API-key login, natural-language candidate search,
  typed fail-closed planner outcomes, CV Library filters/pagination, tenant-safe
  candidate detail, presentation-only current identity, existing-job listing,
  and exact Slice 10 ranking/score/contribution display. Search delegates to
  `plan_and_search_candidates`; ranking delegates to
  `rank_candidates_for_job`; templates contain no policy formula or result
  sorting. New server-side `BrowserSession` stores only API-key id, SHA-256
  session-token digest, CSRF material, eight-hour expiry, and revocation state;
  every request refreshes the live API key and derives current tenant/scopes.
  Authenticated POSTs require constant-time CSRF validation. Cookie defaults are
  Secure/HttpOnly/SameSite=Lax/Path=/ui; local HTTP requires the explicit
  `MEYAR_UI_COOKIE_SECURE=false` override. All `/ui` responses carry restrictive
  CSP/no-sniff/no-referrer/frame-denial/no-store headers. Migration
  `e3b1f7a9c2d4` (down revision `c0a4f2d8e317`); see D-018.
- Slice 12 (REST API / Swagger / README Completion) — implemented on
  `feat/rest-api-openapi-completion`, associated with **M4 — Internal Product
  Interface & API**, closes issue #18 when owner-merged. `/api/v1` gained
  `POST /search`, `POST /search/natural-language`,
  `POST /jobs/{job_id}/criteria/{version_number}/score`,
  `POST /jobs/{job_id}/criteria/{version_number}/rank`, and
  `GET /candidates/{candidate_id}/detail` — all thin wrappers over the
  accepted Slice 5/8/9/10 services and the Slice 11 identity-enrichment
  helpers; no ranking/matching/policy logic duplicated in routes.
  `meyar.core.auth.get_current_tenant` now resolves the API key through
  FastAPI's `HTTPBearer`/`Security()` instead of a manual header read, so
  `/openapi.json` now declares a real `ApiKeyBearer` bearer security scheme
  with identical 401/403 observable behavior. External request/response DTOs
  (`meyar.schemas.api_search`, `api_evaluation`, `api_candidate`) are
  distinct from internal service schemas — `extra="forbid"` throughout, and
  trusted server config (`embedding_config`, structured/semantic weights)
  is injected server-side, never client-suppliable. `numeric_score` is
  always the canonical `ScoreExplanation` decimal string (e.g. `"75.00"`),
  never a bare Decimal/float; `evaluation_as_of_date` stays required with no
  wall-clock default; score/rank preserve exact idempotency/ordering from
  the underlying services. `/usage` now returns real tenant-scoped
  candidate/evaluation counts (`count_candidates_for_tenant`,
  `count_evaluations_for_tenant`) instead of hardcoded placeholder zeros.
  `/docs` is fully offline via the vendored `swagger-ui-bundle` package
  (zero CDN/runtime network dependency, verified by
  `test_openapi_contract.py`); `/ui/*` remains excluded from the OpenAPI
  schema. See D-019. Slice 13 final security/DoD acceptance was not started.

## Tests
473/473 passing (430 prior + 43 Slice 12 search/NL-search/evaluation/
ranking/candidate-detail/usage/OpenAPI-contract regressions). Slice 12
focused coverage proves: `STRUCTURED_ONLY` search never invokes the
embedding provider; all 7 typed `PlannerOutcome`s are reachable and
distinguishable via `POST /api/v1/search/natural-language` with no
fallback search on a non-executable outcome; genuine embedding/DB
infrastructure failure returns 503, distinct from the typed 200
`PLANNER_PROVIDER_FAILURE` outcome; a client cannot smuggle
`embedding_config`/weight overrides into a search request (422 via
`extra="forbid"`); repeated identical score requests reuse the same
`evaluation_id` (`reused=true`); `numeric_score` serializes as a
two-decimal string; batch rank response order exactly matches backend
order with no route-level re-sort; cross-tenant 404 on score/rank/detail;
and `/openapi.json` declares the Bearer scheme, excludes `/ui/*`, and has
unique operationIds.

430/430 passing (395 prior + 35 Slice 11 browser-session/auth/CSRF/UI/
library/search/ranking/XSS/header/packaging/migration regressions).
Slice 11 focused coverage proves valid/generic-invalid login, hashed session
tokens, secure cookie policies, fixation prevention, session/key expiry and
revocation, live scope enforcement, CSRF success/failure before domain work,
actual Azerbaijani POST→Slice 9→Slice 8→escaped HTML, prohibited/unsupported/
ambiguous/malformed/provider-failure no-search behavior, planner-outage
independence for library/detail, bounded library pagination and current-profile
authority with no stale fallback, direct cross-tenant candidate/criteria 404,
presentation identity and XSS escaping, backend ranking-order preservation,
UNKNOWN/INSUFFICIENT_EVIDENCE/manual-review language, security headers, local
asset/resource packaging, and real PostgreSQL migration upgrade→downgrade→
re-upgrade.

The prior 395 include 353 through Slice 9 plus 42 Slice 10 scoring/evaluation/
persistence/batch/CLI regressions.
Slice 10 coverage includes the six exhaustive status factors; exact Decimal
boundaries, unequal weights, textual float conversion, repeating-fraction and
half-cent `ROUND_HALF_UP`; zero total and mixed zero/positive validation;
UNKNOWN vs NOT_MATCHED explanation meaning and exact recomputation; persisted
date/score/policy/explanation; historical NULL rows and partial uniqueness;
same-input reuse plus date/profile/criteria-version changes; explicit 2026 →
2027 → repeated-2026 current-employment proof; fit-tier dominance;
zero-weight MUST_HAVE gating; manual-review tier; current-profile-only batch;
UUID tie/order stability under reversed repository results; identity
invariance; tenant isolation; zero/skip candidate sets; PII-safe audits/CLI;
and static no-AI/vector/search/identity dependency checks. Migration
`c0a4f2d8e317` was also exercised independently as pre-Slice-10 upgrade →
legacy-row insert → upgrade → downgrade → re-upgrade, preserving NULL score
provenance and unchanged criterion JSON throughout.

The prior 353 include 231 pre-Slice-9 tests, 102 Slice 9 planner/policy/
service/CLI tests, and 20 shared Azerbaijani normalization/protected-policy
regressions.
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
Slice 11 (PR #17, closed issue #16) merged at `e182bdd` — squash SHA
`e182bdd58da6992a552fbfeb248f88852ef5a8c8`, single parent `1c9dbbd`, tree
verified identical to accepted PR head `181a634`, CI green. Slice 12
(`feat/rest-api-openapi-completion`, closes issue #18) is implemented and
pending independent acceptance/owner merge. M4 remains open because this PR
must not be merged automatically.

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
2. **Slice 12 — REST API / Swagger / README Completion** (issue #18, D-019),
   associated with **M4 — Internal Product Interface & API**: independently
   audit the focused implementation/CI, then owner Squash and merge. Do not
   close M4 or start Slice 13 until the merge is live-verified and local main
   is synchronized.

The previously planned "Slice 6 — External Async Evaluation API" is
CANCELLED (superseded by D-011) — it is not what "Slice 6" now refers to.

## GitHub milestone status

The detailed canonical mapping is in `docs/MVP_PLAN.md`. No milestone has a
due date because the official timeline has not been supplied.

| Milestone | Slice mapping | Current status |
|---|---|---|
| M0 — Project Foundation & Governance | R0 + Git Infrastructure | CLOSED — merged `16929fd`, issue #2 closed |
| M1 — CV Ingestion & Candidate Library | Slice 6 | CLOSED — merged `55fef2d` (PR #7), issue #6 closed |
| M2 — Candidate Search Intelligence | Slices 7–9 | CLOSED — PRs #9/#11/#13 merged; issues #8/#10/#12 closed |
| M3 — JD Matching & Ranking | Slice 10 | CLOSED — PR #15 merged at `1c9dbbd`, issue #14 closed |
| M4 — Internal Product Interface & API | Slices 11–12 | CLOSED — Slice 11 merged (PR #17, issue #16 closed); Slice 12 merged (PR #19 at `93fa567`, issue #18 closed) |
| M5 — Security, Target-Mac Validation & MVP Acceptance | Slice 13 + target-Mac benchmark | OPEN — issue #20 (Slice 13, governance/placeholder) open; Slice 13 implementation NOT STARTED |

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
| Original file reference | DONE | Opaque storage id + `DocumentStorage` abstraction (Slice 3) | Opaque reference is DONE; the separate mandatory "open original CV" product capability (`docs/PROJECT_VISION.md`) is not implemented — carried into Slice 13 / M5 final MVP acceptance, not Slice 11 | 13 |
| Local CV folder migration/indexing | DONE | Symlink-safe recursive scanner, SHA-256 content-hash incremental/idempotent indexing, existing ingestion pipeline reused, tombstone-not-delete on removal (Slice 6, D-013) | — | 6 |
| Local embeddings / vector storage | DONE | Local `EmbeddingProvider`/`OllamaEmbeddingProvider` (loopback-enforced), pgvector-backed `CandidateEmbeddingVersion` with version/provenance, idempotent, dimension-agnostic column (Slice 7, D-014) | — | 7 |
| Access control | DONE | API-key auth, scopes, tenant isolation (Slice 1) | Extend scopes as new endpoints ship | ongoing |
| JD matching | DONE | Deterministic per-criterion evaluation (Slice 5) | — | 5 |
| 0–100 scoring | DONE | `meyar-score-v1`: exact Decimal weighted formula, exhaustive factors, final `ROUND_HALF_UP`, persisted score and recomputable explanation (Slice 10, D-017) | — | 10 |
| Batch scoring / ranking | DONE | Tenant-library batch service, current-profile-only selection, fit-tier-first ordering, Decimal score, UUID tie-break, deterministic skips (Slice 10, D-017) | UI presentation (Slice 11) and REST presentation (`POST /api/v1/jobs/{job_id}/criteria/{version_number}/rank`, Slice 12, D-019) both live | 10 |
| Structured search | DONE | Deterministic required/preferred filters (skills/certifications/languages/education/min experience) over the current `CandidateProfileVersion`, reusing Slice 5 normalization (Slice 8, D-015) | — | 8 |
| Semantic search | DONE | Local query embedding + pgvector retrieval over current, exactly-compatible embeddings (Slice 8, D-015); strict local natural-language `SearchPlan` conversion delegates to that engine (Slice 9, D-016) | — | 8, 9 |
| Explanations | DONE | Criterion evidence (Slice 5), search components (Slice 8), planner reasons (Slice 9), and exact recomputable score contributions with location-only evidence refs (Slice 10); never chain-of-thought | — | 5, 8, 9, 10 |
| REST API | DONE | Full `/api/v1` surface: jobs/candidates/health/usage plus Slice 12 structured+NL search, single/batch scoring, and identity-enriched candidate detail — all thin wrappers over accepted Slice 5/8/9/10 services (D-019) | — | 12 |
| Swagger / OpenAPI | DONE | Bearer `securitySchemes` entry, tag metadata, unique operationIds, `/ui` excluded from schema, fully offline `/docs` (vendored `swagger-ui-bundle`, zero CDN dependency), `test_openapi_contract.py` regression (D-019) | — | 12 |
| Auth | DONE | API-key + scopes (Slice 1); OpenAPI-visible via `HTTPBearer` (Slice 12, D-019) | — | 1, 12 |
| README examples | DONE | API-key provisioning, auth header, Swagger access, synthetic curl examples for search/NL-search/score/rank, local-AI dependency map, error semantics (Slice 12) | — | 12 |
| Bad-file testing | DONE | Oversized/malformed/MIME-mismatch tests (Slice 3); malformed-PDF/DOCX isolation + path-traversal/symlink tests for the folder indexer (Slice 6) | — | 6, 13 |
| Scoring consistency | DONE | Exact input reuse, explicit historical date, Decimal boundary/rounding/recomputation, version-change, stable-tie, gate-vs-score, and identity-invariance regressions (Slice 10) | Final target acceptance remains Slice 13 | 10, 13 |
| External-network/exfiltration verification | NOT STARTED | Local-only enforced by construction (`OllamaLLMProvider` loopback check) | Explicit verification pass | 13 |
| Data-protection / backup description | PARTIAL | Retention/deletion documented (SECURITY_PRIVACY.md); no backup policy written | Document backup approach | 13 |
| Git branch / PR workflow | PARTIAL | Remote connected (`a-r3/meyar`, private), CI + hooks + PR template merged (`16929fd`, D-012) | Migrate to official bank remote when supplied | Git Infrastructure |

**Official numbered task matrix — 28 items.** Summary: **22 DONE, 2 PARTIAL,
4 NOT STARTED** (28 items), independently recounted after Slice 12. Three rows
moved from the Slice-11 baseline: REST API, Swagger/OpenAPI, and README
examples all moved PARTIAL/NOT STARTED → DONE (Slice 12, D-019). Original-file
reference remains DONE at the storage layer, but the separate mandatory
"open original CV" product capability (`docs/PROJECT_VISION.md`) is not
implemented and is now explicitly carried into Slice 13 / M5 as a mandatory
acceptance item (see issue #20) — it does not add a 29th matrix row or change
this row's DONE status. The two remaining PARTIAL rows are
data-protection/backup description and Git branch/PR workflow. The four
remaining NOT STARTED rows are target-Mac benchmark, OCR, multilingual-CV
fixtures, and external-network/exfiltration verification — all explicitly
Slice 13 (Security + Official Definition-of-Done Acceptance), not started,
not claimed by Slice 12.
