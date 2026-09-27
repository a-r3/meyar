# MEYAR — Backup & Restore

Status: PR9 / PR #72 is merged and post-merge verified at accepted main
`2dcf0a8783e49008b0eb707d81b42248209b34d6`. PR10 adds isolated
restore for independent review. PR11 / PR #74 is merged and post-merge
verified at accepted main `38624ca3440c7dbf750d4248d8e0a9992b7192b5`.
PR12 uses verified backup as a mandatory staged-update gate. Slice 13 (issue #20) separately has an
executed synthetic backup/restore acceptance proof in
`backend/scripts/backup_restore_acceptance.py`. Production cutover remains
future #35 work. Neither mechanism creates a backup schedule.

## Scope

Two things must be backed up together, consistently, or a restore is
incomplete:

1. **PostgreSQL** — tenants, API-key metadata/hash, jobs/criteria,
   candidate/document metadata, identity/profile/photo versions, embedding
   metadata + vectors (pgvector), evaluations, audit events.
2. **Storage root** — raw CV bytes (`LocalFilesystemStorage`) and sanitized
   derived JPEGs in the `photo/<tenant>/<opaque-id>` namespace
   (`LocalPhotoStorage`), both under `MEYAR_STORAGE_ROOT`. Photo keys and
   SHA-256 values live only in `candidate_photo_versions`.

A database-only or storage-only backup is not sufficient: a restored
database with no matching storage directory has `candidate_documents`
rows whose `storage_key` resolves to nothing (original-CV retrieval
fails), and a restored storage directory with no matching database has
orphaned files with no metadata. **Always back up and restore both
together, from one stopped-service maintenance window.**

No cloud backup infrastructure is used or required — this uses
PostgreSQL-native tooling (`pg_dump`/`pg_restore`) plus a plain
filesystem archive.

## Installed production backup

First run `deployment-ready`, then privileged `service-stop`. During the
backup window, bank operations must not independently mutate this
PostgreSQL database; the tool cannot prevent writers outside MEYAR's
operational boundary. Run as the trusted non-root install owner:

```bash
<root>/current/.venv/bin/python -m meyar.ops.cli backup-create \
  --install-root <root> --label <validated-launchd-label> \
  --backup-id <safe-id> --pg-bin-dir <absolute-postgresql-client-bin-dir>

<root>/current/.venv/bin/python -m meyar.ops.cli backup-verify \
  --install-root <root> --backup-id <safe-id> \
  --pg-bin-dir <absolute-postgresql-client-bin-dir>
```

Then run privileged `service-start` and `deployment-ready` again. The
complete sequence is `deployment-ready` → `service-stop` →
`backup-create` → `backup-verify` → `service-start` → `deployment-ready`.
`backup-create` itself does not stop or start the service. It requires
confirmed launchd absence (exit 113) and a refused connection to the
canonical numeric-loopback application port before and after the snapshot.

`<safe-id>` is 1–80 ASCII characters matching
`[A-Za-z0-9][A-Za-z0-9_-]{0,79}`. The fixed artifact directory is
`<root>/shared/backups/<safe-id>/` and contains `database.dump`
(PostgreSQL custom format), `storage.tar` (the complete canonical
`shared/storage` tree, including original CVs and derived photos), and
`backup_manifest.json` (version 1). The manifest carries the backup ID,
time and operator UID; active release ID, source SHA, and Alembic head;
fixed filenames, SHA-256 digests, byte sizes, and storage file count.
It contains no passwords, database URL, candidate details, tenant IDs,
or storage member paths. The files contain **production candidate data**
and need production-equivalent access controls. The directory is `0700`,
the files `0600`, and publication is atomic and no-clobber.

Creation succeeds only after the backup files, backup directory, and parent
`backups` directory are fsynced. If parent-directory fsync fails after the
rename and the published directory still has this invocation's identity,
`backup-create` moves it back to private staging and fsyncs the same
validated parent directory again. Only if that rollback fsync succeeds does
it clean up the private stage and report
`BACKUP_PUBLICATION_DURABILITY_FAILED`; the requested backup ID is then
durably absent. If rollback fsync also fails, the command preserves the
private stage and reports `BACKUP_PUBLICATION_STATE_UNCERTAIN`.
That uncertain result also covers a changed final identity or unsafe
rollback. Inspect `shared/backups`, resolve any private `.backup-*` residue,
and run `backup-verify` if the requested public ID exists before retry.
Do not assume the failed command left that ID durably free or delete an
unfamiliar directory as part of a retry.

`backup-verify` checks the manifest, hashes, dump structure via
`pg_restore --list`, and safe tar members without connecting to the
production DB or extracting anything. It does not read protected `.env`.
**Backup-created != restore-tested.**

For a PR12 update, run `backup-create` and `backup-verify` **after**
`update-prepare` and privileged `service-stop`, then pass the backup ID
to `update-apply`. Apply re-runs this exact artifact verifier and requires
the backup's release ID, source SHA, and Alembic head to equal the plan's
source identity. The backup timestamp must be at or after the plan time.
An old, foreign, incomplete, or tampered backup returns
`UPDATE_BACKUP_INVALID` before migration or activation. Neither PR12
rollback nor PR10 isolated restore silently restores the production DB.

## PR10 isolated installed-host restore

First verify the published backup. Have a bank PostgreSQL operator precreate
an **empty isolated** database on the same protected host and port, accessible
to the same protected MEYAR database principal. Provision any required
extension (including pgvector) through the normal database infrastructure
process. Run as the non-root install owner, from the exact active release:

```bash
<root>/current/.venv/bin/python -m meyar.ops.cli backup-verify \
  --install-root <root> --backup-id <safe-backup-id> \
  --pg-bin-dir <absolute-postgresql-client-bin-dir>

<root>/current/.venv/bin/python -m meyar.ops.cli restore \
  --install-root <root> --backup-id <safe-backup-id> \
  --restore-id <safe-restore-id> --target-database <isolated-database-name> \
  --pg-bin-dir <absolute-postgresql-client-bin-dir>
```

Confirm the sole success code `RESTORE_COMPLETED`. The workspace is always
`<root>/shared/restores/<restore-id>/`, with private `storage/` and
`restore_manifest.json`. The `shared/restores` parent is install-owner
controlled, never service-writable. Restore IDs use the backup ID grammar;
database names use lowercase PostgreSQL identifiers, 1–63 characters,
starting with a letter or underscore; PostgreSQL maintenance/template
database names are refused. The target name must differ from the protected
production database name. There is no DB URL, password, SQL,
backup path, or storage destination option.

The command reuses the complete PR9 verifier before touching the target.
The backup release ID, source SHA, and Alembic head must match the exact
verified active release. The target must be reachable and have no user
relations or non-public user schema; extension-owned objects are allowed.
The command manually streams only regular USTAR files and directories to a
private stage, checks extracted file hashes against the archive, and runs
trusted absolute `pg_restore` with `--exit-on-error --single-transaction
--no-owner --no-privileges --no-password`. It then checks connectivity
and exactly one expected Alembic revision before no-replace publication.
Passwords travel only through a private temporary `PGPASSFILE`. Neither
the live database nor `shared/storage` is written by this command.

Filesystem and PostgreSQL cannot share one transaction. If PostgreSQL
restore fails, its transaction rolls back and no completed workspace is
published. If PostgreSQL succeeds but the DB postcheck or filesystem
publication fails, `RESTORE_INCOMPLETE_ISOLATED_TARGET` leaves isolated
state for operator inspection. **Discard/recreate the isolated target DB
before retrying**; the command never drops it. An existing restore ID is
never overwritten. Only a completed restore has a completion manifest.
An isolated restore completed **!=** production cutover, application
readiness, update, or rollback.

## Synthetic restore demonstration only

The following demonstrates the existing synthetic acceptance script's
isolated destination. It is separate from PR10's installed-host command.
Never overwrite a live database or live storage root. Production cutover
remains a later #35 slice.

```bash
# 1. Create/point at an empty destination database, then:
pg_restore -h <dest-host> -p <dest-port> -U meyar -d meyar --no-owner database.dump

# 2. Extract storage into an empty destination storage root:
mkdir -p /path/to/dest-storage
tar -C /path/to/dest-storage -xf storage.tar

# 3. Point the app at the restore and start it:
MEYAR_DATABASE_URL=postgresql+asyncpg://meyar:<password>@<dest-host>:<dest-port>/meyar \
MEYAR_STORAGE_ROOT=/path/to/dest-storage \
uv run uvicorn meyar.main:app
```

## Validation after restore

At minimum, confirm:

- expected row counts per tenant-scoped table (tenants, api_keys, jobs,
  job_criteria_versions, candidates, candidate_documents,
  candidate_profile_versions, candidate_identity_versions,
  candidate_photo_versions,
  candidate_embedding_versions, evaluations, audit_events)
- a known candidate's original CV opens via
  `GET /ui/candidates/{id}/documents/{id}/original` and its bytes are
  unchanged (`sha256` match)
- the current authorized candidate photo row and its derived JPEG survive
  restore with matching SHA-256; a foreign tenant cannot resolve the photo
- a known `job_criteria_version_id` + `candidate_profile_version_id` +
  `evaluation_as_of_date` score request against the restore reuses the
  original `Evaluation` (`reused=true`) with the same `numeric_score` —
  proving deterministic provenance survived the round trip, not just
  that rows exist
- tenant isolation still holds (a second tenant's data, if present,
  remains inaccessible to the first)

`backend/scripts/backup_restore_acceptance.py` automates exactly this
sequence end to end against **synthetic, disposable data only** — it
seeds a synthetic tenant in a disposable source Postgres container
(never the developer's own database), backs it up, restores into a
second disposable container and a separate storage directory, and
asserts all of the above. Run it on demand:

```bash
cd backend
uv run python scripts/backup_restore_acceptance.py
```

It requires `docker`, `pg_dump`, and `pg_restore` on the host; if any is
missing it reports that environmental gap explicitly and exits non-zero
rather than claiming a pass. It is not part of the default `pytest -q`
gate (it orchestrates real disposable containers, not in-process
fixtures) — run it deliberately, not on every commit.

## Security considerations

- Dump and archive files are as sensitive as the production data they
  contain — store and transmit them under the same access control as
  the database itself; never commit them to git (`.githooks/pre-commit`
  and `scripts/scan-tracked-tree.sh` both reject tracked `*dump*.sql`/
  `.sqlite*`/`.dump` files and paths under `backend/var/`).
- The database persists API-key hashes rather than plaintext API keys,
  but the dump and archive are still sensitive candidate data.
- Restoring into a destination that is reachable from outside the
  operator's own network re-creates the same tenant-isolation and
  authentication surface as production — treat a restore target with
  the same operational care as a production deployment, not as a
  throwaway sandbox, unless it is genuinely disposable (as in the
  synthetic acceptance script above).

## Retention responsibility

Retention/backup-frequency policy values are configurable, not
hardcoded (`docs/SECURITY_PRIVACY.md`) — how often backups are taken,
how long they are kept, and where they are stored is an operational/
business decision for the deployment owner, not something this
document or the codebase prescribes.

## Deployment responsibility

This runbook proves the mechanism works end to end on synthetic data.
It does not itself schedule production backups, encrypt the dump/
archive at rest, or replicate off-host — those are deployment-owner
responsibilities layered on top of this mechanism, consistent with
`docs/SECURITY_PRIVACY.md`'s existing position that encryption-at-rest
for document storage is a deployment/infrastructure control (host/disk-
level encryption), not an application-layer feature in MVP.
