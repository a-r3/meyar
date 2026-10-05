# Issue #46 S10 — configured embedding readiness

Status: implemented; independent acceptance pending. Refs #46 under the existing **M9 — Deployment,
Benchmark & Integration Readiness** milestone (10). Bounded slice, not completion of #46. Also corrects the
stale S9 acceptance wording in STATUS / DECISIONS / `ISSUE_46_S9_VALIDATION.md` (same PR, history kept).

## Verified accepted starting state (live, before any edit)

- PR #125 MERGED/CLOSED; accepted head `6f6f49dada7c62614c84a8f340096a3f95426ef4`, CI 37214484572 attempt 1
  SUCCESS (3847 passed).
- Owner squash/main `51ae314a74585902a5aecb164004b52cf00a0df9`, sole parent
  `e7dd38e00e86b01e476706e7feca5bbbec44c12c`, tree `952d97102a69fa15c9055ee0e0e1850518e704a2` = accepted-head tree.
- Main contains the S9/D-110/D-111 code. #46/#35/#36/#45/#50 OPEN. Hooks `.githooks`. `.aws` untouched.
- Stale pre-acceptance S9 wording found ("independent acceptance pending", "S9 remains NOT accepted", "PR #125
  still NOT accepted") — corrected only after reproducing the engineering defect.

## Before-fix reproduction (real PostgreSQL, deterministic gated local providers, exact main)

`backend/tests/test_folder_embedding_readiness.py`. The embedding side is made stale, the folder is unchanged:
- **Old model_name / changed model_revision / stale serializer_version / stale source_sha256:** the next run
  reported `ReconciliationSummary(candidates_considered=1, already_ready=1, processed=0, ready_after=1, failed=0)`
  with zero embedding-provider calls — READY with an embedding semantic search cannot use; reconciliation
  quiesced instead of re-embedding (all four cases failed their "re-embed" assertion).
- **Multiple historical embeddings** (m1 -> m2 -> m3 then active m2): the fix-less code never re-embedded after the
  model changes (1 row instead of 3).
- **Diverged tracked documents** after a model change: no re-embedding of either document.
- **Wrong dimensions:** not expressible at all — the reconciliation had no active-configuration input, so there was
  nothing to compare dimensions with (a gap, not a pass).
- Concurrency / delete / suspension / no-transaction regressions for the stale-embedding path could not even reach
  embedding inference (timeouts), a direct consequence of the same defect.

## Root cause

D-111's `_is_ready` accepted any `CandidateEmbeddingVersion` with `candidate_profile_version_id == profile.id`,
and reconciliation had no active embedding configuration. Semantic retrieval (D-014/D-015) matches only
`(profile_version_id, current canonical source_sha256)` AND provider, model_name, model_revision,
serializer_version, embedding_dimensions.

## Derived invariant (checked against D-014, D-015, D-100, D-110, D-111 and the search code)

Folder READY for a tracked document = own latest profile COMPLETED + evidence-authorized + own latest identity
COMPLETED + evidence-authorized + at least one embedding bound to THAT profile version compatible with the active
configuration and the current canonical source hash. One exact compatible row is sufficient; order/created_at is
never freshness authority. Identity is never read as a ranking/embedding input.

## Fix

- `candidate_embedding_repo.EmbeddingCompatibility` + `find_compatible_embedding`: ONE predicate. The semantic-
  retrieval query (`search_compatible_embeddings`) now builds its conditions from the same `conditions()`
  (reused, not a second definition). `embedding.serializer.embedding_source` is now the shared text/hash
  derivation called by creation, readiness and semantic retrieval (centralized in the corrective below);
  `prepare_embedding` gained optional `compatibility`.
- `process_pending_candidates` / `reconcile_folder` take an explicit `embedding_config`; the CLI passes the trusted
  `get_embedding_search_config()`. It is resolved once per invocation and is authoritative for that run (no hidden
  global read in repositories). Without one: provider identity + serializer, dimensions unconstrained.
- `_is_ready` requires an exact compatible embedding; the embedding stage reuses a compatible row, otherwise
  re-embeds only that. Provider != active config -> `EMBEDDING_PROVIDER_CONFIG_MISMATCH`. A result whose
  provider/model/revision differs from the config or whose dimensions differ fails closed (nothing persisted,
  failure audit). A same-identity row with other dimensions cannot be replaced (D-014) and is never deleted: the
  document stays truthfully not ready (`EMBEDDING_IDENTITY_INCOMPATIBLE`, no provider call).
- No migration, no dependency, no deletion/flagging of historical embeddings, no profile/identity rework.

## Authority and concurrency

Unchanged S9 shape: Phase A (short, tenant/folder/candidate/document/profile authority, plan, commit) -> embedding
inference with no SQL transaction, pooled connection or row lock -> Phase B (Tenant SHARE -> Candidate UPDATE ->
folder/document/profile revalidation -> reuse-or-persist -> commit). No new lock. Verified: concurrent runs with a
stale embedding create exactly one compatible row with no failure; delete during embedding inference is
`superseded` with no embedding written; tenant suspension wins (`TenantInactiveError`, only the old embedding
remains); zero backends idle in transaction and zero relation locks during stale re-embedding.

## Results after the fix (asserted)

Old model / revision / serializer / source hash: next run `processed=1, ready_after=1, failed=0`, exactly ONE
embedding call, profile and identity inference calls = 0, profile/identity row ids unchanged, historical rows kept,
the following run quiesces (`already_ready == candidates_considered`, no calls, no new rows). Exact active
embedding: ready, no call, no duplicate. Multiple historical embeddings: ready via the exact compatible one, 0
calls, nothing added. Diverged tracked documents: both re-embedded against their own profile (2 calls, 0
inference), then quiesce; exactly one effective profile remains searchable (D-100). Wrong result dimensions fail
closed; stored wrong-dimension identity is not ready and kept.

## Initial S10 gates (audited head, historical)

Actual output: full `pytest -q` 3861 passed; `ruff check .` clean; `mypy src` clean (233 files); `alembic heads`
`b88a2c4d6e10 (head)` (unchanged); `git diff --check` and `scripts/scan-tracked-tree.sh` clean; focused (S10 new 14,
S9/D-110/D-111, S4, S5/S6, S7, S8, folder reconciliation + CLI, tenant authority/isolation, effective profile,
embedding service/repo, semantic + hybrid search + provenance, factuality, no-exfiltration/privacy, busy deferral,
docs policy): 503 passed. Existing tests updated only to pass the now-explicit active embedding configuration
(`_is_ready` / `_process_one_candidate_document` direct callers, the reconcile-folder CLI test patches
`get_embedding_search_config`).


## PR #126 acceptance corrective — direct embedding configuration boundary

Starting audited head: `34ef16817dcca6981029cfa409a6599837c486ba`, branch
`fix/46-s10-embedding-readiness`. Live GitHub before edits: PR #126 OPEN/unmerged,
main still `51ae314a74585902a5aecb164004b52cf00a0df9`; audited-head CI run
37218194000 attempt 1 SUCCESS. Existing M9 milestone (10), no new PR or milestone.

### Before any production change: real PostgreSQL reproduction

Added the focused `test_direct_cli_wrong_dimensions_does_not_poison_folder_identity` on the exact
starting production tree, using synthetic DOCX ingestion, real PostgreSQL/pgvector, deterministic
local providers and the actual `cli._embed_candidate` path. Seeded authorized profile/identity with
an old-model embedding; patched the trusted `get_embedding_search_config()` to matching active-model
identity, serializer v1, dimensions 8; active provider returned a valid 4-dimensional vector.

Actual output: **1 passed in 1.11s** (the characterization asserted the defect). CLI succeeded and
printed `Dimensions: 4`; PostgreSQL contained the old 2-dimensional row plus a NEW active-model
4-dimensional row. The next folder reconciliation with the same active identity and dimensions 8
reported `ReconciliationSummary(candidates_considered=1, already_ready=0, processed=1,
ready_after=0, failed=1, skipped_due_to_limit=0, deferred=0, superseded=0)`;
the valid 8-dimensional repair provider had **0 calls**. The direct row had occupied the immutable
D-014 identity. Folder `prepare_embedding` refuses this state as `EMBEDDING_IDENTITY_INCOMPATIBLE`;
the folder summary surfaces a failure (it does not expose or audit that precondition code).
The regression now asserts the corrected behavior, with both direct-CLI and folder retry variants.

Root cause: `_embed_candidate` omitted the trusted active config, `embed_candidate_profile` omitted
compatibility in `prepare_embedding`, and it persisted returned output without application config
validation. Provider numeric-vector validation cannot know configured dimensions. This was a current
production path defect, not merely a historical corrupt-row residual.

### Production correction and shared design

- CLI resolves trusted `get_embedding_search_config()` once and passes explicit `EmbeddingCompatibility`.
- Direct `embed_candidate_profile` requires compatibility and configured dimensions (no implicit settings,
  no dimension-unconstrained direct path). `prepare_embedding` checks provider/model/revision/serializer
  identity and exact compatible existing-row reuse before inference.
- `validate_embedding_result` is ONE application result check used by direct embedding and folder embedding:
  returned provider/model/revision, active serializer, returned dimensions versus vector length, and configured
  dimensions must match. Invalid output raises `EMBEDDING_INVALID_OUTPUT` before persistence.
- Direct CLI commits the existing closed `CANDIDATE_EMBEDDING_FAILED` audit before reporting exit 3.
  Its metadata has only candidate/profile IDs and error code; no vector, CV text or raw provider message.
  Tenant guarded commit remains active. Exact-compatible reuse stays no-call.
- Synthetic demo and existing service tests now supply explicit synthetic compatibility. The demo's fixed
  vector length is intentional demo config; production CLI always uses trusted settings.
- No schema, repository settings read, dependency, historical deletion or immutable-identity change.

With configured dimensions 8 and a valid 4-dimensional result: fail closed, no active-model row, safe
CLI error and durable failure audit. A later valid 8-dimensional retry via either direct CLI or folder
succeeds, calls the provider once, creates one compatible row, and then direct reuse/folder READY make
no inference call. Profile/identity versions are unchanged. Provider/config identity and returned-result
identity mismatches also fail closed. Direct calls with missing configured dimensions are refused.

Historical wrong-dimension rows remain immutable and truthfully not ready: both folder reconciliation
and the direct service refuse the occupied identity without calling the provider. The historical row
is retained; dimensions remain outside D-014 identity, so operator repair remains a residual. Normal
production embedding paths can no longer create that state from a wrong configured result.

### Source derivation accuracy

Chose option A: moved `embedding_source()` to dependency-neutral `meyar.embedding.serializer` and made
creation, folder readiness AND `search.service` call it. Previously search independently called
`compute_source_sha256(build_professional_embedding_text(...))`; the earlier shared-helper wording was
inaccurate on the audited head. Output text/hash and serializer version are byte-for-byte the same;
no circular service/search import was added. The shared SQL `EmbeddingCompatibility.conditions()`
predicate remains unchanged. D-112 now records the actual helper location and callers.

### Preserved authority and deferred scope

D-100 effective search authority, D-110 tracked-document authority and D-111 document-level readiness
are unchanged. Configuration changes trigger only embedding work; no profile/identity re-extraction.
Folder Phase A commits before inference; inference holds no SQL transaction, pooled connection or lock;
Phase B remains Tenant SHARE → Candidate UPDATE → exact folder/document/profile revalidation →
persist/reuse → commit. No locks or transaction boundaries were added to the folder path.

Direct embedding remains the legacy single-transaction service/CLI path; its transaction separation
is explicitly deferred to #46. This correction does not complete #46 or advance/close #35/#36/#45/#50.
Other #46 scope still includes retention/orphan cleanup and remaining ingestion/resource/runtime/config/
security hardening, as recorded in STATUS. No Target-Mac execution or deployment acceptance is claimed.


### Corrective focused verification (final production/test changes)

Actual output: **628 passed in 111.86s (0:01:51)**, no exclusions, real PostgreSQL.
This includes S10, S9/D-110/D-111, overlap/dedup, changed-file authority, compensation,
delete/photo concurrency, direct embedding/CLI, D-100, exact semantic/hybrid compatibility,
tenant suspension/isolation and diagnostic/privacy/local-only gates. The new negative tests cover
provider/config provider/model/revision/serializer mismatch, returned provider/model/revision mismatch,
reported dimension versus vector-length mismatch, missing configured dimensions, and the wrong-dimension
CLI → valid CLI/folder retry paths. Historical incompatible identity refusal is checked in the direct
service too. Twelve additional test cases versus the audited head; existing regressions retained.

Final focused command from `backend/`:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q \
  tests/test_folder_embedding_readiness.py \
  tests/test_folder_downstream_concurrency.py \
  tests/test_folder_reconcile_concurrency.py \
  tests/test_folder_changed_authority.py \
  tests/test_folder_reconciliation.py \
  tests/test_folder_reconciliation_cli.py \
  tests/test_folder_indexer.py \
  tests/test_folder_indexer_cli.py \
  tests/test_folder_ingestion_compensation.py \
  tests/test_candidate_delete_upload_concurrency.py \
  tests/test_photo_delete_concurrency.py \
  tests/test_candidate_embedding.py \
  tests/test_identity_embedding_cli.py \
  tests/test_m9_effective_profile.py \
  tests/test_candidate_factual_authority_backstop.py \
  tests/test_factuality_context_policy.py \
  tests/test_search_semantic.py \
  tests/test_search_semantic_provenance.py \
  tests/test_search_hybrid.py \
  tests/test_search_structured.py \
  tests/test_api_search.py \
  tests/test_search_tenant_authority.py \
  tests/test_tenant_active_authority.py \
  tests/test_tenant_isolation.py \
  tests/test_no_exfiltration.py \
  tests/test_audit_privacy_guard.py \
  tests/test_logging_privacy.py \
  tests/test_logging_privacy_runtime.py \
  tests/test_inference_busy_deferral.py \
  tests/test_docs_environment_policy.py \
  tests/test_candidate_photo_noninterference.py
```

### Corrective changed files

- `backend/src/meyar/cli.py`
- `backend/src/meyar/embedding/serializer.py`
- `backend/src/meyar/search/service.py`
- `backend/src/meyar/services/candidate_embedding_service.py`
- `backend/src/meyar/services/demo_seed_service.py`
- `backend/src/meyar/services/folder_reconciliation_service.py`
- `backend/tests/fakes.py`
- `backend/tests/test_candidate_embedding.py`
- `backend/tests/test_factuality_context_policy.py`
- `backend/tests/test_folder_embedding_readiness.py`
- `backend/tests/test_identity_embedding_cli.py`
- `backend/tests/test_m9_effective_profile.py`
- `backend/tests/test_tenant_active_authority.py`
- `docs/DECISIONS.md`
- `docs/ISSUE_46_S10_VALIDATION.md`
- `docs/STATUS.md`

Production/tests were reviewed before the gates. Initial focused test adaptations failed on expired
observer objects and old serializer monkeypatch locations; corrected without weakening production config.
The final focused suite passed. Untracked `.aws` was neither read nor staged.


### Corrective full quality gate (actual output)

- `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q`: **3873 passed in 574.06s (0:09:34)**.
- `UV_CACHE_DIR=/tmp/uv-cache uv run ruff check .`: `All checks passed!`
- `UV_CACHE_DIR=/tmp/uv-cache uv run mypy src`: `Success: no issues found in 233 source files`.
- `UV_CACHE_DIR=/tmp/uv-cache uv run alembic heads`: `b88a2c4d6e10 (head)` (unchanged single head).
- `git diff --check`: exit 0, no output.
- `scripts/scan-tracked-tree.sh`: `Tracked-tree secret/real-data scan: clean.`

Full and focused PostgreSQL suites ran sequentially, with no diagnostic override or excluded tests.
No production/test change occurred between the final focused and full gates. Final documentation records
these actual results. Delivery continues on the same PR #126 branch, normal push only; exact-new-head
CI and OPEN/unmerged issue/PR state will be verified in the operational handoff. Independent re-audit is
required before any merge; this report does not declare S10 accepted or #46 complete.
