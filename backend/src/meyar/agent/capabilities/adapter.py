"""Transitional adapter (issue #88 slice B, D-092 §23): today's
``AgentDecision`` and the deterministic forced/resumed routes become a
SERVER-SIDE one-step capability plan for ``validate_plan``.

It never widens model authority:

* a model-routed SEARCH_CANDIDATES is adapted to ``WHOLE_MESSAGE``; the
  model's ``search_query`` is DROPPED, so the planner receives the canonical
  user message — the same input FORCE_CANDIDATE_SEARCH already uses;
* the refine filter/limit, the candidate ordinal and the evidence topic stay
  today's transitional model fields until slice C;
* a model-proposed DRAFT_JOB_CRITERIA is adapted to ANALYZE_VACANCY with
  MODEL origin, which Layer 1 rejects (NOT_MODEL_PROPOSABLE);
* the plan origin is chosen here by the server, never by the model.

This is not the slice-C ``agent-plan-v1`` model contract: the model still
returns one ``AgentDecision`` per loop iteration."""

from __future__ import annotations

from meyar.agent.capabilities.contracts import CAPABILITY_PLAN_SCHEMA_VERSION, CapabilityName
from meyar.agent.clarification_schemas import TaskType
from meyar.agent.schemas import AgentActionType, AgentDecision

_WHOLE_MESSAGE = {"source": {"mode": "WHOLE_MESSAGE"}}


def _plan(goal: TaskType, capability: CapabilityName, args: dict) -> dict:
    return {
        "schema_version": CAPABILITY_PLAN_SCHEMA_VERSION,
        "goal": goal.value,
        "steps": [{"capability": capability.value, "args": args}],
    }


def search_plan() -> dict:
    """SEARCH over the plan's server-owned source text (WHOLE_MESSAGE)."""
    return _plan(TaskType.CANDIDATE_SEARCH, CapabilityName.SEARCH_CANDIDATES, _WHOLE_MESSAGE)


def vacancy_plan(*, start: int, end: int) -> dict:
    """Server-built ANALYZE_VACANCY over exact offsets of the source text."""
    return _plan(
        TaskType.VACANCY_ANALYSIS,
        CapabilityName.ANALYZE_VACANCY,
        {"source": {"start": start, "end": end}},
    )


def result_limit_plan(limit: int) -> dict:
    """FORCE_RESULT_LIMIT: the server-parsed count."""
    return _plan(TaskType.RESULT_FOLLOWUP, CapabilityName.REFINE_RESULTS, {"limit": limit})


def plan_for_model_decision(decision: AgentDecision, *, message: str) -> dict:
    """Adapt one model ``AgentDecision`` tool action. FINAL_ANSWER/CLARIFY
    are conversation codes, not capabilities, and never reach here."""
    action = decision.action
    if action == AgentActionType.SEARCH_CANDIDATES:
        # D-092 §23 grounding: decision.search_query is deliberately ignored.
        return search_plan()
    if action == AgentActionType.REFINE_CANDIDATE_RESULTS:
        args: dict = {}
        if decision.filter_query is not None:
            args["filter_query"] = decision.filter_query
        if decision.limit is not None:
            args["limit"] = decision.limit
        return _plan(TaskType.RESULT_FOLLOWUP, CapabilityName.REFINE_RESULTS, args)
    if action == AgentActionType.GET_CANDIDATE_PROFILE:
        return _plan(
            TaskType.RESULT_FOLLOWUP,
            CapabilityName.GET_CANDIDATE_PROFILE,
            {"candidate_ref": decision.candidate_ref},
        )
    if action == AgentActionType.GET_CANDIDATE_EVIDENCE:
        args = {"candidate_ref": decision.candidate_ref}
        if decision.evidence_topic is not None:
            args["evidence_topic"] = decision.evidence_topic
        return _plan(TaskType.RESULT_FOLLOWUP, CapabilityName.GET_CANDIDATE_EVIDENCE, args)
    if action == AgentActionType.DRAFT_JOB_CRITERIA:
        return vacancy_plan(start=0, end=len(message))
    raise ValueError(f"{action} is not a capability action.")
