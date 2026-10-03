# Issue #46 S7 — folder reconciliation / same-content dedup concurrency

Status: implemented; independent acceptance pending. Refs #46 under the existing
**M9 — Deployment, Benchmark & Integration Readiness** milestone (10). Bounded
concurrency slice, not completion of #46.

## Verified accepted starting state (live, before any production edit)

- PR #121 MERGED; accepted head `8441392ab903433015942f6cb8dd553794addedc`.
- Squash/main `3582189fbb6d4e462f346bc094ccd996a0d8423b`; accepted and merged tree
  and main tree both `14b3199d64797b739ec26dcd89d34210c48124db`.
- Exact-head S6 CI run 37142749403, attempt 1, conclusion SUCCESS.
- #46/#35/#36/#45/#50 OPEN. Hooks path `.githooks`. Untracked `.aws` untouched.
- S6 is **ACCEPTED + MERGED**; branch `fix/46-s7-folder-reconcile-concurrency` cut from it.

## Production sequence at the S6 baseline

`index_folder` held ONE caller transaction for the whole scan: `get_or_create_folder_source`
(plain SELECT, then INSERT) -> list existing rows -> per file `_prepare_new_path`
(`find_indexed_file_by_content_hash`, then parse) -> `create_candidate` ->
`persist_candidate_document` (S4-tracked `storage.save`) -> `create_folder_indexed_file`;
the caller commits (`index-folder`, `reconcile-folder`). Nothing serialized two runs; the
exact-content dedup is a read-then-write with no unique identity behind it.

## Before-fix proof (exact S6 source, real PostgreSQL READ COMMITTED, real synthetic storage)

`backend/tests/test_folder_reconcile_concurrency.py`; the first run is held at a parser
gate while it owns its authority, a second independent session is started, and
`pg_blocking_pids` / actual overlap says what it did. 18 race tests failed, 2 passed
(two further failures were bugs in my own test helper and were fixed before the fix).

- **A, same source + same new file (source already exists):** loser reached the parser
  (no serialization), saved its own original, then failed with an unhandled
  `IntegrityError: uq_folder_indexed_files_path`. S4 compensated the loser's original, so
  no original leaked, but the run crashed and no truthful summary existed.
- **A, source itself new:** `IntegrityError: uq_folder_sources_tenant_root` at the first flush.
- **A, unsynchronized runs:** the same unique violation; summaries inconsistent.
- **B, different sources + identical bytes:** **two Candidates** (and two documents/originals
  before dedup could run) — both transactions observed "not yet present".
- **Duplicate link vs candidate delete:** link insert waited on the deleter's FOR UPDATE, then
  failed with an FK `IntegrityError` after the candidate committed away.
- Different tenants / unrelated sources were already independent (tests passed).

## Fix

1. `get_or_create_folder_source` — `INSERT .. ON CONFLICT DO NOTHING` + `SELECT .. FOR NO KEY
   UPDATE` (populate_existing), held to commit/rollback. The conflicting insert itself waits for
   the holder, so initial and existing state use one mechanism and no advisory lock is needed
   for source state. `index_folder` re-checks the live tenant after the wait.
2. `content_authority.lock_tenant_content` — namespaced `pg_advisory_xact_lock` keyed by
   (tenant, sha256), taken in `_prepare_new_path` before the dedup lookup. Held to commit.
   (No row exists to lock for new content and the schema has no content-unique identity.)
3. Duplicate link: Candidate `SHARE` lock (`get_candidate(share=True)`) before linking; a lost
   delete race repeats the lookup.
4. `index_folder_and_commit` (used by `index-folder` and `reconcile-folder`): storage-recovery
   scope + commit; a PostgreSQL deadlock victim (`40P01`, opposite-order content across sources)
   is compensated by S4 and the scan retried, max 4 attempts; anything else propagates.

Lock order: Tenant (non-locking check, SHARE at commit; suspension waits on no source/content/
candidate lock) -> FolderSource -> content -> Candidate. No edge added to S1/S5/S6. Tenant is
intentionally not held for the whole (long) scan so suspension stays prompt.
No migration (`alembic heads` unchanged: `b88a2c4d6e10`), no dependency change.

## Final invariants asserted (independent observer connection + filesystem)

Candidate count, CandidateDocument count, FolderIndexedFile ownership (INDEXED rows point at a
real candidate/document pair with matching hash), stored originals == durable document keys, no
`.trash`/`.tmp` leftovers, and truthful scan summaries (winner commit/ambiguous -> loser
`unchanged=1, new=0`; winner rollback -> loser `new=1, successful=1`; dedup loser
`new=1, successful=1, failed=0`).

## Regression coverage (24 tests)

- same source, same file x {commit, rollback, ambiguous-commit} x {source exists, source new} (6)
- different sources, same bytes x {commit, rollback, ambiguous} (3)
- unrelated work proceeds while a run is held: other tenant (same bytes), same tenant other source (2)
- tenant suspension during contested persistence: same source, same content (2)
- duplicate link waits for a candidate delete, then ingests fresh (1)
- unsynchronized bounded repetitions: 4 x same-source runs, 4 x five-source same-content runs (8)
- opposite-order deadlock victim: S4 compensation of its saved original + retry, 2 repetitions (2)
  (with `MAX_DEADLOCK_ATTEMPTS=1` this test fails with `DeadlockDetectedError`)

## Gates (actual output)

Full `pytest -q`: 3784 passed. `ruff check .` clean; `mypy src` clean (233 files);
`alembic heads`: `b88a2c4d6e10 (head)`; `git diff --check` clean; `scripts/scan-tracked-tree.sh` clean.
Focused (S7 + folder index/reconcile/compensation/CLI + S4 + S5/S6 + tenant authority/isolation/
auth + privacy/no-exfiltration + scanner bounds): 243 passed.

## Residual #46 scope

Changed-file source-read/stat races; a changed file re-ingested for a candidate being deleted;
downstream extraction/photo concurrency of two same-source runs after ingestion commits; folder
transaction-lifetime/inference refactor; retention/orphan sweeper and S4 hard-kill residuals;
configured embedding readiness; schema drift. #46/#35/#36/#45/#50 remain OPEN.
