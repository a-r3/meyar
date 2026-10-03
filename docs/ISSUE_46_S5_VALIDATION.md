# Issue #46 S5 — candidate hard-delete / direct-upload concurrency

Status: implemented; independent acceptance pending. Refs #46 under the existing
**M9 — Deployment, Benchmark & Integration Readiness** milestone (10). This is a
bounded original-CV concurrency slice, not completion of #46.

## Verified accepted starting state

Before any production edits, live GitHub REST/CLI and remote Git verified:

- PR #119 MERGED, accepted head `6325cbe507d01624d37683be6b96d10a769c71b4`.
- Squash/remote main `dc4b404fc3eec2ee0919b84016ea3466383c0c6f`.
- Accepted head and merged main full tree both
  `4692171ec09dda67f7e848e8c004f74c83afedf5`.
- CI run [37133867521](https://github.com/a-r3/meyar/actions/runs/37133867521),
  attempt 1, completed SUCCESS at the accepted exact head.
- #46/#35/#36/#45/#50 OPEN; M9 (10) OPEN. PR #119 has no auto-merge.
- Hooks path `.githooks`; no tracked local changes. The existing untracked `.aws`
  entry was not inspected, modified or staged.
- Switched from the merged S4 task branch to main, then ran
  `git pull --ff-only origin main`; local HEAD/tree matched the above.

S4 is independently **ACCEPTED + MERGED**, based on the owner's request and the
verified merge/exact-head CI/content equality. Prior rejected S4 heads remain
historical evidence; D-104/STATUS/S4 validation now record the accepted baseline.

## Before-fix deterministic proof

Only `backend/tests/test_candidate_delete_upload_concurrency.py` was added before
production editing. Its initial harness accessor was corrected before the valid
proof below; earlier harness timeouts are not race evidence. Production HEAD was
still the accepted S4 main SHA/tree, with no production diff.

```bash
cd backend
uv run pytest -q tests/test_candidate_delete_upload_concurrency.py
```

The regression uses real PostgreSQL transactions with distinct server connection
PIDs, local synthetic `valid_cv.pdf` bytes and real `LocalFilesystemStorage`.
It pauses the actual delete service immediately before `delete_candidate_row`,
after the service enumerated its old document and staged its original. It proves
the original key is absent and exactly one file is in trash before starting upload.
Upload calls the actual `_revalidate_upload_authority`, `persist_candidate_document`
and outer commit from the direct API persistence phase, on a separate connection.
An independent third connection proves the new document is durable and its new
original is present. Delete is then released to execute the real FK cascade and
S4 purge. No mocked DB locks/cascade/storage mutation or timing sleeps.

The orchestrator observes `pg_blocking_pids` until upload is either blocked by
delete or actually completed. The accepted baseline completed the upload; the
corrected implementation blocks it. Events force the critical interleaving;
10-second deadlines only bound hangs, not establish correctness.

Captured valid pre-fix output (generated synthetic paths/IDs omitted):

```text
E AssertionError: durable candidates=0 documents=0 filesystem originals=1; upload committed after the delete staged its old asset set
E assert 1 == 0
FAILED tests/test_candidate_delete_upload_concurrency.py::test_delete_first_does_not_leave_concurrent_original
1 failed in 2.17s
```

This proves the original-CV orphan, rather than merely a static possibility. S4's
upload ledger legitimately relinquished its created-original compensation after
the successful upload commit. Delete only owned the old staged set; no S4 outcome
ambiguity or cleanup failure was required to produce the orphan.

## Transaction and lock order

| Boundary | Order and lifetime |
|---|---|
| Initial API auth | Credential lookup; Tenant SHARE; key last-used UPDATE; commit/releases. New request Tenant SHARE; existing scope check. |
| Upload ownership/preparation | Candidate lookup; commit/releases; bounded validation/parsing without DB transaction. |
| Upload persistence (unchanged) | Tenant SHARE -> ApiKey SHARE + live tenant/credential/scope validation -> tenant-scoped Candidate SHARE -> original save -> document/canonical/audit SAVEPOINT writes -> outer commit. |
| Old candidate delete | Tenant SHARE from API; unlocked Candidate lookup -> document/photo enumeration -> staged deletes -> Candidate DELETE (UPDATE-strength row lock too late) -> cascade/audit -> commit/purge. |
| Corrected candidate delete | Tenant SHARE, including direct service entry -> tenant-scoped Candidate UPDATE **before enumeration** -> document/photo enumeration -> staged deletes -> cascade/audit -> commit/purge. |
| Supported tenant suspension | Affected Users in UUID order -> Tenant NO KEY UPDATE -> Memberships in UUID order -> BrowserSession revocation; outer commit. |
| Key revocation | Key UPDATE; no subsequent candidate authority acquisition. |
| S1 final tenant guard | Rechecks registered Tenant SHARE before outer commit, on the same transaction; delete already holds it before Candidate. |
| S4 recovery | Commit/rollback event marks outcome; rollback releases writer locks before fresh DB-truth reference checks/compensation. Unknown outcomes invalidate uncertain connections first. |

CandidateDocument.candidate_id references Candidate.id with `ON DELETE CASCADE`;
CanonicalDocument/document-dependent cascades remain unchanged. Inserts acquire
FK KEY SHARE; Candidate FOR UPDATE excludes this and upload's stronger FOR SHARE.
The existing PostgreSQL default READ COMMITTED isolation is verified in the new
fixture. Each post-wait enumeration SELECT sees newly committed documents.

Delete does not introduce any ApiKey/User/Membership/BrowserSession lock after
Candidate. Upload's key lock precedes Candidate; Tenant suspension waits on existing
shared authority before changing state; neither operation takes a new reverse edge.
Delete's initial key/scope semantics remain unchanged: this slice adds no new
live-key revocation guard to that route. The auth touch-last-used phase commits
before candidate authority is acquired. No global/advisory lock or exclusive
Tenant lock is added. Same-tenant distinct candidates and cross-tenant candidates
can continue independently.

Smallest safe boundary: lock the candidate before the first document/photo SELECT,
retain through DB outcome, and acquire Tenant SHARE first. A lock taken only at the
later DELETE still permits the reproduced orphan. A candidate lock taken before
Tenant would risk reversing S1's final Tenant guard against tenant mutations.

## Implementation

- `services/candidate_repo.py`: optional `get_candidate(..., lock=True)` uses a
  tenant-filtered `FOR UPDATE` query with fresh ORM population. Ordinary reads
  retain their existing unlocked behavior.
- `services/candidate_service.py`: checks/locks live Tenant authority, then obtains
  that exclusive candidate lock before any enumeration/storage mutation.
- Existing upload shared lock, API refusals, auth/scope checks, savepoints, FK
  cascade, S4 staging/outcome recovery, no-overwrite restoration, original-CV
  authorization, scoring/evidence and privacy boundaries remain unchanged.
- No migration, dependency or lockfile changes. All assets are synthetic and local;
  candidate content is never sent to an external service.

If DELETE wins, upload waits at Candidate SHARE and then receives the existing
404 before saving anything. If upload wins, delete waits before enumeration and
then stages both originals. If upload rolls back, delete stages only the old
original. If delete rolls back, S4 restores the old bytes to their exact key and
upload can persist a distinct original. Ambiguous commit errors in either direction
still resolve against durable authority, not the exception alone.

## New regressions

The 19 new cases prove:

- Delete-first staging barrier: waiting upload is refused without saving; final
  candidate/document rows, originals and trash are absent. Same proof failed above.
- Upload-first (3 bounded repetitions each of commit, rollback and ambiguous
  committed-then-error outcome): delete is observed waiting in PostgreSQL before
  enumeration. It stages exactly the durable set, then leaves no rows/assets.
- Delete commit failure before commit and after durable commit: waiting upload
  either succeeds with exact old-byte restoration, or is refused; final DB/storage
  authority agrees. No trash remains on handled successful compensation.
- Distinct same-tenant and cross-tenant uploads commit while delete is deliberately
  paused. A foreign-tenant delete of the locked ID returns absent without waiting.
- Upload-first + waiting delete + concurrent tenant suspension/key revocation:
  wait graph confirms both expected waits; all finish within the bound, no deadlock.
- Tenant/key deactivation-first: upload waits then freshly refuses before save.
- Tenant suspension-first: direct delete service waits then refuses before staging.

Existing focused regressions additionally cover candidate upload validation,
API-key scope/expiry/revocation, original-CV reads and tenant isolation, S4 rollback
failure/invalidation, exact no-overwrite restore, privacy and no-exfiltration.

## Local validation

The initial isolated new regression suite: **19 passed in 11.24s**. Focused
compatibility verification: **273 passed in 220.94s (0:03:40)**, with:

```bash
cd backend
uv run pytest -q \
  tests/test_candidate_delete_upload_concurrency.py tests/test_candidate_documents.py \
  tests/test_s4_storage_authority.py tests/test_candidate_photo_service.py \
  tests/test_candidate_photo_noninterference.py tests/test_tenant_active_authority.py \
  tests/test_api_key_auth.py tests/test_search_tenant_authority.py \
  tests/test_ui_candidate_preview.py tests/test_logging_privacy.py \
  tests/test_logging_privacy_runtime.py tests/test_audit_privacy_guard.py \
  tests/test_ops_diagnostic_privacy.py tests/test_no_exfiltration.py \
  tests/test_ops_no_exfiltration.py
```

The source/test implementation, including explicit isolation-level verification,
was finalized before the normal full gate. The normal full suite ran sequentially
after focused compatibility, with no exclusions or diagnostic environment override:

```text
$ uv run ruff check .
All checks passed!
$ uv run mypy src
Success: no issues found in 232 source files
$ uv run pytest -q
3747 passed in 917.28s (0:15:17)
$ uv run alembic heads
b88a2c4d6e10 (head)
```

`git diff --check` and `scripts/scan-tracked-tree.sh` are clean; both are checked
again against the intentionally staged delivery, including the new regression and
validation document. No source/test changes followed the passing full gate.
Live remote main was rechecked unchanged and #46/#35/#36/#45/#50 still OPEN. The
new task branch is created only after these local gates are clean, then committed,
pushed and opened for independent acceptance. Exact-head CI is checked separately.

## Deferred scope / residuals

This proves direct original-CV persistence versus candidate hard-delete only.
The post-commit photo pass and final upload response can race a later deletion;
no photo-worker lease or response-liveness correction is included. Folder overlap,
content leases/dedup, changed-file reads, folder transaction/inference separation,
configured embedding readiness, retention/orphan sweeping (including trash), schema
drift and broader ingestion/resource/runtime hardening remain deferred under #46.
S4's hard process-kill and explicitly reported compensation/cleanup/purge-failure
residuals remain; this slice adds no durable journal or sweeper. No deployment,
Target-Mac benchmark, or #35/#36/#45/#50 work. #46 remains OPEN.

## Delivery and independent acceptance

Create the task branch and bounded PR only after local implementation/gates are
clean. Associate the PR with existing M9 milestone 10; use Refs #46, never closing
syntax. Exact PR head and fresh first-attempt CI run/conclusion are recorded in the
PR delivery report and final report, avoiding a self-referential source commit.
No merge or auto-merge. Green gates/CI are not independent acceptance.

## HUMAN ACTION REQUIRED

Independently audit the new exact PR head after fresh CI succeeds. Review the
pre-fix proof, candidate/Tenant lock order, both concurrency directions and S4
compatibility. Do not merge, enable auto-merge, close #46 or begin deployment/
Target-Mac work during acceptance. Reply `S5 PASS` or provide audit findings.
