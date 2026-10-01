"""Server-built capability plans (issue #88 slice C, D-092 §10.2).

The deterministic forced routes and resumed clarifications build their
``capability-plan-v1`` plan HERE — grounded by construction: the whole
server-owned source text, a router span, or a server-parsed count. The
plan origin (SERVER) is set by the caller, never by a model. This replaces
slice B's transitional ``AgentDecision`` adapter: no model output is
adapted into a plan any more (model plans are ``agent-plan-v1``)."""

from __future__ import annotations

from meyar.agent.capabilities.contracts import CAPABILITY_PLAN_SCHEMA_VERSION, CapabilityName
from meyar.agent.clarification_schemas import TaskType


def _plan(goal: TaskType, capability: CapabilityName, args: dict) -> dict:
    return {
        "schema_version": CAPABILITY_PLAN_SCHEMA_VERSION,
        "goal": goal.value,
        "steps": [{"capability": capability.value, "args": args}],
    }


def search_plan() -> dict:
    """SEARCH over the plan's server-owned source text (WHOLE_MESSAGE)."""
    return _plan(
        TaskType.CANDIDATE_SEARCH,
        CapabilityName.SEARCH_CANDIDATES,
        {"source": {"mode": "WHOLE_MESSAGE"}},
    )


def vacancy_plan(*, start: int, end: int) -> dict:
    """ANALYZE_VACANCY over exact offsets of the source text."""
    return _plan(
        TaskType.VACANCY_ANALYSIS,
        CapabilityName.ANALYZE_VACANCY,
        {"source": {"start": start, "end": end}},
    )


def result_limit_plan(limit: int) -> dict:
    """FORCE_RESULT_LIMIT: the router's server-parsed count."""
    return _plan(TaskType.RESULT_FOLLOWUP, CapabilityName.REFINE_RESULTS, {"limit": limit})
