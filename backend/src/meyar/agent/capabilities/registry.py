"""The capability registry (issue #88 slices B/C, D-092 §8.1 / §9).

Built once at import time from the closed ``CapabilityName`` enum. The model
only ever produces a ``CapabilityName`` value; a definition and its executor
are looked up here by the server — never by a model-supplied module or
function name. ``offered_capabilities`` derives the per-call subset the
model may propose (D-092 §8.1) from these definitions — there is no
hand-maintained duplicate list.

Stable identifiers (§9): ``REFINE_RESULTS`` is the registry name of the
persisted tool-result/audit identifier ``REFINE_CANDIDATE_RESULTS`` and
``ANALYZE_VACANCY`` of ``DRAFT_JOB_CRITERIA``. ``legacy_tool_name`` keeps the
existing ``AgentToolResult.tool_name`` and audit ``tool_name`` strings
unchanged (persisted transcripts and audit history use them); audits only
GAIN ``capability`` / ``capability_version``. It is NOT model authority: no
model output schema contains ``AgentActionType`` after slice C."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from pydantic import BaseModel

from meyar.agent.capabilities import executors
from meyar.agent.capabilities.contracts import (
    CapabilityName,
    CapabilityOutcome,
    ConfirmationPolicy,
    ContentPolicy,
    EvidenceArgs,
    ExecutionContext,
    ExecutionMode,
    HumanActionAffordance,
    IdentityPolicy,
    LiveContextReq,
    NoArgs,
    ProfileArgs,
    RefineArgs,
    SearchArgs,
    ServerLimitArgs,
    SideEffect,
    VacancyArgs,
    ValidatedStep,
)
from meyar.agent.clarification_schemas import TaskType
from meyar.agent.schemas import (
    AgentActionType,
    AgentEvidenceToolResult,
    AgentJobDraftToolResult,
    AgentProfileToolResult,
    AgentRefineToolResult,
    AgentSearchToolResult,
)

Executor = Callable[[ExecutionContext, ValidatedStep], Awaitable[CapabilityOutcome]]

# Closed audit metadata keys every capability execution audit may carry.
TOOL_AUDIT_KEYS = frozenset({"tool_name", "tool_call_index", "capability", "capability_version"})
_EXECUTOR_PACKAGE = "meyar.agent.capabilities."


@dataclass(frozen=True)
class AuditPolicy:
    """Closed metadata keys only; never HR/JD/CV text or identity.
    ``event_type`` None: audited only by the capability's own human route
    (``job.created``, criteria-version, ranking audit) — never in-turn."""

    event_type: str | None
    keys: frozenset[str]


@dataclass(frozen=True)
class CapabilityDefinition:
    name: CapabilityName
    policy_version: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    required_scopes: frozenset[str]
    side_effect: SideEffect
    execution: ExecutionMode
    model_proposable: bool
    live_context: frozenset[LiveContextReq]
    produces: frozenset[LiveContextReq]
    confirmation: ConfirmationPolicy
    candidate_content: ContentPolicy
    identity: IdentityPolicy
    allowed_task_types: frozenset[TaskType]
    executor: Executor
    audit: AuditPolicy
    # One-line, server-owned description sent to the model with the
    # per-call capability subset (D-092 §15). Never HR/CV text.
    description: str
    # Existing AgentToolResult.tool_name / audit tool_name (unchanged).
    legacy_tool_name: AgentActionType | None = None
    # A second, SERVER-only argument shape (e.g. FORCE_RESULT_LIMIT's
    # server-parsed count). Never accepted from a model plan.
    server_input_schema: type[BaseModel] | None = None


class RegistryInvariantError(RuntimeError):
    pass


_READ = frozenset({"candidates:read"})
_FOLLOWUP_TYPES = frozenset({TaskType.CANDIDATE_SEARCH, TaskType.RESULT_FOLLOWUP})
_RESULT_SET = frozenset({LiveContextReq.ACTIVE_RESULT_SET})
_TOOL_AUDIT = AuditPolicy(event_type="agent.tool.executed", keys=TOOL_AUDIT_KEYS)
_HUMAN_ROUTE_AUDIT = AuditPolicy(event_type=None, keys=frozenset())


def _definitions() -> tuple[CapabilityDefinition, ...]:
    return (
        CapabilityDefinition(
            name=CapabilityName.SEARCH_CANDIDATES,
            policy_version="cap-search-v1",
            input_schema=SearchArgs,
            output_schema=AgentSearchToolResult,
            required_scopes=_READ,
            side_effect=SideEffect.SESSION_WORKING_STATE,
            execution=ExecutionMode.IN_TURN,
            model_proposable=True,
            live_context=frozenset(),
            produces=_RESULT_SET,
            confirmation=ConfirmationPolicy.NONE,
            candidate_content=ContentPolicy.PROFESSIONAL_LOCAL_ONLY,
            identity=IdentityPolicy.NEVER_IN_INPUT_OR_MODEL,
            allowed_task_types=frozenset({TaskType.CANDIDATE_SEARCH}),
            executor=executors.execute_search_candidates,
            audit=_TOOL_AUDIT,
            description="Find candidates in the whole candidate library for the user's request.",
            legacy_tool_name=AgentActionType.SEARCH_CANDIDATES,
        ),
        CapabilityDefinition(
            name=CapabilityName.REFINE_RESULTS,
            policy_version="cap-refine-v1",
            input_schema=RefineArgs,
            output_schema=AgentRefineToolResult,
            required_scopes=_READ,
            side_effect=SideEffect.SESSION_WORKING_STATE,
            execution=ExecutionMode.IN_TURN,
            model_proposable=True,
            live_context=_RESULT_SET,
            # A derived set replaces the working pointer, but REFINE is not
            # a §11.1 ACTIVE_RESULT_SET *producer* (it consumes one).
            produces=frozenset(),
            confirmation=ConfirmationPolicy.NONE,
            candidate_content=ContentPolicy.PROFESSIONAL_LOCAL_ONLY,
            identity=IdentityPolicy.NEVER_IN_INPUT_OR_MODEL,
            allowed_task_types=_FOLLOWUP_TYPES,
            executor=executors.execute_refine_results,
            audit=_TOOL_AUDIT,
            description="Filter and/or limit ONLY the current result list (never a new search).",
            legacy_tool_name=AgentActionType.REFINE_CANDIDATE_RESULTS,
            server_input_schema=ServerLimitArgs,
        ),
        CapabilityDefinition(
            name=CapabilityName.GET_CANDIDATE_PROFILE,
            policy_version="cap-profile-v1",
            input_schema=ProfileArgs,
            output_schema=AgentProfileToolResult,
            required_scopes=_READ,
            side_effect=SideEffect.NONE,
            execution=ExecutionMode.IN_TURN,
            model_proposable=True,
            live_context=_RESULT_SET,
            produces=frozenset(),
            confirmation=ConfirmationPolicy.NONE,
            candidate_content=ContentPolicy.PROFESSIONAL_LOCAL_ONLY,
            identity=IdentityPolicy.NEVER_IN_INPUT_OR_MODEL,
            allowed_task_types=_FOLLOWUP_TYPES,
            executor=executors.execute_get_candidate_profile,
            audit=_TOOL_AUDIT,
            description="Show the professional profile of one candidate of the current results.",
            legacy_tool_name=AgentActionType.GET_CANDIDATE_PROFILE,
        ),
        CapabilityDefinition(
            name=CapabilityName.GET_CANDIDATE_EVIDENCE,
            policy_version="cap-evidence-v1",
            input_schema=EvidenceArgs,
            output_schema=AgentEvidenceToolResult,
            required_scopes=_READ,
            side_effect=SideEffect.NONE,
            execution=ExecutionMode.IN_TURN,
            model_proposable=True,
            live_context=_RESULT_SET,
            produces=frozenset(),
            confirmation=ConfirmationPolicy.NONE,
            candidate_content=ContentPolicy.PROFESSIONAL_LOCAL_ONLY,
            identity=IdentityPolicy.NEVER_IN_INPUT_OR_MODEL,
            allowed_task_types=_FOLLOWUP_TYPES,
            executor=executors.execute_get_candidate_evidence,
            audit=_TOOL_AUDIT,
            description="Show stored CV evidence for one candidate of the current results.",
            legacy_tool_name=AgentActionType.GET_CANDIDATE_EVIDENCE,
        ),
        CapabilityDefinition(
            name=CapabilityName.ANALYZE_VACANCY,
            policy_version="cap-vacancy-v1",
            input_schema=VacancyArgs,
            output_schema=AgentJobDraftToolResult,
            # Today's route scope, unchanged (D-094).
            required_scopes=_READ,
            side_effect=SideEffect.SESSION_WORKING_STATE,
            execution=ExecutionMode.IN_TURN,
            # A model proposal becomes the SEARCH_OR_VACANCY clarification.
            model_proposable=False,
            live_context=frozenset(),
            produces=frozenset({LiveContextReq.PENDING_DRAFT}),
            confirmation=ConfirmationPolicy.NONE,
            candidate_content=ContentPolicy.NONE,
            identity=IdentityPolicy.NEVER_IN_INPUT_OR_MODEL,
            allowed_task_types=frozenset({TaskType.VACANCY_ANALYSIS}),
            executor=executors.execute_analyze_vacancy,
            audit=_TOOL_AUDIT,
            description="Server-only vacancy analysis; never proposable by the model.",
            legacy_tool_name=AgentActionType.DRAFT_JOB_CRITERIA,
        ),
        CapabilityDefinition(
            name=CapabilityName.CREATE_JOB,
            policy_version="cap-create-job-v1",
            input_schema=NoArgs,
            output_schema=HumanActionAffordance,
            required_scopes=frozenset(
                {"jobs:write", "jobs:read", "candidates:read", "evaluations:write"}
            ),
            side_effect=SideEffect.BUSINESS_MUTATION,
            execution=ExecutionMode.HUMAN_ACTION_ONLY,
            model_proposable=True,
            live_context=frozenset({LiveContextReq.PENDING_DRAFT}),
            produces=frozenset(),
            confirmation=ConfirmationPolicy.EXPLICIT_HUMAN_ROUTE,
            candidate_content=ContentPolicy.NONE,
            identity=IdentityPolicy.NEVER_IN_INPUT_OR_MODEL,
            allowed_task_types=frozenset({TaskType.VACANCY_ANALYSIS}),
            executor=executors.refuse_human_action_only,
            audit=_HUMAN_ROUTE_AUDIT,
            description=(
                "Point HR to the existing confirmation form of the pending vacancy draft "
                "(never creates a job in chat)."
            ),
        ),
        CapabilityDefinition(
            name=CapabilityName.RANK_JOB_CANDIDATES,
            policy_version="cap-rank-v1",
            input_schema=NoArgs,
            output_schema=HumanActionAffordance,
            required_scopes=frozenset({"jobs:read", "candidates:read", "evaluations:write"}),
            # Writes Evaluation rows: NOT read-only (§9).
            side_effect=SideEffect.DERIVED_RECORDS,
            execution=ExecutionMode.HUMAN_ACTION_ONLY,
            model_proposable=True,
            live_context=frozenset({LiveContextReq.CONFIRMED_JOB_IN_SESSION}),
            produces=frozenset(),
            confirmation=ConfirmationPolicy.EXPLICIT_HUMAN_ROUTE,
            candidate_content=ContentPolicy.NONE,
            identity=IdentityPolicy.NEVER_IN_INPUT_OR_MODEL,
            allowed_task_types=frozenset({TaskType.VACANCY_ANALYSIS}),
            executor=executors.refuse_human_action_only,
            audit=_HUMAN_ROUTE_AUDIT,
            description=(
                "Point HR to the existing ranking form of the job confirmed in this "
                "session (never ranks in chat)."
            ),
        ),
    )


def assert_registry_invariants(registry: Mapping[CapabilityName, CapabilityDefinition]) -> None:
    """D-092 §8.1 startup/test assertion. Raises ``RegistryInvariantError``."""
    if set(registry) != set(CapabilityName):
        raise RegistryInvariantError("Registry keys must equal set(CapabilityName).")
    for name, definition in registry.items():
        if definition.name != name:
            raise RegistryInvariantError(f"{name}: definition name mismatch.")
        executor = definition.executor
        if (
            not inspect.iscoroutinefunction(executor)
            or not getattr(executor, "__module__", "").startswith(_EXECUTOR_PACKAGE)
            or "." in getattr(executor, "__qualname__", ".")
            or getattr(inspect.getmodule(executor), executor.__name__, None) is not executor
        ):
            raise RegistryInvariantError(
                f"{name}: executor must be a module-level coroutine function in "
                "meyar.agent.capabilities.*."
            )
        if definition.execution == ExecutionMode.IN_TURN and definition.side_effect not in (
            SideEffect.NONE,
            SideEffect.SESSION_WORKING_STATE,
        ):
            raise RegistryInvariantError(
                f"{name}: a mutating/derived-record capability can never be IN_TURN."
            )
        if (
            definition.side_effect in (SideEffect.BUSINESS_MUTATION, SideEffect.DERIVED_RECORDS)
            and definition.confirmation != ConfirmationPolicy.EXPLICIT_HUMAN_ROUTE
        ):
            raise RegistryInvariantError(f"{name}: mutation requires an explicit human route.")
        if definition.identity != IdentityPolicy.NEVER_IN_INPUT_OR_MODEL:
            raise RegistryInvariantError(f"{name}: identity may never be a capability input.")
        for schema in (definition.input_schema, definition.server_input_schema):
            if schema is not None and schema.model_config.get("extra") != "forbid":
                raise RegistryInvariantError(f"{name}: input schemas must forbid extra keys.")


CAPABILITY_REGISTRY: Mapping[CapabilityName, CapabilityDefinition] = MappingProxyType(
    {definition.name: definition for definition in _definitions()}
)
assert_registry_invariants(CAPABILITY_REGISTRY)


def capability_audit_metadata(definition: CapabilityDefinition) -> dict[str, str]:
    """The two keys every capability execution audit gains (§9, §19.2)."""
    return {"capability": definition.name.value, "capability_version": definition.policy_version}


def offered_capabilities(
    *,
    principal_scopes: frozenset[str],
    result_context_present: bool,
    pending_draft_live: bool,
    confirmed_job_in_session: bool,
    registry: Mapping[CapabilityName, CapabilityDefinition] = CAPABILITY_REGISTRY,
) -> tuple[CapabilityName, ...]:
    """D-092 §8.1: the per-call subset — model-proposable AND permitted for
    this principal and context. A result-set consumer is offered when a
    result context exists or an offered capability can produce one; a
    HUMAN_ACTION_ONLY capability only with its live target. Advisory for
    the model; Layer 1 re-checks everything on freshly read context."""
    live = {
        LiveContextReq.PENDING_DRAFT: pending_draft_live,
        LiveContextReq.CONFIRMED_JOB_IN_SESSION: confirmed_job_in_session,
        LiveContextReq.ACTIVE_RESULT_SET: result_context_present,
    }
    permitted = [
        definition
        for definition in registry.values()
        if definition.model_proposable and definition.required_scopes <= principal_scopes
    ]
    producible = {req for definition in permitted for req in definition.produces}
    offered: list[CapabilityName] = []
    for definition in permitted:
        satisfied = all(
            live[req]
            or (definition.execution == ExecutionMode.IN_TURN and req in producible)
            for req in definition.live_context
        )
        if satisfied:
            offered.append(definition.name)
    return tuple(name for name in CapabilityName if name in offered)
