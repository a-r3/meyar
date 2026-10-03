# Issue #46 S9 — downstream folder-reconciliation concurrency authority

Status: implemented; independent acceptance pending. Refs #46 under the existing
**M9 — Deployment, Benchmark & Integration Readiness** milestone (10). Bounded
concurrency slice, not completion of #46.

## Verified accepted starting state (live, before any production edit)

- PR #123 MERGED; accepted head `7d87f9d160886f99fa4eb85e3b4b4481562c0f2d`.
- Squash/main `a1841768fca2cd92d733852a71d42087e51341be` (sole parent S7 main
  `1a4e0ce95991fe43449d0d45d9da1b28b4d10ada`), tree `e988994d5befd7610dff1fa6e03e72c23f613a73`.
- #46/#35/#36/#45/#50 OPEN. Hooks path `.githooks`. Untracked `.aws` untouched.
- S8 is recorded **ACCEPTED + MERGED** (STATUS, D-108, `ISSUE_46_S8_VALIDATION.md` corrected).

## Baseline behavior (exact S8 source)

`process_pending_candidates` read the folder rows, then for each not-ready candidate ran
`extract_candidate_profile` -> `extract_candidate_identity` -> `embed_candidate_profile` in ONE
transaction (first flush = the STARTED audit event, before inference), committing at the end.
Version numbers are `max(version_number)+1` computed at INSERT time; profile authority is "latest
attempt's document, newest COMPLETED".

## Before-fix proof (real PostgreSQL READ COMMITTED, gated local providers, Events,
`pg_stat_activity` / `pg_blocking_pids`; no sleep as proof)

`backend/tests/test_folder_downstream_concurrency.py` on exact S8 production (6 failed, 4 passed
in the first run; the changed-document defect re-checked separately):

- **Same candidate + document, two concurrent runs, profile missing:** both reached inference and
  both committed → **2 COMPLETED CandidateProfileVersions** (the loser's `max+1` is computed after the
  winner's commit, so there is no key conflict). **Identity missing:** 2 COMPLETED identity versions.
  **Embedding missing:** the loser raised the unique-constraint `IntegrityError` and was reported as a
  failed candidate (`FOLDER_RECONCILE_CANDIDATE_FAILED`, `failed=1`).
- **Changed document:** a run snapshotted document A, was held in inference, the file changed to B,
  B was fully processed, then the held run persisted A as the NEWEST version → the effective profile
  was A's (`effective.candidate_document_id == A`), i.e. stale content became the current, searchable
  authority.
- **Candidate deleted mid-run:** reported as `failed=1` (FK `IntegrityError` + failed audit event).
- **Transaction lifetime:** during gated profile inference one backend was `idle in transaction`
  (a pooled connection held through local inference); version-insert locks were then held through later
  stages. Confirmed pre-existing, and — here — inseparable from the fix: a revalidated persistence phase
  needs phase-separated transactions.
- Already correct at S8 (passed): delete after downstream persistence (waits), tenant suspension during
  inference (no versions, `TenantInactiveError`), three unrelated runs (two candidates + another tenant)
  all inside inference simultaneously (no global serialization).
- **Photo audit:** two concurrent `process_photo_for_document` calls for one document converge on one
  AVAILABLE row and one derived asset with a clean tree (S6 phases + `uq_photo_document_extractor` +
  exact-row recheck). It is not subject to the downstream version race; **no change made**.

## Root cause

Authority was decided before inference and never revalidated at persistence; persistence was not
serialized per candidate (so `max+1`, "already completed" and "document still current" were all
read-then-write); and the whole candidate pipeline held one transaction/connection across local
inference.

## Fix

- `extraction/service.py`, `extraction/identity_service.py`, `services/candidate_embedding_service.py`
  are split into DB prepare, DB-free inference (`infer_candidate_profile`, `infer_candidate_identity`,
  provider `embed`) and DB persist. The legacy `extract_*` / `embed_candidate_profile` recompose them with
  identical behavior (existing API/CLI/demo tests unchanged).
- `folder_reconciliation_service._process_one_candidate_document` runs each stage as
  **Phase A** (tenant live check, candidate + CURRENT document check without lock, "already COMPLETED?"
  skip, prepare/audit; **commit** — connection released) → **inference with no transaction, pooled
  connection or row lock** → **Phase B** (Tenant SHARE, Candidate `FOR UPDATE`, exact newest
  CandidateDocument, "already COMPLETED by another run?" → discard, else persist; **commit**).
  Embedding Phase B additionally re-derives the effective profile and binds only to the same profile
  version. A failed/deferred provider outcome is recorded in Phase B (deferral raises the existing typed
  outcome after its audit).
- `get_newest_candidate_document` (current-document rule, same ordering as the presentable-photo rule).
- `ReconciliationSummary.superseded` + CLI line + audit `FOLDER_RECONCILE_CANDIDATE_SUPERSEDED`
  (closed reason codes `CANDIDATE_DELETED`, `DOCUMENT_NOT_CURRENT`, `PROFILE_NOT_CURRENT`; ids only).
- No migration (`alembic heads` unchanged `b88a2c4d6e10`), no dependency change, no new table, no
  process-local lock, no reservation/queue.

## Authority and lock order

Phase A: no lock. Phase B: Tenant SHARE (live) → Candidate row FOR UPDATE → commit (tenant commit guard
re-checks). Identical to candidate delete (S5/S6) and photo Phase B. S7/S8 scans take FolderSource →
content → Candidate SHARE and wait on a persister without any hold-and-wait edge back (a persister takes
no source/content lock). The Candidate lock is held only for Phase B — never across inference — and is
per candidate: different candidates, sources and tenants share no lock (asserted with a rendezvous that
requires three runs inside inference at once).

## Outcomes (all asserted from an independent connection; whole storage tree checked incl. `.trash`/`.tmp`)

- Concurrent same-document runs (profile / identity / embedding missing, plus 3×4 unsynchronized
  repetitions): exactly one COMPLETED profile, one identity, one embedding; no failed audit; both runs
  `ready_after=1, failed=0`. Duplicate local inference may still occur (wasted work, never duplicate
  authority); a durable reservation would need a migration and the evidence did not justify one.
- Delete wins during inference: nothing persisted, `failed=0, superseded=1`, no resurrected candidate.
- Persistence wins first: delete observed blocked on the Candidate row until the commit; then everything
  cascades; no failure.
- Changed document: stale document-A work is discarded (`superseded=1`), effective profile = B, a Java
  query no longer matches, embedding provenance is B's. A changed-file scan started during a held
  persistence phase is observed blocked on the Candidate lock (S8 SHARE vs S9 UPDATE) then proceeds; the
  new document wins after its own downstream run.
- Tenant suspension during inference: `TenantInactiveError`, nothing persisted, session reusable.
  `test_tenant_active_authority` was updated for the new contract: earlier stages that committed while
  the tenant was active are durable; the disabled stage and later stages persist nothing.
- Persistence-commit failure (rollback / durable-but-unacknowledged): handled, truthfully `failed=1` with
  an audit event, session reusable; the re-run converges to exactly one version of each kind.
- Provider failure and inference-busy deferral: unchanged behavior (existing reconciliation and
  `test_inference_busy_deferral` suites).
- **Transaction lifetime proof:** for each of profile, identity and embedding inference, while the local
  model is held mid-call, zero backends are `idle in transaction` and zero relation locks are held.

## Tests and gates

S9 regressions: `tests/test_folder_downstream_concurrency.py` — 18 tests (3 same-stage races, 3
unsynchronized repetitions, delete both directions, changed-document both directions, tenant
suspension, non-serialization, 3 transaction-lifetime stages, 2 commit-failure outcomes, photo audit).
Full `pytest -q`: 3838 passed. `ruff check .` clean; `mypy src` clean (233 files); `alembic heads`:
`b88a2c4d6e10 (head)` (unchanged); `git diff --check` and `scripts/scan-tracked-tree.sh` clean.
Focused (S9, S4, S5/S6 delete-upload-photo, S7, S8, folder reconciliation + CLI, profile/identity/
embedding/photo services, tenant authority/isolation, effective-profile, no-exfiltration/privacy,
busy deferral, docs policy): 391 passed. Existing tests adjusted for the new contract:
`test_tenant_active_authority` (earlier committed stages are durable; the disabled and later stages
persist nothing) and `test_m9_effective_profile` (commit the first document so the second is strictly
newer).

## Residual #46 scope

API/CLI single-transaction extraction (still spans inference); the tie-break of "newest document" by
`created_at` (transaction start time) for two documents created in the same transaction; retention/
orphan sweeper and S4 hard-kill residuals; configured embedding readiness; schema drift. #46/#35/#36/#45/#50
remain OPEN.
