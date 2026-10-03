# Issue #46 S4 — Original-CV storage <-> PostgreSQL recovery

Implementation proposed for independent acceptance. Refs #46, existing M9
milestone 10. #46 is not complete; #46/#35/#36/#45/#50 remain OPEN. D-104 records
the architecture. PR #119 head `95b39467f16235129b06135634ea119d59dea362`
is **acceptance-REJECTED**. The correction amends the existing
`fix/46-s4-storage-db-recovery` branch and PR only; new exact-head re-acceptance
is pending. Subsequent head `977aaf34b4e67be3602d95df1cbc1eea0063fe49` is also
**acceptance-REJECTED** for the handled rollback-failure gap documented below.
No merge or issue closure is authorized.

## Starting state and S3 post-merge evidence

Live GitHub `main`, `origin/main` and the branch base matched
`df5264d869ba3810b857b77b7d9491b77b6f39c1` (local `main` was fast-forwarded from the
S2 merge `a470305` before branching; no other divergence). PR #118 is MERGED.
S3 accepted head `d97ac9feec9a47c25dcd19617246e475d46995b1`, exact-head CI
[37122628841](https://github.com/a-r3/meyar/actions/runs/37122628841) SUCCESS,
squash/main `df5264d869ba3810b857b77b7d9491b77b6f39c1`, accepted/merged tree
`9970ce5ad3b2835335df1d2ef1fd205869445945`. Hooks: `.githooks`. Task branch:
`fix/46-s4-storage-db-recovery`. The pre-existing untracked `.aws` entry was not
inspected, staged or modified.

## Exact pre-fix synthetic reproduction

Before any commit, the new regressions that use only pre-S4 APIs ran against starting
main's source (`git stash` of `backend/src`):

- document-row / canonical / audit creation failure after `storage.save`:
  `assert [<original path>] == []` — original left on disk; the CandidateDocument row
  from the failed attempt was still present (`assert 1 == 0`).
- storage save failure and `os.replace` failure: `assert [<...>.tmp] == []` — an
  abandoned temporary file remained.
- API upload with a failing post-persist commit: HTTP 500 but the original remained
  on disk (`assert [<path>] == []`).
- candidate delete with a later row-delete / audit / commit failure (3 variants):
  `FileNotFoundError` reading the original after rollback — the DB candidate and
  CandidateDocument survived while the authorized original was permanently gone.
- candidate delete with one already-missing and one present original: the present
  original was gone after the failed delete.

All data synthetic (`fixtures/synthetic_cvs`); no real CVs, paths or operator data.

## Acceptance blocker: rejected-head partial-stage reproduction

Before modifying production source, added
`test_partial_stage_source_unlink_failure_cleans_new_trash[document/photo]`
on exact rejected HEAD `95b39467f16235129b06135634ea119d59dea362`.
Production diff was empty. Each case saved a real synthetic owned object through
its namespace, allowed the real `os.link` to succeed, then injected failure only
when `os.unlink` received the original path. Before raising, the injection
asserted one trash object and `os.path.samefile(original, trash)`. The exception
was the injected source-unlink failure, and identical original bytes survived.
The final assertion failed because the untracked trash hardlink also survived.

```bash
cd backend
uv run pytest -q tests/test_s4_storage_authority.py -k partial_stage_source_unlink_failure
```

Captured failing assertion/output (synthetic temporary paths omitted):

```text
> assert leftovers == [], "source remains present but an unwanted trash hardlink remains"
E AssertionError: source remains present but an unwanted trash hardlink remains
E assert [PosixPath('<synthetic .trash path>')] == []
FAILED ...::test_partial_stage_source_unlink_failure_cleans_new_trash[document]
FAILED ...::test_partial_stage_source_unlink_failure_cleans_new_trash[photo]
2 failed, 28 deselected in 1.51s
```

## Partial-stage correction and new regressions

`storage/staging.py::stage_file` now returns `None` before trash creation for an
absent source. Link failure preserves the original and propagates; a missing
trash parent while the source exists is structural, not an absent-object result.
After a successful link, any source-unlink failure triggers removal of that
specific new link before the original failure propagates. The sequence is
reversible same-filesystem hardlink/unlink, **not an atomic rename**. Successful
staging still reads no CV bytes and uses constant memory. Restore remains
no-overwrite; no new journal or ledger design.

If partial cleanup also fails, the primitive raises `StorageStagingError` with
fixed message `Storage staging cleanup unresolved`, suppresses raw exception
chaining, and emits only
`component=storage_staging code=STORAGE_STAGE_CLEANUP_UNRESOLVED unresolved_count=1`.
The original stays present; the unresolved trash hardlink remains detectable for
later orphan reconciliation. This is an explicit unresolved failure, never a
claim of successful staging or clean rollback.

Eleven added cases in `backend/tests/test_s4_storage_authority.py`:

- `test_partial_stage_source_unlink_failure_cleans_new_trash` (document/photo):
  real hardlink, source unlink fails, exact original bytes remain, trash empty.
- `test_partial_stage_cleanup_failure_is_closed_and_observable` (document/photo):
  both unlinks fail, original and trash remain the same inode with identical bytes;
  fixed error/log, no raw path/key/tenant/content/exception payload in emitted logs
  or formatted traceback, no logging exception/stack payload.
- `test_partial_stage_absent_source_and_link_failure_do_not_mutate_original`
  (document/photo × absent/link/trash-parent): absent returns `None`; link errors
  propagate without source mutation or a new trash object.
- `test_partial_stage_cascade_later_source_unlink_failure_restores_earlier_assets`:
  three historical documents, first source already staged, second real trash link
  succeeds then source unlink fails; candidate and every exact document row remain,
  all original bytes are restored/preserved, trash empty. Byte-buffering is vetoed
  during staging/restore.

First correction verification: full S4 suite **39 passed in 10.28s**.

Focused compatibility verification on the same corrected production/tests:

```bash
cd backend
uv run pytest -q tests/test_s4_storage_authority.py tests/test_candidate_documents.py tests/test_candidate_photo_service.py tests/test_candidate_photo_worker.py tests/test_candidate_photo_noninterference.py tests/test_demo_seed.py tests/test_demo_user_ownership.py tests/test_upload_body_limit.py tests/test_folder_indexer.py tests/test_folder_indexer_cli.py tests/test_folder_scanner_bounds.py tests/test_folder_reconciliation.py tests/test_folder_reconciliation_cli.py tests/test_folder_ingestion_compensation.py tests/test_logging_privacy.py tests/test_logging_privacy_runtime.py tests/test_audit_privacy_guard.py tests/test_ops_diagnostic_privacy.py tests/test_no_exfiltration.py tests/test_ops_no_exfiltration.py tests/test_http_response_policy.py
```

```text
........................................................................ [ 21%]
........................................................................ [ 42%]
........................................................................ [ 63%]
........................................................................ [ 84%]
...................................................                      [100%]
339 passed in 225.52s (0:03:45)
```

This includes the full S4 storage authority suite (ambiguous commit, restore
collision, historical originals, mixed photos/originals, reset-demo and tenant
isolation), candidate deletion/photo recovery, direct upload, folder ingestion/
reconciliation, and S2 privacy/no-exfiltration. DB suites ran sequentially.

## Architecture (see D-104)

Per-session ledger + SAVEPOINT-scoped persist + DB-arbitrated settle for created
originals; staged (link/unlink into `.trash`, constant memory) deletes with
purge-after-durable-commit / no-overwrite exact restore for candidate hard-delete
and `reset_demo`; tenant/shape-validated storage primitives; `finally` temp cleanup
in `LocalFilesystemStorage.save`.

## Storage save/delete caller inventory

| Caller | Operation | S4 status |
|---|---|---|
| `candidate_document_service.persist_candidate_document` | `storage.save` + CandidateDocument/Canonical/audit | SAVEPOINT + ledger; self-compensates, caller rollback/commit failure compensated |
| `api/v1/candidates.post_candidate_document` | direct upload | `recover_on_failure` around revalidate/persist/commit |
| `folder_indexer_service._handle_new_file`, `_handle_changed_or_retry` | persist via shared service | covered by the shared persist; callers below own the transaction |
| `folder_reconciliation_service.reconcile_folder` | index + commit | `recover_on_failure` |
| `cli._index_folder`, `cli._seed_demo` | caller-owned transactions | `recover_on_failure` / `commit_with_recovery` |
| `demo_seed_service.seed_demo` (via `ingest_candidate_document`) | persist | CLI `recover_on_failure` |
| `candidate_service.delete_candidate_cascade` | original + photo deletion | staged deletes, `commit_with_recovery`, exact restore |
| `demo_seed_service.reset_demo` | tenant cascade (previously orphaned originals and photos) | now stages originals and photos; finished by `commit_with_recovery` |
| `candidate_photo_service` | derived-photo save/delete | unchanged (own S-photo compensation); only the photo delete step in the cascade moved to staging |
| `ui/router.candidate_document_original` | read only | unchanged |

No other production path removes CandidateDocument authority (no soft-delete, no
retention sweeper yet; tenant DB cascade is reachable only through `reset_demo`).

## Residual hard-crash window

No durable journal (no migration). A process kill (power loss, SIGKILL) between a
filesystem mutation and its compensation can leave: an unreferenced original
(killed after `save`, before commit); a duplicate hardlink under `.trash` and the
original name (killed between link creation and source unlink); or an object
under `.trash` (killed after completed
`stage_delete`, before purge/restore), where the DB either kept the rows (object
absent at its key but intact in `.trash`) or deleted them (orphan in `.trash`).
Bytes are never destroyed irrecoverably before the DB outcome is durable. Repair
is the later #46 orphan/retention reconciliation (DB-vs-storage sweep that
purges or restores `.trash`). A crash mid `reset_demo` of an already committed
tenant delete has no rows left to enumerate; same owner.
Separately, explicitly reported staging-cleanup, compensation or post-commit purge
failures can leave observable residuals for that same future reconciliation. The
handled partial-mutation gap at the rejected head was not a hard-crash window;
it is corrected as described above when cleanup succeeds.

## Original S4 regressions (rejected-head history)

`backend/tests/test_s4_storage_authority.py` (28 tests): creation / canonical /
audit failure (1, 2); commit failure for parsed and terminal parse-failure
documents (3, 4); successful commit retention; ambiguous commit that became
durable (no deletion); failure before save (no compensation); storage save and
`os.replace` failure with temp cleanup (5, 6); API upload commit failure with
safe HTTP 500 (7); shared folder-ingestion commit failure and mid-scan failure
(8); direct caller rollback compensated lazily; delete with later row-delete /
audit / commit failure (9); multiple originals with partial staging failure and
no byte reads (10); already-missing original not fabricated (11); restore
collision fails closed and is idempotent with identical bytes (12, 12b); mixed
photos + originals, then successful delete with audit semantics (13, 14);
ambiguous delete commit purges instead of restoring; tenant isolation of every
primitive including forged handles and traversal keys (15); idempotent
delete/stage/restore/purge (16); unresolved compensation is never success and
leaks no path/key/exception payload (17); purge failure after durable delete does
not fail the operation; `reset_demo` stage/restore/purge. Existing photo-recovery
tests now inject faults at `stage_delete`; `reset_demo` callers in demo tests pass
the stores.

## Original S4 gates (rejected-head history)

`uv run ruff check .` clean; `uv run mypy src` clean (232 files); full
`uv run pytest -q`: **3706 passed**; `uv run alembic heads`: `b88a2c4d6e10` (unchanged);
`git diff --check` clean; `scripts/scan-tracked-tree.sh` clean. No migration,
dependency or lockfile change. Exact-head CI is recorded in the PR.

Rejected-head CI run [37126184791](https://github.com/a-r3/meyar/actions/runs/37126184791):
attempt 1 failed the existing S3
`test_real_rest_candidate_search_evaluation_and_204_are_private` because scoring
returned 404 instead of 200; attempt 2 succeeded at the **same exact SHA**.
Both attempts are live-verified. The cause remains unexplained; attempt 2 does
not erase attempt 1 or confer acceptance on the rejected implementation.
The correction requires full local gates and a fresh exact-head CI run. A failed
fresh run must be investigated before re-acceptance, never proactively rerun for green.

## First correction local gates and delivery boundary (977aaf34 history)

Final production/tests were unchanged between focused and full gates. Normal full
suite ran sequentially after the focused suite, with no exclusions or diagnostic
environment override:

```text
$ uv run ruff check .
All checks passed!
$ uv run mypy src
Success: no issues found in 232 source files
$ uv run pytest -q
3717 passed in 638.19s (0:10:38)
$ uv run alembic heads
b88a2c4d6e10 (head)
```

`git diff --check` and `scripts/scan-tracked-tree.sh` are clean; the scan includes
the intentionally staged correction before commit. No migration, dependency or
lockfile change. The existing `.aws` entry was not inspected, staged or modified.
No folder concurrency, readiness, retention, schema drift, #45/#35/#36/#50 or
Target-Mac work was started. PR #119 remains the only delivery PR and is associated
with existing M9 milestone 10. #46/#35/#36/#45/#50 remain OPEN; nothing is merged.
The new exact head, fresh CI run ID and first-attempt outcome are recorded in
PR #119's delivery report, avoiding a self-referential source commit.

Its fresh CI [37130613061](https://github.com/a-r3/meyar/actions/runs/37130613061)
completed SUCCESS on attempt 1 at `977aaf34b4e67be3602d95df1cbc1eea0063fe49`:
3717 passed in 605.62s, all quality/migration/hang-diagnostic/demo-wrapper steps
successful. That head was subsequently acceptance-REJECTED for rollback failure;
the preceding gates and first remediation evidence remain historical, not acceptance.

## Rollback-failure acceptance blocker: exact pre-fix proof

Starting HEAD and live PR #119 both matched
`977aaf34b4e67be3602d95df1cbc1eea0063fe49`; hooks `.githooks`, base/remote main
`df5264d869ba3810b857b77b7d9491b77b6f39c1`. PR OPEN, M9 OPEN, no auto-merge or
closing references, #46/#35/#36/#45/#50 OPEN. No production diff before proof.

Added `test_rollback_failure_reconciles_pending_effects_using_fresh_db_truth`
(created/staged × uncommitted/durable). Each case saves a real synthetic original
or stages an owned historical original and deletes its document row in the writer
transaction. The test proves the ledger is PENDING before injecting an ordinary
primary error and `db.rollback()` failure before an outer event. Durable variants
use a real connection COMMIT without the Session event; the ledger is still PENDING.
This proves neither direction may guess the outcome. The rejected source emitted
only the primary failure, retained PENDING entries, and made zero authority queries.

```bash
cd backend
uv run pytest -q tests/test_s4_storage_authority.py -k rollback_failure_reconciles_pending_effects
```

Captured failing output (opaque synthetic keys/paths omitted):

```text
> assert pending == [], (...)
E AssertionError: primary escaped with PENDING storage effects; durable-truth queries=0
E assert [_Created(... state=<_State.PENDING: 'PENDING'>)] == []
E assert [_Staged(... state=<_State.PENDING: 'PENDING'>)] == []
FAILED ...[uncommitted-created]
FAILED ...[uncommitted-staged]
FAILED ...[durable-created]
FAILED ...[durable-staged]
4 failed, 39 deselected, 2 warnings in 2.20s
```

The two warnings were `transaction already deassociated from connection` in the
durable variants' rejected-source teardown after explicit connection COMMIT.
Corrected recovery invalidates/reset-closes that Session before verification;
the final S4 run has no warnings. This handled rollback failure is separate from
the documented process-kill residual window.

## Rollback-state correction and regressions

`rollback_with_recovery` marks any effects still PENDING after rollback UNKNOWN.
This state means “resolve with DB authority”, never “rollback is known”. Rollback
failure retains `needs_invalidation`; recovery calls supported
`AsyncSession.invalidate()` and verifies no active transaction remains before
settlement. SQLAlchemy invalidation discards the uncertain connections, expunges
ORM state and retains Session.info/ledger. Tests prove the verification transaction
starts fresh and its `pg_backend_pid()` differs from the failed writer's connection.

Fresh authority decides every UNKNOWN effect:

| Effect | Durable reference present | Durable reference absent |
|---|---|---|
| Created original | Keep bytes | Delete owned original |
| Staged delete | Restore exact original | Purge staged object |

Reset failure, reset that leaves the writer active, or failed fresh authority query
raises closed `StorageCompensationError` and retains UNKNOWN entries and bytes.
`settle`/lazy `settle_leftovers` cannot query an uncertain writer when reset is still
required. Retry resets first, reconciles correctly and is idempotent. No journal.
Outermost event behavior is retained; nested rollback/release never marks unrelated
pending originals rolled back/durable.

Private primary/rollback/reset/query errors are not formatted into logs or default
chained tracebacks. The original primary object is retained on `error.primary`;
an already closed staging error remains the exact cause, while arbitrary primary
messages receive a fixed `Primary storage operation failed.` cause. Error text stays
`Storage compensation incomplete.`; logging uses closed structural fields only.

Eleven new cases in `backend/tests/test_s4_storage_authority.py`:

- `test_rollback_failure_reconciles_pending_effects_using_fresh_db_truth` (4):
  created/staged × uncommitted/durable, real files/DB writes, PENDING proof, fresh
  server connection, correct byte fate, no discarded entries and repeat recovery.
- `test_rollback_failure_unresolved_effects_fail_closed_and_retry_safely` (6):
  created/staged × failed query/failed invalidation/reset no-op; bytes observable,
  UNKNOWN ledger retained, blocked lazy uncertain-query path, raw primary and DB
  fault payloads absent from logs/default traceback, safe exact cause where
  appropriate, successful retry and idempotent repeat.
- `test_savepoint_failure_preserves_other_pending_original_and_outer_commit` (1):
  first SAVEPOINT release remains uncommitted to an independent observer; second
  document's failed SAVEPOINT compensates only its own bytes, first stays PENDING
  and becomes independently visible only after outer commit.

Final S4 verification: **50 passed in 14.79s**. Focused compatibility verification:
**350 passed in 230.38s** using:

```bash
cd backend
uv run pytest -q \
  tests/test_s4_storage_authority.py tests/test_candidate_documents.py \
  tests/test_candidate_photo_service.py tests/test_candidate_photo_worker.py \
  tests/test_candidate_photo_noninterference.py tests/test_demo_seed.py \
  tests/test_demo_user_ownership.py tests/test_upload_body_limit.py \
  tests/test_folder_indexer.py tests/test_folder_indexer_cli.py \
  tests/test_folder_scanner_bounds.py tests/test_folder_reconciliation.py \
  tests/test_folder_reconciliation_cli.py tests/test_folder_ingestion_compensation.py \
  tests/test_logging_privacy.py tests/test_logging_privacy_runtime.py \
  tests/test_audit_privacy_guard.py tests/test_ops_diagnostic_privacy.py \
  tests/test_no_exfiltration.py tests/test_ops_no_exfiltration.py \
  tests/test_http_response_policy.py
```

## Rollback correction local gates and delivery boundary

Production/tests were final before focused and full verification. The normal full
suite ran sequentially after focused compatibility, with no exclusions or diagnostic
environment overrides:

```text
$ uv run ruff check .
All checks passed!
$ uv run mypy src
Success: no issues found in 232 source files
$ uv run pytest -q
3728 passed in 587.15s (0:09:47)
$ uv run alembic heads
b88a2c4d6e10 (head)
```

`git diff --check` and `scripts/scan-tracked-tree.sh` are clean, including the
intentionally staged correction before commit. Only recovery, its S4 tests and
the three authority/validation documents changed; no migration/dependency/lockfile.
The existing `.aws` entry was not inspected, staged or modified. No later slice
was started. PR #119 is the only delivery PR under existing M9 milestone 10;
#46/#35/#36/#45/#50 remain OPEN. New exact head and fresh CI run ID/attempt/conclusion
are recorded in PR #119's delivery report, avoiding a self-referential source commit.
Green gates do not establish S4 acceptance; independent new-head re-audit is pending.

## Separate concurrency finding — documented, not implemented

Independent audit identified the delete/upload race. Static source inspection
confirms `delete_candidate_cascade` uses the unlocked `get_candidate` and lists
documents before deleting the candidate. Direct upload's persistence revalidation
holds a shared candidate-row lock through commit. A concurrent upload can commit
a new document after delete's list was read; the later candidate-row DELETE can
cascade that new row without ever staging the new original. No concurrency
reproduction or correction is claimed here. This finding remains owned by the
remaining #46 concurrency work; no candidate lock, API upload or deletion behavior
was modified in this correction. #46 stays OPEN.

## HUMAN ACTION REQUIRED

Independently re-audit the NEW exact PR #119 head after its fresh CI is verified.
Do not merge, enable auto-merge, close #46 or begin a later slice. Reply with audit
findings or `S4 PASS` if independently accepted. Green local gates and CI are not
independent acceptance; S4 acceptance is not claimed by this correction.

Fixture note: tests that inserted arbitrary non-generated `storage_key` values
(`test/<hex>`, used with candidate delete) now use the real `<tenant>/<hex>` shape,
because the new storage primitives reject keys outside the tenant namespace.
