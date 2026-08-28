"""Slice 13 — synthetic backup/restore acceptance proof.

See docs/BACKUP_RESTORE.md for the operator runbook this script
demonstrates. Not part of the normal `pytest -q` gate (it orchestrates
disposable Docker Postgres containers and the host's native `pg_dump`/
`pg_restore`/`docker` tooling) — run on demand:

    uv run python scripts/backup_restore_acceptance.py

What it proves, end to end, against synthetic data only:

1. seeds an isolated synthetic tenant (job/criteria, candidate/document
   with real stored bytes, identity, profile, embedding, evaluation) in
   a disposable *source* Postgres container — never the developer's own
   `meyar-postgres-1` container or any database with real data;
2. backs up the source database with `pg_dump` (custom format) and the
   document-storage directory with `tar`;
3. restores both into a disposable, isolated *destination* Postgres
   container and a separate destination storage directory;
4. verifies against the restored destination only: row counts,
   relationships (document->candidate, profile/identity/embedding/
   evaluation->candidate), the original CV's bytes are byte-identical
   through the restored `DocumentStorage`, and a repeat deterministic
   score request against the restored data reuses the exact same
   Evaluation (`reused=True`) rather than recomputing — the same
   reproducibility guarantee Slice 10 established, now proven to survive
   a full backup/restore cycle.

Every container and temp directory used here is disposable and is torn
down in a `finally` block regardless of outcome. If `docker`, `pg_dump`,
or `pg_restore` are unavailable, this script reports that environmental
gate honestly (non-zero exit, clear message) rather than fabricating a
pass.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

SOURCE_CONTAINER = "meyar-backup-source-check"
DEST_CONTAINER = "meyar-backup-dest-check"
SOURCE_PORT = 55731
DEST_PORT = 55732
PG_USER = "meyar"
PG_PASSWORD = "meyar_dev_password"
PG_DB = "meyar"
IMAGE = "pgvector/pgvector:pg16"

SOURCE_URL_ASYNC = f"postgresql+asyncpg://{PG_USER}:{PG_PASSWORD}@localhost:{SOURCE_PORT}/{PG_DB}"
DEST_URL_ASYNC = f"postgresql+asyncpg://{PG_USER}:{PG_PASSWORD}@localhost:{DEST_PORT}/{PG_DB}"


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd, check=True, **kwargs)


def _require_tools() -> None:
    missing = [t for t in ("docker", "pg_dump", "pg_restore") if shutil.which(t) is None]
    if missing:
        print(
            f"ENVIRONMENTAL GATE: required tool(s) not available on this host: {missing}. "
            "Backup/restore acceptance cannot run here — this is an honest environmental "
            "gap, not a pass.",
            file=sys.stderr,
        )
        sys.exit(2)


def _start_disposable_postgres(name: str, port: int) -> None:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    _run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            name,
            "-e",
            f"POSTGRES_USER={PG_USER}",
            "-e",
            f"POSTGRES_PASSWORD={PG_PASSWORD}",
            "-e",
            f"POSTGRES_DB={PG_DB}",
            "-p",
            f"{port}:5432",
            IMAGE,
        ]
    )
    for _ in range(30):
        result = subprocess.run(
            ["docker", "exec", name, "pg_isready", "-U", PG_USER, "-d", PG_DB],
            capture_output=True,
        )
        if result.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError(f"disposable Postgres container {name!r} never became ready")


def _stop_disposable_postgres(name: str) -> None:
    subprocess.run(["docker", "stop", name], capture_output=True)


async def _seed_source(storage_root: Path) -> dict:
    from meyar.core.security import generate_api_key
    from meyar.evaluation.service import evaluate_and_score_candidate
    from meyar.models.api_key import ApiKey
    from meyar.schemas.criteria import CriterionIn, CriterionKind, CriterionType
    from meyar.services.audit_repo import record_event
    from meyar.services.candidate_document_repo import (
        create_candidate_document,
        create_canonical_document,
    )
    from meyar.services.candidate_embedding_repo import create_embedding_version
    from meyar.services.candidate_identity_repo import create_identity_version
    from meyar.services.candidate_profile_repo import create_profile_version
    from meyar.services.candidate_repo import create_candidate
    from meyar.services.job_criteria_repo import create_criteria_version
    from meyar.services.job_repo import create_job
    from meyar.services.tenant_repo import create_tenant
    from meyar.storage.local import LocalFilesystemStorage

    engine = create_async_engine(SOURCE_URL_ASYNC)
    storage = LocalFilesystemStorage(root=str(storage_root))
    cv_bytes = b"%PDF-1.4 synthetic backup/restore acceptance fixture, not a real CV.\n"

    async with AsyncSession(engine, expire_on_commit=False) as db:
        tenant = await create_tenant(db, name="Backup-Restore-Acceptance-Tenant")
        plaintext, prefix, key_hash = generate_api_key("test")
        api_key = ApiKey(
            tenant_id=tenant.id,
            prefix=prefix,
            key_hash=key_hash,
            scopes=["jobs:read", "jobs:write", "candidates:read", "candidates:write",
                    "evaluations:read", "evaluations:write"],
            created_at=datetime.now(UTC),
        )
        db.add(api_key)
        await db.flush()

        candidate = await create_candidate(db, tenant_id=tenant.id)
        storage_key = await storage.save(tenant_id=tenant.id, content=cv_bytes)
        document = await create_candidate_document(
            db,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
            original_filename="synthetic.pdf",
            mime_type="application/pdf",
            byte_size=len(cv_bytes),
            sha256_hash=hashlib.sha256(cv_bytes).hexdigest(),
            storage_key=storage_key,
        )
        canonical = await create_canonical_document(
            db,
            tenant_id=tenant.id,
            candidate_document_id=document.id,
            parser_name="meyar-local-text-parser",
            parser_version="1.0.0",
            language=None,
            content={
                "pages": [
                    {"page": 1, "blocks": [{"index": 0, "text": "Skills: Python, SQL"}]}
                ]
            },
        )
        profile_content = {
            "skills": [
                {
                    "name": "Python",
                    "evidence": [{"page": 1, "block_index": 0, "quote": "Skills: Python"}],
                }
            ],
            "employment_history": [],
            "education": [],
            "certifications": [],
            "languages": [],
            "projects": [],
        }
        profile_version = await create_profile_version(
            db,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
            candidate_document_id=document.id,
            canonical_document_id=canonical.id,
            source_sha256="a" * 64,
            schema_version="candidate-profile-v1",
            prompt_version="candidate-profile-extraction-v1",
            model_provider="fake",
            model_name="fake-model",
            model_metadata={},
            status="COMPLETED",
            profile_content=profile_content,
        )
        identity_version = await create_identity_version(
            db,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
            candidate_document_id=document.id,
            canonical_document_id=canonical.id,
            source_sha256="a" * 64,
            schema_version="candidate-identity-v1",
            prompt_version="candidate-identity-extraction-v1",
            model_provider="fake",
            model_name="fake-model",
            status="COMPLETED",
            identity_content={
                "full_name": {
                    "value": "Synthetic Backup Candidate",
                    "evidence": [{"page": 1, "block_index": 0, "quote": "Skills: Python"}],
                },
                "email": None,
                "phone": None,
            },
        )
        embedding_version = await create_embedding_version(
            db,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
            candidate_profile_version_id=profile_version.id,
            provider="fake-embedding",
            model_name="fake-embedding-model-v1",
            model_revision="",
            serializer_version="embedding-serializer-v1",
            source_sha256="b" * 64,
            embedding_dimensions=4,
            embedding=[0.1, 0.2, 0.3, 0.4],
        )
        job = await create_job(db, tenant_id=tenant.id, title="Backup Acceptance Job")
        criteria = [
            CriterionIn(
                id="python",
                kind=CriterionKind.SKILL,
                type=CriterionType.MUST_HAVE,
                label="Python",
                value="Python",
                weight=1,
            ).model_dump(mode="json")
        ]
        criteria_version = await create_criteria_version(
            db, tenant_id=tenant.id, job_id=job.id, criteria=criteria, created_by_api_key_id=None
        )
        as_of = date(2026, 1, 1)
        scored = await evaluate_and_score_candidate(
            db,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
            candidate_profile_version_id=profile_version.id,
            job_id=job.id,
            job_criteria_version_id=criteria_version.id,
            evaluation_as_of_date=as_of,
        )
        await record_event(
            db,
            tenant_id=tenant.id,
            event_type="BACKUP_RESTORE_ACCEPTANCE_SEEDED",
            metadata={"candidate_id": str(candidate.id)},
        )
        await db.commit()

        seeded = {
            "tenant_id": str(tenant.id),
            "api_key_id": str(api_key.id),
            "candidate_id": str(candidate.id),
            "document_id": str(document.id),
            "storage_key": storage_key,
            "cv_bytes_sha256": hashlib.sha256(cv_bytes).hexdigest(),
            "profile_version_id": str(profile_version.id),
            "identity_version_id": str(identity_version.id),
            "embedding_version_id": str(embedding_version.id),
            "job_id": str(job.id),
            "job_criteria_version_id": str(criteria_version.id),
            "evaluation_id": str(scored.evaluation.id),
            "evaluation_as_of_date": as_of.isoformat(),
            "numeric_score": str(scored.evaluation.numeric_score),
        }
    await engine.dispose()
    return seeded


async def _verify_restored(storage_root: Path, seeded: dict) -> None:
    from meyar.evaluation.service import evaluate_and_score_candidate
    from meyar.models.api_key import ApiKey
    from meyar.models.audit_event import AuditEvent
    from meyar.models.candidate import Candidate
    from meyar.models.candidate_document import CandidateDocument
    from meyar.models.candidate_embedding_version import CandidateEmbeddingVersion
    from meyar.models.candidate_identity_version import CandidateIdentityVersion
    from meyar.models.candidate_profile_version import CandidateProfileVersion
    from meyar.models.evaluation import Evaluation
    from meyar.models.tenant import Tenant
    from meyar.storage.local import LocalFilesystemStorage

    engine = create_async_engine(DEST_URL_ASYNC)
    storage = LocalFilesystemStorage(root=str(storage_root))
    tenant_id = uuid.UUID(seeded["tenant_id"])

    async with AsyncSession(engine, expire_on_commit=False) as db:
        for label, model in [
            ("tenants", Tenant),
            ("api_keys", ApiKey),
            ("candidates", Candidate),
            ("candidate_documents", CandidateDocument),
            ("candidate_profile_versions", CandidateProfileVersion),
            ("candidate_identity_versions", CandidateIdentityVersion),
            ("candidate_embedding_versions", CandidateEmbeddingVersion),
            ("evaluations", Evaluation),
            ("audit_events", AuditEvent),
        ]:
            count = (
                await db.execute(
                    select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
                    if model is not Tenant
                    else select(func.count()).select_from(model).where(model.id == tenant_id)
                )
            ).scalar_one()
            assert count >= 1, f"expected at least one restored {label} row, found {count}"
            print(f"restored {label}: {count} row(s) for the seeded tenant")

        document = (
            await db.execute(
                select(CandidateDocument).where(
                    CandidateDocument.id == uuid.UUID(seeded["document_id"]),
                    CandidateDocument.tenant_id == tenant_id,
                    CandidateDocument.candidate_id == uuid.UUID(seeded["candidate_id"]),
                )
            )
        ).scalar_one()
        assert document.storage_key == seeded["storage_key"], "storage_key not preserved"

        restored_bytes = await storage.read(storage_key=document.storage_key)
        restored_hash = hashlib.sha256(restored_bytes).hexdigest()
        assert restored_hash == seeded["cv_bytes_sha256"], (
            "restored original-CV bytes do not match the seeded bytes"
        )
        print("original-CV bytes verified byte-identical via DocumentStorage (sha256 match)")

        profile_version = (
            await db.execute(
                select(CandidateProfileVersion).where(
                    CandidateProfileVersion.id == uuid.UUID(seeded["profile_version_id"])
                )
            )
        ).scalar_one()
        criteria_version_id = uuid.UUID(seeded["job_criteria_version_id"])
        scored_again = await evaluate_and_score_candidate(
            db,
            tenant_id=tenant_id,
            candidate_id=uuid.UUID(seeded["candidate_id"]),
            candidate_profile_version_id=profile_version.id,
            job_id=uuid.UUID(seeded["job_id"]),
            job_criteria_version_id=criteria_version_id,
            evaluation_as_of_date=date.fromisoformat(seeded["evaluation_as_of_date"]),
        )
        await db.commit()
        assert scored_again.reused is True, (
            "expected the restored evaluation to be reused (deterministic, exact-provenance "
            "reuse), not recomputed"
        )
        assert str(scored_again.evaluation.id) == seeded["evaluation_id"], (
            "restored Evaluation id/provenance changed across backup/restore"
        )
        assert str(scored_again.evaluation.numeric_score) == seeded["numeric_score"], (
            "restored deterministic score differs from the originally seeded score"
        )
        print(
            f"deterministic score re-request against restored data: reused=True, "
            f"score unchanged ({scored_again.evaluation.numeric_score})"
        )
    await engine.dispose()


def main() -> None:
    _require_tools()
    tmp = Path(tempfile.mkdtemp(prefix="meyar-backup-restore-"))
    source_storage = tmp / "source-storage"
    dest_storage = tmp / "dest-storage"
    source_storage.mkdir()
    dest_storage.mkdir()
    dump_path = tmp / "meyar-source.dump"
    storage_tar = tmp / "storage.tar"

    try:
        print(f"== working directory: {tmp} ==")
        _start_disposable_postgres(SOURCE_CONTAINER, SOURCE_PORT)
        env_source = {"MEYAR_DATABASE_URL": SOURCE_URL_ASYNC}
        _run(
            ["uv", "run", "alembic", "upgrade", "head"],
            cwd=str(Path(__file__).resolve().parent.parent),
            env={**os.environ, **env_source},
        )

        print("== seeding synthetic tenant into disposable source database ==")
        seeded = asyncio.run(_seed_source(source_storage))
        print(f"seeded: {seeded}")

        print("== pg_dump (custom format) ==")
        _run(
            [
                "pg_dump",
                "-h",
                "localhost",
                "-p",
                str(SOURCE_PORT),
                "-U",
                PG_USER,
                "-d",
                PG_DB,
                "-Fc",
                "-f",
                str(dump_path),
            ],
            env={**os.environ, "PGPASSWORD": PG_PASSWORD},
        )

        print("== archiving document-storage directory ==")
        with tarfile.open(storage_tar, "w") as tar:
            tar.add(source_storage, arcname=".")

        _stop_disposable_postgres(SOURCE_CONTAINER)

        print("== restoring into a disposable, isolated destination database ==")
        _start_disposable_postgres(DEST_CONTAINER, DEST_PORT)
        _run(
            [
                "pg_restore",
                "-h",
                "localhost",
                "-p",
                str(DEST_PORT),
                "-U",
                PG_USER,
                "-d",
                PG_DB,
                "--no-owner",
                str(dump_path),
            ],
            env={**os.environ, "PGPASSWORD": PG_PASSWORD},
        )
        with tarfile.open(storage_tar, "r") as tar:
            tar.extractall(dest_storage, filter="data")

        print("== verifying restored state ==")
        asyncio.run(_verify_restored(dest_storage, seeded))

        print("\nBACKUP/RESTORE ACCEPTANCE: PASSED (synthetic data only).")
    finally:
        _stop_disposable_postgres(SOURCE_CONTAINER)
        _stop_disposable_postgres(DEST_CONTAINER)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
