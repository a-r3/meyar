from pydantic import BaseModel, Field

from meyar.schemas.candidate_profile import EvidenceRef

CRITERION_STATUS_MATCH = "MATCH"
CRITERION_STATUS_PARTIAL_MATCH = "PARTIAL_MATCH"
CRITERION_STATUS_NOT_MATCHED = "NOT_MATCHED"
CRITERION_STATUS_UNKNOWN = "UNKNOWN"
CRITERION_STATUS_CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
CRITERION_STATUS_MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"

OVERALL_STRONG_MATCH = "STRONG_MATCH"
OVERALL_POTENTIAL_MATCH = "POTENTIAL_MATCH"
OVERALL_INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
OVERALL_MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"


class CriterionResult(BaseModel):
    """One job criterion's deterministic evaluation outcome. `evidence` is
    always carried through unmodified from the already-validated
    CandidateProfileVersion — never new quotes invented at evaluation
    time. See docs/MASTER_SPEC.md (Slice 5 spec) §4/§13."""

    criterion_id: str
    kind: str
    type: str
    configured_weight: float
    status: str
    reason_code: str
    explanation: str
    evidence: list[EvidenceRef] = Field(default_factory=list)
    confidence: float | None = None
    manual_review_required: bool = False
