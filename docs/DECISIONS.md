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

## D-021 — Slice 14 folder reconciliation and automatic candidate processing

**Date:** 2026-08-30
**Decision:** Slice 6's folder indexer (D-013) already delivered discovery,
secure canonical ingestion, and idempotent per-path tracking, reusing
`ingest_candidate_document` unchanged. It stopped there: a folder-imported
`CandidateDocument` never automatically continued through profile
extraction, identity extraction, or embedding — the same gap that applies
to direct upload, since `meyar.services.candidate_document_service
.ingest_candidate_document` has exactly one downstream-processing
convention across the whole app: an explicit, separate operator/CLI step.
Slice 14 closes this gap for the folder path only, without creating a
second ingestion pipeline:

1. **Existing folder scanner/indexer reused unchanged for
   discovery/ingestion.** `meyar.ingestion.folder_scanner` and
   `meyar.services.folder_indexer_service.index_folder` are extended
   in place (a new `stability_window_seconds` parameter, a cross-path
   dedup lookup inside `_handle_new_file`), never duplicated. The new
   orchestration lives in a sibling module,
   `meyar.services.folder_reconciliation_service`, which calls
   `index_folder` and then drives whichever of
   `extract_candidate_profile` / `extract_candidate_identity` /
   `embed_candidate_profile` (Slice 4/7, unchanged) each pending
   candidate still needs.
2. **Periodic reconciliation, not a filesystem watcher.** A watcher
   (inotify/FSEvents/`watchdog`) loses events across a process restart,
   is unreliable over a network/shared filesystem a bank-controlled
   folder plausibly is, and needs an OS-specific dependency to work on
   both the reference macOS host and a future Linux host. A periodic,
   idempotent full re-scan (the same design Slice 6 already assumed)
   needs none of that: it re-derives truth from the filesystem plus
   `folder_indexed_files` every run, has no persisted watcher state to
   lose, and adds zero new dependencies. The application owns one
   repeatable CLI command (`meyar reconcile-folder`); deployment
   infrastructure (macOS `launchd`, Linux `systemd` timer/cron) owns
   scheduling — see `docs/DEPLOYMENT_AND_OPERATIONS.md`.
3. **File-stability window: 60-second default, stdlib only.**
   `MEYAR_FOLDER_STABILITY_SECONDS` (default 60). A discovered file
   whose mtime is newer than `now - window` is skipped for that scan
   only — never marked FAILED, and (if already tracked) never
   tombstoned MISSING, since it is simply not observed this pass, not
   actually gone. No atomic-rename producer convention is required
   (MEYAR does not control how the bank's own systems write into the
   folder), no OS-specific dependency, no busy-wait inside the scan.
   **Accepted bounded limitation (independent-audit item, 2026-08-31):**
   mtime is read immediately before the bytes, so a writer still actively
   appending to a file *after* it happens to pass the stability check can
   still produce a torn read on that pass. This is not corruption-prone —
   a torn read either fails MIME/parse validation (already retried
   automatically on the next scan) or simply hashes differently from a
   later, genuinely stable read (handled as an ordinary `changed` file,
   not silently accepted as final). No second stat/hash consistency
   check is added for this pass; the window plus the existing
   retry-on-next-scan behavior is the accepted MVP mitigation, not a
   claim of atomicity.
4. **Exact-content (SHA-256) dedup is document-content dedup only,
   never human-identity resolution.** Same relative path + same bytes:
   unchanged Slice 6 no-op. Same relative path + changed bytes: new
   immutable `CandidateDocument`/`CanonicalDocument` version under the
   *same*, path-derived `Candidate` identity (D-013 #2, unchanged) —
   Slice 14 additionally ensures a new profile/identity/embedding is
   generated for that new document so search/scoring is never left
   stale on the superseded content. Different relative path + identical
   bytes, same tenant: the new path is linked to the already-ingested
   `candidate_id`/`candidate_document_id` (tenant-scoped lookup by
   `sha256_hash` in `find_indexed_file_by_content_hash`) instead of
   minting a duplicate `Candidate` and a duplicate stored copy — never
   cross-tenant. Different bytes at different paths are **never**
   linked, even when plausibly the same person: MEYAR has no
   candidate-identity matching/merge capability, and Slice 14
   deliberately does not invent one.
5. **Downstream readiness is derived from existing provenance — no new
   migration.** `CandidateProfileVersion` and `CandidateIdentityVersion`
   both already carry `candidate_document_id` directly (established in
   Slice 4/7, unchanged); a `COMPLETED` row for the *current* document
   id is sufficient to know that stage is done, with no redundant
   processing-status column. Embedding readiness reuses
   `embed_candidate_profile`'s own idempotency (Slice 7, D-014) — it is
   always safe/cheap to call once a profile is `COMPLETED`. New
   read-only lookups (`get_latest_profile_version_for_document`,
   `get_latest_identity_version_for_document`) were added to the
   existing repositories; no schema change, no Alembic revision.
6. **Per-candidate commit boundary inside
   `process_pending_candidates` — a deliberate, narrow deviation from
   the "caller controls the transaction boundary" convention used
   elsewhere (e.g. `index_folder`).** A bounded batch loop over
   independent candidates needs its own commit boundary for restart
   safety: each candidate's outcome (success, or a caught/logged
   failure after `db.rollback()`) is committed immediately, so a crash
   or an unexpected exception mid-batch only ever loses that one
   candidate's uncommitted work — never previously-completed
   candidates in the same run, and never candidate B's turn because
   candidate A failed. `index_folder` itself is unchanged in this
   respect; the initial scan is committed once, immediately before
   downstream processing starts, by the new `reconcile_folder` entry
   point.
7. **Sequential processing, bounded by `--limit`, no concurrency yet.**
   `meyar reconcile-folder --tenant-id --root [--limit N]` serves both
   initial bulk import and repeatable reconciliation in one command.
   `--limit` bounds how many *not-yet-ready* candidates are attempted
   per invocation (already-ready candidates are free and never count
   against it) so a large backlog is worked off incrementally rather
   than forcing one unbounded sequential local-Ollama run. Discovery/
   ingestion (`index_folder`) itself is never bounded by `--limit` — a
   full scan/hash of the folder always runs first; only the downstream
   extraction/identity/embedding stage is bounded. No bounded-
   concurrency primitive is added — `docs/MASTER_SPEC.md` §11 already
   frames local Ollama as a single-worker resource; real throughput
   evidence is a Target-Mac-benchmark question (D-020), not invented
   here.
   **Fairness fix (independent-audit item, 2026-08-31):** the initial
   implementation attempted not-yet-ready candidates in whatever order
   the database happened to return them, which — combined with a small
   `--limit` and no explicit ordering — let a persistently-failing
   candidate consume the entire budget on every run, indefinitely
   starving a candidate that had never been attempted. Fixed with two
   changes, both derived from existing state, no new schema: (a)
   `list_folder_indexed_files` now orders by `relative_path` for
   deterministic iteration; (b) within one `process_pending_candidates`
   call, not-yet-ready candidates are sorted so a document with **no**
   existing profile-extraction attempt (`get_latest_profile_version_for
   _document` returns `None`) is always processed before a document that
   already has one (any status) — this ordering is recomputed fresh from
   provenance on every call, so as soon as a persistently-failing
   candidate has one recorded failed attempt, a genuinely-untried
   candidate is prioritized ahead of it on the next run. Regression:
   `test_limit_fairness_prevents_permanent_starvation`.
8. **New `FOLDER_FILE_DUPLICATE_CONTENT_LINKED` and
   `FOLDER_RECONCILE_CANDIDATE_FAILED` audit event types**, both
   ids/counts/codes only, verified against the existing privacy guard
   (`meyar.services.audit_repo._assert_metadata_is_privacy_safe`).
9. **Single-active-reconciler operational model — stated explicitly, not
   enforced by the database.** The supported MVP deployment model is at
   most one `meyar reconcile-folder` invocation running at a time per
   `(tenant, source root)`. Concurrency is not DB-enforced: two
   simultaneous invocations importing identical new content before
   either commits could both miss each other's uncommitted exact-content
   dedup lookup (item 4) and each mint a separate `Candidate`. This is a
   narrow, non-destructive race (no data corruption, no cross-tenant
   leak — worst case is a duplicate candidate later resolved by
   operator/product review), and distributed locking is deliberately
   **not** implemented for MVP — see
   `docs/DEPLOYMENT_AND_OPERATIONS.md` §20 for the explicit operational
   statement. Do not run two overlapping scheduled reconcilers against
   the same source.

**Why:** Closes the gap identified in the pre-implementation Slice 14 gap
analysis: Slice 6 already solved discovery/ingestion/idempotency; the
missing piece was purely the downstream orchestration to make a
folder-imported candidate actually searchable, without inventing a second
ingestion pipeline, a new background-worker subsystem, or a filesystem
watcher the architecture rules would require separate justification for.

**Reversibility:** Fully reversible and additive. `stability_window_seconds`
defaults to 0 (disabled) on `index_folder` itself for backward
compatibility with existing callers/tests; production callers (both CLI
commands) pass `Settings.folder_stability_seconds`. No data is destroyed by
any of these choices — a future policy change (e.g. a real processing-status
column, bounded concurrency once Target-Mac throughput is known) can be
layered on without touching the semantics recorded here.

**Hardening pass (2026-08-31, same PR):** an independent acceptance audit
found two real-but-non-blocking gaps (items 3 and 7 above record the
accepted/fixed outcomes) and confirmed items 1-2, 4-6, 8 correct as
designed. This does not change the design recorded above; it records the
fixes and the accepted limitations precisely rather than leaving them
implicit. No Target-Mac validation occurred as part of this pass —
unrelated to and does not affect issue #20/M5.

## D-022 — Pre-presentation readiness: synthetic demo bootstrap and guard-hook fix

**Date:** 2026-08-31
**Decision:** Chore-level work (issue #25, not a Slice) ahead of a local
product demonstration:

1. **`meyar seed-demo` bootstraps one fixed-name, isolated demo tenant
   (`MEYAR Demo (Synthetic)`) through the real service layers only.**
   `meyar.services.demo_seed_service` calls the exact same
   `ingest_candidate_document`, `extract_candidate_profile`,
   `extract_candidate_identity`, `embed_candidate_profile`, and
   `evaluate_and_score_candidate` functions every other code path uses —
   no parallel data path, no hand-inserted scores. The one deliberate
   exception: `_DemoLLMProvider`/`_DemoEmbeddingProvider` supply
   pre-written, evidence-matched extraction/embedding results instead of
   calling a live model, exactly the same technique `tests/fakes.py`
   already uses for the test suite. **This is not a fake production AI
   mode:** these classes are constructed only inside
   `demo_seed_service.seed_demo`, never wired into
   `meyar.llm.dependency.get_llm_provider` or
   `meyar.embedding.dependency.get_embedding_provider` (the real,
   Ollama-backed factories every request-serving path uses), and the
   command only ever runs from an explicit operator invocation — never
   at application startup, never automatically. Live search/extraction
   endpoints remain honestly dependent on a reachable Ollama daemon
   exactly as before (see `docs/LOCAL_DEMO.md` §9–10 for exactly which
   surfaces do and don't need one).
2. **Idempotent by positive tenant identification, not by display name,
   and not a new schema.** Display name (`DEMO_TENANT_NAME`) alone is
   *never* sufficient proof that a tenant is the demo tenant — an
   ordinary tenant could share that exact string by accident (no
   uniqueness constraint on `tenants.name`) or by another operator's
   own choice, and treating "same name" as "same tenant" would let
   `--reset` delete real, unrelated data (see item 2a below).
   `_find_demo_tenant` instead requires a tenant to be (a) the *sole*
   tenant named `DEMO_TENANT_NAME`, AND (b) carry a
   `DEMO_TENANT_BOOTSTRAPPED` marker — an `AuditEvent` this module
   itself writes, once, at the moment it creates a new demo tenant,
   reusing the existing tenant-scoped `audit_events` table as the
   durable proof-of-origin. No new column, no migration. If more than
   one tenant shares the name, or exactly one does but it lacks the
   marker, `_find_demo_tenant` raises `DemoTenantAmbiguousError` and
   both `seed_demo`/`reset_demo` abort before reading, adopting,
   creating, deleting, or mutating anything. Only when a tenant is
   positively identified this way does `seed_demo` treat it as already
   seeded (skip reseeding, still mint a fresh API key since a prior
   plaintext can never be recovered) and does `--reset` delete it —
   cascading via each table's existing `ondelete="CASCADE"` foreign
   key, no bespoke deletion logic, and no path that accepts an
   arbitrary tenant id.
   2a. **Hardening (2026-08-31, same PR, independent-audit P0 follow-up).**
       The first implementation of this item resolved the demo tenant
       by display name alone. An independent acceptance audit
       constructed and reproduced the exact failure this design
       predicted: creating an ordinary tenant literally named
       `MEYAR Demo (Synthetic)` caused `seed-demo --reset` to delete
       the real demo tenant and silently adopt the ordinary one,
       and a second `--reset` then deleted *that* tenant's real data.
       Fixed by adding the positive-identification marker described
       above; regression tests cover all of: no tenant with the name
       (normal create), one *marked* tenant (normal operate), one
       *unmarked* same-named tenant (refuse, untouched), two same-named
       tenants where one is genuinely marked (refuse — the ambiguity
       itself is unsafe even though one candidate is legitimate), and a
       repeated seed/reset cycle (stays idempotent, never accumulates
       tenants). See `backend/tests/test_demo_seed.py`.
3. **`.claude/hooks/guard.sh` gained the same env-file exception
   `.githooks/pre-commit` and `scripts/scan-tracked-tree.sh` already
   had.** The guard previously blocked automated Write/Edit to *any*
   `.env`-shaped path, including the tracked, non-secret
   `backend/.env.example` template — an inconsistency with the other two
   governance tools, which already carve out exactly
   `.env.example`/`.env.sample`/`.env.template` via an
   `is_allowed_env_file` helper. `guard.sh` now uses the identical
   helper/allowlist; real `.env`, `.env.local`, `.env.production`, etc.
   remain fully blocked (manually verified: `.env`/`.env.local`/
   `.env.production`/`id_rsa` all still block; `.env.example` and an
   unrelated file both pass).
4. **`backend/.env.example` completeness.** Added the 6 active
   `MEYAR_*` settings it was missing (5 `MEYAR_EMBEDDING_*` fields, plus
   `MEYAR_FOLDER_STABILITY_SECONDS` from D-021) — all already had safe
   code-level defaults; this is documentation completeness, not a
   behavior change.
5. **Stale test-count/status wording corrected.** `docs/STATUS.md`'s
   Tests section said "535/535"; the actual count (post-Slice-14
   hardening pass) is 537. `docs/STATUS.md`/`docs/MVP_PLAN.md`'s Slice 14
   sections also still described it as "pending merge" after PR #24 had
   already merged (squash `f6e31ff`) — corrected to MERGED, with M6 noted
   as having no remaining open issues but deliberately left open pending
   an explicit owner closure decision, per the standing rule against
   closing milestones without an approved roadmap decision.

**Why:** A department-head-facing local demo needs a truthful, inspectable
UI without requiring live Ollama for every screen, and without any
production code branching on "is this a demo." The synthetic dataset gives
that without touching search/scoring/matching logic at all. The guard-hook
fix removes recurring friction on a file the project's own other two
governance tools already treat as safe to edit.

**Reversibility:** Fully reversible and additive. The demo tenant can be
removed at any time with `meyar seed-demo --reset` and affects nothing
else. The guard-hook change only narrows what was already blocked for one
specific, non-secret, already-tracked filename pattern — every other
`.env`-shaped path remains blocked exactly as before.

## D-023 — HR UI productization and presentation readiness (M7)

**Date:** 2026-08-31
**Decision:** Following owner visual inspection of the running local UI
(issue #27, milestone M7), the primary `/ui/*` surfaces were reworked to
speak HR/product language rather than database/developer language. Backend
architecture, deterministic scoring, and the API/CLI contracts are
untouched — this is a UI-layer and two narrowly-scoped bug fixes only.

1. **Evaluation date is no longer a manual UI input.** The `as_of_date`
   field on `/ui/search` and `evaluation_as_of_date` on `/ui/jobs/*/rank`
   are removed from the HTML forms. `meyar.ui.router` now computes
   `date.today()` once, at the request boundary, in the `search` and
   `rank_job` handlers, and threads it explicitly into the same
   `plan_and_search_candidates`/`rank_candidates_for_job` calls as before
   — the deterministic services still receive an explicit date argument,
   never `datetime.today()` reached internally, and the effective date is
   still shown on the results page. The `/api/v1/*` and CLI contracts,
   which need an explicit, possibly-backdated date for reproducible
   evaluation, are entirely unchanged.
2. **Root-caused the owner-reported `Plan yoxlamadan keçmədi /
   REQUEST_CONTROL_CHARACTERS` failure on an ordinary query.** Reproduced
   with `"pythonda 5 il tecrubesi olan\n".isprintable()` → `False`: Python's
   `str.isprintable()` treats `\t`/`\n`/`\r`/`\v`/`\f` as non-printable
   control characters, and a `<textarea>` normalizes embedded line breaks
   to CRLF on submission — so a completely ordinary query (the owner
   pressing Enter while composing, or pasting text with a trailing
   newline) tripped the same guard meant to catch actual control-character
   injection. Fixed in `meyar.search.planner_policy
   .precheck_natural_language_request` by folding exactly those five
   benign whitespace control characters to a space before the
   `isprintable()` check — every other non-printable character (NUL, ANSI
   escapes, RTL overrides, etc.) is still rejected exactly as before; nothing
   about the injection guard was weakened. This function is the single
   shared precheck for the UI, API, and CLI, so the fix applies everywhere
   without new call-site logic. Regression tests cover both the benign
   whitespace cases and that genuine control characters still fail closed
   (`backend/tests/test_search_planner_policy.py`). Reproduced live against
   a running local Ollama after the fix: the same query now proceeds to a
   real (friendly, truthful) plan-validation outcome instead of surfacing
   a raw reason code.
3. **`meyar seed-demo` key-rotation fix (owner-reported operational gap,
   §20 of the M7 task brief).** The idempotent reseed path previously
   minted a brand-new API key on every re-run but discarded its plaintext
   (`api_key_plaintext=None`), leaving the operator with no usable
   credential and an ever-growing set of orphaned, never-shown keys.
   `meyar.services.api_key_repo.revoke_active_api_keys_for_tenant` now
   revokes every currently-active key for a tenant; `seed_demo`'s
   idempotent branch calls it (scoped to the positively-identified demo
   tenant only, via the existing `_find_demo_tenant` marker check) before
   minting and returning the new key's plaintext. Net effect: every
   `seed-demo` run — first or repeat — always ends with exactly one active,
   usable demo credential, never key sprawl. Not exposed as a generic
   cross-tenant rotation capability anywhere; the repo function requires an
   explicit `tenant_id` and is only ever called from this demo-scoped path.
4. **HR-facing information boundary.** Raw UUIDs (candidate, document,
   job-criteria-version), planner reason codes, and pipeline internals
   (parser name/status, folder-indexer status) are removed from the
   primary HR screens — never from the API/OpenAPI/logs. Candidate
   library/detail collapsed the separate parser/profile/folder-index
   status axes into one HR-relevant readiness signal (Hazır / Diqqət
   tələb edir / Emal olunur), derived from the existing `profile_status`
   field (`meyar.ui.presentation.readiness_label`) — no new column, no
   invented data. The parser/folder-index filters are dropped from the
   `/ui/library` form (the repository function `list_candidate_library`
   still accepts them; nothing was removed from the backend). Document
   metadata (MIME, bytes, parser name/status, `parse_error_code`) moved
   into a collapsed "Texniki məlumat" disclosure on the candidate detail
   page rather than being removed, since an operator can still need it.
5. **Truthful two-level CV access.** The previous single "Aç" link opened
   PDFs inline but silently downloaded DOCX with no explanation. Added a
   new authenticated, tenant-scoped route,
   `GET /ui/candidates/{candidate_id}/documents/{document_id}/preview`,
   that renders the existing `CanonicalDocument` (already-parsed, safe
   text — the same data source Slice 4 evidence citations use) as an
   in-app "CV-yə bax" view; it never touches the original bytes and never
   calls a model. The original-bytes route is kept unchanged and
   relabeled "Originalı yüklə" — still inline for PDF, still an attachment
   for DOCX, but now truthfully described as a download either way.
6. **Evaluation history resolves job titles.** `EvaluationHistoryView`
   gained `job_title`, resolved via a small batched `Job.id -> Job.title`
   lookup in `meyar.ui.service`, replacing the raw
   `job_criteria_version_id` column on the candidate detail page. The
   ranking-results page resolves and shows the job title the same way
   instead of the raw criteria-version id in the header.

**Why:** MEYAR is an internal HR product; the owner's inspection found the
running UI reading as an engineering console (raw ids, pipeline status
enums, a manual date field, a misleading download link) rather than an HR
tool, plus the two genuine operational bugs above. None of this touches
scoring, matching, tenant isolation, or the LLM/embedding boundary.

**Reversibility:** Fully reversible. No migration; no schema change. The
date-injection change is UI-layer only — API/CLI callers pass their own
explicit date exactly as before. The control-character fix only widens
what the guard treats as benign formatting; reverting the five-character
translate table restores the previous (overly strict) behavior instantly.
The demo-key rotation only affects the already demo-scoped, chore-level
`seed-demo` command.

## D-024 — HR UI productization: second-round visual-inspection fixes (M7)

**Date:** 2026-08-31
**Decision:** A second owner visual inspection of PR #29 (same branch,
`feat/hr-ui-productization`, still unmerged) found seven remaining
blockers. All are UI-layer or safety-net-regex fixes; scoring, tenant
isolation, and the LLM/embedding boundary are untouched. Real local Ollama
inference was not made a required acceptance dependency, per the owner's
8GB-laptop constraint — the natural-language pipeline fix was diagnosed
and verified with the deterministic `FakeLLMProvider` test double and
direct unit tests of the policy module, not a live model run.

1. **Root-caused the still-generic outcome for `"pythonda 5 il tecrübesi
   olan"`.** D-023 fixed the `REQUEST_CONTROL_CHARACTERS` false positive on
   this exact query, but two separate, deeper problems in the deterministic
   fidelity-check safety net (`meyar.search.planner_policy`) remained and
   independently produced a generic `VALIDATION_FAILURE`/
   `UNSUPPORTED_SEMANTICS` outcome for an entirely ordinary request — a
   SearchPlan/domain-model limitation was ruled out; both are regex gaps in
   the guard that verifies the model didn't invent/weaken a filter:
   - The fixed keyword/marker regexes (`təcrüb`, `il`, `mütləq`, etc.) are
     written with correct Azerbaijani spelling and require it literally;
     most HR staff type on a plain Latin keyboard without the dedicated
     diacritic keys and substitute the nearest ASCII letter (e.g.
     "tecrübə" for "təcrübə"), so `explicit_total_experience_years` found
     no explicit year in the request text and rejected the model's
     (correct) `min_total_experience_years=5`. Fixed with a shared
     `meyar.core.text.fold_az_ascii` diacritic-folding helper applied to
     both the searched text and (at compile time) the fixed pattern
     source/marker canonicalizers, so either spelling matches.
   - Azerbaijani is agglutinative: a locative/ablative case suffix attaches
     directly to a noun with no space ("Pythonda" = "in Python"), but
     `_value_supported_by_request` required the exact word `python` at a
     hard boundary, so a plainly-supported skill mention was rejected as
     `STRUCTURED_FILTER_NOT_SUPPORTED_BY_REQUEST`. Fixed by adding a second,
     narrowly-scoped match attempt that tolerates exactly the standard
     locative/ablative suffixes (`da/də/ta/tə/dan/dən/tan/tən`, the regular
     voiced/voiceless alternation), gated to values of 3+ characters so a
     short acronym (e.g. "C") still cannot false-positive-match an unrelated
     word — verified this does not resurrect the "Java matches inside
     JavaScript" false positive the strict boundary exists to prevent.
   Neither fix special-cases the reported sentence — both are general
   typing/morphology accommodations, covered by parametrized regression
   tests including the ASCII-only and fully-diacriticized spelling, an
   ablative-case example, and an explicit non-regression case for the
   Java/JavaScript boundary (`backend/tests/test_search_planner_policy.py`,
   `backend/tests/test_ui_routes.py`).
2. **New vacancy creation.** `/ui/jobs` previously had no create action —
   vacancies could only be made via the API/CLI. Added
   `GET /ui/jobs/new` (form) and `POST /ui/jobs` (create), both requiring
   the existing `jobs:write` scope and CSRF token like every other
   state-changing `/ui/*` route. The handler reuses the exact same
   `create_job`/`create_criteria_version` repository functions as
   `POST /api/v1/jobs` (`meyar.api.v1.jobs.post_job`) and the same
   `CriterionIn`/`JobCreateRequest` Pydantic schemas — one job/criteria
   creation path, one deterministic scoring model, for both surfaces. The
   HR user never types a criterion id or job UUID: `meyar.ui.service
   .build_job_create_request` derives a stable ASCII-only slug from the
   HR-entered label (`meyar.core.text.fold_az_ascii` again, plus a
   collision-safe numeric suffix) purely as the policy engine's internal
   join key. The form is a fixed set of rows (no JS row-adding, consistent
   with the rest of this JS-free `/ui` surface, and compatible with the
   strict `script-src 'self'` CSP already in place); blank rows are
   silently skipped, kind-specific validation (e.g. `EXPERIENCE` requires
   `min_years`) and the existing sensitive/prohibited-term denylist both
   fire through the same `CriterionIn` validators the API uses, and a
   validation failure re-renders the form with the HR user's own input
   preserved rather than discarding it.
3. **Raw internal criterion ids/kind enums no longer reach the ranking
   table.** `ranking_results.html` rendered
   `{criterion_id} ({criterion_kind})` — e.g. `aml_skill (SKILL)` — because
   `CriterionScoreContribution` (the deterministic scoring engine's own
   output schema, intentionally unchanged — scoring semantics are
   untouched) only carries the internal id, not the label. Fixed
   presentation-side only: `meyar.ui.service.build_ranked_candidate_views`
   now resolves each contribution's id against the same job criteria
   version's stored `criteria` JSON (which already carries the HR-entered
   `label`) and a new `meyar.ui.presentation.CRITERION_KIND_LABELS` maps
   the kind enum to an Azerbaijani noun (`SKILL` → "Bacarıq", etc.) for
   display only; the raw id/kind remain on `ScoreContributionView` for any
   future API-parity use, just no longer rendered as the visible text.
4. **Ranking-page wording.** Column headers softened
   (`Status`→`Nəticə`, `Əmsal`→`Uyğunluq dərəcəsi`, "Meyar töhfələri"→
   "Meyarlar üzrə təfərrüat"); `/ui/jobs`'s "sıralayın deterministik
   şəkildə" replaced with the owner-suggested "Namizədləri vakansiya
   meyarlarına əsasən sıralayın." No numeric calculation changed.
5. **Candidate-detail "Texniki məlumat" reduced to genuinely HR-meaningful
   facts.** Dropped `parser_name`/`parser_version` (implementation
   identity) and the raw `parse_error_code` from the rendered table; kept
   only file type (now shown as "PDF"/"DOCX" like the row above it, not
   the raw MIME string), file size, and the existing friendly
   `state_label(parser_status)` readiness badge. Nothing was deleted from
   `CandidateDocumentView`/the database — this is a template-only
   reduction of what's rendered, per the same pattern D-023 item 4 already
   established for the rest of this page.
6. **CV-preview XSS: confirmed, not newly introduced.** Jinja2 autoescaping
   was already on for `.html` templates project-wide
   (`select_autoescape(...)`) and no template uses `|safe`/`Markup`, so
   candidate-controlled canonical-document text was already rendered as
   inert text. Added a regression test that seeds a `CanonicalDocument`
   block directly with `<script>alert(1)</script>`,
   `<img src=x onerror=alert(1)>`, and `< > & " '`, and asserts the
   response contains only the escaped form
   (`backend/tests/test_ui_candidate_preview.py`) — this is now enforced,
   not just believed true from reading the template.
7. **"Originalı yüklə" now truthfully downloads.** The original-CV route
   previously used `Content-Disposition: inline` for PDFs specifically
   (D-023 item 5 kept this unchanged), so clicking the button labeled
   "download" silently opened the PDF in the browser tab instead — a
   truthfulness gap the owner's second inspection flagged directly. The
   safe in-app text view already lives at the separate `/preview` route
   introduced in D-023, so there is no remaining reason for `/original` to
   ever be anything but a true download: it now always sends
   `Content-Disposition: attachment` regardless of MIME type.

**Why:** The owner's second pass found the natural-language search path
still practically unusable for ordinary Azerbaijani phrasing/typing, no
way for HR to create a vacancy at all (a core documented product
capability), and several of the same "reads like an engineering console"
symptoms D-023 addressed elsewhere on the page that had not yet been
applied to the ranking table and candidate-detail technical section.

**Reversibility:** Fully reversible. No migration; no schema change. The
planner-policy fold/suffix-tolerance changes only widen what the
safety-net regex accepts — reverting `meyar.core.text.fold_az_ascii` usage
and the locative/ablative suffix branch in `_value_supported_by_request`
restores the previous (stricter) behavior instantly. Vacancy creation is
a net-new, additive route pair; disabling it (removing the two routes)
does not affect existing jobs, criteria, or the API/CLI creation path,
which is unchanged. The ranking-table/candidate-detail wording and the
`/original` disposition change are presentation-only.

## D-025 — Third-round visual inspection: vacancy-form correctness/UX, search-outcome truthfulness (M7)

**Date:** 2026-08-31
**Decision:** A third owner visual inspection of PR #29 (same branch,
still unmerged) found two acceptance blockers, both traced end-to-end
before any fix — no guessing.

1. **Root cause of "Python -> Məlumat məlum deyil (UNKNOWN)" on an
   owner-created vacancy.** Reproduced live against the actual demo tenant
   the owner used. The persisted criteria JSON for the owner's "Senior
   Python Developer" vacancy was
   `{"kind": "SKILL", "label": "Python", "value": "MUST_HAVE", ...}` — the
   `value` field, which `meyar.evaluation.evaluators.evaluate_skill`
   correctly and deterministically matches against candidate-profile
   skill names, held the literal string `"MUST_HAVE"`, not `"Python"`.
   The seeded candidate ("Tural Demo-Aliyev") genuinely has a verified
   `"Python"` skill with evidence — confirmed directly from the database.
   This was **not** a scoring/evaluator bug, a normalization mismatch, a
   label/value swap in the persistence code, or a divergence between the
   UI-creation and API-creation paths (all four were checked directly,
   not assumed): the deterministic scorer did exactly the right thing
   with the criterion it was given. The malformed criterion itself came
   from the *previous* two-field form: it separated an internal "Ad"
   (label) field from an internal "Dəyər" (value) field, and an HR tester
   — with no way to know these needed to be the same text for a skill —
   typed the requirement's *type* ("MUST_HAVE") into "Dəyər" instead of
   repeating "Python". This is exactly the confusion Blocker B (below)
   independently flagged about the same form.
2. **Fix: collapse "Ad"/"Dəyər" into one "Tələb" field.**
   `meyar.ui.service.CriterionRowInput` now carries a single
   `requirement: str` instead of separate `label`/`value` strings; for
   SKILL/CERTIFICATION/EDUCATION/LANGUAGE criteria `_parse_criterion_row`
   sets **both** `CriterionIn.label` and `CriterionIn.value` from that one
   HR-entered string, so the label shown on screen and the value the
   deterministic scorer matches against evidence can no longer diverge —
   not just harder to misuse, structurally incapable of it. For
   EXPERIENCE criteria the same field becomes the descriptive label (e.g.
   "Minimum təcrübə"), paired with the existing required numeric "illik
   təcrübə" input. The criterion id remains fully server-generated
   (`_slugify_criterion_label`, unchanged) — the HR user still never types
   or sees a raw id/UUID. Row count reduced 6 → 4 per section (less
   spreadsheet-like); the weight column, renamed "Əhəmiyyət", now
   pre-fills a safe default of `1` instead of an empty box needing to be
   filled every time. Column headers/help text rewritten in HR language
   throughout (`Növ`/`Tələb`/`Təcrübə (il)`/`Əhəmiyyət`).
3. **Regression coverage added, not just fixed.** A CRITICAL ACCEPTANCE
   TEST (`test_ui_created_skill_criterion_matches_real_candidate_evidence`
   in `backend/tests/test_ui_job_creation.py`) seeds a real candidate with
   verified Python evidence and a second candidate without it, creates a
   vacancy through the exact `/ui/jobs/new` -> `POST /ui/jobs` application
   path used by HR, ranks it, and asserts the evidenced candidate resolves
   to MATCH while the non-evidenced one still correctly resolves to
   UNKNOWN — proving both that the fix works and that UNKNOWN was never
   weakened into a false match. A second test
   (`test_ui_created_criterion_is_structurally_equivalent_to_api_created`)
   creates the same requirement through the UI form and directly through
   `create_job`/`create_criteria_version` (the same services
   `POST /api/v1/jobs` uses) and asserts the persisted criterion JSON is
   byte-for-byte identical in shape — proving the two creation paths
   cannot semantically diverge for scoring.
4. **Search-outcome truthfulness (Blocker C).** Reproduced the owner's
   exact query, `"pythonda 5 il tecrubesi olan"`, against the real local
   Ollama available on this development machine (not simulated) and read
   the actual persisted `AuditEvent` for that request. The internal
   outcome was genuinely `UNSUPPORTED_SEMANTICS` with reason code
   `LANGUAGE_PROFICIENCY_UNSUPPORTED` — **not** `PLANNER_PROVIDER_FAILURE`
   (Ollama responded normally) and **not** a false positive in this
   module's own deterministic precheck regex (verified directly:
   `precheck_natural_language_request` returns cleanly for this exact
   text). The reason code came from `PlannerDraft.unsupported_reason_codes`
   — the small local planner model (`qwen3:0.6b`, chosen to fit the
   owner's 8GB laptop) itself incorrectly self-flagged an ordinary
   Python+experience request as involving language proficiency, even
   though the request never mentions a language. `convert_planner_draft`
   correctly, and by design, never second-guesses a model's own admission
   that it can't safely interpret something — so the outcome itself was
   not wrong to reject. What was misleading is that the exact same HR
   message ("Tələb hazırda dəstəklənmir") was shown for this
   model-quality-dependent self-decline as for a genuine, deterministic,
   model-independent product-policy gap (e.g. salary/location filters are
   really not supported, on any model). Added
   `PlannerReasonCode.MODEL_DECLINED_INTERPRETATION`, an internal-only
   marker `convert_planner_draft` attaches whenever
   `draft.unsupported_reason_codes` is what triggered the rejection (never
   for the module's own deterministic precheck/postcheck reasons —
   regression-tested both ways). `meyar.ui.presentation.planner_outcome_view`
   shows a distinct, honest message for that case ("AI tələbi tam anlaya
   bilmədi" — explicitly notes this may be a limitation of the configured
   local model, not of MEYAR) while every genuine deterministic
   UNSUPPORTED_SEMANTICS rejection keeps the original message. Raw reason
   codes are still never rendered (existing + new tests). Fail-closed
   validation is unchanged — this is purely a presentation-layer
   distinction, added generically (any draft self-decline, not this one
   sentence) so it also improves every other case where a small local
   model misjudges an ordinary request, not just this reported one. Real
   semantic-model quality remains deferred to Target-Mac/capable-machine
   acceptance, unchanged from D-023/D-024.

**Why:** Both issues trace back to the same theme: MEYAR must not let an
HR user believe "the product can't do this" when the real cause is either
(a) a confusing form that silently produced a malformed criterion, or (b)
a small local model's own misjudgment on this specific laptop — neither
is a genuine, permanent product limitation.

**Reversibility:** Fully reversible. No migration; no schema change. The
form-field change only affects new vacancy creation going forward —
existing criteria versions (created via the old form or the API) are
untouched and continue to score exactly as before, since scoring reads
only `CriterionIn.value`/`min_years`, never how a criterion was
constructed. The `MODEL_DECLINED_INTERPRETATION` marker is additive and
presentation-only; removing the `planner_outcome_view` branch instantly
reverts to the single shared UNSUPPORTED_SEMANTICS message.

## D-026 — Conservative deterministic fast path for explicit NL search intents (M7)

**Date:** 2026-08-31
**Decision:** D-025 explained *why* `"pythonda 5 il tecrubesi olan"` could
fail even with a working local Ollama (the small `qwen3:0.6b` planner
model self-declining) but left the request still dependent on that
model's judgment. The owner asked for a stronger guarantee: common,
explicit, supported HR search intents must not depend on local-model
quality at all. Added `meyar.search.planner_policy.try_deterministic_intent_parse`
— a narrowly-scoped, whole-clause-anchored pattern set for exactly the
concepts `CandidateSearchRequest` already represents (skills, languages,
certifications, total experience years, and simple `"və"`-joined
combinations of these) — invoked in `plan_candidate_search`
(`meyar.search.planner_service`) between the existing security precheck
and the LLM loop. When it returns a draft, that draft is run through the
*exact same* `convert_planner_draft` fidelity/validation the LLM path
uses (no second, weaker validation surface) and executes with zero LLM
calls; when it declines, the existing LLM loop runs completely unchanged.

**Why "conservative," not a general NLP engine:**
- Every one of the five patterns (skill list + "bilən", "X dili
  olan/bilən", "X sertifikatı olan", "N il təcrübəsi olan", and the
  reported-bug shape "Xda N il təcrübəsi olan" for an agglutinated
  locative/ablative skill suffix) is anchored `^...$` against the whole
  clause — a request with anything not accounted for by a recognized
  concept or one of a tiny, fixed set of glue words (`namizədləri`,
  `göstər`, `tap`, leading `mənə`, etc.) never partially matches. This is
  the direct implementation of "never discard the remainder and execute a
  weaker search": the function returns `None` (defer to the LLM) rather
  than a subset.
- Multiple concepts combine only via a literal `" və "` split into up to
  4 clauses, each independently required to fully match on its own — not
  a general clause grammar.
- Recognized languages are limited to the existing `_LANGUAGE_ALIASES`
  catalog (canonicalized to `"English"`/`"Russian"`/`"Azerbaijani"`/
  `"Turkish"`); an unlisted language (e.g. French) declines rather than
  inventing support.
- Any preferred-marker vocabulary (`üstünlükdür`, `preferred`, ...)
  anywhere in the request declines immediately — the fast path only ever
  produces `MUST_HAVE` filters, so a request that might need the
  required/preferred nuance goes to the LLM.
- Skill-*specific* duration ("N years experience IN skill X", e.g.
  `"Python üzrə ən az 5 il təcrübəsi"` or `"5 il Java təcrübəsi"`) is
  deliberately **not** reinterpreted as total experience — `SearchPlan`
  has no field for it, and guessing would silently change what was
  asked. These already fail the *existing* precheck
  (`_skill_duration_is_unsupported`, unchanged) before the fast path is
  even reached, so behavior here is identical to before this change.
  Regression-tested explicitly so this boundary doesn't drift.
- Java vs. JavaScript: the skill value captured is always the literal
  token the user typed (`"Javascript bilən"` → `skills=["Javascript"]`,
  never truncated to `"Java"`) — there is no catalog-substring matching
  to collide in the first place. Explicitly regression-tested.
- Reuses, not duplicates, D-023's machinery: `fold_az_ascii` for
  ASCII/diacritic typing variance and the same bounded
  `_AZ_LOCATIVE_ABLATIVE_SUFFIXES` set for the agglutinated-skill shape.

**Provenance and auditability:** a fast-path result is tagged with a
fixed synthetic provenance (`provider="meyar-deterministic"`,
`model_name="meyar-deterministic-parser-v1"`, `attempt_count=0`) so audit
events (`SEARCH_PLAN_CREATED`, same as the LLM path) and any future
`/api/v1/search/natural-language` consumer can tell a deterministic
result apart from a model-produced one at a glance — this is a bounded
resilience layer, not a "fake AI" mode: it never fabricates an AI
provenance, and it is not a replacement for the local semantic planner
(semantic/free-text requests, and anything outside the five patterns,
still require it exactly as before).

**Confirmation/clarification-state investigation (requested, not
implemented):** the owner asked whether a structured
clarification/confirmation state could be represented for a partially
understood request using the existing server-rendered architecture. By
construction, this fast path never produces a "partially understood"
state — it is binary (full match -> draft, anything else -> `None`,
handled by the unchanged LLM/precheck outcomes) — so no such state exists
for this feature to represent. A genuine future "I understood X but not
Y, confirm?" flow is architecturally feasible on top of the existing
`BrowserSession`/CSRF/server-rendered pattern (e.g. a short-lived signed
pending-plan token or a session-scoped pending-plan row, plus a new
confirm/reject route), but is a materially new feature — session-state
lifetime, CSRF, and audit implications of its own — not a fix folded into
this pass. Left as a candidate for a future slice if the owner wants it.

**Tests:** `backend/tests/test_search_deterministic_parser.py` — the
requested regression matrix (diacritics vs. ASCII typing, case, the
agglutinated-suffix shape, whitespace/CRLF, multiple skills, experience
years, language, certification, Java/JavaScript non-collision, ambiguous/
unsupported declines) plus full-pipeline proof: zero LLM calls
(`FakeLLMProvider` configured to error if invoked), the produced
`CandidateSearchRequest` passes the same validation and executes with
real structured-search results, tenant isolation holds, and the audit
event carries the synthetic provenance with no raw request text. Existing
tests that used a now-fast-path-eligible query specifically to exercise
LLM failure/repair paths (`test_malformed_then_valid_uses_exactly_one_repair`,
`test_malformed_twice_stops_after_two_attempts`,
`test_malformed_model_output_outcome_never_searches`,
`test_malformed_planner_output_has_no_fallback_search`,
`test_local_planner_outage_is_safe_and_library_remains_independent`, and
one D-025 parametrized case) were updated to use semantic/free-text
requests that are genuinely outside the fast path's scope, so they keep
testing what they always tested.

**Reversibility:** Fully reversible and additive. No migration; no schema
change. Deleting the single `try_deterministic_intent_parse` call site in
`plan_candidate_search` restores 100% LLM-dependent behavior instantly;
nothing else in the pipeline (precheck, `convert_planner_draft`,
`search_candidates`, audit) was modified to accommodate it.

## D-027 — Deterministic search-semantics audit: no silent skill-duration weakening

**Date:** 2026-08-31
**Decision:** A dedicated semantic-correctness audit of D-026's fast path
found a real correctness bug, root-caused before any change (per the
audit's own requirement): for `"pythonda 5 il tecrubesi olan"`
("5 years of experience IN Python"), the fast path introduced in D-026
was producing `RequiredFilters(skills=["python"],
min_total_experience_years=5.0)` — i.e. "has the Python skill" AND
"has >= 5 years of TOTAL career experience", **not** "has 5 years of
experience specifically in Python". These are not equivalent: a
candidate with 1 year of Python and 10 years of unrelated total
experience would incorrectly satisfy the first, weaker reading. The
identical connector phrasing (`"Python üzrə 5 il təcrübəsi"`) was already
correctly rejected as unsupported — so the same HR intent was getting
inconsistent treatment purely based on which grammatical form was typed.

1. **Can MEYAR prove per-skill duration? Inspected, not assumed: no.**
   `CandidateProfileExtraction` (`meyar/schemas/candidate_profile.py`)
   holds `skills: list[SkillItem]` and
   `employment_history: list[EmploymentItem]` as two independent flat
   lists — `SkillItem` has no field referencing an `EmploymentItem`, and
   `EmploymentItem` has no field listing which skills were used.
   `meyar.evaluation.evaluators.evaluate_skill` only checks a skill NAME
   is present; `evaluate_experience` only sums `EmploymentItem` date
   ranges. There is no code path, schema field, or evidence relationship
   anywhere that could substantiate "N years of experience with skill X"
   as a single fact. Per the audit's own instruction, this means the
   fast path must **not** invent that duration by combining a skill
   mention with total career years — a missing capability stays UNKNOWN/
   declined, never guessed.
2. **Unified fix at the shared precheck, not two separate patches.**
   `precheck_natural_language_request` (shared by the LLM path, the
   deterministic fast path, the REST API, and the CLI — it is the very
   first thing every one of them runs) already rejected the "üzrə"/"ilə"
   connector phrasing as `SKILL_SPECIFIC_EXPERIENCE_DURATION_UNSUPPORTED`
   via `_SKILL_DURATION_PATTERNS`. Added a fifth pattern there for the
   agglutinated locative/ablative-suffix shape ("Pythonda", "SQL-dan",
   reusing D-023's `_AZ_LOCATIVE_ABLATIVE_SUFFIXES`), so **every**
   equivalent phrasing is now rejected identically, before either the
   deterministic parser or the LLM ever sees the request — closing the
   gap for all four consumers with one change. Also fixed a latent gap
   surfaced while writing the regression matrix: the connector pattern's
   optional marker only recognized `"ən azı"`, not the equally common
   `"ən az"` (no trailing "ı") — `"Python üzrə ən az 5 il təcrübəsi"` was
   silently passing precheck unrejected; now folds both spellings, same
   as the deterministic total-experience pattern already did.
   `try_deterministic_intent_parse`'s own skill+experience pattern is
   removed (dead code — precheck rejects it first) with a comment
   explaining why it is deliberately absent, not merely missing.
3. **New: `find_skill_specific_duration_mention(text) ->
   (skill, years) | None`.** Extracts what a rejected request named, for
   two honest purposes only: powering an HR-safe clarification (never a
   silent guess) and letting `_skill_duration_is_unsupported` reuse one
   source of truth instead of duplicating the pattern list.
4. **HR-safe clarification instead of a generic failure page.** When
   `/ui/search`'s outcome is `UNSUPPORTED_SEMANTICS` with reason
   `SKILL_SPECIFIC_EXPERIENCE_DURATION_UNSUPPORTED`, a new
   `search_clarification.html` explains, in plain HR language and naming
   the actual skill/years, that MEYAR can check "has this skill" and
   "total experience" independently but not "N years with this skill",
   and offers one explicit, CSRF-protected confirm action. Confirming
   resubmits a reconstructed, unambiguous request
   (`"{skill} bilən və ümumi iş təcrübəsi {years} il olan"`) through the
   *normal* `/ui/search` flow — no bypass of precheck/fidelity
   validation, no new outcome type, no REST API contract change (the
   "exact 7-way" `PlannerOutcome` documented in
   `ApiNaturalLanguageSearchResponse` is untouched; API/CLI clients keep
   getting the existing typed `UNSUPPORTED_SEMANTICS` rejection and can
   build their own handling from `reason_codes`, exactly as before). The
   confirmed alternative only ever executes after this explicit click —
   never automatically. Added a matching deterministic pattern
   (`"[ümumi/peşəkar] iş təcrübəsi [ən az/minimum] N il olan"`, the
   duration-second word order) and its `explicit_total_experience_years`
   fidelity-check counterpart, so the confirmed alternative — and any
   other request already phrased with skill and total-experience as two
   distinct clauses — resolves deterministically with zero LLM calls.
5. **Vacancy kind-aware validation (owner follow-up): was not
   implemented, now is.** Investigated as requested rather than assumed:
   `meyar.ui.service._parse_criterion_row` correctly required
   `min_years` for EXPERIENCE and correctly kept SKILL/CERTIFICATION
   distinct via `CriterionKind`, but a value typed into the "Təcrübə
   (il)" field for any NON-EXPERIENCE row (SKILL/CERTIFICATION/
   EDUCATION/LANGUAGE) was silently read and discarded — the form
   accepted input it then ignored, exactly the "no silent ignoring of
   incompatible form fields" failure mode the owner asked to check for.
   Fixed: any non-empty `min_years` on a non-EXPERIENCE row now raises a
   clear validation error instead.
6. **Job duplicate/lifecycle finding (reported, not implemented this
   pass).** `Job` (`meyar/models/job.py`) has no unique constraint on
   `(tenant_id, title)` and no status/lifecycle field at all (no active/
   closed/archived state, no soft-delete) — `create_job` inserts
   unconditionally and neither `/ui/jobs` nor `POST /api/v1/jobs` checks
   for an existing title. Two vacancies can be created with the
   identical title, each independently rankable, and a filled/cancelled
   vacancy has no way to be closed or hidden from the active listing —
   it remains visible and rankable forever. This is a genuine product
   gap for a real HR tool, not a scoring/tenant-isolation/security issue
   (each `Job`/`JobCriteriaVersion` is still a distinct, correctly
   tenant-scoped row; nothing cross-contaminates). Not fixed in this
   pass — a full lifecycle (status field, close/reopen action, listing
   filter) is a materially new feature, not a blocker fix, and no
   concrete desired behavior was specified to implement against. Left as
   an explicit open gap for a future slice.

**Why:** The owner's core objection was structural, not cosmetic: a
deterministic system that silently reinterprets "N years IN skill X" as
"skill + N years of anything" produces a materially different (weaker)
candidate pool than what was asked, with no way for HR to know. The fix
keeps the guarantee "the system never invents what it cannot prove"
intact while still giving HR an honest, one-click path to the weaker
search when they genuinely want it.

**Reversibility:** Fully reversible. No migration; no schema change.
Removing the fifth `_SKILL_DURATION_PATTERNS` entry and the "ən az"
fold restores the exact pre-audit (buggy) precheck behavior; removing
the clarification branch in `meyar.ui.router.search` restores the
generic outcome page for this reason code. The kind-aware validation
addition only rejects input that was previously silently dropped —
no previously-accepted request is now rejected.

## D-028 — Job/vacancy lifecycle: ACTIVE/ARCHIVED, no hard delete, duplicate-creation safety

**Date:** 2026-08-31
**Decision:** `Job` previously had no lifecycle at all — no status, no
way to close a filled/cancelled vacancy, and no protection against an HR
tester accidentally re-submitting an identical vacancy (a real event
already observed in this repository's own demo tenant during prior
sessions' testing). Implemented the smallest production-defensible
lifecycle model, explicit soft states only — no hard delete anywhere.

1. **Persisted lifecycle.** `Job` gained `status` (`ACTIVE` | `ARCHIVED`,
   default `ACTIVE`) and `archived_at` (nullable). Migration
   `db7e4523f491` (`add_job_lifecycle_status_and_duplicate_signature`,
   `Revises: e3b1f7a9c2d4`) adds both columns with `nullable=False,
   server_default='ACTIVE'` on `status`, so every existing `Job` row
   backfills to `ACTIVE` deterministically in the same `ALTER TABLE` — no
   separate `UPDATE`, no data loss. Verified three ways, not assumed:
   (a) `alembic upgrade head` / `downgrade -1` / `upgrade head` again
   against the real dev DB, (b) a from-scratch throwaway database run
   through the *entire* migration chain (`base` → `head`, all 10
   revisions) confirming a single head and no ordering conflicts, and
   (c) a new automated test,
   `test_job_lifecycle_migration_backfills_existing_jobs_as_active`
   (`test_ui_migration_packaging.py`), that inserts a raw `Job` row
   against the pre-migration schema and asserts it becomes
   `status='ACTIVE'`, `archived_at IS NULL` after upgrading. No
   `JobCriteriaVersion` or `Evaluation` row is ever touched by archiving
   — `archive_job` (`meyar.services.job_repo`) only ever sets
   `status`/`archived_at` on the `Job` row itself.
2. **HR UX.** `/ui/jobs` defaults to `status=ACTIVE`; a new
   `?status=archived` view (linked as "Aktiv vakansiyalar" / "Arxiv" tabs)
   shows archived vacancies with a visible "Arxivləşdirilib" badge and
   deliberately **no** rank action — archived vacancies are never
   presented as open. A new `POST /ui/jobs/{job_id}/archive` (auth via
   the existing `jobs:write` scope, CSRF-verified, tenant-scoped —
   `archive_job` returns `None` and the route renders a safe 404 for a
   foreign-tenant `job_id`) is the only lifecycle transition; there is no
   "reopen" and no edit/delete in this pass, matching the requested
   scope. No raw UUID is shown as visible text — `job.id` appears only
   inside the archive form's `action` attribute, the same established
   pattern as `criteria.id` in the rank form
   (`test_jobs_page_does_not_expose_criteria_version_uuid` extended to
   cover it).
3. **Duplicate-creation safety — canonical signature, not title
   uniqueness.** Job titles remain deliberately non-unique (two vacancies
   may legitimately share a title — explicitly required). Instead,
   `meyar.ui.service.compute_job_duplicate_signature` hashes a canonical,
   order-independent, display-text-independent signature of
   (normalized title, sorted list of (kind, MUST_HAVE/PREFERRED type,
   normalized value, min_years, weight) per criterion) — never the
   free-text label or the server-generated criterion id, so two
   vacancies with the same underlying requirements are recognized as
   duplicates regardless of incidental label wording. `POST /ui/jobs`
   pre-checks for an existing `ACTIVE` job with the same signature
   (`find_active_duplicate_job`) and rejects with "Eyni tələblərlə aktiv
   vakansiya artıq mövcuddur." — no id, no hash, no internal detail
   exposed. Same title with materially different criteria is allowed (a
   different signature); an `ARCHIVED` job with an identical signature
   never blocks a new `ACTIVE` one.
4. **Concurrency — a real DB constraint, not just a pre-check.** The
   pre-check alone cannot close a genuine double-submit race (two
   requests can both pass it before either commits). The actual guard is
   a **partial unique index**,
   `uq_jobs_active_duplicate_signature` on `(tenant_id,
   duplicate_signature)` `WHERE status = 'ACTIVE' AND duplicate_signature
   IS NOT NULL` — declared identically in both the `Job` model's
   `__table_args__` (so `Base.metadata.create_all`, what the test suite
   actually builds its schema from, creates it too — the first version of
   this fix silently had *no* real constraint in tests because the index
   existed only in the Alembic migration, and the concurrency test
   correctly caught this) and the migration (so real deployments get it
   via `alembic upgrade`). A concurrent double-submit that races past the
   pre-check hits `IntegrityError` at `flush()`/`commit()`, caught in the
   router and converted to the identical friendly message. NULL
   `duplicate_signature` values are never constrained (Postgres allows
   multiple NULLs in a unique index, matching the design), so this never
   affects existing or future `POST /api/v1/jobs`-created rows.
5. **API compatibility.** `POST /api/v1/jobs` is completely untouched —
   no duplicate check, no lifecycle field accepted or required, `Job`
   rows it creates simply carry `status='ACTIVE'` (the column default)
   and `duplicate_signature=NULL` (never populated, never constrained).
   Verified explicitly with a new regression test asserting two
   API-created jobs with identical title+criteria both succeed. No
   existing schema (`JobOut`, `JobCriteriaVersionOut`,
   `ApiCandidateSearchResponse`, etc.) gained a lifecycle field in this
   pass — deliberately out of scope, since nothing requested API-visible
   lifecycle yet and every additive field is a contract decision of its
   own.
6. **Ranking/scoring untouched.** `rank_candidates_for_job` and the
   deterministic scoring engine were not modified — an archived job's
   criteria version can still technically be ranked via the existing
   route if directly invoked (e.g. a stale link), since nothing in the
   requested scope asked for a backend-level ranking block, only that the
   *normal HR UI* not invite it; the archive view simply never renders
   the rank action. `JobCriteriaVersion` rows are never deleted or
   modified by archiving, so historical `Evaluation` rows keep resolving
   correctly (`_job_titles_by_id` has no status filter, verified by a new
   end-to-end test: rank against a job, archive it, then confirm the
   candidate's evaluation history still shows the job title).

**Why:** A real HR tool needs to close vacancies without losing their
history, and needs protection against the exact accidental-duplicate
scenario already observed firsthand in this project's own demo tenant —
without over-constraining a legitimate case (the same title reused for a
genuinely different role).

**Reversibility:** Fully reversible. Migration `db7e4523f491` has a
tested `downgrade()` (columns and indexes dropped, verified by upgrade →
downgrade → re-upgrade against the real dev DB). No existing data is
deleted by either direction. Removing the `find_active_duplicate_job`
pre-check call and the partial unique index (via a follow-up migration)
would restore unrestricted duplicate creation; removing the archive route
and the `?status=` branch restores the single unfiltered listing —
neither touches scoring, evidence, or tenant isolation.

## D-029 — Vacancy-form kind-aware duration field: client-side presentation fix

**Date:** 2026-08-31
**Decision:** D-027 fixed the server-side rule (`_parse_criterion_row`
rejects a "Minimum müddət (il)"/`min_years` value on any non-EXPERIENCE
row) but never touched the form's presentation — a fourth owner visual
check confirmed the input stayed visibly enabled for every criterion
kind, inviting exactly the invalid combination the server then rejected
(Növ=Bacarıq, Tələb=Python, Təcrübə=4 → correctly rejected, but the field
never signaled that before submit).

1. **Progressive enhancement, not a new source of truth.** Server-side
   validation in `meyar.ui.service._parse_criterion_row` is unchanged and
   remains the sole authority — this pass only changes what
   `job_new.html`/`base.html` render and adds one static asset,
   `backend/src/meyar/ui/static/job-form.js` (self-hosted, loaded via
   `<script src="/ui/static/job-form.js" defer>`; no inline script, no
   CDN — compliant with the existing `script-src 'self'` UI CSP, which
   required no change).
2. **Server-rendered initial/re-rendered state, not JS-only.** Each
   criterion row's `min_years` `<input>` now carries the HTML `disabled`
   attribute at render time whenever `row.kind != "EXPERIENCE"` (Jinja:
   `{% if not is_experience %} disabled aria-disabled="true"{% endif %}`),
   and its displayed value is blanked for a non-EXPERIENCE row
   (`value="{{ row.min_years if is_experience else "" }}"`) — this holds
   for the initial `GET /jobs/new` blank-row render *and* every
   validation-error re-render, so the correct disabled/cleared state is
   present even with JavaScript disabled, not just as a JS side effect.
3. **JS enhancement handles the live, same-page case.** `job-form.js`
   listens for `change` on each `.js-kind-select`; when a row's kind
   differs from EXPERIENCE it disables the paired `.js-min-years` input,
   clears its value, and swaps its placeholder to "Tətbiq olunmur" —
   this is the only way to reproduce, without a server round-trip, "HR
   selected Təcrübə, typed 4, then switched to Bacarıq" and see the
   stale 4 disappear rather than sit in a disabled-but-still-populated
   field. Verified live in a real browser session against the running
   dev server (not just `pytest`, since `httpx` never executes page
   JavaScript): initial load showed every default SKILL row's duration
   field disabled with the "Tətbiq olunmur" placeholder; switching a
   row's Növ to Təcrübə enabled it with the "Minimum müddət (il)"
   placeholder; typing "4" then switching back to Bacarıq left the field
   disabled and empty (confirmed via `element.value === ""` and
   `element.disabled === true`, not just visual inspection); the only
   `<script>` loaded was the same-origin `job-form.js`.
4. **No-JS fallback is the pre-existing server-side rejection, not a
   parallel client-side guarantee.** A disabled HTML input is never
   submitted by the browser, so a JS-enabled client naturally can't
   reproduce the invalid combination in the first place; a client with
   JavaScript off (or a hand-crafted/malicious POST — added as an
   explicit regression test) can still submit `kind=SKILL` with a
   `min_years` value, and `_parse_criterion_row` rejects it exactly as
   before D-029 — no weakening of server-side validation was made or
   was needed.
5. **Wording.** The shared table header changed from "Təcrübə (il)" to
   "Minimum müddət (il) (yalnız Təcrübə üçün)" so the column itself no
   longer implies every criterion kind accepts a duration; the per-row
   placeholder further disambiguates "Minimum müddət (il)" (EXPERIENCE)
   vs "Tətbiq olunmur" (every other kind).
6. **Tests.** Four new cases in `test_ui_job_creation.py`: SKILL → years
   control rendered disabled/cleared; EXPERIENCE → years control
   rendered enabled with the new placeholder (via a mixed-row re-render
   that preserves a valid EXPERIENCE row's posted values alongside a
   second, invalid row); EXPERIENCE→SKILL kind-switch submission neither
   echoes the stale value back as editable nor persists a `Job`; and a
   direct manual POST of SKILL + `min_years` (the JS-disabled/malicious
   case) is still rejected server-side with no `Job` row created.

**Why:** A disabled-but-visible-anyway control is worse than either a
truly disabled one or an honest error — the owner's objection was that
the UI actively invited input the system already knew it would reject.
The fix keeps the deterministic policy engine and its validator as the
single source of truth (per project non-negotiables) while making the
form itself stop lying about which fields apply to which criterion kind.

**Reversibility:** Fully reversible, no migration, no schema change, no
change to `_parse_criterion_row` or any Pydantic schema. Removing the
`disabled`/blanked-value template logic and `job-form.js`'s `<script>`
tag restores the exact pre-D-029 form (every field always enabled) while
server-side rejection of the invalid combination is untouched either
way — the two are fully decoupled.

## D-030 — Product-direction pivot: bounded local-AI HR agent as primary future UX

**Date:** 2026-09-01
**Decision:** Following an independent full product/architecture audit
(owner-requested, `feat/hr-ui-productization` @ `2296b6f`), MEYAR adopts a
**bounded local-AI HR agent** as the primary future user experience, rather
than continuing to grow the current natural-language search/filter surface
indefinitely. This is a product-direction decision, not a code change —
implementation proceeds only through the roadmap slices this decision
authorizes (see GitHub milestones **M8 — Bounded Local-AI HR Agent
Platform** and **M9 — Deployment, Benchmark & Integration Readiness**,
issues #30–#37).

1. **Permanent product principles (unchanged, now explicit as the agent's
   operating constraint, not just the search planner's):**
   AI understands. Database remembers. Search retrieves. Deterministic
   policy evaluates. Evidence explains. Humans decide.
2. **The LLM/agent must never:** decide the final numeric score; decide a
   hiring outcome; silently weaken a requirement; fabricate an unsupported
   fact; use identity/PII secretly in ranking; or bypass tenant/auth/tool
   schemas. These are the same invariants D-016 and D-017 already enforce
   for the search planner and scoring engine respectively — this decision
   extends them to cover every future agent tool call, not just the NL
   search path.
3. **Local-only, unchanged.** Candidate-content AI remains local via Ollama
   (`meyar.llm.LLMProvider`, no direct Ollama import outside `meyar/llm/`).
   No external AI API may receive candidate content, at any point in an
   agent's tool-calling loop, exactly as already required for extraction and
   search.
4. **Target primary product surface:** "MEYAR AI" (a conversational
   workspace) + Candidate Library / Candidate Detail. Intended future
   interactions include: finding candidates from natural HR requirements;
   refining results conversationally; explaining candidate evidence;
   comparing candidates; evaluating a JD; drafting structured criteria; and
   proposing approved operational actions. Every consequential/mutating
   action requires explicit human confirmation (see D-031 point 4 and the
   Slice 5 / confirmed-actions issue, #34) — the agent proposes, it never
   silently executes a mutation.
5. **Supersedes/clarifies D-016 point 7's framing.** D-016 point 7 states
   "No cloud fallback or agent framework exists" — that sentence described
   the accurate Slice 9 baseline at the time and is **not** read retroactively
   as a permanent prohibition on ever building a local agent. D-016's other
   points (strict `PlannerDraft` boundary, deterministic mode selection,
   meaning-preserved-or-rejected, protected-criteria enforcement,
   trusted-runtime-owned execution configuration, local-LLM-only, bounded
   provenance) remain fully in force and are generalized to typed tool
   calls in general, not narrowed to the NL-search planner alone — see
   D-031.
6. **Vacancy/Job product direction** is a related but separate decision —
   see D-032.
7. **Audit scope note.** This decision was informed by, and does not
   contradict, the independent audit's finding that the deterministic
   scoring/evaluation engine (D-010, D-017) is already agent-safe
   (UI/search-agnostic, reproducible, UNKNOWN-correct) and requires no
   rework — the pivot is concentrated at the search/interaction layer
   (D-031) and the vacancy-creation UX layer (D-032), not the core engine.

**Why:** The owner's audit found the product had organically grown a
traditional search/filter application with an increasingly complex
deterministic NL-parsing layer, while the intended direction is a bounded
local-AI agent operating typed MEYAR tools under unchanged deterministic
guarantees. This decision records that direction as canonical product
authority so implementation work (M8/M9) proceeds against an unambiguous
target instead of being re-litigated per slice.

**Reversibility:** Documentation/governance only — no source code changed
by this decision. Fully reversible by a future superseding decision; no
migration, no schema change, no removed functionality. PR #29's existing
functionality is unaffected.

## D-031 — Search architecture: SearchPlan/deterministic policy become internal tool boundaries; deterministic fast-path FROZEN

**Date:** 2026-09-01
**Decision:** `SearchPlan`, `PlannerDraft`, and the deterministic
precheck/fidelity-validation machinery in
`meyar.search.planner_policy`/`planner_schemas` (D-016) **remain** — their
role is reframed, not removed:

1. **New role: internal typed tool/policy boundary, not the user-facing
   parse target.** Once Slice 2 (#31) ships, the intended shape is that a
   local agent populates tool-call arguments directly (already close to
   today's `PlannerDraft` shape — strict Pydantic, `extra="forbid"`) rather
   than the current design of guessing intent from a whole free-text
   sentence and reconciling the guess against the original text after the
   fact. `SearchPlanResult`'s executable/outcome contract is retained as-is
   — it is already the right shape for a typed tool result.
2. **The deterministic language fast-path (D-026, `try_deterministic_intent_parse`)
   is FROZEN effective immediately.** No new phrase/suffix/regex intent
   pattern is to be added to it, and its scope is not to be widened, unless
   the change is required to fix a genuine security or correctness bug in
   already-shipped behavior before agent parity exists (in the spirit of
   D-027's correctness fix, not new capability). This freeze applies to
   `planner_policy.py`'s precheck/fidelity heuristics in general: no new
   language-specific heuristic surface area is to be added there while the
   agent foundation (#31) is being built.
3. **Explicit sunset condition.** Once the read-only local agent (#31)
   demonstrates accepted functional parity — evaluated and accepted by the
   owner, not automatic — for the search intents the fast path currently
   covers, the fast path (D-026) is to be removed or materially reduced.
   D-026 already documents this as a one-line reversal: deleting the single
   `try_deterministic_intent_parse` call site in `plan_candidate_search`
   restores 100% LLM/agent-dependent behavior with no other pipeline change.
   This sunset is tracked as part of Slice 4 (#33), which is the point at
   which parity is evaluated and, if accepted, acted on.
4. **Tool-calling does not eliminate deterministic validation — it is
   subject to the same rules as today's NL path, with no exception.**
   Every LLM/agent-produced tool argument remains **untrusted input** and
   must pass, unchanged: typed schema validation (`extra="forbid"` Pydantic
   boundaries, same discipline as `PlannerDraft`); the prohibited-attribute
   policy (`find_prohibited_term`, D-006, checked before and after any model
   call); the no-silent-weakening rule (a mandatory requirement can be
   preserved or rejected, never quietly downgraded to a soft/preferred
   signal, and vice versa); tenant/auth boundaries (`tenant_id` is always an
   explicit trusted-runtime parameter, never model- or client-supplied); and
   evidence/provenance rules (no claim without a traceable `EvidenceRef` or
   persisted score/criterion result; a schema-incapable claim — e.g.
   per-skill duration until Slice 3/#32 closes that gap — is `UNKNOWN`,
   never fabricated, exactly as D-027 already established for the NL path).
   An agent tool-dispatch layer is a new *producer* of these arguments; it
   is never a new *validator* of them, and it does not get a weaker or
   parallel validation surface.
5. **Conversation/multi-turn state.** As D-026 already noted but left
   unimplemented, a server-held, tenant-scoped, short-TTL pending-action/
   plan mechanism (extending `BrowserSession`, not a new auth system) is the
   intended shape for multi-turn clarification and for the propose→confirm
   pipeline (Slice 5, #34) — the LLM proposes each turn, but the persisted
   pending-action row, not the model's own memory, is authoritative for
   what actually executes on confirmation.

**Why:** The audit found `planner_policy.py` had grown three overlapping
NL-understanding subsystems (precheck rejection patterns, the D-026 fast
path, and post-hoc fidelity re-parsing) totaling 992 lines, +451 in the
`feat/hr-ui-productization` slice alone — a trajectory that, left
unacknowledged, risks becoming a permanent pseudo-NLP engine parallel to,
rather than replaced by, a future agent. Freezing new heuristic growth now
and recording an explicit, evaluatable sunset condition prevents that
outcome without discarding anything already shipped and verified.

**Reversibility:** Documentation/governance only — no source code changed
by this decision. The freeze and sunset condition are policy, not a code
change; D-026's own fast path remains merged and functional until the
sunset condition is met and acted on in a future slice. Fully reversible by
a future superseding decision.

## D-032 — Job/Vacancy product direction: backend retained, primary UX deferred to agent-drafted criteria flow

**Date:** 2026-09-01
**Decision:** The existing `Job` / `JobCriteriaVersion` / deterministic
evaluation backend (D-010, D-017, D-028) is **retained as-is** and requires
no rework for the agent direction (D-030) — the audit confirmed
`evaluation`/`scoring` import only `meyar.schemas`/`meyar.models`, with no
`ui`/`search` coupling, and are already safe to wrap in an agent tool
(`rank_candidates_for_job`) without modification.

1. **No further vacancy-management CRUD growth.** The Job/vacancy lifecycle
   work already merged (D-028: ACTIVE/ARCHIVED, duplicate-signature safety)
   and the hand-built criteria form (D-023/D-024/D-025/D-029) are kept
   as-is and are not to be extended with additional lifecycle states,
   workflow steps, or form-UX polish beyond what already exists, ahead of
   the agent-drafted-criteria decision below.
2. **The current user-facing Vacancies section is supporting/deferred
   functionality, not the intended primary product workflow.** It remains
   fully functional and reachable; it is not being removed by this
   decision. Its primary-navigation prominence is a Slice 4 (#33) UX
   decision, gated on the agent-drafted-criteria replacement flow being
   accepted by the owner — this decision does not itself change any
   navigation or template.
3. **Future primary workflow:**
   ```
   HR/JD request → local agent → structured criteria DRAFT
     → human review/confirmation → deterministic evaluation
   ```
   The agent drafts (`draft_job_criteria`, Slice 4/#33); nothing is
   persisted until an accountable human confirms
   (`create_job_after_confirmation`, Slice 5/#34, gated on Slice 1/#30 for
   accountable identity). The existing `CriterionKind`/`CriterionType`/
   weight taxonomy and the prohibited-attribute denylist
   (`schemas/criteria.py`, D-006) are the target shape the agent populates
   — unchanged by this decision.
4. **The current vacancy-creation form (`job_new.html`) may later be
   repurposed as the human review/edit screen for an agent-drafted
   criteria set**, per Slice 4 (#33). This decision authorizes that future
   repurposing; it does not implement it.

**Why:** The audit found vacancy criteria are entirely hand-built via form
today — a real HR-usability gap — while the model/schema layer underneath
is already exactly the right shape for an agent to populate as a reviewable
draft. Recording this now prevents further investment in the hand-built
form as a permanent primary path while the higher-leverage agent-drafting
capability is unbuilt.

**Reversibility:** Documentation/governance only — no source code changed
by this decision, no existing functionality removed. Fully reversible by a
future superseding decision.

## D-033 — Ranking lifecycle enforcement: an ARCHIVED job can no longer be ranked, at the shared-service boundary

**Date:** 2026-09-01
**Decision:** The independent PR #29 acceptance audit confirmed a real gap
disclosed by D-028 point 6: `rank_candidates_for_job`
(`meyar.scoring.batch`) never checked the parent `Job.status`, so an
`ARCHIVED` job's criteria version remained fully rankable via a direct or
stale request even though the normal HR UI hid the "Reytinq et" action.
Closed at the shared service every caller goes through, not only at a
router/template layer:

1. **Enforcement location.** `rank_candidates_for_job` now resolves the
   parent `Job` immediately after resolving the `JobCriteriaVersion`
   (`meyar.services.job_repo.get_job`, tenant-scoped) and raises
   `BatchRankingError("JOB_ARCHIVED", ...)` unless `Job.status ==
   JOB_STATUS_ACTIVE`, before any candidate/profile iteration or
   `Evaluation` construction begins. Because this is the one shared
   service the UI, the REST API, the CLI, and any future agent tool all
   call, none of them can bypass the check by avoiding the UI — the exact
   property the audit asked for.
2. **API/CLI impact — deliberate, not incidental.** `POST
   /api/v1/jobs/{job_id}/criteria/{version_number}/rank`
   (`meyar.api.v1.evaluations.post_rank_job`) and `meyar rank-job` (CLI)
   both call the same function and are therefore now also protected by
   this invariant, closing the same latent gap on those paths — the API
   route maps `JOB_ARCHIVED` to `409 Conflict` (distinct from the existing
   `404`/`422` mapping for other `BatchRankingError` codes); the CLI
   already prints any `BatchRankingError.code` generically, so no CLI
   change was needed.
3. **HR-safe UI wording, no raw code.** `meyar.ui.router.rank_job` renders
   a dedicated, Azerbaijani, human-readable message ("Bu vakansiya
   arxivləşdirilib və artıq yeni reytinq üçün istifadə edilə bilməz.") with
   HTTP `409` for this specific code — the raw string `JOB_ARCHIVED` is
   never rendered to the HR user (regression-tested).
4. **Historical data is untouched.** The check only gates *new* ranking
   execution; it does not read, modify, or gate access to any existing
   `Evaluation` row. `archive_job` (unchanged by this decision) still only
   ever sets `status`/`archived_at` on the `Job` row itself —
   `JobCriteriaVersion` and `Evaluation` history remain fully intact and
   readable (regression-tested: a candidate's evaluation history still
   shows the job title, and shows exactly one entry, not two, after a
   rejected re-rank attempt against the now-archived job).
5. **No new lifecycle state.** Only the existing `ACTIVE`/`ARCHIVED`
   values (D-028) are read; nothing new was added to the `Job` model or
   migration chain.
6. **Tenant isolation unaffected.** The new check runs only after
   `get_criteria_version_by_id`'s existing tenant-scoped lookup already
   succeeded, and `get_job` is itself tenant-scoped — a foreign tenant's
   criteria-version id still resolves the pre-existing, unchanged
   `CRITERIA_VERSION_NOT_FOUND`/404 outcome regardless of that foreign
   job's status, so this change introduces no new cross-tenant
   existence-leak surface (regression-tested).
7. **Scoring mathematics unchanged.** No line in `meyar.scoring.policy` or
   `meyar.evaluation.*` was touched.

**Tests:** `backend/tests/test_ui_job_lifecycle.py` —
`test_active_job_can_still_be_ranked`,
`test_archived_job_direct_stale_rank_post_is_rejected_with_hr_safe_message`
(rank while ACTIVE succeeds and is preserved; archiving; a stale/direct
POST to the same rank URL is rejected with `409` and the HR-safe message,
never the raw code; the candidate's evaluation-history view shows exactly
one entry afterward, proving no second `Evaluation` was persisted by the
rejected attempt), and
`test_archived_job_rank_rejection_does_not_leak_cross_tenant`.
`backend/tests/test_api_evaluations.py` —
`test_rank_archived_job_is_rejected_via_shared_service` (API path returns
`409` with `detail: "JOB_ARCHIVED"`, proving the shared-service enforcement
reaches the REST API too).

**Why:** ARCHIVED is meant to represent "not a current, evaluable
vacancy" (D-028). Leaving ranking execution reachable via a stale URL
undermined that invariant at exactly the point that matters most — new
`Evaluation` rows being created against a closed vacancy — and would have
become materially riskier once a future agent (Slice 2, #31) can invoke
ranking as a typed tool with no lifecycle awareness of its own. Enforcing
in the shared service, not the router, means that risk is closed
structurally rather than by convention.

**Reversibility:** Fully reversible and additive. No migration, no schema
change, no new lifecycle state. Removing the `Job.status` check in
`rank_candidates_for_job` restores the exact pre-fix behavior on all three
call paths simultaneously.

**Follow-up tracking (not fixed here, per audit scope):** UI/API
duplicate-signature consistency, concurrency-test hardening, and small
UI/test cleanup findings from the same acceptance audit are tracked
separately — see issue #38.

## D-034 — Slice 1: Human Identity & Dual Access — schema, hashing, and session-principal design

**Date:** 2026-09-01
**Decision:** Implements M8 Slice 1 (issue #30, per D-030). A human
identity/session layer is added alongside the unchanged machine API-key
path, per the following design choices:

1. **`User`/`TenantMembership` shape.** `User` (id, username, password_hash,
   is_active, timestamps) carries no tenant reference — a user may belong
   to more than one tenant (spec requirement). `TenantMembership` (user_id,
   tenant_id, role, is_active) is the sole authorization join; it is
   unique on `(user_id, tenant_id)`. Neither table stores or derives from
   `CandidateIdentity` — `username` is a login identifier, never a
   matching/ranking signal.
2. **Password hashing: Argon2id via `argon2-cffi`.** The only new runtime
   dependency this slice adds. Chosen over a zero-dependency
   `hashlib.pbkdf2_hmac` scheme because Argon2id is OWASP's first
   recommendation and the encoded hash string self-describes its
   parameters, so a future tuning change never invalidates already-stored
   hashes (`meyar.core.password.needs_rehash` silently upgrades on next
   successful login). Pure local computation, no network call — compatible
   with the local-only boundary the rest of the platform already enforces.
3. **`BrowserSession` becomes a human-only principal.** `api_key_id` is
   replaced with `(user_id, tenant_membership_id)`, both `NOT NULL` — a
   clean break, not a nullable/polymorphic dual-shape column, because no
   route creates an API-key-bridged UI session anymore after this slice
   (the old exchange this replaced, D-018 point 2, is retired) and keeping
   the old shape "just in case" would be dead, untested surface area. Every
   authenticated request re-derives `User.is_active`/
   `TenantMembership.is_active` live from the database (same pattern as
   the existing `ApiKey` live-recheck, D-018 point 4) — a disabled user or
   a revoked membership takes effect immediately, no re-login required.
4. **Role model: intentionally minimal.** `meyar.core.roles` defines
   `HR_USER`/`ADMIN` with an identical permission set today — repository
   evidence showed no existing UI action that is actually admin-only, so
   inventing a differentiated permission set now would be speculative. The
   mapping is centralized (`permissions_for_role`) and fails closed: an
   unrecognized role resolves to zero permissions, never full access.
5. **Multi-tenant-membership login: a stateless signed token, not a new
   session table.** A user with more than one active membership is shown a
   server-rendered tenant-selection screen; the bridge between "password
   verified" and "tenant chosen" is a short-lived (5 min) HMAC-signed
   token (`meyar.ui.pending_login`, new `MEYAR_PENDING_LOGIN_SECRET`
   setting) carrying only the authenticated `user_id` — never a
   client-supplied tenant/membership id trusted directly. The selected
   `membership_id` is always re-validated live against the database
   (belongs to this user, is active) before a real `BrowserSession` is
   minted. Chosen over a persisted pending-login row because it requires
   no schema addition and the token proves nothing except "this user's
   password was already verified" — the authorization decision itself is
   still made from live data on every use.
6. **Structured audit actor identity, not metadata.** `AuditEvent` gains
   nullable `actor_type`/`actor_id` columns (`ACTOR_HUMAN_USER`/
   `ACTOR_API_KEY`/`ACTOR_SYSTEM`) rather than writing an actor reference
   into the existing PII-guarded `metadata` JSON — first-class columns
   keep the existing `_FORBIDDEN_METADATA_KEYS` guard's intent intact
   while giving Slice 5 (confirmed actions) the accountable-actor field it
   will need. Both columns are nullable so every pre-existing event, and
   every call site this slice didn't touch, remains valid and unclaimed
   — never silently coerced into a human or machine attribution. Wired
   into UI login/logout/job-create/job-archive and the two REST mutation
   routes that already had a `TenantContext` in scope
   (`POST/DELETE /api/v1/candidates`, `POST /api/v1/jobs`); other existing
   `record_event` call sites are left unattributed (`NULL`) rather than
   expanding this slice's blast radius to every mutation in the codebase.
7. **Two bugs found and fixed by this slice's own test suite, before
   merge:**
   - `reset_demo` deleted the demo tenant but not the demo `User` row
     (`User` is not tenant-owned, so no `ondelete=CASCADE` covers it) —
     orphaning the demo human login across a reset→reseed cycle and
     causing the next `seed_demo` to misidentify the orphaned user as an
     unrelated name collision. Fixed: `reset_demo` also deletes the demo
     `User` row, but only when it is positively confirmed to hold a
     membership on the exact tenant being deleted — the same
     collision-safety discipline as the existing tenant-identification
     guard (D-022), never a blind delete-by-username.
   - `get_user_by_username`/`get_membership_for_user_and_tenant` lacked
     `execution_options(populate_existing=True)`, so a row already in a
     session's identity map could return stale `is_active` data after a
     same-session update — exactly the failure mode `get_api_key_by_id`
     already guards against (D-018). Fixed by applying the same option.
     No production request is affected (each request gets its own fresh
     session with an empty identity map), but the fix keeps the codebase's
     "live recheck" repository functions consistent and closes a latent
     footgun for any future same-session reuse.

**Why:** Slice 5 (Confirmed Actions Framework, #34) requires that every
human confirmation of an agent-proposed mutation be attributable to a
specific accountable person — this slice is the identity/session
prerequisite, not a parallel nice-to-have (see issue #30's own framing).

**Reversibility:** Additive with one narrowing change: `BrowserSession`
downgrade discards any live session rows (documented in the migration
itself — sessions are short-lived, revocable, and never durable identity
data, so this only forces re-login, not data loss). `User`/
`TenantMembership`/the `AuditEvent` actor columns can all be dropped by
the migration's `downgrade()`. The machine API-key path
(`meyar.core.auth`) was not modified by this decision at all.

## D-035 — Slice 2: Read-Only Local AI Agent Foundation — tool-result
evidence grounding, and a real Ollama `format`-schema `maxLength` limit

**Date:** 2026-09-01
**Decision:** Implements M8 Slice 2 (issue #31, per D-030/D-031). A
bounded orchestration loop (`meyar.agent.service.run_agent_turn`) with
exactly three read-only tools — `search_candidates`,
`get_candidate_profile`, `get_candidate_evidence` — dispatched from one
strict, `extra="forbid"` model-output schema (`AgentDecision`, mirroring
`PlannerDraft`'s discipline). Two design points and one real bug, found
and fixed during this slice's own manual real-Ollama verification:

1. **Evidence grounding is structural, not a prompt instruction.** The
   model's `message` field (used only by `FINAL_ANSWER`/`CLARIFY`) is
   framing/clarifying text alone; every factual claim about a candidate
   comes from a separate, typed, deterministic tool-result payload
   (`AgentToolResult`) that the UI layer renders independently — the
   model's own words are never the record a fact is checked against.
   `search_candidates` forwards the model's restated query, unmodified,
   into the existing frozen `plan_and_search_candidates` pipeline
   (D-026/D-027/D-031 guarantees reused as-is). `get_candidate_profile`/
   `get_candidate_evidence` resolve a model-produced ordinal
   `candidate_ref` only against the conversation's own server-held
   `last_search_candidate_ids` — the model is never shown or trusted
   with a real `candidate_id` — and both always finalize the turn
   immediately, found or not, so a small local model never gets a second
   inference pass to freelance about an answer that is already complete.
2. **Real bug: Ollama's JSON-schema `format`-constrained decoding fails
   outright above a `maxLength` of roughly 2000.** `AgentDecision.
   search_query` was originally capped at 4000 (matching the raw HR
   message field). Every real call to the new
   `OllamaLLMProvider.decide_agent_action` against a real local Ollama
   daemon (`qwen3:0.6b`) failed with HTTP 500
   (`"failed to load model vocabulary required for format"`) — a
   request-time provider failure, never a validation-time symptom, so no
   `FakeLLMProvider`-based test could have caught it. Bisected precisely
   (isolated to the single `search_query` field, then to its `maxLength`
   value specifically) by posting hand-built schema variants directly to
   the real Ollama `/api/chat` endpoint outside the application. Fixed by
   capping `search_query` at a new `MAX_AGENT_SEARCH_QUERY_LENGTH = 2000`
   — the same bound already proven safe in production on
   `PlannerDraft.semantic_query` (`meyar.search.policy.
   MAX_SEMANTIC_QUERY_LENGTH`). Verified fixed both directly (repeated
   real-Ollama calls) and through a real authenticated browser session
   end to end. A regression test
   (`test_search_query_max_length_stays_within_the_ollama_grammar_safe_bound`)
   pins the bound itself, since no functional test can otherwise detect
   a future regression here.
3. **Ollama inference concurrency guard.** `Settings.inference_concurrency`
   existed since an earlier slice but was never wired to anything. Added
   `meyar.llm.concurrency`: one process-wide (module-level, not
   per-instance) `asyncio.Semaphore`, sized from that setting, shared by
   every `OllamaLLMProvider._chat` call — extraction, identity
   extraction, NL search planning, and the new agent loop alike — so no
   combination of concurrent browser sessions can exceed the configured
   local-inference budget.

**Why:** A tiny local model (the only kind this project can assume on
constrained target hardware — see the still-open Mac Mini benchmark,
issue #20/M5) cannot be trusted to narrate candidate facts reliably;
structural grounding removes that trust requirement entirely rather than
asking the prompt to enforce it. The `maxLength` limit is genuinely
non-obvious, hardware/model-dependent, and would silently break any
future agent-facing Pydantic schema that adds a long free-text field
without re-testing against a real Ollama daemon — recording it here is
the only way a future slice avoids re-discovering it the same way.

**Reversibility:** Fully additive — new package (`meyar.agent`), new
table (migration `a1c5e9f2b6d3`, cleanly dropped by `downgrade()`), new
`LLMProvider.decide_agent_action` protocol method. No existing search,
scoring, or evaluation behavior changed. The `MAX_AGENT_SEARCH_QUERY_LENGTH`
bound and the concurrency guard are both simple constant/wiring changes,
trivially adjustable if re-verified against different hardware or a
different local model.

## D-036 — Slice 2 correctness fix: a successful tool result must never
co-render with a fatal-looking outcome, and an assistant turn must never
be stored/shown blank

**Date:** 2026-09-01
**Decision:** Owner live inspection of PR #40 found `/ui/agent` rendering
an empty assistant bubble, a red "AI response could not be safely
processed" error, a simultaneous green "query executed" banner, and a
valid candidate card with a misleading "Uyğunluq 0%" badge — all for one
turn. Root-caused (not guessed) with a real-DB repro test before any fix:
`meyar.agent.service.run_agent_turn`'s loop always asks the model a
second "what next" decision after a successful `SEARCH_CANDIDATES` call;
when that second call failed (repeated schema-invalid output, or a
provider timeout/outage), the returned `AgentTurnResult` carried
`outcome=MALFORMED_MODEL_OUTPUT` (red, by name-collision with the
existing `PlannerOutcome.MALFORMED_MODEL_OUTPUT` CSS rule) while still
carrying the FIRST call's successful `tool_results` — the search's own
outcome banner (green, `EXECUTABLE`) and candidate card rendered
underneath the red one. The stored assistant turn was `""`, redisplayed
verbatim as an empty bubble.

1. **A follow-up decision failure is never fatal once a tool already
   succeeded.** New `AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT`: used
   whenever the loop's second-or-later `decide_agent_action` call fails
   (schema-invalid, timeout, unavailable) but `tool_results` is
   non-empty, and whenever `GET_CANDIDATE_PROFILE`/`GET_CANDIDATE_EVIDENCE`
   succeeds (neither ever carries a model message — there is no
   "framing" to fail). Plain `ANSWERED` is now reserved for a real
   model-authored `FINAL_ANSWER`, which `AgentDecision`'s own shape
   validator already guarantees always carries a non-empty `message` — so
   `ANSWERED` with `message=None` is unreachable, and grounded tool
   results never again co-occur with `MALFORMED_MODEL_OUTPUT`/
   `AGENT_PROVIDER_FAILURE` (both now only ever returned with
   `tool_results == []`).
2. **No fabricated fallback text in the service layer.** AZ-language text
   continues to live only in `meyar.ui.presentation`
   (`AGENT_TURN_OUTCOME_TEXT["ANSWERED_FROM_TOOL_RESULT"] = "Nəticələr
   aşağıdadır."`) — `meyar.agent.service` stays UI-agnostic, matching
   `meyar.search.planner_service`'s existing precedent.
3. **A stored assistant turn is never blank.** `conversation.turns` now
   persists `(outcome, message)` per assistant turn instead of
   pre-rendered text; `meyar.ui.router._agent_turn_log_views` redisplays
   past turns through the exact same `agent_turn_outcome_message`
   function the live turn's banner uses, so a past turn is exactly as
   informative on reload as it was live, and is never empty (every
   `AgentTurnOutcome` has a non-empty deterministic fallback, verified by
   a regression test that loops over the whole enum).
4. **No misleading relevance percentage on unscored discovery.** The
   agent's search-result card (`agent.html` only — the classic
   `/ui/search` page is unchanged) now shows the "Uyğunluq X%" pill only
   for `SEMANTIC_ONLY`/`HYBRID` search modes, where `relevance_score` is a
   real similarity signal. A plain `STRUCTURED_ONLY` discovery query with
   no `preferred_filters` always produces `relevance_score == 0.0` by
   construction (no preferred criteria are being scored) — that is not a
   deficiency to hide, it is simply not a percentage, so the card shows
   matched required/preferred criteria instead (already rendered).

**Why:** A contradictory red-error/green-success render is a trust-safety
defect for an HR-facing decision-support tool, independent of local-model
quality — the owner's framing ("model parse/timeout/failure states MUST
produce coherent UI") is the correct bar regardless of what hardware or
model MEYAR eventually runs on.

**Reversibility:** Fully additive to the Slice 2 (D-035) design — one new
`AgentTurnOutcome` member, one new `AGENT_TURN_OUTCOME_TEXT` entry, a
`conversation.turns` shape change (`text` + `outcome` instead of
pre-rendered `text` alone; no migration, JSON column, old rows still
readable via `.get()` defaults), and a template-only conditional for the
relevance pill. No scoring/evaluation math changed, no planner regex
changed, no navigation removed.

## D-037 — Slice 2 product-gap fix: grounded conversational answers for
profile/evidence explanations, with independent server-side validation

**Date:** 2026-09-02
**Decision:** Owner inspection found that "birincinin təcrübəsini izah
et" (explain the first one's experience) correctly resolved the ordinal
and fetched the right profile/evidence data, but the assistant only ever
said the generic D-036 fallback ("Nəticələr aşağıdadır.") while the UI
dumped the full structured profile below it — not a coherent explanation.
Root cause: D-035's design deliberately restricted the model's `message`
to framing text only, with no path for it to synthesize prose from a
tool's own facts. Closed with the smallest workable grounded-answer
contract, not a general-purpose agent framework:

1. **A second, narrow LLM call, used only after a successful
   `GET_CANDIDATE_PROFILE`/`GET_CANDIDATE_EVIDENCE`.**
   `LLMProvider.synthesize_grounded_answer(question, facts)` (new
   protocol method, reusing `OllamaLLMProvider._chat` — the existing
   concurrency guard applies automatically) is given the user's own
   question and a small, bounded, INDEXED list of `GroundedFact` (id,
   category, title, detail) built server-side from the SAME
   already-extracted, already-schema-validated `CandidateProfileExtraction`
   fields `_EVIDENCE_CATEGORIES` already uses for topic matching — never
   raw `evidence.quote` CV text (that stays server-rendered-only, never
   model input, for both tools identically), never `CandidateIdentity`.
2. **The model's `GroundedAnswer` (`answer` + `used_facts`) is never
   trusted at face value.** `meyar.agent.service._validate_grounded_answer`
   independently re-checks it against the exact facts supplied: rejects
   an answer citing zero facts; rejects any `used_facts` id that was
   never actually given to the model; and — the concrete, testable guard
   against an invented duration/count, the owner's explicit safety
   concern — rejects an answer stating any number that does not appear
   verbatim in the title/detail of the facts it was given. A rejected
   answer is discarded outright, never partially trusted or repaired
   into something "close enough."
3. **Failure is never a turn failure.** `_synthesize_grounded_answer`
   returns `None` on any provider error, repeated schema-invalid output,
   or a failed validation, and the caller treats `None` exactly like "no
   model framing available" — the existing D-036 `ANSWERED_FROM_TOOL_RESULT`
   deterministic fallback, tool results intact, no contradictory error
   state. The bounded two-attempt repair pattern already used for
   `decide_agent_action` is reused unchanged.
4. **Scope.** Applies only to `GET_CANDIDATE_PROFILE`/
   `GET_CANDIDATE_EVIDENCE` (the owner's exact repro) — `SEARCH_CANDIDATES`
   is unchanged, still governed entirely by the existing orchestrator
   decision loop. No planner regex, scoring math, evidence schema, or
   Search/Vacancies navigation changed.
5. **UI.** The synthesized (or deterministic-fallback) message renders as
   the turn's existing outcome banner, ABOVE the unchanged structured
   profile/evidence cards, which now read as supporting evidence rather
   than the entire answer — a "Tam profilə bax" link was added to the
   evidence card (the profile card already had one), closing the one
   missing piece of the owner's UI requirement.

**Why:** A local-AI agent that can correctly fetch grounded facts but
cannot say anything about them beyond a fixed generic sentence does not
meet the product's own bar (`docs/PROJECT_VISION.md`: "AI understands...
evidence explains"). The fix keeps the LLM's role exactly where D-030/
D-031 already draw the line — interpretation and phrasing, never fact
authority — by making every claim the model's prose contains
independently checkable against data the server already trusts.

**Reversibility:** Fully additive to the Slice 2 (D-035/D-036) design —
one new provider method, two new schemas (`GroundedFact`,
`GroundedAnswer`), no migration, no change to `AgentTurnResult`'s shape
beyond how `message` is now sometimes populated. Deleting the
`_synthesize_grounded_answer` call site restores the exact D-036
behavior (deterministic fallback only) with no other change required.

## D-038 — Slice 2 factuality hardening: the server, not the model, is
authoritative over every span of factual text in a grounded answer

**Date:** 2026-09-02
**Decision:** Owner review of D-037's `GroundedAnswer.answer` free-text
field found it could not prevent an unsupported NON-numeric claim: given
only the fact "Data Analyst at Caspian Analytics, 2021–2025", the model
could still write "He managed a team there" — a predicate absent from
every supplied fact — and D-037's validation (fact-id citation + numeric
grounding) would not catch it, since it never inspected qualitative
content at all. Fixed by removing the model's ability to author sentence
text altogether, not by trying to detect bad prose after the fact (which
would need an LLM judge — explicitly out of bounds):

1. **`GroundedAnswer` (D-037) is replaced by `GroundedSelection`
   (`used_facts: list[int]`, `caveat: GroundedCaveat | None`).** There is
   no `answer` field, no free-text field of any kind — `extra="forbid"`
   makes attempting to add one a validation error, not merely a policy
   ask. The model's only two levers are: which already-supplied
   `GroundedFact` ids are relevant (and in what order to lead with them),
   and whether to flag `DURATION_NOT_PROVEN` — a single, fixed,
   non-extensible caveat enum member (mirroring the D-027 skill-duration
   precedent), never a free-text caveat.
2. **`meyar.agent.service.render_grounded_answer` builds the entire
   displayed sentence server-side** from `_render_fact_clause` — one
   fixed AZ template per `GroundedFact.category` — applied to the
   selected facts' own `title`/`detail` values, joined in the model's
   chosen order. Every span of the resulting string is either one of a
   small number of hand-authored template phrases or a verbatim value
   already sourced from the same already-schema-validated
   `CandidateProfileExtraction` fields the rest of this module treats as
   trusted (D-030/D-031) — there is structurally no channel through
   which "managed a team," an invented duration, or any other
   unsupported predicate could appear. This is checked, not just argued:
   `test_render_grounded_answer_never_contains_unsupported_claim` and an
   HTTP-level equivalent assert the literal absence of such words from
   real rendered output over the exact reported repro facts.
3. **Validation surface shrinks accordingly.** `render_grounded_answer`
   only re-checks that every selected id was actually supplied (D-037's
   fact-existence check, kept unchanged) — the D-037 numeric-hallucination
   regex is deleted outright, because it is now structurally impossible
   for a number to appear in the answer that didn't already come from a
   fact's own `title`/`detail`.
4. **Failure handling, scope, PII/CV boundary, and the bounded two-attempt
   repair mechanism are all unchanged from D-037** — an unrenderable
   selection (no facts, no caveat, or an invalid id) still falls back to
   the existing D-036 deterministic message, never a turn failure; only
   `GET_CANDIDATE_PROFILE`/`GET_CANDIDATE_EVIDENCE` are affected;
   `SEARCH_CANDIDATES`, scoring, planner regex, and navigation are
   untouched.

**Why:** "The model selects, the server writes" is the only design that
can make "no unsupported non-numeric claim can survive" a structural
guarantee rather than a best-effort filter — the owner's bar was
explicit that this must not depend on pattern-matching model prose after
the fact, and no second LLM is permitted to serve as a factuality judge.

**Reversibility:** Fully additive to the Slice 2 (D-035/D-036/D-037)
design — one schema rename/shape change (`GroundedAnswer` ->
`GroundedSelection`), one provider method rename
(`synthesize_grounded_answer` -> `select_grounded_facts`), one new pure
rendering function, no migration. No scoring/evaluation math, planner
regex, evidence schema, or navigation changed.

## D-039 — Slice 2 acceptance-loop hardening: a real Ollama bug
(hidden-thinking latency) and a real orchestration bug (redundant
identical searches) found via the PR #40 real 3-turn qwen3:1.7b flow

**Date:** 2026-09-02
**Decision:** Owner-directed autonomous acceptance testing of PR #40 ran
the exact real Ollama flow ("Python bilən namizədləri göstər" ->
"birincinin təcrübəsini izah et" -> "onun Python təcrübəsi neçə ildir?")
against a real local `qwen3:1.7b` daemon (8 GB dev hardware) and found two
real, reproducible defects — neither visible to `FakeLLMProvider`-based
tests, matching the D-035 precedent that only a real daemon can surface
them:

1. **Real bug: qwen3's default hybrid "thinking" mode makes every
   schema-constrained call ~5x slower**, which turned Turn 1's very first
   `decide_agent_action` call into an outright `AGENT_PROVIDER_FAILURE`
   (measured cold-load latency: ~34s with thinking on vs. ~6.5s with
   `think: false`, isolated by posting both payload variants directly to
   the real Ollama `/api/chat` endpoint outside the application, mirroring
   D-035's isolation method). Every prompt in `meyar.agent.prompts` /
   `meyar.search.planner_prompts` already forbids chain-of-thought output,
   so the hidden reasoning was pure waste, not a quality tradeoff being
   traded away. Fixed by adding `"think": False` to
   `OllamaLLMProvider._chat`'s request payload — applies uniformly to
   every call site (extraction, identity extraction, NL search planning,
   agent decision, grounded-fact selection), consistent with there being
   exactly one `_chat` method.
2. **Real bug: the orchestration loop had no guard against a model
   re-issuing an identical `SEARCH_CANDIDATES` call.** Once (1) made the
   model fast enough to reliably reach a second "what next" decision
   within one turn, it would sometimes re-search the exact same query it
   had already fully answered, eventually hitting
   `TOOL_CALL_LIMIT_EXCEEDED` — which co-rendered a "simplify your query"
   message above several duplicated, otherwise-correct result blocks: a
   contradictory-looking state matching the D-036 class of defect. Fixed
   structurally, not by a prompt instruction alone (this module's D-038
   precedent): `run_agent_turn` now tracks the (folded) search queries
   already executed within this one turn and finalizes on the existing
   `tool_results` as `ANSWERED_FROM_TOOL_RESULT` the moment an identical
   query recurs, never re-dispatching or spending another real tool/model
   call. Scoped to a single turn's local loop state only — never
   persisted, never shared across turns/tenants/sessions.
3. **Prompt clarification (`AGENT_PROMPT_VERSION` bumped to
   `agent-orchestrator-prompt-v2`).** The real flow's Turn 2 and Turn 3
   also showed two related routing gaps: `evidence_topic` had no guidance
   for a general category ask ("təcrübəsini" — "his experience", inflected)
   vs. one specific named fact, so a category-level question could
   topic-filter itself down to zero evidence matches; and a duration
   question about an already-identified candidate ("onun Python təcrübəsi
   neçə ildir?") was answered with `CLARIFY` echoing the user's own
   question verbatim instead of using the already-built D-038
   evidence+caveat mechanism designed for exactly this case. The
   `GET_CANDIDATE_EVIDENCE`/`CLARIFY` bullets in `AGENT_SYSTEM_PROMPT` now
   say explicitly: set `evidence_topic` only for one named skill/fact,
   leave it unset for a whole-category ask; a pronoun referring to an
   already-discussed candidate resolves to that candidate_ref rather than
   `CLARIFY`; a duration/count question about an identifiable candidate
   always routes to `GET_CANDIDATE_EVIDENCE` (the D-038 caveat mechanism
   decides provability, never the model directly). This is prompt guidance
   only — it does not change the underlying grounding mechanism, and a
   small model can still make an imperfect tool choice (explicitly out of
   scope per the owner's own acceptance framing: latency and semantic
   sophistication are not acceptance criteria).

Re-verified end to end after the fix: all three turns of the real flow
produced a single, coherent, evidence-grounded, non-contradictory result
each, with Turn 3 correctly stating the available evidence does not prove
a specific Python duration (`Mövcud sübut konkret müddəti göstərmir.`)
without ever deriving "2021–2025 = 4 years of Python" or silently
substituting total experience for it.

**Why:** Slice 2's own acceptance bar (issue #31, D-035/D-036/D-037/D-038)
requires a real candidate result with no contradictory success/error
state — a request-shape latency bug and an unbounded-loop-adjacent
orchestration gap both defeat that bar independently of model quality,
and (per D-035's own precedent) neither is discoverable without a real
local daemon.

**Reversibility:** Fully additive/corrective to the Slice 2 (D-035
through D-038) design. `think: false` is a per-request Ollama parameter,
not a model/config change — reverting it is a one-line diff with no
migration impact. The redundant-search guard is pure orchestration-loop
logic (`meyar.agent.service`), no schema/migration change. The prompt
edit only affects `AGENT_SYSTEM_PROMPT` text and its version constant. No
scoring, planner regex, Search/Vacancies UI, or mutation path touched.

## D-040 — Scope correction: `think: false` narrowed to agent-only calls,
never applied to extraction/identity/planner

**Date:** 2026-09-02
**Decision:** D-039 disabled Ollama's hidden-thinking mode on every
`OllamaLLMProvider._chat` call — correct for the two agent call sites
(`decide_agent_action`, `select_grounded_facts`), which is what Slice 2's
own acceptance run actually exercised, but out of scope for
`extract_candidate_profile`/`extract_candidate_identity`/
`plan_candidate_search`: those are previously-accepted AI behavior on
`main` from earlier slices (4, 7, 9), and changing their real-model
generation behavior — even to make it faster — is a model-behavior change
with no dedicated extraction/planner quality benchmark backing it, not a
Slice 2 bug fix.

Narrowed: `OllamaLLMProvider._chat` gained a `think: bool | None = None`
parameter. When omitted, the request payload has **no** `think` key at
all — byte-identical to every pre-D-039 call, the exact previously-
accepted request shape. Only `decide_agent_action`/`select_grounded_facts`
now pass `think=False` explicitly; `extract_candidate_profile`,
`extract_candidate_identity`, and `plan_candidate_search` pass nothing and
so keep relying on the Ollama/model default exactly as before D-039. Two
new regression tests assert the two agent call sites still send
`think: false`; three new regression tests assert extraction, identity,
and planner requests never carry a `think` key at all.

Re-verified end to end after the narrowing: the real 3-turn qwen3:1.7b
flow (Slice 2 acceptance) still produces the same coherent, grounded,
non-contradictory result each turn as under D-039 — narrowing the change
to the two agent call sites that actually needed it does not reintroduce
the cold-start timeout or the redundant-search loop, both of which were
never touched by this correction.

**Benchmark issue-reference correction:** the target-hardware benchmark
that would formally validate real-model generation-parameter changes like
this is issue #36 ("Slice 7 — Real Target-Mac Model Selection &
Benchmark", M9) — its matrix explicitly covers "Agent understanding
(tool-call accuracy/reliability)" as a dimension distinct from #20's
original scope. Issue #20 ("Slice 13 — Security + Official
Definition-of-Done Acceptance", M5) is the broader MVP-acceptance/DoD
issue that happens to include a target-hardware benchmark execution gate
among many unrelated gates (security, backup/restore, the original-CV
route) — it is not itself "the benchmark issue" and should not be cited
as such going forward; #36 is. Both remain open and neither supersedes
the other (per #36's own text).

**Why:** the owner's Slice 2 acceptance framing was explicit that a
real-Ollama fix must stay inside Slice 2's own bounded scope — a shared
provider file makes it easy to over-apply a fix meant for one call site
to every call site, and doing so here would have silently changed
extraction/identity/planner behavior that Slices 4/7/9 already accepted,
without the benchmark evidence (#36) that a change like that should be
backed by.

**Reversibility:** Pure narrowing of D-039 — one new optional parameter
on `_chat`, two call sites opt in, three call sites unchanged. No schema,
migration, scoring, or navigation impact.

## D-041 — Slice 3 (issue #32): evidence-capability completion — skill↔employment grounding and explicit-only domain/sector evidence

**Date:** 2026-09-02
**Decision:** Closes the D-027-identified gap (`SkillItem`/`EmploymentItem`
were independent flat lists) with the minimum coherent extension, applying
the same "prove it or say UNKNOWN" discipline D-027 already established.

1. **New structural grounding, not new inference.** `CandidateProfileExtraction`
   gains two optional lists: `skill_experience` (`SkillExperienceItem`:
   skill name + `employment_index` into the same extraction's
   `employment_history` + its own evidence) and `domain_experience`
   (`DomainExperienceItem`: domain/sector label + optional
   `employment_index` + evidence). `employment_index` is a 0-based
   position into this extraction's own list, re-validated by a
   `model_validator` on `CandidateProfileExtraction` — never a
   free-floating id the model could point anywhere. An unlinked
   `SkillItem` is unchanged and still valid; it simply has no
   `SkillExperienceItem`, so its duration is provably UNKNOWN.
2. **No migration.** `CandidateProfileVersion.profile_content` and
   `JobCriteriaVersion.criteria` are both plain JSON columns. Both new
   list fields default to `[]`, so a pre-Slice-3 row round-trips through
   `CandidateProfileExtraction.model_validate` unchanged (regression test:
   `test_pre_slice_3_profile_content_still_validates_backward_compatibly`).
3. **Domain/sector evidence is explicit-only by construction — no
   company-name mapping was built.** Issue #32 allows either "an accepted
   deterministic/domain mapping" or "explicit extracted evidence"; this
   slice implements only the latter, deliberately, rather than building
   and maintaining a curated known-employer→sector table (higher risk,
   heavier, and not required to close the gap). `meyar.core.domain_terms`
   holds a small curated synonym table of unambiguous sector *descriptor*
   phrases (e.g. "banking sector", "anti-money laundering", "AML") —
   deliberately never a bare word that commonly appears inside an
   unrelated proper noun (no bare "bank"/"banka" entry). Extraction
   verification (`meyar.extraction.evidence.verify_extraction_evidence`)
   independently re-checks that a `domain_experience` item's own cited
   quotes contain one of these terms before the extraction can be
   persisted — same terminal, no-retry failure discipline as a fabricated
   evidence quote. Consequence: a company name alone (e.g. "ABC Bank
   Holdings LLC" with no other sector language) can never produce a
   domain claim; confirmed by
   `test_scenario_g_extraction_rejects_domain_claim_without_explicit_term`
   and `test_scenario_g_company_name_alone_never_yields_domain_evidence`.
   `canonicalize_domain`/`domain_term_present` fold Azerbaijani diacritics
   (`meyar.core.text.fold_az_ascii`, D-023's typing-variance discipline)
   so "bankçılıq" and its plain-keyboard variant "bankcilik" compare
   equal.
4. **Two new `CriterionKind` values**, additive to the existing five:
   `SKILL_EXPERIENCE` (skill + required `min_years`, e.g. "5 years Java" —
   distinct from unscoped `EXPERIENCE`) and `DOMAIN_EXPERIENCE` (domain
   presence, with optional `min_years`). Both go through `CriterionIn`'s
   existing `_validate_not_sensitive` check unchanged, so a
   `DOMAIN_EXPERIENCE`/`SKILL_EXPERIENCE` criterion can no more reference
   a protected attribute than any other kind. This does not touch or
   widen the frozen NL search fast path (D-031) — these are evaluation-
   engine criterion kinds, not new search-planner phrase patterns.
5. **Deterministic duration aggregation, never LLM-computed.**
   `evaluate_skill_experience`/`evaluate_domain_experience`
   (`meyar.evaluation.evaluators`) sum only `SkillExperienceItem`/
   `DomainExperienceItem`-linked, date-parseable periods via a new
   `merge_and_sum_years` (`meyar.evaluation.experience`) that merges
   overlapping/adjacent ranges before summing — deliberately different
   from `evaluate_experience`'s total-career EXPERIENCE evaluator, which
   still flags any overlap as `CONFLICTING_EVIDENCE` (frozen, unchanged;
   overlapping attributable periods for the *same skill* across two jobs
   are a normal, legitimate shape — e.g. a full-time role and a
   concurrent freelance project — not a data conflict). No linkage ->
   `UNKNOWN` (`..._NO_ATTRIBUTABLE_PERIODS`), never 0 years and never
   total career experience. Any linked-but-unparseable date ->
   `UNKNOWN` (`..._DATES_UNPARSEABLE`) for both evaluators — deliberately
   never `MANUAL_REVIEW_REQUIRED` (unlike `evaluate_experience`'s
   unparseable-date case) per issue #32's explicit instruction that
   unsupported per-skill/domain dates stay UNKNOWN. `evaluation_as_of_date`
   remains the pre-existing explicit application-boundary parameter
   (never a UI prompt, never the wall clock) for both new evaluators.
6. **Agent surfacing (issue #32 item 7).** `meyar.agent.service`'s
   `_EVIDENCE_CATEGORIES` (GET_CANDIDATE_EVIDENCE) and
   `_build_profile_facts` (D-038 grounded-answer synthesis) both gained
   `skill_experience`/`domain_experience` entries, so HR can retrieve and
   have explained which attributable period(s) support a duration/domain
   conclusion — the classic (non-agent) candidate-detail UI is
   unchanged/out of scope, consistent with D-030's agent-first framing.
7. **Prompt version bumped** `candidate-profile-extraction-v1` ->
   `candidate-profile-extraction-v2` (materially new instructions on when
   to populate the two new lists, including an explicit "do not infer
   domain from an employer name" rule); `SCHEMA_VERSION`
   (`candidate-profile-v1`) intentionally left unchanged since the JSON
   shape is additive/backward-compatible, not a breaking format change.
8. **Considered and declined:** a deterministic `find_prohibited_term`
   scan on the new free-text `domain`/`skill_name` fields. Declined for
   consistency, not oversight — every existing profile free-text field
   (`SkillItem.name`, `EmploymentItem.title`, `ProjectItem.description`,
   etc.) has exactly the same soft, prompt-level-only protection against
   a hostile/malformed extraction containing a protected-attribute word;
   the actual deterministic enforcement point is, and remains, at
   criterion configuration (`CriterionIn._validate_not_sensitive`, which
   `SKILL_EXPERIENCE`/`DOMAIN_EXPERIENCE` criteria already inherit
   unchanged) — where a sensitive value could actually influence
   matching/ranking, not in read-only extracted display text. Adding an
   asymmetric guard only on the two new fields would suggest a partial
   protection model without closing the equivalent pre-existing surface
   on every other free-text field.

**Why:** The agent (Slice 2/4) will be asked exactly these questions in
ordinary HR conversation; shipping without closing this gap risks either
silent fabrication (skill duration quietly computed from unrelated total
experience) or a permanently-declining UX for a legitimately answerable
question. The core rule throughout: a duration/domain claim is provable
only when stored evidence supports both the subject and an attributable
interval strongly enough for the deterministic layer to compute it —
otherwise UNKNOWN, never guessed.

**Reversibility:** Fully additive and migration-free. Removing
`skill_experience`/`domain_experience` from `CandidateProfileExtraction`,
the two new `CriterionKind` values, the two new evaluators, and the
`meyar.core.domain_terms` module restores exactly the pre-Slice-3 shape;
every pre-existing field, evaluator, and prompt instruction is untouched.
No schema/data migration exists to roll back.

**Correctness fix (same PR #41, pre-merge, 2026-09-02):** an owner final
review caught a real bug in the shape above:
`SkillExperienceItem`/`DomainExperienceItem.employment_index` pointed at
an employment entry, and the evaluators computed duration from THAT
ENTRY's own `start_date`/`end_date` — so a skill/domain claim linked to a
5-year job automatically became "5 years," even when the source text only
evidenced a 6-month sub-period within that job. This silently violated
the slice's own core rule (an attributable interval must be
evidence-backed for the specific claim, not inherited from context).

Fix: `SkillExperienceItem`/`DomainExperienceItem` each gained their OWN
`start_date`/`end_date`/`is_current` fields — the item's own attributable
interval, stated independently of the linked entry. `employment_index`
is now explicitly documented and enforced-by-construction (evaluators
never read `profile.employment_history[item.employment_index].start_date`/
`.end_date` for duration) as CONTEXT/PROVENANCE ONLY. Both evaluators now
resolve the period from the item itself via the same `_resolve_period_years`
helper (duck-typed, reused unchanged); an item with no declared interval
at all is a new explicit case (`SKILL_DURATION_NO_ATTRIBUTABLE_INTERVAL`
— hard block, since every `SkillExperienceItem` exists specifically to
ground a duration; `DOMAIN_DURATION_NO_ATTRIBUTABLE_INTERVAL` — for
domain, a presence-only claim with no interval is a normal, complete
shape on its own, so a matched item lacking an interval is skipped, not
blocking, and the whole result is UNKNOWN only if NO matched item ends up
contributing any computable interval). Extraction prompt bumped again,
`v2` -> `v3`, with explicit "state the skill/domain's OWN period, a
narrower sub-period when that's what the text supports, never
automatically the job's full span" instructions. Agent fact/evidence
presentation (`meyar.agent.service._build_profile_facts`) updated to show
the item's own interval as the "detail," never the linked entry's.
Four new regression tests added directly from the owner's request: (1)
5-year employment + a short, year-boundary-crossing attributable Java
sub-period != 5 years Java; (2) a skill linked to an employment entry
with no interval of its own -> UNKNOWN; (3) two explicit non-overlapping
attributable Java intervals summing to exactly 5 years -> MATCH; (4) the
domain evaluator follows the identical rule. All prior scenario A-H
tests were also updated to set explicit per-item intervals (several
deliberately reusing a WIDER linked employment entry than the item's own
interval, so a passing test also proves attribution, not just
duration-vs-total non-substitution).

**Extraction-version/reprocessing finding:** confirmed there is, and was
already, no automatic mechanism that reprocesses an existing
`CandidateProfileVersion` when `PROMPT_VERSION` changes — this predates
Slice 3 and is not a regression it introduced.
`meyar.services.folder_reconciliation_service._process_candidate_document`
only (re-)extracts "when not already COMPLETED for this document" by
design (its own docstring), and no API/UI route triggers extraction at
all. The only existing re-extraction path is the operator-only CLI
command `meyar extract-profile <tenant_id> <candidate_id> <document_id>`
(`meyar/cli.py`), which unconditionally calls `extract_candidate_profile`
and always inserts a new version row regardless of an existing COMPLETED
version (`create_profile_version`'s own docstring: "a re-extraction
always creates the next version_number" — verified live by the existing
`test_reextraction_creates_v2_and_leaves_v1_unchanged` regression). So
Slice 3 is not new-CVs-only in the sense that operators CAN retroactively
backfill `skill_experience`/`domain_experience` grounding onto any
existing candidate by re-running that CLI command per candidate/document
— it is new-CVs-automatic-only: nothing re-extracts existing candidates
by itself. A pre-existing candidate simply keeps returning UNKNOWN for
any `SKILL_EXPERIENCE`/`DOMAIN_EXPERIENCE` criterion (safe — never a
fabricated/inherited duration) until someone explicitly re-extracts it.
Building automatic bulk reprocessing keyed off a `PROMPT_VERSION`/
`SCHEMA_VERSION` mismatch was explicitly out of scope for this pass (no
prior prompt-version bump in this codebase's history has ever had one
either — including the v1->v2 bump earlier in this same slice) and would
be a genuinely new operational feature, not a "smallest correct fix";
recorded here as a known, deliberate limitation rather than left
undocumented. `SCHEMA_VERSION` remains `candidate-profile-v1` for the
same additive/backward-compatible reasoning as the original entry above.

**Second correctness fix (same PR #41, pre-merge, 2026-09-02) — interval
must be grounded by evidence, not merely accompanied by it:** a further
owner review found that the first correctness fix above (item's own
`start_date`/`end_date`) was necessary but not sufficient. `verify_
extraction_evidence` re-verified that a `skill_experience`/
`domain_experience` evidence quote was real, verbatim text (`verify_
evidence`) and — for domain — that an accepted sector term appeared in
it (`domain_term_present`), but nothing checked that the item's claimed
`start_date`/`end_date` were actually SUPPORTED by that quote's content.
A model could cite a real, verbatim quote that only proves the skill/
domain was mentioned (or supports a narrower/different period, or even
borrow a *different* item's real quote — the linked employment entry's
own dates line) while still claiming an arbitrary, broader interval such
as an entire linked employment span. This was a genuine gap, not already
safe — confirmed by reproducing all three of the owner's counterexamples
against the pre-fix code before changing anything.

Fix: a new deterministic function,
`meyar.core.interval_terms.interval_grounded_in_quotes`, checked at
extraction-verification time (same terminal, no-retry failure discipline
as every other evidence check) for both `skill_experience` and
`domain_experience`. For each bound the item actually claims
(`start_date`/`end_date`; a bound left `None`, e.g. an `is_current`
claim's `end_date`, trivially requires nothing — the evaluator's own
UNKNOWN/insufficient-evidence handling already covers an absent bound),
the SAME year the model claims must literally appear somewhere in that
item's own cited evidence quotes — never a different item's quotes, never
merely "a real quote exists somewhere." For `skill_experience` only, an
additional `subject` check requires the skill name itself to also appear
in the same quotes, so a quote that states the right years but never
mentions the skill (or vice versa) still fails; domain intentionally does
NOT reuse this literal-subject check — `domain_term_present`'s existing
synonym-aware matching already independently guarantees domain-relatedness
(e.g. "anti-money laundering" grounds domain "AML" without the literal
string "AML" appearing anywhere), and a second, literal-only check on top
would conflict with that, not reinforce it. New codes: `SKILL_INTERVAL_
NOT_EXPLICIT`, `DOMAIN_INTERVAL_NOT_EXPLICIT`.

Explicitly NOT an LLM-based verifier (issue #32 forbids one) — this is a
token-presence heuristic, deterministic and auditable like `domain_term_
present`, with an accepted, documented limit: it reliably rejects an
interval whose claimed year(s)/subject are simply absent from the cited
text (the shape of all three owner counterexamples), but cannot detect a
quote deliberately engineered to contain the right years and subject
without genuinely establishing them together (e.g. a quote listing every
skill and the job's full date range in one sentence). Closing that
residual gap would require either semantic (LLM) judgment — explicitly
ruled out — or requiring every date/subject pair to co-occur within a
single quote rather than across an item's whole evidence list, which was
judged too strict for genuine multi-quote grounding (see the earlier
scenario-A pattern of citing separate supporting lines) and out of scope
for this pass; recorded here rather than left unstated.

Confirmed unchanged and still correct: open/current interval grounding
remains fully deterministic — `meyar.evaluation.evaluators.
_resolve_period_years` resolves an `is_current=True` bound via the
explicit `evaluation_as_of_date` parameter (never the wall clock, never a
UI prompt), exactly as before this pass; the new extraction-time
grounding check does not touch that logic and does not spuriously reject
an open-ended claim (only the stated start year needs to be grounded,
proven by a new regression, `test_final_review_open_current_interval_
grounding_requires_only_the_start_year`).

Eight new regression tests cover both positive (genuinely grounded skill/
domain intervals accepted) and negative (all three owner counterexamples,
plus a subject-without-dates and dates-without-subject variant, plus the
domain equivalent) cases in
`backend/tests/test_evidence_capability_completion.py`.

No `PROMPT_VERSION` bump was needed for this pass — the prompt (already
at `v3`) already instructed the model to "cite the exact text that
supports both the skill-to-job link and the specific dates you set";
this pass only added deterministic enforcement of that existing
instruction, not a change to what the model is asked to do.

**Third correctness fix (same PR #41, pre-merge, 2026-09-02) — RELATIONAL
grounding: subject and interval must be tied together in one quote, not
merely each present somewhere:** a further owner review found that the
second fix above, while correct as far as it went, was itself provably
insufficient: `interval_grounded_in_quotes` joined ALL of an item's
evidence quotes into one combined string before checking subject
presence and year presence independently. Reproduced live before
changing anything: `interval_grounded_in_quotes(start_date="2020",
end_date="2025", quotes=["Java ilə işləyib.", "2020–2025 — Data
Analyst."], subject="Java")` returned `True` — citing a real quote that
only proves "Java" was used, plus a second real quote that only proves
some unrelated "2020-2025" span, together satisfied the joined check even
though the two facts were never actually stated together. A genuine gap,
confirmed before fixing, not already safe.

Fix: `interval_grounded_in_quotes` now checks PER-QUOTE, never a joined
haystack. It requires ONE single evidence quote — one "accepted evidence
span" out of the item's evidence list — that contains BOTH the subject
(any term in a new `subject_terms: frozenset[str] | None` parameter) AND
every year the item claims. Different quotes are never combined to
satisfy different parts of the claim; an item with several evidence
quotes still passes as soon as ONE of them alone is sufficient (so
supplementary, non-qualifying quotes don't break a genuinely grounded
claim — see `test_relational_3`).

`subject_terms` generalizes the prior single-string `subject` param so
domain can reuse the identical relational check: a new
`meyar.core.domain_terms.accepted_terms_for_domain(domain) ->
frozenset[str]` (the same curated synonym-set lookup `domain_term_present`
already used internally, now shared) is passed as `subject_terms` for
`domain_experience`, so "AML" and "anti-money laundering" are recognized
as the same subject inside the relational check exactly as
`domain_term_present` already recognizes them for the separate
presence-anywhere check. `meyar.core.interval_terms._normalize` was also
aligned to the identical az-ascii-fold + casefold normalization
`domain_terms._normalize` already used (previously only casefold, no
diacritic folding) — otherwise an AZ-diacritic quote could fail to match
an ASCII-typed domain synonym inside the now-per-quote comparison.

Five new regression tests, matching the owner's five required cases
exactly: (1) skill subject in one quote + unrelated dates in another ->
rejected (`SKILL_INTERVAL_NOT_EXPLICIT`, extraction never persisted — the
strongest available guarantee, stronger than a mere evaluator-level
UNKNOWN, consistent with every other evidence check in this module never
persisting on failure); (2) domain term in one quote + unrelated dates in
another -> rejected (`DOMAIN_INTERVAL_NOT_EXPLICIT`); (3) subject and
interval genuinely tied together in one accepted quote, with an
additional non-qualifying quote alongside it -> still valid; (4) two
separate, EACH-individually-relationally-grounded `SkillExperienceItem`s
both pass extraction verification and still aggregate correctly at
evaluation time (3 + 3 = 6 years, `merge_and_sum_years` untouched); (5)
an end-to-end `is_current` case: passes the relational check (only the
start year needs grounding — an unset bound requires nothing), then the
evaluator still resolves duration from the explicit
`evaluation_as_of_date` parameter alone, confirmed against two different
evaluation dates (2023 vs 2026) — the same deterministic mechanism as
before this pass, unaffected by it.

Deliberately still not an LLM verifier (explicitly ruled out again this
round). Documented residual limit, unchanged in kind from the second fix:
a single quote engineered to contain the right years and the right
subject together, without those actually being causally related in the
source CV, cannot be distinguished from a genuine claim by a token-
presence check — closing that would require semantic judgment. This is
now the smallest remaining gap after three correctness passes, and it is
inherent to any purely deterministic, non-LLM text-matching approach, not
a shortcut taken in this fix.

## D-042 — Slice 4 (issue #33): agent-first product UX + JD → criteria
drafting, and two real-Ollama findings

**Date:** 2026-09-02
**Decision:** Implements M8 Slice 4 (issue #33, per D-030/D-031/D-032) on
`feat/agent-product-ux-jd-matching` from synced `main` (`8c1782f`).

1. **MEYAR AI is now the primary post-login HR surface.** Top-level nav is
   `MEYAR AI | Namizədlər | Çıxış`; classic NL search (`/ui`) and
   Vacancies (`/ui/jobs`) remain fully reachable via a de-emphasized
   secondary nav line ("Digər alətlər") rather than being removed, per
   D-032 point 2. Human login (`_finalize_human_login`) now redirects to
   `/ui/agent` instead of `/ui` — the only behavior change to the login
   flow itself; six existing redirect-target test assertions updated
   accordingly.
2. **Conversation UX consolidation.** A new `AgentTurnView.headline`
   (`meyar.ui.service._agent_turn_headline`), computed once server-side,
   replaces the previous two-tier "generic outcome banner" +
   "tool-specific outcome banner" stack — one meaningful assistant message
   first, supporting cards/evidence second, no duplicated success/status
   text. A non-executable search outcome's own explanation still IS that
   headline (not redundant — it is the only informative content in that
   case). New `POST /ui/agent/reset` ("Yeni söhbət") clears this browser
   session's own `AgentConversation` (turns + `last_search_candidate_ids`)
   via a new `meyar.services.agent_conversation_repo.reset_conversation`
   — tenant/session-scoped exactly like every other conversation call
   site, never touches another session/tenant.
3. **Candidate presentation:** the semantic-similarity pill (previously
   unconditional "Uyğunluq %" on `/ui/search`, and already mode-gated but
   identically labeled on `/ui/agent`) is relabeled "Semantik yaxınlıq %"
   on both surfaces and now consistently hidden outside SEMANTIC_ONLY/
   HYBRID search modes — a plain structured/discovery result never shows
   a percentage that could read as a compatibility score. The real 0–100
   deterministic score stays exactly where it already correctly lived
   (`ranking_results.html`, `rank_candidates_for_job` only) — unchanged.
4. **JD → structured criteria draft → human review → deterministic rank.**
   New `AgentActionType.DRAFT_JOB_CRITERIA`: the model signals only that
   the message is a JD (no argument on `AgentDecision` itself — the
   server uses the user's own already-known message text as the JD input
   for a second, narrower LLM call, `LLMProvider.draft_job_criteria`,
   deliberately never asking the model to reproduce the JD inside its own
   output schema — the D-035 Ollama `maxLength`-in-output-schema failure
   mode this avoids). The model drafts a `JDCriteriaDraft` (title +
   bounded must-have/preferred lists, restricted to the same five
   `CriterionKind`s the manual form already offers); every item is
   re-validated into a real `CriterionIn` server-side (same schema +
   prohibited-attribute denylist as the manual form/REST API — D-031
   point 4, no exception), and any item that fails is silently dropped
   (never shown, `dropped_count` surfaced) rather than weakened. The
   result renders as an editable review form (shared Jinja macro,
   `_criteria_rows.html`, extracted out of `job_new.html` so both surfaces
   render criteria rows identically) that posts through the EXISTING,
   unchanged `POST /ui/jobs` — nothing is persisted by drafting alone.
   Ranking after creation reuses the existing `/ui/jobs` "Namizədləri
   sırala" action unchanged: no manual evaluation-date input, today's date
   injected at the UI boundary exactly as `/ui/search` already does, and
   the effective evaluation date is already displayed on
   `ranking_results.html`. No new vacancy-CRUD surface area; `Job`/
   `JobCriteriaVersion`/scoring untouched. `AgentTurnOutcome.
   JOB_DRAFT_FAILED` (bounded retry, then this outcome with empty
   `tool_results`) covers the drafting call itself never producing a
   usable result — mirrors the existing AGENT_PROVIDER_FAILURE/
   MALFORMED_MODEL_OUTPUT precedent (D-036).
5. **D-031 sunset condition NOT acted on this slice.** Issue #33/D-031
   point 3 makes the fast-path sunset an owner-evaluated decision, not
   automatic. This slice implements the agent-first JD/criteria flow the
   sunset evaluation depends on but does not itself judge "accepted
   functional parity" — that determination is left to the owner's UI
   review (see the accompanying `## HUMAN ACTION REQUIRED`); D-026's fast
   path is untouched.
6. **Two real findings from live-Ollama acceptance testing** (qwen3:1.7b,
   the same integration-verified model as D-039/D-040), matching the
   established Slice 2 pattern (D-035 through D-040) of bugs no
   `FakeLLMProvider`-based test can surface:
   - **Prompt-leak defect (real, fixed structurally).** The model can
     copy one of `AGENT_SYSTEM_PROMPT`'s own English instructional
     sentences verbatim into `AgentDecision.message` for CLARIFY/
     FINAL_ANSWER, instead of authoring real content — a raw
     planner-internals leak into HR-facing text that D-035's "the
     model's message is safe to show verbatim" boundary did not
     anticipate. Fixed with `meyar.agent.service._looks_like_prompt_leak`
     — an exact/near-exact containment check against the fixed prompt
     text (not a fuzzy heuristic) — treated exactly like schema-invalid
     output: bounded retry (`MAX_DECISION_ATTEMPTS`), then the existing
     deterministic `MALFORMED_MODEL_OUTPUT` fallback. Reproduced live
     post-fix: the guard correctly rejected a repeat leak and rendered
     only the safe fallback text, never the leaked sentence. Two new
     regression tests (`test_prompt_leaking_clarify_message_is_rejected_
     and_retried`, `test_prompt_leak_persisting_through_every_retry_
     falls_back_safely`).
   - **Known, documented model-quality limitation (not fixed further this
     slice).** `AGENT_PROMPT_VERSION` was bumped to
     `agent-orchestrator-prompt-v3` (DRAFT_JOB_CRITERIA added, then its
     disambiguation against SEARCH_CANDIDATES sharpened once) after live
     testing showed qwen3:1.7b sometimes still routes a raw pasted JD to
     SEARCH_CANDIDATES instead of DRAFT_JOB_CRITERIA when the message
     carries no explicit trigger verb — the same class of small-local-
     model intent-classification limitation the project already documents
     honestly elsewhere (D-025's "AI tələbi tam anlaya bilmədi" framing).
     Both branches remain fully safe regardless of which the model picks:
     a misrouted JD either fails its own search-planner call (typed
     `PLANNER_PROVIDER_FAILURE`/`MODEL_TIMEOUT`, never a fabricated
     result) or, when routed correctly, drafts and validates exactly as
     designed — confirmed live for both outcomes. HR can reliably reach
     DRAFT_JOB_CRITERIA today with an explicit lead-in (e.g. "Bu elan
     üçün kriteriyalar hazırla: ..."); further routing-accuracy tuning
     (a bigger model, or — if ever justified by a DECISIONS.md entry — a
     bounded deterministic pre-classifier) is deferred, not silently
     attempted, per this slice's bounded-cycle scope.

**Why:** Issue #33 requires MEYAR AI to become the primary HR surface with
a bounded JD → draft → confirm → deterministic-rank flow, while explicitly
prohibiting new vacancy-CRUD growth, any LLM scoring authority, and
manual evaluation-date input — this decision records the concrete design
(schema shape, validation boundary, reuse of existing job/ranking
services) and the two real defects/limitations only live-model testing
could surface, consistent with the Slice 2 precedent of documenting such
findings rather than only the intended design.

**Reversibility:** Fully additive at the schema/service layer (`agent/
schemas.py`, new tool result/outcome variants) — no migration, no change
to `Job`/`JobCriteriaVersion`/scoring/evaluation. `meyar.core.text.
slugify_criterion_label` is a pure extraction of previously-private
`meyar.ui.service._slugify_criterion_label` logic (same behavior, now
shared with `meyar.agent.service`). The login-redirect and nav changes
are template/route-level and trivially reversible. `_looks_like_prompt_
leak` is a narrow, additive guard around one existing decision-fetch
loop.

## D-043 — PR #42 owner correction (issue #33): deterministic JD intent,
no-silent-drop disclosure, and Vacancies discovery removed from normal HR UI

**Date:** 2026-09-02
**Decision:** Owner UI review of PR #42 (D-042) found two product blockers
and one navigation/exposure correction before merge; all three are
resolved on the same `feat/agent-product-ux-jd-matching` branch, no new
branch:

1. **Deterministic JD entry — no prompt magic.** D-042 point 6 documented
   that qwen3:1.7b sometimes fails to route an implicit (no explicit
   lead-in) pasted JD to `DRAFT_JOB_CRITERIA`. Rather than expanding the
   frozen regex/NL planner, `/ui/agent` (`meyar.ui.router.agent_turn`)
   gained one new optional `Form` field, `intent`; the "JD-dən meyar
   hazırla" button submits the fixed literal `intent=draft_job_criteria`.
   `run_agent_turn` (`meyar.agent.service`) gained a matching
   `explicit_action: AgentActionType | None` parameter: when set, the
   very first decision of the turn is constructed directly
   (`AgentDecision(action=explicit_action)`) and `llm.decide_agent_action`
   is never called for that turn — no model call, no routing ambiguity,
   no dependence on the small model inferring intent from arbitrary text.
   Only `DRAFT_JOB_CRITERIA` is accepted; any other value raises
   `ValueError` defensively (the caller is `meyar.ui.router`, never a
   client-supplied action). Normal conversational routing (no `intent`
   field) is completely unchanged — this is a second, parallel entry
   point, not a modification of the existing planner/routing prompt.
2. **No silent drop of JD requirements.** `AgentJobDraftToolResult.
   dropped_count` (a single opaque integer) is replaced by two typed
   signals in `meyar.agent.schemas`: `unsupported: list[
   UnsupportedJDCriterionItem]` (a non-sensitive requirement `CriterionIn`
   could not represent — e.g. an `EXPERIENCE` item the JD gave no
   derivable duration for — carries the requirement's own, already-
   confirmed-non-sensitive text) and `prohibited_count: int` (a
   sensitive/denylist match — count only, the matched text is never
   redisplayed, unchanged from the existing denylist discipline).
   `meyar.agent.service._build_criterion_from_draft_item` distinguishes
   the two by inspecting the `pydantic.ValidationError` `CriterionIn(...)`
   raises: `ProhibitedCriterionError` (itself a `ValueError` subclass
   raised inside a `model_validator`) is always re-wrapped by pydantic
   before it reaches the caller — verified empirically against pydantic
   2.11 — so the original exception is recovered from each error's own
   `ctx["error"]`, not caught directly. The review form
   (`meyar.ui.templates.agent.html`) renders `unsupported` rows as a
   visible, non-submittable notice under each section ("bu tələb
   avtomatik qiymətləndirməyə daxil edilmədi — sistem hazırda
   dəstəkləmir") and a separate, generic `prohibited_count` notice —
   neither ever becomes a `must_*`/`pref_*` form field, so neither can be
   persisted by submitting the form. `_agent_turn_headline`
   (`meyar.ui.service`) surfaces the same two safe counts in the one-line
   summary.
3. **Vacancies is no longer a normal HR navigation/secondary-tool
   destination.** Removed from `base.html`'s secondary nav line and from
   `home.html`'s quick-links feature card — the normal HR product surface
   is exactly `MEYAR AI | Namizədlər | Çıxış` plus classic search
   (unchanged, still secondary). `Job`/`JobCriteriaVersion`, `/ui/jobs`,
   `/ui/jobs/new`, and the manual creation/archive/rank routes are
   NOT deleted and NOT reduced in capability — they remain reachable by
   direct URL as backend/supporting capability, per D-032's original
   "backend stays, UI prominence changes" framing, now carried one step
   further. Confirming the agent's JD-drafted review
   (`POST /ui/jobs` with the review form's own hidden `from_agent_draft=1`
   field, set only by `agent.html`, never by the unchanged manual
   `job_new.html` form) still creates the `Job`/`JobCriteriaVersion`
   through the exact same `create_job`/`create_criteria_version` calls,
   but then renders straight into that criteria version's ranking result
   (the new shared `meyar.ui.router._render_job_ranking`, factored out of
   the existing manual "Namizədləri sırala" `rank_job` handler — same
   `rank_candidates_for_job` call, no new scoring authority) instead of
   redirecting to the de-emphasized `/ui/jobs` list. The manual
   `/ui/jobs/new` → `POST /ui/jobs` path is completely unchanged (no
   `from_agent_draft` field, so it still redirects to `/ui/jobs`).
   `create_job_route`'s declared required scopes grew to include
   `jobs:read`/`candidates:read`/`evaluations:write` alongside the
   existing `jobs:write` so it may call the ranking service inline; every
   HR role already holds all of these together
   (`meyar.core.roles._FULL_HR_PERMISSIONS` is deliberately flat with no
   partial-permission tier yet), so this is a declared-intent widening,
   not a functional access change.

**Why:** The owner's PR #42 review explicitly blocked merge on exactly
these two product defects (unreliable JD routing requiring "prompt
magic"; silent loss of JD requirements the deterministic schema could not
represent) plus a UX-consistency instruction (Vacancies must not read as
a normal HR destination once MEYAR AI is the primary surface) — this
entry records the concrete fix for all three so the next owner pass has
one coherent decision record rather than three untracked edits.

**Reversibility:** Fully additive/route-level. `explicit_action` is an
optional parameter with a `None` default — every existing caller
(`_run` in tests, any future caller) is unaffected unless it opts in.
`AgentJobDraftToolResult.dropped_count` is removed (not deprecated) since
PR #42 was never merged — no external consumer exists yet. The nav/home
template edits are two-line removals, trivially reversible. The
`from_agent_draft`-gated ranking redirect only changes behavior for
requests carrying that exact hidden field; the manual creation flow's
tests (`tests/test_ui_job_creation.py`) pass unchanged.

## D-044 — PR #42 owner UX re-review (issue #33): chat hierarchy, unified
composer, HR-facing copy, evidence dedup, deterministic headlines, nav trim

**Date:** 2026-09-03
**Decision:** A second owner UI pass on PR #42 found the product still
"feels like a developer form" despite D-043's functional corrections.
Presentation-only fixes, same branch, no agent/scoring/security
architecture change:

1. **Chat hierarchy.** `meyar.ui.router.agent_workspace`/`agent_turn` now
   pass `history_turns` (all PRIOR turns, plain text bubbles) and
   `latest_user_message` (the just-submitted text) separately from
   `latest` (the rich `AgentTurnView`). `agent.html` renders one
   unified `<ol>`: history bubbles, then the just-submitted user message,
   then the assistant's headline AND its cards in the SAME `<li>` —
   the composer renders only after all of that, never sandwiched between
   a turn's own text and its results. A turn's own `(user, assistant)`
   pair is spliced out of `history_turns` (`all_turns[:-2]`) exactly when
   `run_agent_turn` actually persisted one (the `try` succeeded); on the
   provider-failure path nothing was persisted, so `history_turns` stays
   the untouched full list and `latest_user_message` is the raw submitted
   `message` (previously lost entirely on that path — now shown, same
   hierarchy, still safe/generic assistant text). Net effect: the
   previous architecture's live turn was ALWAYS duplicated (once as a
   plain history bubble, once again in a separate outcome banner) —
   `test_user_message_and_model_message_are_html_escaped_in_render` is
   updated from asserting the payload appears 3× to 2× to reflect this
   deduplication.
2. **One AI composer.** The two-submit-button form is replaced by one
   `<select name="intent">` (values `""` / `draft_job_criteria`, labels
   "Adi söhbət" / "Vakansiya elanını analiz et") plus one `Göndər`
   button — `meyar.ui.router.agent_turn`'s existing `intent` handling is
   unchanged (still the only source of `explicit_action`, still never
   inferred from routing). The textarea's `required` attribute is
   removed; a new `meyar.ui.static.agent-composer.js` (same progressive-
   enhancement pattern as the existing `job-form.js`) disables the send
   button while the message is empty/whitespace-only, so an empty
   submission never triggers the browser's own native-language "Please
   fill out this field" popup — the server's existing generic
   `Form(min_length=1)` → "Forma məlumatlarını yoxlayın." error page
   remains the authoritative fallback if JS is unavailable.
3. **HR-facing copy.** `meyar.ui.service._format_filter_match_label`
   replaces `f"{item.category}: {item.value}"` (literally
   `"skill: Python"`) for `required_matches`/`preferred_matches` — every
   category except `min_total_experience_years` now renders as just the
   already-self-descriptive value; the experience category gets a
   `"{value} il təcrübə"` unit suffix. `GET_CANDIDATE_EVIDENCE` match
   headings change from `"{category_label}: {title}"` to `"Uyğun gələn
   tələb: {title} ({category_label})"` (category demoted to parenthetical
   meta, mirroring `ranking_results.html`'s existing
   `label <span class="meta">(kind)</span>` convention).
4. **Evidence dedup + citation text.** `meyar.ui.service._evidence_views`
   now deduplicates by `(page, quote)` before truncating to `maximum` — a
   candidate whose CV evidence is cited by several extracted facts no
   longer shows the identical quote repeated. A new shared macro
   (`meyar.ui.templates._evidence_list.evidence_items`) renders every
   evidence list as `"CV, səhifə {page} — "{quote}""` — never `"Səhifə
   {page}, blok {block_index}"` — reused by `agent.html`,
   `search_results.html`, and `candidate_detail.html` so all three
   surfaces read identically; `block_index` stays on
   `EvidenceLocationView` for internal provenance, it is simply never
   the thing HR reads.
5. **Deterministic headline copy.** `meyar.ui.service._agent_turn_headline`
   gains three improvements, all still server-authored from already-
   computed, non-model data (no new LLM call): SEARCH_CANDIDATES now
   reads `"{top matched requirement} tələbinə uyğun {count} namizəd
   tapdım."` (built from the top result's own `required_matches`/
   `preferred_matches`, item 3's HR phrasing) with a distinct zero-result
   sentence ("Bu tələbə uyğun namizəd tapılmadı.") instead of "0 namizəd
   tapıldı."; GET_CANDIDATE_PROFILE reads `"{full_name} üçün profil
   məlumatları aşağıdadır."`; GET_CANDIDATE_EVIDENCE reads `"{full_name}
   üzrə {topic} sübutlar aşağıdadır."` when matches exist, or an explicit
   `"{full_name} üzrə bu mövzuda profildə açıq sübut yoxdur."` when they
   don't — replacing the previous generic "Nəticələr aşağıdadır." filler
   for these two tool types entirely (they had no dedicated branch
   before, only the outcome fallback).
6. **Navigation trimmed further.** `base.html`'s secondary nav
   (`Klassik axtarış`, the only entry left after D-043 removed
   Vakansiyalar) is removed outright — normal HR navigation/discovery is
   now exactly `MEYAR AI | Namizədlər | Çıxış`. The brand/logo link and
   `error.html`'s "Əsas səhifə" link now point at `/ui/agent` (was `/ui`)
   so no page's own chrome quietly re-offers classic search as "home."
   `/ui` itself is untouched and still fully reachable by direct URL
   (D-032/D-043's "backend/supporting capability stays" framing, applied
   one step further) — `search_results.html`/`search_clarification.html`'s
   own in-page "new query" links, which loop within that already-de-
   emphasized surface rather than re-introducing discovery, are
   unchanged.
7. **JD confirmation copy.** The review form's submit button on
   `agent.html` reads "Tələbləri təsdiqlə və namizədləri sırala" instead
   of "Vakansiyanı yarat" — the vacancy-CRUD framing is gone from the
   one HR-facing verb in the agent flow, while `POST /ui/jobs` and the
   `Job`/`JobCriteriaVersion` persistence it drives (D-043) are
   byte-for-byte unchanged; the manual `job_new.html` form keeps its own
   "Vakansiya yarat" copy, since that page is explicitly the
   backend/supporting surface, not the primary HR product concept.

Verified against the real local `qwen3:1.7b` (not only `FakeLLMProvider`
fixtures): a live browser session (Chromium via the Claude-in-Chrome
extension) logged in as the seeded demo HR user, submitted a plain-
language search and a JD-analysis-mode message, and visually confirmed
items 1/3/4/5 together in one screenshot — the just-submitted user
message, the deterministic "Python tələbinə uyğun 1 namizəd tapdım."
headline, the "Məcburi uyğunluqlar: Python" line (no "skill:" prefix),
and three deduplicated "CV, səhifə 1 — "..."" evidence citations, all in
one turn block immediately above the composer. A second live JD-analysis
call surfaced a genuine `qwen3:1.7b`-drafted EXPERIENCE item with no
derivable duration, independently confirming the D-043 unsupported-item
disclosure path end-to-end with real (not fixture-forced) model output,
rendered under the new "Tələbləri təsdiqlə və namizədləri sırala" button.

**Why:** The owner's second UI pass explicitly accepted the D-043
architecture/backend behavior and scoped this pass to presentation only:
chat hierarchy, composer unification, HR language, evidence readability,
headline quality, and navigation — with an explicit "do not add new
functionality, do not change scoring/planner/evidence authority/auth/
tenant rules/persistence" boundary, which every change above respects
(no new tool, no new persisted field, no new route beyond the existing
`intent` value already wired in D-043).

**Reversibility:** Template/presentation-layer and one new pure
formatting/dedup function each in `meyar.ui.service` — no schema, no
migration, no change to `AgentTurnResult`/`AgentToolResult`/persistence.
`agent-composer.js` is additive and inert if the browser blocks
JavaScript (the button just stays enabled, falling back to existing
server-side validation). The nav/brand-link changes are single-line
template edits.

## D-045 — PR #42 owner UX correction pass 3 (issue #33): turn-render
consistency, grounded-copy dedup, requirement-attributable evidence,
composer/criteria/ranking presentation, unsupported-requirement contract

**Date:** 2026-09-05
**Decision:** A third owner UI pass on PR #42, scoped to nine verified
issues, no agent/scoring/security architecture change:

1. **Turn-render consistency (root cause + fix).** The live turn's
   headline (`meyar.ui.service._agent_turn_headline`, D-044 item 5) and
   the value `meyar.agent.service._finish_turn` persisted for that same
   turn (`result.message`, the raw model framing — empty for a plain
   tool-result turn) were two independently computed values. A later
   history re-render (`agent_turn_outcome_message`) then fell back to
   the generic per-outcome table instead of reproducing the richer live
   headline. Fixed by making the persisted text and the rendered
   headline the same value: `meyar.services.agent_conversation_repo.
   sync_last_turn_display_text` overwrites the just-persisted assistant
   turn's `text` with `latest.headline` right after
   `build_agent_turn_view` computes it, in `meyar.ui.router.agent_turn`.
   D-036's original blank-bubble guard is untouched (the overwrite is a
   no-op when there is no headline).
2. **Grounded-copy dedup + duration precision.** A new shared
   `meyar.core.text.combine_degree_and_field` joins an education item's
   degree/field_of_study without repeating a field_of_study already
   contained in degree (fixes "BSc Data Science Data Science" wherever
   an education title is built: `meyar.ui.service._facts` — candidate
   detail and the agent's GET_CANDIDATE_PROFILE view — and
   `meyar.agent.service._PROFILE_FACT_CATEGORIES`/`_EVIDENCE_CATEGORIES`
   — grounded-answer facts and evidence-topic matching). The
   `GroundedCaveat.DURATION_NOT_PROVEN` sentence changes from "Mövcud
   sübut konkret müddəti göstərmir." to "Mövcud sübut bu mövzu üzrə
   konkret təcrübə müddətini əsaslandırmır." — explicitly scoped to the
   asked-about topic/skill, so it never reads as "no dated evidence
   exists at all" when a dated employment fact is cited in the same
   message.
3. **Requirement-attributable search evidence.** `meyar.ui.service.
   build_search_result_views` previously flattened evidence from every
   profile category (skills, employment, education, certifications,
   languages, projects) regardless of which requirement matched — a
   Python-only skill match could show unrelated education/employment
   snippets as if they proved Python. A new
   `_requirement_attributable_evidence` restricts evidence to the
   profile entries that actually caused each `RequiredFilterMatch`/
   `PreferredFilterMatch` (skill/certification/language/education matched
   by the same case/diacritic-fold comparison `meyar.search.structured`
   itself uses; `min_total_experience_years` — a genuine aggregate — is
   attributed to every employment_history entry, never a different
   category). Applies identically to classic `/ui/search` and the
   agent's SEARCH_CANDIDATES tool result (both call the same function).
4. **Composer productization.** `agent.html`'s composer is restyled from
   a large full-width textarea + full-width native `<select>` + detached
   button into one visually merged, rounded, chat-style input
   (`.composer`/`.composer-toolbar` in `styles.css`): a compact pill
   mode-select and a circular send button share one bordered container
   with the textarea. The JD mode stays the same explicit
   `<select name="intent">` (values `""`/`draft_job_criteria`) — no
   prompt-detection/heuristic routing was added; only presentation
   changed.
5. **JD criteria review noise reduction.** `_criteria_rows.html`'s
   duration column shows a muted "—" instead of a "Tətbiq olunmur"
   disabled-input placeholder repeated on every non-EXPERIENCE row (and
   "il" instead of "Minimum müddət (il)" when applicable — same for
   `job-form.js`'s client-side toggle); the requirement-text column is
   now the dominant column (`table-layout: fixed`, 46% width); weight
   (`Əhəmiyyət`) is a narrow, small-type, muted column — still fully
   editable, no longer visually competing with Növ/Tələb. No field name,
   validation rule, or submitted value changed.
6. **Unsupported-requirement contract.** Inspected the actual boundary:
   `meyar.agent.schemas.JDDraftCriterionItem.kind` accepted the full
   `CriterionKind` enum, so a genuinely out-of-scope, non-sensitive
   requirement (e.g. relocation willingness, a driving license) had no
   structural way to be flagged — the model would either omit it or
   force it into a supported kind. A new `JDDraftCriterionKind` (SKILL/
   EXPERIENCE/CERTIFICATION/EDUCATION/LANGUAGE/OTHER) — deliberately
   NOT `CriterionKind` itself, which stays the deterministic evaluator's
   own persisted scoring vocabulary — is the model's actual output type;
   `OTHER` routes straight to `DroppedJDCriterionReason.UNSUPPORTED` in
   `_build_criterion_from_draft_item`, deterministically, never via an
   incidental `CriterionIn` validation failure. The prompt now instructs
   the model to use `OTHER` for such requirements rather than omitting
   or misclassifying them. "Survives confirmation": `agent.html`'s
   unsupported-requirement disclosure now also renders one hidden
   `unsupported_must_have`/`unsupported_preferred` input per item;
   `create_job_route` reads them and threads them into
   `_render_job_ranking` as `unsupported_requirements`, which
   `ranking_results.html` renders as a page-level "Məlumat üçün —
   qiymətləndirməyə daxil edilmir" (informational, not scored) notice —
   still never validated as a criterion, never persisted to
   `JobCriteriaVersion`, never touching `ranking`/`results`, so it
   contributes nothing to score/ranking by construction while no longer
   vanishing once the drafting turn scrolls past. German
   language/Power BI remain ordinary supported LANGUAGE/SKILL criteria
   with unchanged UNKNOWN-when-missing-evidence behavior — untouched by
   this item.
7. **Vacancy-admin exposure removed from ranking.** `ranking_results.
   html`'s `<a class="back-link" href="/ui/jobs">← Vakansiyalar</a>` is
   removed outright. Primary nav (`base.html`, unchanged since D-043/
   D-044) remains exactly `MEYAR AI | Namizədlər | Çıxış`. `Job`/
   `JobCriteriaVersion` backend and every existing route (`/ui/jobs`,
   `/ui/jobs/{id}/rank`, `POST /ui/jobs`) are untouched and still fully
   reachable by direct URL/link from elsewhere.
8. **Ranking reading-hierarchy reorder.** Each candidate card's primary,
   always-visible content is now: name → overall score pill + fit band
   (+ MANUAL_REVIEW/INSUFFICIENT_EVIDENCE alert where applicable) → a
   new `.criterion-status-list` (one row per criterion: HR label + kind,
   status badge, and — newly rendered, previously absent from this page
   entirely — that criterion's own evidence via the existing
   `_evidence_list` macro, or an explicit "no evidence found" sentence
   for UNKNOWN). Raw weight/uyğunluq-dərəcəsi/bal-töhfəsi numbers move
   into a renamed, still-collapsed `<details>` ("Hesablama detalları")
   below that — secondary, not hidden. `ScoreContributionView.evidence`
   already existed (deterministic per-criterion evidence from the
   evaluation engine) but was never rendered on this page before.
   Evaluation date and the UNKNOWN/definitive-mismatch distinction are
   unchanged.
9. **Candidate detail.** No template change beyond item 2's shared
   `_facts()` dedup fix, which already applies here (candidate detail
   uses the same function). "CV-yə bax" (in-app preview) vs "Originalı
   yüklə" (true download) stay separately implemented routes; no
   UUID/parser/index/version internal identifier is rendered as visible
   page text (only inside `href` attributes, e.g. the candidate/document
   ids already required for the links to work).

**Why:** Continuing the owner's iterative "fast-track MVP, presentation
first, no new authority" correction pattern (D-043/D-044) — every fix
above is either a genuine bug (item 1: two divergent text sources for
one concept; item 2: a real string-concatenation duplication bug; item
3: evidence not actually attributable to what it was shown under) or a
presentation-only change (items 4/5/7/8/9), except item 6, which is a
deliberately narrow, additive dispatch-layer type (JDDraftCriterionKind)
that never touches the deterministic evaluator's own `CriterionKind`,
scoring policy, or `JobCriteriaVersion` schema.

**Reversibility:** No schema/migration change. `JDDraftCriterionKind` is
a new enum scoped to `meyar.agent.schemas`/`meyar.agent.service` only —
`CriterionKind` (evaluator/DB-facing) is unchanged. `sync_last_turn_
display_text` only ever overwrites the `text` field of the just-written
conversation-JSON turn already being committed in the same request: no
new column, no new table. `unsupported_requirements` is a request-scoped
list threaded through one render call, never persisted. Template/CSS/JS
changes are presentation-only.

## D-046 — PR #42 acceptance blockers: JD requirement grounding, and the
850/846/848 test-count history

**Date:** 2026-09-05
**Decision:** Closes the two remaining owner-flagged acceptance blockers
on PR #42, no new branch, no architecture expansion.

1. **Root cause of the reported "Passing an exam" hallucination.**
   `LLMProvider.draft_job_criteria`'s prompt (`JD_CRITERIA_DRAFT_SYSTEM_
   PROMPT`) already instructed "only include a requirement that is
   actually stated in the text — never invent one," but nothing
   downstream ever verified that instruction was followed —
   `meyar.agent.service._build_criterion_from_draft_item` re-validated a
   drafted item's *shape* (schema/prohibited-attribute denylist) but
   never its *provenance*. Reproduced live against the real, integration-
   verified `qwen3:1.7b` (not merely asserted): a short/underspecified
   JD reliably gets a plausible-sounding but wholly unstated requirement
   "filled in" from the model's own prior/training knowledge about what a
   role "typically" requires — e.g. a JD stating only "Namizəd
   ezamiyyətə getməyə hazır olmalıdır" (travel readiness) for a
   "Regional Satış Nümayəndəsi" role, with no mention of language or
   sales experience, reproducibly returned a fabricated "İngilis dili"
   LANGUAGE requirement and a fabricated "Satış nümayəndəsi təcrübəsi"
   EXPERIENCE requirement across repeated `temperature=0` calls — the
   exact same fabrication class the owner observed as "Passing an exam."
   Critically, this reproduced through **both** disclosure paths: a
   fabricated item can land as a real, scored `CriterionIn` (the
   "İngilis dili" case) or — the reported case — as a `JDDraftCriterionKind.
   OTHER` item, which D-045 routes unconditionally to the HR-visible
   `unsupported` disclosure ("a real JD requirement the system merely
   cannot score") with **zero grounding check**. D-045 solved "a real,
   out-of-scope requirement must not vanish"; it did not address "a
   requirement must first be confirmed real." The literal historical
   session that produced "Passing an exam" was not preserved (no chat
   log, no committed fixture), so the exact string could not be
   regenerated byte-for-byte — the reproduction above establishes the
   same failure *class* deterministically and repeatably against the
   real model, which is what the fix targets.
2. **Grounding fix.** `meyar.agent.service._is_requirement_grounded_in_
   jd_text` — a deterministic, local, lexical check (fold_az_ascii +
   normalize_azerbaijani_case, matching the project's existing
   Azerbaijani-suffix-tolerant comparison convention) requiring at least
   half of a drafted requirement's own non-connector words to appear as a
   substring of the actual JD text HR submitted. `_build_criterion_from_
   draft_item` now runs this check on **both** remaining disclosure
   outcomes (a would-be-valid `CriterionIn`, and an `OTHER`/schema-
   failure item otherwise bound for `unsupported`) — never on the
   `PROHIBITED` path, which is an unconditional early return, unreordered
   and unweakened by this change (a doubly-bad item — fabricated *and*
   sensitive — is still counted `PROHIBITED`, exactly as before). A new
   `DroppedJDCriterionReason.UNGROUNDED` and `AgentJobDraftToolResult.
   ungrounded_count` mirror the existing `PROHIBITED`/`prohibited_count`
   discipline exactly: count-only, the unconfirmed text is never
   redisplayed (redisplaying it would itself misattribute invented
   content to HR's own JD — the very defect being fixed), surfaced via
   the existing headline/template notice pattern
   (`_agent_turn_headline`, `agent.html`). No new architecture: no new
   LLM call, no embedding call, no new persisted field —
   `JobCriteriaVersion`/scoring/ranking untouched. This is a real
   correctness bug fix (an HR-facing disclosure with no provenance
   check), not a UX redesign.
3. **Acceptance invariants held:** supported requirements' semantics are
   unchanged (grounded items pass through exactly as before — the
   existing `test_draft_job_criteria_builds_valid_criteria_from_llm_
   draft`/`AWS`/`Python` fixtures are untouched); genuinely unsupported,
   non-sensitive, JD-stated requirements remain visible (`test_draft_job_
   criteria_discloses_unsupported_non_sensitive_item`, `test_draft_job_
   criteria_other_kind_is_unsupported_never_scored` — both updated only
   to give their fixture JD text an honest lexical trace of the item
   being asserted, never to weaken an assertion); unsupported/ungrounded
   requirements contribute nothing to deterministic scoring (unchanged —
   neither ever becomes a `CriterionIn`); prohibited/sensitive
   requirements remain safely blocked (`test_draft_job_criteria_drops_
   prohibited_attribute_item`, unchanged priority); no LLM-invented
   requirement can be presented as a JD requirement (new: `test_draft_
   job_criteria_drops_fabricated_unrelated_requirement`, `test_draft_job_
   criteria_fabricated_requirement_never_rendered`, plus a direct
   `test_is_requirement_grounded_in_jd_text_unit` contract test); HR
   review/edit before persistence is untouched (`POST /ui/jobs` path
   unmodified); no external AI API introduced (pure string comparison,
   no network call).
4. **Known residual limitation, stated honestly (matching the D-042
   point 6 precedent):** the lexical check compares word-level content,
   so a fabrication that reuses real JD words in a new, unstated claim
   (e.g. inferring "Satış nümayəndəsi təcrübəsi" from a JD whose only
   real content is the job title "Regional Satış Nümayəndəsi") is not
   caught by this check alone — this is a narrower residual risk than
   the reported defect (content entirely disconnected from the JD, in
   the reproduced case even a different language), and HR review before
   `POST /ui/jobs` persistence remains the final backstop for it, exactly
   as it already is for every other drafted field.
5. **The 850/846/848 test-count history.** Every commit in PR #42's
   actual lineage (`8c1782f` synced-`main` base → `b55bea6` → `a38fc16`
   (D-043) → `c87178b` (D-044) → `cac333f` (D-045), verified via
   `git log`/`git reflog` — no rebase, no amend, no dropped commit
   anywhere in this history) was checked out into a disposable worktree
   and `pytest --collect-only -q`'d independently: **822 → 838 → 846 →
   846 → 848** collected tests. A full pairwise node-id diff (not just
   counts) across all four Slice-4 commits shows **zero test removals at
   any single transition** — b55bea6→a38fc16 added 8 with 0 removed
   (matches D-043's own scope), a38fc16→c87178b added/removed 0
   (D-044 was presentation-only, exactly as its own decision record
   states), c87178b→cac333f added the 2 D-045 tests with 0 removed. This
   independently confirms STATUS.md's own existing D-045 note ("848
   passed, +2 new tests vs D-044's 846, none deleted/weakened"). **No
   commit, reflog entry, stash entry, or GitHub PR/issue comment/review
   anywhere in this repository's history produced a count of 850** —
   `origin/main` is still exactly the `8c1782f` base this branch started
   from, so there is no other merged work that could account for it
   either. The number cannot be corroborated from any artifact this
   investigation could check; it is not treated as evidence of a real
   regression, since the only verifiable chain is monotonic with zero
   deletions at every step. Current HEAD (after this decision's own 3
   new grounding tests) collects **851**.
6. **Quality gates:** `ruff check .` clean, `uv run mypy src` clean (136
   files), `uv run pytest -q` — 851 passed, 0 failed, 0 skipped/xfailed
   (grepped for skip/xfail markers across `tests/` — none exist in this
   suite), `uv run alembic heads` unchanged (single head, still
   `a1c5e9f2b6d3` — no migration), `scripts/scan-tracked-tree.sh` clean.

**Why:** Both were explicit, named PR #42 acceptance blockers requiring a
real root-cause diagnosis, not documentation or reassurance — item 1-4
because an HR-facing disclosure with no provenance check is a genuine
production-correctness defect (an invented requirement read as if it
came from HR's own JD), and item 5 because "no tests were removed in the
latest diff" was, on its own, an insufficiently verified claim against a
number the owner had independently recorded.

**Reversibility:** Fully additive. `DroppedJDCriterionReason.UNGROUNDED`/
`AgentJobDraftToolResult.ungrounded_count`/`AgentJobDraftView.
ungrounded_count` are new enum member/fields alongside the existing
`PROHIBITED`/`prohibited_count` ones — no existing field removed or
renamed. `_is_requirement_grounded_in_jd_text`/`_grounding_tokens` are
new, narrowly-scoped pure functions in `meyar.agent.service`. Template/
headline changes are the same additive notice pattern already
established for `prohibited_count`. No schema/migration change.

## D-047 — Local Ollama transport-egress P0: httpx `trust_env` proxy
bypass of the loopback boundary

**Date:** 2026-09-05
**Decision:** Closes an internal-audit-reproduced P0 with the smallest
security-focused change, no new branch (same task branch as D-046, PR
#42 not yet accepted/merged), no API/scoring/evidence/agent/UI/migration/
deployment change.

1. **Root cause.** `require_loopback_url` validates `MEYAR_OLLAMA_BASE_URL`
   as a logical URL string only. Every `httpx.AsyncClient` constructed for
   Ollama traffic (`OllamaLLMProvider.health`, `OllamaLLMProvider._chat`,
   `OllamaEmbeddingProvider.embed`) previously used httpx's default
   `trust_env=True`. Verified directly against installed httpx 0.28.1
   internals (`Client.__init__`'s `allow_env_proxies = trust_env and
   transport is None`, feeding `_get_proxy_map`/`_mounts`): with
   `trust_env=True` and `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` set in the
   process environment and `NO_PROXY` absent or not covering `127.0.0.1`,
   httpx populates `Client._mounts` with a proxy-routed transport that
   `_transport_for_url` selects **ahead of** the client's own transport —
   including an explicitly injected one — for a request whose logical URL
   is still `127.0.0.1`. So a configured loopback endpoint could still be
   silently re-routed to an attacker-controlled proxy purely by process
   environment configuration, with no code-level indication. This is a
   distinct defect from (and sits below) the existing logical-URL
   loopback check, which cannot detect it — confirmed empirically:
   `httpx.AsyncClient(timeout=5.0)` under `HTTP_PROXY` set nonempty
   `_mounts`; `build_local_only_async_client(timeout=5.0)` under the same
   env has empty `_mounts`.
2. **Fix.** Added one shared construction boundary,
   `meyar.llm.loopback.build_local_only_async_client`, used by all three
   sites above (previously each called `httpx.AsyncClient(...)` directly).
   It passes `trust_env=False` (disables all environment-derived proxy
   selection outright — does not depend on `NO_PROXY` being correct) and
   explicit `follow_redirects=False` (httpx's own default, made explicit
   so a redirect response can never carry a request outside the
   boundary). `OllamaLLMProvider.health` previously built its client with
   no transport-injection support at all (`httpx.AsyncClient(timeout=5.0)`,
   ignoring `self._transport`); it now passes `self._transport` through
   like `_chat` already did, both to use the shared boundary and because
   the audit's regression-test requirement ("health/readiness path if
   separately constructed") is otherwise untestable without live network.
   No route, schema, scoring, evidence, agent-behavior, UI, or migration
   change.
3. **Regression tests** (`tests/test_ollama_transport_proxy_isolation.py`,
   18 new tests): assert directly on the constructed `httpx.AsyncClient`'s
   internal `_trust_env`/`_mounts` state — not merely `request.url.host`,
   which the audit correctly flagged as insufficient since the original
   defect sits below that logical-URL layer — under `HTTP_PROXY`/
   `HTTPS_PROXY`/`ALL_PROXY` individually, with `NO_PROXY` absent (the
   exact audit scenario) and separately with `NO_PROXY=""`; a negative
   control proves a naively-constructed `httpx.AsyncClient` is genuinely
   vulnerable under the same env (mounts non-empty), so the fixed-path
   assertions are not vacuous. Each of the three call sites (chat, embed,
   health) is exercised end-to-end through the real provider classes
   against `httpx.MockTransport`, under proxy env, both proving the fix
   is actually wired in at every site and that request/response handling
   is otherwise unchanged. Two redirect tests (one direct on the shared
   boundary, one through `_chat`) prove a same-origin 302 is surfaced as
   a failure/response rather than followed to an external `Location`.
   Two tests prove the pre-existing non-loopback rejection
   (`require_loopback_url`) still fails closed even under attacker-set
   proxy env, i.e. the transport fix didn't weaken it.
4. **Local-only Ollama operating contract** documented in
   `docs/SECURITY_PRIVACY.md` ("Local-only Ollama operating contract"
   section and an added threat-model row), explicitly distinguishing the
   APPLICATION GUARANTEE this fix provides (loopback URL + no env-proxy
   routing + no cross-boundary redirect, all test-verified here) from the
   HOST/OLLAMA CONFIGURATION GUARANTEE a future deployment preflight must
   separately verify (daemon interface binding, cloud-backed-Ollama
   disabled, approved-model-only, release-managed model digest, host-level
   egress denial as defense in depth) — none of which this repository's
   test suite can check. Explicitly marked **NOT VERIFIED** in this
   development environment; no Mac deployment tooling implemented (out of
   this task's scope by the audit's own instruction).
5. **Quality gates:** `ruff check .` clean, `uv run mypy src` clean (136
   files), `uv run pytest -q` — 869 passed (851 pre-existing + 18 new),
   0 failed, 0 skipped/xfailed, no test removed or disabled, `uv run
   alembic heads` unchanged (single head, still `a1c5e9f2b6d3` — no
   migration), `scripts/scan-tracked-tree.sh` clean.

**Why:** The audit's own required invariant — reject non-loopback
endpoints, ignore environment-derived proxy configuration, don't depend
on `NO_PROXY`, don't follow cross-boundary redirects, preserve existing
timeout/failure semantics and test transport injection — is a direct,
narrowly-scoped security requirement with a verified reproduction path
(installed httpx 0.28.1 internals, not a hypothetical), and the smallest
correct fix is exactly the shared `trust_env=False` construction boundary
the audit itself named as the expected direction, not a broader redesign.

**Reversibility:** Fully additive/localized. `build_local_only_async_client`
is a new function in `meyar/llm/loopback.py`; the three existing call
sites now call it in place of `httpx.AsyncClient(...)` directly, with the
same `timeout`/`transport` arguments (plus `self._transport` now honored
in `health`, previously silently dropped) — no signature of any public
provider method changed, no field added/removed on any schema, no
migration. `test_no_exfiltration.py`'s docstring was updated to describe
the now-shared construction boundary (no assertion changed). Nothing
committed depends on any deployment-side change; the host/daemon
operating-contract section is documentation only.

## D-048 — Candidate-factuality P0: claim-specific extraction evidence and closed agent text authority

**Date:** 2026-09-13
**Decision:** Close two audit-reproduced candidate-factuality defects on
the existing PR #42 task branch. This change does not alter JD grounding,
duration arithmetic, deterministic scoring policy, the API, or the local
Ollama-only model boundary.

1. **Accepted-extraction boundary.** A real quote existing in the cited
   document is necessary but no longer sufficient. Before a professional
   fact can enter a successful `CandidateProfileVersion`, one of that
   item's own verified quotes must contain all populated material values
   of the fact. Skills accept the same curated aliases used by
   deterministic matching; language proficiency, certification identity,
   education institution/degree/field/date, and employment
   role/employer/date/current relationships are checked when populated.
   The existing skill/domain interval checks remain separate and their
   arithmetic is unchanged. Failure is `CLAIM_EVIDENCE_UNSUPPORTED`,
   making the extraction `FAILED`; it cannot become positive search/
   scoring evidence and is not converted into `NOT_MATCHED`.
2. **Exact deterministic guarantee and limit.** Attribution uses
   normalized literal whole-term matching, including Azerbaijani case/
   diacritic folding and the existing curated skill aliases. A positive
   skill mention is rejected for explicit English constructions matching
   `no`, `without`, and enumerated `not required/known/used/possessed/...`
   forms. This is intentionally not a claim of general entailment,
   paraphrase resolution, negation scope, or multilingual contradiction
   detection. Ambiguous/non-literal support fails closed as unverified.
3. **Agent authority boundary.** `AgentDecision` no longer contains a
   model-authored `message`. `FINAL_ANSWER`/`CLARIFY` select a closed
   `AgentResponseCode`, and the server maps it to bounded non-candidate
   copy. Candidate facts are rendered only from validated, tenant-scoped
   tool results or server templates over model-selected `GroundedFact`
   ids. The deterministic evaluator remains the only numeric authority;
   hiring remains a human decision, represented by fixed server copy.
4. **History boundary.** Newly persisted assistant turns carry an explicit
   `SERVER_VALIDATED` text-authority marker. Re-rendering trusts stored
   assistant text only with that marker; legacy unrestricted text falls
   back to the deterministic outcome message, so an old model-authored
   candidate claim cannot reappear after refresh.
5. **Verification.** The focused extraction/evidence/search/evaluation/
   agent/UI/prompt suite passes 216 tests. Full gates: `ruff check .`
   clean, `mypy src` clean (136 source files), `pytest -q` 887 passed with
   no failures/skips/xfails, one Alembic head (`a1c5e9f2b6d3`), and the
   tracked-tree scan clean. No test was removed, skipped, xfailed, or
   weakened.

**Why:** The former evidence check proved only that a quote existed, so
`Python` plus an `Advanced Excel` quote could be accepted and later
deterministically match Python. Separately, schema-valid zero-tool
`FINAL_ANSWER` prose could state invented experience and a hiring
recommendation, then be rendered and persisted. Both violated the product
authority model at the point where untrusted model output became accepted
state or HR-facing text.

**Reversibility:** The extraction checks and shared inverse alias view are
localized deterministic validation. The agent schema replacement is
closed and explicit; persisted JSON remains migration-free because legacy
rows are handled conservatively at render time. No model, external
service, database column, public route, scoring rule, or UI style changed.

## D-049 — Current factual authority is evidence-valid now, not merely `COMPLETED`

**Date:** 2026-09-14
**Decision:** Close the remaining issue #44 candidate-factuality P0s on
the existing PR #42 branch without changing APIs, duration arithmetic,
JD criterion grounding, or immutable stored provenance.

1. **Central contradiction policy.** The claim-support primitive now
   applies its deterministic positive-term check to every populated
   material term, not skills alone. It rejects bounded, explicit English
   `no`, `without`, nearby `not`, and supported auxiliary-plus-negative
   verb constructions for skill, language/proficiency, certification,
   education, employment, project, domain, skill-experience, and
   domain-experience facts. Existing normalization, skill aliases, domain
   aliases, verbatim-location checks, and interval rules are reused. This
   is deliberately lexical and fail-closed, not general entailment.
2. **Linked employment attribution.** A skill/domain experience item's
   accepted quote must support its subject and interval as before and,
   when `employment_index` is present, the referenced employment's
   material role/employer relationship too. A valid index alone cannot
   attach a Globex Python period to an Acme role or contribute misleading
   linked duration/context.
3. **Current-read authority backstop.** `meyar.services.profile_authority`
   rebuilds the exact canonical professional view and runs the current
   evidence validator before a `COMPLETED` profile is consumed. Search,
   deterministic evaluation (including cached-evaluation reuse), agent
   profile/evidence tools, library/detail/search/ranking presentation, and
   evaluation-history presentation use that shared boundary. Unsupported
   legacy facts are unavailable; they are never translated to
   `NOT_MATCHED`, and stored profile/evaluation rows are not rewritten.
4. **Identity value attribution.** `meyar.services.identity_authority`
   similarly revalidates current identity content for HR presentation.
   Email must match normalized value, phone must match normalized digits,
   and all material normalized name tokens must occur in one field-owned
   accepted quote. Identity remains absent from all suitability inputs.
5. **Agent display authority.** A model-authored JD title can remain only
   as editable review data when source-bound to the HR's JD; otherwise it
   becomes `Vakansiya qaralaması`. The assistant headline is fixed server
   copy, so title text cannot be persisted as `SERVER_VALIDATED` prose or
   replayed from history. Raw `evidence_topic` is never returned for
   display; it selects a validated fact title, and unresolved topics use
   generic server-owned copy. Inspection found no equivalent remaining
   `AgentDecision` free-text route to live/persisted assistant prose.
6. **Known product degradation unchanged.** One unsupported claim still
   makes the entire extraction version `FAILED`, temporarily withholding
   otherwise valid facts. This is **SAFE BUT PRODUCT-DEGRADING** and is
   intentionally left for later issue #44 remediation; no partial-claim
   persistence/recovery was introduced in this P0 closure.
7. **Verification.** Focused adversarial/existing-flow suite: 186 passed.
   Full gates: `ruff check .` clean; `mypy src` clean (138 source files);
   `pytest -q` 910 passed with no failures/skips/xfails; one Alembic head
   (`a1c5e9f2b6d3`); tracked-tree scan clean.

**Why:** A stored status described historical processing, not current
authority. Narrow skill-only contradiction handling, index-only employment
links, location-only identity evidence, and two model-authored display
fields each allowed unsupported content to cross that boundary.

**Reversibility:** Two small read-time authority services centralize the
existing validators; consumer changes are call-site substitutions and
presentation redaction only. No migration, stored-row mutation, public
schema change, scoring-policy change, or duration-policy change.


## D-050 — Canonical context and durable factual authority (issue #44)

**Date:** 2026-09-14. **Status:** Local corrective implementation; PR #42
remains not accepted. Supersedes D-049's claim of complete consumer coverage.

**Problem:** The independent audit at `7b748f4` reproduced cropped-quote
negation bypasses, overbroad negation, unproved current state, split domain
interval support, wrong employment periods, unauthorized legacy embedding
input, identity substring manufacture, and replay of older unsafe
`SERVER_VALIDATED` text. The shared search fixture helper also repaired
unsupported evidence silently, masking invalid positive fixtures.

**Decision:**
- Resolve evidence against its canonical page/block and every matching
  quote occurrence. Claims still need a quoted material term, but contradiction
  checks include up to 200 normalized source characters on each side.
  Ambiguous occurrences must all support the claim. English local no/not/without
  rules use token boundaries and stop at conjunction/clause boundaries;
  notable/notification and negation of another conjoined subject remain valid.
- Current state needs a positive marker in the attributed relationship,
  including when a textual end date triggers the existing duration parser.
  Ended/negative/closed relationships cannot authorize extending to as-of.
  Domain subject and all interval fields must share positive evidence;
  skill/job attribution additionally checks the referenced employment's
  calendar bounds. Duration arithmetic itself is unchanged.
- Embedding generation and reuse call the same profile authority as search
  and evaluation. Folder readiness validates profile and identity; demo
  readiness uses authorized profiles and completed evaluations.
- Identity requires complete canonical email tokens, one coherent formatted
  phone occurrence, and material name tokens with canonical boundaries.
  Identity remains presentation-only.
- New assistant display text persists `text_authority_version` equal to
  `candidate-factuality-v2` alongside `SERVER_VALIDATED`. Only that combination
  permits verbatim replay. Older/missing versions use fixed outcome copy.
  This is an additive JSON field, not a database migration or historical
  backfill; reads never mutate historical provenance.
- Persistence test helpers preserve supplied claims/evidence exactly.
  `synthetic_evidence(...)` is an explicit positive-fixture authoring helper,
  never an automatic repair. Adversarial tests supply independent canonical
  source and evidence, and exercise actual persisted consumers.

**Limits:** Bounded lexical validation is not NLI. Unenumerated multilingual
negation, distant context, complex grammatical scope and arbitrary semantic
paraphrases are not inferred. Ambiguity may reject legitimate claims. One
unsupported fact still rejects the whole profile: SAFE BUT PRODUCT-DEGRADING.
No partial-claim persistence, JD binding, API or duration redesign is included.

**Verification:** 59 new regressions; the focused new/identity/legacy-consumer
suite passed 86 tests. Before correction, the original 37-case reproduction
had 19 failures and 18 passes. Final `ruff check .` passed; `mypy src` passed
for 138 source files; `pytest -q` passed 969 tests, with no skips or xfails.
Alembic retains the single `a1c5e9f2b6d3` head. Tracked-tree scan and diff
whitespace checks passed. AST comparison confirmed all 204 existing test
functions in modified test files retain their assertions and decorators.
An additional probe executed the actual `86d3e3f` parent JD-headline renderer
and verified its unsafe output is suppressed on current history replay,
without changing the stored turn. Model/provider tests use synthetic data
and local fakes; no live model or Target-Mac benchmark was run.

## D-051 — Deterministic factual-authority scope completion (issue #44)

**Date:** 2026-09-14. **Status:** Local corrective implementation; PR #42
remains open and unaccepted.

**Problem:** An independent audit of `6d12c0c` found five remaining bounded
scope defects in D-050's token/span validator: `or` unconditionally terminated
negation before a second coordinated subject; newline normalization erased a
structural boundary; `not only` was treated as genuine negation; a repeated
short quote could not distinguish historical negative and later positive
occurrences; and the phone token grammar allowed a period to join separate
numeric fragments.

**Decision:**
- Keep the existing token/span architecture. `or` stays inside a governing
  `no`/`without`/`not` phrase, while ordinary positive `and`, contrastive
  `but`/`however`, sentence punctuation, semicolons, and newlines terminate
  scope. This is a bounded English lexical rule, not general NLP.
- Treat `not only ... but also ...` as non-negative without exempting ordinary
  `not` assertions such as `Python is not used`.
- Preserve newlines during canonical context matching and polarity analysis;
  flexible whitespace still allows a model quote to bind to the cited source.
- Retain D-050's fail-closed repeated-occurrence invariant because
  `EvidenceRef` contains page, block, and quote but no character offset. Every
  identical occurrence of an ambiguous short quote must agree. A longer unique
  quote can identify and authorize the later positive occurrence in the same
  block; the validator never guesses which short occurrence was intended.
- Define phone support as one complete phone-like lexical occurrence composed
  of digit groups, optional leading `+`, balanced digit parentheses, spaces,
  and hyphens. Periods, words, commas, extensions, and additional digits cannot
  be concatenated into the requested identity. Identity remains presentation-
  only.

**Scope:** No schema/migration, JD source binding, API, duration arithmetic,
deployment, scoring, search, agent orchestration, or persistence behavior was
changed. The shared current-authority boundary automatically applies the
correction to extraction, embedding, search, evaluation/cache reuse, ranking,
agent tools, library/detail views, and assistant history presentation.

**Verification:** Direct scope/idiom/boundary/repeated-occurrence/phone tests
and a database-backed all-consumer quarantine regression were added. The final
gate counts are recorded in the associated local commit report.

## D-052 — Coordinated-negation and phone-occurrence authority completion (issue #44)

**Date:** 2026-09-15. **Status:** Local corrective implementation; PR #42
remains open and unaccepted.

**Problem:** The independent audit of `12e218f` found that D-051 treated every
`and` as a scope boundary, allowing the second member of `no`/`without`/
`does not use` coordination to become positive authority. `neither ... nor ...`
had no explicit negative-governor state. The phone occurrence grammar also
treated two arbitrary numeric fragments separated by a space or hyphen as one
phone token.

**Decision:**
- Preserve the token/span and canonical-context architecture. A small lexical
  state now activates only for `no`, `without`, `neither`, and the supported
  `do`/`does`/`did not use` construction. While active, `and`, `or`, and `nor`
  coordinate members; they neither create negative scope in a positive list
  nor end an existing negative list. Periods, semicolons, independent newlines,
  `but`, and `however` reset the state.
- Keep ordinary `not` local to its own coordinated member and retain the
  existing post-subject `is/was/are/were not` check. `not only ... but also ...`
  remains explicitly non-negative.
- Phone attribution now filters complete canonical phone-token matches by
  shape. A plain uninterrupted digit occurrence is coherent; formatted values
  require an international/local prefix or at least three groups. Two arbitrary
  fragments such as `1234 56789` or `1234-56789` cannot be joined, including
  when a cropped evidence quote omits the canonical `Reference` context.

**Scope:** No schema/migration, JD source binding, API-first work, duration
redesign, deployment, scoring, ranking, search-planner, or embedding-identity
feature was introduced. The shared current-authority boundary applies the
correction to existing consumers only.

**Verification:** The pre-fix focused reproduction failed 12 cases (the second
`and` member, all `neither`/`nor` members, and both split-number forms). The
corrected direct matrix passed 79 tests; the broader extraction/identity/
embedding/search/evaluation/ranking/agent/UI suite passed 362 tests. Full gates:
Ruff clean; mypy clean for 138 source files; 1032 pytest tests passed with no
failures, skips, or xfails; Alembic retained the single `a1c5e9f2b6d3` head.

## D-053 — Phone identity requires canonical contact authority (issue #44)

**Date:** 2026-09-15. **Status:** Local corrective implementation; PR #42
remains open and unaccepted. Supersedes D-052's treatment of every uninterrupted
digit token as inherently phone-coherent.

**Problem:** The independent audit of `2df8ea9` showed that a model-extracted
phone value `123456789` was accepted from canonical `Reference`, `Invoice`,
`Employee ID`, and `Account` occurrences containing those digits. The untrusted
field name `phone` supplied the only phone meaning; canonical source context did
not.

**Decision:** Classify each complete numeric occurrence using its canonical
block, including context outside a cropped evidence quote. Explicit bounded
non-phone identifier labels (`Reference`/`Ref`, `Invoice`, `Employee ID` or
number, `ID`, `Account`/`Acct`, and `Code`) reject first. Otherwise phone
authority requires either conventional syntax (leading `+`, a balanced numeric
parenthesis group, or at least three space/hyphen-separated digit groups) or a
directly adjacent bounded phone/contact label (`phone`, `mobile`, `telephone`,
`tel`, `telefon`, `mobil`, `contact number`, or Azerbaijani `əlaqə nömrəsi`).
The label is canonical evidence, not the model-produced schema field name.

An uninterrupted bare digit token with no trustworthy contact context now
fails closed. This can suppress a legitimate unlabeled phone number; that is an
accepted bounded limitation because canonical provenance cannot distinguish it
from an arbitrary identifier without guessing. Repeated cropped occurrences
retain D-050's fail-closed all-occurrences rule.

**Scope:** Shared identity evidence/authority and synthetic regressions only.
No professional claim/negation semantics, schema/migration, JD source binding,
API-first work, duration arithmetic, deployment, scoring, ranking, search, or
stored-row mutation changed. Historical identity rows remain immutable and are
suppressed on read when they fail the current contract.

**Verification:** Direct positive/negative and cropped-canonical phone matrices,
plus a database-backed legacy `COMPLETED` identity presentation regression, were
added. Final gate counts are recorded in the associated local commit report.

## D-054 — JD source-fragment and classification-independent prohibition boundary (issue #44)

**Date:** 2026-09-15. **Status:** Local corrective implementation; PR #42
remains open and unaccepted.

**Pre-fix reproduction at exact accepted HEAD `8798c6a`:** the production
`_dispatch_draft_job_criteria` boundary, driven with synthetic model drafts,
accepted `Python Kubernetes` from `Python required`; accepted `Python` while
silently discarding model-authored `min_years=20`; exposed `Female` classified
as `OTHER` as ordinary unsupported (`prohibited_count=0`); converted `5 years
of Python experience` into general `EXPERIENCE=5` plus bare `Python`; and
returned no item at all when the model omitted `Candidate must be willing to
travel`.

**Root causes:** D-046 checked only whether half of a requirement's words
occurred anywhere in the JD. It had no attributable source fragment, no
field/kind/modality/number/scope binding, discarded `min_years` for non-general
experience kinds, entered the `OTHER` branch before the `CriterionIn` denylist,
and never reconciled source requirements against model output. The review form
also cannot round-trip `required_level`, `SKILL_EXPERIENCE`, or
`DOMAIN_EXPERIENCE`, so accepting those drafts would erase or weaken semantics
at confirmation.

**Decision:**
- Each `JDDraftCriterionItem` now carries `source_text`. The service locates it
  inside a bounded source requirement span and validates all material subject
  and scope tokens in both directions, with only the project's established
  case/diacritic and bounded Azerbaijani suffix tolerance. Kind cues,
  MUST_HAVE/PREFERRED modality, numeric values, duration scope, and language
  level must be attributable to that same fragment. One matching token is
  never sufficient. Model-authored weights are removed; accepted drafts use
  the existing deterministic default `1.0`.
- General `EXPERIENCE` accepts an attributed number only for explicitly total/
  general experience or a subject-free duration phrase. A named-skill/domain
  duration cannot become total experience or bare presence. Numbers cannot be
  borrowed from another source span. No duration arithmetic changed.
- `find_prohibited_term` runs over the raw JD and every model-produced
  requirement/source/level before `OTHER` or any other kind branch. Raw
  prohibited text is represented count-only. The post-confirmation carry-
  through fields are filtered through the same denylist and never score.
- Source spans are reconciled after draft validation. Every detected span is
  accepted/scorable, visible unsupported/unscored, prohibited count-only, or
  visible needs-human-review. A model omission therefore cannot erase it.
- The current review form cannot losslessly round-trip language proficiency or
  skill/domain-duration kinds. Those validly source-bound requirements are
  kept as source-verbatim unsupported/unscored items instead of being silently
  persisted as weaker criteria. General numeric experience remains supported.
  This is compatibility enforcement, not the deferred duration-policy/UI
  redesign.
- Drafting still performs no mutation. Only editable accepted/scorable rows
  reach the existing `POST /ui/jobs` confirmation path and deterministic
  ranking. Unsupported/review items remain visible before and immediately
  after confirmation but never enter `JobCriteriaVersion`; prohibited items
  never render verbatim. HR copy contains no internal reason enum.

**Verification:** 17 focused production-boundary adversarial/positive tests
plus database-backed agent/UI confirmation coverage pass; the combined agent
service/UI/source-binding suite passes 105 tests. Full gates: Ruff clean; mypy
clean for 138 source files; 1060 pytest tests pass with no failures, skips, or xfails; Alembic and
tracked-tree results are recorded in the final local report. No migration,
API-first work, deployment work, score arithmetic, candidate factual authority,
or duration arithmetic changed.

**Bounded limitations:** This is deterministic lexical/structural attribution,
not NLI. Implicit modality, word-form numbers, complex multi-requirement prose,
and unrecognized paraphrases may fail closed into human review. A partial
source quote does not authorize an entire multi-requirement sentence; the
uncovered source span remains visible for review. These are safe but potentially
product-degrading P1s, not silent acceptance paths.

## D-055 — Server-owned canonical JD requirement spans supersede model source authority (issue #44)

**Date:** 2026-09-15. **Status:** Local corrective implementation atop exact
audit HEAD `f4a728c`; PR #42 remains open and unaccepted.

**Reproduced P0:** D-054 still let the model choose the effective authority
boundary. `_source_span_index` accepted a model substring inside a larger
source occurrence, while bidirectional prefix/token matching equated distinct
subjects. Consequently `5 years Python experience required` plus model
`source_text="Python experience required"` produced scorable bare Python;
Java/JavaScript, C/C++, and SQL/NoSQL collided; `Banking experience preferred`
could become plain SKILL; ordinary `sağlamlığı`/`əlilliyi` inflections bypassed
the raw denylist; and omitted `Python is a plus` had no canonical span to
reconcile.

**Decision:**
- The server deterministically segments the raw JD before inference into
  occurrence-specific `RequirementSpan` objects with a stable per-operation id,
  start/end offsets, exact original slice, and normalized representation.
  Conservative sentence/newline/semicolon boundaries are used; a same-sentence
  conjunction splits only when every side has its own explicit modality.
  Otherwise the complete clause is retained for human review.
- The model receives those ids and references `span_id`. Its retained
  `source_text` is debugging/usability data only. Unknown/missing ids cannot
  score, a repeated occurrence is reconciled by id rather than first text
  match, and one occurrence authorizes at most one criterion.
- Complete-span semantics, not prefix overlap, determine modality, numeric and
  proficiency qualifiers, general versus skill/domain-qualified experience,
  and kind compatibility. Complete subject identity is exact after only the
  accepted deterministic normalization and curated aliases (`py`/Python,
  `k8s`/Kubernetes, Postgres/PostgreSQL). Distinct subjects such as Java/
  JavaScript, C/C++, SQL/NoSQL, and Go/Django never authorize one another.
  Unordered multi-token equivalence is not used.
- Skill/domain-specific experience and language proficiency remain visible as
  UNSUPPORTED/UNSCORED when the current review form cannot round-trip them;
  incompatible/weakened or ambiguous forms are NEEDS_HUMAN_REVIEW. General
  explicitly total experience remains scorable. Scoring arithmetic and the
  accepted candidate factual-authority contracts are unchanged.
- Every canonical material occurrence receives one explicit terminal state:
  SCORABLE, UNSUPPORTED, PROHIBITED, or NEEDS_HUMAN_REVIEW. Omission therefore
  cannot delete a requirement. Raw-JD and post-parse prohibition remain
  independent of model kind. Azerbaijani protected lexeme families add bounded
  consonant-mutation forms without generic substring matching.
- The server stores the pending canonical draft only in the authenticated,
  tenant-scoped conversation. The confirmation form submits `draft_id` and
  span ids; `POST /ui/jobs` resolves that server authority and accepts only an
  unchanged subset of its SCORABLE rows. Browser edits/additions/duplicates and
  replay are rejected. Unsupported/review text is reconstructed from server
  state, never trusted from hidden fields. Only after this check are the
  existing Job/CriteriaVersion/ranking services called.
- HR guidance now says a personal/sensitive requirement was detected, cannot
  be used for ranking, and should be removed or replaced by a job-related
  professional requirement. Internal reason codes and prohibited source text
  are not rendered as system guidance or persisted into scoring structures.

**Explicit deferrals:** durable unsupported/review-state persistence across a
later reload/re-rank and natural-language requested-result-count (`top 10`)
handling remain NOT IMPLEMENTED. No deployment/API-first work, evaluator
schema expansion, duration arithmetic, or candidate-authority redesign is in
this correction.

## D-056 — Dedicated idempotent agent confirmation and bounded residual review (issue #44)

**Date:** 2026-09-15. **Status:** Local corrective implementation atop exact
audit HEAD `4b900567f289c57b027b83e8ae04476dad5d98dc`; PR #42 remains open
and unaccepted. Supersedes D-055 only where D-055 routed confirmation through
`POST /ui/jobs`, accepted a scorable subset, and deleted consumption state.

**Reproduced P0/P1:** The browser-owned `from_agent_draft` field selected
whether `/ui/jobs` enforced canonical authority; deleting it converted agent
confirmation into unrestricted manual creation. Consumption then removed the
only draft link before inline ranking, so a ranking failure reported an error
after Job/version/audit state had committed and could not be retried through
the original operation. The bounded modality parser also treated arbitrary
`is plus` material as preference, and standalone `Python` produced no span.

**Decision:**
- Manual creation and agent confirmation are distinct operations. The normal
  review form posts to `POST /ui/agent/drafts/{draft_id}/confirm`; its path and
  authenticated context, never a hidden mode flag, select the protected flow.
  `/ui/jobs` rejects agent provenance instead of ignoring it.
- Confirmation locks the tenant/session conversation row, resolves only its
  stored canonical draft, and requires the exact unchanged SCORABLE set.
  Value/subject, kind, modality, duration, weight, span identity, insertion,
  duplication, deletion, and unsupported-to-scorable conversion all fail
  before Job/version/audit persistence.
- The same transaction replaces `pending_job_draft` with a durable
  `confirmed_job_draft` link containing the resulting Job/version ids and the
  safe unscored display lists. Replay resolves that object idempotently. The
  row lock makes concurrent confirmations serialize; exactly one creates the
  canonical Job/version. Ranking begins only after that transaction commits.
  A later ranking failure explicitly says confirmation succeeded and offers
  the existing rank action as a retry, without duplicating confirmed state.
- English `<subject> is a plus` is recognized only as a full bounded idiom
  whose subject belongs to a small reviewed professional taxonomy shared with
  curated skill aliases. Trailing VAT/bonus/arithmetic material is not
  preference authority. Exact standalone recognized professional subjects,
  bullets, and existing short requirement cues receive a canonical span but
  no invented modality; they remain NEEDS_HUMAN_REVIEW. Ordinary descriptive
  prose remains outside reconciliation.
- A zero-scorable draft retains its source disclosures but renders no
  confirm/rank action and explains in HR language that clarification/review is
  required.

**Schema and scope:** No migration. The existing bounded conversation JSON
holds the durable confirmation link. Candidate factuality, canonical span
offset/subject/type authority, scoring arithmetic, and evaluator behavior are
unchanged. Skill/domain-duration scoring, language-level scoring, durable
unsupported/review display across arbitrary later reload/re-rank, natural-
language top-K, API-first/deployment work, and the concrete mixed unsupported
HR example's missing evaluator capabilities remain explicitly deferred.

## D-057 — Zero-authority model source hints and independent confirmation identity (issue #44)

**Date:** 2026-09-15. **Status:** Local corrective implementation atop exact
audit HEAD `155bc8f7d89a95b58b1e5862b68a184850510cd2`; PR #42 remains open
and unaccepted. Supersedes D-056 only where it treated bounded conversation
JSON as the durable confirmation identity.

**Reproduced P1s:** `_build_criterion_from_draft_item` passed model-authored
`requirement`, `source_text`, and `required_level` into prohibited-term policy
before resolving the server's canonical span. Thus a `Female required`
`source_text` hint on canonical `Python required` manufactured a prohibition,
suppressed Python, and incremented `prohibited_count`. Separately, the only
confirmed draft→Job/version lookup walked bounded `AgentConversation.turns`;
reset/truncation removed the otherwise committed idempotency mapping.

**Decision:**
- `source_text` remains an optional/default-empty model usability hint, but no
  policy branch reads it. Prohibition is derived from raw JD and server-owned
  canonical spans only; complete-span binding continues to own subject, kind,
  modality, number/duration, proficiency, terminal state, and scoring
  eligibility. The valid/missing and fabricated-hint matrix therefore has the
  same result as the canonical span, while a real canonical prohibition cannot
  be hidden by a safe-looking hint.
- A dedicated `agent_draft_confirmations` table stores only confirmation
  identity: tenant, canonical draft id, owning browser-session id, Job id,
  criteria-version id, fixed `CONFIRMED` status, and timestamp. Unique
  constraints enforce one row per tenant/draft and one confirmation per Job
  and criteria version; foreign keys bind the server-owned objects.
- Confirmation locks the session conversation as before, checks the new table
  before pending transcript state, validates the unchanged canonical draft,
  then atomically writes Job, criteria version, audit event, independent
  confirmation row, and optional transcript UI state. Ranking remains a
  separate post-commit step. Replay resolves the independent row even after
  transcript reset and uses transcript disclosures only when their ids agree;
  the independent record always wins identity disagreements.
- Lookup requires the authenticated tenant and exact browser session as well
  as draft id. Another tenant/session sees safe not-found behavior. The
  existing conversation row lock serializes ordinary same-session concurrency,
  while database uniqueness fails closed if an abnormal competing write
  bypasses that serialization.

**Schema and scope:** Migration `c7e91a4d2f60` adds only the independent
identity table, indexes, foreign keys, fixed-status check, and unique
constraints; no JD or candidate content is duplicated. D-055 canonical span
segmentation and all accepted candidate factual-authority logic are unchanged.
Skill/domain-duration scoring, language-level scoring, natural-language top-K,
full durable unsupported/review product history, API-first work, and deployment
remain explicitly deferred.

## D-058 — Final criterion/workflow parity and bounded top-K (issue #44)

**Date:** 2026-09-16. **Status:** Local corrective implementation on
`feat/agent-product-ux-jd-matching`; PR #42 remains open and unaccepted.

**Root cause:** The deterministic evaluator already supported attributable
skill/domain intervals, but the canonical JD dispatch deliberately routed
`SKILL_EXPERIENCE`, `DOMAIN_EXPERIENCE`, and any `required_level` to
UNSUPPORTED because the review form could not round-trip them. The same seam
kept unsupported/review disclosures only in bounded transcript state, had no
vacancy result-count contract, and resolved UI dates from unconfigured
`date.today()`. Language evaluation compared only exact strings and treated a
missing level as partial evidence.

**Decision:**
- Canonical span binding now admits the three already-defined semantic shapes
  after exact subject/scope/modality/number/level validation. Confirmation
  revalidates kind, value, modality, `min_years`, `required_level`, weight,
  evidence policy, and review policy. Draft weights remain system-owned `1.0`.
- Skill duration unions attributable item-owned intervals, never employment
  duration, and additionally requires each interval to fit its linked
  employment occurrence. Domain presence without a numeric threshold matches
  only explicit accepted domain evidence; numeric domain duration follows the
  same attributable/overlap-safe rules. Missing or incompatible evidence is
  UNKNOWN.
- CEFR has one fixed ordering (`A1 < A2 < B1 < B2 < C1 < C2`). A lower CEFR
  level is NOT_MATCHED, an equal/higher level MATCH, missing level UNKNOWN,
  and incompatible scales UNKNOWN. Non-CEFR labels compare only by exact
  normalized equality.
- `JobCriteriaVersion` now durably stores safe unsupported/review disclosures,
  effective `result_limit`, and whether the agent workflow presents eligible
  results only. Migration `6f4c2a9d8e10` backfills `[]`, `[]`, `20`, and false.
  Prohibited text is not stored. Reload/re-rank reads the immutable version.
- Result-count phrases are parsed separately from requirement spans. Default
  is 20 and the server bounds explicit intent to 1–100. Count affects only
  presentation truncation. Agent-confirmed vacancies present only
  STRONG_MATCH/POTENTIAL_MATCH results; API/manual historical ranking retains
  its prior all-fit-band behavior. Scores and MUST_HAVE/PREFERRED policy are
  unchanged.
- `MEYAR_BUSINESS_TIMEZONE` (default `Asia/Baku`) owns UI date resolution.
  Search, agent turns, confirmation, and re-rank resolve one date at their
  application boundary and pass it explicitly. Deterministic scoring services
  still have no clock access.
- Evaluator semantics materially changed, so provenance/cache identity moves
  from `meyar-policy-v1` to `meyar-policy-v2`; scoring arithmetic remains
  `meyar-score-v1`. Explanations now persist and display the evaluator's
  criterion-level evidence conclusion, while internal reason codes remain
  hidden from normal HR UI.

**Unchanged:** candidate factual authority, canonical RequirementSpan and
prohibited policy, confirmation idempotency/tamper/tenant/concurrency guards,
ranking retry, identity exclusion, local-only candidate AI, and deterministic
score arithmetic. No API-first or deployment work is included.

## D-059 — Primary vacancy browser-flow typed boundary and protected review (issue #44)

**Date:** 2026-09-16. **Status:** Local corrective implementation on exact
audit base `dea50267c2936d2538dae305d20b6208c149b926`; PR #42 remains open
and unaccepted.

**Reproduced root cause:** The real browser path did call the configured local
Ollama provider. Its JSON-Schema mode failed before inference because the
configured model/runtime could not compile the schema vocabulary. In plain
JSON mode the same provider exposed the second boundary mismatch: it could
still emit legacy `EXPERIENCE`/`SKILL + min_years`, incomplete `LANGUAGE`, a
wrong-bucket domain item, and a top-K `OTHER` item. The application accepted
only the new typed contract, so valid source occurrences never reached the
review UI and the entire turn collapsed to a generic draft failure.

**Decision:**
- Ollama JD drafting uses its supported JSON-output mode, followed by the
  existing strict Pydantic validation and bounded repair attempt. Runtime
  configuration accepts only `ollama`; no fake or deterministic provider can
  be selected silently. Provider unavailability still fails visibly.
- A bounded application adapter resolves every model item through its exact
  server-owned `RequirementSpan`, derives only fields already present in that
  occurrence, and then runs the unchanged canonical authority validator.
  It maps skill duration, domain experience, and language level onto the D-058
  typed contract; unsafe generic, mismatched, invented, or ungrounded shapes
  continue to fail closed. A model echo of top-K remains workflow metadata,
  never a criterion.
- An implicit bounded CEFR-language occurrence has no invented modality. It
  is rendered as a supported human-review item. HR may choose only the
  server-declared required/preferred field; subject, level, span identity, and
  professional semantics remain server-owned. Confirmation is unavailable
  until that ambiguity is resolved and continues through the protected draft
  endpoint.
- The review visibly presents scorable rows, review-required rows, and result
  count. Ranking exposes criterion evidence and replaces the post-confirm URL
  with a stable authenticated GET route so reload preserves the confirmed
  semantics and disclosures. The existing `Namizədlər` header correctly
  targets `/ui/library`; no `/ui/candidates` alias is added.

**Unchanged:** candidate/JD factual authority, prohibited filtering,
tenant/session isolation, confirmation tamper protection and idempotency,
deterministic evaluator/date/top-K semantics, score arithmetic, and local-only
AI. No evaluation-date input, API-first work, deployment work, push, or merge
is included.

## D-060 — Source-bound AZ/EN semantic requirements and shared search parity (issue #44)

**Date:** 2026-09-17. **Status:** Local corrective implementation on exact
audit base `1aed40ef66df563f05b1782e9861909176c77fa7`; PR #42 remains open
and unaccepted.

**Root cause:** D-059 still let the small local model determine too much of
the material shape. Canonical spans were coarse for ordinary Azerbaijani,
ASCII input, paragraph lists, and unfamiliar professional vocabulary;
normalization deleted example-specific tokens rather than bounded grammatical
wrappers; result-count syntax was narrow; ordinary search rejected semantic
shapes already supported by the evaluator; and routing never consulted a
pending draft before treating a follow-up as a new search. The protected-term
denylist also covered Azerbaijani ``milliyyət`` but not ordinary
``vətəndaş``/citizenship language, allowing nationality to be recast as a
scorable language.

**Decision:**
- Supported HR input is Azerbaijani and English, including safe mixed
  professional terminology. Cyrillic/Russian fails closed before model
  inference and before any criterion exists, with guidance to use AZ or EN.
- The server first scans raw supported-language text for protected classes,
  creates exact requirement occurrences, derives independently attributed
  subject/modality/duration/proficiency slots, scans the resulting span and
  normalized subject again, and only then constructs a typed criterion.
  Gender/sex, age, nationality/citizenship, health, and disability policy is
  independent of model kind, subject, modality, omission, or ``OTHER``.
- Ollama may propose a title and legacy semantic draft, but no model-authored
  material field is scoring authority. A server-owned ``SemanticRequirement``
  binds every material field to offsets in one canonical source occurrence.
  Each occurrence terminates as SCORABLE, NEEDS_HUMAN_REVIEW,
  UNSUPPORTED_VISIBLE, or PROHIBITED; result count is separate workflow data.
- Grammar normalization removes only bounded AZ/EN HR wrappers and preserves
  the remaining exact professional occurrence. Curated aliases still resolve
  true aliases; unfamiliar safe terms are not rejected for being absent from
  the taxonomy. Strict ``>`` duration is never weakened to ``>=``.
- Ordinary search consumes the same source-bound skill duration,
  domain-duration, language-level, and result-count primitives and delegates
  evaluation to the existing deterministic criterion evaluators. Unsupported
  project-scoped meaning remains non-executable rather than becoming a softer
  skill or semantic query.
- Before normal model routing, a pending draft accepts only four bounded
  server-side changes: result limit, CEFR level, duration threshold, and
  required-to-preferred modality. Each produces a new draft identity and
  retains the exact modification text; unsafe/ambiguous edits request
  clarification. A simple search entered in vacancy mode produces guidance
  and no nonsense criteria.

**Unchanged:** candidate facts and evidence provenance, scoring arithmetic,
identity exclusion, canonical confirmation binding and idempotency,
tenant/session/auth/CSRF controls, protected review endpoints, local-only
candidate/JD AI, and immutable confirmed versions. No migration, model-default
change, API-first work, deployment work, push, or merge is included.

## D-061 — Concept-safe protected policy and source-occurrence reconciliation (issue #44)

**Date:** 2026-09-18. **Status:** Local corrective implementation on exact
independent-audit base `23374bbc43f6a738f7a1b2117accc73fb5ec4aa2`; PR #42 remains open
and unaccepted.

**Reproduced failures:** unseen Azerbaijani citizenship inflection, English
comparative age wording, and an English health idiom reached SCORABLE state.
Whole-clause subtraction retained applicant/predicate/contrast wrappers in
professional identities; undated named experience was weakened to SKILL; a
certification quantity became a skill; simple English search in vacancy mode
missed deterministic guidance; and preferred-to-required follow-up minted a
new draft id without moving the criterion.

**Root cause:** D-060 still combined phrase-bounded protected lexicons with a
subtractive whole-clause normalizer. The final typed criterion schema checked
sensitive labels but did not reject structurally impossible family subjects.
Material detection and modality vocabulary were narrower than the supported
natural prose, result-count recognition was not an independent consumed
occurrence in every form, and follow-up direction was inferred from the mere
presence of both modality words.

**Decision:**
- Protected authority now combines bounded protected lexeme families with
  server-owned AZ/EN concept grammar for nationality/citizenship, comparative
  age, gender/sex, health/medical-fitness idioms, disability, and common
  euphemisms. It runs on raw text and canonical spans before family handling,
  then again on normalized subject and at `CriterionIn`; a wrong model kind or
  safe-looking model subject cannot authorize protected scoring.
- Subject identity is selected from exact source occurrences by bounded
  professional grammar (knowledge/proficiency, AZ knowledge/use inflections,
  experience scope, language, certification, and education wrappers). Unknown
  entities remain supported. Ambiguous unhyphenated AZ suffixes are stripped
  only for reviewed aliases/domains, preventing entity collisions such as an
  unseen product name ending in `-da`.
- Professional family is preserved. Named experience without the evaluator's
  required duration is NEEDS_HUMAN_REVIEW, not weakened to SKILL; generic
  certification quantities and ambiguous coordinated subjects likewise remain
  visible and unscored. Every server-owned material span is checked by a
  one-span/one-terminal-state reconciliation invariant.
- Result-limit consumption stays separate from requirements. Candidate/result
  count-only clauses are removed as workflow control; certification quantities
  cannot become a skill; duration, CEFR, certification quantity, and result
  limit retain independent source slots.
- Modality grammar covers required, preferred, optional/desirable/advantageous,
  negative, and AZ/EN contrast forms. Follow-ups resolve direction from
  `instead of`, `from ... to ...`, or AZ `... yox, ...`; both promotions and
  demotions move the unchanged criterion between buckets and persist the new
  draft. Ambiguous/no-op requests fail truthfully without a new draft id.
- Deterministic wrong-mode detection now treats equivalent simple AZ/EN
  candidate-search requests alike, while duration/level/multi-requirement
  vacancy text is not misclassified. It invokes no model and creates no Job or
  criteria version.

**Validation:** A fresh loopback `qwen3:1.7b` matrix used unchanged model
options: 12 new AZ, 10 new EN, 5 protected probes per language, 5 cross-domain,
5 multi-number/top-K, and 4 modality cases. The initial run exposed the
wrong-mode overreach; after correction the affected five-case rerun passed,
and a final real-model probe confirmed the unseen `-da` entity identity fix.
Corrected aggregate: 46/46 materially correct, zero omissions, hallucinated
scorable fields, protected violations, or generic failures; 5 repair calls;
about 526.4 seconds total selected-sample latency, maximum 38.36 seconds.

**Unchanged:** candidate factual authority, canonical JD ownership,
tenant/session/draft isolation, confirmation idempotency, evidence provenance,
identity exclusion, deterministic scoring/evaluation date, local-only Ollama,
and no-fallback provider behavior. No migration, candidate Q&A/result-set
comparison, deployment, model-option change, push, or merge is included.

## D-062 — Exact-browser subject, domain-presence, follow-up, and search correction (issue #44)

**Date:** 2026-09-18. **Status:** Local corrective implementation on exact
audit base `12392c5113f38b25880ba36417ba5f3aefc57063`; PR #42 remains open
and unaccepted.

**Root causes:** English subject extraction did not consume ordinary
recruitment prose and could attach sentence wrappers to the first experience
criterion; coordinating/subordinate clauses were not always separated before
slot assignment. Azerbaijani `sektorunda` was absent from generic domain
grammar, while D-061's no-duration guard incorrectly included
`DOMAIN_EXPERIENCE` despite its accepted optional `min_years` schema/evaluator
contract. A context-only modality follow-up required a literal criterion name
and did not recognize `əsas tələb`; the exact Java duration/top-K request was
therefore also left to the model planner after a consumed result count made its
otherwise-valid requirement look ambiguous. Wrong-mode template rows were
empty, but the deterministic headline still promised criteria below.

**Decision:**
- Bounded leading recruitment prose, duration-first English experience syntax,
  coordinating `while`/`whereas`, and non-material subordinate prefixes are
  handled before one-to-one terminal-state reconciliation. Subject identity
  remains an exact source occurrence; no product/domain sentence is special
  cased.
- Explicit sector/domain grammar remains open to unfamiliar professional
  domains. Presence-only `DOMAIN_EXPERIENCE(subject, min_years=null)` is
  scorable; duration remains mandatory for `SKILL_EXPERIENCE`. Central domain
  aliases canonicalize Azerbaijani finance wording without limiting generic
  sector grammar. IFRS is preserved as a presence-only domain acronym.
- A pronoun/context-only modality request may select an unnamed target only
  when exactly one criterion exists in the source modality. Named mismatches
  and multiple possible targets still request clarification; successful
  changes mint and persist a new server-owned pending-draft identity.
- A recognized result-count tail is workflow control and no longer converts
  the attached duration requirement to review. Ordinary search therefore
  builds a structured Java-duration plan and treats zero matches as a valid
  result, not provider failure.
- Wrong-mode guidance now replaces the incompatible headline, headings,
  actions, and draft-review help text; routing and persistence behavior are
  unchanged.

**Unchanged:** protected policy, candidate/JD factual authority, deterministic
evaluator and scoring, tenant/session/CSRF/draft authorization, confirmation
idempotency, identity exclusion, local-only Ollama, and the prohibition on
candidate result-set Q&A/comparison. No migration, push, or merge is included.

## D-063 — Result-control and professional-identity authority completion (issue #44)

**Date:** 2026-09-20. **Status:** Local corrective implementation on exact
independent-audit base `5bb13d5dc566d6796072cbde53905962e3cc99bd`; PR #42 remains open
and unaccepted.

**Root causes:** Passport-holder eligibility was outside the protected
citizenship grammar. Result-limit extraction returned the same shape for an
absent and an unresolved explicit count and retained no complete source range,
so count fragments could be reparsed as material. Subject validation rejected
some count-plus-noun shapes but not a bare number word. Certification quantity
masking discarded adjacent named identities. Discourse wrappers could survive
as subject prefixes, and bare English domain experience still depended on a
small sector list despite the evaluator accepting `min_years=null`.

**Decision:**
- Protected grammar recognizes personal citizen/national/passport-holder and
  country-of-citizenship eligibility constructions independently of model
  family. Passport authentication/document systems do not match without the
  personal holder/eligibility grammar; ambiguous personal context fails closed.
- `ResultCountIntent` carries ABSENT, VALID, AMBIGUOUS, or OUT_OF_RANGE and
  exact `ResultControlSpan` occurrences. Only ABSENT receives default 20;
  unresolved explicit wording blocks confirmation with review state. Complete
  control occurrences are subtracted by offset before material parsing, and a
  runtime reconciliation assertion rejects any control/material overlap.
- A shared typed boundary rejects pure digits, AZ/EN number words, counters,
  result nouns, and generic quantity phrases as SKILL, DOMAIN_EXPERIENCE,
  CERTIFICATION, EDUCATION, or LANGUAGE identities. Server semantic parsing
  applies the same non-quantifier identity invariant before SCORABLE state.
- Certification-list grammar propagates source modality through `including`,
  `such as`, and `namely` lists and preserves each exact named identity. Generic
  certification counts remain review-visible and unscored.
- Exact subject offsets exclude leading/trailing AZ/EN connectives, discourse
  preambles, modality wrappers, and bounded case particles. Presence-only
  English experience wrappers classify unambiguous non-technical subjects as
  DOMAIN_EXPERIENCE without inventing duration; structurally technical or
  ambiguous names remain on the review-safe path.

**Validation:** The new structural matrix contains 8 fresh protected forms, 10
AZ result-limit forms, 6 multi-number cases, 6 certification/count cases, 8
English domain cases, 10 connective/subject identities, typed-boundary probes,
and safe passport-system controls. Real loopback `qwen3:1.7b` acceptance used
unchanged options across 32 independent-style requests: 32/32 materially
correct, 3 bounded repairs, zero generic failures, hallucinated scorable
fields, protected violations, or silent omissions; 261.49 seconds total and
32.833 seconds maximum latency. Authenticated Playwright/Google Chrome checks
against the real local app passed 5/5 rendered semantic states with synthetic
data. Focused authority/security/search/evaluator/UI regression: 682 passed;
the full repository suite passed 1,422 tests.

**Unchanged:** deterministic scoring, candidate factual authority, model-output
authority, tenant/session and pending-draft ownership, confirmation idempotency,
ordinary-search persistence/presentation, identity exclusion, no-exfiltration,
default Ollama configuration, and the ban on candidate result-set conversation.
No migration, push, or merge is included.

## D-064 — Structural span-role and family-authorization boundary (issue #44)

**Date:** 2026-09-19. **Status:** Local corrective implementation on exact
independent-audit base `ee5f9f2d9f4bbe1b77cbc5300c17670762f66af9`; PR #42 remains open
and unaccepted.

**Root cause:** D-063 retained exact source occurrences but still allowed the
subject extractor and a narrow count-shape check to confer authority. Generic
quantifiers outside that shape became professional identities, recruitment
wrappers contaminated subjects, certification quantities displaced named
credentials, and result-count extraction could call an unfamiliar explicit
construction ABSENT or select one of two competing values.

**Decision:** Every relevant source occurrence is reconciled into exact-offset
roles (`SUBJECT`, `RELATION`, `QUANTITY`, `DURATION`, `PROFICIENCY`, `MODALITY`,
`CONTROL_RESULT_COUNT`, connective/preamble/protected/generic/other) and one
terminal owner. Result-control ownership is consumed before material parsing;
runtime reconciliation rejects incompatible overlap. Family classification is
not authorization: each scorable family must have an identity-bearing subject,
its required source relation, source modality/material fields, and no personal
eligibility or workflow-control meaning. Pure quantity/result/person shapes fail
again at `CriterionIn`, independent of parser or model kind.

Result-count candidacy now precedes ABSENT. A locally attached number plus a
result noun and ranking/listing intent resolves to VALID/OUT_OF_RANGE or, when
values compete, AMBIGUOUS; unrelated duration and certification quantities do
not participate. Generic certification counts remain review-visible while
separately attributable named credentials survive. Subject extraction is
relation-centered and removes grammatical recruitment/connective wrappers
without altering the original canonical text.

Ordinary search authorizes `with` as filter modality and can deterministically
execute top-K plus skill duration. The same request submitted through vacancy
analysis is identified as wrong-mode search intent rather than minting vacancy
modality. No scoring arithmetic, evaluator semantics, default Ollama model,
candidate-result conversation, migration, push, or merge is changed.

## D-065 — Result-count branch ordering, shared coordination-split authority, and orthography-independent family (issue #44)

**Date:** 2026-09-22. **Status:** Local corrective implementation on exact
independent-audit base `989569bc3789b2a0b3c46764c54e8f86c65909e4`; PR #42
remains open and unaccepted.

**F1 root cause:** `extract_result_count_intent`'s single-exact-candidate
branch returned `VALID` before its own ambiguous-cue safeguard could run, and
treated the AZ number word `bir` ("one") as an exact count candidate even when
it was the first word of the vague-quantity idiom `bir neçə`/`bir qədər`
("a few"/"some"). **Decision:** a number-word candidate immediately continued
by `neçə`/`qədər` is never counted as an exact value; a sentence expressing
result-count intent (a result noun plus a ranking/listing action) that
contains a vague quantifier (`bir neçə`, `bir qədər`, `çoxlu`, `few`,
`several`, `some`, `many`, `texminen`, `about`, `approximately`) with no
independently attributable exact number resolves to `AMBIGUOUS`, never
`VALID(1)`. Only true absence of any count-intent construction defaults to 20.

**F3 root cause:** `jd_authority.segment_requirement_spans` already
implemented D-055 (a same-sentence coordination splits only when every side
has its own explicit modality), but
`semantic_requirements.analyze_hr_text`'s independent `_split_units`
segmenter decided the same question by "is either side material by keyword,"
which could split a shared-modality clause with only one materially-cued
side and silently drop the other (`"<X> should hold ACCA and CFA
certifications"` lost ACCA entirely). **Decision:** the D-055 split rule is
extracted once into `meyar.agent.segmentation_authority
.coordinated_split_authorized`, used by both segmenters. A coordinated side
may split from its sibling only when it is independently material or an
already-attributed governing modality (a preceding `with`/`required`/
`preferred`/enumeration-list occurrence) covers it; otherwise the complete
clause is retained as one attributable material span. Nothing is silently
dropped; independently modalized sides (`"X required and Y preferred"`)
still split and both survive.

**F2 root cause:** `_family()`'s bare-`"<X> experience"` classification used
orthography (digits/symbols/ALL-CAPS/camelCase) as a `technical_identity`
proxy to decide `SKILL_EXPERIENCE` vs `DOMAIN_EXPERIENCE`, so structurally
identical sentences behaved differently purely by spelling (`Java` →
scorable `DOMAIN_EXPERIENCE`, `JavaScript` → review-only `SKILL_EXPERIENCE`).
**Decision:** family authority for a bare, duration-less `"<X> experience"`
now comes only from explicit sector/industry/domain grammar or a server-known
domain-taxonomy match (`_DOMAIN_HEADS`/`DOMAIN_SYNONYMS`, now including the
word `industry`); a subject with neither stays `SKILL_EXPERIENCE`, which
review-gates on duration exactly like any other unnamed skill claim. Known
domain words (e.g. `Banking`, `Risk`/`Credit Risk`) and known collision pairs
(`Java`/`JavaScript`, `C`/`C++`, `Go`/`Django`, `SQL`/`NoSQL`) now behave
consistently; a handful of previously orthography-authorized bare domain
words with no taxonomy/grammar signal (`Aviation`, `Hospitality`,
`Construction`, `Healthcare`, `Manufacturing`, bare `Background/Experience in
X`) now correctly fall to `NEEDS_HUMAN_REVIEW` instead of inconsistent
scoring.

**AZ wrapper:** a leading Azerbaijani postposition `ilə` ("with") was not
excluded from the professional subject when it began the remaining relation
text (`"ilə SQL"`), only when trailing. Added to the same leading
discourse-prefix strip already used for `and`/`but`/`while`/`amma`/`lakin`/
etc., in both the mask-based subject-bounds path and the
`_SUBJECT_SYNTAX_PATTERNS` discourse-prefix post-strip. No SQL-specific
logic was added.

**Explicit deferrals:** no scoring arithmetic, evaluator semantics, default
Ollama model, candidate-result conversation, migration, push, or merge is
changed. Certification-list enumeration splitting (`"including CPA and
CISA"`) is unchanged — it is a distinct, already-authorized shared-modality
list construction, not the bare coordination this fix targets.

## D-066 — `meyar-ops` foundation: manifests, preflight/status/readiness, release verification (issue #35 PR1)

**Date:** 2026-09-23. **Status:** Local implementation on
`feat/meyar-ops-foundation`, branched from accepted `main`
`dddd6eb0967bb1a9225fb43470c2468db30da5d6`; PR not yet merged.

**Scope.** First bounded PR for issue #35 (Slice 6 — Agentless Mac
Deployment Readiness, M9). Adds `backend/src/meyar/ops/` as normal typed/
tested/linted package code (never an ad-hoc shell script), with a
`meyar-ops` console entry point (`backend/pyproject.toml`
`[project.scripts]`) and four commands: `preflight`, `status`,
`readiness`, `verify-release`. Full detail: `docs/MEYAR_OPS.md`.

**Result contract.** One stable JSON envelope (`meyar.ops.result.OpsResult`)
for every command — `action`, `ok`, `started_at`, `finished_at`,
`findings[]`, each a typed `Finding` (`component`, `status`, `code`,
`message`). `ok` is defined uniformly across all four commands as "no
`Finding` has status `FAIL`" — `WARN`/`SKIPPED` never affect it. This was
chosen over a per-command-bespoke definition so a caller never has to
special-case which command it invoked to interpret `ok`/the exit code.
Exit codes are a 4-value `OpsExitCode` (`SUCCESS=0`, `CHECK_FAILURE=1`,
`INVALID_INVOCATION=2`, `INFRASTRUCTURE_FAILURE=3`); argparse's own
invalid-argument exit code is already `2`, so no special-casing was
needed there either.

**Ops config kept separate from application config.** `meyar.ops.config
.OpsSettings` (env prefix `MEYAR_OPS_`) is its own `pydantic-settings`
class, not an addition to `meyar.config.Settings` — meyar-ops is
operationally separate tooling (per `.claude/rules/architecture.md`), and
keeping its handful of settings (disk-space minimum, an alembic.ini
override) out of the widely-imported application `Settings` avoids
coupling business-config surface area to deployment-tooling surface area.

**Embedding provider gained `health()`.** `meyar.embedding.provider
.EmbeddingProvider` and `OllamaEmbeddingProvider` gained a `health()`
method mirroring the existing `LLMProvider.health()` (same `/api/tags`
reachability/model-availability check, same boundary). This was necessary
so `meyar-ops status`/`readiness` can report configured-embedding-model
availability through the existing approved local-only boundary
(`meyar.llm.loopback.build_local_only_async_client` +
`require_loopback_url`) instead of ops code constructing its own Ollama
HTTP call — see the static `test_ops_package_never_imports_httpx_directly`
proof in `test_ops_no_exfiltration.py`. `tests/fakes.FakeEmbeddingProvider`
and `demo_seed_service._DemoEmbeddingProvider` both gained a matching
`health()` so they still satisfy the `EmbeddingProvider` protocol under
mypy.

**Release-identity uses `pyproject.toml`'s `[project].version`
(currently `0.1.0`), never the FastAPI/OpenAPI display version
(`meyar.main.app.version`, currently `1.0.0`).** These two numbers are
already inconsistent in the repository today; this PR does not change
either value or attempt to reconcile them — that is a separate, later
decision if ever made. `release_id` is deterministic:
`meyar-{release_version}+{source_sha[:12]}` from the full 40-character
`source_sha`, never randomly generated, so re-verifying the same accepted
commit always yields the same identity.

**Release-artifact integrity model.** Tarball + release manifest +
external `SHA256SUMS` — the artifact's own SHA-256 is never computed over
bytes that are themselves inside that same artifact (no self-referential
hashing). The release manifest *may* also be embedded inside the archive
(for installed-state identity once deployed); `verify-release` treats
that as an optional additional consistency check against the external
manifest, never as the source of truth for the checksum. Archive member
metadata (`meyar.ops.archive_safety`) is inspected for absolute paths,
`..` traversal, symlinks, hard links, device/special files, and paths
escaping the one expected top-level root (`release_id/`) — a violation is
a hard failure and the archive is never extracted (`extractall`/`extract`
are never called anywhere in this PR; `verify-release` only ever reads
one designated member's bytes into memory via `extractfile`, and only
after every member has already passed the safety inspection with zero
violations).

**No production model approved; no artifact built.** `meyar.ops
.model_manifest` defines the three-state approval contract
(`DEVELOPMENT_INTEGRATION` / `BENCHMARKED_PENDING_APPROVAL` /
`PRODUCTION_APPROVED`) but this PR ships no instance anywhere in the repo
with `PRODUCTION_APPROVED` — current truth stays `qwen3:0.6b` (source
default) and `qwen3:1.7b` (recent local acceptance override), both
`DEVELOPMENT_INTEGRATION`; production selection remains TBD, blocked on
issue #36's real Target-Mac benchmark. Release-artifact *building* is
explicitly deferred to a later #35 PR — this PR's `verify-release` only
verifies synthetic fixtures.

**#35/#46 boundary preserved.** No HTTP `/ready` route was added; `/api/v1
/health` is untouched (liveness-only). `meyar-ops readiness` is a local
CLI composition of existing primitives, not an authenticated HTTP
endpoint — that, plus in-application degraded-state semantics and
production config fail-closed hardening, remains issue #46.

**Corrective hardening (pre-merge independent review, same branch/PR).**
Five defects found in independent review of the above before merge, fixed
in one follow-up commit, still #35 PR1 scope — no new command, no #36/#46
work pulled forward:

1. `internal_manifest_consistency` compared only `release_id`/
   `release_version`/`source_sha`, so an embedded manifest could disagree
   on `uv_lock_sha256`, `alembic_heads`, `rollback_compatibility`,
   `model_manifest`, `required_python_version`, or `artifact_format`(`_version`)
   and still report `INTERNAL_MANIFEST_CONSISTENT`. Now compares the full
   typed `ReleaseManifest` (structured field equality, not raw JSON
   text/order) — any field mismatch is `INTERNAL_MANIFEST_MISMATCH`.
2. `uv_lock_sha256` was only syntactically validated (64 hex chars) and
   never checked against anything — a "valid" artifact could omit
   `backend/uv.lock` entirely. `verify-release` now locates the exact
   `release_id/backend/uv.lock` member (missing/duplicate/non-regular is
   a hard failure), streams its SHA-256 in bounded chunks, and compares
   it to the manifest's declared digest.
3. `probe_storage_writable` called `mkdir(parents=True, exist_ok=True)`,
   so a readiness *check* silently provisioned the storage root. It now
   only reports truthfully on an already-provisioned root (missing root
   or non-directory ⇒ not writable) and never creates anything —
   provisioning remains a later #35 step.
4. `get_db_alembic_revision` caught every `SQLAlchemyError` from the
   revision query and returned `None`, conflating "table never migrated"
   with a genuine query/permission failure, and read only `.first()`.
   It now does a PostgreSQL-specific table-existence probe first, reads
   every revision row, and raises a distinct `AlembicRevisionQueryError`
   for a post-connection query failure so `status`/`readiness` can report
   it truthfully (`ALEMBIC_REVISION_QUERY_FAILED`) instead of as "no
   revision", and report more-than-one DB revision row as its own
   `MULTIPLE_DB_REVISIONS` instead of silently using the first.
5. Archive inspection was otherwise unbounded (`TarFile.getmembers()`
   eagerly parses every header; `SHA256SUMS` duplicate filenames resolved
   last-write-wins). `archive_safety.inspect_archive_members` now scans
   via `TarFile.next()` and aborts the instant member count, member-name
   length, or aggregate declared uncompressed size exceeds a documented
   bound; the two members `verify-release` reads by name each have their
   own declared-size bound checked before any read; and a duplicate
   `SHA256SUMS` entry for the target artifact filename is rejected as
   `CHECKSUM_ENTRY_AMBIGUOUS` rather than silently resolved.

Also added, same review: `ModelManifestEntry` now requires a non-empty
`benchmark_reference` for `BENCHMARKED_PENDING_APPROVAL`/
`PRODUCTION_APPROVED` (schema-level only — no stronger cross-field rule
invented; still ships no `PRODUCTION_APPROVED` instance, still #36's
call). No behavior changed for candidate/search/scoring, no HTTP `/ready`
was added, no launchd/update/rollback orchestration was added, and no
production model was approved.

**Explicit deferrals:** no candidate/search/scoring semantics changed, no
launchd/service lifecycle, no update/rollback orchestration, no Apple
Silicon runtime claim (Linux-verified only in this PR), no bank-Mac
verification claim, no PostgreSQL/Homebrew/Docker production provisioning
topology chosen.

## D-067 — macOS LaunchDaemon service foundation: render/verify/status only (issue #35 PR2)

**Date:** 2026-09-23. **Status:** Local implementation on
`feat/macos-launchd-service-foundation`, branched from accepted `main`
`e868632cca457d69c99e8454d0cc35f85023dae1`; subsequently merged
as PR #54. Its original path policy below is historical and superseded
by D-073/D-074.

**Scope.** Second bounded PR for issue #35 (Slice 6 — Agentless Mac
Deployment Readiness, M9), following PR1 (#52, D-066). Adds three
`meyar-ops` commands — `service-render`, `service-verify`,
`service-status` — that render, verify, and read-only-probe a macOS
system LaunchDaemon plist for the MEYAR application process. Full detail:
`docs/MEYAR_OPS.md`.

**Accepted target service model (not yet installed by any command):**
system LaunchDaemon -> dedicated non-root `UserName` -> MEYAR application
process -> loopback-bound Uvicorn. PostgreSQL and Ollama remain local
external dependencies, unmanaged by this PR. There is still no accepted
immutable release/install filesystem layout — this PR deliberately does
not invent one (`WorkingDirectory`/`Executable`/log paths are fully
operator-supplied, lexically validated only, symlink components are not
blanket-rejected).

**Deliberately narrow — no privileged operation.** `service-render` only
writes plist bytes to an explicit operator-supplied output path (never
`/Library/LaunchDaemons`, never with overwrite, never `launchctl`, never
`sudo`). `service-verify` only reads and validates an existing plist file.
`service-status` only runs a fixed-argv, read-only `launchctl print
system/<label>` probe — no `bootstrap`/`bootout`/`kickstart`, no mutation,
no `sudo`. `service-install`/`start`/`stop`/`restart`, service-account
creation, and PostgreSQL/Ollama lifecycle are explicitly out of scope and
not implemented.

**Plist key set is narrow and deterministic.** Exactly seven keys —
`Label`, `UserName`, `WorkingDirectory`, `ProgramArguments`,
`StandardOutPath`, `StandardErrorPath`, `KeepAlive`
(`{"SuccessfulExit": false}`) — via `meyar.ops.service_plist
.REQUIRED_PLIST_KEYS`, `plistlib.dumps(..., sort_keys=True)` for
determinism. No `EnvironmentVariables` (secrets stay outside the plist in
host-local configuration — enforced by both a general unsupported-key
check and a dedicated `no_environment_variables` finding), no explicit
`RunAtLoad` (`KeepAlive.SuccessfulExit=false` already supplies
restart-after-abnormal-exit semantics), no `ThrottleInterval` (would only
restate launchd's own default). `ProgramArguments` is always a real argv
array — `[executable, "-m", "uvicorn", "meyar.main:app", "--host",
"127.0.0.1", "--port", str(port)]` — never a `Program` shell-string, and
`executable` is a required absolute path so the daemon never depends on
bare `uv`/PATH resolution at launch time. `service-verify` rejects any
plist key outside this set as `UNSUPPORTED_KEY`, never silently accepting
an unexpected/dangerous key (e.g. a tampered `EnvironmentVariables` or
`RunAtLoad` re-injected into an already-rendered plist).

**Path validation is lexical only, on purpose.** `WorkingDirectory`,
`Executable`, `StandardOutPath`, `StandardErrorPath` are each validated
for: required absolute, no NUL/control characters, no lexical `..`
traversal component. None of these calls `Path.resolve()`/
`os.path.realpath()` — there is no final immutable-release-path contract
yet (per the open #35 issue's own acceptance addendum), so a path
containing a symlinked component is not blanket-rejected here; that
containment decision is deferred to whichever future PR actually defines
the release/install layout.

**`service-render` no-overwrite is atomic, not a pre-check.** The output
file is created with `os.O_CREAT | os.O_EXCL`, never a `Path.exists()`
check followed by a separate write (which would be a TOCTOU race).
Validation of the `ServiceSpec` happens before any filesystem call, so
invalid input never creates or touches the output path; any failure
during the write removes the partial file it created, so no partial file
is ever left behind.

**`service-verify` bounded read mirrors `verify-release`'s pattern.**
`meyar.ops.service_plist._read_bounded_plist_bytes` never requests more
than `limit + 1` bytes from the open file handle (1 MiB bound — a real
service plist is a few hundred bytes), so an oversized or hostile file is
rejected as `PLIST_TOO_LARGE` before any XML parsing is attempted; the
bound is enforced by the read call itself, never by a `stat()` taken
beforehand.

**`service-status` is read-only, fixed-argv, macOS-only, and never
installs a coding-agent-free path around itself.** `meyar.ops
.service_status.default_launchctl_runner` calls `subprocess.run` with a
fixed `[/bin/launchctl, "print", f"system/{label}"]` argv — never
`shell=True`, never a bare `launchctl` relying on `PATH`, never `sudo`.
The runner is injected (`LaunchctlRunner` protocol) so every test uses a
fake — no test claims real macOS/`launchctl` acceptance. On any
non-Darwin `platform.system()` (all current CI), it returns a truthful
`PLATFORM_UNSUPPORTED` finding without invoking any runner — it never
pretends the check ran, and never requires `launchctl` to exist on Linux
CI. Raw `launchctl` stdout/stderr is never included in the returned
`OpsResult` — only the fixed argv, the process exit status, and (on the
rare runner-level infrastructure failure) bounded/redacted error text via
the existing `meyar.ops.redact.safe_exception_text`.

**Real macOS/Apple-Silicon acceptance remains UNCONFIRMED by this PR.**
All new tests (`test_ops_service_plist.py`, `test_ops_service_status.py`)
run and pass on Linux only, using synthetic fixtures and an injected fake
`launchctl` runner. No claim is made that a rendered plist has been
loaded by a real `launchd`, that `launchctl print` output has been
observed on real hardware, or that the service survives a real reboot —
that rehearsal remains issue #35's later scope (plist *installation* and
lifecycle) plus the Apple Silicon rehearsal called out in #35's
acceptance criteria and issue #36.

**Explicit deferrals (same as the issue's own "DO NOT implement" list):**
`service-install`/`start`/`stop`/`restart`, `sudo`/privilege escalation,
writing to `/Library/LaunchDaemons`, `launchctl bootstrap`/`bootout`/
`kickstart`, service-account creation, PostgreSQL/Ollama provisioning or
lifecycle, release-artifact build/install/update/rollback, HTTP `/ready`
(#46), issue #46 runtime hardening, issue #36 benchmark/model selection,
production-model approval, HTTPS/reverse-proxy/PKI topology.

## D-068 — Immutable application release artifact builder: `build-release` (issue #35 PR3)

**Date:** 2026-09-24. **Status:** Local implementation on
`feat/release-artifact-builder`, branched from accepted `main`
`233af33debe47731a2665afd4eccabf2fd5523a0`; PR not yet merged.

**Scope.** Third bounded PR for issue #35 (Slice 6 — Agentless Mac
Deployment Readiness, M9), following PR1 (#52, D-066) and PR2 (#54,
D-067). Adds one `meyar-ops` command — `build-release` — that builds an
immutable, verifiable MEYAR **application** release artifact from an
exact Git commit. Full detail: `docs/MEYAR_OPS.md`.

**Application release artifact vs. complete deployment bundle — read
this distinction before treating this PR as "deployment done."** The
artifact this PR produces contains MEYAR application source
(`backend/src/meyar/**`), migration code (`backend/alembic.ini` +
`backend/alembic/**`), `backend/pyproject.toml`/`backend/uv.lock`, and
release identity/integrity metadata (`ReleaseManifest`). It does **not**
contain a Python runtime, the `uv` executable, third-party wheels or an
offline wheelhouse, Ollama, models, or PostgreSQL, and it defines no host
install layout, extraction mechanism, or activation/service-lifecycle
action. A later #35 slice must deliver the accepted offline/pinned
Apple-Silicon runtime/dependency provisioning mechanism, and a further
slice the install-layout/activation contract, before this line of work is
a complete offline bank-Mac deployment bundle. Issue #35 remains OPEN
after this PR.

**Exact Git commit provenance — the whole point of this PR.** Every byte
of the produced artifact, and every source-derived `ReleaseManifest`
field, is read from the selected commit's Git tree object via fixed-argv
Git plumbing (`git rev-parse --show-toplevel`, `git cat-file -e
<sha>^{commit}`, `git ls-tree -r -z --full-tree`, `git cat-file -p
<blob-sha>` — `meyar.ops.build_release.default_git_runner`, always
`subprocess.run` with a fixed argv, never `shell=True`, never a shell
command string) — never from the mutable working tree. A dirty/modified
tracked file and an untracked file living under an allowed-looking source
directory both provably do not affect the artifact
(`test_dirty_tracked_file_modification_does_not_affect_artifact`,
`test_untracked_allowlist_shaped_file_does_not_enter_artifact`). A short,
non-hex, nonexistent, or non-commit (blob/tree) `--source-sha` is
rejected as a truthful `FAIL` finding — never silently defaulted to
`HEAD`. `git archive` was deliberately not used as the sole mechanism:
per the issue's own guidance, MEYAR code (not a shelled-out archive
command) constructs the final member allowlist and metadata contract so
both remain explicit and independently testable; Git plumbing here is
used only as the exact-commit content-retrieval primitive.

**Allowlist-driven, not "tar everything and exclude bad things."**
Exactly five pathspecs are ever passed to Git
(`meyar.ops.build_release.ALLOWED_PATHSPECS`): `backend/src/meyar`,
`backend/pyproject.toml`, `backend/uv.lock`, `backend/alembic.ini`,
`backend/alembic`. `backend/tests/`, `backend/scripts/`, `.env`, `.git/`,
real CVs/customer data, caches, and repository-level developer-agent
metadata are never in that pathspec list, so they never enter the
picture regardless of what else exists at the selected commit. Every
listed tree entry is independently re-checked for shape: a Git symlink
entry (`mode 120000`) or a submodule entry (object type `commit`) aborts
the whole build (`UNSAFE_GIT_TREE_ENTRY`) before any byte is written,
and a lexical `..`/absolute-path escape is rejected the same way as
defense-in-depth, even though Git's own pathspec resolution would not
normally produce one.

**Alembic heads are computed from the selected commit's own migration
tree, reusing existing code unmodified.** `build_release` materializes
only the selected commit's `backend/alembic.ini` + `backend/alembic/**`
blobs into a throwaway temp directory (Alembic's `%(here)s` token in
`script_location` resolves relative to the ini file's own directory, so
this works from any temp location) and calls the existing
`meyar.ops.alembic_introspect.get_code_alembic_heads` against it — the
same head-computation `status`/`readiness` already use, never
reimplemented, and never pointed at the working tree's copies.

**Archive layout: a single top-level `<release_id>/` root, embedded
manifest mandatory.** `release_manifest.json` sits at the archive root
alongside `backend/`; the embedded manifest is mandatory for a
`build-release`-produced artifact, while `verify-release`'s own
compatibility semantics for a missing embedded manifest (`SKIPPED`, for
other/legacy artifacts) are deliberately left unchanged.

**Deterministic content selection; explicitly NOT byte-for-byte
reproducibility.** The exact same `--source-sha` always yields the exact
same allowlisted file set and bytes, and archive-internal metadata is
normalized independent of build time — fixed lexical member order, fixed
`uid=0`/`gid=0`/empty `uname`/`gname`/mode `0o644`/`mtime=0` on every
member, and a normalized gzip wrapper (fixed header `mtime`, no embedded
filename). `built_at` is, on purpose, a real build timestamp — two builds
of the same commit at different real times legitimately differ in
`built_at` and therefore in the embedded/external manifest bytes and both
checksums (proven by
`test_two_real_time_builds_of_the_same_commit_differ_in_built_at`, and a
fixed-clock-injected test proves the deterministic-content-selection half
separately). `ReleaseManifest`'s existing embedded/external full-equality
contract is unchanged; this PR does not change `manifest_schema_version`
or equality semantics to chase byte identity.

**Output-write safety mirrors the accepted `service-render` pattern
(D-067).** Each of the three release-bundle files
(`<release_id>.tar.gz`, `<release_id>.release-manifest.json`,
`SHA256SUMS`) is created with `os.O_CREAT | os.O_EXCL` relative to a
dir-fd anchored to `--output-dir` — never a `Path.exists()` pre-check, so
no overwrite race. An existing target aborts the build
(`OUTPUT_TARGET_EXISTS`) before anything is replaced and is never
touched. A failure after one or more of the three files has already been
created removes only the file(s) *this invocation itself created*,
identity-checked via `(st_dev, st_ino)` — mirroring
`service_plist._unlink_if_same_file` — never a pre-existing file and
never a file a concurrent actor has since swapped in at the same path
(unit-proved directly against the low-level helper via `os.replace`,
independent of any timing assumption about inode reuse). `--output-dir`
must already exist and be a directory; no parent directory tree is ever
created.

**Self-check before publishing — the produced artifact must pass
`verify-release` unmodified.** Immediately after writing the artifact,
`build-release` runs the exact same `meyar.ops.archive_safety
.inspect_archive_members` bounded safety inspection `verify-release`
performs; a violation or resource-bound excess removes the just-written
artifact and fails the build before the manifest/`SHA256SUMS` files are
ever created. `verify-release` itself is unmodified by this PR — the
strongest acceptance test
(`test_build_release_output_passes_verify_release`) round-trips a built
release bundle through the real, unmodified `verify_release()` and
asserts `ok=True` with zero `FAIL` findings.

**Model/rollback governance stays declarative — no new production
approval.** `--rollback-compatibility` and `--model-approval-status` are
required CLI inputs; the builder records whichever enum value is
supplied and performs no rollback/backup/restore/Alembic-downgrade
action, no benchmark, and no approval inference. This PR ships no
`ModelManifest`/`ReleaseManifest` fixture or example marked
`PRODUCTION_APPROVED`, invents no target-Mac benchmark reference, and
does not change issue #36 governance. There is no `--release-version`
CLI override — the release-version authority remains the selected
commit's `backend/pyproject.toml` `[project].version`, matching D-066's
existing `compute_release_id` contract unchanged.

**Real macOS/Apple-Silicon acceptance remains UNCONFIRMED by this PR.**
`test_ops_build_release.py` and the extended `test_ops_cli.py`/
`test_ops_no_exfiltration.py` tests all run and pass on Linux only,
against real throwaway Git repositories created under `tmp_path` (never
synthetic in-memory tar fixtures alone, and never real MEYAR repository
history) — the one CLI success test that exercises this actual
repository does so read-only, against its own real HEAD commit, and
writes only into a pytest `tmp_path`. No claim is made about build
performance or behavior on Apple Silicon.

**Explicit deferrals (same as the issue's own "DO NOT implement" list):**
offline Python runtime/`uv`/wheelhouse provisioning, host install layout,
install/activation, service lifecycle, update/rollback orchestration,
issue #36 benchmark/model selection, production-model approval, issue
#46 runtime hardening, HTTPS/reverse-proxy/PKI topology.

**Corrective update (PR #56 acceptance pass, 2026-09-24).** An
independent acceptance audit of the PR3 diff above found three blockers
in `build_release`'s selected-commit handling, fixed on the same branch
without broadening PR3's scope:

1. **Selected migration Python was executed, not just read.** The
   original Alembic-heads step (described two sections up) materialized
   the selected commit's `backend/alembic.ini` + `backend/alembic/**`
   blobs into a temp directory and called the real, unmodified
   `alembic_introspect.get_code_alembic_heads`, which loads every
   migration `.py` file as a Python module via Alembic's own
   `ScriptDirectory` — executing its top-level code. That is correct for
   `status`/`readiness`, which point it at the trusted, already-running
   working tree, but wrong for `build-release`, whose whole contract is
   to READ an arbitrary selected commit's metadata, never EXECUTE it.
   Fixed by a new module, `meyar.ops.alembic_static_metadata`, that
   parses each `backend/alembic/versions/*.py` blob with `ast` and
   extracts only the static `revision`/`down_revision` literal
   assignments (via `ast.literal_eval`, which rejects calls, attribute
   lookups, name references, f-strings, and computed expressions) to
   derive heads from the resulting revision graph — no `exec`, `eval`,
   `importlib`, `runpy`, or Python subprocess involved anywhere.
   `get_code_alembic_heads` itself is unchanged and remains correctly
   used by `status`/`readiness`/`preflight`.
   Proven by `test_malicious_migration_top_level_side_effect_is_never_
   executed`, which ships a migration file whose import-time side effect
   would write a sentinel file — the build succeeds (its static metadata
   is valid) and the sentinel is never created.

2. **`release_version` (attacker-controlled selected-commit content) had
   no filesystem-safety validation before reaching output filenames.**
   `release_version` comes from the selected commit's `pyproject.toml`,
   which is not more trustworthy than any other blob at an operator-
   selected commit; `release_id`/output basenames were built from it and
   passed straight to `os.open(..., dir_fd=output_dir_fd)` with no check
   for `/`, `\`, control characters, or `.`/`..`. Fixed with a build-
   release-specific validator, `_validate_filesystem_safe_component`,
   applied to both `release_version` and the derived `release_id` before
   either can reach an output filename or archive member root; an unsafe
   value fails the build truthfully (`RELEASE_VERSION_UNSAFE`/
   `RELEASE_ID_UNSAFE`) rather than being silently normalized.
   `compute_release_id()`'s own semantics and `ReleaseManifest`'s
   existing length constraints are unchanged.

3. **`_create_exclusive`'s raw fd could leak if `os.fdopen()` itself
   failed.** The original code took no explicit ownership stance on the
   fd returned by `os.open()` before it was wrapped by `os.fdopen()`; a
   failure in `os.fdopen()` itself (before ownership transfer to the
   resulting file object) left the raw fd unclosed. Fixed by explicit
   ownership discipline: `_create_exclusive` owns the raw fd until
   `os.fdopen()` succeeds, closes it directly (suppressing a possible
   redundant-close `OSError`) on any failure up to and including
   `os.fdopen()` itself, and only then runs the existing identity-checked
   path cleanup — never masking the original exception.

**Resource-safety review (same pass).** Selected-commit blob content was
read via `git cat-file -p` (`subprocess.run(capture_output=True)`, which
buffers all of stdout) with no bound of its own — a pathologically large
allowlisted blob could be fully read into memory before the existing
archive self-check ever ran. Fixed with the smallest defensible
check: `git cat-file -s <blob-sha>` reads each blob's declared size
first, and a running aggregate is compared against the already-accepted
`archive_safety.MAX_AGGREGATE_UNCOMPRESSED_SIZE` bound before that blob's
content is fetched — so the check happens strictly before the
oversized/aggregate-exceeding blob's bytes are ever requested from Git.
No existing verifier bound was weakened.

All three fixes and the resource-safety addition ship with focused
regression tests in `test_ops_build_release.py` (16 new tests: 7 for
non-execution, 5 for release-identity path safety, 3 for fd-ownership
failure handling, 1 for the blob-size bound); the full backend suite
(1,883 tests), `ruff`, `mypy`, and `alembic heads` all pass unchanged.
PR #56 remains open for re-audit; issue #36 and issue #46 remain
untouched and unstarted.

**Final resource-safety corrective pass (independent re-audit, same PR
#56 branch, 2026-09-24).** A second independent audit of the corrected
diff found three narrower remaining gaps, fixed on the same branch
without broadening PR3's scope:

1. **Only the selected-blob aggregate size was pre-checked; member count
   and final member-name length were only ever discovered by the
   post-build self-check.** The builder could still construct and write a
   full artifact it already knew, deterministically, the existing
   verifier (`archive_safety.MAX_ARCHIVE_MEMBER_COUNT`/
   `MAX_MEMBER_NAME_LENGTH`) would reject. Fixed with three prechecks, run
   as early as the needed inputs allow and always before
   `_build_tar_gz_bytes`: **member count**
   (`member_count_bound`/`MEMBER_COUNT_EXCEEDS_BOUND`) — selected entries
   plus the one mandatory embedded manifest, checked immediately after the
   allowlisted tree listing and before a single blob is fetched; **member
   name length** (`member_name_length_bound`/
   `MEMBER_NAME_LENGTH_EXCEEDS_BOUND`) — the *final* `<release_id>/<path>`
   name, checked once `release_id` is known; **aggregate uncompressed
   size** (`aggregate_size_bound`/`AGGREGATE_SIZE_EXCEEDS_BOUND`) —
   selected source blobs' Git-declared size (`_fetch_all_blobs` now
   returns this total for reuse, rather than it being recomputed) plus the
   embedded manifest's own bytes, checked once the manifest is built. The
   post-build `inspect_archive_members` self-check remains as
   defense-in-depth; no verifier bound was weakened.

2. **`_create_exclusive`'s `os.fstat(fd)` failure path left an untracked
   created file.** If `os.fstat(fd)` itself failed right after a
   successful `O_CREAT | O_EXCL` create, the raw fd was correctly closed,
   but the on-disk path was never identity-checked-cleaned (no
   `(st_dev, st_ino)` was ever captured) and `_CreatedFile` was never
   returned, so the outer `build_release` cleanup never learned about it.
   The narrowest defensible fix was chosen: fail-**closed**, not
   guess-and-delete. A new `OutputIdentityUnavailableError` is raised;
   `build_release` reports it as `OUTPUT_IDENTITY_UNAVAILABLE`, cleans up
   only any *other* already-identity-tracked created files via the
   existing `_cleanup_all()`, and deliberately leaves the
   identity-unverifiable file in place for operator inspection rather than
   risk deleting a concurrent actor's replacement at the same basename.
   Every other `_create_exclusive` failure point (post-`fstat`) already
   holds `created_stat` and continues to clean up exactly as before — this
   changes behavior at exactly one narrow point, not the module's general
   cleanup discipline.

3. **Head computation over the static `down_revision` graph did not prove
   acyclicity.** "Heads = revisions not referenced as a parent" silently
   *loses* a cyclic pair (`A.down_revision = B`, `B.down_revision = A`) —
   both ends are mutually "referenced" and simply vanish from the result,
   while an independent, valid branch's head is still reported as if
   nothing were wrong; real Alembic rejects a cyclic graph outright. Fixed
   with `alembic_static_metadata._detect_down_revision_cycle`, a
   deterministic white/gray/black DFS over the already-parsed
   `down_revision` edges, run after parent-reference validation and before
   head computation — still zero migration-code execution/import. Rejects
   a self-cycle, a two-node cycle, and a longer cycle, even alongside an
   independent valid branch; a normal linear/branch/merge graph (including
   tuple/list `down_revision` merge revisions) is unaffected.

One existing test (`test_archive_bound_exceeded_during_self_check_fails_
and_cleans_up`) was updated in place rather than left to assert stale
behavior: the same `MAX_ARCHIVE_MEMBER_COUNT` scenario it exercised is now
caught by the `member_count_bound` precheck, not the post-build
self-check, so its expected component/code changed accordingly (renamed
`test_archive_bound_exceeded_is_now_caught_by_precheck_before_self_
check`). 12 new regression tests were added to `test_ops_build_release.py`
(6 for the three resource-bound prechecks — one at-limit-succeeds and one
one-over-fails-before-construction test per bound; 2 for the
`os.fstat` identity-unavailable fail-closed behavior, one direct unit
test and one full `build_release()` integration test; 4 for Alembic
`down_revision` cycle detection — self-cycle, two-node cycle alongside an
independent valid branch, a longer cycle, and a normal branch/merge graph
proving no false positive). Full gate: `ruff check .` clean, `mypy src`
clean (163 source files), `alembic heads` unchanged (one real head,
non-executing static parse matches), `scripts/scan-tracked-tree.sh`
clean, `git diff --check` clean, focused `test_ops_build_release.py` +
`test_ops_cli.py` + `test_ops_no_exfiltration.py` (81 tests) green, full
backend suite (1,895 tests) green. PR #56 remains open for re-audit;
issue #36 and issue #46 remain untouched and unstarted; no merge, no
force push, no history rewrite performed.

## D-069 — One-command local demo bootstrap: `demo-up.sh`/`demo-down.sh`

**Date:** 2026-09-24
**Decision:** Chore-level operator-UX work (presentation convenience, not a
Slice/issue-#35 deployment artifact, not #36, not #46) wrapping the
already-accepted manual local-demo flow (`docs/LOCAL_DEMO.md`, issue #25,
D-022) behind two scripts:
1. **`scripts/demo-up.sh`** sequences, and fails closed at, the exact
   existing manual steps: prerequisite checks (git/docker/uv/curl,
   `docker info`, `docker compose version`; no auto-install, no sudo) →
   `backend/.env` bootstrap only if missing (an existing file, including
   owner customization, is never touched or overwritten) → `uv sync
   --locked` → `docker compose up -d postgres` with a bounded
   `docker compose ps --format '{{.Health}}'` poll (default 60s; no fixed
   `sleep` substitute) → `alembic upgrade head` + an `alembic heads`
   single-head check → the existing, unmodified `uv run meyar seed-demo`
   (rotates both credentials every run, as already documented) → a
   best-effort, read-only `ollama list` status check (never a hard
   prerequisite, never auto-pulls/starts anything) → a loopback-only
   (`127.0.0.1`, no `--reload`) Uvicorn child process, health-polled via
   `/api/v1/health` before printing a concise ready banner. Any step
   failing stops the sequence before the next one runs (e.g. a migration
   failure never reaches seeding or server start).
2. **No backend/product behavior changed.** `meyar seed-demo` itself is
   untouched — the wrapper only captures its stdout in a shell variable
   (never a file) to extract just the human `demo.hr` username/password
   for a concise banner; if that extraction ever fails for any reason, it
   falls back to printing `seed-demo`'s full authoritative credential
   block verbatim rather than guessing at a shorter summary. The API key
   secret is never repeated in the wrapper's own banner.
3. **Port-conflict handling never guesses or kills.** If `127.0.0.1:8000`
   already answers `/api/v1/health` successfully, the existing process is
   reused (no duplicate server). If the port is held by anything else,
   `demo-up.sh` fails with a clear message and touches nothing.
4. **Signal handling targets only the PID this invocation started.** The
   Uvicorn child is launched via `(cd backend && exec uv run uvicorn ...) &`
   so `$!` tracks the exact process through both `exec` hops; Ctrl+C
   sends `SIGTERM` to only that PID and waits for it — no `pkill`,
   `killall`, or process-group-wide signal. `docker`/Postgres are left
   running on exit (not stopped automatically) since demo data is meant
   to persist between sessions.
5. **`scripts/demo-down.sh` is deliberately narrow:** `docker compose
   stop postgres` only — no `-v`, no volume/database reset, no `.env`
   deletion, no process killing by pattern. If MEYAR still appears to be
   answering on port 8000, it reports that truthfully rather than
   guessing which host process to stop (a foreground `demo-up.sh` session
   already owns its own child directly; nothing else can identify it
   safely).
6. **Test harness (`scripts/tests/test_demo_scripts.py`):** a standalone
   Python `unittest` harness (not wired into backend `pytest`, whose
   `testpaths` is scoped to `backend/tests`) stubs `docker`/`uv`/`ollama`
   via an isolated fake `PATH` entry — no real Docker daemon, Postgres, or
   Python backend is touched — while using real `git`/`curl`/`bash`/
   coreutils. Covers: env-file create-only-if-missing/never-overwritten,
   missing-prerequisite failure, Postgres-unhealthy timeout blocking
   migration, migration/multi-head/seed failures each blocking every
   later step, exactly-once credential printing with the API key never
   repeated, Ollama-unavailable-is-warning-only (and available-is-
   reported), both port-conflict branches (unrelated process left
   untouched; already-healthy MEYAR reused), Ctrl+C cleanup targeting
   only the owned PID while an unrelated process survives, no secret ever
   written to a file by the wrapper, and static-content checks (no
   `sudo`/`eval`/`pkill`/`killall`/`compose down`/`0.0.0.0`/`--reload`/
   `--reset`). One real timing bug was found and fixed *in the test
   harness itself* during this work, not in the scripts: an external
   TCP/HTTP probe from the test process could observe the Uvicorn stub as
   healthy a moment before `demo-up.sh`'s own internal readiness loop
   completed its own check; sending Ctrl+C in that narrow window raced
   the script's own cleanup trap against its own next poll iteration and
   surfaced a confusing (but still safe — the process was still correctly
   stopped, nothing leaked) "exited before becoming healthy" message.
   Fixed by having the harness wait for the script's own literal
   "MEYAR LOCAL DEMO READY" banner (steady state) before signaling,
   rather than racing a side-channel probe against it.
**Why:** presentation-readiness operator UX only — too many manual
commands were required before a demo. **Reversibility:** fully additive
(two new scripts, one new test file, `docs/LOCAL_DEMO.md`/`README.md`
pointer updates); no schema, API, or existing-command behavior changed.

## D-070 — Candidate photo as identity-only presentation data (issue #60)

Photo processing is a best-effort stage after a CandidateDocument commits.
The original CV ingestion policy is unchanged. A disposable local worker
uses bounded DOCX body-media relationship inspection or first-two-page
pypdf XObject inspection plus Pillow; only one plausible static JPEG/PNG
is accepted, sanitized, and re-encoded as a deterministic JPEG. Ambiguous,
unsafe, missing, and failed results are immutable terminal
`CandidatePhotoVersion` rows. The photo-only `photo/<tenant>/<opaque-id>`
namespace is under the existing backed-up storage root, and the photo
row has an exact tenant/candidate/document composite FK. The UI route
checks the current authorized identity on the newest stored document
(`created_at DESC, id DESC`) and the photo result for that document and
extractor version. It never falls back to an older photo. No photo value
enters professional facts, embeddings, search, planning, scoring,
ranking, or LLM/VLM input. The nine demo portraits are locally generated
geometric illustrations embedded in fresh synthetic DOCX files.

Pillow is the only new image dependency. Issue #35 owns offline Apple
Silicon wheel bundling; macOS worker memory behavior remains a deployment
validation item. Deterministic shape heuristics cannot prove human
identity: a large portrait-shaped logo is a residual presentation risk.

Hard-delete compensation snapshots the exact bytes of every present
AVAILABLE photo before deleting files. A pre-existing missing photo is
recorded as absent, and a readable hash-mismatched photo is retained as
the same corrupt bytes if deletion later fails; neither blocks successful
candidate deletion. This currently holds all present photo bytes in
memory at once. There is no independently verified bound on the number
of candidate documents or photo versions, so aggregate memory use during
deletion remains a P2 resource concern for #46 runtime hardening. No
arbitrary deletion-blocking count or byte threshold is introduced here.

## D-071 — Search match fact positions for truthful UI evidence (issue #62)

Structured search records bounded positions of the exact authorized profile
facts behind a matched skill-duration or language-level filter in the typed
`RequiredFilterMatch`/`PreferredFilterMatch`. Search still uses the same
criterion evaluator and explicit `as_of_date`; the positions add presentation
provenance only. The UI resolves those positions against the same authorized
profile version and displays only each matched fact's own evidence. Missing or
invalid positions yield no evidence, never a whole-profile fallback. Bare
skill/language filters retain their existing presentation behavior. Search
request, plan, eligibility, ranking, scores, embeddings, and Agent result
membership remain unchanged. The UI's bounded snippet and dedup rules still
apply, and the Agent card uses the same search result view as direct Search.

The current language evaluator is order-sensitive when duplicate same-name
facts disagree: its first same-name fact determines eligibility. Issue #63
tracks that separate search-semantic change; #62 preserves membership and
attributes evidence only to the fact that actually matched today.

## D-072 — Order-independent duplicate language-level evaluation (issue #63)

For a language-level requirement, the evaluator collects every fact with
the same normalized language name. The existing CEFR order
`A1 < A2 < B1 < B2 < C1 < C2` is the only threshold scale. Exact matching
of the same normalized non-CEFR label remains supported, but labels such
as `Fluent` are never mapped onto CEFR. If any comparable fact meets the
requirement, the result is `MATCH`. Its evidence and the structured
search `matched_fact_indices` contain **all and only** satisfying facts,
sorted by source evidence location rather than extraction-list order.
If none meets the requirement, an unstated or incomparable same-language
fact makes the aggregate `UNKNOWN`, even alongside below-threshold facts;
only a complete set of comparable below-threshold facts is
`NOT_MATCHED`. Absent language remains `UNKNOWN`. Bare language presence
behavior is unchanged.

`evaluate_language_with_fact_indices` is the sole decision and provenance
authority for both ordinary criterion evaluation and structured required/
preferred language-level filters. Direct Search and Agent continue to
use the shared result view. Single same-language facts retain their
existing status, reason, explanation, and evidence. Only previously
order-sensitive duplicate-fact eligibility may change. This semantic
change bumps deterministic `POLICY_ENGINE_VERSION` from `meyar-policy-v2`
to `meyar-policy-v3`: a scored evaluation with the same profile version,
criteria version, and date gets a separate v3 row, while historical v2
rows remain immutable. The ranking algorithm and numeric scoring formula
are unchanged (`SCORING_POLICY_VERSION` remains `meyar-score-v1`), but
changed criterion outcomes can legitimately change scores, fit bands,
eligibility, and ranking for this duplicate-fact case. Request/plan,
other filter kinds, embeddings, tenant/auth, and photo functionality
are unchanged. The existing scored-provenance unique index includes both
policy versions, so no migration is required.

## D-073 — Offline macOS arm64 dependency and filesystem activation foundation (issue #35 PR4)

**Date:** 2026-09-25. **Status:** Merged as PR #66, squash SHA
`860357e1bc6a63e38553ab65119adfd67109056b`; implementation was
based on `9b234a2c8ea0b4a3be533dfd0c382564016191d6`.

The accepted PR3 artifact is source and migrations, not a deployable
Python environment. PR4 binds an offline dependency directory to that
artifact's full source SHA, `ReleaseManifest`, `uv.lock`, target tags,
exact wheel bytes, and a bank-provisioned Python 3.12 patch-version and
interpreter-executable SHA. The build side selects compatible macOS arm64
wheels from exact URLs and hashes in the selected artifact's lockfile;
it downloads and inspects but never runs foreign wheels. The target
installer uses no `uv`, PyPI, Homebrew, Git, coding agent, or network
resolution. It requires an approved CPython 3.12 installation with
`venv`/`pip`; absence or identity mismatch fails closed. No Python
runtime is redistributed. The build side can use `uv` for its own
development environment, but `bundle-build` itself does not invoke or
ship a `uv` binary. `greenlet` is an explicit dependency because the
SQLAlchemy lock marker excludes macOS `arm64`; Pillow remains explicit.
The lock uses `asyncpg` plus Python `pgvector`, not `psycopg`.

The install root must preexist; `/opt/meyar` is the canonical production
example. `releases/<id>` holds read-only source, migrations, and a
release-local venv; `shared/config`, `shared/storage`, `shared/backups`,
and `shared/logs` hold all mutable and secret material. The stdlib-only
installer copied from the exact application artifact verifies the
manifest and wheel hashes, rebinds every wheel to `uv.lock`, extracts
only allowlisted regular archive members into a disposable staging
directory, installs dependencies with fixed argv and `pip --no-index
--require-hashes`, verifies the resulting tree, and renames a complete
release into place. A non-blocking local `flock` serializes install and
activation; a process death releases a stale lock. Identical reinstall
is idempotent; a conflicting/tampered release id is refused. No real
candidate data is inspected or copied.

Activation creates a generation with exact current/previous release ids
and rollback classification, then atomically replaces `<root>/current`
with a symlink to that generation's release link. This public symlink is
the sole active-release pointer; `activations/current` is not created.
Before the replacement, a first activation has no public current entry;
after it, the link resolves to a complete generation. The already accepted
LaunchDaemon argv can later target its venv Python.
`PROHIBITED_PENDING_PROCEDURE` and `BACKUP_RESTORE_REQUIRED` prevent
replacing an existing release until a later workflow can supply the
declared procedure or verified backup/restore gate.
No service lifecycle, Alembic downgrade, schema rollback, PostgreSQL,
Ollama/model provisioning, production-model approval, #36 benchmark,
or #46 runtime/ingestion hardening is performed. Hashes prove integrity
relative to the reviewed artifact and lock, not publisher identity; the
accepted commit and handoff remain operator trust inputs. Linux tests
simulate platform/filesystem contracts. Real Apple-Silicon import,
service, and reboot behavior remains unconfirmed.

## D-074 — Host production config and active-release service binding (issue #35 PR5)

**Date:** 2026-09-26. **Status:** Implementation for independent review
on `feat/issue-35-production-config-service-binding`, from accepted
`main` `860357e1bc6a63e38553ab65119adfd67109056b`.

PR2's original operator-supplied interpreter and working directory
could point to a pre-PR4 venv or cause `Settings(env_file=".env")` to
load config from an immutable release. PR4 established the release-local
venv and mutable `shared/` layout. PR5 resolves that mismatch with one
install-root-derived service contract: `<root>/current/.venv/bin/python`
invokes `-m uvicorn meyar.main:app --host 127.0.0.1 --port <port>`;
`WorkingDirectory` is `<root>/shared/config`; stdout/stderr logs are under
`<root>/shared/logs`. No `EnvironmentVariables`, shell wrapper, bare
executable or release-internal `.env` is accepted. The existing seven-key
plist shape and render-only output safety remain.

The service imports PR4's stdlib-only host verifier and checks the public
activation generation, installed tree, release-local Python and matching
backend source before rendering or accepting a plist. The copied offline
installer never imports the normal application package. Host config is
read with no symlink following and a 64 KiB limit; dotenv syntax,
duplicate critical keys and interpolation are checked before applying
application `Settings` validation without ambient shell overrides.
Findings use fixed codes and never include values. The host-specific
storage path must exactly match `<root>/shared/storage`.

Independent review found two fail-open metadata gaps: a world-readable or
wrong-owned secret file passed `config-verify`, and an active Python with
its execute bits removed passed service rendering because PR4's content
digest does not cover mode bits. The corrective contract ties `.env` and
its `root/shared/config` directory chain to the PR4 install-root owner UID;
all use the dedicated service GID, directories are group-traversable and
not group/other-writable, `shared/config` excludes other users, and `.env`
is owner/group-readable with no group write/execute or other access (e.g.
`0640`). Ancestors of the install root must also resist replacement by
unrelated users (root/operator owned, non-group/other-writable, with a
root-owned sticky temporary-directory exception). The service account must
belong to that dedicated group; bank operator provisioning and membership
verification remain PR6 work. Active
Python must have owner, group and other execute bits, matching PR4's `0555`
installed interpreter. This is metadata validation, not a claim that a
daemon has launched or can write its runtime paths. PR6 must establish
service-account traversal, protected write access to `shared/storage` and
`shared/logs`, and log-file ownership while honoring or explicitly revising
PR4's install-operator ownership/non-group-writable directory checks.

Application lifespan startup now calls `get_settings()` before serving
requests, so bypassing `config-verify` cannot defer a bad production
configuration until the first dependent request. When launched from the
canonical `shared/config` working directory, startup also validates the
host file independently and rejects any ambient `MEYAR_*` override that
would change effective Settings. Direct production
`Settings` requires an explicitly supplied,
nonblank DB URL without the known development credential, an explicit
nonblank non-default pending-login secret, Secure UI cookies, local
Ollama LLM and embedding providers, and a loopback Ollama endpoint.
Environment mode typos fail schema validation. Development/test behavior
remains compatible. Production model selection remains TBD until #36;
this decision makes no model approval claim. Issue #46 remains OPEN;
only its deployment-blocking production-config item is advanced.

Immutable code/runtime != mutable host configuration != mutable
candidate/document storage. PR5 performs no LaunchDaemon mutation,
service lifecycle, migration, model provisioning, or target-Mac
acceptance. Issue #35 remains OPEN.

## D-075 — Privileged LaunchDaemon lifecycle and protected runtime write contract (issue #35 PR6)

**Date:** 2026-09-26. **Status:** MERGED as PR #69; post-merge verified on
accepted main `5b247153151243a3c329b94dd9cb9184902e1c67`.

The system LaunchDaemon runs the verified active release as a dedicated
non-root service principal; the trusted install operator remains the
owner of the install root, configuration, runtime directories, and logs.
Privileged commands require Darwin and effective UID 0, supplied by bank
policy outside the tool. They require an explicit non-root install-owner
UID and verify PR5's complete host config chain against it. Ordinary
`config-verify` retains its caller-effective-UID rule. The tool does not
elevate itself or create users/groups. `pwd`, `grp`, and
`os.getgrouplist` verify the account, non-root UID, group existence,
and membership before installation or start.

Privileged operations acquire the existing PR4 lock by opening its
operator-owned inode read-only with `O_NOFOLLOW`, validating owner,
regularity, link count, and mode, then taking nonblocking `flock`.
They never create or chown that lock. Service installation prepares
operator-owned, service-group-traversable control paths, setgid
activation/shared paths, and only `shared/storage` and `shared/logs`
as service-group-writable `2770`; canonical logs are protected `0660`
regular files. The `.env` remains PR5's `0640` host-owned file and
immutable release files remain read-only. PR4's host-layout verifier
accepts group write only on those two mutable paths when their GID
equals the protected config service GID, setgid is present, and other
write is absent. This keeps future PR4 install/activation usable.

The one PR5 plist renderer remains authoritative. `service-install`
publishes its exact seven-key bytes to the fixed system destination as
root:wheel `0644` via a same-directory fsynced temporary file and an
atomic no-clobber hard link; conflicting or unsafe existing entries
are never overwritten. It does not start launchd. Start, stop, and
restart verify the installed exact bytes and host/principal/runtime
binding, then use fixed `/bin/launchctl` system-domain argv with finite
timeouts and discarded output. Start uses bootstrap plus kickstart if
absent, or ordinary kickstart if loaded. Stop uses bootout and verifies
absence; restart uses `kickstart -k` if loaded, or bootstrap plus ordinary
kickstart if absent. Probe status 113 is the absent state; other probe
failures are errors. The commands never claim HTTP, database, schema,
or model readiness from launchd visibility.

Application startup under the dedicated service UID uses a separate
runtime-only host-config verifier. From the canonical `shared/config`
working directory it accepts the non-root root owner only when that UID
differs from the non-root service UID, the complete PR5 owner/GID/mode and
ancestor-chain checks pass, the service belongs to the config group, and
the service has traversal/read access without control-path write access.
The same config parser, production Settings validation, and ambient
override comparison still apply. Ordinary `config-verify` continues to
require the invoking UID; privileged lifecycle continues to require the
explicit install-owner UID. No owner identity is placed in the plist.

This is a Linux-tested contract implementation, not real Apple-Silicon
launchd acceptance. No migration, service uninstall, update/rollback,
PostgreSQL/Ollama/model provisioning, HTTPS, Target-Mac benchmark, or
candidate/search behavior is added. Issues #35 and #46 remain OPEN.

## D-076 — Active-release fresh database schema initialization (issue #35 PR7)

**Date:** 2026-09-26. **Status:** Implementation for independent review on
`feat/35-active-release-schema-init`, from accepted main
`5b247153151243a3c329b94dd9cb9184902e1c67` (PR #69/PR6 merged).

`meyar-ops schema-init --install-root <root>` is an install-operator command
for first deployment only. It takes the existing PR4 exclusive operation lock,
verifies the PR4 active release and read-only installation, proves the imported
MEYAR package and Python executable belong to that exact release, verifies
PR5's protected production config, and loads the DB URL only from that file.
The canonical active `backend/alembic.ini` and script directory provide the
only migration authority; the actual single Alembic head must equal the one
declared in the installed release manifest. No caller-selected revision or
migration path is accepted.

The command queries only Alembic revision and PostgreSQL table names. An
exact-head database succeeds without mutation. A database with no revision
and no user tables (an empty `alembic_version` table is allowed) runs Alembic's
upgrade to that exact head through the active release's Python API and an
explicit connection made from protected settings, then independently checks
for exactly that revision. Any other unversioned table, stale or different
revision, or multiple revisions is refused. Connection, migration, and
postcheck failures use fixed codes without raw DB exceptions or credentials.
No candidate rows are read. No service lifecycle operation follows migration.

This is **not** the production update migration workflow. Existing older
schemas must await backup, compatibility, staged update, migration, readiness,
activation, and rollback boundaries. This PR adds no downgrade, stamp,
backup, restore, PostgreSQL/pgvector provisioning, Ollama/model provisioning,
or Target-Mac benchmark. Schema current does not mean application ready,
service healthy, or Ollama/model ready. Issues #35 and #46 remain OPEN;
issue #36 remains unstarted.

## D-077 — Read-only installed deployment acceptance gate (issue #35 PR8)

**Date:** 2026-09-26. **Status:** Implementation for independent review on
`feat/35-installed-deployment-ready`, from accepted main
`76ef38f8404657e904c096e114180f1a3c165ebc` (PR #70/PR7 merged and
post-merge verified).

`meyar-ops deployment-ready --install-root <root> --label <label>` is a
separate installed-host contract; PR1's general `readiness` is unchanged.
It proves the running interpreter, imported package, command implementation,
verified PR4 active release, and exact active Alembic code/manifest head
before trusting an installed deployment. PR5's protected config supplies
the DB URL and local Ollama/model settings without ambient-shell overrides.
The only service definition is the root-owned canonical plist at
`/Library/LaunchDaemons/<label>.plist`; user and port are extracted only
after safe-file/shape validation and exact renderer comparison. PR6's
principal and read-only runtime permission validation, the fixed
`/bin/launchctl print system/<label>` probe, direct numeric-loopback
`GET /api/v1/health` liveness probe, bounded `SELECT 1`, exact sole DB
revision check, and explicit-settings local-only Ollama providers produce
separate fixed-code findings. No candidate/business rows or secrets are
reported or read. The health route remains liveness-only; HTTP `/ready`
belongs to #46.

The command opens the existing lock without creating or changing it for
local installed-state checks, releases it during bounded health probes, and
reacquires it to reject changed local state, launchd visibility, or liveness
before `ok=true`. This is a
point-in-time operator gate; later host changes require a new run. It does
not mutate the filesystem, database, launchd, accounts, or models. It adds
no provisioning, update/rollback, benchmark, or model approval. #35 and
#46 remain open; #36 remains unstarted and real Target-Mac behavior
unconfirmed.

## D-078 — Quiesced installed-host backup creation and verification (issue #35 PR9)

**Date:** 2026-09-26. **Status:** Merged as PR #72 and post-merge verified
at accepted main `2dcf0a8783e49008b0eb707d81b42248209b34d6`.
PR #71 / PR8 was the implementation base.

`backup-create` requires the trusted non-root install owner, exact active
release Python/package/command and Alembic code/manifest identity,
protected PR5 Settings, canonical installed PR6 plist/runtime, a reachable
database with exactly the active revision, confirmed launchd absence
(`print system/<label>` exit 113), and a refused numeric-loopback
connection to the canonical application port. It holds the existing PR4
operation lock through snapshot and publication. Unexpected launchctl
results, socket timeouts, or ambiguous errors fail closed. The service is
stopped separately by the privileged lifecycle command. Bank operations
must exclude independent writers to this database during the maintenance
window; no tool can enforce that external boundary.

The command uses exact trusted `pg_dump`/`pg_restore` files from a validated
absolute client directory, fixed argv, PostgreSQL custom format, and a
private ephemeral `0600` libpq password file. It archives exactly
`shared/storage`, rejecting links and special nodes, streams file bytes,
and normalizes tar ownership. The versioned non-secret manifest records
release/source/schema identity, safe backup ID/time/operator UID, fixed
filenames, digests, sizes, and storage file count. After structural and
hash verification, a second installed-state/quiescence check must match
the first. A private `0700` stage with `0600` files is fsynced and
published under `shared/backups/<backup-id>` by a kernel no-replace
atomic rename; pre-existing backups cannot be overwritten. The parent
directory is opened and identity-checked before rename and fsynced after it.
Success requires that fsync. If it fails, the exact published directory is
moved back to its private staging name and the same validated parent
directory is fsynced again. Only a successful rollback fsync permits
`BACKUP_PUBLICATION_DURABILITY_FAILED` and identity-checked staging cleanup.
If rollback fsync fails, the private stage is preserved and
`BACKUP_PUBLICATION_STATE_UNCERTAIN` is returned. A changed final identity
or unsafe rollback also produces that uncertain-state result. Operators
must inspect `shared/backups`, resolve any private `.backup-*` residue, and
run `backup-verify` if the requested public ID exists before retry.

`backup-verify` reads the published artifact without protected config or
live DB access, verifies exact manifest schema/permissions/digests,
`pg_restore --list`, and bounded safe tar member structure without
extracting. A created/verified backup is not restore-tested. The existing
synthetic backup/restore acceptance remains the separate end-to-end proof;
production restore, update, rollback, scheduling, replication, and custom
encryption remain out of PR9. Infrastructure owns encryption at rest and
production-equivalent access control for backup candidate data. #35 and
#46 remain OPEN; #36 remains unstarted, and real Apple-Silicon behavior is
unconfirmed.

## D-079 — Verified backup to isolated restore (issue #35 PR10)

**Date:** 2026-09-26. **Status:** MERGED as PR #73, squash SHA
`39d1c2222aa96708aecd68d715d92024a1f72a1a`; post-merge verified.
Implementation began from accepted main `2dcf0a8783e49008b0eb707d81b42248209b34d6`.
PR9 / PR #72 was merged and post-merge verified before this slice.

The installed release adds `meyar-ops restore` with operator-selected safe
backup/restore IDs and a bounded isolated PostgreSQL database name. The
PR4 operation lock covers verification through completion. The full PR9
backup verifier is the source authority; the release ID, source SHA, and
Alembic head must match the exact immutable active release. PR5 protected
Settings supply host, port, principal, and password; only the database name
changes. The production database name, nonempty target, or unreachable
target is refused. The bank provisions the isolated target database and
any required extension; this command never creates or drops a database.
The empty-target precheck uses PostgreSQL dependency and ownership catalogs
across user object classes, plus catalogs for standalone database objects;
it rejects prior user objects before storage extraction. Only system
objects, the default `public` schema, and the deployment-required `vector`
extension and its owned objects (alongside PostgreSQL's default `plpgsql`)
are allowed. In particular, an unrelated installed extension is not an
empty-target exception.

Verified USTAR storage is manually streamed to a private
`shared/restores/.restore-*` stage, rejecting unsafe members, with archive
and extracted tree digests compared. Fixed-argv trusted absolute
`pg_restore` uses a private `PGPASSFILE`, `--exit-on-error`, and
`--single-transaction`; postcheck requires exactly the expected single
Alembic revision. Only then is `shared/restores/<restore-id>` published
with no-replace semantics and a non-secret fsynced completion manifest.
The live `shared/storage` and production database are never restore
destinations.

PostgreSQL and filesystem publication are not one transaction. Failure
after database success returns `RESTORE_INCOMPLETE_ISOLATED_TARGET` and
preserves isolated state; the operator must discard/recreate the isolated
database before retry. `RESTORE_COMPLETED` alone signals completion of
this isolated restore. It is not production cutover or application
readiness. Update, rollback, migrations, provisioning, and Target-Mac
acceptance remain separate future work. #35 and #46 stay OPEN; #36 is
unstarted.

## D-080 — Offline Ollama runtime and local model provisioning (issue #35 PR11)

**Date:** 2026-09-27. **Status:** Implementation for independent audit from
exact accepted main `39d1c2222aa96708aecd68d715d92024a1f72a1a`.
PR10 / PR #73 is merged and post-merge verified. Issue #35 and #46 remain
OPEN; issue #36 remains OPEN and unstarted.

The application release remains the source-code/runtime authority and its
existing `ModelManifestReference` remains the model-governance authority.
PR11 adds a separate transport manifest, `ai_bundle_manifest.json`, with
one macOS arm64 Ollama Mach-O runtime, its declared version and SHA-256, an
exact `model_manifest.json` SHA-256, and LLM/EMBEDDING local GGUF names and
SHA-256s. The release reference now has executable installed-host semantics:
`sha256:<model_manifest.json hash>` resolves only to the no-clobber immutable
manifest in `shared/ollama/manifests`. Existing documentary references are
not accepted by PR11 installation/verification. No GGUF or actual runtime
binary is committed. Hashes prove correspondence to a reviewed handoff,
not independent publisher authenticity.

The trusted non-root install owner verifies the bundle and installs the
runtime under `shared/ollama/runtimes/<SHA-256>/ollama` read-only, without
overwriting a different identity. Privileged lifecycle operations install
an exact root-owned system LaunchDaemon under the existing PR4 lock. It runs
`ollama serve` with fixed argv as a pre-existing dedicated non-root Ollama
user, distinct from the install owner and app user. Its fixed environment
contains only numeric loopback `OLLAMA_HOST`, canonical `OLLAMA_MODELS`,
private `HOME`, fixed `PATH`, and Ollama's documented `OLLAMA_NO_CLOUD=1`.
The Ollama principal's complete primary and supplementary group set must
exclude the installed MEYAR service GID from `shared/config`. Installed
path modes are checked for secret, candidate-storage, release, backup, and
restore isolation. Local model tags reject both `cloud` and `*-cloud` at
the shared manifest, bundle, installed-model, and production Settings
boundaries.
The Ollama user owns its private model/state paths and logs; other-execute
traversal on root/shared/logs permits those paths without granting read
access to app releases, protected config, candidate storage, backups, or
restores. The app continues to access Ollama only over numeric loopback.

`model-install` accepts no arbitrary model name. It validates the active
release reference and protected production Settings against exactly one
LLM and one EMBEDDING manifest entry, rejects cloud/URL/namespace names,
verifies source artifacts, then copies each GGUF to a private temporary
stage readable by the Ollama user. Its sole Modelfile content is
`FROM <verified-local-staged-GGUF>`; it invokes the immutable executable
with fixed `create <manifest-name> -f <Modelfile>` argv and a fixed local-only
environment. There is no shell, `ollama pull`, registry lookup, Git,
Homebrew, curl/wget, pip, or dependency download on the target host.
Partial failure never publishes the final receipt and never deletes
unrelated models; an unreceipted name conflict requires operator review.

The completion receipt distinguishes source GGUF SHA-256 from the installed
Ollama tag digest. Read-only `model-verify` checks the exact active release,
manifest hash/status/config names, installed runtime and LaunchDaemon,
local `/api/version` and `/api/tags` digest, and one bounded synthetic
`/api/embed` vector dimension. `deployment-ready` requires this authority
before reporting model availability and rechecks it after probes. Production
provider requests recheck local tag identity before candidate-bearing
requests. Results are fixed-code and never include candidate text, secrets,
raw process output, or Ollama bodies.

All PR11 synthetic manifests remain `DEVELOPMENT_INTEGRATION`.
**Model installed != model production-approved.** PR11 proves only the
installation and verification mechanism. Final production model selection
and approval require real Target-Mac evidence under Issue #36. Real
Apple-Silicon LaunchDaemon, binary import, cloud-disable, and host egress
behavior are not established by Linux simulations.

## D-081 — Staged installed-host update and explicit application rollback (issue #35 PR12)

**Date:** 2026-09-27. **Status:** Implementation for independent audit from
exact accepted main `38624ca3440c7dbf750d4248d8e0a9992b7192b5`.
PR11 / PR #74 is merged and post-merge verified. #35/#46 remain OPEN;
#36 is OPEN and unstarted.

The trusted non-root install owner executes five separate phases:
`update-prepare`, `update-apply`, `update-finalize`, `rollback-apply`, and
`rollback-finalize`. Existing privileged `service-stop` and
`service-start` remain outside that process and preserve the PR6 root,
install-owner, application-user, and Ollama-user separation. No phase
invokes `sudo` or a shell. The target is an already installed, verified
immutable release; there is no target-host Git or network update.

Prepare requires full current `deployment-ready`, verifies source/target
release and unchanged local model manifest identity, checks production
config and the target migration graph, then reacquires the PR4 operation
lock and rechecks identity before publishing a private plan. APP_ONLY
requires equal heads. FORWARD_COMPATIBLE_SCHEMA requires exactly one
target head and a statically parsed, unbranched descendant path from
the source head; no migration module is executed for this proof.
BACKUP_RESTORE_REQUIRED and PROHIBITED_PENDING_PROCEDURE are rejected
before mutation, as is a model-manifest reference or status change.

Apply independently confirms launchd exit-113 absence, re-verifies the
entire PR9 backup, and requires its source release/SHA/head and timestamp
to bind to the plan. APP_ONLY never runs Alembic. Forward migration runs
only through the verified target release's Python/package/migration code
against protected host config, from the exact source head to the exact
target head; argv contains no DB credential and output is suppressed.
PostgreSQL transaction and revision postchecks guard the migration.
Failure with a confirmed source head leaves the old pointer and stopped
service; uncertain DB revision refuses activation. If migration reached
the target head before activation, the explicit forward-compatibility
declaration permits retry to complete activation without rerunning it.

Activation reuses PR4's atomic `current` pointer and records update ID,
plan digest, backup ID, reason, and DB head in its generation. Private
`shared/updates/<id>/` phase receipts use exclusive no-follow creation,
file and directory fsync, and no overwrite. A missing apply or rollback
receipt can be reconstructed only from the exact matching generation.
`UPDATE_APPLIED_SERVICE_STOPPED` is not completion: only full readiness
and durable finalize yield `UPDATE_COMPLETED`. Readiness failure never
automatically rolls back a running app.

Explicit rollback only returns to the transaction's prior app release.
APP_ONLY keeps the common DB head. FORWARD_COMPATIBLE_SCHEMA retains the
target DB head; **no Alembic downgrade** is run. The normal readiness
exact-head rule gains one evidence-bound exception requiring the matching
rollback generation, plan, rollback receipt, verified source release,
declared compatibility, and unchanged model identity. It emits
`DB_REVISION_FORWARD_COMPATIBLE_ROLLBACK`. A new update is blocked over
an incomplete transaction or this forward-compatible rollback state;
reconciliation needs a separate reviewed procedure. Production restore
cutover, model migration, automatic rollback, cleanup, and Target-Mac
validation remain outside PR12. Real Apple-Silicon behavior is unconfirmed.

## D-082 — Agentless diagnostics and lifecycle acceptance evidence (issue #35 PR13)

**Date:** 2026-09-27. **Status:** Proposed engineering implementation
for independent audit from exact accepted main
`7c911cbc850d21999eae2239e6b51bfedc570d4a`. PR12 / PR #75 is
merged and post-merge verified. #35/#46 remain OPEN; #36 is explicitly
next after #35 acceptance; #49 is post-presentation expansion.

**Decision:** Use the existing `meyar-ops` interface, PR4 lock and active
release authority, PR8 readiness, PR9 backup verification, PR10 isolated
restore evidence, PR11 model identity, and PR12 update/rollback receipts.
Add local-only allowlisted `collect-diagnostics`/`diagnostics-verify`,
fixed-path HTTPS `edge-verify`, human two-phase `reboot-prepare`/
`reboot-verify`, dry-run-first `cleanup`, and private
`lifecycle-acceptance` with `PASS|FAIL|INCOMPLETE`. No second release,
service, DB, model, backup, update, or lock authority is introduced.

Diagnostics publish safe metadata only after lock → identity snapshot →
unlocked bounded probes → lock → identity recheck. The artifact excludes
raw logs and candidate/secret material by construction. HTTPS is an
externally managed bank reverse proxy, with both application and Ollama
remaining on numeric loopback. Reboot proof requires a changed macOS
`kern.boottime` and full postboot readiness. Cleanup deletes only
canonical orphan activation temporary symlinks. Lifecycle acceptance
requires matching physical reboot, backup, isolated restore, finalized
update and explicit rollback, HTTPS edge, diagnostics, and disposable
synthetic smoke evidence; missing evidence is never a pass. No Alembic
downgrade or production restore cutover occurs.

**PR13 provenance correction:** A synthetic smoke receipt is valid only when
the smoke workload itself runs from the verified immutable active release.
Evidence mode launches `meyar.smoke_workload` with the active release's
Python; its migration, CLI, and Uvicorn processes use that same Python and
installed source tree. Disposable PostgreSQL and storage are explicit. The
worker verifies active release/source/schema/model/config identity before and
after the run. Receipt format 2 names fixed required capability checks, and
`lifecycle-acceptance` refuses legacy `PASS` plus step-count evidence. A local
Ollama timeout or missing capability yields no PASS receipt.

**PR13 offline database correction:** Installed smoke requires an operator-
documented `pgvector/pgvector@sha256:<approved-manifest-digest>` reference.
It verifies that exact image locally before container creation and runs with
`--pull=never`; it never repairs or pulls a missing image. The receipt binds
the exact digest and lifecycle verification compares it with the same approved
input. Docker is a prerequisite of the disposable synthetic rehearsal only,
not a selected production PostgreSQL topology; PR13 does not provision it.
Missing Docker or the exact preloaded image leaves synthetic smoke incomplete.

**Boundary:** PR13 engineering tooling and Linux tests are not #35
acceptance. A separate owner audit after merge decides issue closure.
Real Apple-Silicon launchctl, reboot and native-runtime behavior remain
unconfirmed without a non-target Mac rehearsal. The bank-Mac benchmark,
production model decision, and latency/RAM/concurrency gates remain
unstarted #36 work. **Issue #35 tooling complete != real bank Mac
benchmark complete.** No production deployment acceptance is inferred.

## D-083 — Server-owned AgentResultSet replaces last_search_candidate_ids (issue #49 PR49-1)

**Date:** 2026-09-28. **Status:** Accepted.

**Decision:** Replace `AgentConversation.last_search_candidate_ids` (a bare
JSON id list) with `AgentResultSet`/`AgentResultSetMember` — tenant/session/
context-epoch-scoped rows carrying full search provenance (canonical
`CandidateSearchRequest`, planner/search policy versions, a corpus
fingerprint, and an `expires_at` bound to the owning `BrowserSession`).
`AgentConversation` gains `context_epoch` (int, bumped on every "Yeni
söhbət" reset) and `active_result_set_id` (FK, `ON DELETE SET NULL`). The
LLM still only ever emits `candidate_ref = N`; resolution is now
`meyar.services.agent_result_set_repo.resolve_active_candidate_ref`, an
8-step ordered check (active pointer set → AgentResultSet exists AND
`tenant_id` matches in one query → `browser_session_id` matches →
`context_epoch` matches → not expired → corpus fingerprint still matches →
ordinal in range → candidate still currently authorized) — the model
interprets/orchestrates, the server alone decides which candidate_id (if
any) a `candidate_ref` names, matching the existing D-030/D-031 split.

**Corpus fingerprint:** sha256 over sorted
`"{candidate_id}:{profile_version_id}[:{embedding_version_id|NONE}]"`
tuples for the tenant's current, search-authorized profiles (same
`list_current_profile_versions_for_tenant` + `authorize_profile_version`
notion `meyar.search.service` itself uses) — never profile content, CV
text, or CandidateIdentity. A mismatch at resolution time means the corpus
has drifted (reprocessing, a new candidate, an embedding change) since the
result set was created, and resolves to `RESULT_SET_STALE`; an expired
`expires_at` resolves to `RESULT_SET_EXPIRED` — both distinct from
`CANDIDATE_REF_NOT_FOUND` (no legitimate reference could ever be
identified) so HR is told the truthful reason to re-run the search.

**CandidateIdentity exclusion:** neither `AgentResultSet` nor
`AgentResultSetMember` carries a name/email/phone field, and membership
order/staleness never depends on one — ordinal authority is a
professional-fact/provenance concept exactly like search/evaluation.

**Immutable-snapshot FK rationale:** `AgentResultSetMember.candidate_id`,
`candidate_profile_version_id`, and `candidate_embedding_version_id` are
deliberately plain UUID columns with NO `ForeignKey` constraint against
`candidates`/`candidate_profile_versions`/`candidate_embedding_versions`.
A hard candidate delete (`meyar.services.candidate_service.
delete_candidate_cascade`) must never be blocked by, and must never
cascade-delete or silently renumber, a historical membership row — a
deleted/reprocessed candidate simply makes the fingerprint stop matching
(`RESULT_SET_STALE`), never a DB integrity error. `result_set_id` DOES
`ON DELETE CASCADE` to `agent_result_sets.id` — deleting a whole result set
legitimately deletes its own membership rows; that is not the same
"destructive FK" concern.

**Migration:** `d2a8f6c1b3e9` (chained on `8bd12e7c4a60`) drops
`last_search_candidate_ids` with no backfill — there is no provenance
(planner/search request, corpus fingerprint, expiry) to safely reconstruct
an `AgentResultSet` from a bare id list. A deployment upgrade may require
one fresh search before an ordinal follow-up works again; existing
transcripts/pending job drafts are untouched.

**Scope note:** the agent's own NL search planner
(`meyar.search.planner_service`) only ever produces `STRUCTURED_ONLY`
requests today — SEMANTIC_ONLY/HYBRID result-set population is exercised
via direct `agent_result_set_repo` unit tests (constructing a
`PlannedCandidateSearchResponse` directly), not the full agent turn; no
product behavior regresses, this is pre-existing planner scope.

## D-084 — Conversational result-set refinement (issue #49 PR49-2)

**Date:** 2026-09-28. **Status:** Accepted (implementation), NOT YET owner-
accepted for issue #49 closure — see the PR's own human-checkpoint report.

**Decision:** Add a dedicated `REFINE_CANDIDATE_RESULTS` agent action
completing D-083's result-set authority with conversational continuation
("ilk üçü", "bunlardan SQL bilənlər", "5 nəfərə endir"). The model-facing
decision carries ONLY bounded intent — `filter_query: str | None` (forwarded
unmodified into the existing frozen NL search-planner pipeline, exactly like
`SEARCH_CANDIDATES.search_query`) and `limit: int | None` (`1..
MAX_CANDIDATE_REF`) — never a `result_set_id`, `candidate_id`,
`candidate_profile_version_id`, `candidate_embedding_version_id`, candidate
list, or ordinal membership list. At least one of the two must be set
(enforced by `AgentDecision` schema validation); a bare "bunlardan"-shaped
request with neither routes to `CLARIFY(NEED_MORE_DETAIL)` instead.

**Core rule:**

```text
LLM interprets refinement intent.
Server-owned ResultSet defines the candidate universe.
Deterministic policy filters/limits that universe.
A new immutable ResultSet becomes the new context.
```

Concretely: **derived members ⊆ parent members** — a refinement's
authoritative input universe is exactly `active_result_set.members ordered
by ordinal`; a candidate absent from that set can never enter the derived
set no matter what a global search for the same criterion would return
(regression-tested: `test_filter_never_admits_a_candidate_outside_the_
parent_set`). Filtering evaluates `CandidateSearchRequest.required_filters`
(the Slice 8 `evaluate_required_filters` gate, unchanged) against each
retained member's own CURRENT authorized profile — never `preferred_
filters` (no reranking in this PR) and never a fresh
`meyar.search.service.search_candidates` tenant-wide query. **Filtering
preserves the parent's existing order; limit truncates that same preserved
order; new dense ordinals (`1..N`) are server-generated** — the LLM never
assigns or modifies `relevance_score`/`structured_score`/`semantic_score`/
ordinal; scores are copied unchanged from the retained source member.

**Authority reuse, not duplication:** `_validate_active_result_set` factors
the 6 shared steps of D-083's own check order (active pointer set → tenant-
scoped existence → session match → epoch match → not expired → corpus
fingerprint match) out of `resolve_active_candidate_ref` into one function
both it and the new `create_result_set_from_refinement` call — ordinal
resolution, `active_result_set_size`, and refinement all consult the exact
same authoritative definition, never a second staleness policy. STALE/
EXPIRED resolve to the same `RESULT_SET_STALE`/`RESULT_SET_EXPIRED` turn
outcomes D-083 already established; every other reason (no active result
set, cross-tenant/cross-session/cross-epoch mismatch) collapses to one
deterministic `CLARIFY(RESULT_CONTEXT_REQUIRED)` clarification — the new
`AgentResponseCode` member for a refinement-shaped request with no
resolvable context, distinct from `CANDIDATE_REFERENCE_REQUIRED` (which
names one candidate, not a whole result list).

**Defense in depth beyond the corpus fingerprint:** before evaluating any
filter, each source member's own recorded `candidate_profile_version_id`
must still equal the candidate's CURRENT authorized profile version. A
mismatch (an edge case the aggregate fingerprint does not happen to cover)
fails the WHOLE refinement as `RESULT_SET_STALE` — never a partial result,
never silently evaluating a different profile version than the one this
result set's own provenance recorded.

**Provenance model:** `AgentResultSet` gains `result_set_kind` (`SEARCH` |
`REFINEMENT`), `parent_result_set_id` (a plain immutable UUID snapshot
reference — deliberately NOT a self-referential `ForeignKey`, mirroring
`AgentResultSetMember`'s own candidate/profile/embedding snapshot-reference
rationale in D-083: a derived row's provenance chain must never be
corrupted by, or block, a later deletion of an ancestor row),
`refinement_request_sha256`, `canonical_refinement_request` (DB-only
provenance — `{"filter_request": <validated CandidateSearchRequest|null>,
"limit": <int|null>}` — never copied into audit metadata, exactly like
`canonical_search_request`), and `refinement_policy_version`. A derived
row's `canonical_search_request`/`planner_*`/`search_policy_version`/
`search_mode`/`corpus_fingerprint_sha256` are copied VERBATIM from the
parent at creation time — never overwritten with the refinement's own
filter text — so "what search produced this context" is always answerable
from one row without walking the parent chain. `expires_at` is inherited
exactly (`derived.expires_at == parent.expires_at`); a refinement never
extends validity beyond its parent. Every row that predates this PR is
backfilled truthfully as `result_set_kind='SEARCH'`,
`parent_result_set_id=NULL` (migration `543c60f7efc5`, chained on
`d2a8f6c1b3e9`) — no fabricated refinement provenance for a pre-existing
row.

**Limit-exceeds-count policy:** a `requested_limit` greater than the
current (post-filter) member count still creates an auditable derived
ResultSet containing every available member, truthfully reporting the
smaller count (`AgentRefineToolResult.limit_truncated=True`) — never a
fabricated member, never silently treated as a no-op.

**Zero-result policy:** a valid deterministic refinement that retains zero
members is NOT an error — a `result_count=0` derived ResultSet is created
and becomes the new active context (a later ordinal reference against it
then fails safely via the normal ordinal-out-of-range path, never
auto-restoring the parent).

**Model-facing context correction:** orchestration prompt v6 keeps two
bounded, non-authoritative facts separate on every server request:
`active_result_context_present` is derived only from
`conversation.active_result_set_id is not None`, while
`available_candidate_refs` contains only currently valid individual
ordinals after normal ResultSet validation. Therefore a valid zero-member,
stale, expired, foreign, or otherwise tampered pointer is still presented as
context-present with no ordinal authority. This permits a refinement-shaped
request to reach the existing server validation and return the truthful
zero-result child, `RESULT_SET_STALE`, `RESULT_SET_EXPIRED`, or fail-closed
context clarification. A reset clears the pointer and therefore presents
context-absent. The model receives only the boolean — never the ResultSet UUID,
candidate/profile/embedding IDs, or membership — and a true value never
authorizes `candidate_ref`.

**Turn-terminal, no reranking, no side effects:** `REFINE_CANDIDATE_RESULTS`
is always turn-terminal (`meyar.agent.service._dispatch_refine`, mirroring
`GET_CANDIDATE_PROFILE`/`GET_CANDIDATE_EVIDENCE`) — the model never keeps
looping after a valid refinement into an accidental new search. A
semantic/hybrid filter plan (not producible by today's agent NL planner
per D-083's own scope note, but guarded regardless as defense in depth) is
rejected with a fixed `UNSUPPORTED_REQUEST` clarification, never silently
downgraded to `STRUCTURED_ONLY` and never executed as a global search. A
prohibited-attribute filter_query is rejected by the SAME
`precheck_natural_language_request`/`find_prohibited_term`/
`CandidateSearchRequest` denylist chain SEARCH_CANDIDATES already uses —
no second denylist, no derived ResultSet, the active ResultSet is left
completely untouched. `CandidateIdentity` is never imported by
`meyar.services.agent_result_set_repo` or `meyar.models.agent_result_set`
(same structural test as D-083, now also covering the refinement code
added to those two modules). No `Job`/`JobCriteriaVersion`/`Evaluation`
row is ever created by a refinement turn.

**Audit:** `agent.result_set.refined` (success) / `agent.result_set.
refine_rejected` (an active-result-set validation failure) carry only
ids/enums/counts/bools (`source_result_set_id`, `result_set_id`,
`context_epoch`, `source_result_count`, `result_count`,
`refinement_request_sha256`, `refinement_policy_version`, `requested_limit`,
`has_filter`) — never the raw HR filter/query text, never a candidate
name/email/phone.

**Concurrency:** the complete refinement turn runs inside the same
`get_or_create_conversation_for_update` row-lock transaction D-083
established — two concurrent `REFINE_CANDIDATE_RESULTS` turns on the SAME
browser session serialize into one coherent chain (R0 → R1 → R2), proven at
the real `/ui/agent` route boundary (`test_real_ui_agent_route_
serializes_concurrent_same_session_refinements`), never two siblings both
derived independently from R0 with a competing active pointer.

**Non-scope (unchanged from issue #49's own non-scope, and PR49-2's own
governance bounds):** semantic reranking of the current set, deep candidate
factual Q&A, candidate-to-candidate comparison, hiring recommendation, LLM
numeric scoring, new scoring policy, RAG/long-term conversation memory, the
future unified-composer/sidebar UI redesign.

## D-085 — Unified HR composer with server-owned entry routing (issue #79)

**Decision:** the normal HR agent surface has one composer (one textarea and
one Send button). The removed HTML `intent` selector is not replaced by a
hidden client authority: **client UI mode is not authority**, and an unknown
or forged legacy `intent=draft_job_criteria` form field is ignored. The
server-owned `meyar.agent.intent_routing` boundary alone decides one of three
narrow entry outcomes before ordinary agent dispatch:

```text
FORCE_JOB_DRAFT | MODEL_ROUTED | CLARIFY_AMBIGUOUS
```

This boundary performs no search and no business mutation. It reuses the
existing normalized semantic requirement analysis and considers only
workflow/structural evidence. An explicit vacancy/JD noun plus an explicit
analyse/draft request is a confirmed draft. A vacancy-labelled source with
material semantic requirements, or a genuinely pasted multi-line/bulleted
role description with multiple material professional requirements, is also a
confirmed draft. Candidate/result nouns, find/show/list verbs, an explicit
requested candidate count, and current-result/ordinal language strongly
protect the normal model-routed path. Requirement language by itself is not a
draft authorization. Requirement-shaped text whose purpose remains genuinely
unclear receives fixed Azerbaijani clarification asking whether it should be
used for candidate search or vacancy-criteria analysis; neither the model nor
a business tool runs for that turn.

**Authority order:**

```text
server-owned deterministic entry routing where confidence is sufficient
→ bounded typed AgentDecision for remaining conversational routing
→ server validation of every proposed action
→ existing domain authority
```

The local model continues to receive the strict bounded `AgentDecision`
schema for normal conversation, search, current-ResultSet refinement, and
ordinal profile/evidence actions. **LLM intent is proposal, not
authorization.** A model-proposed `DRAFT_JOB_CRITERIA` from the normal route
is rejected with fixed HR-facing clarification and no tool execution; it is
never silently reinterpreted. A deterministically confirmed JD bypasses the
model and enters the existing source-bound, review-only draft path. Actual
`Job`/`JobCriteriaVersion` persistence still requires the pre-existing
explicit human confirmation flow; ordinary candidate search can never
silently persist a vacancy. `Evaluation` creation is likewise outside entry
routing.

The existing pending-draft follow-up boundary remains ahead of new-entry
routing, so bounded required/preferred, result-limit, and supported criterion
updates continue against the server-held source-bound draft. D-083/D-084
remain authoritative inside `MODEL_ROUTED`: active ResultSet, zero-result,
stale/expired, ordinal, tenant/session/context-epoch, and same-session row-lock
semantics are unchanged. The owning `AgentConversation` row is still locked
before routing-dependent state mutation, ResultSet changes, pending-draft
changes, or transcript writes. Existing prohibited-attribute policy remains
authoritative in both search and JD paths; `CandidateIdentity` gains no
routing role.

**Audit/privacy:** `agent.entry.routed` records only closed structural values
(`routing_source`, `routed_action`, and routing policy version).
`agent.entry.action_rejected` records the same structural routing provenance
when a model proposes an unauthorized draft. Neither event contains the raw
message/JD, model raw output, candidate identity, CV/evidence content, or
internal regex matches. Existing tool/turn events remain the action record;
the entry event does not duplicate business payloads.

**Non-scope:** persistent conversation sidebar/history (#80), cross-login
conversation ownership, candidate comparison/deep Q&A (#50), RAG memory,
hiring recommendations, scoring changes, deployment/Target-Mac work, and an
unrelated UI redesign.

**Amendment — routing policy `agent-entry-routing-v3` (PR #81 visual
acceptance correction).** Real-Ollama visual review showed that leaving an
explicit new search to the orchestration model is unsafe: the local model
routed `Python bilən namizədləri göstər` to `REFINE_CANDIDATE_RESULTS` with no
active ResultSet, so HR received result-context-required copy and no search.
The entry outcomes are now:

```text
FORCE_JOB_DRAFT | FORCE_CANDIDATE_SEARCH | FORCE_RESULT_LIMIT | MODEL_ROUTED
| CLARIFY_AMBIGUOUS | CLARIFY_JOB_SOURCE_REQUIRED
```

Precedence (named predicates in `meyar.agent.intent_routing`):

```text
pending draft follow-up (caller, unchanged)
→ explicit vacancy-analysis request (JD draft, or source-required clarification)
→ count-only current-result follow-up (FORCE_RESULT_LIMIT, #49 limit)
→ other current ResultSet / refinement / ordinal language (MODEL_ROUTED, #49)
→ explicit new-search imperative (FORCE_CANDIDATE_SEARCH)
→ structurally strong pasted JD (FORCE_JOB_DRAFT)
→ requirement ambiguity (CLARIFY_AMBIGUOUS)
→ ordinary MODEL_ROUTED
```

- **Deterministic search authority.** A search imperative in imperative
  position (Azerbaijani verb ending the request — `göstər`, `tap`, `axtar`,
  … optionally `-in/-iniz` and a polite tail; English verb starting it —
  `find`, `show`, `list`, …) plus a candidate noun, a source-bound material
  requirement, or an explicit result count. Inflected verbs inside
  requirement sentences (`göstərməlidir`, `must show`) and a bare candidate
  noun never qualify. The server builds
  `AgentDecision(SEARCH_CANDIDATES, search_query=<exact user text>)`; the
  router parses no filters. The existing validated planner (deterministic
  fast path or local LLM planner), search service and ResultSet authority run
  unchanged, and the validated tool result ends the turn — the orchestration
  decision call is not consulted for such an entry. Audit:
  `routing_source=DETERMINISTIC_SEARCH`, `routed_action=SEARCH_CANDIDATES`.
- **Count-only follow-up.** Live review also showed the local model shaping
  `ilk 3` as `REFINE_CANDIDATE_RESULTS(filter_query="ilk 3")` (no limit), which
  the refine planner cannot execute. When the WHOLE message is only a count on
  the current results (`ilk 3`, `ilk üçü`, `first 3`, `top 5`, `5 nəfərə
  endir`, 1..`MAX_CANDIDATE_REF`), the server builds
  `AgentDecision(REFINE_CANDIDATE_RESULTS, limit=N)` and the existing #49
  refinement dispatch validates the active ResultSet (truthful
  result-context-required copy when none). Any filter text (`bunlardan SQL
  bilən ilk 3`) stays model-routed. Audit:
  `routing_source=DETERMINISTIC_RESULT_CONTEXT`,
  `routed_action=REFINE_CANDIDATE_RESULTS`.
- **Exact JD source slice.** When an explicit analysis instruction is the
  leading segment before the first `:`/newline ("Vakansiyanı analiz et:"),
  the router returns exact offsets of the remaining user-owned text and only
  that substring reaches JD extraction/fallback. No rewrite, no LLM
  stripping. A structural header (`Vakansiya: Senior Backend…`) has no
  analysis verb and keeps the full source. Offsets are ephemeral; the slice
  is never audited.
- **Source required.** An explicit analysis command with no useful source
  (no material requirement; for a wrapper, also no multi-line source) returns
  fixed clarification asking for the vacancy text/requirements. No
  extraction, no pending empty draft.
- The orchestration prompt (`agent-orchestrator-prompt-v8`) now states that
  confirmed JDs and explicit new searches are handled before its call; it
  gains no authority.

**Amendment — routing policy `agent-entry-routing-v4` (PR #81 final
blocker).** Entry routing now runs deterministic semantic analysis on every
`/ui/agent` turn, which made a pre-existing parser limit reachable from the
browser: one long unpunctuated clause (e.g. `"Python " * 400 + "tələb
olunur"`, well under the 4000-character composer cap) cannot be represented
as a bounded exact `SourceOccurrence` (`text` max 500) and raised an unhandled
Pydantic `ValidationError` (HTTP 500). `route_agent_entry` is now total over
valid composer input: it catches `ValidationError` only when the validated
model is `SourceOccurrence` and every error is `string_too_long` on `text`,
and returns the new outcome `CLARIFY_INPUT_STRUCTURE`
(`routing_source=DETERMINISTIC_CLARIFICATION`, `routed_action=CLARIFY`) with
fixed copy asking HR to split the requirements into shorter sentences or
separate lines. Any other validation failure propagates as an internal
defect. The text is never truncated, never sent to the orchestration model,
search planner or JD extraction, and no ResultSet/Job/JobCriteriaVersion/
Evaluation is created. Audit metadata stays structural only (source, action,
version); the raw message and exception text are never recorded. The
orchestration prompt contract is unchanged.

## D-086 — Durable conversation authority separated from BrowserSession live context (issue #80 PR80-1)

**Context:** before #80, `AgentConversation` was 1:1 with one
`BrowserSession` (`browser_session_id UNIQUE`) and carried the live
`context_epoch`/`active_result_set_id`. Logout/login therefore produced a new,
empty conversation and made the old transcript unreachable. Simply dropping
the unique constraint, or rebinding an old conversation to a new session,
would make ResultSet and pending-draft authority ambiguous.

**Decision — two separate representations, one authority each:**

- **BrowserSession = authentication transport, NOT durable conversation
  owner.**
- **`AgentConversation` = durable tenant/user/membership-owned history:**
  `tenant_id`, `owner_user_id`, `owner_membership_id`, closed `title_kind`,
  bounded `turns`, timestamps. It has no FK to `browser_sessions`, so session
  expiry/deletion never deletes history.
- **`AgentConversationSessionContext` = BrowserSession-bound live ResultSet /
  pending-action authority:** `tenant_id`, `conversation_id`,
  `browser_session_id`, `context_epoch`, `active_result_set_id`,
  `active_pending_draft_id`; `UNIQUE(conversation_id, browser_session_id)`;
  CASCADE with its BrowserSession and conversation. It is the ONLY live
  authority for the active ResultSet, candidate ordinals, refinement, and
  pending JD mutation authority. `run_agent_turn` now receives
  `conversation` (durable transcript) and `session_context` (live authority)
  explicitly and never infers session state from the transcript.
- **Historical transcript is NOT ResultSet or mutation authority.** It never
  recreates ordinals, the active ResultSet, pending-draft authority,
  tenant/user identity, or scoring criteria.
- **A new BrowserSession never inherits old ResultSet/pending-draft
  authority.** Relogin sees the durable, server-validated transcript, but its
  new context starts with `active_result_set_id = NULL` and
  `active_pending_draft_id = NULL`; "ilk 3" returns result-context-required,
  old ordinals do not resolve, and an old draft cannot be confirmed. A new
  context's `context_epoch` is one past the conversation's highest epoch
  (monotonic per durable conversation, defense in depth).

**ResultSet conversation binding (security blocker fix):** one BrowserSession
may now hold several conversations' contexts at the same epoch, so
session + epoch no longer identify the candidate universe. `AgentResultSet`
gains non-null `conversation_id` (FK → `agent_conversations`, CASCADE). SEARCH
rows copy it from the resolving context; REFINEMENT rows preserve the
(validated) parent's. Active-set validation requires context tenant/session
match, row tenant, row `browser_session_id`, row `conversation_id ==
session_context.conversation_id` (new `CONVERSATION_MISMATCH`, collapsed
outward to `NOT_FOUND`), epoch, expiry, and corpus fingerprint — additive
defense in depth; no earlier check was removed. A tampered pointer from
conversation B to conversation A's set fails closed even inside one
BrowserSession at the same epoch.

**Pending-draft authority:** a pending draft is actionable only when (1) the
live owner may access the conversation, (2) the current BrowserSession
resolves that conversation's context, (3) `active_pending_draft_id ==
draft_id`, (4) the matching payload exists in that conversation's server-held
transcript, and (5) all existing source-bound confirmation rules pass
(`resolve_pending_draft_authority`, `get_active_pending_job_draft`). A
modified draft (new id) moves the pointer, so the old id fails; confirmation
sets the pointer to NULL. Replay/idempotency remains exclusively the
dedicated `AgentDraftConfirmation` row (checked after the lock attempt so a
concurrent duplicate observes the winner) — never transcript scanning.

**Ownership:** every read/write validates `tenant_id`, `owner_user_id`, and
`owner_membership_id` against the live `UIContext` principal, which
`get_ui_context()` still re-derives from active User + TenantMembership on
every request; stored ownership never bypasses live auth, so a disabled user
or membership loses access immediately. Foreign-tenant, foreign-user,
foreign-membership, and nonexistent conversation ids are indistinguishable
(generic 404, no title/timestamp).

**FK/retention:** tenant deletion cascades conversations. Owner
user/membership FKs are NO ACTION: a user or membership cannot disappear
silently while it owns durable history (a tenant delete still succeeds
because the deferred end-of-statement check sees both rows gone). Session
contexts and ResultSets are deleted with their BrowserSession; the durable
conversation survives.

**Transcript storage vs. model context:** durable storage is bounded by
`MAX_PERSISTED_AGENT_TURNS = 100` (oldest entries dropped); the model still
receives only the last `agent_max_context_turns` (default 8). Raising the
storage bound never increases model input; there is no unbounded memory and
no whole-history prompt.

**History listing:** `list_owned_conversations` is paginated (`page >= 1`,
`page_size` default 20, max 50), ordered `updated_at DESC, id DESC`, owner
scoped. No unbounded "all conversations" query.

**Title:** closed server-owned `title_kind` (`NEW`, `CANDIDATE_SEARCH`,
`VACANCY_ANALYSIS`, `RESULT_REFINEMENT`, `GENERAL`), DB check-constrained.
`NEW` transitions once, from the completed server-validated turn result (first
executed tool type, or `GENERAL` for a zero-tool closed-response answer);
clarifications/failures leave it `NEW`. Never derived from message, query, JD,
candidate, or CV text; no LLM title call. PR80-2 renders localized labels plus
date/time.

**"Yeni söhbət":** creates a NEW durable conversation (`NEW`, empty turns)
plus a clean context for the current BrowserSession (`context_epoch=1`, no
ResultSet/pending draft). The previous conversation is never cleared or
deleted. `reset_conversation` is retired.

**Legacy `/ui/agent` compatibility (PR80-1 only; no sidebar):** GET renders
the session's current conversation (the owned conversation this
BrowserSession most recently used, else the owner's most recent, else a new
one) and embeds its id as a hidden composer field; POST uses that explicit
selector (owner-validated, row-locked) or, for a selector-less legacy form,
resolves/creates the current conversation in a short separate transaction
under a per-owner membership row lock (race-safe first creation), then locks
only the conversation for the turn. A minimal unstyled
`GET /ui/agent?conversation=<id>` selector exists for reopening history.

**Concurrency:** state-changing turns hold the durable conversation row's
PostgreSQL `SELECT ... FOR UPDATE` lock for the whole turn; the session
context is created/updated under it. Different conversations remain
independently concurrent. First context creation is race-safe
(`uq_agent_conversation_session_context` + SAVEPOINT retry). No
process-global locks.

**Migration `b7e3c9d41f28` (on `543c60f7efc5`):** owners are backfilled only
through `browser_session_id → browser_sessions → (user_id,
tenant_membership_id)` when that membership belongs to the same user and the
conversation's tenant; any unattributable row aborts the migration (ownership
is never fabricated). One context per existing conversation is backfilled
from its old `browser_session_id`/`context_epoch`/`active_result_set_id`, so
an old active ResultSet remains bound only to its original session.
`agent_result_sets.conversation_id` is backfilled through the pre-migration
1:1 session link (abort on any unattributable row). New columns become NOT
NULL only after backfill, then the old live columns are dropped from
`agent_conversations`. Transcript-bearing conversations become `GENERAL`,
empty ones `NEW`. **The migration invalidates pre-existing unconfirmed
pending draft authority (`active_pending_draft_id = NULL`); HR must
re-analyse the vacancy before confirmation.** Already confirmed Jobs,
criteria versions, and `AgentDraftConfirmation` rows are untouched.
**Downgrade fails closed.** `b7e3c9d41f28` downgrade is permitted only while
ALL conversation/session/draft authority remains losslessly representable by
the previous 1:1 schema. Before any DDL it refuses (RuntimeError; the
database stays on `b7e3c9d41f28` with no column, table, transcript, pointer,
or ResultSet change) unless:

- every conversation has exactly one session context (none, or one
  conversation opened by multiple BrowserSessions, is rejected);
- every BrowserSession holds at most one conversation context;
- owner/session/tenant identity agrees: context tenant = conversation tenant,
  and the context's BrowserSession user/membership (and that membership's
  user/tenant) equal the conversation's `owner_user_id`/`owner_membership_id`/
  `tenant_id` — the old schema makes `browser_session_id` the ownership
  boundary, so a mismatch would transfer a durable conversation;
- every ResultSet's `browser_session_id`/tenant equals its conversation's
  single context (otherwise a later re-upgrade's session-based backfill would
  silently fabricate provenance);
- NO live pending-draft authority (`active_pending_draft_id IS NULL`); and
- NO transcript turn carries a `pending_job_draft` payload.

The pending-draft restriction exists because pre-#80 code treats transcript
`pending_job_draft` payloads as actionable authority (the latest payload is
the follow-up draft and any payload id is confirmable), while #80
deliberately does not — only `active_pending_draft_id` is authority. The old
schema cannot express "historical payload vs one active pointer", so a
downgrade would revive migrated-invalidated or superseded drafts. This
deliberately shrinks the safe downgrade subset (the migration's own legacy
pending-draft invalidation is itself not downgradable). Downgrade never picks
a "most recent" conversation or draft, never deletes, truncates, or rewrites
durable history, never copies draft authority into another field, and never
rebinds a ResultSet. In the representable case every conversation is
retained and the old `browser_session_id`/`context_epoch`/
`active_result_set_id` are copied from its single context. Production
rollback remains compatibility-aware release rollback under the accepted
release/schema compatibility policy, never arbitrary Alembic downgrade.

**Audit:** `agent.conversation.created` (`conversation_id`, `title_kind`),
`agent.conversation.opened` (explicit selector only — plain GET refreshes are
not audited), `agent.conversation.access_rejected` (`reason_code` only;
foreign and missing ids audited identically). `agent.result_set.created`
additionally records `conversation_id`. No transcript, query, JD, or title
text.

**Non-scope (PR80-2 and later):** visual sidebar, mobile drawer, history
styling, conversation search/deletion/rename, model-generated titles,
semantic/RAG history, shared conversations, #50, scoring changes,
deployment work.

## D-087 — PR80-2 conversation workspace presentation (issue #80)

**Status:** proposed for independent code and visual review; issue #80 remains
OPEN until owner acceptance and merge.

**Decision:** the agent page uses a two-column desktop workspace with a
bounded conversation-history sidebar and separate Candidate Library and Job
Library links. The narrow-screen drawer is presentation only. Conversation
links use the existing owner-validated explicit selector; creation remains
POST + CSRF and redirects to the newly created durable row. The composer
retains its single textarea and explicit hidden conversation selector.

History lists 20 summary projections per page, ordered by `updated_at DESC,
id DESC` under the live tenant/user/membership principal. The query selects
only id, closed `title_kind`, and `updated_at`; it never loads every transcript
or one transcript per sidebar item. Local date/time plus a fixed Azerbaijani
mapping differentiates duplicate safe category labels. The selected item is
set server-side and receives `aria-current`. An active item outside the
requested page is shown separately without changing chronology on GET.

BrowserSession remains authentication transport, not durable ownership.
Opening history never recreates ResultSet, ordinal, refinement, or pending
draft authority. JD confirmation/review controls use a server-prepared
`can_act` flag derived from the current session-context draft pointer; old
historical drafts are never made actionable by transcript rendering. There
is no raw-content or model-generated title, no conversation RAG, and no
schema migration. This PR does not implement #50.

## D-088 — Canonical professional-requirement boundary for JD drafts (issue #84)

Context: the final independent adversarial audit of `main` @
`096afb8caec18750f27499f20dc9ceeb00981102` (H-1, M-7, M-11 deterministic
part, L-11) reproduced deterministic, model-independent defects: the JD path
turned raw grammatical remainders into scoring authority
(`"PostgreSQL ilə işləməyi"` → SCORABLE SKILL that evaluates UNKNOWN for a
PostgreSQL candidate), let a one-line vacancy header leak into a criterion
(`"Vakansiya: Analitik — Python"`), offered location text as a SKILL review
item (`"Bakıda"`), let a coordinated list give siblings different authority,
and let a trailing vacancy-title number on one line plus "Namizəd ..." on the
next be parsed as a result count (JD misrouted away from drafting). A local
model draft was requested but only its title was used; the deterministic
subject was the material authority.

Decision (semantic policy `jd-semantic-policy-v2`, prompt
`jd-criteria-draft-prompt-v4`):

1. **Canonical boundary.** `meyar.agent.canonical_requirements` introduces an
   in-memory, Pydantic v2 `CanonicalRequirement` (span_id, kind,
   canonical_subject, criterion_type, min_years, required_level,
   interpretation_state, interpretation_source, review_reason,
   shape_validated). Every JD-draft `CriterionIn` is built only from a
   SCORABLE `CanonicalRequirement`. The in-memory object itself is not
   persisted; the durable per-criterion provenance record is (amendment
   A-1 below, migration `c84a5e2f9d17`). Kinds reuse the existing `JDDraftCriterionKind`
   families; no new family was added.
2. **Deterministic authority stays deterministic.** `analyze_hr_text` still
   owns spans, exact offsets, modality, duration, level, prohibited and
   unsupported classification. A deterministic subject is scorable only when
   `is_canonical_subject` accepts it (curated alias, canonical language, or a
   short clean term with no header/person tokens, function words or
   Azerbaijani verbal-noun residue). This is a fail-closed rejection check,
   not a language-understanding vocabulary: anything suspicious goes to
   review. Unknown clean professional subjects (e.g. "Camunda") stay
   scorable.
3. **Model = proposal, server = authority.** The local model (only through
   `LLMProvider`) receives the same authoritative spans plus structural
   hints (modality/family/min_years/required_level) and returns canonical
   subject proposals per span id. A proposal may close ONLY a
   subject-normalization gap (`SUBJECT_NOT_CANONICAL`,
   `RECRUITMENT_SUBJECT_WITHOUT_CUE`). It must reference a known span id
   (else counted as ungrounded, never displayed), keep the deterministic
   family, duration and level exactly, be itself canonical, not prohibited,
   and be grounded in the span's exact text (token occurrence, bounded
   Azerbaijani case suffix, or a reviewed alias of a span n-gram). One
   distinct valid proposal per span; conflicting proposals → review. The
   model never supplies modality, weight, score, offsets, permissions or new
   requirements, and cannot override PROHIBITED/UNSUPPORTED.
4. **Fallback.** Provider timeout/unavailable/schema-invalid never erases a
   requirement and never degrades to a raw remainder: canonical
   deterministic subjects stay scorable, everything else becomes review.
5. **Coordination symmetry.** Spans split from one coordinated clause carry a
   server-owned `coordination_group`; if any interpretable member is not
   scorable, no member is (`COORDINATION_SYMMETRY`).
6. **Header, non-professional, instruction-like text.** A one-line
   `Vakansiya:/Vacancy: <title> — ...` header is trimmed at segmentation.
   Location/residence, salary, work authorization/visa, remote/on-site/
   hybrid, relocation/shift/travel and system-directed instructions are
   deterministically UNSUPPORTED (visible, never scored, never a SKILL review
   shape). No location scoring was introduced.
7. **Unresolved MUST_HAVE blocks confirmation.** A NEEDS_HUMAN_REVIEW item
   whose source modality is explicitly MUST_HAVE is `blocking`;
   `agent_draft_requires_resolution` (the single UI + mutation rule) keeps
   the draft unconfirmable until HR either resolves a server-declared
   interpretation or explicitly acknowledges exclusion
   (`decision=exclude`). Exclusion never mints a criterion; the requirement
   stays disclosed on the draft and in the criteria version's
   `needs_review_requirements`.
8. **Review cannot turn text into a criterion.** A review item exposes
   kind/subject (and modality `allowed_types`) only for a server-validated
   canonical shape. `resolve_job_draft_review_modality` re-validates the
   subject (canonical, grounded, not prohibited, source not non-professional)
   before minting a CriterionIn.
9. **Result count never spans a line break** (M-7), and "show (me) the first
   three" is the same deterministic count-only follow-up as "ilk üç" /
   "first 3" (M-11). ResultSet authority (#49) is unchanged.
10. **Russian.** No Russian semantic support is claimed. JD drafts keep the
    explicit unsupported-language panel; search now shows an explicit
    "Bu dil hazırda dəstəklənmir" copy instead of the generic
    "mənasını zəiflətmədən" message.
11. **Provenance/audit.** Drafts carry `semantic_policy_version`;
    `agent.tool.executed` for a draft and `agent.draft.review_resolved` add
    only the policy version and structural counts (scorable / review /
    unsupported / prohibited / blocking). No span text, JD text or model
    output is audited.

Non-scope: Agent Core v2 (#88: task/dialogue state, resumable
clarification, capability registry), candidate Q&A (#50), Russian NLP,
target-host model selection (#36). The architecture is sector-neutral.

### D-088 amendment (review correction pass on PR #89)

A-1. **Durable semantic provenance.** The pending draft is bounded session
  state, so it cannot be the audit record for an immutable criteria
  version. One nullable JSON column,
  `job_criteria_versions.agent_semantic_provenance` (migration
  `c84a5e2f9d17`, parent `b7e3c9d41f28`, single head), stores a strict
  `jd-semantic-provenance-v1` record (superseded by v2, A-4) (`meyar.agent.semantic_provenance`,
  Pydantic v2, `extra=forbid`, frozen): draft_id, `source_sha256` of the JD,
  semantic policy version, prompt version and `{provider, model_name,
  model_revision}` only when a schema-valid local-model result was accepted
  (else null), `rejected_proposal_count`, per criterion `{criterion_id,
  span_id, start_offset, end_offset, source_text (the exact fragment),
  interpretation_source DETERMINISTIC|MODEL_VALIDATED}`, and explicit human
  `review_decisions` `{span_id, MUST_HAVE|PREFERRED|EXCLUDED_BY_REVIEWER}`.
  It never stores the full JD, raw model output, chain-of-thought or
  candidate data. Existing rows stay NULL (never fabricated). Downgrade
  refuses before any DDL while any row carries provenance. The record is
  built fail-closed at the confirm boundary from the locked server draft
  (never browser data): missing/duplicate/unknown-span provenance, criterion
  mismatch, offset/text/grounding mismatch, inconsistent exclusions or a
  missing policy version/digest → 422, nothing created. Reads go through
  `get_agent_semantic_provenance` (tenant-scoped, strictly validated). The
  scorer never reads it. `agent.draft.review_resolved` now also carries
  `draft_id`; draft audit metadata carries `rejected_proposal_count` and
  `model_result_accepted`.
A-2. **One semantic authority.** The unused legacy model-reconciliation path
  (`_build_authorized_model_draft`, `_build_criterion_from_draft_item`,
  `_canonical_binding_result`, their helpers and `meyar.agent.jd_authority`,
  a second segmenter) is removed. `tests/test_jd_source_binding.py` now
  drives `_dispatch_draft_job_criteria` — the real production path — and
  asserts canonical outcomes. Doing so exposed three pre-existing
  production gaps (present on `main` too, masked by tests of dead code),
  fixed narrowly in the single authority: bullet/numbered list items with
  no modality cue are kept as review instead of silently dropped;
  non-terminal "X is plus Y" is not a preference cue (review); a
  scope/purpose/composition qualifier in the deterministic subject
  (`for|in|within|using|via|with|on|üçün`) cannot be dropped by a model
  proposal (`SCOPE_QUALIFIED` rejection).
A-3. **Span overflow.** A JD with more than 64 source requirement spans
  previously raised a validation error in production; it now yields one
  blocking review item covering the source, so nothing can be confirmed
  and nothing is silently dropped.
A-4. **Human amendments are durable provenance (schema
  `jd-semantic-provenance-v2`).** A bounded HR follow-up that changes one
  criterion's `criterion_type`, `min_years` or `required_level` (only these
  three, closed enum) appends an immutable, ordered, server-created
  `SemanticCriterionAmendment {sequence, criterion_id, span_id, field,
  previous_value, new_value, source_text, source_sha256}`; `source_text` is
  the bounded HR follow-up that authorized it (an HR instruction, never
  candidate data or model output). Chains are kept whole (3→5→7, B2→C1→C2).
  New values are validated through `CriterionIn` before they apply. A
  follow-up may name the old value instead of the subject ("3 ili 5 et")
  only when exactly one criterion carries that value. At confirmation the
  builder re-hashes and deterministically re-analyses the session-held JD
  (the pending draft keeps it, excluded from every render/dump and from
  durable provenance), re-derives each span's offsets/text/family/modality/
  duration/level, and proves each final value = source value (+ explicit
  review decision or conflict choice) replayed through an unbroken
  amendment chain. Missing/forged source text, a value change with no
  amendment, a broken `previous_value`, a cross-criterion/span, unknown,
  duplicate or out-of-order amendment → 422, nothing created. The persisted
  record stores per criterion `origin` and `final` parameters plus the full
  chain; strict read validation replays it. v2 supersedes v1, which was
  never part of an accepted release, so v1 JSON is not accepted on read; no
  new column or migration (head stays `c84a5e2f9d17`).
A-5. **Canonical collision policy.** After canonicalization and before any
  `CriterionIn` exists, SCORABLE spans are grouped by the one central key
  `semantic_identity = (kind, normalized canonical subject)` (skill aliases
  via `normalize_skill_name`, domain via `canonicalize_domain` with
  bank→banking, language via the language alias map; `EXPERIENCE` has no
  subject). Kinds stay distinct because evaluator semantics differ (`SKILL`
  presence ≠ `SKILL_EXPERIENCE` duration). Equal `(criterion_type,
  min_years, required_level)` → exact duplicate: ONE criterion supported
  by every span (provenance lists all `source_spans`). Different parameters
  → every span of the group is a blocking `SEMANTIC_CONFLICT` review
  (non-liftable by the model), shown as one item that says the requirement
  appears with conflicting importance/parameters; no value is chosen
  automatically (no max/min, no MUST_HAVE default). HR either picks one
  server-declared option (`option` on the resolve route; recorded as a
  `SemanticConflictResolution` whose options are re-derived from the source
  at confirmation) or explicitly excludes it. A review resolution that
  would recreate an existing identity merges into it only with identical
  parameters and is refused otherwise. Confirmation also fails closed if two
  confirmed criteria share one identity.

### D-088 amendment A-6 (real-Ollama acceptance correction on PR #89)
A-6. **Canonical transport newlines and explicit vacancy title.** Real
  browser acceptance with the local `qwen3:1.7b` model reproduced (3/3)
  that `Vakansiya: Kredit Analitiki 2` + `Excel tələb olunur.` lost its
  title: the textarea submits CRLF, the model's CRLF proposal carried an
  empty `required_level`, the strict `JDCriteriaDraft` schema correctly
  rejected it, and the title was model-only. Two narrow rules, no schema
  relaxation and no model-specific handling:
  (1) `run_agent_turn` normalizes `\r\n`/`\r` to `\n`
  (`normalize_message_newlines`) before the transcript, routing, span
  offsets, hashing, the session-held source and provenance are produced, so
  transport never changes semantics and every offset/hash refers to one
  canonical source;
  (2) `semantic_requirements.explicit_vacancy_title` returns the exact source
  slice of ONE explicit `Vakansiya:`/`Vacancy:` header line (optionally cut
  at the existing explicit dash separator), bounded to 120 characters, and
  only when it overlaps no requirement span and no result-count control
  span. It is title data only: never a criterion subject, never a count.
  When present it takes precedence over the model title (exact HR source
  beats interpretation; a model can neither invent nor rewrite it). Without
  a header the existing model-title grounding rule and generic fallback are
  unchanged. No first-line heuristic. Title is not part of provenance or
  scoring.

## D-089 — Agent inference transaction boundary and overload control (issue #85)

Status: proposed with the #85 PR; not accepted until owner acceptance.

Context: the final independent audit of `main` @ `096afb8` (H-2, agent part
of L-5) and a synthetic reproduction on `650492866572` showed that
`POST /ui/agent` took the durable conversation row lock
(`SELECT … FOR UPDATE`, #80) and kept that pooled PostgreSQL connection and
transaction open for the WHOLE turn. That included the wait on the
process-wide inference semaphore (unbounded waiters, unbounded wait) and
every local-model HTTP call (up to `llm_timeout_seconds` each, several per
turn). The request session was also already in a transaction from the UI
auth dependency's reads. With a slow model, N concurrent model-bound turns
held N connections. Once the pool (SQLAlchemy defaults 5 + 10 overflow)
was exhausted, unrelated routes (`/ui/library`, candidate detail, login)
failed after `pool_timeout`, `/api/v1/health` still said ok, and queued
turns kept running long after their clients had left. Pre-fix reproduction
(`tests/test_agent_inference_boundary.py`, pool 2 + 0, concurrency 1):

- the agent request held 1 connection while the model call was blocked;
- with 22 slow turns on distinct conversations, `GET /ui/library` did not
  answer within the 5 s budget.

Decision:

1. **Transaction boundary (`meyar.agent.turn_boundary`).** No pooled
   connection, SQL transaction or row lock is held while a turn waits for
   admission or runs a local model or embedding call.
   - **Phase A** (`reserve_agent_turn[_waiting]`): lock the conversation
     row, check ownership against the live `UIContext` principal, get or
     create this BrowserSession's context, and write a server-owned
     reservation.
   - **Orchestration** (`execute_agent_turn`) runs on plain snapshots
     (`ConversationSnapshot`, `TurnSessionState`), never on live ORM
     authority.
   - `BoundaryLLM` and `BoundaryEmbedding` wrap every provider call:
     `leave_db()` COMMITs first (the connection goes back to the pool),
     and `reenter()` then re-locks and revalidates.
   - **Phase B**: `reenter()` once more, then `apply_agent_turn_commit`
     appends the (user, assistant) pair to the current transcript, sets
     the live pointers, applies the title transition, audits completion,
     and clears the reservation in one COMMIT.
     *Superseded for final agent Phase-B transaction entry by D-093 /
     D-092 §12.2 (issue #88 slice A): Phase B now always starts from a
     fresh transaction via `TurnBoundary.enter_phase_b()`.*
   - Tool-phase DB writes that commit early are only append-only audit
     events and new `AgentResultSet` rows. Those rows stay inert until a
     committed live pointer references them, so an abandoned turn leaves
     no dangling authority.
   - A turn that makes no model call never leaves its Phase A transaction.
     Deterministic-only turns keep the short, simple locked path, and any
     path that may call a model cannot carry a lock into the call.
     *Superseded by D-093 (issue #88 slice A): a no-inference turn now
     commits its Phase A before the final Phase B, which starts fresh via
     `TurnBoundary.enter_phase_b()`.*
   - `run_agent_turn` keeps its single-transaction contract for
     service-level callers (it now composes execute + apply).
   - `/ui/search` commits its read-only auth transaction before NL planning.
     The API NL-search path already committed in its auth dependency.
2. **Turn reservation / version (migration `e5d7a3c91b04`).**
   `agent_conversations` gains three columns:
   - `turn_version`, bumped by every transcript write
     (`save_conversation_turns`, `sync_last_turn_display_text`);
   - `active_turn_id` and `active_turn_expires_at`, set together (CHECK).

   The reservation token is a server-generated UUID and is never sent to
   the client. Re-entry and Phase B require the same token, an unchanged
   `turn_version`, and the same context row, `context_epoch`,
   `active_result_set_id` and `active_pending_draft_id` as at reservation.
   Every re-entry refreshes the expiry. `agent_turn_reservation_seconds`
   (default 600, validated to exceed queue timeout + LLM timeout) only
   matters when a process died mid-turn.
3. **Same-conversation serialization (#80 preserved).** At most ONE
   accepted in-flight turn per conversation. A second turn on a
   conversation with a live reservation is refused IMMEDIATELY with the
   truthful "still processing" outcome (409). Its text is shown back and
   nothing is persisted. There is deliberately no hidden wait queue, so
   ordering never depends on poll or scheduling timing; the HR user
   resends after the first turn finishes.
   - Result: no lost update, no duplicate execution of a reserved turn,
     and commit order = acceptance order.
   - Each BrowserSession keeps its own live context.
   - Different conversations never share a reservation. They contend only
     for global inference capacity, which is intentional.
   - Deterministic-only turns never release the row lock, so a concurrent
     request simply waits on that short lock (PostgreSQL lock order).
     *Superseded by D-093 (issue #88 slice A): the concurrent request
     waits only for the deterministic turn's Phase A, then sees the live
     reservation and receives the 409 "still processing" outcome above.*
4. **Bounded inference admission (`meyar.llm.concurrency.InferenceAdmission`).**
   One process-wide gate replaces the plain `asyncio.Semaphore`. It is
   shared by EVERY request-serving local-model execution:
   `OllamaLLMProvider._chat` AND `OllamaEmbeddingProvider.embed`. It is one
   Ollama daemon, so there is one capacity budget. Health checks are
   never gated.
   - Settings, all validated: `inference_concurrency` (1..16, default 1),
     `inference_queue_max_waiters` (0..64, default 4) and
     `inference_queue_timeout_seconds` (0..300, default 30). Both provider
     factories pass these same fields.
   - The singleton records its policy. A provider presenting a different
     policy raises `InferenceAdmissionPolicyMismatchError` instead of
     silently sharing, or splitting, the budget.
   - FIFO admission; a full queue is rejected immediately (`QUEUE_FULL`).
   - A waiter not admitted in time is rejected (`QUEUE_TIMEOUT`).
   - Accounting is cancellation-safe: a cancelled waiter leaves the queue,
     and a slot handed to a waiter that is cancelled in the same tick is
     given back. Only the gate's own counters are authority.
   - Refusals surface as `InferenceBusyError` (LLM) and
     `EmbeddingBusyError` (embedding), both with code `INFERENCE_BUSY`.
     This is a TRANSIENT admission outcome, deliberately distinct from
     `MODEL_UNAVAILABLE`, `MODEL_TIMEOUT` and `MODEL_SCHEMA_INVALID`:
     no model attempt happened.
   - The agent boundary maps both to `AgentInferenceBusyError`, which is
     deliberately not an `LLMProviderError`, so no planner or agent
     fallback can turn it into a degraded "successful" answer.
   - Non-agent callers:
     - **Profile and identity extraction** DEFER. They raise
       `ExtractionDeferredError`, write NO `CandidateProfileVersion` or
       `CandidateIdentityVersion`, and audit
       `CANDIDATE_{PROFILE,IDENTITY}_EXTRACTION_DEFERRED` with ids and
       codes only. A transient overload therefore never creates an
       immutable FAILED version that would supersede the accepted
       COMPLETED one (profile authority follows the highest version).
     - **Folder reconciliation** keeps any stage already completed and
       counts the candidate as `deferred`, never `failed`. It stops
       attempting further candidates in that run, and the next run
       retries exactly what is missing.
     - **Embedding creation** never persists a failure anyway. It audits
       `CANDIDATE_EMBEDDING_DEFERRED`.
     - **CLI** prints a deferral and exits non-zero.
     - **NL planner** returns `PLANNER_PROVIDER_FAILURE` with reason
       `INFERENCE_BUSY`. `attempt_count` excludes the refused attempt,
       and the UI shows the busy copy.
     - **`/ui/search` and `/api/v1/search`** map an embedding busy to the
       busy copy (503) or `503 INFERENCE_BUSY`.
     - Because the query embedding can now wait on the shared gate, the
       non-agent search routes wrap the embedding provider in
       `DbReleasingEmbeddingProvider`, which commits before `embed()`. No
       pooled connection is held while it waits or runs.
5. **Busy UX.** A rejected turn is abandoned: its reservation is cleared,
   and no transcript entry or fabricated answer is written.
   - HR sees "MEYAR hazırda digər sorğuları emal edir. Bir qədər sonra
     yenidən cəhd edin." (503). Their own text is still shown.
   - Audit records only `agent.turn.busy {reason_code}`.
   - No queue, pool, semaphore, exception or Ollama detail is rendered.
6. **Stale authority fails closed.** Any re-entry or Phase B mismatch
   raises `TurnAuthorityLostError` with a closed `TurnStaleReason`
   (`PRINCIPAL_REVOKED`, `CONVERSATION_UNAVAILABLE`, `RESERVATION_LOST`,
   `CONVERSATION_CHANGED`, `CONTEXT_CHANGED`).
   - Nothing is committed, the turn's own reservation is cleared, and
     `agent.turn.stale {reason_code}` is audited.
   - A revoked principal (disabled user or membership, revoked or expired
     BrowserSession) gets the same 303-to-login as any request after
     revocation.
   - Every other reason gets truthful copy (409): the conversation changed,
     nothing was applied, please resend.
   - Never replayed against new context, never tenant-wide, never with old
     candidate refs or revived draft authority.
7. **Cancellation and abandoned clients.** The route runs orchestration
   under `run_until_client_disconnects`, which polls Starlette
   `Request.is_disconnected()` every 0.5 s.
   - On disconnect, the work is cancelled: queued admission is left, the
     in-flight local HTTP call is cancelled, and slots are released by
     their context managers. No detached task survives; both tasks are
     cancelled and awaited in `finally`.
   - A shielded `abandon_reserved_turn` clears the reservation. If even
     that fails, the reservation expires by TTL.
   - Worst case: a queued wait is bounded by the queue timeout. An active
     call is cancelled at the next disconnect poll (≤ 0.5 s). If the server
     cannot observe the disconnect, the turn still ends within its own
     bounds (bounded model calls × `llm_timeout_seconds`, each wait ≤ the
     queue timeout). Ollama itself may finish computing a cancelled
     request server-side; MEYAR does not wait for or use it.
8. **Liveness vs readiness (boundary with #46).** `GET /api/v1/health`
   stays liveness only and never reflects AI or DB load, so a busy model
   never triggers a restart.
   - New minimal `GET /api/v1/health/ready`
     (`meyar.core.saturation.probe_saturation`) returns 200
     `{"status":"ready"}`, or 503 with closed reasons:
     - `INFERENCE_SATURATED`: all slots busy and the queue full,
       continuously for `inference_saturation_grace_seconds` (default 10);
     - `DB_POOL_SATURATED`: every pooled connection is checked out.
   - It is process-local: no DB query, no queue contents, no figures, no
     user or candidate data.
   - DB/migration/storage/model reachability readiness remains #46.
9. **DB pool policy.** `make_engine` now passes explicit, validated
   `db_pool_size` (5), `db_max_overflow` (10) and `db_pool_timeout_seconds`
   (30). These equal SQLAlchemy's previous implicit defaults, so there is
   no behaviour change, only a reviewable contract. With the boundary
   above, the pool bounds concurrent DB work, not concurrent AI work. #85
   is deliberately NOT solved by enlarging the pool.

Early-commit side effects (independent review correction pass). While a
turn waits on the model, `leave_db()` commits only the rows in this table.
Nothing else becomes authoritative before Phase B. No existing row is
updated before Phase B.

| Operation | Rows written | Committed before Phase B? | Authoritative / live? | Safe if the turn becomes stale? |
|---|---|---|---|---|
| Phase A reservation | `agent_conversations.active_turn_id/expires_at` | yes (first `leave_db`) | reservation only | yes: cleared by `abandon_reserved_turn`, else TTL |
| Session context get/create + touch | new `agent_conversation_session_contexts` row (clean: no pointers) or `updated_at` | yes | clean context only | yes: identical to opening the conversation |
| Entry routing / tool audit | `audit_events` (`agent.entry.*`, `agent.tool.*`) | yes | no (append-only) | yes: truthful record of what ran |
| NL planning audit | `audit_events` (`SEARCH_PLAN_*`) | yes | no | yes |
| Search execution audit | `audit_events` (`CANDIDATE_SEARCH_EXECUTED`) | yes | no | yes |
| Search result set | new `agent_result_sets` + members, `agent.result_set.created` | yes | NO: only the session context pointer (written in Phase B) makes a set live | yes: an inert orphan; `resolve_active_candidate_ref` requires the live pointer + session + conversation + epoch (regression-tested) |
| Refinement result set | new `agent_result_sets` (REFINEMENT) + members, `agent.result_set.refined` | yes | NO (same as above) | yes |
| Reference / refine rejection audit | `audit_events` | yes | no | yes |
| Transcript, title kind, `turn_version` | `agent_conversations` | NO (Phase B only) | yes | n/a |
| Live ResultSet / pending-draft pointers | `agent_conversation_session_contexts` | NO (Phase B only) | yes | n/a |
| `agent.turn.completed` | `audit_events` | NO (Phase B only) | no | n/a |

Non-scope: #86 (ResultSet corpus snapshot scalability), #87 (session
revocation / request integrity / PRG / idempotency), #88 (Agent Core v2), #50
(candidate Q&A), #46 (full readiness design), #20/#36 (target-host
benchmarks). No new process, worker, queue service, Kafka, Celery or Redis.
The local-only inference boundary (`LLMProvider`, loopback Ollama), tenant
isolation, BrowserSession/ResultSet/draft authority, deterministic scoring
and CandidateIdentity exclusion are unchanged.

## D-090 — ResultSet = immutable member snapshot; bounded validation and retention (issue #86)

Status: proposed with the #86 PR; not accepted until owner acceptance.

Context: the final independent audit (H-3, M-1, ResultSet part of L-6)
and a synthetic pre-fix reproduction on `7e456a4`
(`tests/test_agent_result_set_scale.py`) found three problems.

**1. Validation cost grew with the tenant.** Every ResultSet validation
recomputed a tenant-wide corpus fingerprint: it listed every current
profile, authorized each one (one `canonical_documents` query per profile)
and, for semantic/hybrid, ran a tenant-wide embedding lookup. The same
bounded 10-member ResultSet cost:

| Operation | 16 candidates | 2,000 candidates |
|---|---|---|
| ordinal reference | 22 SQL statements | 2,006 SQL statements |
| `active_result_set_size` | 18 | 2,002 |
| refinement | 42 | 2,026 |

At 2,000 candidates each operation took about 1.7 s (local development
measurement, not a Target Mac benchmark). Creating a ResultSet also
rescanned the tenant after search.

**2. Unrelated changes staled everything.** Any unrelated ingestion or
re-extraction invalidated every live ResultSet in the tenant (M-1).

**3. No retention.** ResultSet and member rows grew without bound.

Decision:

1. **ResultSet = immutable search snapshot.** An `AgentResultSet` is the
   ordered set of members that one accepted search or refinement returned
   at that time. It is not a live mirror of the tenant's future corpus.
   The following do not stale an existing ResultSet:
   - a new candidate is ingested;
   - another candidate is reprocessed, fails extraction, or gets a new
     embedding;
   - any CandidateIdentity change.

   To include newer or improved candidates, HR runs a NEW search. This is
   explicit snapshot semantics, not stale data.
2. **Validation = ownership + member authority.** No tenant-wide
   fingerprint is computed. `compute_corpus_fingerprint` and its
   embedding helper are removed; nothing replaces them with a cache or
   another tenant scan.
   - `_validate_active_result_set` checks only structure: tenant,
     BrowserSession, durable conversation, `context_epoch`, the active
     pointer and expiry.
   - `validate_member_snapshots` checks each referenced member's own
     authority. A member is valid only while ALL of these hold:
     - the candidate still exists in the tenant;
     - its CURRENT profile version (max `version_number`) is exactly the
       recorded `candidate_profile_version_id`. A newer COMPLETED, FAILED
       or manual-review version makes it stale, and the member is never
       served under its old ordinal;
     - that version still passes the SAME professional evidence authority
       as `authorize_profile_version`. The single-version and batch paths
       share one implementation (`_verify_against_canonical`); there is no
       weaker "fast" authority.
     - **SEMANTIC_ONLY / HYBRID only:** the exact recorded embedding row
       still exists and matches all of: the member's candidate and profile
       version; the ResultSet's persisted `EmbeddingSearchConfig`
       (provider, model, revision, serializer, dimensions); and the source
       hash of the recorded profile's canonical professional text. It is
       never "the latest embedding".
   - STRUCTURED_ONLY ResultSets never look at embeddings.
   - CandidateIdentity is never read.
   - Hard delete keeps the no-FK member snapshot pattern: a deleted member
     fails closed as STALE, and its historical row is never rewritten.
3. **Bounded query shape** (asserted in the scale tests: identical counts
   at 16 and 2,000 candidates, and no tenant-wide
   `GROUP BY candidate_profile_versions.candidate_id` scan):
   - **Ordinal reference: 5 statements.** ResultSet, member, the current
     profile for that candidate, its canonical document, and the audit
     insert.
   - **Refinement: 9 statements** (8 before the PR #91 correction pass).
     ResultSet, ordered members, current profiles for all members (one
     bounded `IN` using `DISTINCT ON`), their canonical documents (one
     `IN`), the inserts and audit, then the two LIMITed retention id
     selects (item 9). A pass that actually retires rows adds one
     `DELETE ... RETURNING` and one batched audit `INSERT`. Over a
     2,000-row historical backlog the whole turn is 10 statements.
     Semantic/hybrid adds one bounded embedding query.
     Member counts are bounded by `MAX_SEARCH_LIMIT=100`.
   - Search itself (`search_candidates`) still scans current profiles;
     that remains out of scope, as the issue states.
4. **`active_result_set_size` is advisory.** It checks only structure and
   returns the stored bounded `result_count` (1 statement). It only limits
   the candidate refs shown to the model. Actual authority is always
   re-checked when a ref is resolved, which fails closed for a stale
   member.
5. **Refinement = subset of THIS snapshot.** "Refine these results" is
   never a fresh tenant-wide search.
   - The pre-validation (before any planner call) and the persistence
     path both batch-validate every source member.
   - If any member is stale, the WHOLE refinement fails as STALE.
   - Otherwise it filters the existing members in their existing order and
     never admits a candidate from outside the parent.
   - Unrelated new candidates never make a refinement stale. For latest
     corpus completeness HR runs a new SEARCH.
6. **Truthful copy.**
   - Member stale: "Bu axtarış nəticəsindəki məlumatlardan biri sonradan
     dəyişib. Dəqiq nəticə üçün axtarışı yenidən aparın."
   - Expiry keeps its existing copy.
   - An unrelated corpus change shows no error.
   - A member that no longer passes evidence authority is now STALE, where
     it was previously NOT_FOUND. That is still fail-closed, and still
     never reveals whether a foreign ResultSet exists.
7. **Creation stops rescanning the tenant.** `create_result_set_from_search`
   persists the member snapshot directly from the accepted
   `CandidateSearchResponse`: candidate id, profile version id, embedding
   version id, rank/scores and search mode/config.
8. **Schema (migration `f3a9c6d2e815`).**
   - New column `agent_result_sets.snapshot_policy_version`
     (`member-snapshot-v1`, NOT NULL).
   - `corpus_fingerprint_sha256` becomes nullable and LEGACY: new rows store
     NULL, and old rows keep their historical value, unused.
   - Backfill: every existing row is set to `member-snapshot-v1`. This is
     representable because pre-#86 rows already persist, per member, the
     immutable candidate, profile-version and embedding-version snapshot
     references plus their own `EmbeddingSearchConfig`.
   - Downgrade fails closed. It refuses, before any DDL, while any row has
     a NULL fingerprint, because the pre-#86 code would need a fabricated
     fingerprint. A database with only legacy rows round-trips.
9. **Retention (no daemon), exact contract.** Per (tenant, conversation,
   BrowserSession): **1 active + at most 5 inactive** ResultSets
   (`MAX_INACTIVE_RESULT_SETS_PER_CONTEXT = 5`). "Active" is the set this
   scope's own session context points at. A set some OTHER live session
   context points at is independently protected: it is never deleted and
   never counted. (Owner decision in the PR #91 acceptance pass; the first
   revision allowed 1 + 6.)
   - **Creation-time pass.** Every search or refinement runs ONE bounded
     pass after creating its set. `keep_ids` holds the new set and the
     superseded/source set, and only `MAX - 1 = 4` other unexpired inactive
     sets are kept. One slot is reserved for whichever of {new, superseded}
     ends up inactive:
     - Phase B pointer switch succeeds: new set active; superseded + 4
       inactive.
     - Switch fails or goes stale: old set still active; new set + 4
       inactive.
     - Either way the result is 1 active + ≤ 5 inactive.
   - **Bounded execution (SQL, never Python slicing).** A pass selects at
     most `RESULT_SET_RETIRE_BATCH_SIZE = 20` ids:
     1. expired unprotected rows first (`ORDER BY expires_at LIMIT 20`);
     2. then fills the rest of the batch with unexpired rows ranked below
        the newest kept slots (`ORDER BY created_at DESC OFFSET slots
        LIMIT rest`).

     It loads ids only, never ORM rows or members. It then runs one
     `DELETE ... WHERE id IN (...) AND id NOT IN (live pointers) RETURNING
     <structural columns>` and one batched audit `INSERT`. A normal turn
     therefore retires at most one batch, whatever the backlog. Measured
     over 2,000 historical rows: 0 backlog ORM rows loaded, 20 retired,
     20 audit rows, 10 statements for the whole turn. The 2eb6042 code
     loaded 2,000 rows, retired 1,995, wrote 1,995 audit rows and issued
     2,004 statements.
   - **Backlog drain.** The exact bound holds per turn once no backlog
     remains. Normal turns converge one batch at a time. The agentless ops
     command `meyar retire-result-sets --tenant-id <uuid> [--max-batches
     N]` drains a tenant's backlog in committed batches
     (`retire_next_result_set_batch`):
     - one explicit tenant only; there is no global/wildcard mode;
     - output is counts only (batches / retired / pending);
     - each batch keeps the full 5 inactive slots, and the scope's active
       set is pointer-protected.
   - **Audit truthfulness.** Each row the DELETE actually removed in the
     same transaction (from `RETURNING`) emits exactly one
     `agent.result_set.retired` event. A row that became pointer-protected
     between selection and delete is neither deleted nor reported.
   - Members cascade through their own FK. `parent_result_set_id` is a
     plain UUID, and a derived set carries its own members, so retiring
     its parent never affects it.
   - Transcript and AuditEvents are never deleted.
   - No new migration: the existing `conversation_id` index scopes the
     LIMITed selects.

   Retirement event metadata is safe structural provenance only: ids,
   kind, parent, conversation, epoch, request and refinement hashes,
   policy versions, mode, result count, and created/expires timestamps.
   Query text, canonical request JSON, CV content and CandidateIdentity are
   never recorded.

   ResultSets of BrowserSessions that are never used again stay bounded per
   scope and disappear with their BrowserSession (CASCADE). Purging
   BrowserSession rows is general retention (#46).
10. **#85 interaction.** All ResultSet validation and pruning is short
    DB-phase work: bounded, never tenant-wide, and run inside the existing
    phases. The no-DB-during-inference invariant and the Phase B
    revalidation are unchanged.

11. **Snapshot policy fails closed.** Structural validation
    (`_validate_active_result_set`) requires
    `snapshot_policy_version == SNAPSHOT_POLICY_VERSION`
    (`member-snapshot-v1`). It checks this only after the ownership checks
    (tenant, BrowserSession, conversation, epoch, expiry), so a foreign
    row's policy is never observable. Any other value gives the typed
    internal failure `UNSUPPORTED_SNAPSHOT_POLICY`:
    - `active_result_set_size` returns 0;
    - ordinal resolution fails before any member is read;
    - refinement fails in pre-validation, before the planner runs, and
      creates no derived set;
    - outward it is the existing RESULT_SET_STALE "re-run the search"
      outcome;
    - the audit event carries only the closed reason `STALE` plus the
      context epoch.

    The raw policy string is never exposed or logged.

Non-scope: #87, #88, #50; general search performance; general orphan and
BrowserSession retention (#46); distributed caches.

## D-091 — Issue #87 proposal: session revocation and agent request integrity

**Status:** Proposed on `feat/87-session-request-integrity`; not accepted or
merged. Accepted baseline: `ac9a249359c9a5dcf417b4e3f269a2c0b92b3ab0`.

1. **Session security changes.** `set_password` updates the Argon2id hash and
   revokes all BrowserSessions for that user in one transaction. Disabling a
   user does the same. Disabling a membership revokes only BrowserSessions
   tied to that membership. Re-enabling never clears `revoked_at`. Demo
   credential rotation uses the same `set_password` path only after the
   existing positive synthetic-demo identification. Machine API-key auth is
   unchanged.
2. **Pending tenant choice.** Direct session revocation cannot revoke a
   stateless token issued after password verification but before a
   BrowserSession exists. User and membership `security_version` UUID stamps
   rotate on their respective security changes. Membership disable also
   rotates the user's pending-login stamp without revoking sessions on the
   user's other memberships. A signed 5-minute pending
   claim binds both, and tenant selection reloads live User and membership
   state before minting a session. Existing sessions remain truthful on
   upgrade; no old pending-token format is accepted after deployment.
3. **Failed login audit.** `AuthSecurityEvent` is tenant-independent because
   unknown usernames cannot truthfully be placed in tenant-owned
   `AuditEvent`. It stores a closed outcome code, nullable known-user UUID,
   and timestamp only. Unknown username, wrong password and disabled user
   share browser copy. Submitted unknown usernames, passwords, cookies,
   tokens, CSRF material and raw forms are absent. Tenant-selection failures
   have structural codes. Successful tenant-scoped `AuditEvent` remains.
4. **Canonical text.** Both agent and classic search normalize CRLF/lone CR
   to LF before counting. The HR limit is 4000 canonical characters,
   inclusive; no truncation. Form transport has a separate 8192-character
   ceiling, enough for any browser-valid textarea and a 4001-character
   all-newline diagnostic. The agent preserves over-limit canonical text in
   an escaped authenticated workspace and does no inference or mutation.
5. **Agent submission lifecycle.** GET/render issues an unguessable,
   server-owned `AgentTurnSubmission` scoped to tenant, owner user,
   membership, BrowserSession, durable conversation and context epoch. The
   browser token is only a request identity, never authorization. The
   existing #85 conversation reservation is still the concurrency
   authority. Phase A checks owner and canonical SHA-256 request hash and
   changes ISSUED to PROCESSING in the reservation transaction. Phase B
   revalidates principal, reservation, context and submission, then commits
   transcript/live pointers, COMPLETED and reservation clear atomically.
   A same-token PROCESSING duplicate is 409; COMPLETED replay redirects to
   canonical GET; different bytes and foreign context fail closed. Fresh
   token with identical text is a new intentional turn. Busy, cancellation,
   and stale non-commit paths terminate the old identity as ABANDONED in
   shielded cleanup; a rendered retry uses its new issued token.
   "Phase B revalidates principal" means principal authority is
   transactionally serialized against credential/session revocation for the
   final consequential commit: every re-entry (including Phase B) locks
   User -> TenantMembership -> BrowserSession `FOR SHARE`, in that order,
   before the conversation/context rows, and holds them until that short
   transaction commits or rolls back. Security mutators write the same rows
   in the same order (`set_password`, `set_user_active(False)`: User ->
   BrowserSession; `set_membership_active(False)`: User -> Membership ->
   BrowserSession; login/tenant selection: User -> Membership; logout:
   BrowserSession only), so either the security change commits first and
   Phase B waits, then fails closed with `PRINCIPAL_REVOKED`, or Phase B
   holds the rows first and the security change waits until the turn has
   committed, then revokes the session for every later request. A
   revocation can never commit between the check and the turn's commit.
   `FOR SHARE` blocks those UPDATEs but not the `FOR KEY SHARE` locks FK
   inserts take, nor other turns' re-entry, avoiding new lock inversions.
   No locks are held across inference (the boundary commits first); no
   `security_version` comparison is added to Phase B and no schema change
   is needed. Ordinary non-agent in-flight requests get no new guarantee.
6. **Browser semantics and retention.** Durable replay protection is used
   instead of true PRG for the first rich result, avoiding duplicate
   persistence of rendered CV/model content. Refresh/back resubmission is
   harmless because completed POST replays redirect. After a successful rich
   render, progressive JavaScript replaces the history entry with the canonical
   GET URL; this improves reload/forward UX without becoming replay authority.
   Submission rows expire after 24 hours. Expired non-processing
   rows are retired in bounded lazy batches of 20; expired PROCESSING rows
   are collectible only after their own lease and conversation reservation
   expire. A crash before Phase B leaves a bounded lease, not a completed
   request; retry after expiry can reclaim the uncommitted intent. Active inference
   holds no DB connection. CSRF is checked independently before submission
   claim. Revoked BrowserSessions cannot use their submissions.
7. **Migration and rollback.** Revision `a87d4c6e2b19` follows
   `f3a9c6d2e815` as one head. It adds only two stamps and the two
   security/request tables. No token/request history is fabricated for old
   conversations or sessions. Downgrade refuses to erase nonempty durable
   auth/submission tables, and revokes all BrowserSessions before removing
   security stamps when representable. #88 and #50 remain out of scope.

## D-092 — Agent Core v2 task/dialogue/capability architecture (issue #88)

**Status: ACCEPTED DESIGN / IMPLEMENTATION NOT STARTED.** Independently
reviewed and accepted at design head `dfc7636b1c0b788024c28d17f2b913784b5a897c`.
No code exists yet. #88 remains OPEN, and implementation begins only after
the owner merges the design PR. Full design: `docs/AGENT_CORE_V2_DESIGN.md`. Audited baseline:
`main` @ `fb03477a4b4c3ec4698b7294f2589fbbbeafa4e7` (#84–#87 closed).
#88 stays open; #50 is out of scope.

**Amendment A1 — clarification append-position binding (ACCEPTED AMENDMENT /
IMPLEMENTATION NOT STARTED; owner selected "Variant 1", separate docs PR
#94, before slice A).**
- *Status.* A1 was independently reviewed and accepted. Slice A
  implementation has NOT started. #88 remains OPEN, and #50 remains OPEN and
  out of scope. Slice A may begin only after the OWNER manually merges PR #94
  and post-merge verification succeeds.
- *Finding.* The slice A audit of `main` @ `3ac42a5` shows that the lane-B
  human routes rewrite transcript entries in place and bump `turn_version`
  (D-089):
  - confirm, via `mark_pending_job_draft_confirmed`;
  - review-resolve, via `replace_pending_job_draft`.

  The original liveness rule (`turn_version == created_turn_version`) would
  therefore stale an open lane-A clarification whenever HR confirms or
  reviews D1. That contradicts item 2 ("confirmation never cancels lane A").
- *Change.* Clarifications gain `question_turn_id` (NOT NULL), the
  server-issued `turn_id` of the assistant entry that asked. A clarification
  is live only while that entry is the **last** transcript entry, with the
  source user turn (`source_turn_id`) directly before it for
  SEARCH_OR_VACANCY. All other checks are unchanged: pointer, OPEN, TTL,
  epoch, source hash/offsets, policy versions.
  - Any appended turn (another tab or session) still stales it.
  - In-place rewrites (D-045 display sync, lane-B confirm and review) do not.
  - Every transcript writer must preserve entry `turn_id`, order and role.
  - `created_turn_version` stays as provenance only. It records the final
    committed `turn_version` after the D-045 sync.
  - `source_turn_id`, `source_sha256` and offsets are required for
    SEARCH_OR_VACANCY and NULL for VACANCY_SOURCE_REQUIRED.
- *Rejected alternatives.*
  - Stop bumping `turn_version` in lane-B routes: this weakens the #85 /
    D-089 invariant.
  - Keep the equality rule and let lane-B actions stale lane A: this
    breaks lane independence.
- No other architecture changes, and implementation has not started.

**Amendment A2 — exchange-chain liveness for the UNCLEAR retry (ACCEPTED
AMENDMENT / IMPLEMENTATION NOT STARTED; owner-specified rule, separate docs
PR #96 before slice A; corrected per review).**
- *Status.* A2 was independently reviewed and accepted. Slice A
  implementation remains PAUSED. #88 remains OPEN, and #50 remains OPEN and
  out of scope. Implementation resumes only after the OWNER merges PR #96,
  post-merge verification succeeds, and the recurring CI pytest hang is
  investigated separately.
- *Finding.* A1's adjacency clause ("the entry immediately before the
  question is the source turn") cannot hold for attempt 2. The tail is then
  `[U1 source, Q1, U2 unclear answer, Q2]`, so every retry would be born
  stale. That contradicts the accepted 2-attempt retry (§6.4 step 5, T5).
- *Rule.* For the current clarification C at attempt n:
  1. C's `question_turn_id` is the latest appended transcript entry.
  2. The original source binding never moves to an unclear answer. For
     VACANCY_SOURCE_REQUIRED it stays NULL at every attempt.
  3. Between the chain start and C's question only the server-recognized
     retry chain may exist:
     - SEARCH_OR_VACANCY: `[U1, Q1]`, then `[U1, Q1, U2, Q2]`;
     - VACANCY_SOURCE_REQUIRED: `[U0, Q1]`, then `[U0, Q1, U2, Q2]`.
  4. Attempt 2 is validated structurally from persisted rows:
     - the predecessor P has `superseded_by_id = C.id`, status SUPERSEDED,
       `superseded_reason = UNCLEAR`, and attempt 1 → 2;
     - P and C share task, session context and type;
     - `T[-4]` = `P.created_from_turn_id`, `T[-3]` = `P.question_turn_id`,
       `T[-2]` = `C.created_from_turn_id` (the UNCLEAR answer appended by
       `C.created_by_submission_id`), `T[-1]` = `C.question_turn_id`;
     - SEARCH_OR_VACANCY additionally requires the identical, valid source
       binding on P and C.
  5. Any foreign appended entry in the chain makes C stale.
  6. Bounded: at most four tail entries and one predecessor row; no
     backward scanning or semantic history search.
  7. Max 2 attempts. Attempt 2 UNCLEAR → EXPIRED, task FAILED_SAFE, pointer
     cleared.
- *`created_from_turn_id` (NOT NULL).* The server-issued user turn whose
  processing created this clarification attempt.
  - SEARCH_OR_VACANCY attempt 1: equals `source_turn_id`.
  - VACANCY_SOURCE_REQUIRED attempt 1: the trigger turn U0
    (`source_turn_id` NULL).
  - Attempt 2 of either type: the UNCLEAR answer U2.

  It is sequencing provenance only, never source authority, and is server-
  selected only. U0 and U2 are never JD/search sources. The
  VACANCY_SOURCE_REQUIRED JD source is only the qualifying current message.
- *VACANCY_SOURCE_REQUIRED keeps the 2-attempt policy.*
  - Qualifying source (material requirement, useful multi-line structure,
    or FORCE_JOB_DRAFT) → resolve via `SOURCE_MESSAGE`.
  - FORCE_CANDIDATE_SEARCH or FORCE_RESULT_LIMIT → new task (supersede,
    `NEW_TASK`).
  - Otherwise → deterministic UNCLEAR. No model is called.
- *`superseded_reason`* is closed: `UNCLEAR` or `NEW_TASK`, non-null ⇔
  SUPERSEDED. Both are real persisted transitions of the clarification.
- *`SOURCE_MESSAGE`* (accepted in review) is the deterministic server-side
  resolution source used only when a VACANCY_SOURCE_REQUIRED clarification
  is satisfied by the user's current qualifying source message. It is
  distinct from BUTTON, LABEL and MODEL, and never implies model-authored
  source text.
- *Classifier infrastructure/contract failure is not UNCLEAR.* UNCLEAR is
  only a valid closed UNCLEAR result. INFERENCE_BUSY, timeout,
  transport/provider error, Ollama unavailable, malformed output after the
  one repair, and any other classifier execution failure all use the
  #85/#87 abandon semantics:
  - nothing is appended, so the clarification is not staled;
  - no supersession, no attempt increment, no attempt 2;
  - no task change;
  - the submission ends ABANDONED;
  - the clarification stays live for the retry.
- *A stale/foreign/mismatched button is a rejected request, not a
  transition.*
  - Nothing is appended.
  - The live clarification, pointer and attempt are unchanged.
  - No capability runs, and the submission ends ABANDONED.
  - Bounded `agent.clarification.rejected` audit (`NOT_ACTIVE` /
    `INVALID_CHOICE`).
- *Unchanged.* `created_turn_version` stays provenance only, `turn_version`
  stays the #85/D-089 concurrency authority, and `question_turn_id` stays
  the current-question append-order authority. A1 lane independence is
  unchanged.
- Implementation has not started (see *Status* above).

**Amendment A3 — Slice-C authority reconciliation (ACCEPTED AMENDMENT;
MERGED through docs PR #103, before any correction of slice-C PR #102).**
- *Status.* ACCEPTED — independently reviewed and accepted, and MERGED
  through PR #103. Accepted PR head
  `7ab7056794f4f693d9b974cba7f6a01f7abd908e` (tree
  `78ce6e29634aa370f3bdabc0f22d7862bb378558`); squash commit on `main`
  `d9e96c06169d9ebf25ab6f276eb63bab4c5d4828` (single parent
  `9817711c73e9d2701c0385d2bbe0c1bd3fba153e`); the squash tree equals the
  accepted-head tree exactly. Accepted exact-head CI run `36965800982`
  SUCCESS (3073 pytest passed; 10 hang diagnostics passed). Docs only: no
  source, test, migration or dependency change; Alembic head stays
  `b88a2c4d6e10`. Slice C is implemented in PR #102 (head
  `4e0daf2191b563c49315139ad7a01536dbe41f56`) but is NOT accepted and NOT
  merged; D-095 belongs to that unaccepted PR and is NOT accepted authority.
  #88 stays OPEN until slice C is accepted and merged; #50 remains OPEN and
  out of scope.
- *Finding.* Independent review of the slice-C implementation found three
  normative reconciliation issues in D-092 itself:
  1. §15 sends the bounded `(role, text)` history "unchanged" AND requires
     CandidateIdentity to be structurally absent from the projection. D-045
     overwrites the persisted assistant text with the rendered HR-facing
     headline, which can name a candidate, and no identity-free original
     assistant text is retained. Both literal requirements cannot always
     hold together.
  2. §10.2 rule 3 requires each material requirement's subject occurrence to
     overlap a grounded span, but deterministic analysis represents a
     coordinated subject ("Python və Java") as ONE occurrence, so the
     normative §22 item 25 case (quote only "Python" →
     SOURCE_COVERAGE_INCOMPLETE) is not enforceable by plain overlap. The
     coverage-source set also needs to be stated precisely.
  3. RANK_JOB_CANDIDATES is model-proposable with live context
     CONFIRMED_JOB_IN_SESSION, but the durable model permits several
     `AgentDraftConfirmation` rows per BrowserSession and has no
     authoritative single "current confirmed job" pointer. Choosing "latest
     confirmation" or scanning transcript/history would be a NEW selection
     authority D-092 never accepted.
- *A3.1 Privacy-safe bounded model history (supersedes §15 for assistant
  turns only).* For `propose_agent_plan`:
  - USER turns: the canonical bounded user-authored text, as today, within
    the existing `agent_max_context_turns` limit.
  - ASSISTANT turns: persisted HR-facing display text is NOT sent, because
    it can carry CandidateIdentity. Only a closed privacy-safe server value
    sufficient for dialogue continuity is projected — in v1, the turn's
    `AgentTurnOutcome` code.
  - CandidateIdentity, candidate UUIDs, and names/contact fields derived
    from candidate records remain structurally absent.
  - No identity redaction by heuristic name matching. No conversation RAG.
  - This intentionally supersedes §15's literal "unchanged `(role, text)`
    pairs" for assistant turns only. Human-visible transcript storage and
    display (D-045) are unchanged.
- *A3.2 Coordinated requirement coverage precision (clarifies §10.2 rule
  3).*
  - When deterministic semantic analysis represents a coordinated material
    subject as one occurrence (e.g. "Python və Java"), coverage is required
    for EACH deterministic coordinated part. The closed coordinator set may
    be implementation-policy-versioned and must stay deterministic and
    bounded. This is what makes §22 item 25 ("Python və Java bilən" →
    quote only "Python" → SOURCE_COVERAGE_INCOMPLETE) enforceable.
  - Material-requirement coverage sources are exactly: SEARCH/REFINE
    grounded semantic source spans; the grounded candidate reference span;
    the grounded evidence topic span.
  - A result-count `limit_quote` is NOT a material-requirement coverage
    source; it is numeric workflow grounding only.
  - WHOLE_MESSAGE still trivially covers the message. Protected-attribute
    anti-laundering (§10.2 rule 4) is not weakened.
- *A3.3 RANK_JOB_CANDIDATES chat affordance deferral (supersedes only the
  statements that RANK is model-proposable from CONFIRMED_JOB_IN_SESSION:
  §8.1/§9 RANK row, §11.1 lane selection, §21, §22 item 8 as it applies to
  RANK, §27 answer 6).*
  - RANK_JOB_CANDIDATES remains a registered HUMAN_ACTION_ONLY capability.
    It never executes in an agent turn. The model supplies no job id.
  - Existing authenticated CSRF ranking routes and deterministic scoring
    are unchanged; the direct Job/ranking UI remains fully available.
  - Until MEYAR has an explicit server-owned, unambiguous "current confirmed
    job in this BrowserSession/conversation" selector or pointer,
    RANK_JOB_CANDIDATES is NOT model-proposable and NOT offered in Agent
    Core v2 chat.
  - Do NOT choose the "latest `AgentDraftConfirmation`" implicitly. Do NOT
    scan the transcript to manufacture ranking authority. A3 adds no
    migration and no pointer.
  - A future chat ranking affordance needs its own reviewed authority
    decision before RANK becomes model-proposable.
  - CREATE_JOB is unchanged: HUMAN_ACTION_ONLY, offered only with the live
    PENDING_DRAFT authority.
- *Consequence for slice C.* A3 is accepted and merged; the slice-C
  implementation (PR #102) must now be corrected to:
  - keep the privacy-safe assistant outcome-code projection (A3.1);
  - keep coordinated-part coverage (A3.2);
  - exclude LIMIT grounding from requirement coverage (A3.2);
  - make RANK registry model-proposability consistent with A3.3 instead of
    a `model_proposable=True` capability that production permanently hard
    codes away;
  - preserve all existing direct ranking routes.
- No other part of D-092, A1 or A2 changes.

1. **Authority.** The hierarchy (DB ownership > live BrowserSession/ResultSet
   /mutation authority > deterministic services > capability execution >
   evidence > structured task state > transcript/model context > model) is
   preserved. The model proposes goals, bounded plans, ordinals and closed
   clarification answers. It never authorizes candidates, ResultSets,
   drafts, tenant/session, scopes, scores, dates or confirmation.
2. **State.** Two new BrowserSession-context-bound tables:
   - `agent_tasks`: closed task_type/status/phase plus a versioned
     `AgentTaskStateV1` with closed codes only. A row is persisted only when
     a turn ends waiting.
   - `agent_clarifications`: source-bound by transcript `turn_id` +
     SHA-256 + exact offsets + policy versions + `created_turn_version`.
     No text is copied.

   There is one new live pointer, `active_clarification_id`, on the session
   context. Plans are not persisted; plan provenance goes to audit. Nothing
   survives into a new BrowserSession as live authority.

   **Two independent waiting lanes** exist per session context and may
   coexist:
   - the dialogue lane: ≤1 WAITING_CLARIFICATION task, pointer
     `active_clarification_id`;
   - the mutation-confirmation lane: ≤1 WAITING_CONFIRMATION vacancy task,
     pointer `active_pending_draft_id`.

   Clarifications, searches and lookups never cancel lane B. Only a new
   pending draft replacing the pointer does (T11). Confirmation never
   cancels lane A. With both lanes live, clarification resolution runs
   before the pending-draft amendment branch. The model sees only
   `waiting_clarification` {task_type, phase} | null and
   `pending_vacancy_confirmation` {present}, never ids.
3. **Resumable clarification (M-8).**
   - SEARCH_OR_VACANCY and VACANCY_SOURCE_REQUIRED are server-typed.
   - The next message is resolved in a fixed order: button (exact) → strict
     whole-message closed-label table → clear new task (supersede) → local
     classifier returning exactly one allowed value, NEW_REQUEST or UNCLEAR.
   - UNCLEAR re-asks (≤2 attempts) and never executes.
   - A resolved answer resumes the **original bound source** through the
     existing forced search / JD-draft paths.
   - A clarification is answerable only on the next **appended** transcript
     turn (A1: its `question_turn_id` is still the last entry, directly
     preceded by the source turn), within its TTL (default 1800 s), in the
     same session context and epoch. Superseded and expired clarifications
     are terminal.
4. **Capability registry.**
   - A strict `CapabilityName` enum and a `CapabilityDefinition` covering
     schemas, scopes, real side effect, execution mode, proposability, live
     context, confirmation, content and identity policy, and a server-bound
     executor.
   - Each Ollama call gets an enum subset of what is offered.
   - The initial seven capabilities wrap the existing services unchanged.
   - CREATE_JOB and RANK_JOB_CANDIDATES are `HUMAN_ACTION_ONLY`. Ranking
     writes Evaluation rows, so it is recorded as DERIVED_RECORDS, not
     read-only.
5. **Plans.**
   - `agent-plan-v1` with `extra=forbid`: no ids, scopes, confirmation,
     dates, scores, and no model-authored search/filter/topic text or
     numbers.
   - At most 3 steps (`min(3, agent_max_tool_calls)`).
   - One planner call plus one repair, and no post-tool re-decision loop.
   - **Source grounding.** SEARCH/REFINE inputs are the whole canonical
     message or exact, unique quotes of it. The server resolves offsets and
     hashes. Every material requirement must be covered by some grounded
     span, prohibited content forces WHOLE_MESSAGE, limits and ordinals are
     parsed by closed server parsers from grounded quotes, and the evidence
     topic is a grounded quote or absent. The planner still normalizes
     source-bound text into filters; faithfulness is enforced before it.
   - **Validation has two layers.**
     - Layer 1 is static and whole-plan: schema, offered capabilities,
       length, duplicates, scopes, task type, grounding, prohibited content,
       dependency shape, pre-existing live context, HUMAN_ACTION_ONLY. Any
       failure means **zero execution**.
     - Layer 2 is dynamic and runs before each step: a producer succeeded,
       the actual count supports the ordinal, ResultSet/member authority is
       current, and the executor's own checks pass.
   - **v1 plans are atomic.** A later step failing gives `PLAN_INCOMPLETE`.
     No plan-produced pointer is activated (inert ResultSets stay inert),
     no successful task transition is recorded, the turn completes with
     truthful fixed copy, and bounded attempt audit is kept. Step
     criticality is deferred.
6. **Transactions.**
   - The #85 boundary is unchanged: no DB connection or lock is held during
     inference.
   - Task and clarification changes are staged in `AgentTurnCommit`.
   - Phase B locks in the order principal → conversation → context →
     clarification → task → submission and commits everything once.
   - #87 replay protection is preserved, with UNIQUE submission-id columns
     as the backstop.
7. **Deterministic rules.** Rules are kept for safety and authority
   boundaries. `intent_routing.py` regexes and `_FOLLOWUP_RE` are frozen as
   the general-understanding layer and do not grow. The capability layer is
   language-neutral. Claimed input languages are AZ, EN and mixed AZ/EN;
   Russian is not claimed.
8. **Migration shape.** Additive tables, a column, and CHECK/partial-unique
   constraints after `a87d4c6e2b19`. No history is fabricated. Downgrade is
   representable, drops only session working state, and keeps pending-draft
   authority intact. No revision is assigned in the design PR.
9. **Slices.** Implementation proceeds in three slices, only after owner
   merge of the accepted design:
   - A: persistence + resumable clarification;
   - B: registry + validator, behaviour-preserving;
   - C: model plan contract + full grounding contract + retirement of
     `AgentDecision` and the loop.

   Slice B MUST pass WHOLE_MESSAGE instead of the model's `search_query`
   for model-routed searches.

## D-093 — Issue #88 slice A implementation record (D-092 + A1/A2)

**Status: IMPLEMENTED, technically accepted by independent review (code
head `195387bddaf7f1f4b6d11aa3c2dd56e070478bf6`) and MERGED through PR #98
(accepted head `4b27e6a87cebed083fdeb0cfa4afbf8c99b5c2c6`, squash commit
`a9b8ff39746aa5767f8ad2b3b95809db5b1b9bc4` on `main`; accepted tree ==
squash tree).**
D-092, A1 and A2 semantics are unchanged; this entry records only the
implementation choices the accepted design left open. #88 stays OPEN
(slices B and C remain); #50 is out of scope.

- **Schema.** Migration `b88a2c4d6e10` (after `a87d4c6e2b19`, single head)
  adds `agent_tasks`, `agent_clarifications` and
  `agent_conversation_session_contexts.active_clarification_id` exactly as
  §4.3/§18, with CHECKs for every closed value, the type-dependent source
  binding, attempt-1 `created_from_turn_id = source_turn_id`, and
  `superseded_reason ∈ {UNCLEAR, NEW_TASK}` ⇔ SUPERSEDED. The pointer FK is
  a deferred (`use_alter`) constraint to break the context ↔ clarification
  cycle. Downgrade drops pointer, clarifications, tasks (counts logged) and
  leaves `active_pending_draft_id` intact.
- **Turn ids.** Every new user/assistant entry gets a server uuid4
  `turn_id`; legacy entries get none and therefore never validate a chain.
- **Relationship to D-089 (normative).** Slice A supersedes/narrows
  D-089's deterministic-only Phase-B statements: for `POST /ui/agent` after
  this slice, every final consequential Phase B starts from a fresh
  transaction via `TurnBoundary.enter_phase_b()`, including no-inference
  turns. D-089 remains the historical #85 decision for the pre-Slice-A
  implementation; its no-DB-across-inference and reservation principles
  remain authoritative. D-089 carries forward-reference notes at the
  superseded statements; its text is otherwise unchanged.
- **Phase B order.** The final consequential Phase B ALWAYS starts from a
  fresh transaction (`TurnBoundary.enter_phase_b`): whatever DB phase is
  still open is committed first — Phase A for a deterministic, no-inference
  turn (T1, label-resolved search, T5, T6, T8), or the last post-inference
  re-entry otherwise — so no Phase A conversation/context/submission lock
  is held when the principal is re-locked. That commit can only contain
  what any #85 `leave_db` already persists: the reservation and #87
  PROCESSING claim, attempt-provenance audit rows and inert (pointer-less)
  ResultSet rows; transcript, pointers and task/clarification state are
  written only in Phase B. Order: `reenter` (User → TenantMembership →
  BrowserSession `FOR SHARE` → conversation → context `FOR UPDATE`) →
  `lock_dialogue_rows` (clarification → task → lane-B task) → submission →
  writes → D-045 sync → `created_turn_version` stamped with the final
  `turn_version` → one commit. No lock is held across Ollama (#85).
  *Correction (Slice-A independent review):* the first implementation
  re-entered inside the still-open Phase A transaction on no-inference
  turns, taking the principal locks after the conversation/context/
  submission locks; fixed with a failing-first engine/`pg_locks` probe
  regression (`tests/test_issue88_slice_a_phase_b.py`). Visible
  consequence: a second same-conversation turn arriving while a
  deterministic turn runs now first blocks on its Phase A row lock and then
  gets the D-089 409 "still processing" outcome (as it already did during
  inference turns) instead of running afterwards;
  `test_real_ui_agent_route_serializes_concurrent_same_session_turns` was
  updated to that contract (one 200, one 409, nothing persisted by the
  refused turn).
- **Request hash.** `request_hash` is SHA-256 of canonical JSON
  `{"v":1,"message","clarification_id","clarification_choice"}` for every
  turn. A PROCESSING submission claimed under the old message-only hash at
  deploy time fails closed (re-render with a fresh token).
- **Observed-stale pointer (T8).** When the live pointer's clarification
  fails §6.2 (TTL, appended foreign turn, epoch, versions, source), the turn
  expires it (task EXPIRED, pointer cleared) and answers with fixed
  "resend" copy without executing anything — the literal §6.2 / scenario D
  rule, whatever the message says. A pointer whose row is no longer OPEN is
  cleared defensively (no row transition).
- **Rejected button (T8b).** 409 with "Bu sual artıq aktiv deyil.", audit
  `agent.clarification.rejected(NOT_ACTIVE|INVALID_CHOICE)`. Because the
  rejection happens before any inference, the rollback also undoes the
  PROCESSING claim, so the router retires the still-ISSUED token as
  ABANDONED explicitly.
- **Classifier failure (T8a).** Timeout/unavailable/provider error or
  malformed output after the one repair → `ClarificationClassifierError`:
  turn abandoned, existing provider-failure copy (503), audit
  `agent.turn.abandoned(CLARIFICATION_CLASSIFIER_FAILURE)`. INFERENCE_BUSY
  keeps the existing #85 busy path. A proposal outside the type's allowed
  codes is a contract failure, not UNCLEAR.
- **VACANCY_SOURCE_REQUIRED order.** FORCE_CANDIDATE_SEARCH /
  FORCE_RESULT_LIMIT (new task) are checked before the slot fill, so an
  explicit search with a requirement is never mistaken for a JD. A
  CLARIFY_INPUT_STRUCTURE (unrepresentable) reply is UNCLEAR, never a source.
- **Resumed search bound.** A resolved CANDIDATE_SEARCH whose bound source
  exceeds the forced-search input bound (2000) ends T4 FAILED_SAFE with the
  existing input-structure copy; nothing executes.
- **Lane B.** Every turn that produces a new pending draft (forced JD,
  SOURCE_MESSAGE, resumed VACANCY_ANALYSIS) owns a WAITING_CONFIRMATION
  task; the deterministic amendment updates its `pending_draft_id` (T9);
  the confirm route completes it (T10). Legacy pending drafts without a task
  stay confirmable.
- **Unchanged until slice C.** A model-proposed DRAFT_JOB_CRITERIA still
  gets the non-resumable fixed copy (§23 lists that conversion under C).
- **Deferred.** The `meyar retire-agent-tasks` CLI and its T12
  maintenance expiry remain optional in slice A per §19.1 (required before
  pilot). The quoted-source continuity headline and disabled historical
  buttons (§20) are deferred by Slice-A independent review as non-authority
  UX follow-up; D-092 §20 remains the target contract. Buttons render only
  for the one answerable question (live pointer, OPEN, unexpired, last
  entry).
- **Test bootstrap.** `tests/conftest.py` drops the session-context table
  before `drop_all` so the deferred FK cannot break a reset of a test schema
  created by an earlier revision.

## D-094 — Issue #88 slice B implementation record (capability registry + validator)

**Status: IMPLEMENTED — slice B independently technically accepted at code head
`76d3719bde0a5bfb8a8b1c8e7aab84832a8de198` (accepted code tree
`0a89fd6a12f1d4ec23f5580d7a71b72f1a908282`; exact-head CI run
`36909027654` SUCCESS: 3073 pytest passed, 10 hang diagnostics passed),
and MERGED through PR #100: squash commit
`2961365c961894cc0f6b61ac65619c9891e8cf4e` on `main` (single parent
`1dc79eadf92bc433d0772a9ebd863385a1e90cf6`); final accepted PR head
`4b3a2260922f1dc1c19ba753a3c990b44d0c8d2f`, whose tree equals the squash
tree exactly (`ad51d945352b3283a98d03516cbde33bc00f47dc`); accepted
exact-head CI run `36911274851` SUCCESS (3073 pytest passed, 10 hang
diagnostics passed).** D-092 §8–§11/§23, A1,
A2 and D-093 are unchanged; this
entry records only the slice-B implementation choices. Slice C has NOT
started; #88 stays OPEN; #50 stays OPEN and out of scope. No migration
(Alembic head stays `b88a2c4d6e10`). The historical CI hang root cause
remains NOT PROVEN.

- **Package `meyar.agent.capabilities`.** `contracts` (closed enums, strict
  `extra="forbid"` input schemas, plan/outcome types), `registry` (the seven
  §9 definitions, built once at import), `executors` (module-level wrappers
  of the unchanged `_dispatch_*` functions), `validator` (pure Layer 1),
  `execution` (Layer 2 + atomic activation), `adapter` (transitional).
- **Registry invariants** asserted at import and in tests: keys ==
  `set(CapabilityName)`; every executor is a module-level coroutine function
  in `meyar.agent.capabilities.*`; no IN_TURN capability with
  BUSINESS_MUTATION or DERIVED_RECORDS; mutation ⇒ EXPLICIT_HUMAN_ROUTE;
  identity never an input; every input schema forbids extra keys. The model
  only ever yields a `CapabilityName` string; a module/function name is
  UNKNOWN_CAPABILITY.
- **Compatibility.** `REFINE_RESULTS` ↔ `REFINE_CANDIDATE_RESULTS` and
  `ANALYZE_VACANCY` ↔ `DRAFT_JOB_CRITERIA` via `legacy_tool_name`;
  `AgentToolResult.tool_name` and audit `tool_name` are unchanged.
  `agent.tool.executed` / `agent.tool.failed` gain `capability` and
  `capability_version`; no key is renamed. ANALYZE_VACANCY keeps today's
  `candidates:read` scope (the §9 "+ jobs:write" proposal is not adopted
  in this behaviour-preserving slice).
- **Transitional adapter.** Each forced/resumed route becomes a SERVER-origin
  one-step plan over its server-owned source (current message or bound
  resume span); each model `AgentDecision` tool action a MODEL-origin
  one-step plan. The `while True` loop, `AgentDecision`, `AgentActionType`,
  `TOOL_ACTIONS` and `decide_agent_action` remain (slice C retires them).
  The if/elif dispatch is replaced by `validate_plan` → `execute_plan` →
  registry executor; per-capability outcome handling and sequencing are
  unchanged.
- **Grounding change (the one intended behaviour change).** A model-routed
  SEARCH_CANDIDATES is WHOLE_MESSAGE: the planner receives the canonical
  user message, never `AgentDecision.search_query`. Consequences, accepted
  by §23: (a) a second model-routed search in the same turn resolves to the
  same source and finalizes through the existing identical-search guard, so
  the loop's `max_tool_calls` bound is reached through searches only at
  `max_tool_calls=1`; (b) the planner's own validated input bound (4000)
  applies to a model-routed search instead of the 2000-character model
  field bound (the resume T4 rule is unchanged). Refine filter, limit,
  ordinal and evidence topic stay transitional model fields until slice C.
- **Layer 1 (pure).** Checks in §11.1 order: UNSUPPORTED_VERSION,
  SHAPE_INVALID, UNKNOWN_CAPABILITY, NOT_MODEL_PROPOSABLE, INVALID_ARGUMENTS,
  PLAN_TOO_LONG (`min(3, agent_max_tool_calls)`), DUPLICATE_STEP,
  SCOPE_MISSING (live `UIContext.scopes`), TASK_TYPE_CONFLICT,
  PROHIBITED_ATTRIBUTE, DEPENDENCY_INVALID, RESULT_CONTEXT_REQUIRED /
  RESULT_SET_STALE / RESULT_SET_EXPIRED, CANDIDATE_REF_OUT_OF_RANGE,
  CONFIRMATION_REQUIRED, CANDIDATE_CONTENT_POLICY. `SourceSelection` admits
  only WHOLE_MESSAGE; the §10.2 SOURCE_* / REFERENCE_NOT_GROUNDED families
  are slice C. PROHIBITED_ATTRIBUTE covers model text no frozen planner sees
  (the transitional evidence topic); WHOLE_MESSAGE and the refine filter
  keep the planner's prohibited refusal as the authority (§11.1). A
  rejection audits `agent.plan.rejected(reason_code, schema_version)` and
  shows fixed copy; a model DRAFT proposal is NOT_MODEL_PROPOSABLE and keeps
  today's `agent.entry.action_rejected` clarification.
- **Pre-existing ResultSet in the real ValidationContext (§11.1).** Before
  every validation the server builds the context from read-only, audit-free
  authority: `inspect_active_result_set` runs the same structural check as
  the executors and the shared `validate_member_snapshots`, scoped to
  exactly what the plan consumes (`pre_existing_result_set_requirements`):
  the referenced ordinals of PROFILE/EVIDENCE, the whole snapshot for
  REFINE. This is compatible with §11.1 and #86 without amendment: §11.1
  names the statuses (none / valid with n / STALE / EXPIRED); #86 fixes
  STALE's granularity — a reference is stale only if ITS member changed (an
  unrelated changed member never invalidates it), a refinement if any member
  of its source snapshot changed; set-level STALE is an unsupported snapshot
  policy. A ResultSet-family Layer-1 rejection of a one-step plan keeps
  today's truthful outward behaviour (§11.3): the same outcome, message,
  not-found card and `agent.result_set.reference_rejected` /
  `refine_rejected` audit, with ZERO executors run (no `agent.tool.executed`
  — nothing executed). The executors' own checks are unchanged and still run
  as Layer-2 / TOCTOU defense in depth.
- **Plan provenance (§19.2).** Every successfully validated plan attempt
  (server-built or model-adapted) emits `agent.plan.validated` with exactly
  `plan_sha256`, `step_count`, `capabilities` (closed codes) and
  `schema_version`. `plan_sha256` is SHA-256 over the canonical JSON of the
  validated plan (schema/policy versions, origin, goal, capability codes,
  bounded integers, affordance targets) in which every text value — the
  resolved source and the transitional filter/topic — is replaced by its
  SHA-256 and length; the canonical form is never persisted. A Layer-1
  rejection emits `agent.plan.rejected` and no `agent.plan.validated`.
- **Layer 2 + atomic activation.** Before a step consuming a ResultSet
  produced earlier in the plan: exists, same tenant / BrowserSession /
  conversation / context epoch, unexpired, non-empty, ordinal ≤ actual
  count. A pre-existing set gets no extra check (the executor's #86 check
  decides). A produced set is only the in-turn working pointer; a non-
  completed plan restores the previous `active_result_set_id` exactly.
  Multi-step failure is PLAN_INCOMPLETE (new closed outcome, fixed copy,
  `agent.plan.incomplete(step_index, reason_code)`, no active result cards);
  single-step failure stays the step's own outcome. No model multi-step
  planning exists in slice B: multi-step execution is exercised by
  server-built test plans only.
- **HUMAN_ACTION_ONLY.** CREATE_JOB / RANK_JOB_CANDIDATES are registry
  entries with `NoArgs`; with a live target they become id-free affordances,
  without one CONFIRMATION_REQUIRED. Their executor fails closed
  (`HumanActionOnlyError`), `execute_plan` refuses them, and no
  AgentDecision can be adapted into them. Creation/ranking remain the
  existing authenticated CSRF routes.
- **Existing tests adapted to the accepted grounding change** (no assertion
  weakened): the loop-bound test now uses `max_tool_calls=1` plus a new test
  proving distinct model queries collapse to one WHOLE_MESSAGE search; the
  rephrased-search presentation test asserts one audited search; the #85
  hybrid-embedding tests post a real hybrid search message; the #85 orphan
  ResultSet test plans the HR message through the gated local planner.
- **Independent-review correction (PR #100, reviewed head `9746b84`).** The
  first implementation passed a hard-coded transitional ResultSet status to
  Layer 1 and did not emit `agent.plan.validated`; both were corrected as
  recorded above (real read-only context; §19.2 provenance). Real
  `execute_agent_turn` / `POST /ui/agent` regressions prove zero executor
  calls for missing / expired / stale / out-of-range references and
  refinements, the normal path for a valid set, and that a change after
  Layer 1 is still rejected by the executor checks.

## D-095 — Issue #88 slice C implementation record (agent-plan-v1 + source grounding + loop retirement)

**Status: ACCEPTED and MERGED — the accepted Slice-C implementation
record.** Implemented in PR #102 (branch `feat/88c-agent-plan-contract`,
originally from `main` @ `9817711c73e9d2701c0385d2bbe0c1bd3fba153e`) and
corrected to the ACCEPTED D-092 Amendment A3 (merged through PR #103; status
recorded by PR #104) after a normal merge of `main` @
`32f1a0e5411f0445f3bc794451e5288561ae5cd1`. Independently accepted at head
`117d0abbad4f012911e0cd96794604eaa9f474ba`; exact-head CI run
`36995144617` SUCCESS (3229 pytest passed; 10 hang diagnostics passed).
Merged through PR #102 as squash commit
`82d6f7b4a6e4e51b02442b62dc7a556519b66e5c` (single parent
`32f1a0e5411f0445f3bc794451e5288561ae5cd1`); the accepted-head tree equals
the squash tree exactly (`d259e06fe73b11d33aca751bf4f444924d09bace`).
D-092 (§5, §8–§12, §15–§16, §19, §22 items 24–31, §23 slice C, §24) as
amended by A1, A2 and the accepted A3, D-093 and D-094 are the authority;
this entry records the slice-C implementation choices. Issue #88 is
CLOSED/completed; #50 stays OPEN and was out of scope. No migration
(Alembic head stays `b88a2c4d6e10`); no new dependency. The target-Mac
benchmark remains separate; model quality/selection remains #36. The
historical CI hang root cause remains NOT PROVEN.

- **Model contract `agent-plan-v1`** (`meyar.agent.capabilities.contracts`):
  strict (`extra="forbid"`, strict JSON parsing) `AgentPlanProposal` with
  `kind` PLAN / CLARIFY / CONVERSE, `goal`, ≤`MAX_PLAN_STEPS`=3 steps
  (discriminated on `capability`), closed `clarification_code` (incl.
  SEARCH_OR_VACANCY) and `response_code`. Step arguments are grounding
  primitives only: `SourceSelection` (WHOLE_MESSAGE | 1..4 QUOTES),
  `limit_quote`, `ref_quote`, `topic_quote`. No field exists for any id,
  scope, token, score, weight, date, module/function name, free text, or
  model-authored search/filter/topic/number. The kind/field combination is
  Layer-1 SHAPE_INVALID (`validate_model_reply` for CLARIFY/CONVERSE).
- **Provider.** `LLMProvider.propose_agent_plan(context, repair)` replaces
  `decide_agent_action` (Ollama, BoundaryLLM, fakes, demo provider). The
  constrained-decoding schema is per call (`agent_plan_json_schema`), built
  from the registry-derived subset (`offered_capabilities`: model-proposable
  ∧ scopes ⊆ principal ∧ live context satisfiable, where a result-set
  consumer is offered when a result context exists or an offered capability
  can produce one, and a HUMAN_ACTION_ONLY capability only with its live
  target). It is kind- and mode-specific (PLAN needs a goal and 1..n offered
  steps; WHOLE_MESSAGE takes no quotes). The response is still parsed
  against the full `AgentPlanProposal`; a registered-but-not-offered name is
  Layer-1 UNKNOWN_CAPABILITY, ANALYZE_VACANCY is NOT_MODEL_PROPOSABLE.
  Exactly one proposal per MODEL_ROUTED turn plus at most one repair
  (`MAX_PLAN_PROPOSAL_ATTEMPTS`=2). Still schema-invalid after the repair →
  MALFORMED_MODEL_OUTPUT (scenario H). Infrastructure failure (timeout,
  unavailable, transport) → `AgentPlanProviderError`: the route abandons the
  turn (#85: no transcript, no execution, no pointer/task/clarification
  change, no consumed clarification attempt; audit `agent.turn.abandoned`
  `AGENT_PLAN_PROVIDER_FAILURE`, 503 not-run copy). Busy stays
  AgentInferenceBusyError. The model call goes through `BoundaryLLM` (no DB
  connection held; pool-probe test covers proposal + repair).
- **Projection (§15).** `AgentPlanContext` is the entire model input: last
  `agent_max_context_turns` (role, text), offered capability names + one-line
  server descriptions, `max_plan_steps`, `active_result_context_present`,
  `available_candidate_refs` (ordinals), `waiting_clarification` (always
  null: a model plan exists only when lane A is absent or just superseded,
  §11.1), `pending_vacancy_confirmation`. Per accepted Amendment A3.1,
  assistant entries are projected as their closed `AgentTurnOutcome` code
  only (`[assistant outcome: <code>]`), never the persisted HR-facing display
  text (D-045 stores the rendered headline, which can name a candidate);
  CandidateIdentity stays structurally absent from the model input. No
  heuristic name redaction; no conversation RAG/embeddings.
- **Grounding (§10.2, pure `capabilities.grounding`).** Quotes resolve to
  exactly one byte-exact occurrence of the canonical LF message (overlapping
  occurrences count; no case/diacritic folding); offsets + SHA-256 are server
  computed; spans are non-blank, ≤500 chars, non-overlapping, source-ordered,
  joined by a server `"\n"`. Coverage runs `analyze_hr_text(M)`; every
  SCORABLE / NEEDS_HUMAN_REVIEW subject (or its requirement span when no
  subject) must overlap a COVERAGE span of some step. Per accepted Amendment
  A3.2 the coverage sources are only search/refine semantic source spans,
  the grounded reference span and the grounded topic span; a result-count
  `limit_quote` is numeric workflow grounding and never satisfies coverage
  (validator `_COVERAGE_FIELDS` excludes LIMIT). WHOLE_MESSAGE still covers
  everything. The analyzer reports a coordinated subject ("Python və Java")
  as ONE occurrence, so the overlap rule is applied to each coordinated part
  (A3.2; closed coordinators: `, / & ;`,
  və, and, or, ya, yaxud, həmçinin, habelə). This is strictly stronger than
  whole-occurrence overlap (scenario E still passes) and is what makes §22
  item 25 achievable. A PROHIBITED requirement, a PROTECTED_CUE role or a
  denylist term anywhere in M forbids QUOTES for SEARCH/REFINE
  (SOURCE_SELECTION_FORBIDDEN); WHOLE_MESSAGE keeps the frozen planner's
  refusal authoritative. A grounded evidence topic that hits the denylist or
  a protected range is PROHIBITED_ATTRIBUTE.
- **Closed numeric parsers.** Counts reuse `count_token_value` with
  FORCE_RESULT_LIMIT's suffixes plus `N-<case>`; exactly one count token,
  1..`MAX_CANDIDATE_REF`, not followed by a duration unit (`3 il`, `5 years`)
  or `neçə` (`bir neçə`). Ordinals: digits, `#N`, `N-ci/-cu` (+ case
  endings), English `Nth` with the correct suffix, English first…tenth, and
  Azerbaijani birinci…onuncu with a closed list of case/possessive endings;
  exactly one ordinal token in 1..50. Everything else (`sonuncu`, `last`,
  pronouns such as `onun`, two ordinals, 0, 51, UUIDs) is
  REFERENCE_NOT_GROUNDED and HR gets the existing CANDIDATE_REFERENCE_REQUIRED
  (ref) / RESULT_CONTEXT_REQUIRED (count) copy. Behaviour change by design:
  a pronoun reference the former model resolved now fails closed (D-092 §26
  question 4).
- **Validation order.** As §11.1; within the §10.2 row the families run
  SOURCE_SELECTION_FORBIDDEN → SOURCE_NOT_GROUNDED → REFERENCE_NOT_GROUNDED
  → SOURCE_COVERAGE_INCOMPLETE (whole plan), then PROHIBITED_ATTRIBUTE.
  DUPLICATE_STEP compares the resolved grounded values. Coverage and the
  QUOTES prohibition apply to MODEL plans; SERVER plans
  (`capability-plan-v1`, `capability.server_plans`: WHOLE_MESSAGE search,
  router vacancy span, router-parsed `ServerLimitArgs`) are grounded by
  construction. `ValidatedStep` now carries only server-resolved values
  (`resolved_text`, `candidate_ref`, `limit`, `topic`, grounding records) —
  executors never see model arguments. `PlanRejection` additionally carries
  the grounded field code and the parsed ordinal (for today's not-found
  card). `pre_existing_result_set_requirements` reads the server-parsed
  ordinals.
- **Execution.** One validated plan, `for step in plan.steps` (Layer 2 +
  executor + atomic activation, unchanged from D-094). No post-tool model
  call; the D-038 grounded synthesis still runs once for a final found
  profile/evidence step (a rendering aid). A completed multi-step plan
  activates only the final produced pointer; a zero-member producer before an
  ordinal consumer is now reported as CANDIDATE_REF_OUT_OF_RANGE (scenario M;
  `step_index` stays 0-based as in D-094). Single-step failures keep today's
  outcomes. An affordance-only plan (CREATE_JOB with a live pending draft)
  returns ANSWERED with fixed copy pointing at the existing CSRF form; no
  lane-B change. Per accepted Amendment A3.3, RANK_JOB_CANDIDATES remains a
  registered HUMAN_ACTION_ONLY capability with `model_proposable=False`: it
  is never offered, and a model plan naming it is NOT_MODEL_PROPOSABLE (fixed
  rejection copy; never the vacancy clarification) with zero execution. No
  current-job pointer, "latest AgentDraftConfirmation" selector, transcript
  scan or migration exists; the direct authenticated CSRF ranking routes and
  deterministic scoring are unchanged. A model ANALYZE_VACANCY step or CLARIFY(SEARCH_OR_VACANCY) becomes
  the resumable SEARCH_OR_VACANCY clarification when the message is
  requirement-shaped, else fixed NEED_MORE_DETAIL copy (§6.1); never a draft.
- **Retired:** `decide_agent_action`, `AgentDecision`, `TOOL_ACTIONS`, the
  transitional adapter module, the MODEL_ROUTED `while True` loop,
  `searched_queries`, `_summarize_tool_result`, and the FINAL_ANSWER /
  CLARIFY members of `AgentActionType`. `AgentActionType` remains only as the
  persisted `AgentToolResult.tool_name` / audit `tool_name` identifier
  (transcripts and audit history store these strings); it is in no model
  output schema. TOOL_CALL_LIMIT_EXCEEDED / AGENT_PROVIDER_FAILURE stay as
  outcome values so persisted history renders; neither is produced as a new
  committed turn by the agent service any more.
- **Audit.** `agent.plan.validated` keeps its four closed keys;
  `schema_version` is `agent-plan-v1` for model plans and
  `capability-plan-v1` for server plans; `plan_sha256` is over origin, goal,
  capabilities, policy versions, parsed integers and grounding records
  (field, mode, span offsets, span SHA-256) — no text, quote, id or identity.
  `agent.plan.rejected` / `agent.plan.incomplete` unchanged in shape.
- **Prompt.** `AGENT_PROMPT_VERSION = agent-plan-prompt-v1`: closed schema,
  offered capabilities, quotation mechanics, step bound, no ids/scores/hiring
  decisions, compact closed examples; no chain-of-thought, no free-text
  answer.
- **Tests.** New `tests/test_issue88_slice_c_plan_contract.py` (§22 items
  24–31, prompt injection, per-call subset, projection, provenance), plus
  no-exfiltration and pool-probe extensions. Existing suites that scripted
  model-authored `AgentDecision` values were rewritten to `agent-plan-v1`:
  each scripted ordinal/count/filter/topic is now an exact quote of the
  test's own message that the server parses (messages that named no ordinal
  were given one, since inventing it is exactly what slice C forbids); tests
  of the retired post-tool loop (loop bound, identical-search dedup, D-036
  follow-up-framing failures) now assert the stronger replacements
  (PLAN_TOO_LONG / DUPLICATE_STEP with zero executors, exactly one proposal
  per turn, provider failure abandons the turn).
- **Correction to accepted A3 (PR #102 follow-up commit).** (1) A3.2:
  `limit_quote` grounding no longer contributes to requirement coverage. (2)
  A3.3: RANK `model_proposable` False (was True with production hard-coding
  `confirmed_job_in_session=False`); only an ANALYZE_VACANCY NOT_MODEL_
  PROPOSABLE rejection becomes the SEARCH_OR_VACANCY clarification; the
  prompt no longer describes RANK. (3) §10.2 rule 4 applied literally: with
  protected content in M, a REFINE without a WHOLE_MESSAGE filter source (a
  count-only refinement) is SOURCE_SELECTION_FORBIDDEN — previously it could
  launder the request into a "clean" truncation that never reached the
  planner's refusal. A3.1 projection unchanged. New regressions in
  `test_issue88_slice_c_plan_contract.py` (A3 section) and the slice-B suite.
- **Real-Ollama smoke (not the Target-Mac benchmark).**
  *Historical, pre-A3-correction evidence (head `4e0daf2`, prompt still
  listing RANK):* dev machine,
  synthetic HR text, loopback-only guard, `propose_agent_plan` + Layer 1:
  `qwen3:0.6b` (the configured default) and `qwen3:1.7b`. Before the
  kind/mode-specific per-call schema both models produced only fail-closed
  rejections (SHAPE_INVALID / MALFORMED). After it: `qwen3:1.7b` 7/7 strict
  parses, 6/7 Layer-1-valid and semantically right (greeting, search,
  refine quote, profile ref, evidence ref+topic, hiring CLARIFY); the
  two-step search+profile request was mis-planned as a refinement and
  failed closed (RESULT_CONTEXT_REQUIRED).
  `qwen3:0.6b` 7/7 strict parses, 3/7 valid; the rest fail closed. Every
  proposal used only offered capabilities, no output contained a UUID, and
  zero non-loopback requests were attempted. Model quality/selection remains
  #36; no fake production AI mode exists.
- **Final real-Ollama smoke at the corrected head
  `2f11f1bba7d6102be3d3f732883e7f4b4dfcf051` (dev machine; NOT the
  Target-Mac benchmark).** Same seven synthetic scenarios, the final
  unmodified `AGENT_SYSTEM_PROMPT` / `build_agent_user_prompt`
  (`agent-plan-prompt-v1`, no RANK mention), the per-call subset (computed
  with full HR scopes and `confirmed_job_in_session=True` to probe A3.3),
  strict `agent-plan-v1` parsing and Layer 1; real `OllamaLLMProvider`
  under a loopback-only `httpx` guard. Results (parse / Layer 1 / proposed
  capabilities / semantically right):

  | Scenario | `qwen3:1.7b` | `qwen3:0.6b` |
  |---|---|---|
  | greeting | OK / valid CONVERSE GREETING / right | OK / SOURCE_NOT_GROUNDED (EVIDENCE) / wrong, failed closed |
  | candidate search | OK / VALID SEARCH (whole message) / right | OK / REFERENCE_NOT_GROUNDED (EVIDENCE) / wrong, failed closed |
  | refinement quote | OK / VALID REFINE "SQL bilənləri" / right | OK / VALID REFINE "SQL bilənləri" / right |
  | profile reference | OK / REFERENCE_NOT_GROUNDED (REFINE) / wrong, failed closed | OK / VALID PROFILE ref 1 / right |
  | evidence ref + topic | OK / VALID EVIDENCE ref 2, topic "Python" / right | OK / VALID EVIDENCE ref 2, topic "Python" / right |
  | hiring clarification | OK / valid CLARIFY HIRING_DECISION_REQUIRES_HUMAN / right | OK / SOURCE_NOT_GROUNDED (PROFILE) / wrong, failed closed |
  | two-step search + profile | OK / VALID SEARCH only (whole message) / incomplete | OK / REFERENCE_NOT_GROUNDED (PROFILE) / wrong, failed closed |

  Totals: strict parses 7/7 and 7/7 (no repair needed); Layer-1-valid
  6/7 (`qwen3:1.7b`) and 3/7 (`qwen3:0.6b`); semantically right 5/7 and
  3/7. Every rejected proposal failed closed (Layer 1, zero execution). The
  one valid-but-incomplete result (`qwen3:1.7b`, two-step) omits the
  profile step and would execute only a WHOLE_MESSAGE search of HR's own
  text — a safe under-execution (no authority, grounding or privacy
  breach), not a contract defect. RANK_JOB_CANDIDATES was never offered,
  never present in any request schema and never emitted; no proposal
  contained a UUID; 14 requests, all loopback, zero non-loopback attempts;
  synthetic text only, no candidate content or PII. Model selection and
  quality remain #36; the prompt was not tuned for this smoke.

## D-096 — Issue #46 PR-1 implementation record: bounded ingestion intake

**Status:** ACCEPTED and MERGED through PR #106 (squash
`26e2fa39da728c4a89291ac20d1ef39e4993a405`, parent
`f3497c09eed5f1ef6e488699b84f5806fa94274a`; accepted head
`7edc69fe352736f3a64eed5252c41aa54376561a`, tree equal to the squash tree
`fb585940fa04fe340187d6b05c97e53ed29f7cdc`; exact-head CI `37017300528`
SUCCESS, 3273 pytest passed). Refs #46; #46 stays OPEN. The special-file
(FIFO) handling below was added during independent review before
acceptance. No migration, no new dependency, no `uv.lock` change. This is
availability/security hardening only; tenant isolation, auth/scopes, CSRF,
original-CV authorization, local-only processing and provenance are
untouched.

**Problem (verified on `main` @ `f3497c0`).** (1) The upload handler did
`await file.read()` after Starlette had already parsed and spooled the whole
multipart body; (2) the folder scanner did `read_bytes()` before any size
check; (3) DOCX detection only called `namelist()`, so no member-count,
expansion or ratio bound existed before python-docx loaded the archive.

**Request envelope (HTTP).** `meyar.api.body_limit.DocumentUploadBodyLimitMiddleware`
is pure ASGI (not `BaseHTTPMiddleware`, which buffers) and is the outermost
layer. It applies only to `POST /api/v1/candidates/{id}/documents`. The
request bound is `max_upload_bytes + MAX_MULTIPART_OVERHEAD_BYTES`, where
`MAX_MULTIPART_OVERHEAD_BYTES = 64 KiB`: one `file` part needs two boundary
lines (<= 74 B each), a Content-Disposition header with the filename, a
Content-Type header and CRLFs — a few hundred bytes for real clients — so
64 KiB leaves orders of magnitude of headroom (plus a few small form fields)
while capping non-document bytes at a constant. A valid `Content-Length`
above the bound is rejected with 413 before the handler runs and before any
body is read. Otherwise (absent/invalid/understated length, chunked) the
`receive` channel is wrapped and actual bytes are counted; on crossing the
bound the downstream parser is shown a disconnect, reading stops, its own
response is discarded and the middleware answers 413. `Content-Length` is an
early rejection only; the counted bytes are the authority. The 413 body is
`{"detail": too_large_message(max_bytes)}` — the same copy the application
validator uses. `max_upload_bytes` is resolved per request through the app's
`dependency_overrides` for `get_settings`, so overrides apply here too. The
handler additionally reads via `meyar.ingestion.bounded_read.read_bounded`
(<= `max_upload_bytes + 1` bytes, chunked); the validator stays the final
size authority. An oversized unauthenticated request now gets 413 rather
than 401 (the bound runs before auth, as any body limit does).

**DOCX archive safety** (`meyar.ingestion.validation`, before python-docx
sees the data; nothing extracted to disk; outward errors are the generic
`UnsupportedDocumentError` and never carry member names/paths/sizes).
Constants (module-level safety floor, not operator settings): members <=
1000 (real CVs: tens; a few hundred with many images); per member <= 32
MiB; total <= 128 MiB (python-docx holds every part in memory); compression
ratio <= 100x (real XML deflates ~3-20x; a deflate bomb is ~1000x), applied
only above a 1 MiB floor because tiny empty parts legitimately exceed it;
central directory <= 1 KiB x members. Also rejected: encrypted members,
unsupported compression methods, duplicate normalized names (case-folded,
`\`->`/`, leading `./` stripped), zip64 sentinels, malformed archives.
*Actual expansion is bounded, not trusted:* the central directory is bounded
from the EOCD record (located the way `zipfile` locates it) before
`zipfile` builds a `ZipInfo` per entry; declared sizes are checked, then
every member is decompressed in 64 KiB chunks and the bytes actually
produced are counted against the same per-member/total/ratio limits. A
forged small header is truncated by `zipfile` and fails its CRC (rejected as
malformed); a forged huge header fails the declared check.

**Scanner result contract.** `scan_source_root(root, *, max_bytes)` now
yields the closed union `ScanEntry = DiscoveredFile | OversizedFile |
UnstableFile`. `DiscoveredFile` is unchanged (bytes + SHA-256 + mtime).
`OversizedFile(relative_path, byte_size, mtime)` is decided from `fstat` of
the opened descriptor and carries no bytes and no hash — no sentinel
values, and the file is never read or hashed. `UnstableFile(relative_path)`
covers a file that vanished, is or became a non-regular entry (FIFO,
socket, device), was swapped for a symlink, or changed during the read.
Special files must never block a scan: a non-following `lstat()` pre-check
rejects an already-special entry; the open uses `O_NOFOLLOW | O_NONBLOCK |
O_CLOEXEC` so a regular file swapped for a FIFO after the pre-check cannot
block `open()`; and `fstat()` of the descriptor actually opened is the
authority (`S_ISREG` only). Only `ENOENT`, `ELOOP` and `ENXIO` at open are
classified unstable; permission and I/O errors still propagate. A within-limit file is
opened once, read at most `max_bytes + 1` bytes, then re-`fstat`ed; if size,
mtime, ctime, inode/device or the byte count differ from the first `fstat`
it is `UnstableFile` — not imported, not a permanent failure; the next pass
observes it again. The SHA-256 is computed only from a stable snapshot. All
symlink/root-escape protections are unchanged.

**Indexer behavior and the M-5 boundary.** `FolderIndexedFile.sha256_hash`
is NOT NULL, so an oversized file cannot be given an index row without a
fabricated or read-everything hash; creating a Candidate to hold it would
add another M-5 phantom. Therefore an `OversizedFile` creates **no
Candidate and no index row**. It is recorded as a tenant-scoped audit event
`FOLDER_FILE_OVERSIZED_SKIPPED` (folder source id, byte size, cap, SHA-256 of
the relative path — never the path), counted in `FolderScanSummary.skipped_oversized`
and the `FOLDER_INDEXING_COMPLETED` event, and an existing row for that path
is left untouched (not tombstoned MISSING). The CLI prints the count and
exits 1 (completed with problems), matching the old FAILED-row behavior for
the operator. Behavior change: an oversized folder file previously produced a
FAILED row plus a phantom Candidate; it no longer does. The audit event
recurs on every scan while the file stays oversized (retention is L-6).

**Explicitly NOT changed / deferred.** The general M-5 phantom-candidate and
transactional-ingestion fix, PDF/parser resource limits and timeouts, DOCX
table/header/footer extraction (M-4), reconciliation single-flight locking,
same-content concurrency, tenant-active enforcement, API cache headers,
logging privacy, readiness, Alembic drift, and all ops/deployment tooling
remain later #46/#35 slices. #36 and #50 untouched.


## D-097 — Issue #46 PR-2: bounded parsing and truthful failure authority

**Status:** Independently ACCEPTED and MERGED through PR #108; PR-2 is COMPLETE.
Accepted corrected head: `270260339e12e55fae6338797ca5c100828a36a2`;
exact-head CI run `37043386062` completed SUCCESS. The owner manually Squash
and merged PR #108 as `080e1f98788943e21c3664806f00579d85b61710`, the verified
current main, with sole parent/previous main
`d5a06a2342abe4acfb9d072c22bf8f956497801e` (PR #107, following accepted
PR-1/#106). The squash is exactly one commit ahead of that base; independent
post-merge comparison matched all 19 changed file contents at the accepted
head and squash result, and the complete trees match. Refs #46 under M9;
#46 remains OPEN because M-4/PR-3, general M-5, M-9 and other unresolved
hardening items remain. #35/#36/#50 remain OPEN; this docs-only correction
does not change or advance them or start another #46 slice. No real
Apple-Silicon execution or #36 Target-Mac acceptance has occurred.

**Problem:** The async parser performed synchronous untrusted library work on
the event loop, had no killable elapsed-time/memory boundary or output budget,
accepted empty PDFs as parsed canonical authority, and persisted raw parser
exception text. PR-1's substantial bounded DOCX structural verification also
ran on the event loop before storage.

**Decision:** Preserve PR-1 validation before storage/document creation, with
one admitted thread and four finite-wait callers, retaining the thread slot
after caller cancellation. Keep the DocumentParser async contract; adapt the
accepted photo subprocess/stdin architecture with bounded parser admission,
bounded incremental IPC, cancellation-safe startup/terminate/kill/reap,
feature-detected and allocation-probed RLIMIT_AS on Linux/Darwin, and inclusive
page/block/character/UTF-8/serialized-output budgets. One active parser child,
four waiters, 3s admission, 20s worker deadline, 0.5s termination grace, 768 MiB
address space; output: 300 PDF pages, 10,000 blocks, 1,000,000 characters,
4 MiB UTF-8 text, 8 MiB serialized response. These are safety limits supported
by small synthetic development reproductions, never production throughput or
Mac benchmark claims. Setup fails closed if the memory cap cannot be verified.
The 20s deadline covers normally resolving startup and input-controlled worker
work. Shielded OS spawn resolution and terminate/kill/reap cleanup may delay
delivery beyond it; an indefinitely hung OS spawn is not independently bounded.
Retaining its eventual child handle prevents an orphan process.

**Authority:** Fixed typed failure codes/messages replace raw parser exception
metadata for new failures. Entirely no-text PDF/DOCX produces no canonical row
and cannot reach profile/identity inference. Mixed PDF pages retain numbering;
short text succeeds; DOCX remains body paragraphs only with original index
gaps. No OCR and no certainty that a no-text document is scanned.

**PR #108 correction:** Only `INVALID_DOCUMENT`,
`INSUFFICIENT_EXTRACTABLE_TEXT` and `PARSER_OUTPUT_LIMIT` are terminal content/
policy failures. The other seven enum codes are operational, including timeout
and memory refusal: one attempt cannot distinguish document complexity from
host/runtime/baseline conditions sufficiently to declare permanent authority.
Operational direct uploads return fixed safe HTTP 503 before original storage,
CandidateDocument or CanonicalDocument creation; retries can succeed. Terminal
failures retain an authorized original and safe PARSE_FAILED document, without
canonical authority. Folder operational failures use existing retryable FAILED
index semantics and preserve a prior successful document pointer; terminal
failures retain existing INDEXED semantics. General candidate compensation and
folder transaction redesign remain M-5.

Direct uploads complete a short initial authorization/tenant ownership phase
before validation, parser admission and execution, with no pooled connection or
transaction held during preparation. A fresh persistence transaction rechecks
and takes shared row locks on the live API key and tenant-owned candidate before
any storage/document mutation, checking revocation, expiry, scope, key tenant,
candidate existence and ownership. The initial read-only commit does not split
document writes; all such writes occur together after revalidation. Pool size
is unchanged. Regression instrumentation proves zero checkouts for one active
parser plus four waiters and completion of an unrelated DB-backed route; actor
transactions also exercise authority changes during preparation.

**Version and scope:** Local parser 1.1.0 records changed acceptance/resource/
failure policy while successful block semantics remain unchanged. Historical
canonical JSON, references, profiles and identity are not rewritten or
invalidated; unchanged folder files are not backfilled solely by parser
version. Folder INDEXED semantics are preserved for terminal failures only.
M-4 extraction completeness is PR-3. General
M-5/M-9, other #46 slices, #35/#36/#50 remain deferred. Real Apple-Silicon
resource and lifecycle execution remains later agentless rehearsal; Linux CI
and injected Darwin unit paths do not establish Mac acceptance. No migration,
dependency or lockfile change.

Implementation, measured rationale, closed failure table, verification and
precise deferrals: `docs/ISSUE_46_PR2_VALIDATION.md`.

## D-098 — Issue #46 PR-3 proposal: ordered DOCX tables with bounded source authority

**Status:** IMPLEMENTATION PROPOSAL, awaiting independent acceptance review.
Refs #46 under M9. Base `9cc2cf9623b263ba4a2dd92217fce23c566ba0a7`;
branch `feat/46-docx-ordered-tables`. #46 remains OPEN. No Target-Mac claim.

Parser 1.2.0 supports direct body paragraphs/tables, physical source cells,
ordered cell paragraphs and at most eight nested table levels. All private
OOXML/python-docx dependence is confined to one adapter. Logical DOCX page=1
and one global paragraph ordinal preserve blank gaps and exact source order;
no layout-grid expansion or recursive vertical-merge-origin lookup occurs.
Closed optional BODY/TABLE provenance uses bounded integer source coordinates.
The parent validates schema/unknown fields, duplicate coordinates and warning
cardinality; metadata is included in serialized-output bounds. An aggregate
100,000-node source ceiling bounds empty/unsupported traversal as well.

Table row context can only veto authority: cited-block positive material rules
remain mandatory. Enumerated negative/ambiguous row context, over-limit row
context, and non-phone labels reject conservative interpretations. Nonempty
vertical continuation text is omitted with a closed warning, never promoted
into facts; its incomplete table context cannot grant positive authority.
Immediate-row completeness is a strict server-only boolean: nonblank skipped
content-control, revision or text-box text vetoes that row without extracting
it or choosing revision semantics. Other complete rows/nested rows and separate
header/footer stories are unaffected. Historical TABLE completeness missing this
metadata is unknown and fails closed under current authority, without rewriting
rows or reprocessing files. Historical BODY evidence behavior remains unchanged.
All TABLE identity fields reject the explicit English labels reference, referee,
recommender and emergency contact in supported immediate-row context. Sibling
labels never supply positive identity evidence; persisted identity revalidation
uses the same verifier. Both independent blockers reproduced (11 expected
rejections absent); correction focused suites pass 185 tests in 43.18s. Exact
full-gate results are recorded in the PR-3 validation document.

Existing header/footer source parts and recognized text-box containers are
inspected read-only for omission, never extracted. Five closed durable warning
codes carry no source text/XML/path. Mixed supported/omitted documents succeed
with partial disclosure; unsupported-only documents retain the authorized
original with terminal UNSUPPORTED_DOCX_TEXT_ONLY and no canonical authority.
No-text/no-known-omission retains INSUFFICIENT_EXTRACTABLE_TEXT. Canonical API
exposes a human-meaningful partial boolean and strips coordinates. HR shows
friendly Azerbaijani copy and keeps the original action; DOCX evidence says
CV-də without physical-page claims, while PDF page wording remains.

Historical rows/evidence/accepted profiles/identities are immutable and are not
invalidated just by parser version. No unchanged-file reprocessing, terminal
failure backfill or automatic recovery. Headers/footers/text boxes, revision/
content-control/SmartArt/notes/comments/AlternateContent/object interpretation
and page-break fusion changes remain deferred. No migration, SQLAlchemy change,
dependency or lockfile change; no general M-5/M-9 or #35/#36/#50 work started.
Implementation contract, limits/rationale and verification:
`docs/ISSUE_46_PR3_VALIDATION.md`.
