import uuid
from collections.abc import Sequence

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
    get_effective_profile_version,
    get_profile_version_by_id,
)


class ProfileAuthorityError(Exception):
    """Current profile content is unavailable as professional fact authority."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _require_completed_profile(version: CandidateProfileVersion) -> CandidateProfileExtraction:
    if version.status != PROFILE_STATUS_COMPLETED or version.profile_content is None:
        raise ProfileAuthorityError(
            "INSUFFICIENT_STRUCTURED_DATA",
            "Candidate profile version has no completed structured data.",
        )
    try:
        return CandidateProfileExtraction.model_validate(version.profile_content)
    except ValidationError as exc:
        raise ProfileAuthorityError("PROFILE_SCHEMA_UNSUPPORTED", str(exc)) from exc


def _verify_against_canonical(
    version: CandidateProfileVersion,
    profile: CandidateProfileExtraction,
    canonical: CanonicalDocument | None,
) -> CandidateProfileExtraction:
    """The ONE evidence-authority definition shared by the single-version
    and batch paths (issue #86): identical checks, identical error codes."""
    if (
        canonical is None
        or canonical.id != version.canonical_document_id
        or canonical.tenant_id != version.tenant_id
        or canonical.candidate_document_id != version.candidate_document_id
    ):
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


async def authorize_profile_version(
    db: AsyncSession, *, version: CandidateProfileVersion
) -> CandidateProfileExtraction:
    """Return a profile only if it passes the current evidence boundary.

    This is a read-time backstop for legacy ``COMPLETED`` rows. It does
    not mutate historical rows or change provenance; unsupported stored
    facts simply remain unavailable to search/evaluation/agent/UI
    consumers.
    """
    profile = _require_completed_profile(version)
    canonical = await db.scalar(
        select(CanonicalDocument).where(
            CanonicalDocument.id == version.canonical_document_id,
            CanonicalDocument.tenant_id == version.tenant_id,
            CanonicalDocument.candidate_document_id == version.candidate_document_id,
        )
    )
    return _verify_against_canonical(version, profile, canonical)


async def authorize_profile_versions(
    db: AsyncSession, *, tenant_id: uuid.UUID, versions: Sequence[CandidateProfileVersion]
) -> dict[uuid.UUID, CandidateProfileExtraction | ProfileAuthorityError]:
    """Batch form of ``authorize_profile_version`` (issue #86): EXACTLY the
    same authority semantics, but ONE bounded canonical-document query for
    all given versions instead of one query per version. Returns, per
    profile version id, either the authorized profile or the
    ``ProfileAuthorityError`` the single-version path would have raised.
    Callers bound ``versions`` (ResultSet members: <= MAX_SEARCH_LIMIT)."""
    outcomes: dict[uuid.UUID, CandidateProfileExtraction | ProfileAuthorityError] = {}
    pending: dict[uuid.UUID, tuple[CandidateProfileVersion, CandidateProfileExtraction]] = {}
    for version in versions:
        if version.tenant_id != tenant_id:
            outcomes[version.id] = ProfileAuthorityError(
                "PROFILE_CANONICAL_DOCUMENT_NOT_FOUND",
                "Candidate profile version cannot be tied to its canonical document.",
            )
            continue
        try:
            pending[version.id] = (version, _require_completed_profile(version))
        except ProfileAuthorityError as exc:
            outcomes[version.id] = exc
    canonical_ids = {version.canonical_document_id for version, _ in pending.values()}
    canonicals: dict[uuid.UUID, CanonicalDocument] = {}
    if canonical_ids:
        rows = await db.scalars(
            select(CanonicalDocument).where(
                CanonicalDocument.tenant_id == tenant_id,
                CanonicalDocument.id.in_(canonical_ids),
            )
        )
        canonicals = {row.id: row for row in rows}
    for version_id, (version, profile) in pending.items():
        try:
            outcomes[version_id] = _verify_against_canonical(
                version, profile, canonicals.get(version.canonical_document_id)
            )
        except ProfileAuthorityError as exc:
            outcomes[version_id] = exc
    return outcomes


async def get_current_authorized_profile(
    db: AsyncSession, *, tenant_id: uuid.UUID, candidate_id: uuid.UUID
) -> tuple[CandidateProfileVersion, CandidateProfileExtraction] | None:
    """Effective facts: newest COMPLETED, current evidence, fail closed (D-100)."""
    version = await get_effective_profile_version(
        db, tenant_id=tenant_id, candidate_id=candidate_id
    )
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
