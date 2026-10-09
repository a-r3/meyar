# Issue #46 final closure audit and implementation

Status: **independently ACCEPTED + owner Squash-MERGED through PR #128; #46 CLOSED**.
Accepted head `254895c2b6890019c84bff90f00b88038854a097`; accepted-head CI
[37937363994](https://github.com/a-r3/meyar/actions/runs/37937363994) SUCCESS.
Verified squash/main `386ae9da22f2e7cbb4286cde439292d8f21e25e4`;
accepted and merged full tree `96a9934ffbd997cee5d7d5c2b0f3e2ba185d2b94` (identical).

The initial rejected head, SAME-PR D-114 correction and recorded pending instructions
below remain historical. They are not rewritten as if the rejected head was accepted.
`fix/46-final-closure` is the historical implementation branch, not the normalization branch.
D-115 / [ROADMAP_NORMALIZATION.md](ROADMAP_NORMALIZATION.md) owns current sequencing.

## Starting authority (live verified at the historical task start)

GitHub main / PR #126 squash: `532bd295f1e63926a922eee5ddf6ce0200594dfd`.
Parent: `51ae314a74585902a5aecb164004b52cf00a0df9`.
Tree: `8b55824e4d59e3d6ed5b31bf75ee312b851c4cec`.
PR #126 is MERGED, accepted head `6277b77f0c39ca69dd0e6f9515961463e6269f54`;
its tree equals main's complete tree. CI run 37327942757 attempt 1 is SUCCESS
on that exact head; job 111823362240 completed **3873 passed in 616.85s** (fresh job log, not stale cached output).
Local main was fast-forwarded to this commit and verified clean before the task
branch was created. The pre-existing empty untracked `.aws` file was preserved
outside the checkout at `/tmp/meyar-issue46-preserved-empty-aws`; no contents read.

Live #46/#35/#36/#45/#50: OPEN. M9: OPEN (4 open / 33 closed issues).
Live #80/#84/#85/#86/#87/#88: CLOSED. Merged PRs #89/#90/#91/#92/#98/#100/#102
confirm their respective implementation history. No capability work is authorized.

## Complete pre-production closure matrix

A = already satisfied; B = reproduced residual; C = accepted external ownership;
D = deliberately outside #46 with existing authoritative ownership. Paths below
are repository-relative; source paths start `backend/src/meyar/`, test paths
start `backend/tests/`. These statuses record the baseline, before production edits.
Acceptance/checkmarks in the live issue are not the evidence authority.

| Requirement | Baseline | Code authority | Regression / accepted delivery / residual |
|---|---|---|---|
| Bound upload before full body consumption | A | `api/body_limit.py`, `ingestion/bounded_read.py`, `api/v1/candidates.py` | `test_upload_body_limit.py`; D-096, merged #106; counted ASGI receive and bounded handler reads |
| DOCX expanded bytes/member count/ratio | A | `ingestion/validation.py` | `test_docx_archive_safety.py`; D-096/#106; declared AND counted expansion, 1000 members, 32 MiB/member, 128 MiB total, 100x above 1 MiB floor |
| Parser elapsed-time / termination | A | `ingestion/parser_supervisor.py`, `ingestion/parser_policy.py` | `test_parser_isolation.py` real child timeout, cancellation, late-spawn/reap, admission cases; D-097/#108; 20s worker, 0.5s graceful cleanup then kill/reap |
| Parser memory, output, page/document work | A | `ingestion/parser_worker.py`, `ingestion/parser_output.py`, `ingestion/parsers/docx_source.py` | `test_parser_isolation.py`, `test_docx_tables.py`; D-097/#108 and D-098/#110; 768 MiB worker floor, 300 pages, 10000 blocks, 1M chars, 4 MiB text, 8 MiB IPC, 100000 DOCX nodes/depth 8 |
| Scanned/image-only/empty text | A | `ingestion/parser.py`, `ingestion/parser_output.py` | `test_parser_isolation.py::test_no_extractable_text`, `test_parser_failure_integration.py`; D-097/#108; terminal insufficient-text, original retained, no canonical authority/inference; no OCR |
| Malformed isolation | A | parser worker/supervisor and `services/candidate_document_service.py` | real-child failure/recovery and failure integration suites; D-097/#108; operational refusal precedes storage; terminal bad content has no canonical authority |
| DOCX supported-content completeness | A | `ingestion/parsers/docx_source.py`, `ingestion/parser.py` | `test_docx_tables.py`, `test_docx_table_integration.py`, `test_docx_review_corrections.py`; D-098/#110; ordered paragraphs/tables, bounded provenance; headers/footer/text boxes disclosed as omitted, unsupported-only terminal refusal |
| Production blank/default secrets | A | `config.py::_production_safety`, `ops/host_config.py` | `test_ops_host_config.py::test_direct_settings_production_fails_closed`; merged #68 (#35 PR5) host-config work; development secret/password rejected |
| Invalid runtime env values | B | `config.py` | `test_runtime_closure.py`: 10 invalid values accepted (0 bytes, negative rate/inputs/timeouts, dimension 0, external embedding provider, blank storage, invalid timezone); one infinity case already refused by reservation validator; this PR |
| Active tenant everywhere | A | `services/tenant_authority.py`, core/UI auth, processing/search entry points | `test_tenant_active_authority.py`, `test_search_tenant_authority.py`; D-101/#116; guarded commit and post-inference revalidation |
| Liveness vs full readiness | B | `api/v1/health.py`, existing `ops/readiness.py` primitives | `test_runtime_closure.py::test_readiness_refuses_unreachable_database_while_liveness_is_up`: health/ready 200 with dead DB; only saturation is currently tested; DB/schema/Ollama/models/storage checks needed here |
| Candidate API no-store/security headers | A | `api/response_policy.py` | `test_http_response_policy.py`; D-103/#118; JSON, errors, 413, stream/file/204 |
| SQL/exception/CLI diagnostic privacy | A | `diagnostics.py`, all engine constructors, safe error boundary | `test_logging_privacy.py`, `test_logging_privacy_runtime.py`, `test_ops_diagnostic_privacy.py`, `test_audit_privacy_guard.py`; D-102/#117; closed structural projection, hidden binds, no raw payload tracebacks |
| Synthetic exclusive demo credential/reset | A | `services/demo_seed_service.py` | `test_demo_user_ownership.py`, `test_demo_seed.py`; D-103/#118; immutable exact-user marker chain + all memberships, no name-derived ownership |
| Offline Swagger / ReDoc | A | `main.py` docs registration, local Swagger assets | `test_docs_environment_policy.py`; D-103/#118; production docs/schema/assets absent, ReDoc disabled, local nonprod Swagger |
| Conversation creation / concurrent turns | C | `services/agent_conversation_repo.py`, `agent/turn_boundary.py` | `test_agent_conversation_authority.py`, `test_agent_inference_boundary.py`; D-086/D-089; #80 and CLOSED #85, merged #90; DB reservation/version, no local mutex authority |
| Folder overlap/idempotency | A | `services/folder_indexer_service.py`, `services/folder_reconciliation_service.py` | `test_folder_reconcile_concurrency.py`, `test_folder_downstream_concurrency.py`; D-107/#122 and D-110/D-111/#125; source lock, staged inference, exact tracked document authority |
| Concurrent same-content ingestion | A | folder indexer tenant+hash advisory authority | `test_folder_reconcile_concurrency.py`; D-107/#122; duplicate link locks candidate; bounded deadlock retry |
| Changed-file read races | A | `ingestion/folder_scanner.py`, folder indexer | `test_folder_changed_authority.py`, `test_folder_scanner_bounds.py`; D-108/#123; two matching bounded byte observations + stable descriptor metadata; delete authority acquired before save |
| FS save/delete + DB compensation (normal faults) | A | `services/storage_recovery.py`, `storage/staging.py`, candidate/document/photo services | `test_folder_ingestion_compensation.py`, `test_candidate_delete_upload_concurrency.py`, `test_photo_delete_concurrency.py`; D-104–D-106/#119–#121; fresh durable reference authority even for ambiguous commit |
| FS hard-kill / orphan recovery | B | ephemeral session ledger, opaque unmapped `.trash` | controlled real SIGKILL child exits -9 at save/stage: one orphan original, DB-referenced original absent, one trash object, zero durable mappings; D-104 explicitly assigns this residual to #46; this PR |
| Interrupted extraction/identity/embedding | B | direct services vs accepted folder staged services | all 3 `test_direct_inference_boundary.py` cases FAIL: PostgreSQL idle-in-transaction, checked-out pool, relation/transaction locks; STARTED audit is uncommitted across call. Direct authority snapshot/revalidation and durable observability needed here |
| Configured embedding identity/readiness | A + B repair | `services/candidate_embedding_service.py`, `candidate_embedding_repo.py`, `embedding/serializer.py` | `test_folder_embedding_readiness.py`; D-112/#126 exact config/source/dimension; invalid new output refused. Historical incompatible immutable identity needs tested explicit operator recovery, no deletion/rewrite |
| Retention/orphan/session/conversation/log bounds | B + D policy | browser/session/conversation/audit models; `ops/cleanup.py` only cleans activation symlinks | no domain retention/sweep exists; killed storage reproduction above. Implement one tenant-scoped bounded inspection/apply command. Bank legal ages remain explicit owner policy (MASTER_SPEC §20, SECURITY_PRIVACY retention); do not invent legal periods. Diagnostic stdout/host-log lifecycle belongs to #35; no arbitrary log-file deletion |
| M-4 amendment | A | DOCX rows above | D-098/#110; unsupported structures are truthful, not silently extracted; no scope expansion to OCR |
| M-5 amendment | A + B legacy cleanup | folder indexer + UI status | `test_folder_indexer.py`, `test_folder_reconciliation.py`; D-099/#112 prevents new phantom candidates. Legacy provenance cannot prove exclusive origin: administrative dry-run + explicit age/eligibility required here, never filename-based |
| M-9 amendment | A | `services/candidate_profile_repo.py`, profile/identity authority | `test_m9_effective_profile.py`; D-100/#114; same-document completion fallback, no cross-document/invalid-completion fallback |
| L-4 amendment | A | API response/docs rows | D-103/#118 |
| L-5 amendment | B + C | readiness row; agent saturation | general readiness residual here; saturation admission owned by CLOSED #85/#90/D-089, preserve it |
| L-6 amendment | B + C | retention row; `services/agent_result_set_repo.py` | ResultSet-specific retention is CLOSED #86/#91/D-090, `test_agent_result_set_retention.py`; do not duplicate it |
| L-1 amendment | B | `models/job.py`, accepted job-lifecycle migration | fresh `meyar_46_fresh`: upgrade head succeeds, head `b88a2c4d6e10`; check FAILS remove_index `ix_jobs_tenant_id_status`. Metadata correction and CI drift gate needed, no migration-history rewrite |
| DOCX footer flake | B test oracle | `test_docx_tables.py::test_existing_header_variants_shared_parts_and_no_definition_creation` | controlled ZIP clocks 4s apart: bytes unequal, every member name/content identical; fix semantic/package nonmutation assertion; no product nondeterminism found |
| Direct profile / identity / embedding authority | B | `extraction/service.py`, `identity_service.py`, direct embedding service and CLI callers | 3 real-PG failed boundary tests; fix short plan/commit → DB-free inference → fresh Tenant/Candidate and exact document/canonical/attempt/effective-profile check → guarded persist; derive direct authority separately from folder tracking |
| Direct search CLI query embedding | B | `cli.py::_search_candidates`, `_plan_search(execute=True)` | Both unchanged CLI production paths reproduced idle-in-transaction and relation/transaction locks during gated query embedding; **2 failed** real-PG cases. API/UI already use `embedding/db_release.py`; reuse that accepted boundary here |
| Photo worker cancellation/output lifecycle | B | `services/candidate_photo_service.py`, bounded subprocess supervisor | Actual child ignoring SIGTERM remained live after request cancellation; `test_photo_worker_lifecycle.py::test_cancelled_photo_worker_is_reaped` failed before production change. This PR reuses cancellation-safe spawn/shield/terminate/kill/reap and incremental IPC cap; no process survives cancellation, late spawn or overproduction |
| New JD/agent semantics and API/candidate capability work | D | PROJECT_VISION and live issues | #84/#88 accepted external semantics; #35/#36/#45/#49/#50 are not advanced; target-host and bank-owned operational/legal policy remain their named owners |

## Reproductions and root causes

Initial baseline test source was added before any production edit; later newly discovered
photo and CLI-query defects were each reproduced before editing their respective production lines. Actual results:
`test_direct_inference_boundary.py`: **3 failed**, each idle transaction and
locks during gated inference. `test_runtime_closure.py`: **11 failed, 1 passed**.
The dead-DB readiness response was `{"status":"ready"}` (200).
Fresh migration `alembic check` reported exactly the missing model index.
SIGKILL demonstrated an ephemeral recovery ledger cannot reconstruct staged
keys after process death. DOCX ZIP DOS timestamps invalidate archive byte equality
even though parsing leaves every archive member byte unchanged.

The extra photo-lifecycle reproduction failed before its production fix: cancelling an
actual child that ignored SIGTERM left `returncode=None`. It is included in this same PR.

The final call-path audit additionally reproduced **2 failed** CLI query-embedding cases
before changing those production lines. Both raw-provider callers held idle transactions and
checked-out pools. This extends the direct-inference residual; API/UI query release is accepted #85.

## Final matrix dispositions

Every baseline A remains satisfied by the cited accepted code and regression. C remains
accepted external ownership, verified live. D remains explicit external policy/capability
ownership, not an unassigned #46 implementation deferral. Every baseline B is resolved by
this PR as follows; independent acceptance/owner merge remains pending.

| Baseline B / amendment | Final code authority | Adversarial regression and outcome | #46 residual |
|---|---|---|---|
| Direct profile / identity / embedding transaction separation and interrupted work | `services/direct_inference_authority.py`, direct extraction and embedding wrappers | `test_direct_inference_boundary.py` (23 cases); separate PG observes idle/no xact, zero relation/transaction/tuple locks, pool checked out 0 during local inference. Delete/new document/canonical/profile/status/tenant mutations refuse stale persistence. `test_direct_inference_recovery.py` actual SIGKILL and concurrent winner tests | none |
| Direct search CLI query embedding | `cli.py`, existing `embedding/db_release.py` | Real-PG gated `_search_candidates` and `_plan_search(execute=True)` regressions require idle/no transaction/no relevant locks and pool 0; preserve API/UI and planner authority | none |
| Historical incompatible immutable embedding | shared trusted plan/result validation, explicit new immutable profile | `test_direct_inference_recovery.py::test_historical_incompatible_embedding_repair_preserves_immutable_history` preserves old row and creates a compatible embedding on the new profile; D-100 search authority moves normally | none; explicit operator repair, no automatic historical rewrite |
| Invalid runtime environment | `config.py` field and runtime validators | `test_runtime_closure.py` invalid upload/rate/timeouts/input/dimensions/provider/path/timezone rejected; existing production-secret guard retained | none |
| L-5 general readiness | `core/readiness.py`, `api/v1/health.py` | `test_runtime_closure.py` dead DB, schema mismatch, daemon/model absence, unwritable storage, healthy components; independent liveness and existing saturation tests preserved | none |
| Hard-kill original/photo recovery + L-6 orphan cleanup | `storage/staging.py`, `services/storage_authority.py`, `services/maintenance.py`, CLI | `test_maintenance.py` real SIGKILL after save, staging before DB commit, staging after DB commit; inspection, repeat idempotency, writer-busy safety, journal conflict/malformed/symlink refusal, referenced photo exact restore, legacy tenant hash recovery | none |
| L-6 retention and M-5 legacy empty candidates | same bounded maintenance service + D-114 confirmation decoupling | initial audited head had immortal confirmed sessions; corrected with historical session UUID + nullable SET NULL live FK; explicit age policy; expired session/context removed, live session/durable conversation preserved; live conversation reservation/context retained; audit demo ownership markers retained; total object/action and scan pagination tests; global auth retention separately explicit | none; legal ages remain bank/operator policy, host logs #35, ResultSet policy #86 |
| L-1 schema drift | `models/job.py`, `.github/workflows/ci.yml` | fresh accepted migration chain already has `ix_jobs_tenant_id_status`; metadata now mirrors it. Empty DB upgrade → one head → check clean | none; index correction needs no migration; D-114 adds its separate forward confirmation migration |
| DOCX footer flake | `tests/test_docx_tables.py` | compares every ZIP member name/uncompressed byte, preserving exact content/relationships; controlled unequal timestamped containers have identical member contents | none |
| Photo worker cancellation/output volume | `services/candidate_photo_service.py`, `ingestion/parser_supervisor.py` | `test_photo_worker_lifecycle.py`: actual child cancellation, delayed spawn, 2MB stdout overproduction are reaped; existing timeout/failure and photo noninterference/delete tests | none |

## Engineering and authority analysis

Profile and identity Phase A load bounded source views, durably record STARTED and snapshot
the selected document/canonical plus candidate status, document frontier and latest professional
attempt. Identity work also snapshots identity state; embedding never reads identity for suitability.
Every profile/identity retry revalidates in a short committed phase before entering the model gate.
Embedding commits a professional-source plan and STARTED before its one model call. At each gated
call the serving pool has **0 checked-out connections**, PG is **idle with xact_start NULL**,
and the backend holds **0 relevant relation/tuple/transaction locks**.

Fresh Phase B uses Tenant SHARE → Candidate UPDATE → document/canonical/attempt SHARE and
compares the fingerprint. A concurrent delete, upload frontier change, canonical/profile change,
archive or suspension refuses stale output. Explicitly selected historical direct documents are
permitted while the observed candidate frontier stays unchanged; folder-specific D-110 ownership
is unchanged. Two direct profile/identity workers accept one version and refuse the loser; two
embedding workers converge on the same compatible row. Caller owns the final guarded commit.
Both production search CLI paths reuse the accepted API/UI `DbReleasingEmbeddingProvider`;
gated query embedding likewise has idle/no transaction/no locks/pool 0. The planner already
commits before its LLM call; structured-only search still never calls embedding.
A pre-inference planning commit intentionally commits pending caller work; deterministic synthetic
seed uses staged functions inside its original atomic transaction and has no model transport.

SIGKILL at each direct stage leaves committed STARTED, no generated version and no held model-gap
DB transaction. Maintenance reports old STARTED with no matching version (observability, not proof
of death). Retrying the ordinary extraction/embedding command succeeds. It never fabricates a FAILED
version to change D-100 effective facts. Historical incompatible embeddings remain immutable;
explicit profile re-extraction plus ordinary embedding creates a fresh compatible immutable identity.

Storage save is coordinated with DB transaction advisory writer authority; process death releases
that lock. Maintenance takes exclusive TRY authority and skips busy work. A server-owned journal
(fixed fields, mode 0600, fsynced before unlink) maps trash to exact tenant namespace/key. Staging
also fsyncs the recovery link before unlink; restoration fsyncs its link before retiring trash.
Uncommitted staging is restored from DB references; committed deletion is purged. Conflicting bytes
are never overwritten. Legacy trash recovery checks exact SHA-256 against bounded same-tenant DB
references. Unknown/malformed/symlink/conflicting paths are retained as unresolved counts.

Each maintenance invocation bounds scanned entries, acted-on objects/rows, per-object legacy matches,
SQL statements and elapsed scheduling. Default: 100 acted-on objects/rows, 10000 scanned entries,
20s scheduling budget, 1-hour orphan grace. A legacy object's exact-match lookup is additionally
capped at the object limit; no arbitrarily large file is hashed (32 MiB cap). Filesystem syscalls
can still block in the kernel; the budget does not promise cancellation of a stuck mount. Scan
cursors are best-effort enumeration offsets, not a stable directory snapshot; repeat full cycles
when live activity changes entries. Every destructive action rechecks durable reference authority
under the exclusive writer lock. Counts contain no candidate content or host paths.

Session retention protects consequential AgentDraftConfirmation provenance without preserving
the entire BrowserSession indefinitely (D-114). Historical session UUID stays non-null; the live
FK becomes NULL on session deletion. Creation validates the same-tenant live session under SHARE
and stores both identities; lookup/replay still uses the exact live FK, never historical identity.
Expired/revoked sessions and their contexts/tasks/clarifications/submissions/ResultSets retire
after the explicit operator grace; confirmation, Job and immutable criteria remain unchanged;
durable conversations are preserved by default. Explicit conversation ages still protect live browser contexts and reserved
turns. Demo identity marker chains are retained during audit cleanup. Empty-candidate policy requires
an explicit age and absence of every DB candidate reference (including folder rows), never names or
assumed folder ownership. Tenant-independent AuthSecurityEvent needs its separately named GLOBAL
opt-in. ResultSet-specific retention is still D-090/#86 (which explicitly permits their existing
BrowserSession CASCADE when #46 retires the session); host diagnostic log lifecycle is #35.
Backup/restore must preserve the entire storage root including `.trash` journals. No backup/deployment
or Target-Mac lifecycle implementation is added.

## Operator commands and exit contract

Run from the checkout; replace the tenant UUID with the explicitly selected tenant. Inspection is
the default and rolls back DB work. Existing application configuration selects storage/DB; this
command has no HTTP authentication bypass and is a privileged local operator tool.

```bash
cd "$(git rev-parse --show-toplevel)/backend"
uv run meyar maintain --tenant-id 00000000-0000-0000-0000-000000000000
uv run meyar maintain --tenant-id 00000000-0000-0000-0000-000000000000 --apply
```

`--limit` 1..1000, `--scan-limit` 1..100000 and `--scan-offset` 0..1000000 bound work;
`next_scan_offset` continues enumeration, zero starts/restarts a full cycle. `--orphan-age-seconds`
defaults to 3600. Retention is off unless explicitly configured with `--session-days`,
`--conversation-days`, `--audit-days`, `--empty-candidate-days`; their ages are 1..36500 days.
`--global-auth-event-days` is explicitly GLOBAL, never silently tenant-scoped. Ages must come
from approved bank/operator policy, not this example. Restart full scans periodically to avoid
mutable-directory cursor starvation.

Exit 0: inspection/apply complete for the bounded view (old STARTED counts alone are not failure).
Exit 1: busy writer or incomplete budget, retry/continue. Exit 2: unresolved asset/policy refusal,
inspect before remediation. Exit 4: closed operational failure, no payload traceback. JSON includes
`apply`, closed count names, `complete`, `unresolved`, `busy`, `next_scan_offset`; no paths/PII.

## Ingestion and retained architecture

ASGI receive and handler reads enforce upload bounds before full body consumption. DOCX checks
both declared and counted ZIP expansion; parser limits include memory, wall-clock worker time,
stdout, canonical text/pages/blocks and DOCX traversal. No giant/malicious binary fixture is committed.
Malformed requests fail in the worker; oversized/operational refusals cannot create partial candidate
storage authority. Scanned/empty/unsupported-only content gives terminal insufficient text, preserves
the authorized original and creates no canonical/model authority; OCR remains outside contract.
Ordered DOCX paragraphs/tables and supported evidence remain intact. Headers/footers/textboxes
are explicitly disclosed as omitted; completeness never means unsupported content was silently read.
Parser/photo spawn-handle recovery waits to reap a late handle: a pathological OS spawn that never
returns is not a strict total wall-clock guarantee. Existing resource-unavailable policy fails closed.

Tenant, BrowserSession/membership, CSRF, scoped API keys, original-CV authorization, local-only
loopback AI, hidden SQL binds/closed diagnostics, no-store/security headers and offline docs remain
unchanged authority. D-100 effective facts, D-110 tracked-document ownership, D-111 document READY,
D-112 exact embedding compatibility remain intact. Search retrieves professional facts; deterministic
policy scores/ranks; evidence/provenance is immutable; identity/photo is presentation only. No LLM
writes a numeric hiring score or chooses a winner.

## Actual verification evidence

Before-fix results: direct boundary **3 failed**; runtime **11 failed, 1 passed**; photo cancellation
**1 failed**; CLI query embedding **2 failed** (PG locks 10/14 respectively); fresh schema check reported `remove_index ix_jobs_tenant_id_status`; actual SIGKILL
left an orphan original and missing referenced original with unmapped trash; DOCX containers varied
only in ZIP timestamps. Reproduction logs were kept outside Git under `/tmp/meyar-46-*`.

After-fix focused runs (real PostgreSQL, sequential DB suites, deterministic synthetic providers):

- Final adversarial closure + planner CLI suite: **70 passed in 7.79s**.
  Earlier final-candidate subset: **64 passed in 9.43s**.
- Storage/actual-kill/photo lifecycle regression suite: **61 passed in 18.67s**.
- Runtime/ingestion/parser/DOCX/agent boundary suite: **196 passed in 46.87s**.
- Direct recovery/tenant/folder downstream/configured embedding/M-9 suite: **131 passed in 42.33s**.
- Corrected parser authority/failure-hook suite: **41 passed in 11.26s**.
- Controlled synthetic backup archive: `BACKUP_JOURNAL_AND_STAGED_BYTES_PRESERVED regular_members=2`;
  existing `_archive_storage` preserved the exact .trash journal and staged bytes, no ops change.
- Earlier direct/extraction/runtime compatibility suite: **112 passed in 15.71s**.
- Earlier photo/parser suite: 106 passed, 1 test-target failure (old module after refactor);
  corrected timeout test passes in the final 61-test suite. Earlier storage helper counts were
  adapted to count payloads separately from journals; journal survival/removal is explicitly tested.

The first full run was intentionally interrupted after **651 passed in 296.58s**
when final caller inspection found the two CLI query gaps. It is not reported as a completed gate.
The first completed full run then reported **6 failed, 3933 passed in 642.69s**. All six
were old test hooks targeting renamed `parser_supervisor._run`; parser admission/timeout/failure
assertions are preserved and target the shared worker entry point. Final full gate follows
those corrections; no test exclusions/xfails are added.

Fresh **empty** synthetic PostgreSQL `meyar_46_acceptance`: full `upgrade head` succeeds;
`alembic heads`: `b88a2c4d6e10 (head)`; `alembic check`: **No new upgrade operations detected.**
The initial implementation needed no new migration. D-114 adds forward migration
`c46d7e8f9012`; the evidence in this section remains the initial historical result.
CI runs fresh upgrade + drift check + single-head assertion on the corrected head.

`ruff check .`: **All checks passed!**
`mypy src`: **Success: no issues found in 237 source files** (not whole-repository mypy).
`git diff --check`: exit 0, no output.
`scan-tracked-tree.sh`: **Tracked-tree secret/real-data scan: clean.**
Final full `uv run pytest -q`: **3939 passed in 586.91s (0:09:46)**.
This includes PDF/DOCX/body/ZIP/parser bounds, direct upload, folder/changed/dedup/delete/photo,
S7/S8/S9/S10, M-9, search/semantic/hybrid/planner/deterministic scoring/evidence, tenant/auth/CSRF/
original-CV, local-only AI/privacy, headers/readiness/demo/offline docs and backup/recovery suites.
Explicit security suite (sequential after full gate): **262 passed in 95.56s (0:01:35)**.
Files: `test_no_exfiltration.py`, `test_ops_no_exfiltration.py`, `test_logging_privacy.py`,
`test_logging_privacy_runtime.py`, `test_ops_diagnostic_privacy.py`, `test_audit_privacy_guard.py`,
`test_tenant_active_authority.py`, `test_search_tenant_authority.py`, `test_tenant_isolation.py`,
`test_api_key_auth.py`, `test_ui_auth_session.py`, `test_ui_multi_tenant_login.py`,
`test_ui_original_cv.py`, `test_http_response_policy.py`, `test_docs_environment_policy.py`,
`test_demo_user_ownership.py`, `test_demo_seed.py`. No external AI/content transport is added.

Reproducible normal gates from `backend/`: `uv run ruff check .`, `uv run mypy src`,
`uv run pytest -q`, `uv run alembic heads`, `uv run alembic check`; fresh acceptance used an
explicit empty synthetic DB for `alembic upgrade head` then heads/check. From repository root:
`git diff --check`, `git diff --cached --check`, `scripts/scan-tracked-tree.sh`. Output above
is actual; cache-location override was `/tmp/uv-cache`, not a test exclusion.
Exact final PR head/run/attempt/result are recorded in the PR acceptance handoff and final report;
a commit cannot contain its own SHA. Green CI does not substitute for independent acceptance.

## Historical ownership and closure handoff (completed through PR #128)

#46 remains OPEN. Accepted external #80/#84/#85/#86/#87/#88 are CLOSED, with their merged
PR history recorded above. #35/#36/#45/#50 remain OPEN; #49 was live-verified CLOSED under milestone 9.
All five capabilities are unchanged. Legal retention
ages, host logs/agentless deployment and target-Mac model/benchmark acceptance remain their existing
external owners; no #46 engineering item is deferred to an unnamed future slice.

The final matrix has no known remaining #46-owned engineering defect. Closure-ready means only
**after** independent acceptance, owner manual Squash and merge, post-merge full-tree/remote/issue
verification, then owner issue closure. Do not auto-close or merge from this report. All blocker
fixes and re-audit stay on this same PR. No force-push or merge is performed by this agent.

## Initial audited-head changed-file manifest (historical)

Paths are exact relative to the repository root discovered by `git rev-parse --show-toplevel`;
no machine-specific checkout path is repository authority. **37 files**, no dependencies/lockfiles,
no migration edits, no binary/model/runtime/real-candidate assets.

```text
README.md
.github/workflows/ci.yml
backend/src/meyar/api/v1/health.py
backend/src/meyar/cli.py
backend/src/meyar/config.py
backend/src/meyar/core/readiness.py
backend/src/meyar/extraction/identity_service.py
backend/src/meyar/extraction/service.py
backend/src/meyar/ingestion/parser_supervisor.py
backend/src/meyar/main.py
backend/src/meyar/models/job.py
backend/src/meyar/services/candidate_document_service.py
backend/src/meyar/services/candidate_embedding_service.py
backend/src/meyar/services/candidate_photo_service.py
backend/src/meyar/services/candidate_service.py
backend/src/meyar/services/demo_seed_service.py
backend/src/meyar/services/direct_inference_authority.py
backend/src/meyar/services/maintenance.py
backend/src/meyar/services/storage_authority.py
backend/src/meyar/storage/staging.py
backend/tests/test_agent_inference_boundary.py
backend/tests/test_candidate_delete_upload_concurrency.py
backend/tests/test_candidate_photo_worker.py
backend/tests/test_direct_inference_boundary.py
backend/tests/test_direct_inference_recovery.py
backend/tests/test_docx_tables.py
backend/tests/test_maintenance.py
backend/tests/test_parser_authority_correction.py
backend/tests/test_parser_failure_integration.py
backend/tests/test_photo_worker_lifecycle.py
backend/tests/test_runtime_closure.py
backend/tests/test_s4_storage_authority.py
docs/DECISIONS.md
docs/ISSUE_46_FINAL_CLOSURE.md
docs/ISSUE_46_S10_VALIDATION.md
docs/STATUS.md
docs/SECURITY_PRIVACY.md
```

## Independent acceptance correction: confirmed-session L-6 (D-114)

Initial audited PR #128 head `a4fba702d22b2f7557475b46ec4f8657558a4052`, CI
37391167162 attempt 1 SUCCESS / 3939 passed, was NOT independently accepted.
The non-null `AgentDraftConfirmation.browser_session_id` CASCADE FK prompted a
permanent maintenance exemption, leaving expired confirmed sessions unbounded.
Before any production correction, `test_confirmed_session_retention.py` failed
against real PostgreSQL: 90-day expired/revoked session + context + ResultSet all
remained after apply with `session_days=7` (counts `{}`). Normal expired sessions
without a confirmation were already covered by the passing maintenance regression.
The initial "no residual" L-6 disposition above was premature; this finding supersedes it.

D-057/MASTER_SPEC require independent durable confirmation identity, D-088/#84
immutable criteria semantic provenance, D-086 durable history separated from
session context, D-090 session-cascade ResultSets, and #87/#88 live-session auth,
CSRF and task authority. No contract permits destroying confirmation history just
to shrink session storage. D-114 keeps original session UUID as non-null historical
provenance and makes the live FK nullable ON DELETE SET NULL, constrained to match
historical identity whenever present. Exact live-session lookup/uniqueness and
atomic confirmation/Job/criteria/audit creation remain. Retired historical identity
is never replay authority. No legal age or separate confirmation lifetime is invented.

Forward Alembic `c46d7e8f9012` follows immediately previous accepted schema
`b88a2c4d6e10`, backfills existing historical identity without changing timestamps,
Job/criteria/provenance or uniqueness. No migration history rewrite. Downgrade
refuses detached confirmations before any DDL; attached state can round-trip.
Tests cover fresh upgrade/check and a populated previous-schema upgrade, session
deletion preserving identity, identity mismatch refusal and fail-closed downgrade.
Current-head assertions in prior migration tests advance; historical slice targets remain.

New correction validation and exact-new-head CI are recorded in the final PR handoff
and re-audit report, so this commit never fabricates its own SHA or CI result.

Correction-specific local verification: focused maintenance/session/confirmation/ResultSet/
conversation/auth/tenant/demo/migration/privacy suite **408 passed in 379.11s**. Final strengthened
new regressions separately **12 passed in 7.70s**, including submission/task/clarification
cascade and refusal of relogin replay. Ruff clean; mypy(src) **237 source files** clean.
Fresh empty synthetic PostgreSQL `meyar_128_acceptance_b6ce7ce20b7145bfa1d1f8b7e16bb699`:
`upgrade head` succeeds, `heads` = **c46d7e8f9012 (head)**, `check` = **No new upgrade
operations detected.** Populated `b88a2c4d6e10` upgrade and attached/detached downgrade behavior
are covered by the new migration regression. Full-suite/final CI evidence belongs to the
final handoff below and PR re-audit report; no CI acceptance is inferred from local tests.

Correction changed-file manifest (same branch/PR; exact repository-relative paths):

```text
backend/alembic/versions/c46d7e8f9012_confirmation_session_retention.py
backend/src/meyar/models/agent_draft_confirmation.py
backend/src/meyar/services/agent_draft_confirmation_repo.py
backend/src/meyar/services/maintenance.py
backend/tests/test_agent_conversation_authority_migration.py
backend/tests/test_agent_result_set_snapshot_migration.py
backend/tests/test_agent_semantic_provenance_migration.py
backend/tests/test_agent_turn_reservation_migration.py
backend/tests/test_candidate_photo_migration.py
backend/tests/test_confirmation_session_retention_migration.py
backend/tests/test_confirmed_session_retention.py
backend/tests/test_issue88_slice_a_migration.py
backend/tests/test_ui_migration_packaging.py
docs/DECISIONS.md
docs/ISSUE_46_FINAL_CLOSURE.md
docs/SECURITY_PRIVACY.md
docs/STATUS.md
```

Final correction full local gate: **3951 passed in 1012.85s (0:16:52)**, with no
exclusions or xfails. Focused suite: **408 passed in 379.11s**; strengthened new
regressions: **12 passed in 7.70s**. Ruff and mypy(src) clean (237 source files);
single Alembic head `c46d7e8f9012`, fresh upgrade and drift check clean. Both diff
checks and the complete tracked-tree scan pass. Initial audited-head green CI
remains historical; the corrected exact-head CI must pass separately before
return for independent re-audit. No merge or issue closure is authorized.

## Final accepted closure / current roadmap handoff — 2026-10-09

Independent acceptance, owner manual Squash merge, identical-tree verification and
#46 closure are complete at the exact accepted identities recorded above. The initial
L-6 blocker was corrected before final acceptance; earlier green CI is historical.
This document does not claim independent acceptance of the new normalization PR.
Next: normalization → #45 → independent acceptance/owner merge/post-merge verification
→ comprehensive audit/remediation/full re-audit → #35 → #36 → #20. #49 is delivered;
#50 deferred and #37 conditional. No next-phase implementation starts here.
