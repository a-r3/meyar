from collections.abc import Callable
from datetime import date

from meyar.core.domain_terms import canonicalize_domain
from meyar.evaluation.experience import merge_and_sum_years, parse_year, ranges_overlap
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
    criterion: CriterionIn,
    profile: CandidateProfileExtraction,
    *,
    evaluation_as_of_date: date,
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
        start_year = parse_year(entry.start_date, evaluation_as_of_date=evaluation_as_of_date)
        end_year = parse_year(
            entry.end_date,
            evaluation_as_of_date=evaluation_as_of_date,
            is_current=entry.is_current,
        )
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


def _resolve_period_years(
    entry, *, evaluation_as_of_date: date
) -> tuple[int, int] | None:
    """Deterministically parses one employment entry's [start, end) year
    range, or None when either bound cannot be reliably parsed — callers
    must treat None as "insufficient evidence," never as 0 or "ongoing"."""
    start_year = parse_year(entry.start_date, evaluation_as_of_date=evaluation_as_of_date)
    end_year = parse_year(
        entry.end_date, evaluation_as_of_date=evaluation_as_of_date, is_current=entry.is_current
    )
    if start_year is None or end_year is None:
        return None
    return (start_year, end_year)


def evaluate_skill_experience(
    criterion: CriterionIn,
    profile: CandidateProfileExtraction,
    *,
    evaluation_as_of_date: date,
) -> CriterionResult:
    """A duration claim scoped to ONE named skill (e.g. "5 years Java") —
    provable ONLY via explicit SkillExperienceItem grounding, never via
    total career experience or an unlinked SkillItem. See issue #32 core
    rule: subject + attributable interval, or UNKNOWN."""
    target = normalize_skill_name(criterion.value or "")
    linked = [
        item
        for item in profile.skill_experience
        if normalize_skill_name(item.skill_name) == target
    ]
    if not linked:
        return _finalize(
            criterion,
            CRITERION_STATUS_UNKNOWN,
            "SKILL_DURATION_NO_ATTRIBUTABLE_PERIODS",
            f"No attributable employment period links required skill "
            f"'{criterion.value}' to a specific date range; duration cannot be computed.",
        )

    ranges: list[tuple[int, int]] = []
    evidence: list[EvidenceRef] = []
    for item in linked:
        entry = profile.employment_history[item.employment_index]
        period = _resolve_period_years(entry, evaluation_as_of_date=evaluation_as_of_date)
        if period is None:
            return _finalize(
                criterion,
                CRITERION_STATUS_UNKNOWN,
                "SKILL_DURATION_DATES_UNPARSEABLE",
                f"An attributable period for skill '{criterion.value}' (entry "
                f"'{entry.title}') has dates that cannot be reliably parsed "
                f"(start='{entry.start_date}', end='{entry.end_date}'); duration "
                "cannot be computed.",
                evidence=item.evidence + entry.evidence,
            )
        ranges.append(period)
        evidence.extend(item.evidence)
        evidence.extend(entry.evidence)

    total_years = merge_and_sum_years(ranges)
    required = criterion.min_years or 0.0
    if total_years >= required:
        return _finalize(
            criterion,
            CRITERION_STATUS_MATCH,
            "SKILL_DURATION_SUFFICIENT",
            f"Computed {total_years} attributable year(s) of '{criterion.value}' "
            f"experience, meeting the required {required}.",
            evidence=evidence,
            confidence=1.0,
        )
    return _finalize(
        criterion,
        CRITERION_STATUS_NOT_MATCHED,
        "SKILL_DURATION_INSUFFICIENT",
        f"Computed {total_years} attributable year(s) of '{criterion.value}' "
        f"experience, below the required {required}.",
        evidence=evidence,
        confidence=1.0,
    )


def evaluate_domain_experience(
    criterion: CriterionIn,
    profile: CandidateProfileExtraction,
    *,
    evaluation_as_of_date: date,
) -> CriterionResult:
    """An explicit sector/domain claim (e.g. "banking", "AML"), matched
    only against DomainExperienceItem entries whose evidence already
    passed the extraction-time explicit-term check (never inferred from
    an employer name — see meyar.core.domain_terms). min_years is
    optional: when set, only attributable (dated) linked periods count
    toward it."""
    target = canonicalize_domain(criterion.value or "")
    matches = [
        item for item in profile.domain_experience if canonicalize_domain(item.domain) == target
    ]
    if not matches:
        return _finalize(
            criterion,
            CRITERION_STATUS_UNKNOWN,
            "DOMAIN_NOT_FOUND_IN_PROFILE",
            f"No explicit profile evidence found for required domain/sector "
            f"'{criterion.value}'.",
        )

    presence_evidence: list[EvidenceRef] = [ref for item in matches for ref in item.evidence]
    if not criterion.min_years:
        return _finalize(
            criterion,
            CRITERION_STATUS_MATCH,
            "DOMAIN_EXPLICIT_MATCH",
            f"Profile has explicit evidence of '{criterion.value}' domain/sector "
            "experience.",
            evidence=presence_evidence,
            confidence=1.0,
        )

    ranges: list[tuple[int, int]] = []
    duration_evidence: list[EvidenceRef] = list(presence_evidence)
    for item in matches:
        if item.employment_index is None:
            continue
        entry = profile.employment_history[item.employment_index]
        period = _resolve_period_years(entry, evaluation_as_of_date=evaluation_as_of_date)
        if period is None:
            # An attributed-but-unparseable period makes the duration claim
            # not fully provable — UNKNOWN, never a possibly-undercounted
            # NOT_MATCHED (same discipline as evaluate_skill_experience).
            return _finalize(
                criterion,
                CRITERION_STATUS_UNKNOWN,
                "DOMAIN_DURATION_DATES_UNPARSEABLE",
                f"An attributable period for domain/sector '{criterion.value}' (entry "
                f"'{entry.title}') has dates that cannot be reliably parsed "
                f"(start='{entry.start_date}', end='{entry.end_date}'); duration "
                "cannot be computed.",
                evidence=item.evidence + entry.evidence,
            )
        ranges.append(period)
        duration_evidence.extend(entry.evidence)

    if not ranges:
        return _finalize(
            criterion,
            CRITERION_STATUS_UNKNOWN,
            "DOMAIN_DURATION_NO_ATTRIBUTABLE_PERIODS",
            f"Profile has explicit '{criterion.value}' domain/sector evidence but no "
            "attributable dated period to compute duration.",
            evidence=presence_evidence,
        )

    total_years = merge_and_sum_years(ranges)
    if total_years >= criterion.min_years:
        return _finalize(
            criterion,
            CRITERION_STATUS_MATCH,
            "DOMAIN_DURATION_SUFFICIENT",
            f"Computed {total_years} attributable year(s) of '{criterion.value}' "
            f"domain/sector experience, meeting the required {criterion.min_years}.",
            evidence=duration_evidence,
            confidence=1.0,
        )
    return _finalize(
        criterion,
        CRITERION_STATUS_NOT_MATCHED,
        "DOMAIN_DURATION_INSUFFICIENT",
        f"Computed {total_years} attributable year(s) of '{criterion.value}' "
        f"domain/sector experience, below the required {criterion.min_years}.",
        evidence=duration_evidence,
        confidence=1.0,
    )


_EVALUATORS: dict[
    CriterionKind, Callable[[CriterionIn, CandidateProfileExtraction], CriterionResult]
] = {
    CriterionKind.SKILL: evaluate_skill,
    CriterionKind.CERTIFICATION: evaluate_certification,
    CriterionKind.EDUCATION: evaluate_education,
    CriterionKind.LANGUAGE: evaluate_language,
}

_DATE_AWARE_EVALUATORS: dict[
    CriterionKind,
    Callable[..., CriterionResult],
] = {
    CriterionKind.EXPERIENCE: evaluate_experience,
    CriterionKind.SKILL_EXPERIENCE: evaluate_skill_experience,
    CriterionKind.DOMAIN_EXPERIENCE: evaluate_domain_experience,
}


def evaluate_criterion(
    criterion: CriterionIn,
    profile: CandidateProfileExtraction,
    *,
    evaluation_as_of_date: date,
) -> CriterionResult:
    date_aware_evaluator = _DATE_AWARE_EVALUATORS.get(criterion.kind)
    if date_aware_evaluator is not None:
        return date_aware_evaluator(criterion, profile, evaluation_as_of_date=evaluation_as_of_date)
    evaluator = _EVALUATORS.get(criterion.kind)
    if evaluator is None:
        return _finalize(
            criterion,
            CRITERION_STATUS_MANUAL_REVIEW_REQUIRED,
            "UNSUPPORTED_CRITERION",
            f"No evaluator implemented for criterion kind '{criterion.kind}'.",
        )
    return evaluator(criterion, profile)
