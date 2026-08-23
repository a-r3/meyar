"""Deterministic ``meyar-search-planner-v1`` policy.

The LLM produces a constrained draft. This module—not the model—checks
semantic preservation, derives the Slice 8 mode, injects trusted runtime
configuration, and constructs the final ``CandidateSearchRequest``.
There is no database, provider call, randomness, or wall-clock access here.
"""

import re
from datetime import date

from pydantic import ValidationError

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

_SKILL_DURATION_PATTERNS = (
    re.compile(
        rf"(?i)\b\d+(?:\.\d+)?\s*(?:years?|yrs?)\s+(?:of\s+)?"
        rf"(?P<skill>[{_WORD}/-]{{1,60}})\s+experience\b"
    ),
    re.compile(
        rf"(?i)\b(?P<skill>[{_WORD}/-]{{1,60}})\s+experience\s+(?:of|for)\s+"
        r"\d+(?:\.\d+)?\s*(?:years?|yrs?)\b"
    ),
    re.compile(
        rf"(?i)\b(?P<skill>[{_WORD}/-]{{1,60}})\s+(?:üzrə|ilə)\s+"
        r"(?:ən\s+azı\s+)?\d+(?:[.,]\d+)?\s*il\s+(?:iş\s+)?təcrüb"
    ),
    re.compile(
        rf"(?i)\b\d+(?:[.,]\d+)?\s*il\s+(?P<skill>[{_WORD}/-]{{1,60}})\s+təcrüb"
    ),
)
_TOTAL_EXPERIENCE_PATTERNS = (
    re.compile(
        r"(?i)\b(?P<years>\d+(?:\.\d+)?)\s*(?:years?|yrs?)\s+"
        r"(?:of\s+)?(?:(?:total|overall|professional)\s+)?(?:work\s+)?experience\b"
    ),
    re.compile(
        r"(?i)\b(?P<years>\d+(?:[.,]\d+)?)\s*il\s+"
        r"(?:(?:ümumi|peşəkar)\s+)?(?:iş\s+)?təcrüb"
    ),
)
_RESULT_LIMIT_PATTERNS = (
    re.compile(r"(?i)\b(?P<count>\d{1,4})\s+(?:best\s+)?candidates?\b"),
    re.compile(r"(?i)\b(?P<count>\d{1,4})\s+namizəd(?:i|ə|lər|ləri)?\b"),
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
    re.compile(rf"(?i)\b(?:{_LANGUAGE_TERMS})\b.{{0,40}}\b(?:{_PROFICIENCY_TERMS})\b"),
    re.compile(rf"(?i)\b(?:{_PROFICIENCY_TERMS})\b.{{0,40}}\b(?:{_LANGUAGE_TERMS})\b"),
)

_IDENTITY_PATTERNS = (
    re.compile(
        r"(?i)\b(?:candidate\s+)?(?:name|named|full[_ -]?name|email|e-mail|phone|"
        r"telephone|contact|adlı|adı|e-poçt|emaili|telefon)\b"
    ),
    re.compile(
        rf"(?i)^\s*(?:find|show|search\s+for)\s+(?:a\s+)?[{_WORD}'-]{{2,40}}\s*[.!?]*$"
    ),
)
_EXPLICIT_CUSTOM_WEIGHT_PATTERNS = (
    re.compile(r"(?i)\bprioriti[sz]e\b.{0,80}\bover\s+everything\b"),
    re.compile(r"(?i)\bfocus\s+mostly\b"),
    re.compile(r"(?i)\bhər\s+şeydən\s+üstün\b"),
)
_SEARCH_WEIGHT_COMPONENT_PATTERNS = (
    re.compile(r"(?i)\bsemantics?\b|\bsemantic(?:\s+(?:search|score|relevance|results?))?\b"),
    re.compile(r"(?i)\bstructured(?:\s+(?:search|score|relevance|results?))?\b"),
    re.compile(r"(?i)\bsemantik\b|\bstruktur(?:laşdırılmış)?\b"),
)
_SEARCH_WEIGHT_CONTROL_PATTERNS = (
    re.compile(
        r"(?i)(?:\bweights?\b|\bweighting\b|\bpercent(?:age)?\b|%|"
        r"\bmore\s+important\b|\bprioriti[sz]e\b)"
    ),
    re.compile(
        r"(?i)(?:\bçəki\b|\bfaiz\b|%|\bprioritet\b|\bdaha\s+çox(?:\s+çəki)?\b|"
        r"\büstün\s+tut\b)"
    ),
)
_SALARY_PATTERNS = (re.compile(r"(?i)\b(?:salary|compensation|maaş|əmək\s+haqqı)\b"),)
_LOCATION_PATTERNS = (
    re.compile(
        r"(?i)\b(?:location|located|based\s+in|resident\s+in|living\s+in|"
        r"yerləşən|yaşayan|məkan)\b"
    ),
)
_PROJECT_DURATION_PATTERNS = (
    re.compile(r"(?i)\b\d+(?:\.\d+)?\s*(?:years?|months?)\s+(?:on|in)\s+.+projects?\b"),
    re.compile(r"(?i)\b.+layihə(?:si|ləri)?ndə\s+\d+(?:[.,]\d+)?\s*(?:il|ay)\b"),
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
    def __init__(
        self, outcome: PlannerOutcome, *reason_codes: PlannerReasonCode
    ) -> None:
        self.outcome = outcome
        self.reason_codes = list(dict.fromkeys(reason_codes))
        super().__init__(",".join(code.value for code in self.reason_codes))


def _canonical(text: str) -> str:
    return " ".join(text.casefold().split())


def _matches_any(text: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def _has_custom_search_weighting(text: str) -> bool:
    if _matches_any(text, _EXPLICIT_CUSTOM_WEIGHT_PATTERNS):
        return True
    return _matches_any(text, _SEARCH_WEIGHT_COMPONENT_PATTERNS) and _matches_any(
        text, _SEARCH_WEIGHT_CONTROL_PATTERNS
    )


def _skill_duration_is_unsupported(text: str) -> bool:
    total_words = {"total", "overall", "professional", "work", "ümumi", "iş", "peşəkar"}
    for pattern in _SKILL_DURATION_PATTERNS:
        match = pattern.search(text)
        if match and _canonical(match.group("skill")) not in total_words:
            return True
    return False


def explicit_total_experience_years(text: str) -> set[float]:
    values: set[float] = set()
    for pattern in _TOTAL_EXPERIENCE_PATTERNS:
        for match in pattern.finditer(text):
            values.add(float(match.group("years").replace(",", ".")))
    return values


def explicit_result_limits(text: str) -> set[int]:
    values: set[int] = set()
    for pattern in _RESULT_LIMIT_PATTERNS:
        values.update(int(match.group("count")) for match in pattern.finditer(text))
    return values


def precheck_natural_language_request(text: str) -> None:
    stripped = text.strip()
    if not stripped:
        raise PlannerPolicyError(PlannerOutcome.AMBIGUOUS_REQUEST, PlannerReasonCode.EMPTY_REQUEST)
    if len(text) > MAX_NATURAL_LANGUAGE_REQUEST_LENGTH:
        raise PlannerPolicyError(
            PlannerOutcome.VALIDATION_FAILURE, PlannerReasonCode.REQUEST_TOO_LONG
        )
    if not text.isprintable():
        raise PlannerPolicyError(
            PlannerOutcome.VALIDATION_FAILURE,
            PlannerReasonCode.REQUEST_CONTROL_CHARACTERS,
        )
    if find_prohibited_term(text):
        raise PlannerPolicyError(
            PlannerOutcome.PROHIBITED_REQUEST, PlannerReasonCode.PROTECTED_CRITERION
        )

    reasons: list[PlannerReasonCode] = []
    if _skill_duration_is_unsupported(text):
        reasons.append(PlannerReasonCode.SKILL_SPECIFIC_EXPERIENCE_DURATION_UNSUPPORTED)
    if _matches_any(text, _LANGUAGE_PROFICIENCY_PATTERNS):
        reasons.append(PlannerReasonCode.LANGUAGE_PROFICIENCY_UNSUPPORTED)
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
    canonical = _canonical(value)
    if category == "languages":
        for english_name, variants in _LANGUAGE_ALIASES.items():
            if canonical == english_name or canonical in variants:
                return variants
    return (canonical,)


def _value_supported_by_request(category: str, value: str, request: str) -> bool:
    canonical_request = _canonical(request)
    return any(
        re.search(
            rf"(?<![{_BOUNDARY_WORD}]){re.escape(_canonical(variant))}"
            rf"(?![{_BOUNDARY_WORD}])",
            canonical_request,
        )
        for variant in _value_variants(category, value)
    )


def _closest_marker_distance(
    text: str, start: int, end: int, markers: tuple[str, ...]
) -> int | None:
    closest: int | None = None
    for marker in markers:
        pattern = re.compile(
            rf"(?<![{_BOUNDARY_WORD}]){re.escape(marker)}(?![{_BOUNDARY_WORD}])"
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
            rf"(?<![{_BOUNDARY_WORD}]){re.escape(marker)}(?![{_BOUNDARY_WORD}])",
            text,
        )
        for marker in markers
    )


def _intent_near_spans(
    request: str, spans: list[tuple[int, int]]
) -> tuple[bool, bool]:
    normalized = _canonical(request)
    required_distances = [
        distance
        for start, end in spans
        if (distance := _closest_marker_distance(
            normalized, start, end, _REQUIRED_MARKERS
        ))
        is not None
    ]
    preferred_distances = [
        distance
        for start, end in spans
        if (distance := _closest_marker_distance(
            normalized, start, end, _PREFERRED_MARKERS
        ))
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


def _value_intent(request: str, category: str, value: str) -> tuple[bool, bool]:
    normalized = _canonical(request)
    spans: list[tuple[int, int]] = []
    for variant in _value_variants(category, value):
        canonical_variant = _canonical(variant)
        spans.extend(
            match.span()
            for match in re.finditer(
                rf"(?<![{_BOUNDARY_WORD}]){re.escape(canonical_variant)}"
                rf"(?![{_BOUNDARY_WORD}])",
                normalized,
            )
        )
    return _intent_near_spans(request, spans)


def _experience_intent(request: str, value: float) -> tuple[bool, bool]:
    normalized = _canonical(request)
    representations = {str(value), str(int(value)) if value.is_integer() else str(value)}
    spans: list[tuple[int, int]] = []
    for representation in representations:
        start = normalized.find(representation)
        while start != -1:
            spans.append((start, start + len(representation)))
            start = normalized.find(representation, start + 1)
    return _intent_near_spans(request, spans)


def _has_structured_filters(draft: PlannerDraft) -> bool:
    for filters in (draft.required_filters, draft.preferred_filters):
        if any(
            (
                filters.skills,
                filters.certifications,
                filters.languages,
                filters.education,
                filters.min_total_experience_years is not None,
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
    raise PlannerPolicyError(
        PlannerOutcome.AMBIGUOUS_REQUEST, PlannerReasonCode.NO_SEARCH_CRITERIA
    )


def _validate_filter_fidelity(draft: PlannerDraft, request: str) -> None:
    has_required_marker = _contains_marker(_canonical(request), _REQUIRED_MARKERS)
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
    semantic_tokens = re.findall(r"[^\W_]+", _canonical(draft.semantic_query))
    request_tokens = set(re.findall(r"[^\W_]+", _canonical(request)))
    if not semantic_tokens or any(token not in request_tokens for token in semantic_tokens):
        raise PlannerPolicyError(
            PlannerOutcome.VALIDATION_FAILURE,
            PlannerReasonCode.SEMANTIC_QUERY_NOT_SUPPORTED_BY_REQUEST,
        )

    normalized_request = _canonical(request)
    spans: list[tuple[int, int]] = []
    for token in semantic_tokens:
        start = normalized_request.find(token)
        while start != -1:
            spans.append((start, start + len(token)))
            start = normalized_request.find(token, start + 1)
    required_near, preferred_near = _intent_near_spans(request, spans)
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
    if draft.unsupported_reason_codes:
        raise PlannerPolicyError(
            PlannerOutcome.UNSUPPORTED_SEMANTICS, *draft.unsupported_reason_codes
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
