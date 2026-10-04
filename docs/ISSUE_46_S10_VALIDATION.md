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
  (reused, not a second definition). `candidate_embedding_service.embedding_source` is the shared text/hash
  derivation; `prepare_embedding` gained optional `compatibility`.
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

## Gates

Actual output: full `pytest -q` 3861 passed; `ruff check .` clean; `mypy src` clean (233 files); `alembic heads`
`b88a2c4d6e10 (head)` (unchanged); `git diff --check` and `scripts/scan-tracked-tree.sh` clean; focused (S10 new 14,
S9/D-110/D-111, S4, S5/S6, S7, S8, folder reconciliation + CLI, tenant authority/isolation, effective profile,
embedding service/repo, semantic + hybrid search + provenance, factuality, no-exfiltration/privacy, busy deferral,
docs policy): 503 passed. Existing tests updated only to pass the now-explicit active embedding configuration
(`_is_ready` / `_process_one_candidate_document` direct callers, the reconcile-folder CLI test patches
`get_embedding_search_config`).
