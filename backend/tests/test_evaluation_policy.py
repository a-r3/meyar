"""Deterministic unit tests for the evaluation engine — no DB, no LLM.
See Slice 5 spec §24: "The Evaluation Engine must be independently
testable without Ollama." """

from datetime import date

from meyar.evaluation.evaluators import evaluate_criterion as _evaluate_criterion
from meyar.evaluation.policy import compute_overall_result
from meyar.schemas.candidate_profile import (
    CandidateProfileExtraction,
    CertificationItem,
    EducationItem,
    EmploymentItem,
    EvidenceRef,
    LanguageItem,
    SkillItem,
)
from meyar.schemas.criteria import CriterionIn, CriterionKind, CriterionType
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
)

_EV = [EvidenceRef(page=1, block_index=0, quote="x")]
_AS_OF_DATE = date(2026, 1, 1)


def evaluate_criterion(criterion, profile):
    return _evaluate_criterion(criterion, profile, evaluation_as_of_date=_AS_OF_DATE)


def _criterion(**overrides) -> CriterionIn:
    defaults = {
        "id": "c1",
        "kind": CriterionKind.SKILL,
        "type": CriterionType.MUST_HAVE,
        "label": "Python",
        "value": "Python",
    }
    defaults.update(overrides)
    return CriterionIn(**defaults)


def _empty_profile(**overrides) -> CandidateProfileExtraction:
    return CandidateProfileExtraction(**overrides)


# --- SKILL ---------------------------------------------------------------


def test_skill_explicit_match() -> None:
    criterion = _criterion(id="python", value="Python")
    profile = _empty_profile(skills=[SkillItem(name="Python", evidence=_EV)])
    result = evaluate_criterion(criterion, profile)
    assert result.status == CRITERION_STATUS_MATCH
    assert result.evidence == _EV


def test_skill_absent_is_unknown_not_not_matched() -> None:
    criterion = _criterion(id="rust", value="Rust")
    profile = _empty_profile(skills=[SkillItem(name="Python", evidence=_EV)])
    result = evaluate_criterion(criterion, profile)
    assert result.status == CRITERION_STATUS_UNKNOWN


def test_skill_case_normalization() -> None:
    criterion = _criterion(id="python", value="python")
    profile = _empty_profile(skills=[SkillItem(name="PYTHON", evidence=_EV)])
    result = evaluate_criterion(criterion, profile)
    assert result.status == CRITERION_STATUS_MATCH


def test_java_does_not_match_javascript() -> None:
    criterion = _criterion(id="java", kind=CriterionKind.SKILL, value="Java")
    profile = _empty_profile(skills=[SkillItem(name="JavaScript", evidence=_EV)])
    result = evaluate_criterion(criterion, profile)
    assert result.status == CRITERION_STATUS_UNKNOWN


def test_skill_alias_normalization() -> None:
    criterion = _criterion(id="pg", value="postgres")
    profile = _empty_profile(skills=[SkillItem(name="PostgreSQL", evidence=_EV)])
    result = evaluate_criterion(criterion, profile)
    assert result.status == CRITERION_STATUS_MATCH


# --- CERTIFICATION ---------------------------------------------------------


_AWS_CERT = "AWS Certified Developer"


def test_certification_explicit_match() -> None:
    criterion = _criterion(id="aws", kind=CriterionKind.CERTIFICATION, value=_AWS_CERT)
    profile = _empty_profile(certifications=[CertificationItem(name=_AWS_CERT, evidence=_EV)])
    result = evaluate_criterion(criterion, profile)
    assert result.status == CRITERION_STATUS_MATCH


def test_certification_absent_is_unknown() -> None:
    criterion = _criterion(id="aws", kind=CriterionKind.CERTIFICATION, value=_AWS_CERT)
    profile = _empty_profile()
    result = evaluate_criterion(criterion, profile)
    assert result.status == CRITERION_STATUS_UNKNOWN


# --- EDUCATION -------------------------------------------------------------


def test_education_explicit_match() -> None:
    criterion = _criterion(id="cs_degree", kind=CriterionKind.EDUCATION, value="Computer Science")
    profile = _empty_profile(
        education=[EducationItem(field_of_study="Computer Science", evidence=_EV)]
    )
    result = evaluate_criterion(criterion, profile)
    assert result.status == CRITERION_STATUS_MATCH


def test_education_unsupported_is_unknown() -> None:
    criterion = _criterion(id="cs_degree", kind=CriterionKind.EDUCATION, value="Computer Science")
    profile = _empty_profile(education=[EducationItem(field_of_study="Biology", evidence=_EV)])
    result = evaluate_criterion(criterion, profile)
    assert result.status == CRITERION_STATUS_UNKNOWN


# --- LANGUAGE ---------------------------------------------------------------


def test_language_explicit_level_match() -> None:
    criterion = _criterion(
        id="english", kind=CriterionKind.LANGUAGE, value="English", required_level="C1"
    )
    profile = _empty_profile(
        languages=[LanguageItem(language="English", proficiency="C1", evidence=_EV)]
    )
    result = evaluate_criterion(criterion, profile)
    assert result.status == CRITERION_STATUS_MATCH


def test_language_present_without_required_level_is_partial() -> None:
    criterion = _criterion(
        id="english", kind=CriterionKind.LANGUAGE, value="English", required_level="C1"
    )
    profile = _empty_profile(languages=[LanguageItem(language="English", evidence=_EV)])
    result = evaluate_criterion(criterion, profile)
    assert result.status == CRITERION_STATUS_PARTIAL_MATCH


def test_language_absent_is_unknown() -> None:
    criterion = _criterion(id="german", kind=CriterionKind.LANGUAGE, value="German")
    profile = _empty_profile(languages=[LanguageItem(language="English", evidence=_EV)])
    result = evaluate_criterion(criterion, profile)
    assert result.status == CRITERION_STATUS_UNKNOWN


def test_language_no_level_required_matches_on_presence() -> None:
    criterion = _criterion(id="english", kind=CriterionKind.LANGUAGE, value="English")
    profile = _empty_profile(languages=[LanguageItem(language="English", evidence=_EV)])
    result = evaluate_criterion(criterion, profile)
    assert result.status == CRITERION_STATUS_MATCH


# --- EXPERIENCE --------------------------------------------------------------


def test_experience_sufficient_duration_matches() -> None:
    criterion = _criterion(id="exp", kind=CriterionKind.EXPERIENCE, value=None, min_years=3)
    entry = EmploymentItem(
        title="Backend Developer", start_date="2020", end_date="2024", evidence=_EV
    )
    profile = _empty_profile(employment_history=[entry])
    result = evaluate_criterion(criterion, profile)
    assert result.status == CRITERION_STATUS_MATCH


def test_experience_insufficient_duration_not_matched() -> None:
    criterion = _criterion(id="exp", kind=CriterionKind.EXPERIENCE, value=None, min_years=5)
    entry = EmploymentItem(
        title="Backend Developer", start_date="2022", end_date="2024", evidence=_EV
    )
    profile = _empty_profile(employment_history=[entry])
    result = evaluate_criterion(criterion, profile)
    assert result.status == CRITERION_STATUS_NOT_MATCHED


def test_experience_ambiguous_dates_require_manual_review() -> None:
    criterion = _criterion(id="exp", kind=CriterionKind.EXPERIENCE, value=None, min_years=3)
    entry = EmploymentItem(
        title="Backend Developer", start_date="a while ago", end_date="2024", evidence=_EV
    )
    profile = _empty_profile(employment_history=[entry])
    result = evaluate_criterion(criterion, profile)
    assert result.status == CRITERION_STATUS_MANUAL_REVIEW_REQUIRED


def test_experience_no_history_is_unknown() -> None:
    criterion = _criterion(id="exp", kind=CriterionKind.EXPERIENCE, value=None, min_years=3)
    profile = _empty_profile()
    result = evaluate_criterion(criterion, profile)
    assert result.status == CRITERION_STATUS_UNKNOWN


def test_experience_overlapping_dates_conflicting_evidence() -> None:
    criterion = _criterion(id="exp", kind=CriterionKind.EXPERIENCE, value=None, min_years=1)
    profile = _empty_profile(
        employment_history=[
            EmploymentItem(title="Job A", start_date="2019", end_date="2022", evidence=_EV),
            EmploymentItem(title="Job B", start_date="2020", end_date="2023", evidence=_EV),
        ]
    )
    result = evaluate_criterion(criterion, profile)
    # forced to MANUAL_REVIEW_REQUIRED by _finalize, but the underlying
    # conflict is preserved in the reason code.
    assert result.status == CRITERION_STATUS_MANUAL_REVIEW_REQUIRED
    assert result.reason_code == "EXPERIENCE_OVERLAPPING_DATES"
    assert result.manual_review_required is True


def test_experience_non_overlapping_entries_sum_correctly() -> None:
    criterion = _criterion(id="exp", kind=CriterionKind.EXPERIENCE, value=None, min_years=5)
    profile = _empty_profile(
        employment_history=[
            EmploymentItem(title="Job A", start_date="2015", end_date="2018", evidence=_EV),
            EmploymentItem(title="Job B", start_date="2019", end_date="2022", evidence=_EV),
        ]
    )
    result = evaluate_criterion(criterion, profile)
    assert result.status == CRITERION_STATUS_MATCH
    assert "6" in result.explanation  # 3 + 3 years


# --- manual_review_required config flag forces review regardless -----------


def test_configured_manual_review_flag_forces_review_even_on_match() -> None:
    criterion = _criterion(id="python", value="Python", manual_review_required=True)
    profile = _empty_profile(skills=[SkillItem(name="Python", evidence=_EV)])
    result = evaluate_criterion(criterion, profile)
    assert result.status == CRITERION_STATUS_MANUAL_REVIEW_REQUIRED
    assert result.manual_review_required is True


# --- evidence carry-through --------------------------------------------------


def test_evidence_is_carried_through_unmodified() -> None:
    specific_evidence = [EvidenceRef(page=3, block_index=7, quote="Backend Developer — Python")]
    criterion = _criterion(id="python", value="Python")
    profile = _empty_profile(skills=[SkillItem(name="Python", evidence=specific_evidence)])
    result = evaluate_criterion(criterion, profile)
    assert result.evidence == specific_evidence


# --- policy determinism across repeated identical input --------------------


def test_policy_is_deterministic_for_repeated_identical_input() -> None:
    criterion = _criterion(id="python", value="Python")
    profile = _empty_profile(skills=[SkillItem(name="Python", evidence=_EV)])
    first = evaluate_criterion(criterion, profile)
    second = evaluate_criterion(criterion, profile)
    assert first.model_dump() == second.model_dump()


# --- overall result / policy engine -----------------------------------------


def _result(**overrides):
    from meyar.schemas.evaluation import CriterionResult

    defaults = {
        "criterion_id": "c1",
        "kind": "SKILL",
        "type": "MUST_HAVE",
        "configured_weight": 1.0,
        "status": CRITERION_STATUS_MATCH,
        "reason_code": "X",
        "explanation": "x",
    }
    defaults.update(overrides)
    return CriterionResult(**defaults)


def test_overall_strong_match_all_must_have_no_preferred() -> None:
    results = [_result(type="MUST_HAVE", status=CRITERION_STATUS_MATCH)]
    assert compute_overall_result(results) == OVERALL_STRONG_MATCH


def test_overall_must_have_unknown_is_insufficient_not_rejection() -> None:
    results = [_result(type="MUST_HAVE", status=CRITERION_STATUS_UNKNOWN)]
    assert compute_overall_result(results) == OVERALL_INSUFFICIENT_EVIDENCE


def test_overall_must_have_not_matched_is_insufficient() -> None:
    results = [_result(type="MUST_HAVE", status=CRITERION_STATUS_NOT_MATCHED)]
    assert compute_overall_result(results) == OVERALL_INSUFFICIENT_EVIDENCE


def test_overall_any_manual_review_dominates() -> None:
    results = [
        _result(criterion_id="c1", type="MUST_HAVE", status=CRITERION_STATUS_MATCH),
        _result(
            criterion_id="c2", type="PREFERRED", status=CRITERION_STATUS_MANUAL_REVIEW_REQUIRED
        ),
    ]
    assert compute_overall_result(results) == OVERALL_MANUAL_REVIEW_REQUIRED


def test_overall_conflicting_evidence_dominates() -> None:
    results = [
        _result(criterion_id="c1", type="MUST_HAVE", status=CRITERION_STATUS_MATCH),
        _result(criterion_id="c2", type="MUST_HAVE", status=CRITERION_STATUS_CONFLICTING_EVIDENCE),
    ]
    assert compute_overall_result(results) == OVERALL_MANUAL_REVIEW_REQUIRED


def test_overall_preferred_coverage_below_half_is_potential_match() -> None:
    results = [
        _result(criterion_id="c1", type="MUST_HAVE", status=CRITERION_STATUS_MATCH),
        _result(criterion_id="c2", type="PREFERRED", status=CRITERION_STATUS_MATCH),
        _result(criterion_id="c3", type="PREFERRED", status=CRITERION_STATUS_UNKNOWN),
        _result(criterion_id="c4", type="PREFERRED", status=CRITERION_STATUS_UNKNOWN),
    ]
    assert compute_overall_result(results) == OVERALL_POTENTIAL_MATCH


def test_overall_preferred_coverage_at_or_above_half_is_strong_match() -> None:
    results = [
        _result(criterion_id="c1", type="MUST_HAVE", status=CRITERION_STATUS_MATCH),
        _result(criterion_id="c2", type="PREFERRED", status=CRITERION_STATUS_MATCH),
        _result(criterion_id="c3", type="PREFERRED", status=CRITERION_STATUS_UNKNOWN),
    ]
    assert compute_overall_result(results) == OVERALL_STRONG_MATCH


def test_no_hidden_criteria_only_configured_ones_are_evaluated() -> None:
    criteria = [_criterion(id="python", value="Python"), _criterion(id="sql", value="SQL")]
    skills = [SkillItem(name="Python", evidence=_EV), SkillItem(name="SQL", evidence=_EV)]
    profile = _empty_profile(skills=skills)
    results = [evaluate_criterion(c, profile) for c in criteria]
    assert {r.criterion_id for r in results} == {"python", "sql"}
    assert len(results) == 2
