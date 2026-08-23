import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import pytest

from meyar.schemas.candidate_profile import EvidenceRef
from meyar.schemas.evaluation import CriterionResult
from meyar.scoring.policy import ScoringPolicyError, criterion_weight, score_results

AS_OF = date(2026, 1, 1)
PROFILE_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
CRITERIA_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
EVALUATION_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")


def _result(
    criterion_id: str,
    status: str,
    *,
    weight: float = 1.0,
    criterion_type: str = "PREFERRED",
    reason_code: str = "TEST_REASON",
    manual_review_required: bool = False,
) -> CriterionResult:
    return CriterionResult(
        criterion_id=criterion_id,
        kind="SKILL",
        type=criterion_type,
        configured_weight=weight,
        status=status,
        reason_code=reason_code,
        explanation="deterministic test explanation",
        manual_review_required=manual_review_required,
    )


def _score(results: list[CriterionResult]):
    return score_results(
        results,
        fit_band="POTENTIAL_MATCH",
        evaluation_id=EVALUATION_ID,
        candidate_profile_version_id=PROFILE_ID,
        job_criteria_version_id=CRITERIA_ID,
        evaluation_as_of_date=AS_OF,
        evaluation_policy_version="meyar-policy-v1",
    )


@pytest.mark.parametrize(
    ("status", "expected_factor", "expected_score"),
    [
        ("MATCH", "1.0", Decimal("100.00")),
        ("PARTIAL_MATCH", "0.5", Decimal("50.00")),
        ("NOT_MATCHED", "0.0", Decimal("0.00")),
        ("UNKNOWN", "0.0", Decimal("0.00")),
        ("CONFLICTING_EVIDENCE", "0.0", Decimal("0.00")),
        ("MANUAL_REVIEW_REQUIRED", "0.0", Decimal("0.00")),
    ],
)
def test_exact_status_factors(status: str, expected_factor: str, expected_score: Decimal) -> None:
    score, explanation = _score([_result("criterion", status)])
    assert score == expected_score
    assert explanation.criteria[0].factor == expected_factor


def test_all_match_and_all_not_matched_boundaries() -> None:
    assert _score([_result("a", "MATCH"), _result("b", "MATCH")])[0] == Decimal("100.00")
    assert _score([_result("a", "NOT_MATCHED"), _result("b", "NOT_MATCHED")])[0] == Decimal(
        "0.00"
    )


def test_unequal_declared_weights_produce_seventy_five() -> None:
    score, explanation = _score(
        [_result("a", "MATCH", weight=3), _result("b", "NOT_MATCHED", weight=1)]
    )
    assert score == Decimal("75.00")
    assert explanation.total_weight == "4.0"
    assert explanation.total_weighted_points == "3.00"


def test_repeating_fraction_rounds_only_final_score_half_up() -> None:
    score, explanation = _score(
        [_result("a", "MATCH"), _result("b", "MATCH"), _result("c", "NOT_MATCHED")]
    )
    assert score == Decimal("66.67")
    recomputed = (
        Decimal("100")
        * Decimal(explanation.total_weighted_points)
        / Decimal(explanation.total_weight)
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert recomputed == score


def test_half_cent_boundary_uses_round_half_up() -> None:
    results = [_result("matched", "MATCH")] + [
        _result(f"missed_{index}", "NOT_MATCHED") for index in range(31)
    ]
    assert _score(results)[0] == Decimal("3.13")


def test_float_conversion_is_textual_not_binary_artifact() -> None:
    assert criterion_weight(0.1) == Decimal("0.1")
    score, explanation = _score(
        [_result("a", "MATCH", weight=0.1), _result("b", "NOT_MATCHED", weight=0.2)]
    )
    assert score == Decimal("33.33")
    assert explanation.criteria[0].weight == "0.1"
    assert explanation.criteria[0].weighted_points == "0.10"


def test_zero_total_weight_fails_with_typed_error() -> None:
    with pytest.raises(ScoringPolicyError) as exc_info:
        _score([_result("a", "MATCH", weight=0), _result("b", "UNKNOWN", weight=0)])
    assert exc_info.value.code == "ZERO_TOTAL_CRITERION_WEIGHT"


def test_unknown_future_status_fails_instead_of_defaulting() -> None:
    with pytest.raises(ScoringPolicyError) as exc_info:
        _score([_result("future", "FUTURE_STATUS")])
    assert exc_info.value.code == "UNKNOWN_CRITERION_STATUS"


def test_unknown_and_not_matched_are_distinct_in_explanation() -> None:
    _score_value, explanation = _score(
        [
            _result("unknown", "UNKNOWN", reason_code="SKILL_NOT_FOUND_IN_PROFILE"),
            _result("no", "NOT_MATCHED", reason_code="EXPERIENCE_DURATION_INSUFFICIENT"),
        ]
    )
    by_id = {item.criterion_id: item for item in explanation.criteria}
    assert by_id["unknown"].status_meaning == "INSUFFICIENT_EVIDENCE_TO_DETERMINE"
    assert by_id["no"].status_meaning == "EVIDENCE_SUPPORTS_NON_MATCH"


def test_explanation_recomputes_score_and_contains_no_evidence_quote() -> None:
    result = _result("a", "MATCH", weight=2.5)
    result.evidence = [EvidenceRef(page=2, block_index=4, quote="synthetic quote")]
    score, explanation = _score([result, _result("b", "PARTIAL_MATCH", weight=1.5)])
    payload = explanation.model_dump(mode="json")
    recomputed = (
        Decimal("100")
        * Decimal(payload["total_weighted_points"])
        / Decimal(payload["total_weight"])
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert recomputed == score == Decimal(payload["numeric_score"])
    assert "synthetic quote" not in str(payload)
    assert payload["criteria"][0]["evidence_references"] == [{"page": 2, "block_index": 4}]
