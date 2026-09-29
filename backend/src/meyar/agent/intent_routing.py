"""Server-owned entry routing for the unified MEYAR HR composer.

This boundary answers only one high-level question: is the current message
clearly a vacancy/JD draft request, clearly a NEW candidate search, clearly
safe to leave to the bounded agent (current-result follow-ups, conversation)
or genuinely ambiguous between candidate search and vacancy analysis?  It
does not search, parse search filters, dispatch tools, mutate business data
or authorize anything proposed by the local model.  A forced search only
authorizes the already-existing SEARCH_CANDIDATES action with the user's own
text; filters are still derived by the existing validated search planner.

The implementation deliberately reuses the source-bound semantic requirement
analysis introduced for JD/search parity.  The small regular expressions here
recognize workflow/structure cues only; they are not a technology dictionary
and never construct criteria.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from pydantic import ValidationError

from meyar.agent.schemas import (
    MAX_AGENT_SEARCH_QUERY_LENGTH,
    MAX_CANDIDATE_REF,
    SourceOccurrence,
)
from meyar.agent.semantic_requirements import analyze_hr_text, count_token_value
from meyar.core.result_count import ResultCountState
from meyar.core.text import fold_az_ascii, normalize_azerbaijani_case

ENTRY_ROUTING_POLICY_VERSION = "agent-entry-routing-v4"


class AgentEntryRoute(StrEnum):
    FORCE_JOB_DRAFT = "FORCE_JOB_DRAFT"
    FORCE_CANDIDATE_SEARCH = "FORCE_CANDIDATE_SEARCH"
    FORCE_RESULT_LIMIT = "FORCE_RESULT_LIMIT"
    MODEL_ROUTED = "MODEL_ROUTED"
    CLARIFY_AMBIGUOUS = "CLARIFY_AMBIGUOUS"
    CLARIFY_JOB_SOURCE_REQUIRED = "CLARIFY_JOB_SOURCE_REQUIRED"
    CLARIFY_INPUT_STRUCTURE = "CLARIFY_INPUT_STRUCTURE"


class AgentRoutingSource(StrEnum):
    DETERMINISTIC_JD = "DETERMINISTIC_JD"
    DETERMINISTIC_SEARCH = "DETERMINISTIC_SEARCH"
    DETERMINISTIC_RESULT_CONTEXT = "DETERMINISTIC_RESULT_CONTEXT"
    MODEL = "MODEL"
    DETERMINISTIC_CLARIFICATION = "DETERMINISTIC_CLARIFICATION"


class AgentRoutedAction(StrEnum):
    DRAFT_JOB_CRITERIA = "DRAFT_JOB_CRITERIA"
    SEARCH_CANDIDATES = "SEARCH_CANDIDATES"
    REFINE_CANDIDATE_RESULTS = "REFINE_CANDIDATE_RESULTS"
    MODEL_ROUTED = "MODEL_ROUTED"
    CLARIFY = "CLARIFY"


@dataclass(frozen=True)
class AgentEntryRoutingResult:
    """Ephemeral, never persisted or audited as a whole.

    ``draft_source_start``/``draft_source_end`` are exact offsets into the
    original user message for FORCE_JOB_DRAFT only: the user-owned JD source
    after a deterministic workflow-instruction wrapper ("Vakansiyanı analiz
    et:").  They are never rewritten text and never reach the audit log.
    ``result_limit`` is the count of a count-only current-result follow-up.
    """

    route: AgentEntryRoute
    routing_source: AgentRoutingSource
    routed_action: AgentRoutedAction
    draft_source_start: int | None = None
    draft_source_end: int | None = None
    # FORCE_RESULT_LIMIT only: the count parsed from a count-only follow-up.
    result_limit: int | None = None

    def draft_source(self, message: str) -> str:
        """Exact user-owned JD source substring (whole message by default)."""
        if self.draft_source_start is None or self.draft_source_end is None:
            return message
        return message[self.draft_source_start : self.draft_source_end]


AMBIGUOUS_SEARCH_OR_JOB_COPY = (
    "Bu mətni namizəd axtarışı üçün istifadə etmək, yoxsa vakansiya meyarları "
    "kimi analiz etmək istəyirsiniz?"
)
JOB_SOURCE_REQUIRED_COPY = (
    "Vakansiya elanının mətnini və ya namizədə qoyulan tələbləri göndərin, "
    "sonra onları analiz edim."
)
INPUT_STRUCTURE_CLARIFICATION_COPY = (
    "Bu mətni təhlükəsiz analiz etmək üçün tələbləri daha qısa cümlələrə və ya "
    "ayrı sətirlərə bölüb yenidən göndərin."
)


def _fold(text: str) -> str:
    return fold_az_ascii(normalize_azerbaijani_case(text))



# A candidate/applicant noun on its own is HR-natural JD vocabulary ("Namizəd
# Python bilməlidir." reads exactly like "Candidate must know Python.") and is
# never, by itself, evidence of a search/list action (issue #79 PR81
# correction). It only becomes search evidence when it co-occurs with actual
# search/result-set semantics, which the regexes below already detect
# independently — so a candidate noun is deliberately excluded from the
# search-cue detector rather than listed and then combined with an action.
_RESULT_NOUN_RE = re.compile(
    r"\b(?:results?|netice\w*)\b",
    re.IGNORECASE,
)
_SEARCH_ACTION_RE = re.compile(
    r"\b(?:find|show|display|list|return|search(?:\s+for)?|tap\w*|goster\w*|"
    r"cixart\w*)\b",
    re.IGNORECASE,
)
_RESULT_CONTEXT_RE = re.compile(
    r"\b(?:bunlar\w*|onlardan|hemin\s+namized\w*|cari\s+netice\w*|ilk\s+\S+|"
    r"birinci\w*|ikinci\w*|ucuncu\w*|dorduncu\w*|besinci\w*|"
    r"first|second|third|fourth|fifth|among\s+(?:them|these|those)|"
    r"from\s+(?:them|these|those)|these\s+results?)\b|#\s*\d+",
    re.IGNORECASE,
)

# Explicit NEW-search imperative (issue #79 PR81 acceptance correction).
# Position-bound on purpose: an Azerbaijani imperative search verb ends the
# request ("... namizədləri göstər"), an English one starts it ("Find
# candidates ..."). A verb inside a requirement sentence ("Namizəd liderlik
# göstərməlidir", "Candidates must show ownership") is therefore never a
# search command. Only bare imperative forms are listed, never inflections.
_AZ_POLITE_TAIL = r"(?:\W+(?:zehmet\s+olmasa|xahis\s+edirem))?"
_AZ_SEARCH_IMPERATIVE_END_RE = re.compile(
    r"\b(?:goster|tap|cixart|axtar|sirala|siyahila)(?:in|iniz)?"
    + _AZ_POLITE_TAIL
    + r"\W*$",
    re.IGNORECASE,
)
_EN_SEARCH_IMPERATIVE_START_RE = re.compile(
    r"^\W*(?:please\s+)?(?:find|show|list|search(?:\s+for)?|display|get)\b",
    re.IGNORECASE,
)
_CANDIDATE_NOUN_RE = re.compile(
    r"\b(?:candidates?|applicants?|people|namized\w*|mutexessis\w*|"
    r"isci\w*|emekdas\w*)\b",
    re.IGNORECASE,
)

# Count-only current-result follow-up ("ilk 3", "ilk üçü", "first 3", "5
# nəfərə endir"). The WHOLE message must be this shape: any filter text
# (e.g. "bunlardan SQL bilən ilk 3") stays model-routed. Real-Ollama review
# showed the small local model putting "ilk 3" into filter_query instead of
# limit, so the unambiguous count is taken by the server; the existing #49
# refinement dispatch still owns every ResultSet validation.
_COUNT_TOKEN = r"(?P<n>\d{1,3}|[a-z]+)"
# Possessive/ordinal suffixes a count word may carry ("üçü", "beşi", "onu").
_COUNT_SUFFIXES = ("", "u", "i", "ni", "nu", "si", "su", "ini", "unu")
_COUNT_NOUN = r"(?:\s+(?:namized\w*|nefer\w*|candidates?|results?|netice\w*))?"
_COUNT_VERB = r"(?:\s+(?:goster(?:in|iniz)?|saxla|qalsin|show|keep))?"
_RESULT_LIMIT_ONLY_RES = (
    re.compile(
        r"^\W*(?:ilk|first|top)\s+"
        + _COUNT_TOKEN
        + r"(?:-[a-z]+)?"
        + _COUNT_NOUN
        + _COUNT_VERB
        + r"\W*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\W*" + _COUNT_TOKEN + r"\s+nefer\w*\s+(?:endir|azalt)(?:in|iniz)?\W*$",
        re.IGNORECASE,
    ),
)

# Bounded Azerbaijani noun case-suffix policy (nominative + the six
# grammatical cases), expressed in the ASCII-folded alphabet that `_fold`
# already normalizes text into. This is a fixed, auditable suffix table -
# not open-ended prefix/stem matching - so it generalizes across the small
# set of workflow-noun roots below without becoming a technology/phrase
# dictionary. Longer alternatives are listed before their own prefixes so a
# short alternative never wins a partial match ahead of the full suffix.
_AZ_CASE_SUFFIX = r"(?:nin|nun|ini|unu|dan|den|ya|ye|da|de|ni|nu|in|un|i|u|a|e)?"
_AZ_WORKFLOW_ROOT = r"(?:vakansiya|elan|vezife)" + _AZ_CASE_SUFFIX
_JOB_NOUN_RE = re.compile(
    r"\b(?:vacancy|job\s+(?:description|posting)|role\s+(?:description|requirements?)|"
    + _AZ_WORKFLOW_ROOT
    + r")\b",
    re.IGNORECASE,
)
_JOB_ANALYSIS_ACTION_RE = re.compile(
    r"\b(?:analy[sz]e\w*|analysis|draft\w*|criteria|criterion|tehlil\w*|analiz\w*|"
    r"hazirla\w*|meyar\w*|kriteriya\w*|qiymetlendir\w*)\b",
    re.IGNORECASE,
)
_VACANCY_HEADER_RE = re.compile(
    r"(?im)^\s*(?:vacancy|vakansiya|job\s+description|role)\s*:\s*\S"
)
_BULLET_LINE_RE = re.compile(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+\S")
# First ':' or line break: the only separators that can end a workflow
# instruction wrapper ("Vakansiyanı analiz et:" / "Bu elanı analiz et\n...").
_WRAPPER_SEPARATOR_RE = re.compile(r"[:\n]")


def _result(
    route: AgentEntryRoute,
    source: AgentRoutingSource,
    action: AgentRoutedAction,
) -> AgentEntryRoutingResult:
    return AgentEntryRoutingResult(route=route, routing_source=source, routed_action=action)


def _is_explicit_job_analysis(folded: str) -> bool:
    return bool(_JOB_NOUN_RE.search(folded) and _JOB_ANALYSIS_ACTION_RE.search(folded))


def _job_source_span(message: str) -> tuple[int, int] | None:
    """Exact offsets of the JD source after a workflow-instruction wrapper.

    Only a leading segment (before the first ':'/newline) that is itself an
    explicit vacancy-analysis instruction is a wrapper.  A structural header
    such as "Vakansiya: Senior Backend" has no analysis verb and is kept as
    meaningful source.  Returns None when there is no wrapper; returns an
    empty span when the wrapper is followed by nothing.
    """

    separator = _WRAPPER_SEPARATOR_RE.search(message)
    if separator is None:
        return None
    if not _is_explicit_job_analysis(_fold(message[: separator.start()])):
        return None
    start = separator.end()
    end = len(message)
    while start < end and message[start].isspace():
        start += 1
    while end > start and message[end - 1].isspace():
        end -= 1
    return start, end


def _has_useful_job_source(source: str, *, wrapped: bool) -> bool:
    """A JD draft needs actual vacancy material, not just the command.

    Unwrapped text is the command itself, so it must carry a source-bound
    material requirement.  Text after an explicit wrapper is user-pasted
    source and is also accepted when it has multi-line vacancy structure.
    """

    if not source.strip():
        return False
    if analyze_hr_text(source).requirements:
        return True
    nonempty_lines = [line for line in source.splitlines() if line.strip()]
    return wrapped and len(nonempty_lines) >= 2


def _count_only_result_limit(folded: str) -> int | None:
    for pattern in _RESULT_LIMIT_ONLY_RES:
        match = pattern.match(folded)
        if match is None:
            continue
        token = match.group("n")
        for suffix in _COUNT_SUFFIXES:
            if suffix and not token.endswith(suffix):
                continue
            value = count_token_value(token[: len(token) - len(suffix)])
            if value is not None and 1 <= value <= MAX_CANDIDATE_REF:
                return value
    return None


def _is_current_result_followup(folded: str) -> bool:
    return bool(_RESULT_CONTEXT_RE.search(folded))


def _is_explicit_new_search(
    message: str, folded: str, *, material_requirement_count: int, has_result_count: bool
) -> bool:
    """Search imperative + (candidate noun | requirement | result count).

    Also bounded by the existing agent search-query length, so the forced
    typed action is always schema-valid.
    """

    if len(message) > MAX_AGENT_SEARCH_QUERY_LENGTH:
        return False
    imperative = bool(
        _AZ_SEARCH_IMPERATIVE_END_RE.search(folded)
        or _EN_SEARCH_IMPERATIVE_START_RE.search(folded)
    )
    if not imperative:
        return False
    return bool(
        _CANDIDATE_NOUN_RE.search(folded) or material_requirement_count or has_result_count
    )


def _is_source_occurrence_overflow(exc: ValidationError) -> bool:
    """True only for the known source-bound span length limit.

    The deterministic semantic analyzer represents every material slot as an
    exact ``SourceOccurrence``; one very long unpunctuated clause can exceed
    that model's bounded ``text`` length even inside a valid composer message.
    Any other validation failure is an internal defect and must propagate.
    """

    errors = exc.errors(include_url=False, include_context=False, include_input=False)
    return (
        exc.title == SourceOccurrence.__name__
        and bool(errors)
        and all(
            error["type"] == "string_too_long" and error["loc"] == ("text",)
            for error in errors
        )
    )


def route_agent_entry(message: str) -> AgentEntryRoutingResult:
    """Return the server-owned route for one raw HR message (total).

    A message whose clause structure cannot be represented as bounded exact
    source occurrences receives fixed structure clarification: the text is
    never truncated, guessed at by the model or forwarded to the planner/JD
    tools.  Unrelated validation errors still propagate.
    """

    try:
        return _route_agent_entry(message)
    except ValidationError as exc:
        if not _is_source_occurrence_overflow(exc):
            raise
        return _result(
            AgentEntryRoute.CLARIFY_INPUT_STRUCTURE,
            AgentRoutingSource.DETERMINISTIC_CLARIFICATION,
            AgentRoutedAction.CLARIFY,
        )


def _route_agent_entry(message: str) -> AgentEntryRoutingResult:
    """Return the narrow server-owned route for one raw HR message.

    Precedence (pending-draft follow-ups are handled by the caller first):

    1. explicit vacancy-analysis request -> JD draft over the exact user
       source after any instruction wrapper, or fixed "send the vacancy"
       clarification when there is no useful source;
    2. a count-only current-result follow-up -> the existing #49 limit
       refinement (validated against the active ResultSet by #49 dispatch);
       any other current-result/ordinal language -> model-routed;
    3. explicit new-search imperative -> forced SEARCH_CANDIDATES;
    4. structurally strong pasted JD -> JD draft;
    5. requirement-shaped text with neither -> search-vs-JD clarification;
    6. everything else -> model-routed.
    """

    folded = _fold(message)
    analysis = analyze_hr_text(message)
    material_requirement_count = len(analysis.requirements)

    if _is_explicit_job_analysis(folded):
        span = _job_source_span(message)
        source = message if span is None else message[span[0] : span[1]]
        if not _has_useful_job_source(source, wrapped=span is not None):
            return _result(
                AgentEntryRoute.CLARIFY_JOB_SOURCE_REQUIRED,
                AgentRoutingSource.DETERMINISTIC_CLARIFICATION,
                AgentRoutedAction.CLARIFY,
            )
        return AgentEntryRoutingResult(
            route=AgentEntryRoute.FORCE_JOB_DRAFT,
            routing_source=AgentRoutingSource.DETERMINISTIC_JD,
            routed_action=AgentRoutedAction.DRAFT_JOB_CRITERIA,
            draft_source_start=None if span is None else span[0],
            draft_source_end=None if span is None else span[1],
        )

    result_limit = _count_only_result_limit(folded)
    if result_limit is not None:
        return AgentEntryRoutingResult(
            route=AgentEntryRoute.FORCE_RESULT_LIMIT,
            routing_source=AgentRoutingSource.DETERMINISTIC_RESULT_CONTEXT,
            routed_action=AgentRoutedAction.REFINE_CANDIDATE_RESULTS,
            result_limit=result_limit,
        )

    if _is_current_result_followup(folded):
        return _result(
            AgentEntryRoute.MODEL_ROUTED,
            AgentRoutingSource.MODEL,
            AgentRoutedAction.MODEL_ROUTED,
        )

    has_result_count = analysis.result_count.state != ResultCountState.ABSENT
    if _is_explicit_new_search(
        message,
        folded,
        material_requirement_count=material_requirement_count,
        has_result_count=has_result_count,
    ):
        return _result(
            AgentEntryRoute.FORCE_CANDIDATE_SEARCH,
            AgentRoutingSource.DETERMINISTIC_SEARCH,
            AgentRoutedAction.SEARCH_CANDIDATES,
        )

    # Any remaining search/result vocabulary (a non-imperative verb, a result
    # noun, a count) still protects the message from JD forcing/clarifying.
    candidate_search_cue = bool(
        _RESULT_NOUN_RE.search(folded) or _SEARCH_ACTION_RE.search(folded) or has_result_count
    )
    if candidate_search_cue:
        return _result(
            AgentEntryRoute.MODEL_ROUTED,
            AgentRoutingSource.MODEL,
            AgentRoutedAction.MODEL_ROUTED,
        )

    vacancy_header = bool(_VACANCY_HEADER_RE.search(message))
    bullet_count = len(_BULLET_LINE_RE.findall(message))
    nonempty_lines = [line for line in message.splitlines() if line.strip()]
    # >=2 distinct lines is enough paste structure once each line is
    # independently confirmed as a material requirement below (Case C,
    # issue #79 PR81 correction) — a genuinely single-line message never
    # reaches this threshold regardless of internal punctuation/sentences.
    structured_paste = bullet_count >= 2 or len(nonempty_lines) >= 2
    if (vacancy_header and material_requirement_count >= 1) or (
        structured_paste and material_requirement_count >= 2
    ):
        return _result(
            AgentEntryRoute.FORCE_JOB_DRAFT,
            AgentRoutingSource.DETERMINISTIC_JD,
            AgentRoutedAction.DRAFT_JOB_CRITERIA,
        )

    if material_requirement_count:
        return _result(
            AgentEntryRoute.CLARIFY_AMBIGUOUS,
            AgentRoutingSource.DETERMINISTIC_CLARIFICATION,
            AgentRoutedAction.CLARIFY,
        )

    return _result(
        AgentEntryRoute.MODEL_ROUTED,
        AgentRoutingSource.MODEL,
        AgentRoutedAction.MODEL_ROUTED,
    )
