# MEYAR — Decisions

Append-only log of concise architectural/product decisions. Format: id, date,
decision, why, reversibility.

## D-015 — meyar-search-v1: Slice 8 hybrid candidate search policy

**Date:** 2026-08-23
**Decision:** `meyar.search` (Slice 8) implements a fully deterministic,
no-LLM candidate search service, policy version `meyar-search-v1`:
1. **Three modes**: `STRUCTURED_ONLY`, `SEMANTIC_ONLY`, `HYBRID`
   (`CandidateSearchRequest.mode`). `extra="forbid"` everywhere in the
   request/result schemas — no dynamic field names, no client-suppliable
   tenant id (tenant_id is always an explicit `search_candidates(...,
   tenant_id=...)` parameter, never a request field). This is NOT Slice
   9's natural-language `SearchPlan` parser — it is the validated
   structure Slice 9 will eventually produce.
2. **Required filters are a hard eligibility gate, preferred filters are
   a soft ranking signal.** A candidate failing ANY configured required
   filter is excluded entirely before any semantic scoring happens —
   semantic similarity can never resurrect a candidate who fails a
   required filter (regression:
   `test_hard_constraint_gate_never_bypassed_by_semantic_similarity`).
   Required filters apply uniformly across all three modes, including
   `SEMANTIC_ONLY` — kept consistent rather than mode-conditional, since
   the spec's "required filters gate eligibility" language is general,
   not STRUCTURED/HYBRID-specific.
3. **Search-gating semantics for required filters are deliberately
   stricter than the evaluation engine's UNKNOWN policy (D-010).** D-010
   says missing evidence in a *job evaluation* is `UNKNOWN`, never a
   silent `NOT_MATCHED` — that rule is unchanged and untouched. But a
   search REQUIRED filter is a hard eligibility gate by definition: an
   unproven required filter (skill absent, or an unparseable/ambiguous
   employment-date range) simply excludes the candidate from THIS
   search's results, it does not error and does not get a "manual
   review" status (search has no such concept). This is a new, distinct
   policy for search gating, not a reinterpretation of D-010.
4. **Structured preferred score** = (matched preferred filters) / (total
   configured preferred filters), bounded [0, 1]. If NO preferred
   filters are configured, every eligible candidate gets a uniform 0.0 —
   a neutral floor, never a fabricated advantage. `structured_score` is
   `None` (not 0.0) in `SEMANTIC_ONLY` results, meaning "not evaluated in
   this mode" — a deliberate distinction from a computed 0.0.
5. **Semantic retrieval requires an explicit `EmbeddingSearchConfig`**
   (provider, model_name, model_revision, serializer_version,
   embedding_dimensions) — never "whatever embedding row is newest."
   Query text is embedded locally through the exact same
   `EmbeddingProvider` instance the caller supplies; the service
   verifies `embedding_provider.provider_name/model_name/model_revision`
   match the request's `embedding_config` before use, rejecting a
   mismatch as `EMBEDDING_PROVIDER_CONFIG_MISMATCH`. `STRUCTURED_ONLY`
   never calls `embedding_provider.embed()` — proven by a regression
   test using a provider configured to raise if invoked
   (`test_structured_only_never_calls_embedding_provider`).
6. **Only current, exactly-compatible embeddings participate.** A
   compatible embedding is one whose `candidate_profile_version_id`
   equals the candidate's CURRENT `CandidateProfileVersion` (never a
   superseded/stale version) AND whose
   provider/model_name/model_revision/serializer_version/
   embedding_dimensions all exactly match the active `EmbeddingSearchConfig`
   — mirrors the Slice 7 (D-014) provenance-grouping rule. The dimension/
   config filter runs in an inner SQL subquery
   (`candidate_embedding_repo.search_compatible_embeddings`) so pgvector's
   `cosine_distance` (`<=>`) operator is only ever evaluated over
   already-compatible rows, never a mismatched-dimension row from a
   different configuration group. A candidate lacking a current
   compatible embedding is excluded from `SEMANTIC_ONLY`/`HYBRID` results
   (never assigned a fabricated semantic score of 0) — tracked in
   `excluded_missing_embedding_count`.
7. **Semantic normalization**: pgvector's cosine_distance returns
   `1 - cosine_similarity`; `semantic_score = (cosine_similarity + 1) /
   2`, clamped to [0, 1] for floating-point tolerance
   (`meyar.search.policy`). This is the only normalization formula used.
8. **Hybrid formula**: `hybrid_score = (structured_weight *
   structured_score) + (semantic_weight * semantic_score)`. Weights
   default to 0.5/0.5, must each be in [0, 1], and (for `HYBRID` only)
   must sum to 1.0 within a `1e-6` tolerance. This is a SEARCH RELEVANCE
   score (0-1) — never a hiring score, JD fit score, or 0-100 score
   (Slice 10 owns official JD scoring).
9. **No premature semantic top-k.** For `HYBRID`, the full eligible +
   compatible candidate set is scored before any sort/limit is applied —
   proven by a regression where a candidate with a lower semantic score
   but a perfect preferred-structured score outranks a candidate with a
   near-perfect semantic score under structured-favoring weights, even
   with `limit=1` (`test_no_premature_semantic_top_k_before_hybrid_score`).
10. **Deterministic tie-break**: sort by relevance descending, then
    candidate UUID ascending (string comparison) — never name/email/
    phone, never insertion order.
11. **Experience-duration reproducibility**: `min_total_experience_years`
    filters require an explicit `as_of_date` on the request (validation
    error otherwise) — an "ongoing"/"present" employment entry resolves
    to `as_of_date.year`, never `datetime.now().year`, so an identical
    persisted request always produces an identical result regardless of
    when it is re-run. This intentionally duplicates two small regexes
    from `meyar.evaluation.experience` rather than modifying that
    module's (wall-clock-based) `parse_year` — the evaluation engine's
    own date-parsing behavior is untouched.
12. **`CandidateIdentity` is never queried anywhere in `meyar.search`.**
    Regression test attaches wildly different identity content
    (name/email/phone) to two otherwise-identical candidates and proves
    rank/relevance is unchanged (`test_identity_data_does_not_change_rank`).
13. **Execution strategy**: tenant-scoped current profile versions are
    fetched via `list_current_profile_versions_for_tenant`, required
    filters are evaluated in application code (JSONB predicates would be
    brittle for this content shape), and the resulting eligible profile-
    version-id set constrains the pgvector semantic query — acceptable
    for MVP scale per the task brief. Exact (brute-force) pgvector
    cosine similarity is used; ANN/HNSW is explicitly deferred (same
    D-014 rationale: final embedding model/dimension isn't approved yet).
14. **Zero-norm vector hardening (small Slice 7 change)**:
    `OllamaEmbeddingProvider.embed` now rejects an all-zero vector as
    `EmbeddingInvalidOutputError` — cosine similarity/distance is
    undefined for a zero vector, and a genuine embedding of non-empty
    text is never all-zero. This prevents a zero-norm vector from ever
    being persisted, which is the smallest safe strategy for Slice 8's
    pgvector cosine search (no new defensive filtering needed at query
    time for future data); the *query* vector is still independently
    checked for zero-norm/dimension/finiteness at search time regardless.
15. **Auditability**: `CANDIDATE_SEARCH_EXECUTED` audit events carry only
    mode, counts, limit, policy version, and (for semantic modes) the
    embedding configuration plus a SHA-256 of the semantic query — never
    the raw query text, candidate identity, CV text, or vector values.
16. **No new persistent schema.** No migration was added — Slice 8 reads
    existing Slice 4/7 tables and adds only application-layer query
    functions (`list_current_profile_versions_for_tenant`,
    `search_compatible_embeddings`).
17. **Interface**: internal service (`meyar.search.service.search_candidates`)
    + a CLI demonstration (`meyar search-candidates --tenant-id ...
    --request-file <path>`, a JSON `CandidateSearchRequest` file — no
    natural-language input). No new REST endpoint (Slice 12 owns API
    productization).
**Why:** These are the concrete implementation choices needed to satisfy
issue #10's acceptance criteria — a fully documented, reviewable ranking
policy so Slice 9 (natural-language → this request shape) and Slice
11/12 (presentation/API) can consume it without reverse-engineering
ranking semantics from code, matching the precedent set by D-010 for the
evaluation engine.
**Reversibility:** Fully reversible/tunable — weights, the neutral-floor
policy for zero preferred filters, and the tie-break field are named
constants/documented choices; a materially different ranking formula
requires only bumping `SEARCH_POLICY_VERSION` so results remain
correctly attributed to the policy that produced them. No data was
migrated or destroyed; the OllamaEmbeddingProvider zero-vector rejection
only affects embeddings generated going forward.

## D-014 — Slice 7 identity/embedding semantics + pgvector adoption

**Date:** 2026-08-23
**Decision:** Candidate Identity + Local Embeddings / Vector Index (Slice 7)
implementation choices:
1. **`CandidateIdentity` is a wholly separate immutable, versioned table**
   (`CandidateIdentityVersion`, mirroring `CandidateProfileVersion`) —
   never columns on `Candidate`/`CandidateProfile`. It is read only for
   authorized presentation; no code path in extraction, evaluation,
   embedding generation, or (future) search may query it. Identity
   extraction reuses the existing `LLMProvider`/evidence-verification
   machinery but with its own unredacted view
   (`build_identity_document_view`) and its own schema
   (`CandidateIdentityExtraction`, `extra="forbid"`, only
   full_name/email/phone) — the professional extraction's view stays
   redacted (Slice 4) and its schema still has no identity fields.
2. **Embedding source is deterministic `CandidateProfile` content only.**
   `build_professional_embedding_text` reads fixed professional fields in
   a fixed order (never raw dict iteration, never evidence quotes) so the
   same `CandidateProfileVersion` always yields the same text and
   `source_sha256`. `CandidateIdentity` is never imported by the
   embedding path — structurally impossible to smuggle in, since
   `CandidateProfileExtraction` has no identity field to begin with.
3. **`CandidateEmbeddingVersion` rows are immutable and version-bound to
   an exact `candidate_profile_version_id`.** "Current" is derived
   relationally (does an embedding row exist for the candidate's current
   profile version, for the configured provider/model/revision?) —
   never a boolean flag that can drift stale. An older embedding for a
   superseded profile version is simply absent from that lookup, not
   silently returned. A failed embedding attempt persists no row at all
   (unlike identity/profile versions, which persist a FAILED row for
   audit) — there is no meaningful "partial vector" to keep.
   **Reuse/uniqueness identity is seven fields, not five:**
   `(tenant_id, candidate_profile_version_id, provider, model_name,
   model_revision, serializer_version, source_sha256)` — both on the
   DB `UniqueConstraint` and on the reuse lookup
   (`get_embedding_version_by_source`). `serializer_version` and
   `source_sha256` are provenance fields that also gate reuse, not
   passive metadata: a serializer revision, or any change to the
   serialized professional text under an unchanged serializer_version,
   always produces a new, distinct embedding — it is never silently
   masked by an older row's vector. (An initial version of this slice
   omitted these two fields from the reuse key; an acceptance audit
   reproduced the resulting false-reuse defect before merge, and this
   is the corrected design.)
4. **The `embedding` column is dimension-agnostic** (`pgvector.sqlalchemy
   .Vector()`, no fixed length) with `embedding_dimensions` stored
   explicitly per row — the final production embedding model/dimension
   is not approved (blocked on the target Mac Mini benchmark, M5/Slice
   13). A physical ANN/HNSW index is deferred until one is. The
   configured default model is explicitly labeled
   `DEV_INTEGRATION_MODEL` (`nomic-embed-text`), never presented as
   final.
5. **PostgreSQL image changed to `pgvector/pgvector:pg16`** (from
   `postgres:16-alpine`) in both `docker-compose.yml` and CI — same
   PostgreSQL 16 major version and data directory format, just adds the
   `vector` extension. The local dev container was recreated with this
   image; its existing named volume (`meyar_pg_data`) was preserved, no
   data lost. CI keeps normal bridge networking; the D-005 host-network
   workaround remains a local-dev-only accommodation. `pgvector` (Python)
   is a new backend dependency (`backend/uv.lock` updated).
**Why:** Preserves the identity/profile privacy boundary
(docs/MASTER_SPEC.md §5) as a hard architectural invariant even as the
codebase grows, and keeps embedding "current"-ness correct by
construction rather than by convention — both were explicit MVP risks
this slice was scoped to close before Slice 8 search is built on top of
it. **Reversibility:** fully reversible; no destructive schema/data
choices, and the dimension-agnostic column means the final approved
production embedding model can be adopted later without a migration.

## D-013 — Slice 6 folder-indexer semantics

**Date:** 2026-08-23
**Decision:** Local CV Library & Folder Indexer (Slice 6) implementation
choices, all reversible:
1. **Removed files are tombstoned, never hard-deleted.** A previously
   indexed path no longer seen on a full scan gets
   `FolderIndexedFile.index_status = MISSING`; its `candidate_id`/
   `candidate_document_id` and all underlying `Candidate`/
   `CandidateDocument`/`CanonicalDocument`/`AuditEvent` rows are left
   untouched. If the same content reappears later, the row reactivates
   to `INDEXED` without re-ingesting.
2. **A changed file (same relative path, new SHA-256) creates a new,
   immutable `CandidateDocument`/`CanonicalDocument` version** via the
   existing ingestion pipeline, reusing the *same* `Candidate` identity.
   The index row's `candidate_document_id` is updated to point at the
   new version; the prior version is never mutated or deleted — it
   remains queryable historical evidence.
3. **A failed re-import never clears a previously successful
   `candidate_document_id`.** Only `sha256_hash`/`index_status`/failure
   fields update on a FAILED attempt, so a transient or persistent
   failure can never destroy the last known-good record.
4. **Symlinks are never followed** by the folder scanner — neither
   directory nor file symlinks — as the path-traversal/source-root-escape
   defense, on top of an explicit `is_relative_to(root)` check.
5. **A file that fails only at parse time (not at upload validation) is
   still recorded `INDEXED`**, matching the existing direct-upload
   API's behavior (`CandidateDocument` created with
   `parser_status=PARSE_FAILED`). Only files rejected by
   `validate_upload` itself (`UnsupportedDocumentError`/
   `DocumentTooLargeError`) are recorded `FAILED` at the folder-index
   level.
**Why:** These preserve the existing immutability/evidence guarantees of
`CandidateDocument`/`CanonicalDocument` (docs/MASTER_SPEC.md §12-13)
under repeated, idempotent folder scans, and reuse — rather than
duplicate — the direct-upload pipeline's exact validation/parse-outcome
semantics. **Reversibility:** fully reversible; no data is destroyed by
any of these choices, so a future policy change (e.g. hard-deleting
long-missing files) can be layered on without a migration.

## D-012 — GitHub / branch / PR / CI governance

**Date:** 2026-08-23
**Decision:** MEYAR's development remote and delivery process are
formalized:
1. Current development remote is a **private personal repository**,
   `https://github.com/a-r3/meyar.git` (`a-r3/meyar`). It is temporary
   development infrastructure, not an official bank-owned
   repository — it will be migrated once the bank provides one, with
   **full Git history preserved** on migration (no history rewrite for a
   remote-ownership change alone).
2. `main` is the stable integration branch. After the one-time
   repository-bootstrap push (`f8ac183`, pushed directly since the
   remote was created empty), **direct pushes to `main` are prohibited**.
   Normal changes use a task branch (`feat/*`/`fix/*`/`chore/*`/
   `docs/*`/`test/*`) → PR → review → merge → `git pull --ff-only`.
3. CI (`.github/workflows/ci.yml`) runs on PRs into `main`: `ruff check
   .`, `mypy src`, `pytest -q`, against a Postgres 16 GitHub Actions
   service container (normal bridge networking — the D-005 `network_mode:
   host`/port-55719 workaround is a memory-constrained-dev-laptop fix
   only and is never replicated in CI infrastructure, though CI's service
   container is still mapped to host port 55719 purely to match the
   already-hardcoded `tests/conftest.py` connection string without
   editing test code). No Ollama, no cloud AI key, no production
   credential is required — extraction/loopback-enforcement tests only
   construct `OllamaLLMProvider` to check validation, never call a live
   model.
4. **The CI mypy gate is intentionally `mypy src`, not `mypy .`.**
   Production source is clean; there is known, pre-existing test-only
   mypy debt (30 errors across 5 test files). A future
   `chore/test-mypy-cleanup` branch will fix that debt and then widen CI
   to `mypy .`. This scope decision is deliberate, not a hidden gap.
5. No real candidate data may ever enter GitHub. Enforced by
   `.gitignore`, the pre-existing `.claude/hooks/guard.sh` (Claude Code
   tool-use guard), and new repo-local Git hooks (`.githooks/pre-commit`,
   `.githooks/pre-push`, activated via `scripts/setup-git-governance.sh`
   → `git config core.hooksPath .githooks`, this-repo-only, no global
   Git config touched). `pre-commit` blocks obvious secret/credential
   paths, real-CV/runtime-data paths, DB dumps, and model artifacts.
   `pre-push` blocks a direct `main` push after bootstrap, with an
   explicit owner-only bypass (`MEYAR_ALLOW_MAIN_PUSH=1`) that is never
   set automatically.
6. Full operational detail lives in `.claude/rules/git-workflow.md`;
   `AGENTS.md` and `CLAUDE.md` are concise Codex/Claude entry points to the
   same shared `docs/` authority; `.githooks/` and `.github/` provide
   agent-independent enforcement.
7. Server-side branch protection is unavailable on the current private-
   repository plan (`SERVER_SIDE_BRANCH_PROTECTION_UNAVAILABLE_ON_CURRENT_PLAN`).
   The repository will not be made public and the plan will not be upgraded
   for this task. Until official hosting or plan capabilities change, the
   accepted fallback is task-branch discipline, repo-local Git hooks, pull
   requests, GitHub Actions CI, and owner review.
8. Root `README.md` is a required delivery artifact and the primary human
   onboarding surface. It summarizes current reality and points to `docs/`;
   it is not a second product specification. `AGENTS.md` remains the Codex
   entry point, `CLAUDE.md` remains the Claude entry point, and `docs/` remains
   the detailed canonical authority.
9. Dependabot version updates are configured weekly with low PR limits for
   the supported `uv` ecosystem in `/backend` and GitHub Actions in `/`.
   Compatible routine minor/patch updates are grouped to reduce noise; major
   updates remain separate. Dependabot alerts and security-update PRs are
   enabled where supported by the current repository/account, without buying
   GitHub Advanced Security or adding private registries/credentials.
10. Dependabot never auto-merges under the current policy: Dependabot PR → CI
    → owner review → merge. Major updates require explicit compatibility and
    migration review, and backend dependency changes must commit the updated
    `backend/uv.lock`. A future semver-patch-only auto-merge policy requires a
    separate accepted decision after real CI behavior is observed. Major and
    minor updates, and all application feature PRs, must never be auto-merged
    by default.
11. The standard merge strategy is **Squash and merge**. Normal flow is: task
    branch → implementation → tests → commit(s) → push → PR → CI → owner
    review → owner Squash and merge → agent verifies remote merge → agent
    synchronizes local `main` → next approved task branch. Merge commits and
    rebase-and-merge are disabled by default so `main` retains one concise,
    reviewable commit per coherent PR. Repository settings allow squash merges,
    disallow merge commits/rebase merges, and delete merged branches.
12. Any workflow point requiring manual owner action is an explicit checkpoint,
    never a silent stop. The agent ends its report with `## HUMAN ACTION
    REQUIRED` and states what to do, where, the exact action/value, what not to
    do, and the reply expected. This applies to PR review/merge, unavailable
    authentication or repository UI settings, plan/paid-feature choices,
    destructive Git actions, irreversible production/security decisions,
    bank or target-Mac access, real-data approval, and business-owner scope
    confirmation. After an owner reports a merge, the agent verifies the PR
    and remote `main`, uses `git pull --ff-only origin main`, and only then
    removes the safely merged local branch or starts the next approved branch.
13. GitHub milestone mapping is canonical in `docs/MVP_PLAN.md` and summarized
    with current state in `docs/STATUS.md`: M0 foundation/governance; M1 Slice
    6; M2 Slices 7–9; M3 Slice 10; M4 Slices 11–12; M5 Slice 13 plus target-Mac
    validation. No due dates are invented before the official timeline exists.
    Material work uses the applicable milestone and one issue per coherent
    deliverable when useful—not micro-issues for every edit. PRs reference the
    issue with `Closes #<issue-number>` when appropriate; owner Squash and
    merge closes the issue and milestone progress is updated. Milestones are
    not invented, renamed, closed, or reorganized without an approved roadmap
    decision.
**Why:** The owner approved a concrete GitHub remote and asked for the
task-branch/PR/CI discipline the official task requires (§13 of
`AI-PROJ-CV-01`) to be encoded durably in the repository itself, not just
followed ad hoc in one session.
**Reversibility:** Fully reversible — the remote can be swapped (history
preserved), hooks can be disabled per-clone (`git config
--unset core.hooksPath`), and the CI gate can be widened/narrowed by
editing `.github/workflows/ci.yml` without any application code change.

## D-011 — Official task re-baseline / internal Candidate Intelligence Platform

**Date:** 2026-08-23
**Decision:** The official project task **"CV Screening API — Layihə
Task Bölgüsü"** (`AI-PROJ-CV-01`, v1.0, 18.08.2026) plus owner
clarifications are now the canonical requirement authority, superseding
prior product framing wherever they conflict. Binding changes:
1. MEYAR is an **internal HR system** for the bank, not an
   external/commercial B2B SaaS API product. No external customers,
   billing, or public API surface exist or are planned.
2. Product surfaces expand to: an internal chat-style natural-language
   search UI, a CV Library (browse/search/open original CV), and the
   internal REST API powering both (plus other approved internal
   systems).
3. A local-folder CV ingestion/indexing subsystem is required: scan a
   configured folder, hash-based incremental/idempotent change
   detection, parse/extract only new-or-changed files.
4. Local semantic vector search is **required**, not deferred — target
   direction is PostgreSQL + pgvector, local embedding model only, never
   an external embedding API.
5. A 0–100 numeric JD-match score + explanation is a **required**
   Definition-of-Done item, not deferred. This **supersedes D-010 point
   4** ("numeric scoring is deferred") — the deterministic fit-band
   algorithm in D-010 remains the correct foundation the numeric score is
   layered on top of; the exact formula is a near-term implementation
   decision (Slice 10), not finalized by this entry.
6. `CandidateIdentity` (full_name/email/phone, presentation-only) is
   introduced alongside the existing `CandidateProfile` (professional
   facts only, the only thing matching/search reads) — see
   `docs/PROJECT_VISION.md`. Not implemented yet.
7. The previously planned **"Slice 6 — External Async Evaluation API"**
   (turning the internal evaluation service into a customer-facing
   `POST /v1/evaluations` polling product) is **CANCELLED**. The new
   Slice 6 is **Local CV Library & Folder Indexer** — see
   `docs/MVP_PLAN.md`. Slice 6 has not started.
8. Existing Slice 0–5 implementation (tenant/API-key auth, versioned job
   criteria, secure document ingestion, local AI profile extraction, the
   deterministic evaluation engine) remains valid foundation. Nothing is
   rewritten or reverted because of this re-baseline; the existing
   tenant/organization isolation mechanism is kept as a resource-
   isolation abstraction whose final mapping to the bank's
   organizational boundaries is still an open implementation decision.
**Why:** The owner supplied the official bank task specification and
product clarifications after Slice 5 was implemented; the product
direction materially changed (internal platform, not external API
product) and two requirements previously treated as MVP-deferred
(semantic search, numeric scoring) are official Definition-of-Done
items. Recording this as a single decision, rather than silently editing
every affected doc, keeps the "why the docs changed" traceable.
**Reversibility:** Documentation/roadmap decision — reversible by a
further owner-directed scope change. No code was reverted; no
implementation was started under this decision (documentation-only
pass).

## D-010 — meyar-policy-v1: binding missing-evidence rule + fit-band algorithm

**Date:** 2026-08-19
**Decision:** The evaluation policy engine (`meyar.evaluation.policy`,
version `meyar-policy-v1`) is fully deterministic, no LLM. Binding rules:
1. **Missing/insufficient evidence is `UNKNOWN`, never `NOT_MATCHED`.**
   `NOT_MATCHED` is reserved for cases with explicit, reliable evidence
   that a criterion is *not* satisfied (currently: only
   `EXPERIENCE_DURATION_INSUFFICIENT`, computed from explicit parseable
   dates below the required minimum). Absence of a skill/certification/
   education/language in the profile is `UNKNOWN`.
2. **Overall fit-band algorithm** (`compute_overall_result`): (a) any
   criterion `MANUAL_REVIEW_REQUIRED` or `CONFLICTING_EVIDENCE` →
   overall `MANUAL_REVIEW_REQUIRED`; (b) any `MUST_HAVE` criterion
   `NOT_MATCHED`, `UNKNOWN`, or `PARTIAL_MATCH` → `INSUFFICIENT_EVIDENCE`
   (never an autonomous-rejection label); (c) all `MUST_HAVE` criteria
   `MATCH` and no `PREFERRED` criteria configured → `STRONG_MATCH`; (d)
   all `MUST_HAVE` `MATCH` and ≥50% of `PREFERRED` criteria `MATCH` →
   `STRONG_MATCH`, otherwise `POTENTIAL_MATCH`. No `REJECT`/`HIRE`/
   `AUTO_*` band exists anywhere in the schema.
3. A criterion's configured `manual_review_required` flag, or a raw
   `CONFLICTING_EVIDENCE` finding (e.g. overlapping employment date
   ranges), unconditionally forces that criterion's final status to
   `MANUAL_REVIEW_REQUIRED` — implemented once in
   `evaluators._finalize`, not duplicated per evaluator.
4. Numeric scoring is **deferred** (not implemented) — fit bands +
   structured per-criterion results with evidence were judged sufficient
   for MVP, per the instruction to avoid complexity without material
   value. `Evaluation` has no `numeric_score` column. **SUPERSEDED by
   D-011**: the official task requires a 0–100 score + explanation as a
   Definition-of-Done item. Points 1–3 of this decision (missing-evidence
   rule, fit-band algorithm, manual-review-forcing) remain binding and
   are the foundation the numeric score is layered on top of.
**Why:** These are exactly the binding rules the owner's Slice 5 brief
specified; recording them here (not just in code comments) so future
slices/reviewers don't have to reverse-engineer intent from code.
**Reversibility:** Fully reversible/tunable — thresholds (e.g. the 50%
preferred-match ratio) are named constants; a materially different
algorithm requires only bumping `POLICY_ENGINE_VERSION` so historic
evaluations remain correctly attributed to the version that produced them.

## D-009 — Dev integration model: Qwen3 family, not Qwen3.5 (Ollama too old)

**Date:** 2026-08-18
**Decision:** `MEYAR_OLLAMA_MODEL` defaults to `qwen3:0.6b` for Slice 4
development/testing on this machine, not Qwen3.5 as the spec suggested.
**Why:** The installed Ollama daemon (0.16.2) rejects Qwen3.5 manifests
with "requires a newer version of Ollama"; upgrading the binary requires
root (`/usr/local/bin/ollama` is root-owned, no passwordless sudo
available in this session) and was not attempted rather than stall an
otherwise-unblocked path on an interactive password prompt. Qwen3 is the
closest available same-family small model compatible with this Ollama
version, so it was used as the dev-integration substitute — a direct
continuation of D-001's pattern (dev machine ≠ target hardware, use a
practical local substitute, document it, never treat it as the
production decision).
**Reversibility:** Fully reversible — `MEYAR_OLLAMA_MODEL` is a config
value behind the `LLMProvider` abstraction. Upgrading Ollama (with root
access) and pulling a real Qwen3.5 tag requires no application code
change. **DEV_INTEGRATION_MODEL != FINAL_PRODUCTION_MODEL** — the live
smoke test in Slice 4 proves the local-inference *architecture* works,
not that `qwen3:0.6b` (or any specific tag) is approved for production;
final model selection is a target-Mac benchmark, per D-001.

## D-001 — Dev/test machine is not Apple Silicon; use small model for dev

**Date:** 2026-08-18
**Decision:** Preflight found the actual working machine is a Linux laptop
(Intel i7-1165G7, 8 threads, 7.5GB RAM, no discrete GPU), not the Apple
Silicon Mac the spec assumes. Use `qwen2.5-coder:3b` (already pulled, 1.9GB)
behind `LLMProvider` for all development/testing on this machine. Final
production model selection (Qwen3.5-9B, fallback 4B, or Ministral 3) is
deferred until real Mac hardware is available to benchmark, per spec §2.
**Why:** Spec explicitly forbids finalizing the model before inspecting real
hardware; running a 9B/4B model on 7.5GB total RAM (often <1GB free) is not
reliable.
**Reversibility:** Fully reversible — swapping `OLLAMA_MODEL` env var and
pulling a new model is a config change, no code change, due to the
`LLMProvider` abstraction.

## D-002 — Queue: Postgres-backed table + in-process asyncio worker

**Date:** 2026-08-18
**Decision:** MVP async evaluation queue is a Postgres table (`evaluation`
row with status) polled by an in-process asyncio worker loop inside the same
app process, behind a `JobQueue` interface.
**Why:** Spec forbids Kafka/microservices for MVP and asks for "the simplest
reliable background-job mechanism that fits." Avoids a second infra
dependency (Redis/broker) while remaining swappable later.
**Reversibility:** Reversible — `JobQueue` interface can be re-implemented
against a real broker without changing callers.

## D-003 — No self-service API key issuance endpoint in MVP

**Date:** 2026-08-18
**Decision:** Tenants and their first API key are minted via an internal
CLI (`meyar create-tenant`), not a public endpoint.
**Why:** No admin UI/auth model for "who is allowed to create a tenant" yet;
building that is out of MVP scope. Customers still authenticate with real
issued keys against the real API.
**Reversibility:** Reversible — an admin API can be added later without
changing the key format or verification path.

## D-004 — Retention periods left as configurable policy

**Date:** 2026-08-18
**Decision:** No hardcoded legal retention period. Retention is a config
value (default: keep until explicit deletion), deletion cascades are
implemented and tested, exact numbers are an open owner decision.
**Why:** Spec explicitly forbids inventing final legal retention periods.
**Reversibility:** Reversible — config value.

## D-005 — Postgres dev container uses host networking on port 55719

**Date:** 2026-08-18
**Decision:** `docker-compose.yml`'s `postgres` service uses `network_mode:
host` and listens on `127.0.0.1:55719`, instead of the usual bridge network
+ published port.
**Why:** On this shared dev machine, bridge-network port publishing (via
`docker-proxy`) was observed to stall *new* connections for 30-60s+ (up to
full asyncpg 60s connect timeouts) under memory pressure, causing the test
suite to fail with `TimeoutError`. Host networking removes the
`docker-proxy` hop and connections became consistently sub-100ms. Ports
5432/55432/55433 were already occupied by other unrelated projects on this
machine; 55719 was confirmed free.
**Reversibility:** Fully reversible — revert to bridge networking + a
published port if this machine's networking stabilizes or in a
non-shared/production environment; no application code depends on the
networking mode.

## D-006 — Prohibited-criteria check is a regex denylist, not NLP

**Date:** 2026-08-18
**Decision:** Backend rejection of sensitive/irrelevant job criteria
(gender, age, ethnicity, religion, marital status, health, etc. —
MASTER_SPEC.md §4) is implemented as a case-insensitive regex-pattern
denylist over each criterion's `label`/`value` text
(`meyar.schemas.criteria`), not a classifier or NLP model.
**Why:** Deterministic, dependency-free, fast, and testable — matches the
project's "no LLM in the trust-sensitive validation path" and "no
overengineering for MVP" policies. Known limitation: heuristic word-boundary
matching can false-positive on legitimate terms that contain a denylisted
word as a substring-with-punctuation (e.g. "Single Sign-On" contains
"single"); it can also miss creatively-obfuscated attempts. Acceptable for
MVP since criteria are entered by the tenant's own hiring staff via the
API, not adversarial candidate input — the higher-stakes prompt-injection
boundary is CV content (see SECURITY_PRIVACY.md), which is unaffected by
this list.
**Reversibility:** Fully reversible/tunable — the pattern list is a single
module-level constant; false positives can be fixed by narrowing a pattern
without any schema or migration change.

## D-007 — Defer Docling; use pypdf + python-docx for MVP parsing

**Date:** 2026-08-18
**Decision:** `meyar.ingestion` implements `DocumentParser` with a local
`LocalTextParser` built on `pypdf` (PDF) and `python-docx` (DOCX) — both
pure-Python/lightweight, no `torch`. Docling is not installed in this
slice.
**Why:** Docling's default `[standard]` extra pulls in `torch`,
`torchvision`, `docling-ibm-models` (layout detection), and `rapidocr` —
inspected via PyPI metadata before adding anything. This dev machine
already showed severe resource strain (194MB free RAM, 5.6GB swap in use)
from ordinary Postgres+pytest load (see D-005); loading ML layout/OCR
models on top of that is a diagnosed host-resource risk, not a
hypothetical one. Per MASTER_SPEC.md §9/§11, only digital (non-scanned)
PDF/DOCX text extraction is required for MVP — no OCR — which pypdf/
python-docx handle deterministically without any ML runtime.
**Reversibility:** Fully reversible — `DocumentParser` is a Protocol;
swapping in `DoclingParser` later (e.g. on the target Apple Silicon Mac,
or once OCR is genuinely needed) requires no change to callers, storage,
or the canonical-document schema. OCR fallback remains explicitly
undesigned/deferred, per instruction, rather than blocking this slice.

## D-008 — Max CV upload size: 10MB, configurable

**Date:** 2026-08-18
**Decision:** `MEYAR_MAX_UPLOAD_BYTES` defaults to 10MB
(`Settings.max_upload_bytes`, present since the Slice 1 config scaffold,
exercised for the first time in Slice 3). Enforced in
`meyar.ingestion.validation.validate_upload` before any bytes are hashed
or stored.
**Why:** 10MB comfortably covers real-world CVs (typically well under
1MB as text-based PDF/DOCX; even a CV with several embedded images rarely
exceeds a few MB) while bounding worst-case memory/parse cost per request
on a resource-constrained host. A parser-level page-count cap (300 pages)
in `LocalTextParser` provides a second, independent bound against
pathological small-but-complex files.
**Reversibility:** Fully reversible — single config value, overridable
per environment via `MEYAR_MAX_UPLOAD_BYTES`.
