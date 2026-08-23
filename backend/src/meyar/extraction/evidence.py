import re

from meyar.extraction.view import ProfessionalDocumentView
from meyar.schemas.candidate_identity import CandidateIdentityExtraction
from meyar.schemas.candidate_profile import CandidateProfileExtraction, EvidenceRef


class EvidenceValidationError(Exception):
    """Raised when a model-supplied evidence reference cannot be verified
    against the real ProfessionalDocumentView. Never bypassed — an
    extraction with any unsupported evidence reference must not be
    persisted as a successful CandidateProfileVersion. See
    docs/SECURITY_PRIVACY.md."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def verify_evidence(view: ProfessionalDocumentView, evidence: EvidenceRef) -> None:
    block = next(
        (
            b
            for b in view.blocks
            if b.page == evidence.page and b.block_index == evidence.block_index
        ),
        None,
    )
    if block is None:
        raise EvidenceValidationError(
            "EVIDENCE_INVALID",
            f"Evidence references a nonexistent page/block: page={evidence.page} "
            f"block_index={evidence.block_index}.",
        )
    if _normalize(evidence.quote) not in _normalize(block.text):
        raise EvidenceValidationError(
            "EVIDENCE_INVALID",
            f"Evidence quote not found verbatim in page={evidence.page} "
            f"block_index={evidence.block_index}.",
        )


def verify_extraction_evidence(
    view: ProfessionalDocumentView, extraction: CandidateProfileExtraction
) -> None:
    """Re-verifies every evidence reference in extraction against view —
    the exact ProfessionalDocumentView rebuilt from the CanonicalDocument
    this extraction claims to be about. A reference to another document's
    content cannot pass, because view is always rebuilt from THIS
    document's own canonical rows, never trusted from model output."""
    categories = (
        extraction.skills,
        extraction.employment_history,
        extraction.education,
        extraction.certifications,
        extraction.languages,
        extraction.projects,
    )
    for category in categories:
        for item in category:
            for ref in item.evidence:
                verify_evidence(view, ref)


def verify_identity_evidence(
    view: ProfessionalDocumentView, extraction: CandidateIdentityExtraction
) -> None:
    """Same re-verification discipline as verify_extraction_evidence, for
    the identity schema's three optional fields. view here must be the
    unredacted view from build_identity_document_view — never the
    redacted professional one, which would fail every real match."""
    for field in (extraction.full_name, extraction.email, extraction.phone):
        if field is None:
            continue
        for ref in field.evidence:
            verify_evidence(view, ref)
