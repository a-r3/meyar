"""Server-owned entry routing for the unified MEYAR HR composer.

This boundary answers only one high-level question: is the current message
clearly a vacancy/JD draft request, clearly safe to leave to the bounded agent
or genuinely ambiguous between candidate search and vacancy analysis?  It
does not search, dispatch tools, mutate business data or authorize anything
proposed by the local model.

The implementation deliberately reuses the source-bound semantic requirement
analysis introduced for JD/search parity.  The small regular expressions here
recognize workflow/structure cues only; they are not a technology dictionary
and never construct criteria.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from meyar.agent.semantic_requirements import analyze_hr_text
from meyar.core.result_count import ResultCountState
from meyar.core.text import fold_az_ascii, normalize_azerbaijani_case

ENTRY_ROUTING_POLICY_VERSION = "agent-entry-routing-v1"


class AgentEntryRoute(StrEnum):
    FORCE_JOB_DRAFT = "FORCE_JOB_DRAFT"
    MODEL_ROUTED = "MODEL_ROUTED"
    CLARIFY_AMBIGUOUS = "CLARIFY_AMBIGUOUS"


class AgentRoutingSource(StrEnum):
    DETERMINISTIC_JD = "DETERMINISTIC_JD"
    MODEL = "MODEL"
    DETERMINISTIC_CLARIFICATION = "DETERMINISTIC_CLARIFICATION"


class AgentRoutedAction(StrEnum):
    DRAFT_JOB_CRITERIA = "DRAFT_JOB_CRITERIA"
    MODEL_ROUTED = "MODEL_ROUTED"
    CLARIFY = "CLARIFY"


@dataclass(frozen=True)
class AgentEntryRoutingResult:
    route: AgentEntryRoute
    routing_source: AgentRoutingSource
    routed_action: AgentRoutedAction


AMBIGUOUS_SEARCH_OR_JOB_COPY = (
    "Bu mətni namizəd axtarışı üçün istifadə etmək, yoxsa vakansiya meyarları "
    "kimi analiz etmək istəyirsiniz?"
)


def _fold(text: str) -> str:
    return fold_az_ascii(normalize_azerbaijani_case(text))


_CANDIDATE_OR_RESULT_RE = re.compile(
    r"\b(?:candidates?|applicants?|profiles?|results?|namized\w*|netice\w*)\b",
    re.IGNORECASE,
)
_SEARCH_ACTION_RE = re.compile(
    r"\b(?:find|show|display|list|return|search(?:\s+for)?|tap\w*|goster\w*|"
    r"cixart\w*)\b",
    re.IGNORECASE,
)
_RESULT_CONTEXT_RE = re.compile(
    r"\b(?:bunlardan|bunlarin|bunlari|cari\s+netice\w*|ilk\s+\d+|"
    r"birinci\w*|ikinci\w*|ucuncu\w*|dorduncu\w*|besinci\w*|"
    r"first|second|third|fourth|fifth|among\s+(?:them|these|those)|"
    r"from\s+(?:them|these|those)|these\s+results?)\b|#\s*\d+",
    re.IGNORECASE,
)
_JOB_NOUN_RE = re.compile(
    r"\b(?:vacancy|job\s+(?:description|posting)|role\s+(?:description|requirements?)|"
    r"vakansiya|vezife\s+telebleri|vakansiya\s+elani)\b",
    re.IGNORECASE,
)
_JOB_ANALYSIS_ACTION_RE = re.compile(
    r"\b(?:analy[sz]e\w*|analysis|draft\w*|criteria|criterion|tehlil\w*|analiz\w*|"
    r"hazirla\w*|meyar\w*|kriteriya\w*)\b",
    re.IGNORECASE,
)
_VACANCY_HEADER_RE = re.compile(
    r"(?im)^\s*(?:vacancy|vakansiya|job\s+description|role)\s*:\s*\S"
)
_BULLET_LINE_RE = re.compile(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+\S")


def _result(
    route: AgentEntryRoute,
    source: AgentRoutingSource,
    action: AgentRoutedAction,
) -> AgentEntryRoutingResult:
    return AgentEntryRoutingResult(route=route, routing_source=source, routed_action=action)


def route_agent_entry(message: str) -> AgentEntryRoutingResult:
    """Return the narrow server-owned route for one raw HR message.

    Candidate/result nouns, search/list verbs and candidate-count intent are
    protective cues: requirements inside such a request remain normal agent
    routing unless the same message explicitly asks to analyse/draft a
    vacancy.  A JD is forced only by that explicit request, a vacancy header
    with source-bound material requirements, or a clearly pasted multi-line/
    bullet structure containing multiple material requirements.  Requirement-
    shaped text that satisfies neither side is clarified rather than guessed.
    """

    folded = _fold(message)
    analysis = analyze_hr_text(message)
    material_requirement_count = len(analysis.requirements)

    explicit_job_analysis = bool(
        _JOB_NOUN_RE.search(folded) and _JOB_ANALYSIS_ACTION_RE.search(folded)
    )
    if explicit_job_analysis:
        return _result(
            AgentEntryRoute.FORCE_JOB_DRAFT,
            AgentRoutingSource.DETERMINISTIC_JD,
            AgentRoutedAction.DRAFT_JOB_CRITERIA,
        )

    candidate_search_cue = bool(
        _CANDIDATE_OR_RESULT_RE.search(folded)
        or _SEARCH_ACTION_RE.search(folded)
        or _RESULT_CONTEXT_RE.search(folded)
        or analysis.result_count.state != ResultCountState.ABSENT
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
    structured_paste = bullet_count >= 2 or len(nonempty_lines) >= 3
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
