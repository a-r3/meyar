# Issue #46 S6 — post-upload photo persistence / candidate hard-delete

Status: implemented; independent acceptance pending. Refs #46 under the existing
**M9 — Deployment, Benchmark & Integration Readiness** milestone (10). Bounded
concurrency slice, not completion of #46.

## Verified accepted starting state (live, before any production edit)

- PR #120 MERGED; accepted head `1c18bae1c75c64d419bd361e8dd08995764df47a`.
- Squash/main `dd4b4d0ce8b48b9948f948182094a5b1a50482f1`; accepted and merged
  tree both `721438a6cbccedd54443bc5ba57cdb8718b03bf7`.
- Exact-head S5 CI run 37137376151, attempt 1, completed SUCCESS.
- #46/#35/#36/#45/#50 OPEN. Hooks path `.githooks`. Untracked `.aws` untouched.
- S5 is **ACCEPTED + MERGED**; main synced with `git pull --ff-only`.

## Production sequence at the accepted S5 baseline

Upload: original persisted + committed (S5 locks released) -> `process_photo_for_document`
(own session transaction: reads, isolated extraction, `require_active_tenant`
without lock, `photo_storage.save`, `create_photo_version` flush, commit) ->
`require_active_tenant` -> `get_candidate_document` -> `assert durable_document`.
Delete: Tenant SHARE -> Candidate UPDATE -> enumerate AVAILABLE photo rows ->
stage photos/originals -> `DELETE candidate` (FK cascade) -> commit/purge.
The photo path took **no** Candidate lock and its derived JPEG was written
before its row was visible to delete's enumeration.

## Before-fix deterministic proof (exact S5 source, no production diff)

`backend/tests/test_photo_delete_concurrency.py` (real PostgreSQL connections,
real local synthetic storage, Events, `pg_blocking_pids`; worker subprocess
replaced by a synthetic AVAILABLE outcome). Production `src/` was stashed to
reproduce on the accepted S5 tree. 12 tests: 5 failed, 7 passed.

- **Case A (delete wins during extraction)** — PASSED at S5: the photo INSERT hits
  the document FK, the exception path removes the derived JPEG; no orphan, no row.
  Only cost: two closed warnings (processing failed / terminal row not persisted).
- **Case B (photo row flushed, uncommitted, before delete cascade)** — FAILED at S5:
  delete's enumeration cannot see the uncommitted row, then (`delete_waited=True`)
  blocks on the document row until the photo commits, cascades the row away and
  commits. Final state: zero candidate/document/photo rows, **one orphan derived
  JPEG**. This is the real correctness gap.
- **Case C (photo durable before delete)** — PASSED: delete stages and purges it.
- **Response liveness** — FAILED at S5: delete committed between document commit and
  the response made `assert durable_document is not None` raise; the request ended
  in an unexpected-error 500 (`AssertionError`).
- **Ambiguous photo commit** — FAILED at S5: the old except path deleted the derived
  JPEG even though the COMMIT had become durable (row referencing a missing file).
- **Derived cleanup failure** — FAILED at S5: cleanup failure was only a generic
  warning, not the documented observable unresolved-compensation state.
- Delete-holds-authority-first also FAILED at S5 (photo not serialized behind delete).

## Correction

Photo processing is now three phases (corrected after independent rejection of
head `f83dbb1d0cb105ada889b08657b169aea753c38b`, see below):

- **Phase A (short read phase):** live tenant check, exact document lookup, exact
  photo-version check, copy of primitive fields, then `db.commit()` of the
  read-only transaction (`expire_on_commit=False`, so caller ORM state stays valid).
- **Extraction:** original read, isolated worker and output decode/hash/bounds
  validation hold **no SQL transaction, no pooled PostgreSQL connection and no
  Tenant/Candidate row lock**.
- **Phase B (persistence, below)** freshly revalidates everything; no authority
  decision crosses extraction.

`Tenant SHARE (live) -> Candidate SHARE (tenant-scoped) -> exact document
revalidation -> existing-row recheck -> derived save (tracked in the S4 ledger,
namespace "photo") -> CandidatePhotoVersion insert -> commit`, all inside
`recover_on_failure`. Lock order is the S1/S5 order (Tenant -> Candidate); no
Candidate -> Tenant or Candidate -> ApiKey edge is introduced. Delete (Candidate
UPDATE) now either commits first (photo returns `None`, writes nothing) or waits
for the photo commit and then enumerates the durable derived asset. Unrelated
candidates are not serialized (SHARE on a different row).

S4 compensation is reused: `track_created` gained a `namespace` so a failed or
ambiguous commit is resolved against fresh DB truth (referenced -> kept;
unreferenced -> removed); `LocalPhotoStorage.delete_owned` was added for the shared
protocol; compensation failure surfaces as the existing closed
`component=storage_recovery` log and the photo pass returns `None` (presentation
only, never blocks professional readiness). Tenant suspension during the phase
raises `TenantInactiveError` before any write.

API: if the candidate/document is gone after a durable upload because a delete
committed, the endpoint returns 404 "Candidate not found." (no generic catch; the
assert is replaced by an explicit state contract).

## Regression tests (13 in the new file: the original 12 plus the pool regression)

case A, case B, case C, delete-first photo waits and writes nothing, idempotent
reuse per document/extractor, photo commit failure, ambiguous commit keeps
referenced asset, save-then-insert failure, derived cleanup failure observable,
tenant suspended in persistence phase, unrelated candidate not serialized, API
response 404 when delete wins.

## Independent rejection and correction (historical)

Head `f83dbb1d…` was independently rejected for one blocker: Phase A's SELECTs
autobegan a transaction that stayed open (a pooled connection checked out) for the
entire worker subprocess; "no row lock" was not sufficient under the bounded-pool
architecture. Correction: Phase A commits before extraction. New regression
`test_extraction_holds_no_pooled_connection_transaction_or_lock` uses a real
`pool_size=1, max_overflow=0` PostgreSQL pool with checkout/checkin counters and an
Event-gated extraction; while paused it proves the worker session is not in a
transaction, zero connections are checked out, an unrelated session acquires the
pool, and `FOR UPDATE NOWAIT` on the Candidate row succeeds. Before-fix output
(rejected-head source):

```text
E assert not True
E  +  where True = in_transaction()
FAILED tests/test_photo_delete_concurrency.py::test_extraction_holds_no_pooled_connection_transaction_or_lock
1 failed, 12 passed
```

Test harness corrections: wait-graph probing now finds the backend blocked by a
known holder (the worker's connection is legitimately different per phase and
`pg_stat_activity` is cached per observer transaction); commit-fault tests target
the persistence-phase commit, not Phase A's.

## Gates

See the delivery report / PR for the exact command output and exact-head CI.

## Deferred (unchanged, not addressed)

Folder reconciliation overlap/content leases, changed-file read races, same-content
folder dedup, parser/DOCX completeness, readiness, retention/orphan sweeper
(including `.trash` and S4 hard-kill residuals), schema drift, M9 deployment,
Target-Mac, #35/#36/#45/#50. Folder/CLI callers of `process_photo_for_document` use
the same corrected function. #46 remains OPEN.

## HUMAN ACTION REQUIRED

Independently audit the exact PR head after fresh CI. Do not merge, enable
auto-merge, close #46 or start deployment/Target-Mac work. Reply `S6 PASS` or
provide findings.
