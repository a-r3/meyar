"""Builders for scripted ``agent-plan-v1`` model proposals (issue #88 slice
C). Tests script the MODEL's output only; every value that drives execution
is still resolved by the server from the user's own message (D-092 §10.2):
a quote that does not occur exactly once is rejected, ordinals/counts are
parsed by the server's closed parsers."""

import json
from typing import Any

from meyar.agent.capabilities.contracts import AgentPlanProposal


def proposal(**fields: Any) -> AgentPlanProposal:
    """Parse exactly like the Ollama provider does (strict JSON mode)."""
    return AgentPlanProposal.model_validate_json(
        json.dumps({"schema_version": "agent-plan-v1", **fields})
    )


def _selection(quotes: tuple[str, ...]) -> dict[str, Any]:
    if not quotes:
        return {"mode": "WHOLE_MESSAGE"}
    return {"mode": "QUOTES", "quotes": [{"quote": quote} for quote in quotes]}


def search_step(*quotes: str) -> dict[str, Any]:
    """SEARCH_CANDIDATES over WHOLE_MESSAGE (no quotes) or exact quotes."""
    return {"capability": "SEARCH_CANDIDATES", "args": {"source": _selection(quotes)}}


def refine_step(
    *filter_quotes: str, whole_filter: bool = False, limit_quote: str | None = None
) -> dict[str, Any]:
    args: dict[str, Any] = {}
    if whole_filter:
        args["filter_source"] = {"mode": "WHOLE_MESSAGE"}
    elif filter_quotes:
        args["filter_source"] = _selection(filter_quotes)
    if limit_quote is not None:
        args["limit_quote"] = {"quote": limit_quote}
    return {"capability": "REFINE_RESULTS", "args": args}


def profile_step(ref_quote: str) -> dict[str, Any]:
    return {"capability": "GET_CANDIDATE_PROFILE", "args": {"ref_quote": {"quote": ref_quote}}}


def evidence_step(ref_quote: str, topic_quote: str | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {"ref_quote": {"quote": ref_quote}}
    if topic_quote is not None:
        args["topic_quote"] = {"quote": topic_quote}
    return {"capability": "GET_CANDIDATE_EVIDENCE", "args": args}


def bare_step(capability: str) -> dict[str, Any]:
    """ANALYZE_VACANCY / CREATE_JOB / RANK_JOB_CANDIDATES (no arguments)."""
    return {"capability": capability, "args": {}}


def plan(*steps: dict[str, Any], goal: str = "CANDIDATE_SEARCH") -> AgentPlanProposal:
    return proposal(kind="PLAN", goal=goal, steps=list(steps))


def search_plan(*quotes: str) -> AgentPlanProposal:
    return plan(search_step(*quotes), goal="CANDIDATE_SEARCH")


def refine_plan(
    *filter_quotes: str, whole_filter: bool = False, limit_quote: str | None = None
) -> AgentPlanProposal:
    return plan(
        refine_step(*filter_quotes, whole_filter=whole_filter, limit_quote=limit_quote),
        goal="RESULT_FOLLOWUP",
    )


def profile_plan(ref_quote: str) -> AgentPlanProposal:
    return plan(profile_step(ref_quote), goal="RESULT_FOLLOWUP")


def evidence_plan(ref_quote: str, topic_quote: str | None = None) -> AgentPlanProposal:
    return plan(evidence_step(ref_quote, topic_quote), goal="RESULT_FOLLOWUP")


def vacancy_proposal() -> AgentPlanProposal:
    """A model ANALYZE_VACANCY step: never executable (NOT_MODEL_PROPOSABLE,
    D-092 §6.1). Also the default "must never be consulted" poison plan."""
    return plan(bare_step("ANALYZE_VACANCY"), goal="VACANCY_ANALYSIS")


def converse(code: str = "GREETING") -> AgentPlanProposal:
    return proposal(kind="CONVERSE", response_code=code)


def clarify(code: str = "NEED_MORE_DETAIL") -> AgentPlanProposal:
    return proposal(kind="CLARIFY", clarification_code=code)
