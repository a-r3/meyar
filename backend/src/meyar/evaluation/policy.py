from meyar.schemas.criteria import CriterionType
from meyar.schemas.evaluation import (
    CRITERION_STATUS_CONFLICTING_EVIDENCE,
    CRITERION_STATUS_MANUAL_REVIEW_REQUIRED,
    CRITERION_STATUS_MATCH,
    CRITERION_STATUS_NOT_MATCHED,
    CRITERION_STATUS_PARTIAL_MATCH,
    CRITERION_STATUS_UNKNOWN,
    OVERALL_INSUFFICIENT_EVIDENCE,
    OVERALL_MANUAL_REVIEW_REQUIRED,
    OVERALL_POTENTIAL_MATCH,
    OVERALL_STRONG_MATCH,
    CriterionResult,
)

# Bump whenever the algorithm below materially changes — every Evaluation
# persists the exact version used, so results stay explainable/reproducible.
POLICY_ENGINE_VERSION = "meyar-policy-v3"

# A job with no PREFERRED criteria (or >=50% of them matched) and every
# MUST_HAVE criterion explicitly MATCH is a STRONG_MATCH; otherwise, with
# all must-haves satisfied but weaker preferred coverage, POTENTIAL_MATCH.
PREFERRED_MATCH_RATIO_FOR_STRONG = 0.5


def compute_overall_result(results: list[CriterionResult]) -> str:
    """Deterministic MVP fit-band algorithm (meyar-policy-v3):

    1. Any criterion MANUAL_REVIEW_REQUIRED or CONFLICTING_EVIDENCE
       -> overall MANUAL_REVIEW_REQUIRED (highest priority: a human must
       look regardless of everything else).
    2. Any MUST_HAVE criterion NOT_MATCHED, UNKNOWN, or PARTIAL_MATCH
       -> overall INSUFFICIENT_EVIDENCE. Missing/weak evidence on a
       must-have is never silently treated as disqualifying rejection —
       it just means there isn't yet enough evidence to call it a match.
    3. All MUST_HAVE criteria MATCH, no PREFERRED criteria configured
       -> STRONG_MATCH.
    4. All MUST_HAVE criteria MATCH, >=50% of PREFERRED criteria MATCH
       -> STRONG_MATCH; otherwise POTENTIAL_MATCH.

    No REJECT/HIRE/AUTO_* band exists — this is decision support only.
    """
    if any(
        r.status in (CRITERION_STATUS_MANUAL_REVIEW_REQUIRED, CRITERION_STATUS_CONFLICTING_EVIDENCE)
        for r in results
    ):
        return OVERALL_MANUAL_REVIEW_REQUIRED

    must_have = [r for r in results if r.type == CriterionType.MUST_HAVE.value]
    preferred = [r for r in results if r.type == CriterionType.PREFERRED.value]

    unsatisfied_must_have_statuses = (
        CRITERION_STATUS_NOT_MATCHED,
        CRITERION_STATUS_UNKNOWN,
        CRITERION_STATUS_PARTIAL_MATCH,
    )
    if any(r.status in unsatisfied_must_have_statuses for r in must_have):
        return OVERALL_INSUFFICIENT_EVIDENCE

    if not preferred:
        return OVERALL_STRONG_MATCH

    matched_preferred = sum(1 for r in preferred if r.status == CRITERION_STATUS_MATCH)
    ratio = matched_preferred / len(preferred)
    if ratio >= PREFERRED_MATCH_RATIO_FOR_STRONG:
        return OVERALL_STRONG_MATCH
    return OVERALL_POTENTIAL_MATCH
