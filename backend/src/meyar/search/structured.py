"""Slice 8 — deterministic structured filter evaluation over a
candidate's CURRENT CandidateProfileExtraction. No LLM. Reuses the
existing normalization primitives (meyar.evaluation.normalization) and
overlap-detection helper (meyar.evaluation.experience) rather than
reimplementing skill/text matching — see .claude/rules/architecture.md.

Search-gating semantics are deliberately stricter than the evaluation
engine's UNKNOWN-is-not-NOT_MATCHED policy (docs/DECISIONS.md D-010):
a job evaluation must never silently convert missing evidence into a
rejection, but a search REQUIRED filter is a hard eligibility gate by
definition (docs/DECISIONS.md — this slice) — an unproven required
filter (absent skill, or an unparseable/ambiguous experience duration)
simply excludes the candidate from this search's results. This does not
touch or reinterpret the evaluation engine's own UNKNOWN semantics."""

import re
from dataclasses import dataclass, field
from datetime import date

from meyar.evaluation.evaluators import evaluate_criterion
from meyar.evaluation.experience import ranges_overlap
from meyar.evaluation.normalization import (
    normalize_certification_name,
    normalize_skill_name,
    normalize_text,
)
from meyar.schemas.candidate_profile import CandidateProfileExtraction
from meyar.schemas.criteria import CriterionIn, CriterionKind, CriterionType
from meyar.schemas.evaluation import CRITERION_STATUS_MATCH
from meyar.search.schemas import (
    PreferredFilterMatch,
    PreferredFilters,
    RequiredFilterMatch,
    RequiredFilters,
)

# Search retains its explicit-as-of parser so its structured-filter policy
# remains isolated from the evaluation policy even though both now require a
# caller-supplied date for ongoing employment.
_YEAR_RE = re.compile(r"(19|20)\d{2}")
_PRESENT_RE = re.compile(r"present|current|now|ongoing", re.IGNORECASE)


def _parse_year_deterministic(text: str | None, *, is_current: bool, as_of_year: int) -> int | None:
    if is_current:
        return as_of_year
    if not text:
        return None
    if _PRESENT_RE.search(text):
        return as_of_year
    match = _YEAR_RE.search(text)
    return int(match.group(0)) if match else None


def compute_total_experience_years(
    employment_history: list[dict], *, as_of_year: int
) -> float | None:
    """Returns None when total experience cannot be reliably computed (no
    employment history, an unparseable date, or overlapping date ranges)
    — callers must treat None as "unprovable", never as a false 0 or a
    false match."""
    if not employment_history:
        return None

    ranges: list[tuple[int, int]] = []
    for entry in employment_history:
        start_year = _parse_year_deterministic(
            entry.get("start_date"), is_current=False, as_of_year=as_of_year
        )
        end_year = _parse_year_deterministic(
            entry.get("end_date"),
            is_current=bool(entry.get("is_current", False)),
            as_of_year=as_of_year,
        )
        if start_year is None or end_year is None:
            return None
        ranges.append((start_year, end_year))

    for i in range(len(ranges)):
        for j in range(i + 1, len(ranges)):
            if ranges_overlap(ranges[i], ranges[j]):
                return None

    return float(sum(max(end - start, 0) for start, end in ranges))


def _skill_present(profile: CandidateProfileExtraction, skill_name: str) -> bool:
    target = normalize_skill_name(skill_name)
    return any(normalize_skill_name(item.name) == target for item in profile.skills)


def _certification_present(profile: CandidateProfileExtraction, cert_name: str) -> bool:
    target = normalize_certification_name(cert_name)
    return any(normalize_text(item.name) == target for item in profile.certifications)


def _language_present(profile: CandidateProfileExtraction, language_name: str) -> bool:
    target = normalize_text(language_name)
    return any(normalize_text(item.language) == target for item in profile.languages)


def _education_present(profile: CandidateProfileExtraction, education_value: str) -> bool:
    target = normalize_text(education_value)
    if not target:
        return False
    for item in profile.education:
        candidate_values = {
            normalize_text(item.degree or ""),
            normalize_text(item.field_of_study or ""),
        }
        if target in candidate_values:
            return True
    return False


@dataclass
class RequiredFilterEvaluation:
    satisfied: bool
    matches: list[RequiredFilterMatch] = field(default_factory=list)


@dataclass
class PreferredFilterEvaluation:
    score: float
    matches: list[PreferredFilterMatch] = field(default_factory=list)


def _typed_filter_matches(
    profile: CandidateProfileExtraction,
    *,
    kind: CriterionKind,
    value: str,
    min_years: float | None = None,
    required_level: str | None = None,
    as_of_date: date | None,
) -> bool:
    criterion = CriterionIn(
        id="search_filter",
        kind=kind,
        type=CriterionType.MUST_HAVE,
        label=value,
        value=value,
        min_years=min_years,
        required_level=required_level,
    )
    result = evaluate_criterion(
        criterion,
        profile,
        # Non-date-aware evaluators ignore this value. Duration filters are
        # schema-gated to carry the caller's real as_of_date.
        evaluation_as_of_date=as_of_date or date(1970, 1, 1),
    )
    return result.status == CRITERION_STATUS_MATCH


def evaluate_required_filters(
    profile: CandidateProfileExtraction,
    filters: RequiredFilters,
    *,
    as_of_year: int | None,
    as_of_date: date | None = None,
) -> RequiredFilterEvaluation:
    """Hard eligibility gate: ALL configured required filters must be
    satisfied or the candidate is excluded entirely — semantic
    similarity can never override this (see docs/DECISIONS.md)."""
    matches: list[RequiredFilterMatch] = []

    for skill in filters.skills:
        if not _skill_present(profile, skill):
            return RequiredFilterEvaluation(satisfied=False)
        matches.append(RequiredFilterMatch(category="skill", value=skill))

    for cert in filters.certifications:
        if not _certification_present(profile, cert):
            return RequiredFilterEvaluation(satisfied=False)
        matches.append(RequiredFilterMatch(category="certification", value=cert))

    for language in filters.languages:
        if not _language_present(profile, language):
            return RequiredFilterEvaluation(satisfied=False)
        matches.append(RequiredFilterMatch(category="language", value=language))

    for education in filters.education:
        if not _education_present(profile, education):
            return RequiredFilterEvaluation(satisfied=False)
        matches.append(RequiredFilterMatch(category="education", value=education))

    for language_item in filters.language_levels:
        if not _typed_filter_matches(
            profile,
            kind=CriterionKind.LANGUAGE,
            value=language_item.value,
            required_level=language_item.required_level,
            as_of_date=as_of_date,
        ):
            return RequiredFilterEvaluation(satisfied=False)
        matches.append(RequiredFilterMatch(category="language_level", value=language_item.value))

    for category, kind, items in (
        ("skill_experience", CriterionKind.SKILL_EXPERIENCE, filters.skill_experience),
        ("domain_experience", CriterionKind.DOMAIN_EXPERIENCE, filters.domain_experience),
    ):
        for duration_item in items:
            if not _typed_filter_matches(
                profile,
                kind=kind,
                value=duration_item.value,
                min_years=duration_item.min_years,
                as_of_date=as_of_date,
            ):
                return RequiredFilterEvaluation(satisfied=False)
            matches.append(RequiredFilterMatch(category=category, value=duration_item.value))

    if filters.min_total_experience_years is not None:
        if as_of_year is None:
            return RequiredFilterEvaluation(satisfied=False)
        total_years = compute_total_experience_years(
            [item.model_dump() for item in profile.employment_history], as_of_year=as_of_year
        )
        if total_years is None or total_years < filters.min_total_experience_years:
            return RequiredFilterEvaluation(satisfied=False)
        matches.append(
            RequiredFilterMatch(
                category="min_total_experience_years",
                value=str(filters.min_total_experience_years),
            )
        )

    return RequiredFilterEvaluation(satisfied=True, matches=matches)


def evaluate_preferred_filters(
    profile: CandidateProfileExtraction,
    filters: PreferredFilters,
    *,
    as_of_year: int | None,
    as_of_date: date | None = None,
) -> PreferredFilterEvaluation:
    """structured_score = matched preferred criteria / total configured
    preferred criteria, bounded [0, 1]. If no preferred criteria are
    configured, returns 0.0 for every candidate (a uniform neutral floor,
    never a fabricated advantage) — see docs/DECISIONS.md."""
    total = 0
    matched = 0
    matches: list[PreferredFilterMatch] = []

    for skill in filters.skills:
        total += 1
        if _skill_present(profile, skill):
            matched += 1
            matches.append(PreferredFilterMatch(category="skill", value=skill))

    for cert in filters.certifications:
        total += 1
        if _certification_present(profile, cert):
            matched += 1
            matches.append(PreferredFilterMatch(category="certification", value=cert))

    for language in filters.languages:
        total += 1
        if _language_present(profile, language):
            matched += 1
            matches.append(PreferredFilterMatch(category="language", value=language))

    for education in filters.education:
        total += 1
        if _education_present(profile, education):
            matched += 1
            matches.append(PreferredFilterMatch(category="education", value=education))

    for language_item in filters.language_levels:
        total += 1
        if _typed_filter_matches(
            profile,
            kind=CriterionKind.LANGUAGE,
            value=language_item.value,
            required_level=language_item.required_level,
            as_of_date=as_of_date,
        ):
            matched += 1
            matches.append(
                PreferredFilterMatch(category="language_level", value=language_item.value)
            )

    for category, kind, items in (
        ("skill_experience", CriterionKind.SKILL_EXPERIENCE, filters.skill_experience),
        ("domain_experience", CriterionKind.DOMAIN_EXPERIENCE, filters.domain_experience),
    ):
        for duration_item in items:
            total += 1
            if _typed_filter_matches(
                profile,
                kind=kind,
                value=duration_item.value,
                min_years=duration_item.min_years,
                as_of_date=as_of_date,
            ):
                matched += 1
                matches.append(PreferredFilterMatch(category=category, value=duration_item.value))

    if filters.min_total_experience_years is not None:
        total += 1
        if as_of_year is not None:
            total_years = compute_total_experience_years(
                [item.model_dump() for item in profile.employment_history], as_of_year=as_of_year
            )
            if total_years is not None and total_years >= filters.min_total_experience_years:
                matched += 1
                matches.append(
                    PreferredFilterMatch(
                        category="min_total_experience_years",
                        value=str(filters.min_total_experience_years),
                    )
                )

    score = (matched / total) if total > 0 else 0.0
    return PreferredFilterEvaluation(score=score, matches=matches)
