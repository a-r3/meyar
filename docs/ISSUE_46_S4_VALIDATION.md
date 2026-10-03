# Issue #46 S4 — Original-CV storage <-> PostgreSQL recovery

Implementation proposed for independent acceptance. Refs #46, existing M9
milestone 10. #46 is not complete; #46/#35/#36/#45/#50 remain OPEN. D-104 records
the architecture.

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
(killed after `save`, before commit); or an object under `.trash` (killed after
`stage_delete`, before purge/restore), where the DB either kept the rows (object
absent at its key but intact in `.trash`) or deleted them (orphan in `.trash`).
Bytes are never destroyed irrecoverably before the DB outcome is durable. Repair
is the later #46 orphan/retention reconciliation (DB-vs-storage sweep that
purges or restores `.trash`). A crash mid `reset_demo` of an already committed
tenant delete has no rows left to enumerate; same owner.

## Regressions

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

## Gates

`uv run ruff check .` clean; `uv run mypy src` clean (232 files); full
`uv run pytest -q`: **3706 passed**; `uv run alembic heads`: `b88a2c4d6e10` (unchanged);
`git diff --check` clean; `scripts/scan-tracked-tree.sh` clean. No migration,
dependency or lockfile change. Exact-head CI is recorded in the PR.

Fixture note: tests that inserted arbitrary non-generated `storage_key` values
(`test/<hex>`, used with candidate delete) now use the real `<tenant>/<hex>` shape,
because the new storage primitives reject keys outside the tenant namespace.
