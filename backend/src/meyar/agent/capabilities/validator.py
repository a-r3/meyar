"""Layer 1 — static whole-plan validation (issue #88 slices B/C, D-092 §11.1).

``validate_plan`` is a PURE function: no database, no mutation, no model,
no executor call. It checks a raw, untrusted plan mapping against the
registry and a server-assembled ``ValidationContext`` in the fixed order of
§11.1. The first failure rejects the WHOLE plan, so zero capabilities
execute. HUMAN_ACTION_ONLY steps are never returned as executable steps:
with a live target they become affordances, without one the plan is
rejected (CONFIRMATION_REQUIRED).

Two origins, chosen by the SERVER:

* MODEL — an ``agent-plan-v1`` PLAN proposal. Its arguments are exact
  quotations of the current message, resolved and parsed here (§10.2):
  SOURCE_SELECTION_FORBIDDEN, SOURCE_NOT_GROUNDED, REFERENCE_NOT_GROUNDED
  and SOURCE_COVERAGE_INCOMPLETE, in that family order, then
  PROHIBITED_ATTRIBUTE on the grounded evidence topic. WHOLE_MESSAGE search
  /refine keeps the frozen planner's prohibited-attribute refusal as the
  authority;
* SERVER — a ``capability-plan-v1`` plan built for a deterministic forced
  route or a resumed clarification: grounded by construction (the whole
  server-owned source, a router span, or a server-parsed count).

The returned ``ValidatedStep``s carry only server-resolved values."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from meyar.agent.capabilities.contracts import (
    AGENT_PLAN_SCHEMA_VERSION,
    CAPABILITY_PLAN_SCHEMA_VERSION,
    CONVERSE_RESPONSE_CODES,
    MAX_PLAN_STEPS,
    AgentPlanProposal,
    CapabilityName,
    ContentPolicy,
    EvidenceArgs,
    ExecutablePlan,
    ExecutionMode,
    GroundedField,
    GroundingMode,
    GroundingRecord,
    HumanActionAffordance,
    LiveContextReq,
    ModelClarificationCode,
    PlanKind,
    PlanOrigin,
    PlanRejection,
    PlanRejectionCode,
    ProfileArgs,
    RefineArgs,
    ResultSetStatus,
    SearchArgs,
    ServerLimitArgs,
    SourceQuote,
    SourceSelection,
    VacancyArgs,
    ValidatedStep,
    ValidationContext,
)
from meyar.agent.capabilities.grounding import (
    GroundedSpan,
    has_protected_content,
    joined_text,
    parse_count_quote,
    parse_ordinal_quote,
    protected_ranges,
    resolve_quote,
    resolve_quotes,
    resolve_whole_message,
    span_of,
    uncovered_requirement,
)
from meyar.agent.capabilities.registry import CAPABILITY_REGISTRY, CapabilityDefinition
from meyar.agent.clarification_schemas import TaskType
from meyar.agent.schemas import AgentResponseCode
from meyar.agent.semantic_requirements import SemanticAnalysis, analyze_hr_text
from meyar.schemas.criteria import find_prohibited_term

_SERVER_KEYS = frozenset({"schema_version", "goal", "steps"})
_MODEL_KEYS = frozenset(
    {"schema_version", "kind", "goal", "steps", "clarification_code", "response_code"}
)
_STEP_KEYS = frozenset({"capability", "args"})
_SERVER_STEP_KEYS = frozenset({"capability", "args", "policy_version"})
_PLAN_GOALS = frozenset(
    {TaskType.CANDIDATE_SEARCH, TaskType.RESULT_FOLLOWUP, TaskType.VACANCY_ANALYSIS}
)
_PRE_EXISTING_REJECTION = {
    ResultSetStatus.NONE: PlanRejectionCode.RESULT_CONTEXT_REQUIRED,
    ResultSetStatus.STALE: PlanRejectionCode.RESULT_SET_STALE,
    ResultSetStatus.EXPIRED: PlanRejectionCode.RESULT_SET_EXPIRED,
}
# Capabilities whose text source the frozen planner receives (§10.2 rule 4).
_PLANNER_SOURCE_CAPABILITIES = frozenset(
    {CapabilityName.SEARCH_CANDIDATES, CapabilityName.REFINE_RESULTS}
)


def _reject(
    code: PlanRejectionCode,
    index: int | None = None,
    *,
    grounded_field: GroundedField | None = None,
    candidate_ref: int | None = None,
) -> PlanRejection:
    return PlanRejection(
        code=code, step_index=index, grounded_field=grounded_field, candidate_ref=candidate_ref
    )


# ---------------------------------------------------------------------------
# Grounded resolution (pure; collects the FIRST failure per family)
# ---------------------------------------------------------------------------


@dataclass
class _Resolution:
    resolved_text: str | None = None
    candidate_ref: int | None = None
    limit: int | None = None
    topic: str | None = None
    grounding: tuple[GroundingRecord, ...] = ()
    forbidden: bool = False
    not_grounded: GroundedField | None = None
    reference_not_grounded: GroundedField | None = None

    @property
    def failed(self) -> bool:
        return self.forbidden or self.not_grounded is not None or (
            self.reference_not_grounded is not None
        )

    def spans(self) -> list[GroundedSpan]:
        return [span for record in self.grounding for span in record.spans]


def _resolve_selection(
    selection: SourceSelection,
    *,
    field_name: GroundedField,
    source: str,
    protected: bool,
    resolution: _Resolution,
) -> tuple[str, GroundingRecord] | None:
    if selection.mode == "WHOLE_MESSAGE":
        span = resolve_whole_message(source)
        if span is None:
            resolution.not_grounded = resolution.not_grounded or field_name
            return None
        return source, GroundingRecord(field_name, GroundingMode.WHOLE_MESSAGE, (span,))
    if protected:
        # §10.2 rule 4: a span selection can never launder a protected
        # attribute request into a "clean" planner query.
        resolution.forbidden = True
        return None
    spans = resolve_quotes([quote.quote for quote in selection.quotes], source)
    if spans is None:
        resolution.not_grounded = resolution.not_grounded or field_name
        return None
    return joined_text(spans, source), GroundingRecord(field_name, GroundingMode.QUOTES, spans)


def _resolve_number(
    quote: SourceQuote,
    *,
    field_name: GroundedField,
    source: str,
    ordinal: bool,
    resolution: _Resolution,
) -> tuple[int, GroundingRecord] | None:
    span = resolve_quote(quote.quote, source)
    if span is None:
        resolution.not_grounded = resolution.not_grounded or field_name
        return None
    text = source[span.start : span.end]
    value = parse_ordinal_quote(text) if ordinal else parse_count_quote(text)
    if value is None:
        resolution.reference_not_grounded = resolution.reference_not_grounded or field_name
        return None
    return value, GroundingRecord(field_name, GroundingMode.QUOTES, (span,))


def _resolve_model_args(args: BaseModel, *, source: str, protected: bool) -> _Resolution:
    resolution = _Resolution()
    records: list[GroundingRecord] = []
    if isinstance(args, SearchArgs):
        selected = _resolve_selection(
            args.source, field_name=GroundedField.SOURCE, source=source,
            protected=protected, resolution=resolution,
        )
        if selected is not None:
            resolution.resolved_text, record = selected
            records.append(record)
    elif isinstance(args, RefineArgs):
        if args.filter_source is not None:
            selected = _resolve_selection(
                args.filter_source, field_name=GroundedField.FILTER, source=source,
                protected=protected, resolution=resolution,
            )
            if selected is not None:
                resolution.resolved_text, record = selected
                records.append(record)
        if args.limit_quote is not None:
            parsed = _resolve_number(
                args.limit_quote, field_name=GroundedField.LIMIT, source=source,
                ordinal=False, resolution=resolution,
            )
            if parsed is not None:
                resolution.limit, record = parsed
                records.append(record)
    elif isinstance(args, ProfileArgs | EvidenceArgs):
        parsed = _resolve_number(
            args.ref_quote, field_name=GroundedField.REFERENCE, source=source,
            ordinal=True, resolution=resolution,
        )
        if parsed is not None:
            resolution.candidate_ref, record = parsed
            records.append(record)
        if isinstance(args, EvidenceArgs) and args.topic_quote is not None:
            span = resolve_quote(args.topic_quote.quote, source)
            if span is None:
                # §10.2 rule 6: an ungrounded topic is rejected, never ignored.
                resolution.not_grounded = resolution.not_grounded or GroundedField.TOPIC
            else:
                resolution.topic = source[span.start : span.end]
                records.append(GroundingRecord(GroundedField.TOPIC, GroundingMode.QUOTES, (span,)))
    resolution.grounding = tuple(records)
    return resolution


def _resolve_server_only_args(args: BaseModel, *, source: str) -> _Resolution | None:
    """The two SERVER-only argument shapes, grounded by construction;
    ``None`` marks an ungroundable server plan (a programming error surfaced
    as INVALID_ARGUMENTS, never executed)."""
    if isinstance(args, VacancyArgs):
        start, end = args.source.start, args.source.end
        if not (start < end <= len(source)) or not source[start:end].strip():
            return None
        return _Resolution(
            resolved_text=source[start:end],
            grounding=(
                GroundingRecord(
                    GroundedField.VACANCY_SOURCE, GroundingMode.SERVER_SPAN,
                    (span_of(source, start, end),),
                ),
            ),
        )
    assert isinstance(args, ServerLimitArgs)
    return _Resolution(
        limit=args.limit,
        grounding=(GroundingRecord(GroundedField.LIMIT, GroundingMode.SERVER_VALUE),),
    )


def _ref_from_raw(raw_args: object, source: str) -> int | None:
    """Tolerant pre-validation ordinal read (only narrows bounded reads)."""
    if not isinstance(raw_args, Mapping):
        return None
    ref = raw_args.get("ref_quote")
    quote = ref.get("quote") if isinstance(ref, Mapping) else None
    if not isinstance(quote, str):
        return None
    span = resolve_quote(quote, source)
    return None if span is None else parse_ordinal_quote(source[span.start : span.end])


def pre_existing_result_set_requirements(
    proposal: object,
    *,
    source_text: str,
    registry: Mapping[CapabilityName, CapabilityDefinition] = CAPABILITY_REGISTRY,
) -> tuple[frozenset[int], bool]:
    """Which parts of the PRE-EXISTING ResultSet a (still unvalidated) plan
    would consume — the server-parsed ordinals of member-scoped references
    and whether a whole-snapshot consumer (refinement) is present — so the
    server's read-only Layer-1 inspection checks exactly those (#86).
    Tolerant of a malformed proposal: it only narrows bounded DB reads;
    ``validate_plan`` still rejects the proposal itself."""
    ordinals: set[int] = set()
    whole = False
    steps = proposal.get("steps") if isinstance(proposal, Mapping) else None
    if not isinstance(steps, Sequence) or isinstance(steps, str | bytes):
        return frozenset(), False
    produced = False
    for raw in steps:
        if not isinstance(raw, Mapping):
            continue
        name = raw.get("capability")
        if not isinstance(name, str) or name not in CapabilityName.__members__:
            continue
        definition = registry.get(CapabilityName(name))
        if definition is None:
            continue
        if LiveContextReq.ACTIVE_RESULT_SET in definition.live_context and not produced:
            if "ref_quote" in definition.input_schema.model_fields:
                ref = _ref_from_raw(raw.get("args"), source_text)
                if ref is not None:
                    ordinals.add(ref)
            else:
                whole = True
        if LiveContextReq.ACTIVE_RESULT_SET in definition.produces:
            produced = True
    return frozenset(ordinals), whole


@dataclass(frozen=True)
class ModelReply:
    """A shape-valid CLARIFY / CONVERSE proposal: closed codes only."""

    kind: PlanKind
    clarification_code: ModelClarificationCode | None = None
    response_code: AgentResponseCode | None = None


def validate_model_reply(proposal: AgentPlanProposal) -> ModelReply | PlanRejection:
    """Layer 1 for the non-PLAN kinds (pure). CLARIFY carries exactly one
    closed clarification code, CONVERSE exactly one GREETING /
    ACKNOWLEDGEMENT code; neither carries steps (SHAPE_INVALID). The
    advisory ``goal`` is ignored for them — it authorizes nothing."""
    if proposal.schema_version != AGENT_PLAN_SCHEMA_VERSION:
        return _reject(PlanRejectionCode.UNSUPPORTED_VERSION)
    if proposal.kind == PlanKind.PLAN or proposal.steps:
        return _reject(PlanRejectionCode.SHAPE_INVALID)
    if proposal.kind == PlanKind.CLARIFY:
        if proposal.clarification_code is None or proposal.response_code is not None:
            return _reject(PlanRejectionCode.SHAPE_INVALID)
        return ModelReply(PlanKind.CLARIFY, clarification_code=proposal.clarification_code)
    if (
        proposal.clarification_code is not None
        or proposal.response_code not in CONVERSE_RESPONSE_CODES
    ):
        return _reject(PlanRejectionCode.SHAPE_INVALID)
    return ModelReply(PlanKind.CONVERSE, response_code=proposal.response_code)


def _parse_args(
    raw_args: Mapping, definition: CapabilityDefinition, origin: PlanOrigin
) -> BaseModel | None:
    schemas: list[type[BaseModel]] = [definition.input_schema]
    if origin == PlanOrigin.SERVER and definition.server_input_schema is not None:
        schemas.append(definition.server_input_schema)
    for schema in schemas:
        try:
            return schema.model_validate(dict(raw_args))
        except ValidationError:
            continue
    return None


def validate_plan(
    proposal: object,
    *,
    origin: PlanOrigin,
    source_text: str,
    ctx: ValidationContext,
    offered: frozenset[CapabilityName] | None = None,
    analysis: SemanticAnalysis | None = None,
    registry: Mapping[CapabilityName, CapabilityDefinition] = CAPABILITY_REGISTRY,
) -> ExecutablePlan | PlanRejection:
    """``source_text`` is the canonical server-owned text the plan's
    grounding resolves against: the current LF-canonical message ``M`` for
    model and forced plans, or a resumed clarification's bound source span
    (server-built, §6.6). ``origin`` is set by the server. ``offered`` is
    this call's per-call capability subset (MODEL origin). ``analysis`` is
    ``analyze_hr_text(source_text)`` (computed here when absent)."""
    # -- UNSUPPORTED_VERSION / SHAPE_INVALID ---------------------------------
    if not isinstance(proposal, Mapping):
        return _reject(PlanRejectionCode.SHAPE_INVALID)
    expected_version = (
        AGENT_PLAN_SCHEMA_VERSION if origin == PlanOrigin.MODEL else CAPABILITY_PLAN_SCHEMA_VERSION
    )
    if proposal.get("schema_version") != expected_version:
        return _reject(PlanRejectionCode.UNSUPPORTED_VERSION)
    if origin == PlanOrigin.MODEL:
        if (
            set(proposal) != _MODEL_KEYS
            or proposal["kind"] != PlanKind.PLAN.value
            or proposal["clarification_code"] is not None
            or proposal["response_code"] is not None
        ):
            return _reject(PlanRejectionCode.SHAPE_INVALID)
        step_keys = _STEP_KEYS
    else:
        if set(proposal) != _SERVER_KEYS:
            return _reject(PlanRejectionCode.SHAPE_INVALID)
        step_keys = _SERVER_STEP_KEYS
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
            or not {"capability", "args"} <= set(raw) <= step_keys
            or not isinstance(raw["args"], Mapping)
        ):
            return _reject(PlanRejectionCode.SHAPE_INVALID, index)

    # -- UNKNOWN_CAPABILITY (closed enum + registry + per-call subset) -------
    definitions: list[CapabilityDefinition] = []
    for index, raw in enumerate(raw_steps):
        name = raw["capability"]
        if not isinstance(name, str) or name not in CapabilityName.__members__:
            return _reject(PlanRejectionCode.UNKNOWN_CAPABILITY, index)
        definition = registry.get(CapabilityName(name))
        if definition is None:
            return _reject(PlanRejectionCode.UNKNOWN_CAPABILITY, index)
        if (
            origin == PlanOrigin.MODEL
            and definition.model_proposable
            and (offered is None or definition.name not in offered)
        ):
            # A registered name outside this call's offered subset.
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

    # -- INVALID_ARGUMENTS (strict schema) -----------------------------------
    parsed: list[BaseModel] = []
    for index, (raw, definition) in enumerate(zip(raw_steps, definitions, strict=True)):
        args = _parse_args(raw["args"], definition, origin)
        if args is None:
            return _reject(PlanRejectionCode.INVALID_ARGUMENTS, index)
        parsed.append(args)

    # Pure server-side grounding resolution (no rejection yet: the §11.1
    # order puts the SOURCE_* families after TASK_TYPE_CONFLICT).
    if analysis is None and origin == PlanOrigin.MODEL:
        analysis = analyze_hr_text(source_text)
    protected = (
        origin == PlanOrigin.MODEL
        and analysis is not None
        and has_protected_content(analysis, source_text)
    )
    resolutions: list[_Resolution] = []
    for index, args in enumerate(parsed):
        if isinstance(args, VacancyArgs | ServerLimitArgs):
            # Reachable only for SERVER origin (never a model input schema).
            server = _resolve_server_only_args(args, source=source_text)
            if server is None:
                return _reject(PlanRejectionCode.INVALID_ARGUMENTS, index)
            resolutions.append(server)
        else:
            # Grounded arguments resolve identically for both origins; only
            # MODEL plans get the protected-content QUOTES rule and coverage.
            resolutions.append(
                _resolve_model_args(
                    args,
                    source=source_text,
                    protected=protected
                    and definitions[index].name in _PLANNER_SOURCE_CAPABILITIES,
                )
            )

    # -- PLAN_TOO_LONG -------------------------------------------------------
    if len(raw_steps) > min(MAX_PLAN_STEPS, ctx.max_tool_calls):
        return _reject(PlanRejectionCode.PLAN_TOO_LONG)

    # -- DUPLICATE_STEP (identical capability + resolved grounded args) -----
    seen: set[tuple[str, str]] = set()
    for index, (definition, args, resolution) in enumerate(
        zip(definitions, parsed, resolutions, strict=True)
    ):
        if resolution.failed:
            identity = json.dumps(args.model_dump(mode="json"), sort_keys=True)
        else:
            identity = json.dumps(
                {
                    "text": resolution.resolved_text,
                    "ref": resolution.candidate_ref,
                    "limit": resolution.limit,
                    "topic": resolution.topic,
                },
                sort_keys=True,
            )
        key = (definition.name.value, identity)
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

    # -- SOURCE_SELECTION_FORBIDDEN / SOURCE_NOT_GROUNDED /
    #    REFERENCE_NOT_GROUNDED / SOURCE_COVERAGE_INCOMPLETE (§10.2) ---------
    for index, resolution in enumerate(resolutions):
        if resolution.forbidden:
            return _reject(PlanRejectionCode.SOURCE_SELECTION_FORBIDDEN, index)
    for index, resolution in enumerate(resolutions):
        if resolution.not_grounded is not None:
            return _reject(
                PlanRejectionCode.SOURCE_NOT_GROUNDED, index,
                grounded_field=resolution.not_grounded,
            )
    for index, resolution in enumerate(resolutions):
        if resolution.reference_not_grounded is not None:
            return _reject(
                PlanRejectionCode.REFERENCE_NOT_GROUNDED, index,
                grounded_field=resolution.reference_not_grounded,
            )
    if origin == PlanOrigin.MODEL:
        assert analysis is not None
        all_spans = [span for resolution in resolutions for span in resolution.spans()]
        if uncovered_requirement(analysis, all_spans, source_text):
            return _reject(PlanRejectionCode.SOURCE_COVERAGE_INCOMPLETE)

    # -- PROHIBITED_ATTRIBUTE -------------------------------------------------
    if origin == PlanOrigin.MODEL:
        assert analysis is not None
        ranges = protected_ranges(analysis)
        for index, resolution in enumerate(resolutions):
            topic_spans = [
                span
                for record in resolution.grounding
                if record.field == GroundedField.TOPIC
                for span in record.spans
            ]
            if resolution.topic is not None and (
                find_prohibited_term(resolution.topic)
                or any(span.overlaps(start, end) for span in topic_spans for start, end in ranges)
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
    # Member-scoped (#86): a reference is stale only if ITS member changed;
    # a refinement only if any member of its whole source snapshot changed.
    pre_existing = ctx.pre_existing_result_set
    for index, (definition, resolution) in enumerate(
        zip(definitions, resolutions, strict=True)
    ):
        if not consumes_pre_existing(index, definition):
            continue
        ref = resolution.candidate_ref
        code = _PRE_EXISTING_REJECTION.get(pre_existing.status)
        if code is not None:
            return _reject(code, index, candidate_ref=ref)
        stale = (
            ref in pre_existing.stale_ordinals if ref is not None else pre_existing.snapshot_stale
        )
        if stale:
            return _reject(PlanRejectionCode.RESULT_SET_STALE, index, candidate_ref=ref)

    # -- CANDIDATE_REF_OUT_OF_RANGE (pre-existing set of known size only) ----
    for index, (definition, resolution) in enumerate(
        zip(definitions, resolutions, strict=True)
    ):
        if (
            consumes_pre_existing(index, definition)
            and resolution.candidate_ref is not None
            and pre_existing.member_count is not None
            and resolution.candidate_ref > pre_existing.member_count
        ):
            return _reject(
                PlanRejectionCode.CANDIDATE_REF_OUT_OF_RANGE, index,
                candidate_ref=resolution.candidate_ref,
            )

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
            resolved_text=resolution.resolved_text,
            candidate_ref=resolution.candidate_ref,
            limit=resolution.limit,
            topic=resolution.topic,
            grounding=resolution.grounding,
        )
        for index, (definition, resolution) in enumerate(
            zip(definitions, resolutions, strict=True)
        )
        if definition.execution == ExecutionMode.IN_TURN
    )
    return ExecutablePlan(
        schema_version=expected_version,
        origin=origin,
        goal=goal,
        steps=steps,
        affordances=tuple(affordances),
    )
