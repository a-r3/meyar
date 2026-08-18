import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.models.candidate_document import CandidateDocument
from meyar.models.canonical_document import CanonicalDocument


async def create_candidate_document(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    original_filename: str,
    mime_type: str,
    byte_size: int,
    sha256_hash: str,
    storage_key: str,
) -> CandidateDocument:
    document = CandidateDocument(
        tenant_id=tenant_id,
        candidate_id=candidate_id,
        original_filename=original_filename,
        mime_type=mime_type,
        byte_size=byte_size,
        sha256_hash=sha256_hash,
        storage_key=storage_key,
    )
    db.add(document)
    await db.flush()
    return document


async def get_candidate_document(
    db: AsyncSession, *, tenant_id: uuid.UUID, candidate_id: uuid.UUID, document_id: uuid.UUID
) -> CandidateDocument | None:
    result = await db.execute(
        select(CandidateDocument).where(
            CandidateDocument.id == document_id,
            CandidateDocument.candidate_id == candidate_id,
            CandidateDocument.tenant_id == tenant_id,
        )
    )
    return result.scalar_one_or_none()


async def list_candidate_documents(
    db: AsyncSession, *, tenant_id: uuid.UUID, candidate_id: uuid.UUID
) -> list[CandidateDocument]:
    result = await db.execute(
        select(CandidateDocument)
        .where(
            CandidateDocument.candidate_id == candidate_id,
            CandidateDocument.tenant_id == tenant_id,
        )
        .order_by(CandidateDocument.created_at.asc())
    )
    return list(result.scalars().all())


async def create_canonical_document(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_document_id: uuid.UUID,
    parser_name: str,
    parser_version: str,
    language: str | None,
    content: dict,
) -> CanonicalDocument:
    canonical = CanonicalDocument(
        tenant_id=tenant_id,
        candidate_document_id=candidate_document_id,
        parser_name=parser_name,
        parser_version=parser_version,
        language=language,
        content=content,
    )
    db.add(canonical)
    await db.flush()
    return canonical


async def get_latest_canonical_document(
    db: AsyncSession, *, tenant_id: uuid.UUID, candidate_document_id: uuid.UUID
) -> CanonicalDocument | None:
    result = await db.execute(
        select(CanonicalDocument)
        .where(
            CanonicalDocument.candidate_document_id == candidate_document_id,
            CanonicalDocument.tenant_id == tenant_id,
        )
        .order_by(CanonicalDocument.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()
