"""Deterministic ``meyar-search-planner-v1`` policy.

The LLM produces a constrained draft. This module—not the model—checks
semantic preservation, derives the Slice 8 mode, injects trusted runtime
configuration, and constructs the final ``CandidateSearchRequest``.
There is no database, provider call, randomness, or wall-clock access here.
"""

import re
from collections.abc import Callable
from datetime import date

from pydantic import ValidationError

from meyar.core.text import fold_az_ascii, normalize_azerbaijani_case
from meyar.evaluation.normalization import accepted_certification_terms
from meyar.schemas.criteria import ProhibitedCriterionError, find_prohibited_term
from meyar.search.planner_schemas import (
    PlanInterpretationSummary,
    PlannerDraft,
    PlannerOutcome,
    PlannerReasonCode,
)
from meyar.search.policy import (
    DEFAULT_SEMANTIC_WEIGHT,
    DEFAULT_STRUCTURED_WEIGHT,
    MAX_SEARCH_LIMIT,
)
from meyar.search.schemas import (
    CandidateSearchRequest,
    EmbeddingSearchConfig,
    PreferredFilters,
    RequiredFilters,
    SearchMode,
)

PLANNER_POLICY_VERSION = "meyar-search-planner-v1"
DEFAULT_NL_SEARCH_LIMIT = 20
MAX_NATURAL_LANGUAGE_REQUEST_LENGTH = 4000

# Browsers normalize <textarea> line breaks to CRLF, and a user composing a
# multi-line request routinely produces \t/\n/\r/\v/\f — these are ordinary
# formatting, not a control-character attack. Only these five benign
# whitespace controls are folded to a space before the printable-text safety
# check below; every other non-printable character (ANSI escapes, NUL, RTL
# overrides, zero-width characters, etc.) still fails the guard exactly as
# before. See docs/DECISIONS.md D-023.
_BENIGN_WHITESPACE_CONTROL_CHARS = str.maketrans(
    {"\t": " ", "\n": " ", "\r": " ", "\v": " ", "\f": " "}
)

# HR users frequently type Azerbaijani text on a plain Latin keyboard without
# the dedicated diacritic keys (ə/ç/ş/ö/ü/ğ/ı), substituting the nearest
# ASCII letter (e.g. "tecrübə" for "təcrübə"). The fixed-marker/keyword
# regexes below are written with correct Azerbaijani spelling for
# readability, but every literal keyword match is done against text (and,
# for these specific patterns, against a folded copy of the pattern source
# itself) with diacritics folded to their ASCII base letter via the shared
# meyar.core.text.fold_az_ascii, so either spelling is accepted. This is a
# general typing-variance normalization, not a special case for any one
# query. See docs/DECISIONS.md D-023.


def _az_pattern(source: str) -> re.Pattern[str]:
    """Compile a keyword/marker regex whose literal Azerbaijani diacritics
    are folded to ASCII, so it matches either spelling once the searched
    text is folded the same way with :func:`fold_az_ascii`."""
    return re.compile(fold_az_ascii(source))


_WORD = r"A-Za-z0-9_+#.ƏəĞğİıÖöÜüÇçŞşА-Яа-яЁё"
_BOUNDARY_WORD = r"A-Za-z0-9_ƏəĞğİıÖöÜüÇçŞşА-Яа-яЁё"
_REQUIRED_MARKERS = (
    "must",
    "required",
    "mandatory",
    "minimum",
    "at least",
    "mütləq",
    "mütləqdir",
    "mütləqdır",
    "mütləqdur",
    "mütləqdür",
    "mütləq olmalı",
    "mütləq olmalıdır",
    "ən azı",
    "tələb olunur",
    "tələb edilir",
)
_PREFERRED_MARKERS = (
    "preferably",
    "preferred",
    "ideally",
    "bonus",
    "üstünlükdür",
    "olsa yaxşıdır",
    "arzuolunandır",
)

# Azerbaijani is agglutinative: a locative ("in X") or ablative ("from X")
# case suffix attaches directly to a noun with no space, e.g. "Pythonda"
# ("in Python") or "SQL-dan" ("from SQL"). A bare word-boundary match would
# reject every such case-marked mention of a skill/certification/language
# the request otherwise genuinely names — not a SearchPlan/domain-model
# limitation, just an overly strict safety-net regex. This is deliberately
# limited to the standard locative/ablative markers (with the regular
# voiced/voiceless consonant alternation) and gated to values of at least
# 3 characters, so it cannot turn e.g. "Java" into a false match for the
# unrelated word "JavaScript" (whose suffix, "script", is not one of
# these). See docs/DECISIONS.md D-023.
_AZ_LOCATIVE_ABLATIVE_SUFFIXES = ("dan", "dən", "tan", "tən", "da", "də", "ta", "tə")

_SKILL_DURATION_PATTERNS = (
    _az_pattern(
        rf"(?i)\b(?P<years>\d+(?:\.\d+)?)\s*(?:years?|yrs?)\s+(?:of\s+)?"
        rf"(?P<skill>[{_WORD}/-]{{1,60}})\s+experience\b"
    ),
    _az_pattern(
        rf"(?i)\b(?P<skill>[{_WORD}/-]{{1,60}})\s+experience\s+(?:of|for)\s+"
        r"(?P<years>\d+(?:\.\d+)?)\s*(?:years?|yrs?)\b"
    ),
    _az_pattern(
        rf"(?i)\b(?P<skill>[{_WORD}/-]{{1,60}})\s+(?:üzrə|ilə)\s+"
        r"(?:ən\s+az[ıi]?\s+)?(?P<years>\d+(?:[.,]\d+)?)\s*il\s+(?:iş\s+)?təcrüb"
    ),
    _az_pattern(
        rf"(?i)\b(?P<years>\d+(?:[.,]\d+)?)\s*il\s+(?P<skill>[{_WORD}/-]{{1,60}})\s+təcrüb"
    ),
    # "Pythonda 5 il təcrübəsi olan" ("[having] 5 years of experience in
    # Python") — the skill takes the duration mention directly via a
    # locative/ablative case suffix instead of a "üzrə"/"ilə" connector.
    # Grammatically the same skill-SPECIFIC-duration claim as the
    # patterns above; CandidateProfile has no evidence linking a skill to
    # a duration (see docs/DECISIONS.md D-027), so this must be rejected
    # exactly like the connector forms, not silently read as "skill +
    # total experience". Optional hyphen for a bare acronym skill (e.g.
    # "SQL-dan").
    _az_pattern(
        rf"(?i)\b(?P<skill>[A-Za-z][A-Za-z0-9+#.]{{0,30}})-?"
        rf"(?:{'|'.join(_AZ_LOCATIVE_ABLATIVE_SUFFIXES)})\s+"
        r"(?P<years>\d+(?:[.,]\d+)?)\s*il\s+təcrüb"
    ),
)
_TOTAL_EXPERIENCE_PATTERNS = (
    _az_pattern(
        r"(?i)\b(?P<years>\d+(?:\.\d+)?)\s*(?:years?|yrs?)\s+"
        r"(?:of\s+)?(?:(?:total|overall|professional)\s+)?(?:work\s+)?experience\b"
    ),
    _az_pattern(
        r"(?i)\b(?P<years>\d+(?:[.,]\d+)?)\s*il\s+"
        r"(?:(?:ümumi|peşəkar)\s+)?(?:iş\s+)?təcrüb"
    ),
    # Reversed word order: "[ümumi/peşəkar] iş təcrübəsi [ən az/minimum]
    # N il" — the duration comes after "təcrübə" instead of before it
    # (e.g. the explicit-separation phrasing "ümumi iş təcrübəsi ən az 5
    # il olan").
    _az_pattern(
        r"(?i)\b(?:(?:ümumi|peşəkar)\s+)?(?:iş\s+)?təcrübə\w*\s+"
        r"(?:minimum\s+|ən\s+az[ıi]?\s+)?(?P<years>\d+(?:[.,]\d+)?)\s*il\b"
    ),
)
_RESULT_LIMIT_PATTERNS = (
    _az_pattern(r"(?i)\b(?P<count>\d{1,4})\s+(?:best\s+)?candidates?\b"),
    _az_pattern(r"(?i)\b(?P<count>\d{1,4})\s+namizəd(?:i|ə|lər|ləri)?\b"),
)

_LANGUAGE_TERMS = (
    r"english|ingilis(?:cə|\s+dili)?|russian|rus(?:ca|\s+dili)?|"
    r"azerbaijani|azərbaycan(?:ca|\s+dili)?|turkish|türk(?:cə|\s+dili)?|"
    r"language|dil"
)
_PROFICIENCY_TERMS = (
    r"a1|a2|b1|b2|c1|c2|beginner|intermediate|upper[- ]intermediate|"
    r"advanced|fluent|native|səlis|ana\s+dili"
)
_LANGUAGE_PROFICIENCY_PATTERNS = (
    _az_pattern(rf"(?i)\b(?:{_LANGUAGE_TERMS})\b.{{0,40}}\b(?:{_PROFICIENCY_TERMS})\b"),
    _az_pattern(rf"(?i)\b(?:{_PROFICIENCY_TERMS})\b.{{0,40}}\b(?:{_LANGUAGE_TERMS})\b"),
)

_IDENTITY_PATTERNS = (
    _az_pattern(
        r"(?i)\b(?:candidate\s+)?(?:name|named|full[_ -]?name|email|e-mail|phone|"
        r"telephone|contact|adlı|adı|e-poçt|emaili|telefon)\b"
    ),
    _az_pattern(rf"(?i)^\s*(?:find|show|search\s+for)\s+(?:a\s+)?[{_WORD}'-]{{2,40}}\s*[.!?]*$"),
)
_EXPLICIT_CUSTOM_WEIGHT_PATTERNS = (
    _az_pattern(r"(?i)\bprioriti[sz]e\b.{0,80}\bover\s+everything\b"),
    _az_pattern(r"(?i)\bfocus\s+mostly\b"),
    _az_pattern(r"(?i)\bhər\s+şeydən\s+üstün\b"),
)
_SEARCH_WEIGHT_COMPONENT_PATTERNS = (
    _az_pattern(r"(?i)\bsemantics?\b|\bsemantic(?:\s+(?:search|score|relevance|results?))?\b"),
    _az_pattern(r"(?i)\bstructured(?:\s+(?:search|score|relevance|results?))?\b"),
    _az_pattern(r"(?i)\bsemantik\b|\bstruktur(?:laşdırılmış)?\b"),
)
_SEARCH_WEIGHT_CONTROL_PATTERNS = (
    _az_pattern(
        r"(?i)(?:\bweights?\b|\bweighting\b|\bpercent(?:age)?\b|%|"
        r"\bmore\s+important\b|\bprioriti[sz]e\b)"
    ),
    _az_pattern(
        r"(?i)(?:\bçəki\b|\bfaiz\b|%|\bprioritet\b|\bdaha\s+çox(?:\s+çəki)?\b|"
        r"\büstün\s+tut\b)"
    ),
)
_SALARY_PATTERNS = (_az_pattern(r"(?i)\b(?:salary|compensation|maaş|əmək\s+haqqı)\b"),)
_LOCATION_PATTERNS = (
    _az_pattern(
        r"(?i)\b(?:location|located|based\s+in|resident\s+in|living\s+in|"
        r"yerləşən|yaşayan|məkan)\b"
    ),
)
_PROJECT_DURATION_PATTERNS = (
    _az_pattern(r"(?i)\b\d+(?:\.\d+)?\s*(?:years?|months?)\s+(?:on|in)\s+.+projects?\b"),
    _az_pattern(r"(?i)\b.+layihə(?:si|ləri)?ndə\s+\d+(?:[.,]\d+)?\s*(?:il|ay)\b"),
)
_PROMPT_INJECTION_PATTERNS = (
    re.compile(r"(?i)\bignore\s+(?:all\s+)?(?:previous|system|developer)\b"),
    re.compile(r"(?i)\b(?:system\s+prompt|developer\s+message|output\s+sql)\b"),
    re.compile(r"(?i)\bselect\s+.+\s+from\s+\w+"),
    re.compile(r"(?i)\b(?:database_query|database\s+query)\b"),
)

_LANGUAGE_ALIASES: dict[str, tuple[str, ...]] = {
    "english": ("english", "ingilis", "ingiliscə", "ingilis dili"),
    "russian": ("russian", "rus", "rusca", "rus dili"),
    "azerbaijani": (
        "azerbaijani",
        "azərbaycan",
        "azərbaycanca",
        "azərbaycan dili",
    ),
    "turkish": ("turkish", "türk", "türkcə", "türk dili"),
}


class PlannerPolicyError(Exception):
    def __init__(self, outcome: PlannerOutcome, *reason_codes: PlannerReasonCode) -> None:
        self.outcome = outcome
        self.reason_codes = list(dict.fromkeys(reason_codes))
        super().__init__(",".join(code.value for code in self.reason_codes))


def _canonical(text: str) -> str:
    return " ".join(text.casefold().split())


def _canonical_az(text: str) -> str:
    return " ".join(normalize_azerbaijani_case(text).split())


def _canonical_az_ascii_fold(text: str) -> str:
    return fold_az_ascii(_canonical_az(text))


_CANONICALIZERS: tuple[Callable[[str], str], ...] = (
    _canonical,
    _canonical_az,
    _canonical_az_ascii_fold,
)


def _canonical_variants(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(canonicalize(text) for canonicalize in _CANONICALIZERS))


def _matches_any(text: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    folded = fold_az_ascii(text)
    return any(pattern.search(folded) for pattern in patterns)


def _has_custom_search_weighting(text: str) -> bool:
    if _matches_any(text, _EXPLICIT_CUSTOM_WEIGHT_PATTERNS):
        return True
    return _matches_any(text, _SEARCH_WEIGHT_COMPONENT_PATTERNS) and _matches_any(
        text, _SEARCH_WEIGHT_CONTROL_PATTERNS
    )


_SKILL_DURATION_TOTAL_WORDS = frozenset(
    {"total", "overall", "professional", "work", "umumi", "is", "pesekar"}
)


def find_skill_specific_duration_mention(text: str) -> tuple[str, float] | None:
    """The (skill, years) a request names when it expresses skill-SPECIFIC
    experience duration ("N years of experience IN skill X"), e.g.
    "Pythonda 5 il təcrübəsi" or "Python üzrə 5 il təcrübəsi". Returns
    ``None`` when no such shape is present.

    ``CandidateProfile`` has no evidence linking a ``SkillItem`` to a
    specific ``EmploymentItem`` date range (see docs/DECISIONS.md D-027)
    — MEYAR can currently prove "has skill X" and "has N years of total
    career experience" as two independent facts, never "N years WITH
    skill X" as one. This function exists only to power an HR-safe
    clarification (never to silently combine a skill mention with total
    experience) and to let the shared precheck below reject the shape
    uniformly regardless of phrasing."""
    folded = fold_az_ascii(text)
    for pattern in _SKILL_DURATION_PATTERNS:
        match = pattern.search(folded)
        if not match:
            continue
        skill = match.group("skill")
        if _canonical(skill) in _SKILL_DURATION_TOTAL_WORDS:
            continue
        return skill, float(match.group("years").replace(",", "."))
    return None


def _skill_duration_is_unsupported(text: str) -> bool:
    return find_skill_specific_duration_mention(text) is not None


def explicit_total_experience_years(text: str) -> set[float]:
    values: set[float] = set()
    folded = fold_az_ascii(text)
    for pattern in _TOTAL_EXPERIENCE_PATTERNS:
        for match in pattern.finditer(folded):
            values.add(float(match.group("years").replace(",", ".")))
    return values


def explicit_result_limits(text: str) -> set[int]:
    values: set[int] = set()
    folded = fold_az_ascii(text)
    for pattern in _RESULT_LIMIT_PATTERNS:
        values.update(int(match.group("count")) for match in pattern.finditer(folded))
    return values


def precheck_natural_language_request(text: str) -> None:
    stripped = text.strip()
    if not stripped:
        raise PlannerPolicyError(PlannerOutcome.AMBIGUOUS_REQUEST, PlannerReasonCode.EMPTY_REQUEST)
    if len(text) > MAX_NATURAL_LANGUAGE_REQUEST_LENGTH:
        raise PlannerPolicyError(
            PlannerOutcome.VALIDATION_FAILURE, PlannerReasonCode.REQUEST_TOO_LONG
        )
    if not text.translate(_BENIGN_WHITESPACE_CONTROL_CHARS).isprintable():
        raise PlannerPolicyError(
            PlannerOutcome.VALIDATION_FAILURE,
            PlannerReasonCode.REQUEST_CONTROL_CHARACTERS,
        )
    if find_prohibited_term(text):
        raise PlannerPolicyError(
            PlannerOutcome.PROHIBITED_REQUEST, PlannerReasonCode.PROTECTED_CRITERION
        )

    reasons: list[PlannerReasonCode] = []
    # Skill duration and language proficiency are supported through the
    # shared source-bound semantic layer.  They used to be rejected here
    # because the older flat RequiredFilters schema could not represent them.
    if _matches_any(text, _IDENTITY_PATTERNS):
        reasons.append(PlannerReasonCode.IDENTITY_SEARCH_UNSUPPORTED)
    if _has_custom_search_weighting(text):
        reasons.append(PlannerReasonCode.CUSTOM_WEIGHTING_UNSUPPORTED)
    if _matches_any(text, _SALARY_PATTERNS):
        reasons.append(PlannerReasonCode.SALARY_FILTER_UNSUPPORTED)
    if _matches_any(text, _LOCATION_PATTERNS):
        reasons.append(PlannerReasonCode.LOCATION_FILTER_UNSUPPORTED)
    if _matches_any(text, _PROJECT_DURATION_PATTERNS):
        reasons.append(PlannerReasonCode.PROJECT_DURATION_UNSUPPORTED)
    if _matches_any(text, _PROMPT_INJECTION_PATTERNS):
        reasons.append(PlannerReasonCode.PROMPT_INJECTION_UNSUPPORTED)

    limits = explicit_result_limits(text)
    if any(limit > MAX_SEARCH_LIMIT for limit in limits):
        reasons.append(PlannerReasonCode.RESULT_LIMIT_OUT_OF_RANGE)
    if reasons:
        raise PlannerPolicyError(PlannerOutcome.UNSUPPORTED_SEMANTICS, *reasons)


def _filter_values(draft: PlannerDraft) -> list[tuple[str, str, bool]]:
    values: list[tuple[str, str, bool]] = []
    for is_required, filters in (
        (True, draft.required_filters),
        (False, draft.preferred_filters),
    ):
        for category in ("skills", "certifications", "languages", "education"):
            values.extend((category, value, is_required) for value in getattr(filters, category))
    return values


def _value_variants(category: str, value: str) -> tuple[str, ...]:
    if category == "certifications":
        return tuple(sorted(accepted_certification_terms(value)))
    if category == "languages":
        for english_name, variants in _LANGUAGE_ALIASES.items():
            aliases = (english_name, *variants)
            if set(_canonical_variants(value)).intersection(
                canonical for alias in aliases for canonical in _canonical_variants(alias)
            ):
                return variants
    return (value,)


_MIN_SUFFIX_TOLERANT_VALUE_LENGTH = 3


def _value_supported_by_request(category: str, value: str, request: str) -> bool:
    for canonicalize in _CANONICALIZERS:
        canonical_request = canonicalize(request)
        for variant in _value_variants(category, value):
            escaped = re.escape(canonicalize(variant))
            if re.search(
                rf"(?<![{_BOUNDARY_WORD}]){escaped}(?![{_BOUNDARY_WORD}])",
                canonical_request,
            ):
                return True
            if len(variant) >= _MIN_SUFFIX_TOLERANT_VALUE_LENGTH and re.search(
                rf"(?<![{_BOUNDARY_WORD}]){escaped}"
                rf"(?:{'|'.join(_AZ_LOCATIVE_ABLATIVE_SUFFIXES)})"
                rf"(?![{_BOUNDARY_WORD}])",
                canonical_request,
            ):
                return True
    return False


def _closest_marker_distance(
    text: str,
    start: int,
    end: int,
    markers: tuple[str, ...],
    canonicalize: Callable[[str], str],
) -> int | None:
    closest: int | None = None
    for marker in markers:
        canonical_marker = canonicalize(marker)
        pattern = re.compile(
            rf"(?<![{_BOUNDARY_WORD}]){re.escape(canonical_marker)}"
            rf"(?![{_BOUNDARY_WORD}])"
        )
        for match in pattern.finditer(text):
            marker_start, marker_end = match.span()
            between = text[min(end, marker_end) : max(start, marker_start)]
            if re.search(r"[,;.!?\n]|\b(?:and|but|və|amma)\b", between):
                continue
            distance = max(start - marker_end, marker_start - end, 0)
            if distance <= 70 and (closest is None or distance < closest):
                closest = distance
    return closest


def _contains_marker(text: str, markers: tuple[str, ...]) -> bool:
    return any(
        re.search(
            rf"(?<![{_BOUNDARY_WORD}]){re.escape(canonicalize(marker))}"
            rf"(?![{_BOUNDARY_WORD}])",
            canonicalize(text),
        )
        for canonicalize in _CANONICALIZERS
        for marker in markers
    )


def _intent_near_normalized_spans(
    normalized: str,
    spans: list[tuple[int, int]],
    canonicalize: Callable[[str], str],
) -> tuple[bool, bool]:
    required_distances = [
        distance
        for start, end in spans
        if (
            distance := _closest_marker_distance(
                normalized, start, end, _REQUIRED_MARKERS, canonicalize
            )
        )
        is not None
    ]
    preferred_distances = [
        distance
        for start, end in spans
        if (
            distance := _closest_marker_distance(
                normalized, start, end, _PREFERRED_MARKERS, canonicalize
            )
        )
        is not None
    ]
    required_distance = min(required_distances, default=None)
    preferred_distance = min(preferred_distances, default=None)
    required = required_distance is not None and (
        preferred_distance is None or required_distance <= preferred_distance
    )
    preferred = preferred_distance is not None and (
        required_distance is None or preferred_distance < required_distance
    )
    return required, preferred


def _term_spans(
    normalized: str,
    terms: tuple[str, ...] | list[str],
    canonicalize: Callable[[str], str],
) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for term in terms:
        canonical_term = canonicalize(term)
        spans.extend(
            match.span()
            for match in re.finditer(
                rf"(?<![{_BOUNDARY_WORD}]){re.escape(canonical_term)}"
                rf"(?![{_BOUNDARY_WORD}])",
                normalized,
            )
        )
    return spans


def _intent_near_terms(request: str, terms: tuple[str, ...] | list[str]) -> tuple[bool, bool]:
    required = False
    preferred = False
    for canonicalize in _CANONICALIZERS:
        normalized = canonicalize(request)
        current_required, current_preferred = _intent_near_normalized_spans(
            normalized,
            _term_spans(normalized, terms, canonicalize),
            canonicalize,
        )
        required = required or current_required
        preferred = preferred or current_preferred
    return required, preferred


def _value_intent(request: str, category: str, value: str) -> tuple[bool, bool]:
    return _intent_near_terms(request, _value_variants(category, value))


def _experience_intent(request: str, value: float) -> tuple[bool, bool]:
    representations = {str(value), str(int(value)) if value.is_integer() else str(value)}
    return _intent_near_terms(request, list(representations))


def _has_structured_filters(draft: PlannerDraft) -> bool:
    for filters in (draft.required_filters, draft.preferred_filters):
        if any(
            (
                filters.skills,
                filters.certifications,
                filters.languages,
                filters.education,
                filters.min_total_experience_years is not None,
                filters.skill_experience,
                filters.domain_experience,
                filters.language_levels,
            )
        ):
            return True
    return False


def derive_search_mode(draft: PlannerDraft) -> SearchMode:
    has_structured = _has_structured_filters(draft)
    has_semantic = bool((draft.semantic_query or "").strip())
    if has_structured and has_semantic:
        return SearchMode.HYBRID
    if has_semantic:
        return SearchMode.SEMANTIC_ONLY
    if has_structured:
        return SearchMode.STRUCTURED_ONLY
    raise PlannerPolicyError(PlannerOutcome.AMBIGUOUS_REQUEST, PlannerReasonCode.NO_SEARCH_CRITERIA)


def _validate_filter_fidelity(draft: PlannerDraft, request: str) -> None:
    has_required_marker = _contains_marker(request, _REQUIRED_MARKERS)
    required = draft.required_filters
    has_required_filter = any(
        (
            required.skills,
            required.certifications,
            required.languages,
            required.education,
            required.min_total_experience_years is not None,
        )
    )
    if has_required_marker and not has_required_filter:
        raise PlannerPolicyError(
            PlannerOutcome.VALIDATION_FAILURE,
            PlannerReasonCode.MANDATORY_REQUIREMENT_DOWNGRADED,
        )

    for category, value, is_required in _filter_values(draft):
        if not _value_supported_by_request(category, value, request):
            raise PlannerPolicyError(
                PlannerOutcome.VALIDATION_FAILURE,
                PlannerReasonCode.STRUCTURED_FILTER_NOT_SUPPORTED_BY_REQUEST,
            )
        required_near, preferred_near = _value_intent(request, category, value)
        if not is_required and required_near:
            raise PlannerPolicyError(
                PlannerOutcome.VALIDATION_FAILURE,
                PlannerReasonCode.MANDATORY_REQUIREMENT_DOWNGRADED,
            )
        if is_required and preferred_near and not required_near:
            raise PlannerPolicyError(
                PlannerOutcome.VALIDATION_FAILURE,
                PlannerReasonCode.PREFERRED_REQUIREMENT_UPGRADED,
            )


def _validate_experience_fidelity(draft: PlannerDraft, request: str) -> None:
    explicit = explicit_total_experience_years(request)
    drafted: list[tuple[float, bool]] = []
    if draft.required_filters.min_total_experience_years is not None:
        drafted.append((draft.required_filters.min_total_experience_years, True))
    if draft.preferred_filters.min_total_experience_years is not None:
        drafted.append((draft.preferred_filters.min_total_experience_years, False))

    for value, is_required in drafted:
        if value not in explicit:
            raise PlannerPolicyError(
                PlannerOutcome.VALIDATION_FAILURE,
                PlannerReasonCode.NUMERIC_EXPERIENCE_NOT_SUPPORTED_BY_REQUEST,
            )
        required_near, preferred_near = _experience_intent(request, value)
        if not is_required and required_near:
            raise PlannerPolicyError(
                PlannerOutcome.VALIDATION_FAILURE,
                PlannerReasonCode.MANDATORY_REQUIREMENT_DOWNGRADED,
            )
        if is_required and preferred_near and not required_near:
            raise PlannerPolicyError(
                PlannerOutcome.VALIDATION_FAILURE,
                PlannerReasonCode.PREFERRED_REQUIREMENT_UPGRADED,
            )

    if explicit and not any(value in explicit for value, _required in drafted):
        raise PlannerPolicyError(
            PlannerOutcome.VALIDATION_FAILURE,
            PlannerReasonCode.NUMERIC_EXPERIENCE_OMITTED,
        )


def _validate_limit_fidelity(draft: PlannerDraft, request: str) -> tuple[int, bool]:
    explicit = explicit_result_limits(request)
    if explicit:
        if draft.requested_limit is None:
            raise PlannerPolicyError(
                PlannerOutcome.VALIDATION_FAILURE, PlannerReasonCode.RESULT_LIMIT_OMITTED
            )
        if draft.requested_limit not in explicit:
            raise PlannerPolicyError(
                PlannerOutcome.VALIDATION_FAILURE,
                PlannerReasonCode.RESULT_LIMIT_NOT_SUPPORTED_BY_REQUEST,
            )
        return draft.requested_limit, False

    if draft.requested_limit not in (None, DEFAULT_NL_SEARCH_LIMIT):
        raise PlannerPolicyError(
            PlannerOutcome.VALIDATION_FAILURE,
            PlannerReasonCode.RESULT_LIMIT_NOT_SUPPORTED_BY_REQUEST,
        )
    return DEFAULT_NL_SEARCH_LIMIT, True


def _postcheck_protected_and_unsupported(draft: PlannerDraft) -> None:
    execution_texts = [value for _category, value, _required in _filter_values(draft)]
    if draft.semantic_query:
        execution_texts.append(draft.semantic_query)
    if find_prohibited_term(*execution_texts):
        raise PlannerPolicyError(
            PlannerOutcome.PROHIBITED_REQUEST, PlannerReasonCode.PROTECTED_CRITERION
        )
    if draft.semantic_query and (
        _matches_any(draft.semantic_query, _PROMPT_INJECTION_PATTERNS)
        or _matches_any(draft.semantic_query, _IDENTITY_PATTERNS)
    ):
        raise PlannerPolicyError(
            PlannerOutcome.UNSUPPORTED_SEMANTICS, PlannerReasonCode.SEMANTIC_QUERY_UNSAFE
        )
    if draft.semantic_query and _has_custom_search_weighting(draft.semantic_query):
        raise PlannerPolicyError(
            PlannerOutcome.UNSUPPORTED_SEMANTICS,
            PlannerReasonCode.CUSTOM_WEIGHTING_UNSUPPORTED,
        )


def _validate_semantic_fidelity(draft: PlannerDraft, request: str) -> None:
    """Reject model-added semantic facts and required→ranking weakening.

    This deliberately small MVP guard requires semantic content words to be
    present in the original request. The model may select/reorder request
    language, but may not translate it into new skills, certifications, or
    professional facts. Slice 8 semantic relevance is never a hard gate, so
    explicitly mandatory conceptual intent is non-executable.
    """
    if not draft.semantic_query:
        return
    supported = False
    required_near = False
    preferred_near = False
    for canonicalize in _CANONICALIZERS:
        normalized_semantic = canonicalize(draft.semantic_query)
        semantic_tokens = re.findall(r"[^\W_]+", normalized_semantic)
        normalized_request = canonicalize(request)
        request_tokens = set(re.findall(r"[^\W_]+", normalized_request))
        if not semantic_tokens or any(token not in request_tokens for token in semantic_tokens):
            continue
        supported = True
        current_required, current_preferred = _intent_near_normalized_spans(
            normalized_request,
            _term_spans(normalized_request, semantic_tokens, canonicalize),
            canonicalize,
        )
        required_near = required_near or current_required
        preferred_near = preferred_near or current_preferred

    if not supported:
        raise PlannerPolicyError(
            PlannerOutcome.VALIDATION_FAILURE,
            PlannerReasonCode.SEMANTIC_QUERY_NOT_SUPPORTED_BY_REQUEST,
        )
    if required_near and not preferred_near:
        raise PlannerPolicyError(
            PlannerOutcome.UNSUPPORTED_SEMANTICS,
            PlannerReasonCode.MANDATORY_REQUIREMENT_DOWNGRADED,
        )


def convert_planner_draft(
    draft: PlannerDraft,
    *,
    natural_language_request: str,
    as_of_date: date,
    embedding_config: EmbeddingSearchConfig,
) -> CandidateSearchRequest:
    """Pure draft + trusted context -> validated Slice 8 request."""
    precheck_natural_language_request(natural_language_request)
    # Complex filters are executable only when produced by the shared
    # source-occurrence semantic boundary in planner_service.  A free model
    # draft cannot bypass that authority merely because the schema can now
    # represent the evaluator's capability.
    if any(
        (
            draft.required_filters.skill_experience,
            draft.required_filters.domain_experience,
            draft.required_filters.language_levels,
            draft.preferred_filters.skill_experience,
            draft.preferred_filters.domain_experience,
            draft.preferred_filters.language_levels,
        )
    ):
        raise PlannerPolicyError(
            PlannerOutcome.VALIDATION_FAILURE,
            PlannerReasonCode.STRUCTURED_FILTER_NOT_SUPPORTED_BY_REQUEST,
        )
    if draft.unsupported_reason_codes:
        # The model itself declined this interpretation — tag it so the
        # presentation layer can tell this apart from a deterministic
        # product-policy rejection below. See docs/DECISIONS.md D-025.
        raise PlannerPolicyError(
            PlannerOutcome.UNSUPPORTED_SEMANTICS,
            PlannerReasonCode.MODEL_DECLINED_INTERPRETATION,
            *draft.unsupported_reason_codes,
        )
    _postcheck_protected_and_unsupported(draft)
    _validate_semantic_fidelity(draft, natural_language_request)
    _validate_filter_fidelity(draft, natural_language_request)
    _validate_experience_fidelity(draft, natural_language_request)
    limit, _used_default = _validate_limit_fidelity(draft, natural_language_request)
    mode = derive_search_mode(draft)
    semantic_query = (draft.semantic_query or "").strip() or None
    uses_experience = (
        draft.required_filters.min_total_experience_years is not None
        or draft.preferred_filters.min_total_experience_years is not None
    )

    try:
        return CandidateSearchRequest(
            mode=mode,
            required_filters=draft.required_filters,
            preferred_filters=draft.preferred_filters,
            semantic_query=semantic_query,
            embedding_config=(
                embedding_config if mode in (SearchMode.SEMANTIC_ONLY, SearchMode.HYBRID) else None
            ),
            as_of_date=as_of_date if uses_experience else None,
            limit=limit,
            structured_weight=DEFAULT_STRUCTURED_WEIGHT,
            semantic_weight=DEFAULT_SEMANTIC_WEIGHT,
        )
    except ProhibitedCriterionError as exc:
        raise PlannerPolicyError(
            PlannerOutcome.PROHIBITED_REQUEST, PlannerReasonCode.PROTECTED_CRITERION
        ) from exc
    except ValidationError as exc:
        raise PlannerPolicyError(
            PlannerOutcome.VALIDATION_FAILURE,
            PlannerReasonCode.CANDIDATE_SEARCH_VALIDATION_FAILED,
        ) from exc


def build_interpretation_summary(
    draft: PlannerDraft | None, request: CandidateSearchRequest | None
) -> PlanInterpretationSummary:
    if draft is None:
        return PlanInterpretationSummary()

    def labels(prefix: str, filters: RequiredFilters | PreferredFilters) -> list[str]:
        output: list[str] = []
        for category in ("skills", "certifications", "languages", "education"):
            output.extend(f"{prefix}.{category}:{value}" for value in getattr(filters, category))
        for category in ("skill_experience", "domain_experience"):
            output.extend(
                f"{prefix}.{category}:{item.value}:{item.min_years}"
                for item in getattr(filters, category)
            )
        output.extend(
            f"{prefix}.language_levels:{item.value}:{item.required_level}"
            for item in filters.language_levels
        )
        years = filters.min_total_experience_years
        if years is not None:
            output.append(f"{prefix}.min_total_experience_years:{years}")
        return output

    return PlanInterpretationSummary(
        required_criteria=labels("required", draft.required_filters),
        preferred_criteria=labels("preferred", draft.preferred_filters),
        semantic_intent_present=bool((draft.semantic_query or "").strip()),
        selected_mode=request.mode if request else None,
        result_limit=request.limit if request else draft.requested_limit,
        used_default_limit=(request is not None and draft.requested_limit is None),
    )


# ---------------------------------------------------------------------------
# Conservative deterministic fast path (D-026, refined by D-027). A
# narrowly-scoped, whole-clause-anchored pattern set for a handful of
# explicit HR search intents that already have exact, evidence-provable
# SearchPlan representations — skills, languages, certifications, and
# TOTAL (career-wide) experience years, singly or combined with a simple
# "və" conjunction. This is NOT a general NLP engine: every pattern below
# matches an ENTIRE clause start-to-end (anchored `^...$`), so a request
# containing anything this module cannot confidently attribute to a known
# concept or a fixed, bounded set of connector/boilerplate words never
# partially matches — `try_deterministic_intent_parse` returns None and
# the caller MUST fall back to the LLM planner rather than execute a
# narrower search than what was actually asked. Skill-SPECIFIC experience
# duration (e.g. "Pythonda 5 il təcrübəsi", "Python üzrə 5 il təcrübəsi")
# is deliberately absent here — see the note above
# `find_skill_specific_duration_mention` and docs/DECISIONS.md D-027 for
# why, and `meyar.ui.router`'s clarification flow for how a user can
# explicitly opt into the (weaker, but now honest) skill + total-
# experience alternative. See docs/DECISIONS.md D-026.
# ---------------------------------------------------------------------------

_DET_TERM = rf"[{_WORD}/-]{{1,60}}"
_DET_TERM_LIST = (
    rf"(?P<terms>{_DET_TERM}(?:\s*,\s*{_DET_TERM}|\s+və\s+{_DET_TERM}|\s+and\s+{_DET_TERM}){{0,4}})"
)
# Deliberately tiny, fixed vocabularies — not free-text glue matching.
_DET_LEADING = r"(?:mənə\s+)?"
_DET_TRAILING = (
    r"(?:\s+(?:namizədləri|namizədi|namizədlər|namizəd|göstər|tap\w*|"
    r"siyahısını|siyahıla))*"
)

_DET_SKILL_PATTERN = _az_pattern(rf"(?i)^{_DET_LEADING}{_DET_TERM_LIST}\s+bilən{_DET_TRAILING}\s*$")
_DET_LANGUAGE_PATTERN = _az_pattern(
    rf"(?i)^{_DET_LEADING}(?P<term>{_DET_TERM})\s+dili\s+(?:bilən|olan){_DET_TRAILING}\s*$"
)
_DET_CERT_PATTERN = _az_pattern(
    rf"(?i)^{_DET_LEADING}(?P<term>{_DET_TERM})\s+sertifikatı\s+olan{_DET_TRAILING}\s*$"
)
# "N il [ümumi/peşəkar] təcrübəsi olan" — the duration comes first.
_DET_TOTAL_EXPERIENCE_PATTERN = _az_pattern(
    rf"(?i)^{_DET_LEADING}(?:minimum\s+|ən\s+az[ıi]?\s+)?"
    rf"(?P<years>\d+(?:[.,]\d+)?)\s*il\s+(?:(?:ümumi|peşəkar)\s+)?(?:iş\s+)?"
    rf"təcrübəsi\s+olan{_DET_TRAILING}\s*$"
)
# "[ümumi/peşəkar] iş təcrübəsi [ən az/minimum] N il olan" — the same
# total-experience intent with "təcrübəsi" stated first, e.g. the
# explicit-separation phrasing "ümumi iş təcrübəsi ən az 5 il olan".
_DET_TOTAL_EXPERIENCE_REVERSED_PATTERN = _az_pattern(
    rf"(?i)^{_DET_LEADING}(?:(?:ümumi|peşəkar)\s+)?(?:iş\s+)?təcrübəsi\s+"
    rf"(?:minimum\s+|ən\s+az[ıi]?\s+)?(?P<years>\d+(?:[.,]\d+)?)\s*il\s+"
    rf"olan{_DET_TRAILING}\s*$"
)
# Deliberately NOT a pattern here: "Xda N il təcrübəsi olan" (a skill
# with a locative/ablative suffix immediately followed by a duration
# mention, e.g. "pythonda 5 il təcrübəsi olan") is skill-SPECIFIC
# duration — CandidateProfile cannot prove it (see docs/DECISIONS.md
# D-027) and it is rejected upstream by precheck_natural_language_request
# (find_skill_specific_duration_mention) before this module is ever
# reached, exactly like "Python üzrə 5 il təcrübəsi". It must not be
# silently read here as "skill + total experience" — that combination is
# only ever produced after an explicit user confirmation (see the /ui
# clarification flow in meyar.ui.router).

_DET_TERM_LIST_SPLIT = _az_pattern(r"(?i)\s*,\s*|\s+və\s+|\s+and\s+")
_DET_CLAUSE_SPLIT = _az_pattern(r"(?i)\s+və\s+")
_MAX_DETERMINISTIC_CLAUSES = 4

_LANGUAGE_CANONICAL_NAMES: dict[str, str] = {
    "english": "English",
    "russian": "Russian",
    "azerbaijani": "Azerbaijani",
    "turkish": "Turkish",
}


def _split_term_list(raw: str) -> list[str]:
    return [term.strip() for term in _DET_TERM_LIST_SPLIT.split(raw) if term.strip()]


def _canonicalize_deterministic_language(term: str) -> str | None:
    """Only the small, already-supported language catalog
    (_LANGUAGE_ALIASES) is recognized — an unlisted language falls back
    to the LLM rather than the fast path inventing a new one."""
    for canonical_key, variants in _LANGUAGE_ALIASES.items():
        aliases = (canonical_key, *variants)
        if set(_canonical_variants(term)).intersection(
            canonical for alias in aliases for canonical in _canonical_variants(alias)
        ):
            return _LANGUAGE_CANONICAL_NAMES[canonical_key]
    return None


def _parse_single_deterministic_clause(clause: str) -> PlannerDraft | None:
    clause = clause.strip().rstrip(".!?").strip()
    if not clause:
        return None

    match = _DET_TOTAL_EXPERIENCE_PATTERN.match(clause)
    if match is None:
        match = _DET_TOTAL_EXPERIENCE_REVERSED_PATTERN.match(clause)
    if match:
        years = float(match.group("years").replace(",", "."))
        return PlannerDraft(required_filters=RequiredFilters(min_total_experience_years=years))

    match = _DET_LANGUAGE_PATTERN.match(clause)
    if match:
        canonical = _canonicalize_deterministic_language(match.group("term"))
        if canonical is None:
            return None
        return PlannerDraft(required_filters=RequiredFilters(languages=[canonical]))

    match = _DET_CERT_PATTERN.match(clause)
    if match:
        return PlannerDraft(required_filters=RequiredFilters(certifications=[match.group("term")]))

    match = _DET_SKILL_PATTERN.match(clause)
    if match:
        terms = _split_term_list(match.group("terms"))
        if not terms:
            return None
        return PlannerDraft(required_filters=RequiredFilters(skills=terms))

    return None


def _merge_deterministic_clauses(parts: list[PlannerDraft]) -> PlannerDraft | None:
    skills: list[str] = []
    certifications: list[str] = []
    languages: list[str] = []
    min_years: float | None = None
    for part in parts:
        skills.extend(part.required_filters.skills)
        certifications.extend(part.required_filters.certifications)
        languages.extend(part.required_filters.languages)
        part_years = part.required_filters.min_total_experience_years
        if part_years is not None:
            if min_years is not None and min_years != part_years:
                # Two conflicting experience mentions — ambiguous, must
                # not silently pick one. Fall back to the LLM.
                return None
            min_years = part_years
    if not (skills or certifications or languages or min_years is not None):
        return None
    return PlannerDraft(
        required_filters=RequiredFilters(
            skills=skills,
            certifications=certifications,
            languages=languages,
            min_total_experience_years=min_years,
        )
    )


def try_deterministic_intent_parse(text: str) -> PlannerDraft | None:
    """Conservative fast-path extraction for explicit, unambiguous HR
    search intents (see module-level note above). Returns a
    ``PlannerDraft`` only when the ENTIRE request is confidently
    accounted for; returns ``None`` for anything else, including a
    request that is only partially understood — callers must treat
    ``None`` as "use the LLM planner", never as "search on the part I
    did understand". Assumes ``precheck_natural_language_request`` has
    already passed for ``text`` (this function does not repeat the
    control-character/prohibited-term checks)."""
    stripped = text.strip()
    if not stripped or len(stripped) > MAX_NATURAL_LANGUAGE_REQUEST_LENGTH:
        return None
    # A preferred/required distinction, salary/location/identity mentions,
    # etc. need the fidelity nuance the LLM + existing validators provide
    # — the fast path only ever produces MUST_HAVE filters, so any
    # preferred-marker vocabulary means this is not a safe fit.
    if _contains_marker(stripped, _PREFERRED_MARKERS):
        return None

    # All fixed-vocabulary patterns above were compiled ASCII-folded (see
    # _az_pattern) — fold the text the same way so either spelling matches.
    folded = fold_az_ascii(stripped)

    single = _parse_single_deterministic_clause(folded)
    if single is not None:
        return single

    clauses = _DET_CLAUSE_SPLIT.split(folded)
    if not (2 <= len(clauses) <= _MAX_DETERMINISTIC_CLAUSES):
        return None
    parsed = [_parse_single_deterministic_clause(clause) for clause in clauses]
    if any(part is None for part in parsed):
        return None
    return _merge_deterministic_clauses([part for part in parsed if part is not None])
