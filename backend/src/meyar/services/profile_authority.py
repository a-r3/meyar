import uuid

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.extraction.evidence import EvidenceValidationError, verify_extraction_evidence
from meyar.extraction.view import build_professional_document_view
from meyar.models.candidate_profile_version import (
    PROFILE_STATUS_COMPLETED,
    CandidateProfileVersion,
)
from meyar.models.canonical_document import CanonicalDocument
from meyar.schemas.candidate_profile import CandidateProfileExtraction
from meyar.services.candidate_profile_repo import (
    get_current_profile_version,
    get_profile_version_by_id,
)


class ProfileAuthorityError(Exception):
    """Current profile content is unavailable as professional fact authority."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


async def authorize_profile_version(
    db: AsyncSession, *, version: CandidateProfileVersion
) -> CandidateProfileExtraction:
    """Return a profile only if it passes the current evidence boundary.

    This is a read-time backstop for legacy ``COMPLETED`` rows. It does
    not mutate historical rows or change provenance; unsupported stored
    facts simply remain unavailable to search/evaluation/agent/UI
    consumers.
    """
    if version.status != PROFILE_STATUS_COMPLETED or version.profile_content is None:
        raise ProfileAuthorityError(
            "INSUFFICIENT_STRUCTURED_DATA",
            "Candidate profile version has no completed structured data.",
        )
    try:
        profile = CandidateProfileExtraction.model_validate(version.profile_content)
    except ValidationError as exc:
        raise ProfileAuthorityError("PROFILE_SCHEMA_UNSUPPORTED", str(exc)) from exc

    canonical = await db.scalar(
        select(CanonicalDocument).where(
            CanonicalDocument.id == version.canonical_document_id,
            CanonicalDocument.tenant_id == version.tenant_id,
            CanonicalDocument.candidate_document_id == version.candidate_document_id,
        )
    )
    if canonical is None:
        raise ProfileAuthorityError(
            "PROFILE_CANONICAL_DOCUMENT_NOT_FOUND",
            "Candidate profile version cannot be tied to its canonical document.",
        )
    try:
        view = build_professional_document_view(canonical)
        verify_extraction_evidence(view, profile)
    except EvidenceValidationError as exc:
        raise ProfileAuthorityError("PROFILE_EVIDENCE_UNSUPPORTED", str(exc)) from exc
    except (ValidationError, KeyError, TypeError, AttributeError) as exc:
        raise ProfileAuthorityError("PROFILE_CANONICAL_CONTENT_UNSUPPORTED", str(exc)) from exc
    return profile


async def get_current_authorized_profile(
    db: AsyncSession, *, tenant_id: uuid.UUID, candidate_id: uuid.UUID
) -> tuple[CandidateProfileVersion, CandidateProfileExtraction] | None:
    version = await get_current_profile_version(db, tenant_id=tenant_id, candidate_id=candidate_id)
    if version is None:
        return None
    try:
        return version, await authorize_profile_version(db, version=version)
    except ProfileAuthorityError:
        return None


async def get_authorized_profile_version_by_id(
    db: AsyncSession, *, tenant_id: uuid.UUID, profile_version_id: uuid.UUID
) -> tuple[CandidateProfileVersion, CandidateProfileExtraction] | None:
    version = await get_profile_version_by_id(
        db, tenant_id=tenant_id, profile_version_id=profile_version_id
    )
    if version is None:
        return None
    try:
        return version, await authorize_profile_version(db, version=version)
    except ProfileAuthorityError:
        return None
