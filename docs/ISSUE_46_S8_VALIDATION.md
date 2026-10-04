# Issue #46 S8 — changed-file acquisition / candidate-delete authority

Status: **ACCEPTED + MERGED (PR #123, accepted head `7d87f9d160886f99fa4eb85e3b4b4481562c0f2d`, squash/main `a1841768fca2cd92d733852a71d42087e51341be`, tree `e988994d5befd7610dff1fa6e03e72c23f613a73`, parent S7 main `1a4e0ce95991fe43449d0d45d9da1b28b4d10ada`)**. Independently accepted and merged; recorded in S9. Refs #46 under
existing **M9 — Deployment, Benchmark & Integration Readiness** (10).
This slice did not complete #46; #46/#35/#36/#45/#50 remain OPEN.

## Verified baseline

Live GitHub verified PR #122 MERGED, accepted head
`c74cb32169cdb0b1a38aba20198e390601bd0014`, squash/remote main
`1a4e0ce95991fe43449d0d45d9da1b28b4d10ada` (sole parent S6
`3582189fbb6d4e462f346bc094ccd996a0d8423b`). Both complete trees:
`54570e9711fe4354aaedd08e559efad27486c3c5`. Exact-head CI
[37151559386](https://github.com/a-r3/meyar/actions/runs/37151559386), attempt 1,
SUCCESS. #46/#35/#36/#45/#50 OPEN; M9 OPEN. S7 is recorded ACCEPTED + MERGED
in STATUS, D-107 and its validation record. Hooks `.githooks` verified.
Main fast-forwarded before fresh branch `fix/46-s8-snapshot-delete-authority`.
Untracked `.aws` was not opened, staged or modified.

## Exact pre-fix proof

`backend/tests/test_folder_changed_authority.py`: real PostgreSQL READ COMMITTED
sessions and real synthetic local storage; asyncio Events and pg_blocking_pids,
no elapsed sleep used as proof. Initial ordinary snapshot cases passed (7), while
six delete/persist variants failed. Expanded acquisition exposed metadata limits.
Before editing the scanner, the pending indexer correction was preserved outside
the checkout and ALL production files restored to exact S7. A diff against S7
production source was empty. Both defects then reproduced: **13 targeted failures,
21 deselected, 10.12s**. The preserved indexer correction was restored afterward.

1. **Mixed acquisition:** a real mmap writer established its writable page before
   scan. Acquisition paused after its first 64-byte chunk; the writer changed PDF
   version 1.3 -> 1.4 and a trailing synthetic A comment -> B, same size. Pre/post
   size, mtime-ns, ctime-ns, device, inode were equal (asserted, never faked). S7
   parsed and persisted old 1.3 header + new B tail, matching neither full version.
   SHA-256 and byte_size DID match the mixed stored bytes: the defect was accepting
   a torn source acquisition, not separately hashing the wrong buffer. Folder and
   document hashes agreed, but both authorized mixed content.
2. **Delete owns Candidate UPDATE first:** scanner retained the old candidate,
   paused at real parser entry. Delete staged the old original in real .trash and
   paused before deleting the row. Scanner resumed, saved its own original and
   blocked at CandidateDocument FK. Delete commit/ambiguous commit -> FK
   IntegrityError, no summary. Independent DB/filesystem inspection after the
   failure confirmed S4 compensation: no candidate/doc/original, path links NULL.
   Delete rollback restored old bytes and allowed re-ingest, but the no-save-before-
   authority assertion still failed. These three variants were repeated twice.
3. **Storage save starts first:** pause after real save before returning its key.
   Delete completed without waiting and enumerated only one old document. Released
   scanner then failed at the FK even in the injected commit variants, before it
   could reach a commit. Three variants repeated twice.

One early test wrongly required every atomic path replacement to be skipped.
An unchanged opened old inode can supply a complete old snapshot; the assertion
now accepts that complete snapshot or an observed-mutation skip. It never accepts
mixed bytes. A broader run was stopped on a same-size acquisition failure to
investigate the mmap proof; interrupted runs are not reported as passing gates.

## Scanner sequence and invariant

Discovery lists paths only -> resolve/no-follow regular descriptor open -> pre
fstat -> oversized zero-read rejection -> first capped read -> post fstat ->
second capped read from offset zero -> final fstat -> equal bytes/length/metadata
acceptance -> SHA-256 of immutable DiscoveredFile.data -> service stability-window
check -> validate and parse the same data -> PreparedDocument.data and its hash
-> storage.save of that same data -> CandidateDocument.byte_size = len(data),
hash = prepared hash -> FolderIndexedFile size/hash from the same acquisition.

Every accepted attempt uses one immutable bytes value for validation, parsing,
storage and both persisted authorities. Inconsistent observations become
UnstableFile and preserve previous path/candidate/document state without FAILED
or MISSING; a later stable scan retries. Each pass is capped at max_bytes + 1,
at most two passes. Stable files cost one additional bounded read; oversized files
remain zero-read. There is no retry loop, new dependency or relaxed 60-second
production stability policy. Same-size rewrites with restored mtime and mmap
mutation during EITHER pass are covered.

Atomic replacement can leave a complete stable old descriptor snapshot, which
is internally valid and later reconciled by content hash. The protocol verifies
two matching observations and metadata; it does not lock out a producer or claim
an OS-level atomic filesystem snapshot against arbitrarily coordinated writers.

## Candidate/delete authority and outcomes

Only the existing-candidate changed/retry persistence path gains Candidate SHARE,
before any save. Existing pre-parse existence checks remain, but are not authority.
After a wait, refresh FolderIndexedFile under the already-held FolderSource lock,
check its exact retained candidate and live tenant. Candidate-less/new-path dedup
keeps S7's tenant/content locks and Candidate SHARE for duplicate links.

Order: Tenant live checks (non-locking, SHARE at guarded commit) -> FolderSource
-> content where new-path dedup applies -> Candidate SHARE -> exact refresh /
tenant revalidation -> original save -> CandidateDocument -> folder update -> commit.
Existing-candidate changes retain D-013 identity instead of content-dedup merging;
no extra content lock after Candidate. Delete stays Tenant SHARE -> Candidate UPDATE
before enumeration. Tenant suspension takes no source/content/candidate lock and
can commit promptly. No S5/S6 key/user lock order changes. Existing scan-wide
transaction lifetime and bounded S7 deadlock retries remain.

- Delete commit/ambiguous wins: no new save or resurrected candidate; old data is
  deleted; same path row FAILED/INDEX_AUTHORITY_INVALID, observed hash and size,
  candidate/document links NULL. Summary changed=1, successful=0, failed=1, new=0.
  Later M-5 retry reuses that path row with a fresh candidate ID (not the deleted ID).
- Delete rollback: old original restored, SHARE waiter sees surviving candidate,
  creates a new immutable document for it; two originals/documents, successful=1.
- Re-ingest commit/ambiguous wins: delete is observed waiting before enumeration;
  sees/stages both documents after commit, returns count=2; no rows/assets survive
  except the path's NULL links. Rollback -> compensation, delete stages count=1.
- Save then DB failure or tenant suspension: old candidate/document/index authority
  and original remain, new save is compensated; no successful summary is fabricated.
- Unrelated candidate deletion, another source's changed scan and another tenant's
  changed scan finish while the first candidate's save is held.

Independent observer sessions assert Candidate, CandidateDocument, folder ownership,
actual stored bytes/SHA/size and summaries. Whole isolated storage trees must contain
only durable normal originals, with no .trash or .tmp files for handled outcomes.
S4's ledger is not substituted for DB/filesystem assertions.

## Tests and gates

New S8 suite: 35 cases — mmap with unchanged metadata during either acquisition
pass (2); discovery/read/grow/truncate/atomic replace/stable/after-acquisition,
each with new and already-indexed paths (14); delete wins / persistence wins,
each with commit/refused commit/ambiguous commit, two bounded repetitions (12);
save then DB failure (1); tenant suspension at parser/after-save (2); unrelated
candidate/source/tenant work (3); unchanged 60-second stability window (1).

Focused gate: **349 passed in 139.95s**. Includes new S8, scanner bounds, folder
indexer/reconciliation/compensation/CLIs, S7 concurrency (including opposite-order
deadlock recovery), S4 storage authority, S5 delete/upload, S6 photo/delete, live
tenant/search authority, tenant isolation, diagnostic/audit privacy and application /
ops no-exfiltration. Ruff clean; mypy src clean (233 production source files);
Alembic remains the single `b88a2c4d6e10 (head)`. No whole-repository mypy claim.

Full gate: **3820 passed in 713.90s (0:11:53)**. `git diff --check` clean;
tracked-tree secret/real-data scan clean (rechecked with the new files staged
before commit). No hook bypass, force push, merge, auto-merge or issue closure.
Fresh exact-head CI and independent acceptance are separate delivery requirements;
the operational handoff identifies the PR head and CI run/attempt/conclusion.

## Remaining scope

Downstream profile/identity/embedding concurrency, post-ingestion photo concurrency,
folder transaction-lifetime/inference separation, general retention/orphan sweep,
S4 hard-kill/reported cleanup-failure residuals, configured embedding readiness,
schema drift and other unresolved #46 runtime/resource hardening remain open.
No deployment/readiness/Target-Mac or #45/#35/#36/#50 work. All five issues remain OPEN.
Independent audit must review the acquisition proof/finite-observation boundary,
S5/S6/S7 lock compatibility, M-5 retry semantics and S4 recovery before acceptance.
