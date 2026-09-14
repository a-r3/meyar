import uuid
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.extraction.evidence import EvidenceValidationError, verify_identity_evidence
from meyar.extraction.view import build_identity_document_view
from meyar.models.candidate_identity_version import (
    IDENTITY_STATUS_COMPLETED,
    CandidateIdentityVersion,
)
from meyar.models.canonical_document import CanonicalDocument
from meyar.schemas.candidate_identity import CandidateIdentityExtraction
from meyar.services.candidate_identity_repo import get_current_identity_version


@dataclass(frozen=True)
class IdentityValues:
    full_name: str | None
    email: str | None
    phone: str | None


EMPTY_IDENTITY_VALUES = IdentityValues(full_name=None, email=None, phone=None)


async def authorize_identity_version(
    db: AsyncSession, *, version: CandidateIdentityVersion
) -> CandidateIdentityExtraction:
    """Return identity only if its values pass current evidence checks."""
    if version.status != IDENTITY_STATUS_COMPLETED or version.identity_content is None:
        raise EvidenceValidationError(
            "INSUFFICIENT_IDENTITY_DATA",
            "Candidate identity version has no completed structured data.",
        )
    try:
        identity = CandidateIdentityExtraction.model_validate(version.identity_content)
    except ValidationError as exc:
        raise EvidenceValidationError("IDENTITY_SCHEMA_UNSUPPORTED", str(exc)) from exc
    canonical = await db.scalar(
        select(CanonicalDocument).where(
            CanonicalDocument.id == version.canonical_document_id,
            CanonicalDocument.tenant_id == version.tenant_id,
            CanonicalDocument.candidate_document_id == version.candidate_document_id,
        )
    )
    if canonical is None:
        raise EvidenceValidationError(
            "IDENTITY_CANONICAL_DOCUMENT_NOT_FOUND",
            "Candidate identity version cannot be tied to its canonical document.",
        )
    try:
        view = build_identity_document_view(canonical)
        verify_identity_evidence(view, identity)
    except (ValidationError, KeyError, TypeError, AttributeError) as exc:
        raise EvidenceValidationError(
            "IDENTITY_CANONICAL_CONTENT_UNSUPPORTED", str(exc)
        ) from exc
    return identity


async def identity_values_from_version(
    db: AsyncSession, *, version: CandidateIdentityVersion | None
) -> IdentityValues:
    if version is None:
        return EMPTY_IDENTITY_VALUES
    try:
        identity = await authorize_identity_version(db, version=version)
    except EvidenceValidationError:
        return EMPTY_IDENTITY_VALUES
    return IdentityValues(
        full_name=identity.full_name.value if identity.full_name else None,
        email=identity.email.value if identity.email else None,
        phone=identity.phone.value if identity.phone else None,
    )


async def get_current_identity_values(
    db: AsyncSession, *, tenant_id: uuid.UUID, candidate_id: uuid.UUID
) -> IdentityValues:
    version = await get_current_identity_version(db, tenant_id=tenant_id, candidate_id=candidate_id)
    return await identity_values_from_version(db, version=version)
