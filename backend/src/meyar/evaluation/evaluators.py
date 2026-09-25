from collections.abc import Callable
from datetime import date
from typing import Literal

from meyar.core.domain_terms import canonicalize_domain
from meyar.evaluation.experience import merge_and_sum_years, parse_year, ranges_overlap
from meyar.evaluation.normalization import (
    normalize_certification_name,
    normalize_skill_name,
    normalize_text,
)
from meyar.schemas.candidate_profile import CandidateProfileExtraction, EvidenceRef, LanguageItem
from meyar.schemas.criteria import CriterionIn, CriterionKind
from meyar.schemas.evaluation import (
    CRITERION_STATUS_CONFLICTING_EVIDENCE,
    CRITERION_STATUS_MANUAL_REVIEW_REQUIRED,
    CRITERION_STATUS_MATCH,
    CRITERION_STATUS_NOT_MATCHED,
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
    target = normalize_certification_name(criterion.value or "")
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


_CEFR_ORDER = {level: index for index, level in enumerate(("a1", "a2", "b1", "b2", "c1", "c2"))}
_LanguageComparison = Literal[
    "threshold_met", "below", "explicit_match", "unstated", "incompatible"
]


def _compare_language_level(
    proficiency: str | None, required_level: str
) -> _LanguageComparison:
    """One comparison authority for both single and duplicate language facts."""
    if not proficiency:
        return "unstated"
    candidate_level = normalize_text(proficiency)
    if required_level in _CEFR_ORDER and candidate_level in _CEFR_ORDER:
        return (
            "threshold_met"
            if _CEFR_ORDER[candidate_level] >= _CEFR_ORDER[required_level]
            else "below"
        )
    if candidate_level == required_level:
        return "explicit_match"
    return "incompatible"


def _language_fact_order(entry: tuple[int, LanguageItem]) -> tuple:
    """Stable source order even if extraction's language list is permuted."""
    _index, item = entry
    return (
        tuple((ref.page, ref.block_index, ref.quote) for ref in item.evidence),
        normalize_text(item.proficiency or ""),
    )


def evaluate_language_with_fact_indices(
    criterion: CriterionIn, profile: CandidateProfileExtraction
) -> tuple[CriterionResult, list[int]]:
    """Evaluate all same-name level facts and return only positive provenance.

    A supported satisfying fact wins. Without one, any unstated or
    incompatible fact leaves the level UNKNOWN; only a complete set of
    comparable below-threshold facts is NOT_MATCHED. No scale conversion
    is inferred for unsupported words such as "Fluent" against CEFR.
    """
    target_lang = normalize_text(criterion.value or "")
    matching = [
        (index, item)
        for index, item in enumerate(profile.languages)
        if normalize_text(item.language) == target_lang
    ]
    if not matching:
        return (
            _finalize(
                criterion,
                CRITERION_STATUS_UNKNOWN,
                "LANGUAGE_NOT_FOUND_IN_PROFILE",
                f"No profile evidence found for required language '{criterion.value}'.",
            ),
            [],
        )

    # Preserve existing bare-language presence behavior, including its
    # first-fact evaluator evidence. Ordinary search shows every same-name
    # fact through its separate bare-language presentation path.
    first_index, first_item = matching[0]
    if not criterion.required_level:
        return (
            _finalize(
                criterion,
                CRITERION_STATUS_MATCH,
                "LANGUAGE_PRESENT",
                f"Profile lists language '{first_item.language}' (no proficiency level required).",
                evidence=first_item.evidence,
                confidence=1.0,
            ),
            [first_index],
        )

    required_level = normalize_text(criterion.required_level)
    compared = [
        (index, item, _compare_language_level(item.proficiency, required_level))
        for index, item in matching
    ]
    satisfying = [
        (index, item)
        for index, item, outcome in compared
        if outcome in ("threshold_met", "explicit_match")
    ]
    if satisfying:
        ordered = sorted(satisfying, key=_language_fact_order)
        single = len(matching) == 1
        item = ordered[0][1]
        threshold = required_level in _CEFR_ORDER
        return (
            _finalize(
                criterion,
                CRITERION_STATUS_MATCH,
                "LANGUAGE_LEVEL_THRESHOLD_MET" if threshold else "LANGUAGE_LEVEL_EXPLICIT_MATCH",
                (
                    f"Profile states '{item.language}' at CEFR {item.proficiency}, meeting "
                    f"the required CEFR {criterion.required_level} threshold."
                    if threshold and single
                    else f"Profile states '{item.language}' at proficiency '{item.proficiency}', "
                    f"meeting the required '{criterion.required_level}'."
                    if single
                    else f"Profile has supported '{criterion.value}' proficiency evidence "
                    f"meeting the required '{criterion.required_level}'."
                ),
                evidence=[ref for _, fact in ordered for ref in fact.evidence],
                confidence=1.0,
            ),
            [index for index, _ in ordered],
        )

    ordered_all = sorted(matching, key=_language_fact_order)
    evidence = [ref for _, fact in ordered_all for ref in fact.evidence]
    if any(outcome in ("unstated", "incompatible") for _, _, outcome in compared):
        incompatible = any(outcome == "incompatible" for _, _, outcome in compared)
        reason = (
            "LANGUAGE_LEVEL_SCALE_INCOMPATIBLE" if incompatible else "LANGUAGE_LEVEL_UNSTATED"
        )
        if len(matching) == 1:
            item = matching[0][1]
            explanation = (
                f"Profile states '{item.language}' proficiency as '{item.proficiency}', which "
                f"cannot be deterministically compared with '{criterion.required_level}'."
                if incompatible
                else f"Profile lists '{item.language}' but has no supported proficiency evidence "
                f"to compare with required '{criterion.required_level}'."
            )
        else:
            explanation = (
                f"Not all '{criterion.value}' proficiency facts can be deterministically "
                f"compared with required '{criterion.required_level}'."
            )
        return (
            _finalize(
                criterion, CRITERION_STATUS_UNKNOWN, reason, explanation, evidence=evidence
            ),
            [],
        )

    item = matching[0][1]
    explanation = (
        f"Profile states '{item.language}' at CEFR {item.proficiency}, below "
        f"the required CEFR {criterion.required_level} threshold."
        if len(matching) == 1
        else f"All supported '{criterion.value}' proficiency facts are below "
        f"the required CEFR {criterion.required_level} threshold."
    )
    return (
        _finalize(
            criterion,
            CRITERION_STATUS_NOT_MATCHED,
            "LANGUAGE_LEVEL_THRESHOLD_NOT_MET",
            explanation,
            evidence=evidence,
            confidence=1.0,
        ),
        [],
    )


def evaluate_language(
    criterion: CriterionIn, profile: CandidateProfileExtraction
) -> CriterionResult:
    result, _indices = evaluate_language_with_fact_indices(criterion, profile)
    return result


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


def _has_declared_interval(item) -> bool:  # noqa: ANN001 - duck-typed skill/domain item
    """True only when the item's OWN start_date/end_date/is_current say
    something — never a proxy for its linked employment entry. See
    docs/DECISIONS.md: employment_index is context/provenance only and
    must never be used to derive a skill/domain's duration."""
    return item.start_date is not None or item.end_date is not None or item.is_current


def _period_is_compatible_with_employment(
    period: tuple[int, int], employment, *, evaluation_as_of_date: date
) -> bool:
    employment_period = _resolve_period_years(
        employment, evaluation_as_of_date=evaluation_as_of_date
    )
    return bool(
        employment_period is not None
        and employment_period[0] <= period[0]
        and period[1] <= employment_period[1]
    )


def evaluate_skill_experience(
    criterion: CriterionIn,
    profile: CandidateProfileExtraction,
    *,
    evaluation_as_of_date: date,
) -> CriterionResult:
    """A duration claim scoped to ONE named skill (e.g. "5 years Java") —
    provable ONLY from the SkillExperienceItem's OWN attributable
    start_date/end_date/is_current, never via total career experience, an
    unlinked SkillItem, or the linked employment entry's full period
    (employment_index is context/provenance only — see
    SkillExperienceItem's docstring and docs/DECISIONS.md). See issue #32
    core rule: subject + attributable interval, or UNKNOWN."""
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
            f"The CV does not provide dated evidence showing how long "
            f"'{criterion.value}' was used, so its duration is unknown.",
        )

    ranges: list[tuple[int, int]] = []
    evidence: list[EvidenceRef] = []
    for item in linked:
        entry = profile.employment_history[item.employment_index]
        if not _has_declared_interval(item):
            return _finalize(
                criterion,
                CRITERION_STATUS_UNKNOWN,
                "SKILL_DURATION_NO_ATTRIBUTABLE_INTERVAL",
                f"The CV links '{criterion.value}' to '{entry.title}' but does not "
                "state dates for using that skill, so the job's full duration is "
                "not counted as skill experience.",
                evidence=item.evidence + entry.evidence,
            )
        period = _resolve_period_years(item, evaluation_as_of_date=evaluation_as_of_date)
        if period is None:
            return _finalize(
                criterion,
                CRITERION_STATUS_UNKNOWN,
                "SKILL_DURATION_DATES_UNPARSEABLE",
                f"The dated CV evidence for '{criterion.value}' "
                f"(start='{item.start_date}', end='{item.end_date}') cannot be "
                "reliably parsed; duration cannot be computed.",
                evidence=item.evidence + entry.evidence,
            )
        if not _period_is_compatible_with_employment(
            period, entry, evaluation_as_of_date=evaluation_as_of_date
        ):
            return _finalize(
                criterion,
                CRITERION_STATUS_UNKNOWN,
                "SKILL_DURATION_EMPLOYMENT_INCOMPATIBLE",
                f"The dated '{criterion.value}' evidence does not fit the linked "
                "job period, so the skill duration is unknown.",
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
            f"CV evidence shows {total_years} year(s) of '{criterion.value}' "
            f"experience, meeting the minimum {required}.",
            evidence=evidence,
            confidence=1.0,
        )
    return _finalize(
        criterion,
        CRITERION_STATUS_NOT_MATCHED,
        "SKILL_DURATION_INSUFFICIENT",
        f"CV evidence shows {total_years} year(s) of '{criterion.value}' "
        f"experience, below the minimum {required}.",
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
    optional: when set, duration comes ONLY from a matched item's OWN
    attributable start_date/end_date/is_current — never from a linked
    employment entry's full period (employment_index is context/
    provenance only)."""
    target = canonicalize_domain(criterion.value or "")
    matches = [
        item for item in profile.domain_experience if canonicalize_domain(item.domain) == target
    ]
    if not matches:
        return _finalize(
            criterion,
            CRITERION_STATUS_UNKNOWN,
            "DOMAIN_NOT_FOUND_IN_PROFILE",
            f"The CV has no explicit evidence of experience in '{criterion.value}'.",
        )

    presence_evidence: list[EvidenceRef] = [ref for item in matches for ref in item.evidence]
    if not criterion.min_years:
        return _finalize(
            criterion,
            CRITERION_STATUS_MATCH,
            "DOMAIN_EXPLICIT_MATCH",
            f"The CV explicitly shows experience in '{criterion.value}'.",
            evidence=presence_evidence,
            confidence=1.0,
        )

    ranges: list[tuple[int, int]] = []
    duration_evidence: list[EvidenceRef] = list(presence_evidence)
    for item in matches:
        if not _has_declared_interval(item):
            # Unlike SKILL_EXPERIENCE (where every item exists specifically
            # to ground a duration), a presence-only domain claim with no
            # interval is a normal, complete shape on its own — it simply
            # doesn't contribute to the duration sum. Skipping never
            # fabricates (only ever under-counts); UNKNOWN is still
            # returned below if no matched item ends up contributing any
            # computable interval at all.
            continue
        period = _resolve_period_years(item, evaluation_as_of_date=evaluation_as_of_date)
        if period is None:
            # An attributed-but-unparseable period makes the duration claim
            # not fully provable — UNKNOWN, never a possibly-undercounted
            # NOT_MATCHED (same discipline as evaluate_skill_experience).
            return _finalize(
                criterion,
                CRITERION_STATUS_UNKNOWN,
                "DOMAIN_DURATION_DATES_UNPARSEABLE",
                f"The dated CV evidence for '{criterion.value}' "
                f"(start='{item.start_date}', end='{item.end_date}') cannot be reliably "
                "parsed; duration cannot be computed.",
                evidence=item.evidence,
            )
        if item.employment_index is not None:
            employment = profile.employment_history[item.employment_index]
            if not _period_is_compatible_with_employment(
                period, employment, evaluation_as_of_date=evaluation_as_of_date
            ):
                return _finalize(
                    criterion,
                    CRITERION_STATUS_UNKNOWN,
                    "DOMAIN_DURATION_EMPLOYMENT_INCOMPATIBLE",
                    f"The dated '{criterion.value}' evidence does not fit the linked "
                    "job period, so its duration is unknown.",
                    evidence=item.evidence + employment.evidence,
                )
        ranges.append(period)
        duration_evidence.extend(item.evidence)

    if not ranges:
        return _finalize(
            criterion,
            CRITERION_STATUS_UNKNOWN,
            "DOMAIN_DURATION_NO_ATTRIBUTABLE_INTERVAL",
            f"The CV shows '{criterion.value}' experience but gives no dates for it, "
            "so a linked job's full duration is not counted as domain experience.",
            evidence=presence_evidence,
        )

    total_years = merge_and_sum_years(ranges)
    if total_years >= criterion.min_years:
        return _finalize(
            criterion,
            CRITERION_STATUS_MATCH,
            "DOMAIN_DURATION_SUFFICIENT",
            f"CV evidence shows {total_years} year(s) of '{criterion.value}' "
            f"experience, meeting the minimum {criterion.min_years}.",
            evidence=duration_evidence,
            confidence=1.0,
        )
    return _finalize(
        criterion,
        CRITERION_STATUS_NOT_MATCHED,
        "DOMAIN_DURATION_INSUFFICIENT",
        f"CV evidence shows {total_years} year(s) of '{criterion.value}' "
        f"experience, below the minimum {criterion.min_years}.",
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
