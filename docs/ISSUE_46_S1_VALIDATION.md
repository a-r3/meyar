# Issue #46 S1 — Tenant Active Authorization

Status: independently ACCEPTED + owner-MERGED in PR #116; #46 is NOT complete.
Only S1 under existing #46 / **M9 — Deployment, Benchmark & Integration
Readiness** (milestone 10). No merge or auto-merge by the agent.

## Independent acceptance and owner merge

Accepted exact head: `436e01589d86cc6db2d7926d2801525e9555b702`.
Exact-head CI [37106111151](https://github.com/a-r3/meyar/actions/runs/37106111151)
completed SUCCESS. Owner squash/main: `95b65313920463af6ee03d4eb0072244e25ff089`.
Accepted and merged full trees independently match:
`8c017e6eeae367fbb411e88e64a2faf4af2d9b32`. PR #116 is MERGED;
#46/#35/#36/#45/#50 remain OPEN. The record below describes historical S1 work;
only the later S2 logging/privacy proposal has now started. No S1 semantics change.

## PR #116 acceptance blocker correction (historical)

Reviewed head `d253b3d12c2f86a7b5c417f7ee060131470de8cb` is
**acceptance-REJECTED**, despite successful CI run 37102399674. The correction
amended the same branch and PR #116. The corrected head was independently
accepted and owner-merged as recorded above; no agent merge occurred.

The rejected search checked authority at entry and at final response creation,
but used the embedding result, queried compatible embeddings and built candidate
results between the query provider's DB-release commit and the final check.
Two before-fix synthetic cases (SEMANTIC_ONLY/HYBRID with the real DB-release
wrapper) failed: candidate embedding lookup was awaited once after suspension.

The only production correction is in `backend/src/meyar/search/service.py`:
`require_active_tenant(db, tenant_id, lock=True)` immediately after query
`embed()` returns. It precedes all embedding-result provenance/vector checks,
compatible-embedding lookup and use of preloaded candidate facts to build results.
The short post-inference transaction holds Tenant FOR SHARE and re-registers the
outer commit guard. REST/UI DB release before inference is preserved: no pooled
connection, transaction or Tenant lock is kept during local query inference.
The same check works with direct providers and agent `BoundaryEmbedding` (its earlier principal
re-entry remains unchanged). The existing TenantInactiveError transports refuse
without candidate results: generic API 401/Bearer, UI login redirect/cookie clear,
and typed service/safe CLI refusal.

Boundary review: structured-only search has no inference; semantic/hybrid uses
one query-embedding boundary shared by REST, UI and NL execution. Planner model
calls and bounded repair do no candidate retrieval; every outcome re-establishes
authority in `_audit_plan_result()` before returning, and executable plans enter
the guarded search service. REST/UI identity enrichment/rendering occurs after
search returns and before the guarded final commit. No equivalent Tenant-active authority
gap was found elsewhere in these paths; no planner/route/wrapper production
change is needed. Existing provenance/ranking/isolation behavior remains intact.

New `backend/tests/test_search_tenant_authority.py`: **25 parameterized cases**:

```text
test_direct_query_embedding_loss_refuses_before_candidate_use[release_db=True/False × SEMANTIC_ONLY/HYBRID]
test_tenant_refusal_precedes_embedding_result_validation[SEMANTIC_ONLY/HYBRID]
test_rest_query_embedding_tenant_authority_and_active_control[disable=True/False × natural_language=True/False × SEMANTIC_ONLY/HYBRID]
test_ui_classic_query_embedding_tenant_authority_and_active_control[disable=True/False × SEMANTIC_ONLY/HYBRID]
test_natural_language_service_query_embedding_refuses_candidate_use[SEMANTIC_ONLY/HYBRID]
test_disable_just_before_query_db_release_is_rejected_by_commit_guard[SEMANTIC_ONLY/HYBRID]
test_planner_inference_loss_preserves_existing_post_model_authority[service/REST/UI]
```

Assertions include no candidate-embedding lookup or result construction after
loss, refusal before returned-result validation, no candidate IDs/profile/name
in refusal responses, unchanged active results, real pool checkout count zero
inside query/plan inference, and refusal before the inner provider is called if
suspension occurs immediately before the DB-release commit. All data is synthetic.

Correction gate: new regressions **25 passed in 7.66s**. Required focused suites
**422 passed in 73.67s (0:01:13)**; full gate **3,568 passed in 799.21s
(0:13:19)**. Ruff clean; mypy(src)
clean (227 source files); unchanged single Alembic head `b88a2c4d6e10`. No migration/dependency/lockfile change. All S2+,
other-issue and Target-Mac deferrals remained in force at S1 delivery. S1
acceptance/merge is now recorded above; issue #46 remains OPEN.

The first correction full run had **1 failed, 3,567 passed in 816.54s**:
existing unmodified `test_docx_tables.py::
test_existing_header_variants_shared_parts_and_no_definition_creation[footer]`
compared two independently serialized DOCX ZIP archives. The mismatch at byte
28717 maps to local-header offset 10 (ZIP modification time) for word/settings.xml.
A controlled two-second serialization-time change reproduced differing archive
bytes with identical names and every XML/payload part. The unchanged header/footer
cases passed on recheck (2 passed in 0.79s). Parser/test stabilization is deferred;
no parser source/test, clock patch or test exclusion is part of this correction.
The normal full rerun passed all 3,568 tests with the same production/test code.

## Starting authority and scope

Verified local `main`, `origin/main`, live GitHub `main` and remote-head SHA:
`41833a91aaa1a57643a5020737d5574a80114cd5` (expected base).
PR #115 is MERGED with that squash SHA. Hooks path: `.githooks`.
Branch: `fix/46-s1-tenant-authority`.
Live #46/#35/#36/#45/#50 are OPEN; M9 is open. Canonical root is discovered
with `git rev-parse --show-toplevel`; all paths below are relative to that root.
The existing untracked `.aws` entry was neither opened nor modified.

S2+ logging/privacy, HTTP cache/docs policy, demo ownership, schema drift,
storage recovery, readiness, retention implementation, folder concurrency/
content leases/dedup, folder inference transaction separation, #45/#35/#36/#50
and Target-Mac execution were not started. No migration, dependency or lockfile
change. No candidate data was sent to an external AI/service; all new fixtures
and controlled providers are synthetic.

## Root cause and implemented authority

The previous API authentication checked key validity and touched last_used
without checking Tenant. UI login/session resolution checked human credentials
and memberships; agent re-entry checked User, Membership and BrowserSession.
Neither enforced the tenant's active state. Processing entrypoints could persist
inference/parser output after tenant suspension.

D-101 and the shared `meyar.services.tenant_authority` implement scalar live DB
checks, a closed refusal and final commit serialization. Missing/inactive tenants
fail closed. The current outer transaction stores only tenant IDs requiring a
commit check, never cached active decisions. The SQLAlchemy before_commit hook
re-reads every registered Tenant with FOR SHARE in UUID order, using the actual
committing AsyncSession transaction/connection, before its final flush/commit.
It does not run at savepoint release. Registration survives savepoint completion
and repeated failed commits and clears only at outer transaction end. Helper
refusal rolls back generated pending state; callers roll back failed commits.

Enforcement points:

- API authentication holds live Tenant FOR SHARE before last_used; inactive
  requests return the existing generic 401 with Bearer challenge. Authenticated
  API DB phases retain a Tenant shared lock, including ordinary reads/writes.
- UI filters inactive tenants from login memberships and displayed choices,
  locks User -> Tenant before membership selection/session creation, and checks
  live tenant authority on each session resolution. Session failure clears the
  cookie/redirects to login; login errors keep the generic response. UI resolution
  locks User FOR SHARE before Tenant FOR SHARE, so logout cannot invert the
  tenant guard against BrowserSession revocation. BrowserSession is freshly
  re-read and SHARE-locked last, including after waiting for a concurrent
  deactivate/reactivate cycle; the earlier cookie snapshot grants no authority.
- Agent reservation takes User -> Tenant before conversation. Every re-entry
  and fresh final Phase B locks User -> Tenant -> Membership -> BrowserSession
  FOR SHARE before the unchanged conversation/context/dialogue/submission order.
  Tenant loss maps to PRINCIPAL_REVOKED and the existing safe login outcome.
  Generated transcript, pointers, tasks and clarifications are not applied.
- Upload releases its initial DB phase during bounded validation/parsing, then
  locks Tenant -> ApiKey -> Candidate FOR SHARE before document persistence.
  Post-commit photo/detail work also checks live authority.
- Index/reconciliation, direct ingestion/profile/identity/embedding and photo
  processing check entry authority and recheck before produced writes. The final
  commit guard serializes caller-owned commits and reconciliation per-candidate
  commits. Loss during any reconciliation stage rolls back the whole current
  candidate, without changing already committed candidates. Suspension is never
  converted into a new failed profile/identity/photo/application result.
- Search/planning and existing tenant-bound operator processing commands use
  the same boundary. NL planning ends its short initial check transaction before
  model inference and restores live authority when appending its audit.

## Suspension/reactivation and lock order

Supported service: `set_tenant_active`; supported operator commands, from backend:

```bash
uv run meyar disable-tenant --tenant-id <tenant-uuid>
uv run meyar enable-tenant --tenant-id <tenant-uuid>
```

Deactivation locks affected Users (UUID order) -> Tenant -> tenant Memberships
(UUID order) -> revokes BrowserSessions. User/Tenant/Membership mutation locks are
FOR NO KEY UPDATE, compatible with FK FOR KEY SHARE locks from existing inserts,
but conflicting with authority FOR SHARE. There is no User acquisition after a
Tenant or Membership mutation lock. Existing password/user/membership mutators
remain compatible with the new order. Unrelated memberships and sessions remain
usable, and only this tenant's membership security_version stamps rotate.
User security_version and API keys are unchanged.

Reactivation never clears revoked_at or restores membership stamps. Old browser
sessions and pending claims therefore remain invalid; a valid non-revoked,
unexpired API key can resume. A pending token's unrelated membership choice
remains valid. Raw SQL tenant state is checked live, but operators must use the
supported service for permanent session/claim invalidation across reactivation.

If Phase B/final commit holds authority first, deactivation waits until the
consequential transaction commits. If deactivation owns the rows first, the
application waits, re-reads suspended state and refuses/rolls back. Suspension
cannot commit between the checked authority and final consequential commit.
Actual PostgreSQL lock-wait regressions exercise both directions for agent
Phase B, the outer commit hook, and authenticated UI/logout.

Every agent model/embedding boundary still commits before inference, releasing
all pooled connections, transactions and row locks (#85); final Phase B remains
fresh (#87/D-093). Background checks do not take Tenant authority locks across
inference. Existing folder transactions/connections and their FK locks remain
unchanged and deferred. S1 does not provide filesystem storage compensation.

## Exact production manifest

```text
backend/src/meyar/agent/turn_boundary.py
backend/src/meyar/api/v1/candidates.py
backend/src/meyar/cli.py
backend/src/meyar/core/auth.py
backend/src/meyar/extraction/deferral.py
backend/src/meyar/extraction/identity_service.py
backend/src/meyar/extraction/service.py
backend/src/meyar/search/planner_service.py
backend/src/meyar/search/service.py
backend/src/meyar/services/browser_session_repo.py
backend/src/meyar/services/candidate_document_service.py
backend/src/meyar/services/candidate_embedding_service.py
backend/src/meyar/services/candidate_photo_service.py
backend/src/meyar/services/folder_indexer_service.py
backend/src/meyar/services/folder_reconciliation_service.py
backend/src/meyar/services/tenant_authority.py
backend/src/meyar/services/tenant_membership_repo.py
backend/src/meyar/ui/auth.py
backend/src/meyar/ui/router.py
```

## Exact regression manifest

New `backend/tests/test_tenant_active_authority.py`:

```text
test_inactive_rest_reads_writes_last_used_and_other_tenant
test_single_membership_inactive_login_uses_generic_error
test_inactive_tenant_not_offered_in_multiple_choices
test_deactivate_reactivate_revokes_only_tenant_sessions_and_claims
test_live_session_and_session_creation_reject_direct_inactive_state
test_pending_selection_checks_tenant_without_session_or_stamp_changes
test_upload_deactivated_during_parser_has_no_document_authority
test_folder_deactivated_during_parser_rolls_back_scan
test_direct_processing_disabled_during_provider_has_no_generated_version[profile/identity/embedding]
test_reconciliation_disabled_during_each_stage_rolls_back_whole_candidate[profile/identity/embedding]
test_final_commit_rechecks_live_tenant_and_cannot_be_bypassed
test_savepoint_does_not_clear_outer_commit_authority
test_outer_commit_serializes_with_tenant_disable[True/False]
test_photo_authority_loss_does_not_create_terminal_failure
test_ui_logout_and_deactivation_preserve_security_lock_order[True-False/True-True/False-False]
test_direct_cli_processing_rejects_inactive_tenant[_extract_profile/_extract_identity/_embed_candidate/_index_folder/_reconcile_folder]
```

Added cases in `backend/tests/test_agent_inference_boundary.py`:

```text
test_issue87_security_change_during_inference_rejects_phase_b[tenant]
test_issue87_phase_b_holding_authority_blocks_security_change_commit[tenant]
test_issue87_uncommitted_security_change_makes_phase_b_wait_then_fail_closed[tenant]
test_tenant_disabled_while_inferring_has_no_agent_consequences[True/False]
```

The True/False agent cases exercise supported deactivation and direct Tenant-only
state change independently. They verify zero pooled connection during controlled
inference and no transcript/turn version/pointer/task/clarification/ResultSet
consequence after loss. Existing synthetic #85/#87 and isolation tests stay intact.

Compatibility updates in existing regressions:

- `backend/tests/test_issue88_slice_a_phase_b.py`: add Tenant to the principal
  lock order and distinguish actual Phase B conversation consequences from
  reservation updates. Ignore reacquisition of this request's already-held
  principal rows while preserving fresh-transaction and consequential lock-order
  assertions across all nine existing scenarios.
- `backend/tests/test_search_planner_service.py`: twelve planner policy cases
  now use an active synthetic tenant and real test session for the new initial
  authority check, retaining all existing policy and no-model-call assertions.

## Historical validation — acceptance-rejected initial head

Initial quality-gate results for `d253b3d12c2f86a7b5c417f7ee060131470de8cb`
are recorded below; these did not detect the query-embedding acceptance blocker.
Commands run from backend, with `UV_CACHE_DIR=/tmp/meyar-s1-uv` for a writable cache:

```bash
uv run ruff check .
uv run mypy src
uv run pytest -q
uv run alembic heads
```

Whole-repository mypy is not claimed. The Alembic head is unchanged:
`b88a2c4d6e10 (head)`; no migration was added.
Root checks: `git diff --check` and `scripts/scan-tracked-tree.sh`.

Final source checks: Ruff `All checks passed!`; mypy(src) `Success: no issues
found in 227 source files`; Alembic `b88a2c4d6e10 (head)`. Final authenticated
UI/session/pending/agent/tenant/lock-order regressions: **122 passed in 28.60s**.
Compatibility regressions: **71 passed in 8.82s**.
Full pytest result: **3,543 passed in 488.76s (0:08:08)**. Diff check clean; tracked-tree
scan `Tracked-tree secret/real-data scan: clean.`

Earlier focused runs (before the final UI/logout order adjustment): 56 tenant/
agent passed in 13.56s; 387 affected-boundary tests passed in 154.84s; 98 final
processing/commit-hook/agent/photo/existing-CLI regressions passed in 102.40s.
These are intermediate evidence; final-head full gates govern delivery.
The initial completed full run found 21 test-compatibility failures (nine omitted
Tenant from the lock probe; twelve passed no DB session to the planner) and 3,522
passes. The compatibility updates above resolved the focused failures; the final
full rerun passed all 3,543 tests.

Exact delivered PR head and its exact-head CI run are published in the PR and
operational delivery report, without attempting a self-referential Git commit.
These initial-head gates are historical. Corrected-head S1 acceptance and
owner merge are recorded above; the initial head remains rejected.
