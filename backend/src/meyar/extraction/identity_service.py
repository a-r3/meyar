import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from meyar.extraction.evidence import EvidenceValidationError, verify_identity_evidence
from meyar.extraction.identity_prompts import IDENTITY_PROMPT_VERSION
from meyar.extraction.view import build_identity_document_view
from meyar.llm.provider import (
    LLMProvider,
    ModelSchemaInvalidError,
    ModelTimeoutError,
    ModelUnavailableError,
)
from meyar.models.candidate_document import CandidateDocument
from meyar.models.candidate_identity_version import (
    IDENTITY_SCHEMA_VERSION,
    IDENTITY_STATUS_COMPLETED,
    IDENTITY_STATUS_FAILED,
    IDENTITY_STATUS_MANUAL_REVIEW_REQUIRED,
    CandidateIdentityVersion,
)
from meyar.services.audit_repo import record_event
from meyar.services.candidate_document_repo import get_latest_canonical_document
from meyar.services.candidate_identity_repo import create_identity_version

# One initial attempt + one bounded retry on schema-invalid structured
# output — same bound as candidate-profile extraction (Slice 4 spec §5).
MAX_MODEL_ATTEMPTS = 2


class IdentityExtractionPreconditionError(Exception):
    """Raised when identity extraction cannot even be attempted (e.g. no
    parsed canonical document yet) — nothing to version, so no
    CandidateIdentityVersion row is created for this case."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


async def extract_candidate_identity(
    db: AsyncSession,
    llm: LLMProvider,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    candidate_document: CandidateDocument,
    model_provider_name: str,
    max_input_chars: int,
) -> CandidateIdentityVersion:
    """Runs the identity-extraction pipeline: CanonicalDocument ->
    unredacted identity view -> LLMProvider -> Pydantic validation ->
    evidence verification -> immutable CandidateIdentityVersion. Mirrors
    meyar.extraction.service.extract_candidate_profile exactly, with two
    deliberate differences: the view is unredacted
    (build_identity_document_view) and the persisted content carries
    real PII, never logged or placed in audit metadata — only ids/status
    are. Caller must have already verified candidate_document belongs to
    (tenant_id, candidate_id)."""
    await record_event(
        db,
        tenant_id=tenant_id,
        event_type="CANDIDATE_IDENTITY_EXTRACTION_STARTED",
        metadata={"candidate_id": str(candidate_id), "document_id": str(candidate_document.id)},
    )

    canonical = await get_latest_canonical_document(
        db, tenant_id=tenant_id, candidate_document_id=candidate_document.id
    )
    if canonical is None:
        await record_event(
            db,
            tenant_id=tenant_id,
            event_type="CANDIDATE_IDENTITY_EXTRACTION_FAILED",
            metadata={
                "candidate_id": str(candidate_id),
                "document_id": str(candidate_document.id),
                "error_code": "UNSUPPORTED_CANONICAL_DOCUMENT",
            },
        )
        raise IdentityExtractionPreconditionError(
            "UNSUPPORTED_CANONICAL_DOCUMENT",
            "No parsed canonical document is available for this candidate document.",
        )

    view = build_identity_document_view(canonical)
    if view.total_chars() > max_input_chars:
        return await _persist_failure(
            db,
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            candidate_document=candidate_document,
            canonical_document_id=canonical.id,
            model_provider_name=model_provider_name,
            model_name="n/a",
            status=IDENTITY_STATUS_MANUAL_REVIEW_REQUIRED,
            error_code="INPUT_TOO_LARGE",
            error_message=(
                f"Document text ({view.total_chars()} chars) exceeds the configured "
                f"maximum inference input size ({max_input_chars} chars)."
            ),
        )

    last_error_code = "MODEL_SCHEMA_INVALID"
    last_error_message = "Model output failed schema validation."
    for _attempt in range(MAX_MODEL_ATTEMPTS):
        try:
            extraction, model_name = await llm.extract_candidate_identity(view)
        except ModelUnavailableError as exc:
            return await _persist_failure(
                db,
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                candidate_document=candidate_document,
                canonical_document_id=canonical.id,
                model_provider_name=model_provider_name,
                model_name="n/a",
                status=IDENTITY_STATUS_FAILED,
                error_code="MODEL_UNAVAILABLE",
                error_message=str(exc),
            )
        except ModelTimeoutError as exc:
            return await _persist_failure(
                db,
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                candidate_document=candidate_document,
                canonical_document_id=canonical.id,
                model_provider_name=model_provider_name,
                model_name="n/a",
                status=IDENTITY_STATUS_FAILED,
                error_code="MODEL_TIMEOUT",
                error_message=str(exc),
            )
        except ModelSchemaInvalidError as exc:
            last_error_code, last_error_message = "MODEL_SCHEMA_INVALID", str(exc)
            continue

        try:
            verify_identity_evidence(view, extraction)
        except EvidenceValidationError as exc:
            return await _persist_failure(
                db,
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                candidate_document=candidate_document,
                canonical_document_id=canonical.id,
                model_provider_name=model_provider_name,
                model_name=model_name,
                status=IDENTITY_STATUS_FAILED,
                error_code=exc.code,
                error_message=str(exc),
            )

        version = await create_identity_version(
            db,
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            candidate_document_id=candidate_document.id,
            canonical_document_id=canonical.id,
            source_sha256=candidate_document.sha256_hash,
            schema_version=IDENTITY_SCHEMA_VERSION,
            prompt_version=IDENTITY_PROMPT_VERSION,
            model_provider=model_provider_name,
            model_name=model_name,
            status=IDENTITY_STATUS_COMPLETED,
            identity_content=extraction.model_dump(mode="json"),
        )
        # PII-safe: ids/status/counts only — never the identity values.
        await record_event(
            db,
            tenant_id=tenant_id,
            event_type="CANDIDATE_IDENTITY_EXTRACTED",
            metadata={
                "candidate_id": str(candidate_id),
                "identity_version_id": str(version.id),
                "version_number": version.version_number,
                "model": model_name,
                "fields_found": sum(
                    1
                    for f in (extraction.full_name, extraction.email, extraction.phone)
                    if f is not None
                ),
            },
        )
        return version

    return await _persist_failure(
        db,
        tenant_id=tenant_id,
        candidate_id=candidate_id,
        candidate_document=candidate_document,
        canonical_document_id=canonical.id,
        model_provider_name=model_provider_name,
        model_name="n/a",
        status=IDENTITY_STATUS_FAILED,
        error_code=last_error_code,
        error_message=last_error_message,
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
) -> CandidateIdentityVersion:
    version = await create_identity_version(
        db,
        tenant_id=tenant_id,
        candidate_id=candidate_id,
        candidate_document_id=candidate_document.id,
        canonical_document_id=canonical_document_id,
        source_sha256=candidate_document.sha256_hash,
        schema_version=IDENTITY_SCHEMA_VERSION,
        prompt_version=IDENTITY_PROMPT_VERSION,
        model_provider=model_provider_name,
        model_name=model_name,
        status=status,
        error_code=error_code,
        error_message=error_message[:500],
    )
    await record_event(
        db,
        tenant_id=tenant_id,
        event_type="CANDIDATE_IDENTITY_EXTRACTION_FAILED",
        metadata={
            "candidate_id": str(candidate_id),
            "identity_version_id": str(version.id),
            "error_code": error_code,
        },
    )
    return version
