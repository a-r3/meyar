from meyar.evaluation.experience import parse_year, ranges_overlap
from meyar.evaluation.normalization import normalize_skill_name, normalize_text
from meyar.schemas.candidate_profile import CandidateProfileExtraction, EvidenceRef
from meyar.schemas.criteria import CriterionIn, CriterionKind
from meyar.schemas.evaluation import (
    CRITERION_STATUS_CONFLICTING_EVIDENCE,
    CRITERION_STATUS_MANUAL_REVIEW_REQUIRED,
    CRITERION_STATUS_MATCH,
    CRITERION_STATUS_NOT_MATCHED,
    CRITERION_STATUS_PARTIAL_MATCH,
    CRITERION_STATUS_UNKNOWN,
    CriterionResult,
)


def _finalize(
    criterion: CriterionIn,
    status: str,
    reason_code: str,
    explanation: str,
    *,
    evidence: list[EvidenceRef] | None = None,
    confidence: float | None = None,
) -> CriterionResult:
    """Single point where a criterion's configured manual_review_required
    flag (or a raw CONFLICTING_EVIDENCE finding) forces the final status —
    every evaluator funnels through here so this rule cannot be bypassed."""
    forced = criterion.manual_review_required or status == CRITERION_STATUS_CONFLICTING_EVIDENCE
    final_status = CRITERION_STATUS_MANUAL_REVIEW_REQUIRED if forced else status
    return CriterionResult(
        criterion_id=criterion.id,
        kind=criterion.kind.value,
        type=criterion.type.value,
        configured_weight=criterion.weight,
        status=final_status,
        reason_code=reason_code,
        explanation=explanation,
        evidence=evidence or [],
        confidence=confidence,
        manual_review_required=(final_status == CRITERION_STATUS_MANUAL_REVIEW_REQUIRED),
    )


def evaluate_skill(
    criterion: CriterionIn, profile: CandidateProfileExtraction
) -> CriterionResult:
    target = normalize_skill_name(criterion.value or "")
    for skill in profile.skills:
        if normalize_skill_name(skill.name) == target:
            return _finalize(
                criterion,
                CRITERION_STATUS_MATCH,
                "SKILL_EXPLICIT_MATCH",
                f"Profile lists skill '{skill.name}', matching required skill "
                f"'{criterion.value}'.",
                evidence=skill.evidence,
                confidence=1.0,
            )
    return _finalize(
        criterion,
        CRITERION_STATUS_UNKNOWN,
        "SKILL_NOT_FOUND_IN_PROFILE",
        f"No profile evidence found for required skill '{criterion.value}'.",
    )


def evaluate_certification(
    criterion: CriterionIn, profile: CandidateProfileExtraction
) -> CriterionResult:
    target = normalize_text(criterion.value or "")
    for cert in profile.certifications:
        if normalize_text(cert.name) == target:
            return _finalize(
                criterion,
                CRITERION_STATUS_MATCH,
                "CERTIFICATION_EXPLICIT_MATCH",
                f"Profile lists certification '{cert.name}'.",
                evidence=cert.evidence,
                confidence=1.0,
            )
    return _finalize(
        criterion,
        CRITERION_STATUS_UNKNOWN,
        "CERTIFICATION_NOT_FOUND_IN_PROFILE",
        f"No profile evidence found for required certification '{criterion.value}'.",
    )


def evaluate_education(
    criterion: CriterionIn, profile: CandidateProfileExtraction
) -> CriterionResult:
    target = normalize_text(criterion.value or "")
    for edu in profile.education:
        candidate_values = {
            normalize_text(edu.degree or ""),
            normalize_text(edu.field_of_study or ""),
        }
        if target and target in candidate_values:
            return _finalize(
                criterion,
                CRITERION_STATUS_MATCH,
                "EDUCATION_EXPLICIT_MATCH",
                f"Profile education entry matches required '{criterion.value}'.",
                evidence=edu.evidence,
                confidence=1.0,
            )
    return _finalize(
        criterion,
        CRITERION_STATUS_UNKNOWN,
        "EDUCATION_NOT_FOUND_IN_PROFILE",
        f"No profile evidence found for required education '{criterion.value}'.",
    )


def evaluate_language(
    criterion: CriterionIn, profile: CandidateProfileExtraction
) -> CriterionResult:
    target_lang = normalize_text(criterion.value or "")
    required_level = normalize_text(criterion.required_level) if criterion.required_level else None

    for lang in profile.languages:
        if normalize_text(lang.language) != target_lang:
            continue
        if required_level is None:
            return _finalize(
                criterion,
                CRITERION_STATUS_MATCH,
                "LANGUAGE_PRESENT",
                f"Profile lists language '{lang.language}' (no proficiency level required).",
                evidence=lang.evidence,
                confidence=1.0,
            )
        if lang.proficiency and normalize_text(lang.proficiency) == required_level:
            return _finalize(
                criterion,
                CRITERION_STATUS_MATCH,
                "LANGUAGE_LEVEL_EXPLICIT_MATCH",
                f"Profile states '{lang.language}' at proficiency '{lang.proficiency}', "
                f"meeting the required '{criterion.required_level}'.",
                evidence=lang.evidence,
                confidence=1.0,
            )
        return _finalize(
            criterion,
            CRITERION_STATUS_PARTIAL_MATCH,
            "LANGUAGE_LEVEL_UNSTATED",
            f"Profile lists '{lang.language}' but does not state the required "
            f"proficiency level '{criterion.required_level}'.",
            evidence=lang.evidence,
            confidence=0.5,
        )

    return _finalize(
        criterion,
        CRITERION_STATUS_UNKNOWN,
        "LANGUAGE_NOT_FOUND_IN_PROFILE",
        f"No profile evidence found for required language '{criterion.value}'.",
    )


def evaluate_experience(
    criterion: CriterionIn, profile: CandidateProfileExtraction
) -> CriterionResult:
    if not profile.employment_history:
        return _finalize(
            criterion,
            CRITERION_STATUS_UNKNOWN,
            "EXPERIENCE_NO_EMPLOYMENT_HISTORY",
            "Profile contains no employment history entries.",
        )

    ranges: list[tuple[int, int]] = []
    evidence: list[EvidenceRef] = []
    for entry in profile.employment_history:
        start_year = parse_year(entry.start_date)
        end_year = parse_year(entry.end_date, is_current=entry.is_current)
        if start_year is None or end_year is None:
            return _finalize(
                criterion,
                CRITERION_STATUS_MANUAL_REVIEW_REQUIRED,
                "EXPERIENCE_DATES_UNPARSEABLE",
                f"Employment entry '{entry.title}' has dates that cannot be reliably "
                f"parsed (start='{entry.start_date}', end='{entry.end_date}').",
                evidence=entry.evidence,
            )
        ranges.append((start_year, end_year))
        evidence.extend(entry.evidence)

    for i in range(len(ranges)):
        for j in range(i + 1, len(ranges)):
            if ranges_overlap(ranges[i], ranges[j]):
                return _finalize(
                    criterion,
                    CRITERION_STATUS_CONFLICTING_EVIDENCE,
                    "EXPERIENCE_OVERLAPPING_DATES",
                    "Two or more employment entries have overlapping date ranges; "
                    "total experience cannot be reliably computed.",
                    evidence=evidence,
                )

    total_years = sum(max(end - start, 0) for start, end in ranges)
    required = criterion.min_years or 0.0
    if total_years >= required:
        return _finalize(
            criterion,
            CRITERION_STATUS_MATCH,
            "EXPERIENCE_DURATION_SUFFICIENT",
            f"Computed {total_years} year(s) of relevant experience, meeting the "
            f"required {required}.",
            evidence=evidence,
            confidence=1.0,
        )
    return _finalize(
        criterion,
        CRITERION_STATUS_NOT_MATCHED,
        "EXPERIENCE_DURATION_INSUFFICIENT",
        f"Computed {total_years} year(s) of relevant experience, below the "
        f"required {required}.",
        evidence=evidence,
        confidence=1.0,
    )


_EVALUATORS = {
    CriterionKind.SKILL: evaluate_skill,
    CriterionKind.CERTIFICATION: evaluate_certification,
    CriterionKind.EDUCATION: evaluate_education,
    CriterionKind.LANGUAGE: evaluate_language,
    CriterionKind.EXPERIENCE: evaluate_experience,
}


def evaluate_criterion(
    criterion: CriterionIn, profile: CandidateProfileExtraction
) -> CriterionResult:
    evaluator = _EVALUATORS.get(criterion.kind)
    if evaluator is None:
        return _finalize(
            criterion,
            CRITERION_STATUS_MANUAL_REVIEW_REQUIRED,
            "UNSUPPORTED_CRITERION",
            f"No evaluator implemented for criterion kind '{criterion.kind}'.",
        )
    return evaluator(criterion, profile)
