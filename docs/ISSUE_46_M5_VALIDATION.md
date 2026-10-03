# Issue #46 M-5 — ingestion compensation and truthful library state

Status: implementation proposal awaiting independent acceptance. Refs #46;
GitHub milestone **M9 — Deployment, Benchmark & Integration Readiness**.
M-5 is the audit finding, not milestone M5. Issue #46 stays OPEN.
M-9 failed re-extraction is not started; #35/#36/#50 are not advanced.
No merge or Target-Mac acceptance is claimed.

## Verified base and root cause

Live GitHub main was verified as `f1429c7309b1537fd8e94547a2f666995a80859a`.
PR #110 (PR-3) and its documentation follow-up #111 were both MERGED.
Issue #46 was OPEN under M9. Repository hooks were already `.githooks`.
Task branch: `fix/46-m5-ingestion-compensation`.

`_handle_new_file` created a Candidate before validation/parser preparation.
Validation and operational ParseError were caught and committed as FAILED
index rows that still referenced that empty candidate. Retry asserted a
non-null candidate link. The library/detail badge read only profile status:
a retained terminal parser failure has no profile attempt, so it defaulted
to “Emal olunur”. These are different defects with different outcomes.

The accepted PR-2/PR-3 pipeline already separates preparation from persistence:
validation and operational parser failures propagate without a durable
original; terminal failures intentionally retain an original CandidateDocument
with PARSE_FAILED and no CanonicalDocument. That boundary is unchanged.

## Chosen behavioral contract (D-099)

- New rejected read-sized files and operational parsing failures retain one
  FAILED FolderIndexedFile, with `candidate_id=NULL` and
  `candidate_document_id=NULL`. Its tenant/source/relative-path identity,
  observed content hash, byte size, type and closed safe failure metadata
  remain available for retry. No raw exception message is persisted.
- Every preparation uses accepted validation/offload/parser boundaries.
  Dedup hits still validate the new path's extension/content/size, but skip
  redundant parsing and original storage.
- Successful candidate-less retry attaches one newly created Candidate and
  its persisted CandidateDocument to the existing index row, or attaches the
  existing same-tenant exact-content document without creating a candidate.
  Failure metadata is cleared; repeated successful scans are no-ops.
- A failed path tombstoned MISSING is still retried when it reappears with
  no durable document; unchanged bytes alone do not make it INDEXED.
- An existing candidate's changed/retry rejection preserves that Candidate,
  prior document pointer and immutable canonical evidence. Successful changed
  ingestion preserves candidate identity even if another path has those bytes.
- Terminal parser failures remain INDEXED with their original CandidateDocument
  and Candidate. Original authorization/audit behavior is unchanged. They
  create no canonical/profile/identity authority. No terminal-failure backfill,
  parser version change or forced unchanged-file reprocessing occurs.
- Library and detail use the latest durable document's parser status as a
  presentation fallback **only when no current profile attempt exists**.
  PARSE_FAILED then displays “Diqqət tələb edir”. Latest creation timestamps
  supply this presentation fallback, not a new factual authority. When latest
  timestamps tie, any terminal parser failure requires attention; a UUID
  tie-break must not hide it. A strictly later parsed document can return to “Emal olunur”; an old failed document
  does not keep it permanently in attention. Existing profile-status precedence,
  current-profile selection, search, ranking and M-9 semantics are unchanged.
  Normal HR copy never renders raw parser statuses/reasons or UUIDs; existing
  authorized hrefs/CSS data attributes still carry internal identifiers.
- Exact-content dedup requires the index row, Candidate and CandidateDocument
  to agree on tenant/candidate ownership, and the retained document hash to
  equal the observed hash. A changed-file FAILED row can describe new bytes
  while retaining an older original; that older original is not a dedup owner
  for the failed new bytes. Retry refuses an invalid foreign candidate link
  with closed INDEX_AUTHORITY_INVALID metadata and performs no foreign writes.

Oversized inputs remain PR-1's pre-read skip/audit/summary behavior: no content
hash/index row or Candidate is created. Unsupported scanner extensions remain
ignored. Neither behavior is broadened by M-5.

## Existing phantom compensation policy: fail closed

No automatic candidate deletion, relationship clearing, blanket zero-document
cleanup or library hiding is introduced. Persisted provenance cannot prove that
a legacy candidate was minted exclusively by failed folder ingestion:
`Candidate` has no origin marker, `create_candidate` emits no creation-origin
audit, and the old FOLDER_FILE_IMPORT_FAILED event names the source and error,
not an exclusive candidate/path creation relationship. The API records
CANDIDATE_CREATED when explicitly creating a manual candidate; its presence
can disqualify deletion, but its absence is not positive folder-origin proof.
A folder link proves association, not exclusive ownership. Manually created empty candidates and
legacy shared references can have the same persisted shape.

Zero documents and a single FAILED folder relationship are therefore necessary
but insufficient deletion evidence. Even a seemingly solitary legacy empty
candidate is retained. Its later authorized successful retry reuses the same
tenant-owned candidate; a shared/missing path never triggers deletion. Foreign
candidate links fail closed. This is the strongest safe forward fix without
inventing historical ownership. Existing legacy empty candidates may remain
visible; they are not silently hidden or claimed to have been cleaned up.

Any future owner-approved cleanup must prove same-tenant ownership, no documents,
exclusive failed folder creation provenance, and no other durable references
(including profile/identity/embedding/photo/evaluation authority and agent
snapshot/history references without Candidate FKs). Ambiguity must retain the
candidate. Generic lifecycle/orphan retention remains separately open in #46.

## Transaction, storage and concurrency review

`index_folder` still never commits. `reconcile_folder` commits scan ingestion
before independently committing downstream processing; caller-owned rollback
remains required on unexpected persistence exceptions.

1. Preparation succeeds, Candidate creation fails: there has been no original
   storage write for this file. The error propagates; rollback removes transactional scan
   writes. No FAILED row with a newly minted empty candidate is committed.
2. Candidate creation succeeds but persistence fails: the error propagates,
   rather than being mislabeled a handled validation failure. Caller rollback
   removes the Candidate/document/index/audit DB writes in that transaction.
3. `storage.save()` succeeds and a later DB operation or caller commit fails:
   existing opaque-file save-before-DB ordering has **no rollback/crash recovery
   journal or guaranteed compensation**. The original bytes may remain without
   a DB reference. The synthetic fault-injection test demonstrates this known
   limitation rather than asserting it solved. Save/rename failure may also
   leave a temporary file. No new storage ordering, delete or compensation
   mechanism is introduced in this slice; general filesystem/DB recovery stays
   a separate unresolved #46 item.
4. Handled validation/operational parser failures never call storage.save or
   create CandidateDocument and continue the scan. Unexpected DB/storage
   failures can still abort the transaction; no blanket exception catch hides
   persistence failure or commits an unsafe partial transaction.
5. Repeated supported sequential scans reuse the unique path row and exact
   document-content dedup. D-021's existing **single-active-reconciler** model
   remains operational, not enforced with a new lock. Overlapping same-path
   scans can conflict on the unique constraint and require loser rollback;
   cross-source simultaneous same-content scans can miss uncommitted dedup and
   create duplicate candidates. This pre-existing unsupported concurrency,
   plus storage left after a losing/aborted transaction, remains explicitly
   unresolved in #46. No distributed transaction or locking redesign is claimed.
6. Folder preparation still runs inside the caller's scan transaction and can
   hold a DB connection while parsing. Direct-upload PR-2 transaction release
   and fresh authorization locks are unchanged. This is not a folder connection
   lifetime redesign or an extension of upload semantics.

## Before-production-change reproduction

Only synthetic tests were added first. From `backend/`:

```bash
uv run pytest -q tests/test_folder_ingestion_compensation.py
```

Actual output against accepted main production code: **6 failed in 2.09s**.
Three rejection cases (fake DOCX, non-PDF bytes named PDF, DOCX renamed PDF)
created candidates; operational parser failure created a candidate; a seeded
candidate-less FAILED row hit the retry assertion; terminal malformed PDF
retained an authorized original but its library lacked “Diqqət tələb edir”.
The last case passed original authorization before failing presentation.

Three pre-existing regressions in two test files were updated because they
explicitly asserted the old reserved-empty-candidate behavior. Ordinary direct-upload tests are unchanged.
New tests cover candidate-less retry and dedup, extension validation on dedup,
failed-change document/hash mismatch, MISSING reappearance, ambiguous/shared
legacy retention, manually empty candidates, foreign candidate and document
references, failure isolation/metadata privacy, latest-document presentation,
and candidate-creation/DB-persistence fault injection.

## Verification

The final focused command, from `backend/`, is:

```bash
uv run pytest -q \
  tests/test_folder_ingestion_compensation.py \
  tests/test_folder_indexer.py \
  tests/test_folder_reconciliation.py \
  tests/test_parser_authority_correction.py \
  tests/test_parser_failure_integration.py \
  tests/test_candidate_documents.py \
  tests/test_tenant_isolation.py \
  tests/test_ui_original_cv.py \
  tests/test_ui_routes.py \
  tests/test_ui_candidate_preview.py \
  tests/test_candidate_factual_authority_backstop.py
```

Actual output: **185 passed in 57.69s**. This includes all 17 new M-5 cases.
Ruff: `All checks passed!`; `mypy src`: `Success: no issues found in 226 source files`.
Full required gate, from `backend/`:

```bash
uv run ruff check .
uv run mypy src
uv run pytest -q
uv run alembic heads
```

Actual full-suite output: **3475 passed in 499.99s (0:08:19)**.
Alembic: **b88a2c4d6e10 (head)**, unchanged single head.
From repository root, `git diff --check` and staged diff checks exit 0.
After intentional staging, `scripts/scan-tracked-tree.sh` reports:
**Tracked-tree secret/real-data scan: clean.** The hook remains `.githooks`.
CI delivery evidence will be reported at the exact pushed head; this document
does not self-certify independent acceptance.

No migration: candidate links are already nullable; no schema, dependency,
`uv.lock`, parser-version, model-artifact or fixture-binary changes are needed.

## Explicit deferrals

Historical empty-candidate cleanup without provable ownership; generic orphan/
retention policy; filesystem/DB compensation and crash recovery; overlapping
reconciler locking/concurrent dedup; folder transaction lifetime; M-9 current
profile/re-extraction policy; other unresolved #46 items; #35/#36/#50 and all
real Target-Mac validation. #46 must remain OPEN. Independent acceptance review
is the next step, before any owner-controlled irreversible merge.


## Changed files

Repository-relative paths (the canonical root is discovered with
`git rev-parse --show-toplevel`):

- `backend/src/meyar/models/folder_indexed_file.py` — document nullable failure contract.
- `backend/src/meyar/services/folder_indexed_file_repo.py` — candidate attachment and safe dedup query.
- `backend/src/meyar/services/folder_indexer_service.py` — preparation before creation, nullable retry, closed metadata.
- `backend/src/meyar/ui/presentation.py` — parser-aware HR fallback.
- `backend/src/meyar/ui/service.py` — derive latest durable parser state with conservative timestamp ties.
- `backend/src/meyar/ui/view_models.py` — presentation-only latest parser status.
- `backend/src/meyar/ui/templates/library.html` — use derived fallback.
- `backend/src/meyar/ui/templates/candidate_detail.html` — same detail fallback.
- `backend/tests/test_folder_ingestion_compensation.py` — 17 synthetic adversarial cases.
- `backend/tests/test_folder_indexer.py` — replace reserved phantom assertions with path identity.
- `backend/tests/test_parser_authority_correction.py` — operational candidate-less retry assertion.
- `docs/DECISIONS.md` — D-099 proposal and D-013 cross-reference.
- `docs/STATUS.md` — unaccepted M-5 proposal with explicit issue deferrals.
- `docs/SECURITY_PRIVACY.md` — failure metadata/ownership/cleanup boundary.
- `docs/ISSUE_46_M5_VALIDATION.md` — this contract, reproduction, measured gates and limits.
