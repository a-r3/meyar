# MEYAR — Decisions

Append-only log of concise architectural/product decisions. Format: id, date,
decision, why, reversibility.

## D-018 — Internal server-rendered UI and browser session bridge

**Date:** 2026-08-23
**Decision:** Slice 11 introduces the first browser-facing internal HR surface
without creating a second product-policy engine:
1. **Architecture:** the existing FastAPI modular monolith renders HTML with an
   explicitly auto-escaping Jinja2 environment. Templates and repository-owned
   CSS are Python package resources. There is no SPA, Node runtime, frontend
   package manager, remote font/icon/script, analytics, or telemetry dependency.
2. **API-key exchange:** `/ui/login` accepts an API key only in a POST form body
   and validates it through the same `authenticate_raw_api_key` authority used
   by Bearer authentication. A successful exchange issues a fresh opaque
   `secrets.token_urlsafe(32)` browser token; neither the API key nor the browser
   token is put in HTML, JavaScript, browser storage, logs, or audit metadata.
3. **Server-side sessions:** `BrowserSession` stores only the owning `api_key_id`,
   SHA-256 session-token digest, independent CSRF secret material, creation/
   expiry timestamps, and revocation timestamp. It stores no tenant/scope copy,
   identity, request text, or result payload. The fixed MVP lifetime is eight
   hours with no sliding renewal or remember-me behavior.
4. **Live authority:** every authenticated browser request hashes the cookie,
   resolves a non-expired/non-revoked session, then reloads the live `ApiKey`
   row. Tenant and scopes are derived from that row on every request; key expiry,
   revocation, or scope changes therefore take effect without re-login. Existing
   scopes remain authoritative: `candidates:read` authorizes the accepted
   presentation-only identity view; job/ranking routes additionally use existing
   `jobs:read` and `evaluations:write` scopes. No new scope is invented.
5. **Cookie and logout:** the cookie is `HttpOnly`, `SameSite=Lax`, `Path=/ui`,
   and `Secure=true` by default. Loopback development must explicitly set
   `MEYAR_UI_COOKIE_SECURE=false`. Logout is authenticated, CSRF-protected,
   revokes the server row, clears the cookie, and never reactivates a session.
6. **CSRF and browser policy:** authenticated POSTs require a constant-time
   checked token derived from the session's server-owned CSRF material and raw
   session cookie. `/ui` responses receive restrictive same-origin CSP,
   `nosniff`, `no-referrer`, frame denial, and `Cache-Control: no-store`.
7. **Privacy boundary:** routes give templates narrow Pydantic presentation
   models, never unrestricted ORM/domain objects. All candidate, identity, job,
   query, evidence, and model-derived text remains untrusted display data;
   Jinja auto-escaping is explicit and no `safe`/`Markup` bypass is used.
   URLs contain only non-sensitive operational state and UUIDs; no candidate
   data or credential is placed in localStorage/sessionStorage/IndexedDB.
8. **Service authority:** natural-language POSTs delegate unchanged to Slice 9
   `plan_and_search_candidates`; every non-executable planner outcome stays
   fail-closed. JD forms pin an exact `JobCriteriaVersion` UUID and delegate to
   Slice 10 `rank_candidates_for_job`. The UI preserves both backend result
   orders and canonical scores exactly. It contains no planner parsing, search
   filters, scoring formula, fit-tier order, or ranking logic.
9. **Identity stays presentation-only:** current `CandidateIdentityVersion`
   (maximum version number, tenant-scoped) is resolved only after search/rank
   authority has returned. Changing name/contact cannot affect eligibility,
   relevance, score, or rank. Email/phone are limited to candidate detail.
10. **Boundary of completion:** Slice 11 adds HTML routes under `/ui`; it does
    not finalize the `/api/v1` REST/OpenAPI contract, provide enterprise SSO,
    deliver arbitrary raw CV bytes, or claim final security/target-Mac
    acceptance. Those remain governed by later approved slices.

**Why:** HR needs a usable internal browser interface now, while the accepted
Slice 8–10 services must remain the only authorities for planning, search,
evaluation, scoring, and ranking. A small server-rendered surface minimizes the
browser attack/data-persistence boundary and avoids a separate frontend policy
implementation.
**Reversibility:** Additive and narrow. The browser-session table can be removed
by its migration downgrade; a future bank identity provider can replace only
the login bridge while preserving `/ui` view models and backend service
authority.

## D-017 — meyar-score-v1 deterministic scoring and ranking

**Date:** 2026-08-23
**Decision:** Slice 10 adds a deterministic numeric score and batch ranking on
top of the accepted Slice 5 criterion and fit-band policy:
1. **Evaluation date is explicit provenance.** Every new scored evaluation
   requires and persists the complete `evaluation_as_of_date` (`YYYY-MM-DD`).
   Ongoing employment resolves only to `evaluation_as_of_date.year`; the
   evaluation/scoring policy reads no wall clock. This is a control and
   reproducibility correction, not a change to criterion meaning, so the
   evaluation policy remains `meyar-policy-v1` rather than silently relabeling
   historical semantics.
2. **Exact status factors:** `MATCH=1.0`, `PARTIAL_MATCH=0.5`, and
   `NOT_MATCHED`/`UNKNOWN`/`CONFLICTING_EVIDENCE`/
   `MANUAL_REVIEW_REQUIRED=0.0`. The mapping is exhaustive; a future unknown
   status raises `UNKNOWN_CRITERION_STATUS` rather than inheriting a default.
3. **Exact Decimal formula:** each stored float weight becomes
   `Decimal(str(weight))`; all subsequent arithmetic remains Decimal.
   `raw_score = 100 * Σ(weight * factor) / Σ(weight)`, with only the final
   result quantized to `0.01` using `ROUND_HALF_UP`. Persisted/ranked scores
   remain `NUMERIC(5,2)`/Decimal, never float.
4. **Zero weights:** individual zero-weight criteria are valid and still run.
   A zero-weight `MUST_HAVE` still controls the fit/gate result. New criteria
   versions must contain at least one positive weight. Readable historical
   all-zero versions fail scoring as `ZERO_TOTAL_CRITERION_WEIGHT`; batch
   validates this before candidate iteration and never fabricates a score.
5. **Score and gate are separate.** The score uses declared weights only—no
   hidden MUST_HAVE multiplier. Existing `meyar-policy-v1` fit bands remain
   authoritative; the score never clears a gate, converts uncertainty into
   absence, or produces HIRE/REJECT decisions. Humans decide.
6. **Ranking policy:** explicit tiers are `STRONG_MATCH=0`,
   `POTENTIAL_MATCH=1`, `MANUAL_REVIEW_REQUIRED=2`, and
   `INSUFFICIENT_EVIDENCE=3`. Sort is `(fit_tier ASC, numeric_score DESC,
   candidate_id.int ASC)`. Enum order, SQL row order, timestamps, and identity
   never participate.
7. **Batch candidate set:** batch reads the tenant candidate library directly,
   with exactly one current `CandidateProfileVersion` (max version number) for
   each active candidate. It never needs a semantic/vector shortlist and never
   falls back to a stale profile. Missing/non-completed current profiles are
   reported by deterministic skip reason counts.
8. **Immutable idempotent provenance:** exact identity is `(tenant_id,
   candidate_profile_version_id, job_criteria_version_id,
   evaluation_as_of_date, policy_engine_version, scoring_policy_version)`.
   The service reuses an existing immutable Evaluation; a PostgreSQL partial
   unique index provides the concurrency backstop, and an insert race is
   handled through a savepoint followed by exact re-fetch.
9. **Legacy history is not fabricated.** The four additive Evaluation fields
   (`evaluation_as_of_date`, `numeric_score`, `scoring_policy_version`,
   `score_explanation`) are nullable. Migration does not backfill them, so old
   rows retain NULL score provenance and unchanged criterion-result JSON.
10. **Explanation is deterministic and recomputable.** JSON stores canonical
    decimal strings for criterion weight/factor/weighted points and aggregate
    totals, plus status/reason code, location-only evidence references,
    manual-review state, fit band, policy versions, date, and immutable input/
    Evaluation IDs. It omits evidence quotes, identity, CV text, embeddings,
    semantic similarity, LLM output, and hidden reasoning.
11. **Privacy/AI boundary:** `CandidateIdentity` is absent from scoring and
    ranking. No scoring/ranking module imports an LLM, Ollama, embedding,
    pgvector, search planner, or semantic-search service. Tenant-scoped input
    resolution and tenant-scoped current-profile selection remain mandatory.
12. **Audit/interface:** `CANDIDATE_SCORE_COMPUTED` records safe provenance,
    score/fit, and `reused`; `JOB_BATCH_RANKED` records safe policy/count/skip
    metadata. `meyar evaluate` now requires `--as-of-date`; `meyar rank-job`
    accepts tenant, exact criteria-version id, and date. Both print UUID/
    policy/contribution data only. No REST or UI contract is added.

**Why:** The official task requires a reviewable 0–100 compatibility score and
batch ordering while preserving Slice 5's uncertainty, gate, audit, privacy,
and human-decision boundaries. Explicit date provenance also closes the
accepted engine's last policy-time dependency.
**Reversibility:** Additive, versioned, and non-destructive. A material future
formula or rank-policy change requires a new scoring-policy version; existing
rows remain attributable to `meyar-score-v1` and are never rewritten.

## D-016 — meyar-search-planner-v1: strict local natural-language planning

**Date:** 2026-08-23
**Decision:** Slice 9 introduces `meyar-search-planner-v1`, a strict,
local-only interpretation layer in front of the accepted Slice 8 search
engine:
1. **The LLM interprets; Slice 8 searches and ranks.**
   `plan_candidate_search` produces a typed `SearchPlanResult` and never
   imports/queries candidate, profile, identity, or embedding repositories.
   Its database session is used only for a PII-safe `AuditEvent`.
   `plan_and_search_candidates` is a thin plan→validate→existing
   `search_candidates` delegate; it contains no filtering, pgvector,
   relevance, ranking, or tie-break implementation.
2. **The model-controlled boundary is `PlannerDraft` with
   `extra="forbid"`.** It may express only the existing Slice 8
   required/preferred filters, a bounded professional semantic query, an
   explicit requested result count, and typed unsupported reason codes.
   It has no tenant id, `as_of_date`, mode, embedding configuration,
   weights, policy versions, identity, SQL, database query, score, or
   candidate-result fields. Unknown/nested fields fail parsing; they are
   never silently dropped.
3. **Mode is deterministic:** structured content only →
   `STRUCTURED_ONLY`; semantic content only → `SEMANTIC_ONLY`; both →
   `HYBRID`. Natural-language input alone never implies semantic search.
   The resulting object must pass the existing strict
   `CandidateSearchRequest` validation.
4. **Meaning is preserved or rejected, never weakened.** Explicit MVP
   request-to-draft guards verify supported structured values occur in the
   request, model-produced total-experience numbers/result counts/semantic
   content are supported by the request, and clear required/preferred
   markers are not reversed. The explicit Azerbaijani MVP marker policy
   recognizes common copular forms such as `mütləqdir` without claiming
   general morphological understanding. A mandatory conceptual requirement
   cannot be reduced to soft semantic relevance. Skill-specific duration
   (for example “5 years of Java”) is not converted to Java + five years
   total experience. Slice 8 filters
   language existence only, so B2/C1/etc. proficiency is rejected rather
   than reduced to a language-name filter. Identity, salary, location,
   project-duration, custom search-weighting, and prompt/SQL instructions are
   typed unsupported outcomes. A material unsupported aspect blocks the
   whole plan; it is never silently dropped before partial execution. This
   is intentionally a small explicit MVP guard, not a general NLP
   equivalence engine.
5. **Protected criteria are deterministic before and after the LLM.** The
   existing `find_prohibited_term` authority checks the raw request before
   any model call and all execution strings after parsing. The shared
   denylist includes Azerbaijani equivalents needed by the internal HR
   surface and an explicit root-plus-allowed-suffix policy for common MVP
   inflections. It never uses unrestricted prefix matching (`yaşdan` is an
   age form; `yaşıl` is not). The LLM cannot rephrase a protected request
   into a semantic query to evade policy; a rejected plan never invokes
   Slice 8.
6. **Trusted runtime owns execution configuration.** The caller supplies
   tenant id unchanged, an explicit reference date, and the active
   `EmbeddingSearchConfig`. Deterministic conversion injects `as_of_date`
   only when experience filtering is present, injects embedding provenance
   only for semantic/hybrid mode, and uses the accepted `meyar-search-v1`
   0.5/0.5 weights. None can be emitted or overridden by the model. The
   natural-language default result limit is the existing Slice 8 default
   20; explicit counts must match the request and remain within 1–100.
7. **Local LLM only, with one repair.** The existing `LLMProvider` gains a
   typed planner operation; `OllamaLLMProvider` remains loopback-enforced,
   requests strict JSON schema output, and parses directly to
   `PlannerDraft`. One initial attempt plus one schema-repair attempt is
   allowed; there is never a third. Provider unavailable/timeout,
   persistent malformed output, provenance mismatch, and policy rejection
   remain distinct typed outcomes. No cloud fallback or agent framework
   exists.
8. **Provenance is explicit but reproducibility claims are bounded.** The
   policy/prompt/schema versions are `meyar-search-planner-v1`,
   `search-planner-prompt-v1`, and `search-plan-schema-v1`.
   `LLMResultProvenance` records provider, actual response model name, and
   model revision where available; Ollama currently reports no stable
   revision, represented explicitly as `""`. Actual call metadata must
   match configured provider metadata. LLM text generation is not claimed
   bit-for-bit deterministic; pre/post policy, draft→request conversion,
   trusted configuration injection, and Slice 8 execution for fixed
   inputs/state are deterministic.
9. **User text and raw output stay private.** The prompt JSON-delimits the
   HR request as untrusted data and never requests chain-of-thought. Audit
   events `SEARCH_PLAN_CREATED`/`REJECTED`/`FAILED` retain only request
   SHA-256, executable/outcome, safe reason codes, versions, provider/model
   metadata, attempt count, final mode, and limit. Raw request, semantic
   query, raw/repaired model response, prompt, identity, CV data, vectors,
   and hidden reasoning are never audited/logged. Plans remain ephemeral;
   no migration/table is added.
10. **CandidateIdentity remains presentation-only.** It is absent from the
    draft, deterministic conversion, planner service, Slice 8 request, and
    ranking. Name/email/phone requests are non-executable professional-
    search outcomes, never semantic suitability criteria.
11. **Interface:** `meyar plan-search --tenant-id ... --query ...
    --as-of-date YYYY-MM-DD` is plan-only by default; `--execute` delegates
    to Slice 8 and reuses its PII-safe result shape. Exit 2 is rejected/
    invalid intent, 3 is local planner-provider failure, and 4 is search/
    infrastructure failure. No REST/UI contract is added.

**Why:** Natural-language input creates a new trust boundary. Keeping model
authority narrow and converting through explicit, deterministic policy
preserves Slice 8's hard eligibility, provenance, tenant, and ranking
invariants while providing the planned internal HR interaction flow.
**Reversibility:** Application-only and fully versioned. No persistent
schema or dependency was added; prompt/policy changes require version bumps,
and Slice 8 remains independently callable with its original strict request.

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
   `EmbeddingProvider` instance the caller supplies. Two independent
   checks gate this, both required: (a) before calling `.embed()`, the
   service verifies `embedding_provider.provider_name/model_name/
   model_revision` (the provider object's own declared attributes)
   match `embedding_config`, rejecting a mismatch as
   `EMBEDDING_PROVIDER_CONFIG_MISMATCH`; (b) **after** calling
   `.embed()`, the service also verifies the ACTUAL returned
   `EmbeddingResult.provider/model_name/model_revision` — not just the
   provider object's static attributes — match `embedding_config`,
   rejecting a mismatch as `EMBEDDING_RESULT_PROVENANCE_MISMATCH`. Check
   (b) exists because a provider whose declared attributes match config
   could still, due to a bug or a multi-model routing implementation,
   perform the actual inference call against a different model — same
   vector dimensions do not prove model compatibility. `""`
   (`MODEL_REVISION_UNKNOWN`) matches only an explicit `""` on both
   checks. The query vector itself is independently validated
   (non-empty, every value finite — no NaN/±Inf — via
   `meyar.search.policy.is_valid_query_vector`, exact expected
   dimension, non-zero norm) regardless of which `EmbeddingProvider`
   implementation is in use, not only `OllamaEmbeddingProvider`.
   `STRUCTURED_ONLY` never calls `embedding_provider.embed()` — proven
   by a regression test using a provider configured to raise if invoked
   (`test_structured_only_never_calls_embedding_provider`).
6. **Only current, exactly-compatible embeddings participate — including
   current CANONICAL SOURCE-HASH freshness, not just profile-version
   freshness.** A compatible embedding is one whose
   `candidate_profile_version_id` equals the candidate's CURRENT
   `CandidateProfileVersion` (never a superseded/stale version) AND
   whose provider/model_name/model_revision/serializer_version/
   embedding_dimensions all exactly match the active
   `EmbeddingSearchConfig` AND whose `source_sha256` equals the exact
   hash the CURRENT canonical professional serializer
   (`build_professional_embedding_text` + `compute_source_sha256`,
   recomputed from the candidate's current `profile_content` at search
   time) produces right now. Slice 7's seven-field embedding-row
   uniqueness intentionally allows multiple historical rows for the
   same (tenant, profile version, provider, model, revision, serializer
   version) differing only by `source_sha256` — e.g. a source-text
   change under an unchanged `serializer_version`. Selecting among
   those by profile-version match alone is insufficient and was an
   acceptance-audit-caught defect (see the correction note below):
   `candidate_embedding_repo.search_compatible_embeddings` now takes a
   `{profile_version_id: expected_source_sha256}` mapping and filters
   with a `(candidate_profile_version_id, source_sha256)` SQL tuple
   match — never `ORDER BY created_at`, `MAX(id)`, or "latest row" as a
   freshness substitute (freshness is provenance-based, not
   chronology-based). Combined with the DB's own seven-field unique
   constraint, this guarantees at most one row can match per candidate,
   independent of SQL row-return order. The dimension/config/hash
   filter runs in an inner SQL subquery so pgvector's `cosine_distance`
   (`<=>`) operator is only ever evaluated over already-compatible rows.
   A candidate lacking a current, hash-fresh, compatible embedding is
   excluded from `SEMANTIC_ONLY`/`HYBRID` results (never assigned a
   fabricated semantic score of 0) — tracked in
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

**Post-acceptance-audit correction (same date, before merge):** An
independent acceptance audit of the initial implementation reproduced
two defects, both fixed on the same PR/branch before merge, addressed
by points 5 and 6 above: (a) the service validated only the
`EmbeddingProvider` object's declared static attributes against
`embedding_config`, never the actual `EmbeddingResult`'s own
provider/model_name/model_revision — fixed by the second check in point
5; (b) `search_compatible_embeddings` filtered only by profile-version
id and the six-field config, so when a candidate had multiple embedding
rows differing only by `source_sha256` (a state Slice 7 explicitly
allows), the "current" one was selected by arbitrary/unordered SQL
row-return order rather than by provenance — independently proven to
flip between the current and a stale embedding purely by reversing
insertion order — fixed by point 6's `(profile_version_id,
source_sha256)` tuple match. Both are covered by dedicated regression
tests (`test_search_semantic_provenance.py`).
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

## D-019 — Final internal REST API and OpenAPI contract

**Date:** 2026-08-23
**Decision:** Slice 12 finalizes the `/api/v1` presentation layer over the
already-accepted domain/services, without introducing a second business-logic
implementation:
1. **Additive surface.** All 13 pre-existing routes (health, usage, jobs,
   candidates, documents) are unchanged. Five new routes wrap accepted
   services only: `POST /api/v1/search` (Slice 8), `POST
   /api/v1/search/natural-language` (Slice 9), `POST /api/v1/jobs/{job_id}
   /criteria/{version_number}/score` (Slice 5/10), `POST /api/v1/jobs
   /{job_id}/criteria/{version_number}/rank` (Slice 10), and `GET
   /api/v1/candidates/{candidate_id}/detail` (Slice 7/11 identity/profile
   presentation). No route computes a score, rank, or search result itself.
2. **OpenAPI Bearer security scheme.** `meyar.core.auth.get_current_tenant`
   previously read the `Authorization` header manually via
   `request.headers.get(...)`, so the generated OpenAPI had no
   `securitySchemes` entry — Swagger's Authorize control had nothing to bind
   to. It now resolves the same key through FastAPI's `HTTPBearer`/
   `Security()` (`bearer_scheme`, `scheme_name="ApiKeyBearer"`,
   `auto_error=False`), with byte-identical 401/403 observable behavior —
   verified by the existing `test_api_key_auth.py`/`test_tenant_isolation.py`
   suites passing unchanged. No OAuth2/Basic scheme is introduced; `/ui`'s
   BrowserSession/CSRF cookie mechanism remains fully separate and is never
   the API's auth boundary.
3. **Scope reuse, not invention.** All six scopes seeded on every key since
   Slice 1 (`jobs:read`, `jobs:write`, `candidates:read`, `candidates:write`,
   `evaluations:read`, `evaluations:write`) are sufficient. Both mutating
   evaluation routes require `evaluations:write`: score may persist (or
   idempotently reuse) an `Evaluation` row via `evaluate_and_score_candidate`,
   so it requires the same write authority as the rank endpoint, which
   reuses `evaluations:write` matching the scope the Slice 11 UI already
   required for the same operation. An initial draft of this slice gated
   score behind `evaluations:read`; a post-merge authorization audit found a
   read-only credential could trigger `Evaluation` persistence through it,
   and the scope was corrected to `evaluations:write` before acceptance.
   `evaluations:read` remains seeded and reserved for genuinely read-only
   evaluation retrieval routes if/when one is added — it is not repurposed.
4. **External DTO boundary.** New request/response models live in
   `meyar.schemas.api_search`/`api_evaluation`/`api_candidate`, distinct from
   internal service schemas, `extra="forbid"` throughout (matching the
   project-wide convention). `CandidateSearchRequest.embedding_config` and
   its structured/semantic weights are deliberately absent from the external
   request DTO — the route injects trusted server-side values
   (`get_embedding_search_config()`, `DEFAULT_STRUCTURED_WEIGHT`/
   `DEFAULT_SEMANTIC_WEIGHT`) so a client can never smuggle its own embedding
   provenance or hybrid weighting into the ranking path.
5. **Fail-closed NL search preserved over REST.** All seven
   `PlannerOutcome` values (`EXECUTABLE`, `PROHIBITED_REQUEST`,
   `UNSUPPORTED_SEMANTICS`, `AMBIGUOUS_REQUEST`, `MALFORMED_MODEL_OUTPUT`,
   `PLANNER_PROVIDER_FAILURE`, `VALIDATION_FAILURE`) remain distinguishable
   in the `200` response body — no collapse to a generic error. `503` is
   reserved for a genuinely unavailable embedding/database dependency (an
   exception path), never for a normal typed non-executable planner outcome.
6. **Decimal/date/UUID serialization.** `numeric_score` in both the score and
   rank responses always uses the already-canonical `ScoreExplanation`
   `".2f"`-formatted decimal string (e.g. `"75.00"`) — the route reuses
   `explanation.numeric_score`/`item.score_explanation.numeric_score` rather
   than reformatting the underlying `Decimal` itself, avoiding the float-like
   default Pydantic/JSON encoding of `Decimal`. Dates stay ISO 8601;
   UUIDs stay canonical string form — no change from existing convention.
7. **No wall-clock default, preserved idempotency/ordering.**
   `evaluation_as_of_date` is a required field on both the score and rank
   request DTOs — never defaulted to "today." Score requests replay the
   underlying service's exact-provenance idempotency (`reused=true` on an
   identical repeat call, same `evaluation_id`, no duplicate row). Rank
   responses preserve the batch service's exact backend order
   (`fit_tier, -numeric_score, candidate_id.int`) — the route never re-sorts.
8. **Truthful `/usage`.** The previous hardcoded `candidates_count: 0,
   evaluations_count: 0` placeholder is replaced with real tenant-scoped
   counts (`count_candidates_for_tenant`, `count_evaluations_for_tenant`) —
   a fresh tenant genuinely has zero of both; activity changes the count on
   the next call; no cross-tenant aggregation.
9. **Offline Swagger UI.** FastAPI's default `/docs` loads
   `swagger-ui-bundle.js`/`swagger-ui.css` from `cdn.jsdelivr.net` at
   runtime — a real gap for bank-controlled/offline infrastructure.
   `docs_url=None` disables the default route; a manual `GET /docs`
   (`meyar.api.docs`) serves `get_swagger_ui_html()` pointed at
   locally-mounted assets from the `swagger-ui-bundle` PyPI package (a
   small, maintained package that vendors Swagger UI's static assets — no
   Node/npm/package.json introduced), mounted at `/docs-assets`. Verified
   (`test_openapi_contract.py`) that the rendered `/docs` HTML contains no
   `cdn.jsdelivr`/`unpkg`/`cdnjs`/external URL, and that the local asset
   routes return `200`. `/openapi.json` itself already had zero network
   dependency and is unchanged. Both the docs route and asset mount use
   `include_in_schema=False`, same as `/ui/*`.
10. **UI stays excluded.** `/ui/*` (already `include_in_schema=False` since
    Slice 11) continues to be absent from `/openapi.json` — the OpenAPI
    schema represents the product REST API only, never BrowserSession/CSRF/
    login-form internals.
11. **No CORS, no rate limiting added.** The application has no CORS
    middleware before or after this slice — the current same-origin `/ui` +
    internal-API deployment does not need one; a specific internal
    cross-origin client would require an explicit allowlisted-origin
    decision later, never a wildcard. Rate limiting remains unimplemented
    (`Settings.rate_limit_per_minute` exists but is unenforced) and is
    explicitly deferred to Slice 13 security acceptance, not silently
    claimed here.
12. **Raw CV delivery stays out of scope.** No endpoint serves original CV
    bytes, a storage key, or a filesystem path — candidate detail exposes
    only extracted structured facts, evidence locations, and parse
    metadata, matching the pre-existing `docs/STATUS.md` "Original file
    reference" row (DONE at the storage layer, browser delivery
    intentionally not added).

**Why:** The official task (`AI-PROJ-CV-01`) requires a usable internal REST
API with Swagger documentation for approved internal HR clients/systems.
Slice 11 (D-018) proved the domain services end-to-end through a
server-rendered UI but left the REST presentation of search/scoring/ranking
unfinished, and left a functional-but-undocumented auth mechanism (no OpenAPI
security scheme) and a placeholder `/usage` metric that would mislead a real
integrator. Fixing all three in one slice — rather than deferring OpenAPI
correctness or `/usage` truthfulness further — keeps the eventual Slice 13
security/DoD acceptance pass from having to re-audit a REST surface that
silently changed shape after "final."
**Reversibility:** Fully reversible. The five new routes are additive and can
be removed/changed independently of the underlying services they wrap. The
`get_current_tenant` refactor is a drop-in replacement with an identical
authentication contract (test-verified). `swagger-ui-bundle` is a single,
small, easily-replaceable dependency (`backend/uv.lock`) — switching to a
different offline-asset strategy later requires no change to any route or
schema. The `/usage` fix only changes two integer values in an existing
response shape; no API consumer contract is broken.

## D-020 — MVP security and acceptance boundary (Target-Mac gate PENDING)

**Date:** 2026-08-28
**Status:** Partial/interim record. This is NOT a declaration of MVP
completion — the Target-Mac benchmark gate is explicitly pending (see
below) and this decision must be revisited and finalized once it closes.

**Decision:** Records the exact tested security/acceptance boundary MEYAR
can currently claim, after Slice 13 implementation pass 1 (PR #22).

1. **Tested reference hardware.** Mac mini M4 Pro (12-core CPU, 16-core
   GPU, 24 GB unified memory, 512 GB SSD) is the owner-confirmed MVP
   *reference* configuration — not a permanent platform lock-in. Future
   deployment to another Mac, a Mac Studio, or bank-controlled
   Linux/NVIDIA infrastructure remains architecturally possible subject to
   fresh dependency/performance/model validation on that platform. **The
   benchmark has not yet been executed on this reference hardware** — see
   item 15.
2. **Local-only candidate-data AI boundary.** Every LLM/embedding call is
   made through `meyar.llm.LLMProvider`/`meyar.embedding` abstractions,
   both of which reject any non-loopback `base_url` at construction time
   (`require_loopback_url`). Formally verified this pass: a deterministic
   runtime guard (`test_no_exfiltration.py`) proves a representative
   extract+embed workflow, run through the real provider classes, never
   attempts a non-loopback network request, plus a negative control
   proving the guard itself works. Claim scope: validated
   application-level, tested-configuration behavior — not a
   physical-firewall or network-layer guarantee.
3. **Original-CV access boundary.** `GET /ui/candidates/{candidate_id}
   /documents/{document_id}/original` — UI-only, `candidates:read`
   scope via the existing live-revalidating `BrowserSession`, tenant +
   candidate + document ownership verified server-side, storage_key
   never client-supplied, synthetic filename only, safe 404 for
   foreign/mismatched resources. No REST byte-serving route exists.
4. **API/UI auth boundaries.** Bearer + scopes + tenant-derived-from-key
   for REST; `BrowserSession` (8h fixed TTL, live API-key revalidation
   every request, Secure/HttpOnly/SameSite=Lax cookie, HMAC CSRF) for
   `/ui`. Both pre-date this decision and are unchanged by it.
5. **Score/rank write-scope rule.** `evaluations:write` is required for
   both scoring and ranking; `evaluations:read` alone is rejected (403).
   No read-only scope carries a mutation capability anywhere in the API.
6. **No-exfiltration evidence.** See item 2.
7. **Backup/restore tested scope.** PostgreSQL (`pg_dump`/`pg_restore`)
   and document storage (`tar`) must be backed up and restored together;
   an executed synthetic proof (`backend/scripts/backup_restore_acceptance.py`)
   confirms row counts, relationships, byte-identical original-CV
   restoration, and exact-provenance score reuse (`reused=true`) survive
   the cycle, entirely against disposable, non-production data. Backup
   *scheduling* policy (frequency, retention) remains an explicit,
   undecided deployment/business decision (`docs/SECURITY_PRIVACY.md`).
8. **Multilingual evidence scope.** Azerbaijani/Russian/English synthetic
   fixtures prove the existing local parser (Unicode-transparent by
   construction) and extraction/identity pipeline (Pydantic validation +
   Postgres JSON persistence) round-trip all three languages unchanged,
   via `FakeLLMProvider`. This is pipeline/schema evidence, not a
   real-model per-language quality claim — real-model sampling, where
   practical, is a Target-Mac-run activity (item 15), supplementary to,
   not a replacement for, this evidence.
9. **Deterministic vs. AI reproducibility boundary.** Score/rank/
   evaluation are exact-provenance deterministic: an identical
   `(candidate_profile_version_id, job_criteria_version_id,
   evaluation_as_of_date)` tuple always reuses the same `Evaluation`
   (`reused=true`), never recomputes a different value — proven again
   this pass surviving a full backup/restore cycle. Extraction and NL
   planning are versioned and provenance-pinned (model name+revision,
   `request_sha256`, schema/prompt versions recorded) but never claimed
   bit-for-bit identical across LLM calls.
10. **Deployment encryption responsibility.** Application-layer
    encryption-at-rest is not implemented in MVP; `LocalFilesystemStorage`
    relies on host/disk-level protection. Unchanged by this pass — a
    deployment-owner responsibility, not a new gap.
11. **OCR — explicitly deferred, non-MVP.** Per D-007, unchanged.
    Scanned-image PDF support is out of MVP scope.
12. **Git-infrastructure limitation.** The development remote
    (`a-r3/meyar`) is personal/temporary (D-012); migration to an
    official bank-owned remote is pending owner action, preserving full
    history when it happens. Organizational, not a software gap, not an
    MVP blocker.
13. **Known technical debt (non-blocking).** `ApiCandidateDetailResponse`
    reuses the UI-owned `CandidateDetailView` model (reviewed this pass —
    already excludes storage paths/raw bytes/prompts/vectors by
    construction; no security/contract risk found) — classified P3
    post-MVP, no action taken.
14. **No blanket security claim.** This decision records what has been
    tested, under the tested configuration, as of this pass. It is not
    "bank secure," "fully secure," or "production secure" in any
    unqualified sense — each claim above is scoped to what was actually
    exercised and how.
15. **Exact limitation — Target-Mac gate PENDING.** The benchmark
    (`backend/scripts/target_mac_benchmark.py`) has not been executed on
    the confirmed Mac mini M4 Pro reference hardware; the current
    development machine (Intel x86_64 laptop, Linux) is confirmed not to
    match it. No production LLM or embedding model is approved. This is
    the sole remaining mandatory blocker to MVP closure — M5 and issue
    #20 remain open until it closes and this decision is updated to
    record the actual result (or the finding that measured behavior is
    impractical, per the owner's 2026-08-28 acceptance-policy decision,
    which sets no invented latency threshold).

**Why:** Slice 13's official task requires a Definition-of-Done boundary
statement before MVP can be considered for closure. Recording it now, with
the pending item explicitly flagged, prevents two failure modes: silently
treating Pass-1 software completion as full MVP acceptance, and losing
track of exactly what has vs. has not been validated once the Target-Mac
run eventually happens.
**Reversibility:** This entry is expected to be revised (not superseded by
a new D-0xx) once the Target-Mac benchmark executes and a production model
decision is recorded — item 15 and the model-approval status are the parts
expected to change; items 1–14 record already-tested, stable boundaries.
