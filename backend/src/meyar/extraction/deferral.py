"""Transient inference-admission deferral for extraction (issue #85, D-089).

When the process-wide local-inference gate refuses a call (``QUEUE_FULL`` /
``QUEUE_TIMEOUT``), NO model attempt happened and nothing about the
document failed. Minting an immutable FAILED ``CandidateProfileVersion`` /
``CandidateIdentityVersion`` for that would be a fake model failure, and —
because profile authority follows the highest version number — could even
supersede an accepted COMPLETED version. Extraction therefore DEFERS: no
version row is written, a bounded structural audit event is recorded, and the
caller receives this typed transient outcome to retry on a later run."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from meyar.services.audit_repo import record_event

INFERENCE_BUSY = "INFERENCE_BUSY"


class ExtractionDeferredError(Exception):
    """Typed transient outcome: extraction was not attempted because local
    inference capacity was busy. Retry later; nothing was versioned."""

    code = INFERENCE_BUSY

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Extraction deferred: local inference busy ({reason}).")


async def defer_extraction(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    event_type: str,
    candidate_id: uuid.UUID,
    document_id: uuid.UUID,
    reason: str,
) -> ExtractionDeferredError:
    """Audit (ids + closed codes only — never PII, prompt, or model output)
    and return the error for the caller to raise."""
    await record_event(
        db,
        tenant_id=tenant_id,
        event_type=event_type,
        metadata={
            "candidate_id": str(candidate_id),
            "document_id": str(document_id),
            "error_code": INFERENCE_BUSY,
            "reason_code": reason,
        },
    )
    return ExtractionDeferredError(reason)
