"""Layer 1 — static whole-plan validation (issue #88 slice B, D-092 §11.1).

``validate_plan`` is a PURE function: no database, no mutation, no model,
no executor call. It checks a raw, untrusted plan mapping against the
registry and a server-assembled ``ValidationContext`` in the fixed order of
§11.1. The first failure rejects the WHOLE plan, so zero capabilities
execute. HUMAN_ACTION_ONLY steps are never returned as executable steps:
with a live target they become affordances, without one the plan is
rejected (CONFIRMATION_REQUIRED).

Slice-B limits (honest, not pretended): ``SourceSelection`` is
WHOLE_MESSAGE only, so the §10.2 SOURCE_* / REFERENCE_NOT_GROUNDED families
are slice C. PROHIBITED_ATTRIBUTE applies to model-authored text that no
frozen planner sees (the transitional evidence topic); WHOLE_MESSAGE and the
transitional refine filter keep the frozen planner's prohibited-attribute
refusal as the authority, exactly as §11.1 states for WHOLE_MESSAGE."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ValidationError

from meyar.agent.capabilities.contracts import (
    CAPABILITY_PLAN_SCHEMA_VERSION,
    MAX_PLAN_STEPS,
    CapabilityName,
    ContentPolicy,
    EvidenceArgs,
    ExecutablePlan,
    ExecutionMode,
    HumanActionAffordance,
    LiveContextReq,
    PlanOrigin,
    PlanRejection,
    PlanRejectionCode,
    ProfileArgs,
    ResultSetStatus,
    SearchArgs,
    VacancyArgs,
    ValidatedStep,
    ValidationContext,
)
from meyar.agent.capabilities.registry import CAPABILITY_REGISTRY, CapabilityDefinition
from meyar.agent.clarification_schemas import TaskType
from meyar.schemas.criteria import find_prohibited_term

_PROPOSAL_KEYS = frozenset({"schema_version", "goal", "steps"})
_STEP_KEYS = frozenset({"capability", "args", "policy_version"})
_PLAN_GOALS = frozenset(
    {TaskType.CANDIDATE_SEARCH, TaskType.RESULT_FOLLOWUP, TaskType.VACANCY_ANALYSIS}
)
_PRE_EXISTING_REJECTION = {
    ResultSetStatus.NONE: PlanRejectionCode.RESULT_CONTEXT_REQUIRED,
    ResultSetStatus.STALE: PlanRejectionCode.RESULT_SET_STALE,
    ResultSetStatus.EXPIRED: PlanRejectionCode.RESULT_SET_EXPIRED,
}


def _reject(code: PlanRejectionCode, index: int | None = None) -> PlanRejection:
    return PlanRejection(code=code, step_index=index)


def _resolve_text(args: BaseModel, source_text: str) -> str | None:
    """Server-side resolution of grounded text; ``""`` marks ungroundable."""
    if isinstance(args, SearchArgs):
        # WHOLE_MESSAGE resolves to the server-owned source exactly.
        return source_text if source_text.strip() else ""
    if isinstance(args, VacancyArgs):
        start, end = args.source.start, args.source.end
        if not (start < end <= len(source_text)):
            return ""
        span = source_text[start:end]
        return span if span.strip() else ""
    return None


def validate_plan(
    proposal: object,
    *,
    origin: PlanOrigin,
    source_text: str,
    ctx: ValidationContext,
    registry: Mapping[CapabilityName, CapabilityDefinition] = CAPABILITY_REGISTRY,
) -> ExecutablePlan | PlanRejection:
    """``source_text`` is the canonical server-owned text the plan's
    grounding resolves against: the current LF-canonical message for
    model-adapted and forced plans, or a resumed clarification's bound
    source span (server-built, §6.6). ``origin`` is set by the server."""
    # -- UNSUPPORTED_VERSION / SHAPE_INVALID ---------------------------------
    if not isinstance(proposal, Mapping):
        return _reject(PlanRejectionCode.SHAPE_INVALID)
    if proposal.get("schema_version") != CAPABILITY_PLAN_SCHEMA_VERSION:
        return _reject(PlanRejectionCode.UNSUPPORTED_VERSION)
    if set(proposal) != _PROPOSAL_KEYS:
        return _reject(PlanRejectionCode.SHAPE_INVALID)
    try:
        goal = TaskType(proposal["goal"])
    except (ValueError, TypeError):
        return _reject(PlanRejectionCode.SHAPE_INVALID)
    raw_steps = proposal["steps"]
    if (
        goal not in _PLAN_GOALS
        or not isinstance(raw_steps, Sequence)
        or isinstance(raw_steps, str | bytes)
        or not raw_steps
    ):
        return _reject(PlanRejectionCode.SHAPE_INVALID)
    for index, raw in enumerate(raw_steps):
        if (
            not isinstance(raw, Mapping)
            or not {"capability", "args"} <= set(raw) <= _STEP_KEYS
            or not isinstance(raw["args"], Mapping)
        ):
            return _reject(PlanRejectionCode.SHAPE_INVALID, index)

    # -- UNKNOWN_CAPABILITY (closed enum + registry lookup only) -------------
    definitions: list[CapabilityDefinition] = []
    for index, raw in enumerate(raw_steps):
        name = raw["capability"]
        if not isinstance(name, str) or name not in CapabilityName.__members__:
            return _reject(PlanRejectionCode.UNKNOWN_CAPABILITY, index)
        definition = registry.get(CapabilityName(name))
        if definition is None:
            return _reject(PlanRejectionCode.UNKNOWN_CAPABILITY, index)
        pinned = raw.get("policy_version")
        if pinned is not None and pinned != definition.policy_version:
            return _reject(PlanRejectionCode.UNSUPPORTED_VERSION, index)
        definitions.append(definition)

    # -- NOT_MODEL_PROPOSABLE -------------------------------------------------
    if origin == PlanOrigin.MODEL:
        for index, definition in enumerate(definitions):
            if not definition.model_proposable:
                return _reject(PlanRejectionCode.NOT_MODEL_PROPOSABLE, index)

    # -- INVALID_ARGUMENTS (strict schema + server-side resolution) ----------
    parsed: list[tuple[BaseModel, str | None]] = []
    for index, (raw, definition) in enumerate(zip(raw_steps, definitions, strict=True)):
        try:
            args = definition.input_schema.model_validate(dict(raw["args"]))
        except ValidationError:
            return _reject(PlanRejectionCode.INVALID_ARGUMENTS, index)
        resolved = _resolve_text(args, source_text)
        if resolved == "":
            return _reject(PlanRejectionCode.INVALID_ARGUMENTS, index)
        parsed.append((args, resolved))

    # -- PLAN_TOO_LONG -------------------------------------------------------
    if len(raw_steps) > min(MAX_PLAN_STEPS, ctx.max_tool_calls):
        return _reject(PlanRejectionCode.PLAN_TOO_LONG)

    # -- DUPLICATE_STEP (generalizes today's searched_queries guard) ---------
    seen: set[tuple[str, str, str | None]] = set()
    for index, (definition, (step_args, resolved)) in enumerate(
        zip(definitions, parsed, strict=True)
    ):
        key = (
            definition.name.value,
            json.dumps(step_args.model_dump(mode="json"), sort_keys=True),
            resolved,
        )
        if key in seen:
            return _reject(PlanRejectionCode.DUPLICATE_STEP, index)
        seen.add(key)

    # -- SCOPE_MISSING --------------------------------------------------------
    for index, definition in enumerate(definitions):
        if not definition.required_scopes <= ctx.principal_scopes:
            return _reject(PlanRejectionCode.SCOPE_MISSING, index)

    # -- TASK_TYPE_CONFLICT ---------------------------------------------------
    for index, definition in enumerate(definitions):
        if goal not in definition.allowed_task_types:
            return _reject(PlanRejectionCode.TASK_TYPE_CONFLICT, index)

    # -- PROHIBITED_ATTRIBUTE -------------------------------------------------
    for index, (step_args, _resolved) in enumerate(parsed):
        if (
            isinstance(step_args, EvidenceArgs)
            and step_args.evidence_topic
            and find_prohibited_term(step_args.evidence_topic)
        ):
            return _reject(PlanRejectionCode.PROHIBITED_ATTRIBUTE, index)

    # -- DEPENDENCY_INVALID ---------------------------------------------------
    for requirement in LiveContextReq:
        producers = [i for i, d in enumerate(definitions) if requirement in d.produces]
        if len(producers) > 1:
            return _reject(PlanRejectionCode.DEPENDENCY_INVALID, producers[1])
        for index, definition in enumerate(definitions):
            if requirement in definition.live_context and producers and producers[0] >= index:
                # Consumes a LATER (or its own) step's output.
                return _reject(PlanRejectionCode.DEPENDENCY_INVALID, index)

    def consumes_pre_existing(index: int, definition: CapabilityDefinition) -> bool:
        return LiveContextReq.ACTIVE_RESULT_SET in definition.live_context and not any(
            LiveContextReq.ACTIVE_RESULT_SET in earlier.produces
            for earlier in definitions[:index]
        )

    # -- RESULT_CONTEXT_REQUIRED / RESULT_SET_STALE / RESULT_SET_EXPIRED -----
    pre_existing = ctx.pre_existing_result_set
    for index, definition in enumerate(definitions):
        if consumes_pre_existing(index, definition):
            code = _PRE_EXISTING_REJECTION.get(pre_existing.status)
            if code is not None:
                return _reject(code, index)

    # -- CANDIDATE_REF_OUT_OF_RANGE (pre-existing set of known size only) ----
    if pre_existing.status == ResultSetStatus.VALID and pre_existing.member_count is not None:
        for index, (definition, (step_args, _resolved)) in enumerate(
            zip(definitions, parsed, strict=True)
        ):
            if (
                consumes_pre_existing(index, definition)
                and isinstance(step_args, ProfileArgs | EvidenceArgs)
                and step_args.candidate_ref > pre_existing.member_count
            ):
                return _reject(PlanRejectionCode.CANDIDATE_REF_OUT_OF_RANGE, index)

    # -- CONFIRMATION_REQUIRED (HUMAN_ACTION_ONLY -> affordance, never run) --
    live_targets = {
        LiveContextReq.PENDING_DRAFT: ctx.pending_draft_live,
        LiveContextReq.CONFIRMED_JOB_IN_SESSION: ctx.confirmed_job_in_session,
    }
    affordances: list[HumanActionAffordance] = []
    for index, definition in enumerate(definitions):
        if definition.execution != ExecutionMode.HUMAN_ACTION_ONLY:
            continue
        targets = [req for req in definition.live_context if req in live_targets]
        if not targets or not all(live_targets[req] for req in targets):
            return _reject(PlanRejectionCode.CONFIRMATION_REQUIRED, index)
        affordances.append(
            HumanActionAffordance(capability=definition.name, live_target=targets[0])
        )

    # -- CANDIDATE_CONTENT_POLICY ---------------------------------------------
    for index, definition in enumerate(definitions):
        if (
            definition.candidate_content == ContentPolicy.PROFESSIONAL_LOCAL_ONLY
            and not ctx.candidate_content_local_only
        ):
            return _reject(PlanRejectionCode.CANDIDATE_CONTENT_POLICY, index)

    steps = tuple(
        ValidatedStep(
            index=index,
            capability=definition.name,
            policy_version=definition.policy_version,
            args=step_args,
            resolved_text=resolved,
        )
        for index, (definition, (step_args, resolved)) in enumerate(
            zip(definitions, parsed, strict=True)
        )
        if definition.execution == ExecutionMode.IN_TURN
    )
    return ExecutablePlan(
        schema_version=CAPABILITY_PLAN_SCHEMA_VERSION,
        origin=origin,
        goal=goal,
        steps=steps,
        affordances=tuple(affordances),
    )
