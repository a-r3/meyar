# MEYAR — Backup & Restore

Status: Slice 13 (issue #20) synthetic acceptance proof executed and
passing — see `backend/scripts/backup_restore_acceptance.py`. This
document is the operator runbook that script demonstrates; it does not
by itself constitute a production backup schedule (see Deployment
responsibility below).

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
together, from the same point in time.**

No cloud backup infrastructure is used or required — this uses
PostgreSQL-native tooling (`pg_dump`/`pg_restore`) plus a plain
filesystem archive.

## Prerequisites

- `pg_dump` / `pg_restore` (PostgreSQL 16 client tools, matching the
  `pgvector/pgvector:pg16` server image)
- `tar` (or equivalent archive tool)
- Enough disk space for one dump + one storage archive

## Backup

```bash
# 1. Database (custom format — supports parallel/selective restore)
pg_dump -h <host> -p <port> -U meyar -d meyar -Fc -f meyar-backup.dump

# 2. Document storage
tar -C "$MEYAR_STORAGE_ROOT" -cf meyar-storage-backup.tar .
```

Take both in the same maintenance window; MEYAR does not currently
provide a point-in-time-consistent combined snapshot mechanism, so avoid
taking the storage archive while an upload is actively in flight.

Store `meyar-backup.dump` and `meyar-storage-backup.tar` together,
labeled with the same timestamp. Neither file contains an API-key
plaintext (only `key_hash`, per `docs/SECURITY_PRIVACY.md`) — but both
still contain tenant-scoped operational data and must be handled with
the same access control as production data.

## Restore

**Restore into an isolated destination — never overwrite a live
database or the live storage root.**

```bash
# 1. Create/point at an empty destination database, then:
pg_restore -h <dest-host> -p <dest-port> -U meyar -d meyar --no-owner meyar-backup.dump

# 2. Extract storage into an empty destination storage root:
mkdir -p /path/to/dest-storage
tar -C /path/to/dest-storage -xf meyar-storage-backup.tar

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
- API-key secrets are never at risk from a backup — only the SHA-256
  hash is ever persisted (`docs/SECURITY_PRIVACY.md`); a restored
  environment cannot be used to recover a plaintext key.
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
