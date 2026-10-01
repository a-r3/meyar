"""Capability executors (issue #88 slices B/C, D-092 §9).

Each IN_TURN executor is a thin, module-level wrapper around today's
``meyar.agent.service._dispatch_*`` function: it adds NO search, ResultSet,
evidence, JD or evaluation policy of its own. It reads ONLY the step's
server-resolved values (exact source slices, server-parsed integers) —
never a model-authored argument (slice C). Domain authority stays in the
existing services (frozen planner, #86 ResultSet authority, current
authorized profile, #84 canonicalization).

HUMAN_ACTION_ONLY capabilities (CREATE_JOB, RANK_JOB_CANDIDATES) have no
in-turn executor: ``refuse_human_action_only`` fails closed if anything ever
tries. Their only executors are the existing authenticated CSRF routes."""

from __future__ import annotations

from meyar.agent.capabilities.contracts import (
    CapabilityName,
    CapabilityOutcome,
    ExecutionContext,
    ValidatedStep,
)


class HumanActionOnlyError(RuntimeError):
    """A HUMAN_ACTION_ONLY capability reached in-turn execution."""


async def execute_search_candidates(
    ctx: ExecutionContext, step: ValidatedStep
) -> CapabilityOutcome:
    """The planner receives the step's SERVER-resolved source text
    (WHOLE_MESSAGE, exact grounded quotes joined by the server separator, or
    a bound resume source) — never model-authored text (§10.2)."""
    from meyar.agent.service import _dispatch_search

    assert step.resolved_text is not None
    tool_result, new_result_set_id = await _dispatch_search(
        ctx.db,
        ctx.llm,
        tenant_id=ctx.tenant_id,
        session_context=ctx.session_context,
        previous_result_set_id=ctx.session_context.active_result_set_id,
        natural_language_request=step.resolved_text,
        as_of_date=ctx.as_of_date,
        embedding_config=ctx.embedding_config,
        embedding_provider=ctx.embedding_provider,
    )
    return CapabilityOutcome(
        capability=step.capability,
        succeeded=new_result_set_id is not None,
        tool_result=tool_result,
        produced_result_set_id=new_result_set_id,
    )


async def execute_refine_results(ctx: ExecutionContext, step: ValidatedStep) -> CapabilityOutcome:
    from meyar.agent.service import _dispatch_refine

    assert step.resolved_text is not None or step.limit is not None
    dispatch = await _dispatch_refine(
        ctx.db,
        ctx.llm,
        tenant_id=ctx.tenant_id,
        session_context=ctx.session_context,
        filter_query=step.resolved_text,
        limit=step.limit,
        as_of_date=ctx.as_of_date,
        embedding_config=ctx.embedding_config,
    )
    return CapabilityOutcome(
        capability=step.capability,
        succeeded=dispatch.tool_result is not None,
        tool_result=dispatch.tool_result,
        produced_result_set_id=dispatch.new_result_set_id,
        resolution_failure=dispatch.resolution_failure,
        rejection_message=dispatch.rejection_message,
    )


async def execute_get_candidate_profile(
    ctx: ExecutionContext, step: ValidatedStep
) -> CapabilityOutcome:
    from meyar.agent.service import _dispatch_profile

    assert step.candidate_ref is not None
    tool_result, profile, failure = await _dispatch_profile(
        ctx.db,
        tenant_id=ctx.tenant_id,
        candidate_ref=step.candidate_ref,
        session_context=ctx.session_context,
    )
    assert tool_result.profile is not None
    return CapabilityOutcome(
        capability=step.capability,
        succeeded=tool_result.profile.found,
        tool_result=tool_result,
        matched_profile=profile,
        resolution_failure=failure,
    )


async def execute_get_candidate_evidence(
    ctx: ExecutionContext, step: ValidatedStep
) -> CapabilityOutcome:
    from meyar.agent.service import _dispatch_evidence

    assert step.candidate_ref is not None
    tool_result, profile, failure = await _dispatch_evidence(
        ctx.db,
        tenant_id=ctx.tenant_id,
        candidate_ref=step.candidate_ref,
        evidence_topic=step.topic,
        session_context=ctx.session_context,
    )
    assert tool_result.evidence is not None
    return CapabilityOutcome(
        capability=step.capability,
        succeeded=tool_result.evidence.found,
        tool_result=tool_result,
        matched_profile=profile,
        resolution_failure=failure,
    )


async def execute_analyze_vacancy(
    ctx: ExecutionContext, step: ValidatedStep
) -> CapabilityOutcome:
    """Review-only pending draft; never a Job row (#84 path unchanged)."""
    from meyar.agent.service import _dispatch_draft_job_criteria

    assert step.resolved_text is not None
    tool_result = await _dispatch_draft_job_criteria(ctx.llm, jd_text=step.resolved_text)
    return CapabilityOutcome(
        capability=step.capability,
        succeeded=tool_result is not None,
        tool_result=tool_result,
    )


async def refuse_human_action_only(
    ctx: ExecutionContext, step: ValidatedStep
) -> CapabilityOutcome:
    """CREATE_JOB / RANK_JOB_CANDIDATES never execute inside an agent turn.
    Reaching this is a programming error and fails closed — no Job, no
    Evaluation, no criteria version is ever written from here."""
    raise HumanActionOnlyError(
        f"{CapabilityName(step.capability).value} is HUMAN_ACTION_ONLY; "
        "only its authenticated CSRF route may execute it."
    )
