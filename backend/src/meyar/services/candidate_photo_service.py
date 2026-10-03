"""Best-effort post-ingestion photo pass and presentation-only authority."""

import asyncio
import base64
import hashlib
import json
import logging
import sys
import uuid
from io import BytesIO

from PIL import Image, ImageDraw
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.extraction.evidence import EvidenceValidationError
from meyar.models.candidate_document import CandidateDocument
from meyar.models.candidate_photo_version import (
    PHOTO_AVAILABLE,
    PHOTO_EXTRACTOR_VERSION,
    CandidatePhotoVersion,
)
from meyar.photo import policy
from meyar.services.candidate_document_repo import get_candidate_document
from meyar.services.candidate_identity_repo import get_current_identity_version
from meyar.services.candidate_photo_repo import create_photo_version, get_photo_for_document
from meyar.services.candidate_repo import get_candidate
from meyar.services.identity_authority import authorize_identity_version
from meyar.services.storage_recovery import (
    StorageCompensationError,
    recover_on_failure,
    track_created,
)
from meyar.services.tenant_authority import TenantInactiveError, require_active_tenant
from meyar.storage.base import DocumentStorage
from meyar.storage.photo import LocalPhotoStorage

logger = logging.getLogger(__name__)


def _placeholder() -> bytes:
    image = Image.new("RGB", (256, 256), (231, 236, 240))
    draw = ImageDraw.Draw(image)
    draw.ellipse((90, 52, 166, 128), fill=(151, 163, 173))
    draw.ellipse((54, 140, 202, 278), fill=(151, 163, 173))
    output = BytesIO()
    image.save(output, format="JPEG", quality=82, optimize=False, progressive=False, subsampling=0)
    return output.getvalue()


PLACEHOLDER_JPEG = _placeholder()


async def _extract_isolated(data: bytes, kind: str) -> dict:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "meyar.photo.worker",
        kind,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(
            process.communicate(data), timeout=policy.WORKER_TIMEOUT_SECONDS
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        return {"status": "EXTRACTION_FAILED", "reason_code": "WORKER_TIMEOUT"}
    if process.returncode != 0 or len(stdout) > policy.MAX_DERIVED_BYTES * 2 + 4096:
        return {"status": "EXTRACTION_FAILED", "reason_code": "WORKER_FAILED"}
    try:
        outcome = json.loads(stdout)
        if outcome["status"] not in (
            "AVAILABLE",
            "NO_PHOTO",
            "AMBIGUOUS",
            "UNUSABLE",
            "EXTRACTION_FAILED",
        ):
            raise ValueError("Invalid worker outcome")
        return outcome
    except (ValueError, KeyError, TypeError):
        return {"status": "EXTRACTION_FAILED", "reason_code": "WORKER_INVALID_OUTPUT"}


async def process_photo_for_document(
    db: AsyncSession,
    document_storage: DocumentStorage,
    photo_storage: LocalPhotoStorage,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    document_id: uuid.UUID,
) -> CandidatePhotoVersion | None:
    """Call only after the original CandidateDocument transaction commits.

    A terminal result is reused for this exact document/extractor. Failure
    never changes the original CV or professional processing state.

    Isolated extraction holds no DB lock. Persistence is a short
    fresh phase: Tenant SHARE -> Candidate SHARE -> exact document revalidation,
    held through derived save, row insert and commit, so candidate hard-delete
    (Candidate UPDATE) either completes first (nothing is saved) or waits and
    then enumerates the durable derived asset (issue #46 S6). Returns None when
    the document/candidate no longer exists.
    """
    await require_active_tenant(db, tenant_id)
    try:
        document = await get_candidate_document(
            db, tenant_id=tenant_id, candidate_id=candidate_id, document_id=document_id
        )
        if document is None:
            return None
        existing = await get_photo_for_document(
            db,
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            document_id=document_id,
            extractor_version=PHOTO_EXTRACTOR_VERSION,
        )
        if existing is not None:
            return existing
        storage_key, mime_type = document.storage_key, document.mime_type
        # The read-only transaction so far holds no row locks (READ COMMITTED);
        # it is not rolled back here so callers' loaded ORM state stays valid.
        original = await document_storage.read(storage_key=storage_key)
        kind = "PDF" if mime_type == "application/pdf" else "DOCX"
        outcome = await _extract_isolated(original, kind)
        jpeg: bytes | None = None
        if outcome["status"] == PHOTO_AVAILABLE:
            jpeg = base64.b64decode(outcome.pop("jpeg_base64"), validate=True)
            if (
                len(jpeg) > policy.MAX_DERIVED_BYTES
                or hashlib.sha256(jpeg).hexdigest() != outcome["derived_sha256"]
            ):
                raise ValueError("Invalid sanitized output")
        async with recover_on_failure(db):
            await require_active_tenant(db, tenant_id, lock=True)
            if await get_candidate(
                db, tenant_id=tenant_id, candidate_id=candidate_id, share=True
            ) is None or await get_candidate_document(
                db, tenant_id=tenant_id, candidate_id=candidate_id, document_id=document_id
            ) is None:
                await db.rollback()
                return None  # deletion won; no derived asset was written
            existing = await get_photo_for_document(
                db,
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                document_id=document_id,
                extractor_version=PHOTO_EXTRACTOR_VERSION,
            )
            if existing is not None:
                await db.rollback()
                return existing
            derived_key: str | None = None
            if jpeg is not None:
                derived_key = await photo_storage.save(tenant_id=tenant_id, content=jpeg)
                track_created(
                    db, photo_storage, tenant_id=tenant_id, storage_key=derived_key,
                    namespace="photo",
                )
            row = await create_photo_version(
                db,
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                document_id=document_id,
                extractor_version=PHOTO_EXTRACTOR_VERSION,
                outcome=outcome,
                derived_storage_key=derived_key,
            )
            await db.commit()
            return row
    except Exception as exc:
        # recover_on_failure already rolled back and removed any unreferenced
        # derived asset against fresh DB truth (never after a durable commit).
        if isinstance(exc, TenantInactiveError):
            raise
        if isinstance(exc, StorageCompensationError):
            logger.warning("Photo derived-asset compensation incomplete")
            return None
        logger.warning("Photo processing failed for a stored candidate document")
        try:
            await db.rollback()
        except Exception:
            logger.warning("Photo transaction rollback failed")
        try:
            await require_active_tenant(db, tenant_id)
            row = await create_photo_version(
                db,
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                document_id=document_id,
                extractor_version=PHOTO_EXTRACTOR_VERSION,
                outcome={"status": "EXTRACTION_FAILED", "reason_code": "PROCESSING_FAILED"},
                derived_storage_key=None,
            )
            await db.commit()
            return row
        except TenantInactiveError:
            raise
        except Exception:
            await db.rollback()
            logger.warning("Could not persist terminal photo failure")
            return None


async def current_presentable_photo(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
) -> CandidatePhotoVersion | None:
    newest = await db.scalar(
        select(CandidateDocument)
        .where(
            CandidateDocument.tenant_id == tenant_id,
            CandidateDocument.candidate_id == candidate_id,
        )
        .order_by(CandidateDocument.created_at.desc(), CandidateDocument.id.desc())
        .limit(1)
    )
    if newest is None:
        return None
    identity = await get_current_identity_version(
        db, tenant_id=tenant_id, candidate_id=candidate_id
    )
    if identity is None or identity.candidate_document_id != newest.id:
        return None
    try:
        await authorize_identity_version(db, version=identity)
    except EvidenceValidationError:
        return None
    photo = await get_photo_for_document(
        db,
        tenant_id=tenant_id,
        candidate_id=candidate_id,
        document_id=newest.id,
        extractor_version=PHOTO_EXTRACTOR_VERSION,
    )
    return photo if photo is not None and photo.status == PHOTO_AVAILABLE else None
