import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from meyar.diagnostics import exception_type
from meyar.extraction.deferral import defer_extraction
from meyar.extraction.evidence import EvidenceValidationError, verify_extraction_evidence
from meyar.extraction.prompts import PROMPT_VERSION
from meyar.extraction.view import ProfessionalDocumentView, build_professional_document_view
from meyar.llm.provider import (
    InferenceBusyError,
    LLMProvider,
    ModelSchemaInvalidError,
    ModelTimeoutError,
    ModelUnavailableError,
)
from meyar.models.candidate_document import CandidateDocument
from meyar.models.candidate_profile_version import (
    PROFILE_STATUS_COMPLETED,
    PROFILE_STATUS_FAILED,
    PROFILE_STATUS_MANUAL_REVIEW_REQUIRED,
    SCHEMA_VERSION,
    CandidateProfileVersion,
)
from meyar.schemas.candidate_profile import CandidateProfileExtraction
from meyar.services.audit_repo import record_event
from meyar.services.candidate_document_repo import get_latest_canonical_document
from meyar.services.candidate_profile_repo import create_profile_version
from meyar.services.tenant_authority import require_active_tenant

# One initial attempt + one bounded retry on schema-invalid structured
# output. Never unbounded — see Slice 4 spec §5.
MAX_MODEL_ATTEMPTS = 2


class ExtractionPreconditionError(Exception):
    """Raised when extraction cannot even be attempted (e.g. no parsed
    canonical document yet) — nothing to version, so no
    CandidateProfileVersion row is created for this case."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ProfileOutcome:
    """Closed, DB-free result of local inference (issue #46 S9).

    ``kind`` is a profile status (COMPLETED / FAILED / MANUAL_REVIEW_REQUIRED) or
    ``DEFERRED`` (admission refused: no attempt happened, nothing is versioned).
    """

    kind: str
    model_name: str = "n/a"
    extraction: CandidateProfileExtraction | None = None
    error_code: str = ""
    error_message: str = ""
    defer_reason: str = ""


DEFERRED = "DEFERRED"


async def load_profile_view(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    candidate_document: CandidateDocument,
) -> tuple[ProfessionalDocumentView, uuid.UUID]:
    """Short DB phase: STARTED audit + canonical document -> model-input view."""
    await require_active_tenant(db, tenant_id)
    await record_event(
        db,
        tenant_id=tenant_id,
        event_type="CANDIDATE_PROFILE_EXTRACTION_STARTED",
        metadata={"candidate_id": str(candidate_id), "document_id": str(candidate_document.id)},
    )

    canonical = await get_latest_canonical_document(
        db, tenant_id=tenant_id, candidate_document_id=candidate_document.id
    )
    if canonical is None:
        await record_event(
            db,
            tenant_id=tenant_id,
            event_type="CANDIDATE_PROFILE_EXTRACTION_FAILED",
            metadata={
                "candidate_id": str(candidate_id),
                "document_id": str(candidate_document.id),
                "error_code": "UNSUPPORTED_CANONICAL_DOCUMENT",
            },
        )
        raise ExtractionPreconditionError(
            "UNSUPPORTED_CANONICAL_DOCUMENT",
            "No parsed canonical document is available for this candidate document.",
        )
    return build_professional_document_view(canonical), canonical.id


async def infer_candidate_profile(
    llm: LLMProvider,
    view: ProfessionalDocumentView,
    *,
    max_input_chars: int,
    before_attempt: Callable[[], Awaitable[None]] | None = None,
) -> ProfileOutcome:
    """Local inference + evidence verification. Touches NO database: the staged
    folder pipeline calls this with no SQL transaction, pooled connection or row
    lock held. ``before_attempt`` lets a transactional caller re-check tenant
    authority before each model attempt."""
    if view.total_chars() > max_input_chars:
        return ProfileOutcome(
            kind=PROFILE_STATUS_MANUAL_REVIEW_REQUIRED,
            error_code="INPUT_TOO_LARGE",
            error_message=(
                f"Document text ({view.total_chars()} chars) exceeds the configured "
                f"maximum inference input size ({max_input_chars} chars)."
            ),
        )

    last_error_code = "MODEL_SCHEMA_INVALID"
    last_error_message = "Model output failed schema validation."
    for _attempt in range(MAX_MODEL_ATTEMPTS):
        if before_attempt is not None:
            await before_attempt()
        try:
            extraction, model_name = await llm.extract_candidate_profile(view)
        except InferenceBusyError as exc:
            # Issue #85: admission refused -> no model attempt happened.
            # Never persist a fake FAILED version (it would supersede the
            # current accepted one); defer to a later run instead.
            return ProfileOutcome(kind=DEFERRED, defer_reason=exc.reason)
        except ModelUnavailableError as exc:
            return ProfileOutcome(
                kind=PROFILE_STATUS_FAILED,
                error_code="MODEL_UNAVAILABLE",
                error_message=exception_type(exc),
            )
        except ModelTimeoutError as exc:
            return ProfileOutcome(
                kind=PROFILE_STATUS_FAILED,
                error_code="MODEL_TIMEOUT",
                error_message=exception_type(exc),
            )
        except ModelSchemaInvalidError as exc:
            last_error_code, last_error_message = "MODEL_SCHEMA_INVALID", exception_type(exc)
            continue

        try:
            verify_extraction_evidence(view, extraction)
        except EvidenceValidationError as exc:
            return ProfileOutcome(
                kind=PROFILE_STATUS_FAILED,
                model_name=model_name,
                error_code=exc.code,
                error_message=exception_type(exc),
            )
        return ProfileOutcome(
            kind=PROFILE_STATUS_COMPLETED, model_name=model_name, extraction=extraction
        )

    return ProfileOutcome(
        kind=PROFILE_STATUS_FAILED,
        error_code=last_error_code,
        error_message=last_error_message,
    )


async def persist_profile_outcome(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    candidate_document: CandidateDocument,
    canonical_document_id: uuid.UUID,
    model_provider_name: str,
    outcome: ProfileOutcome,
) -> CandidateProfileVersion:
    """Short DB phase: the immutable version (or the typed deferral) for an outcome.
    Raises ExtractionDeferredError for a DEFERRED outcome after its audit event."""
    if outcome.kind == DEFERRED:
        raise await defer_extraction(
            db,
            tenant_id=tenant_id,
            event_type="CANDIDATE_PROFILE_EXTRACTION_DEFERRED",
            candidate_id=candidate_id,
            document_id=candidate_document.id,
            reason=outcome.defer_reason,
        ) from None
    if outcome.kind != PROFILE_STATUS_COMPLETED or outcome.extraction is None:
        return await _persist_failure(
            db,
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            candidate_document=candidate_document,
            canonical_document_id=canonical_document_id,
            model_provider_name=model_provider_name,
            model_name=outcome.model_name,
            status=outcome.kind,
            error_code=outcome.error_code,
            error_message=outcome.error_message,
        )
    await require_active_tenant(db, tenant_id)
    version = await create_profile_version(
        db,
        tenant_id=tenant_id,
        candidate_id=candidate_id,
        candidate_document_id=candidate_document.id,
        canonical_document_id=canonical_document_id,
        source_sha256=candidate_document.sha256_hash,
        schema_version=SCHEMA_VERSION,
        prompt_version=PROMPT_VERSION,
        model_provider=model_provider_name,
        model_name=outcome.model_name,
        model_metadata={},
        status=PROFILE_STATUS_COMPLETED,
        profile_content=outcome.extraction.model_dump(mode="json"),
    )
    await record_event(
        db,
        tenant_id=tenant_id,
        event_type="CANDIDATE_PROFILE_EXTRACTED",
        metadata={
            "candidate_id": str(candidate_id),
            "profile_version_id": str(version.id),
            "version_number": version.version_number,
            "model": outcome.model_name,
        },
    )
    return version


async def extract_candidate_profile(
    db: AsyncSession,
    llm: LLMProvider,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    candidate_document: CandidateDocument,
    model_provider_name: str,
    max_input_chars: int,
) -> CandidateProfileVersion:
    """Runs the full Slice 4 pipeline synchronously: CanonicalDocument ->
    ProfessionalDocumentView -> LLMProvider -> Pydantic validation ->
    evidence verification -> immutable CandidateProfileVersion. Caller
    must have already verified candidate_document belongs to
    (tenant_id, candidate_id).

    One caller-owned transaction spans inference here (API/CLI use). The
    folder reconciliation pipeline instead composes load_profile_view ->
    infer_candidate_profile -> persist_profile_outcome in separate short
    transactions (issue #46 S9)."""
    view, canonical_id = await load_profile_view(
        db, tenant_id=tenant_id, candidate_id=candidate_id, candidate_document=candidate_document
    )
    outcome = await infer_candidate_profile(
        llm,
        view,
        max_input_chars=max_input_chars,
        before_attempt=lambda: require_active_tenant(db, tenant_id),
    )
    return await persist_profile_outcome(
        db,
        tenant_id=tenant_id,
        candidate_id=candidate_id,
        candidate_document=candidate_document,
        canonical_document_id=canonical_id,
        model_provider_name=model_provider_name,
        outcome=outcome,
    )


async def _persist_failure(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    candidate_document: CandidateDocument,
    canonical_document_id: uuid.UUID,
    model_provider_name: str,
    model_name: str,
    status: str,
    error_code: str,
    error_message: str,
) -> CandidateProfileVersion:
    await require_active_tenant(db, tenant_id)
    version = await create_profile_version(
        db,
        tenant_id=tenant_id,
        candidate_id=candidate_id,
        candidate_document_id=candidate_document.id,
        canonical_document_id=canonical_document_id,
        source_sha256=candidate_document.sha256_hash,
        schema_version=SCHEMA_VERSION,
        prompt_version=PROMPT_VERSION,
        model_provider=model_provider_name,
        model_name=model_name,
        model_metadata={},
        status=status,
        error_code=error_code,
        error_message=error_message[:500],
    )
    await record_event(
        db,
        tenant_id=tenant_id,
        event_type="CANDIDATE_PROFILE_EXTRACTION_FAILED",
        metadata={
            "candidate_id": str(candidate_id),
            "profile_version_id": str(version.id),
            "error_code": error_code,
        },
    )
    return version
