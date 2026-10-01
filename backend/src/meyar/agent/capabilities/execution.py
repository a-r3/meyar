"""Layer 2 — per-step dynamic preconditions — and v1 atomic activation
(issue #88 slice B, D-092 §11.2 / §11.3).

``execute_plan`` runs a Layer-1-validated ``ExecutablePlan`` step by step
through the registry executors (never a hard-coded dispatch):

* before a step that consumes a ResultSet produced by an EARLIER step of
  this plan, that set must exist, belong to this tenant / BrowserSession /
  conversation / context epoch, be unexpired and non-empty, and any ordinal
  must fit its actual member count;
* a step consuming the PRE-EXISTING active ResultSet gets no extra check
  here: the executor's own authoritative #86 validation runs exactly as
  today (Layer 2 never replaces it);
* activation is atomic: a produced ResultSet is only the in-turn WORKING
  pointer for later steps. Unless every step succeeds, the previous
  ``active_result_set_id`` is restored exactly and nothing is activated —
  earlier rows stay inert (#86 bounded retention). A single-step plan's
  failure is that step's own truthful outcome (STEP_FAILED), exactly as
  today; only a multi-step plan can be PLAN_INCOMPLETE.

HUMAN_ACTION_ONLY capabilities never reach this module (the validator turns
them into affordances); reaching one here raises before any executor runs.
Infrastructure errors (busy gate, revoked authority, cancellation)
propagate untouched and abandon the whole turn (#85/#87)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

from sqlalchemy import select

from meyar.agent.capabilities.contracts import (
    CapabilityName,
    EvidenceArgs,
    ExecutablePlan,
    ExecutionContext,
    ExecutionMode,
    LiveContextReq,
    PlanExecution,
    PlanStatus,
    ProfileArgs,
    StepFailureReason,
)
from meyar.agent.capabilities.executors import HumanActionOnlyError
from meyar.agent.capabilities.registry import CAPABILITY_REGISTRY, CapabilityDefinition
from meyar.models.agent_result_set import AgentResultSet
from meyar.services.audit_repo import record_event


async def check_plan_produced_result_set(
    ctx: ExecutionContext, *, result_set_id: uuid.UUID, candidate_ref: int | None
) -> StepFailureReason | None:
    """§11.2 rules 1–2 for a ResultSet produced earlier in THIS plan."""
    result_set = await ctx.db.scalar(
        select(AgentResultSet).where(
            AgentResultSet.id == result_set_id,
            AgentResultSet.tenant_id == ctx.tenant_id,
        )
    )
    if result_set is None:
        return StepFailureReason.RESULT_SET_MISSING
    session = ctx.session_context
    if (
        result_set.browser_session_id != session.browser_session_id
        or result_set.conversation_id != session.conversation_id
        or result_set.context_epoch != session.context_epoch
    ):
        return StepFailureReason.RESULT_SET_FOREIGN
    if result_set.expires_at <= datetime.now(UTC):
        return StepFailureReason.RESULT_SET_EXPIRED
    if result_set.result_count == 0:
        return StepFailureReason.RESULT_SET_EMPTY
    if candidate_ref is not None and candidate_ref > result_set.result_count:
        return StepFailureReason.CANDIDATE_REF_OUT_OF_RANGE
    return None


async def execute_plan(
    plan: ExecutablePlan,
    ctx: ExecutionContext,
    *,
    registry: Mapping[CapabilityName, CapabilityDefinition] = CAPABILITY_REGISTRY,
) -> PlanExecution:
    original_pointer = ctx.session_context.active_result_set_id
    plan_result_set_id: uuid.UUID | None = None
    execution = PlanExecution(status=PlanStatus.COMPLETED)
    multi_step = len(plan.steps) > 1

    async def stop(position: int, reason: StepFailureReason) -> PlanExecution:
        # Atomic activation: nothing this plan produced becomes live.
        ctx.session_context.active_result_set_id = original_pointer
        execution.failed_step_index = position
        execution.failure_reason = reason
        execution.activated_result_set_id = None
        if multi_step:
            execution.status = PlanStatus.INCOMPLETE
            await record_event(
                ctx.db,
                tenant_id=ctx.tenant_id,
                event_type="agent.plan.incomplete",
                metadata={"step_index": position, "reason_code": reason.value},
            )
        else:
            execution.status = PlanStatus.STEP_FAILED
        return execution

    for position, step in enumerate(plan.steps):
        definition = registry[step.capability]
        if definition.execution != ExecutionMode.IN_TURN:
            raise HumanActionOnlyError(f"{step.capability.value} is HUMAN_ACTION_ONLY.")
        if (
            LiveContextReq.ACTIVE_RESULT_SET in definition.live_context
            and plan_result_set_id is not None
        ):
            args = step.args
            reason = await check_plan_produced_result_set(
                ctx,
                result_set_id=plan_result_set_id,
                candidate_ref=(
                    args.candidate_ref if isinstance(args, ProfileArgs | EvidenceArgs) else None
                ),
            )
            if reason is not None:
                return await stop(position, reason)
        outcome = await definition.executor(ctx, step)
        execution.outcomes.append(outcome)
        if not outcome.succeeded:
            return await stop(position, StepFailureReason.STEP_NOT_SUCCESSFUL)
        if outcome.produced_result_set_id is not None:
            # In-turn working pointer for later steps only (§12.1).
            plan_result_set_id = outcome.produced_result_set_id
            ctx.session_context.active_result_set_id = plan_result_set_id
    execution.activated_result_set_id = plan_result_set_id
    return execution
