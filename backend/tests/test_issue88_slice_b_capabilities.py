"""Issue #88 slice B (ported to the slice-C ``agent-plan-v1`` contract) —
capability registry, pure Layer-1 validator, Layer-2 preconditions, atomic
activation and source grounding (D-092 §8–§11, §22 items 1–8 and 16–20,
§23; D-094 / D-095).

Pure validator/registry tests need no database. Execution tests use the real
executors over synthetic DB fixtures and the deterministic FakeLLMProvider.
Slice C replaced the transitional AgentDecision fields with grounded quotes:
every assertion below that used a model-authored ``candidate_ref`` /
``limit`` / ``filter_query`` / ``evidence_topic`` now uses the exact quote of
the user's own message the server parses, which is at least as strict.
Synthetic data only."""

import hashlib
import inspect
import json
import uuid
from dataclasses import replace
from types import MappingProxyType

import pytest
from agent_plans import evidence_plan, refine_plan, search_plan, vacancy_proposal
from fakes import FakeLLMProvider
from search_helpers import seed_active_result_set, seed_candidate_with_profile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from test_agent_service import (
    AS_OF_DATE,
    _embedding_config,
    _new_conversation,
    _run,
)

from meyar.agent import capabilities as capabilities_pkg
from meyar.agent import service as agent_service
from meyar.agent.capabilities import (
    AGENT_PLAN_SCHEMA_VERSION,
    CAPABILITY_PLAN_SCHEMA_VERSION,
    CAPABILITY_REGISTRY,
    CapabilityName,
    ExecutablePlan,
    ExecutionContext,
    PlanOrigin,
    PlanRejection,
    PlanRejectionCode,
    PlanStatus,
    ResultSetContext,
    ResultSetStatus,
    StepFailureReason,
    ValidationContext,
    assert_registry_invariants,
    execute_plan,
    validate_plan,
)
from meyar.agent.capabilities import executors as capability_executors
from meyar.agent.capabilities.contracts import (
    ConfirmationPolicy,
    ExecutionMode,
    LiveContextReq,
    SideEffect,
    ValidatedStep,
)
from meyar.agent.capabilities.executors import HumanActionOnlyError
from meyar.agent.capabilities.registry import RegistryInvariantError
from meyar.agent.capabilities.server_plans import result_limit_plan, vacancy_plan
from meyar.agent.schemas import (
    AgentActionType,
    AgentTurnOutcome,
)
from meyar.agent.turn_boundary import ConversationSnapshot, TurnSessionState
from meyar.models.agent_result_set import AgentResultSet
from meyar.models.audit_event import AuditEvent
from meyar.models.evaluation import Evaluation
from meyar.models.job import Job
from meyar.models.job_criteria_version import JobCriteriaVersion
from meyar.search import planner_service
from meyar.search.planner_schemas import PlannerDraft
from meyar.search.schemas import RequiredFilters
from meyar.ui.presentation import agent_turn_outcome_message

ALL_SCOPES = frozenset(
    {
        "jobs:read", "jobs:write", "candidates:read", "candidates:write",
        "evaluations:read", "evaluations:write",
    }
)
ALL_OFFERED = frozenset(
    name for name, definition in CAPABILITY_REGISTRY.items() if definition.model_proposable
)
# A search message (one material requirement, covered by WHOLE_MESSAGE).
MESSAGE = "Python bilən namizədlər"
# A follow-up message with NO material requirement: ordinals and a count.
REF_MESSAGE = "1-ci, 2-ci, 3-cü və 5-ci namizədi aç; ilk 2 nəfər"
# Both: a WHOLE_MESSAGE search covers the requirement, refs follow it.
COMBO_MESSAGE = "Python bilən namizədlər; 1-ci, 2-ci, 3-cü və 5-ci namizəd; ilk 2"
PYTHON_PROFILE = {
    "skills": [
        {
            "name": "Python",
            "category": None,
            "evidence": [{"page": 1, "block_index": 0, "quote": "Synthetic: Python"}],
        }
    ],
    "employment_history": [], "education": [], "certifications": [],
    "languages": [], "projects": [],
}


def _ctx(**overrides) -> ValidationContext:  # noqa: ANN003
    values: dict = {
        "principal_scopes": ALL_SCOPES,
        "pre_existing_result_set": ResultSetContext(ResultSetStatus.VALID, member_count=3),
        "pending_draft_live": False,
        "confirmed_job_in_session": False,
        "max_tool_calls": 3,
    }
    values.update(overrides)
    return ValidationContext(**values)


def _plan(goal: str | None, *steps: dict, kind: str = "PLAN") -> dict:
    """A MODEL ``agent-plan-v1`` proposal as Layer 1 receives it."""
    return {
        "schema_version": AGENT_PLAN_SCHEMA_VERSION, "kind": kind, "goal": goal,
        "steps": list(steps), "clarification_code": None, "response_code": None,
    }


def _server(goal: str, *steps: dict) -> dict:
    return {"schema_version": CAPABILITY_PLAN_SCHEMA_VERSION, "goal": goal, "steps": list(steps)}


def _step(capability: str, **args) -> dict:  # noqa: ANN003
    return {"capability": capability, "args": args}


def _q(text: str) -> dict:
    return {"quote": text}


SEARCH = _step("SEARCH_CANDIDATES", source={"mode": "WHOLE_MESSAGE"})


def _profile(ref: str) -> dict:
    return _step("GET_CANDIDATE_PROFILE", ref_quote=_q(ref))


def _evidence(ref: str, topic: str | None = None) -> dict:
    args: dict = {"ref_quote": _q(ref)}
    if topic is not None:
        args["topic_quote"] = _q(topic)
    return _step("GET_CANDIDATE_EVIDENCE", **args)


LIMIT_2 = _step("REFINE_RESULTS", limit_quote=_q("ilk 2"))


class _SpyExecutors:
    """A registry copy whose executors only record calls (zero-execution
    proofs). Reuses every real definition field except the executor."""

    def __init__(self) -> None:
        self.calls: list[CapabilityName] = []
        definitions = {}
        for name, definition in CAPABILITY_REGISTRY.items():

            async def _spy(ctx, step, _name=name):  # noqa: ANN001, ANN202
                self.calls.append(_name)
                raise AssertionError("executor must not run")

            definitions[name] = replace(definition, executor=_spy)
        self.registry = MappingProxyType(definitions)


def _validate(  # noqa: ANN202
    proposal: object, *, origin=PlanOrigin.MODEL, source=REF_MESSAGE, offered=ALL_OFFERED,  # noqa: ANN001
    **ctx,  # noqa: ANN003
):
    return validate_plan(
        proposal, origin=origin, source_text=source, ctx=_ctx(**ctx), offered=offered
    )


def _rejected(result: object, code: PlanRejectionCode) -> None:
    assert isinstance(result, PlanRejection), result
    assert result.code == code, result


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_is_complete_and_matches_the_accepted_mapping() -> None:
    assert set(CAPABILITY_REGISTRY) == set(CapabilityName)
    assert len(CapabilityName) == 7
    assert_registry_invariants(CAPABILITY_REGISTRY)
    in_turn = {
        name for name, d in CAPABILITY_REGISTRY.items() if d.execution == ExecutionMode.IN_TURN
    }
    assert in_turn == {
        CapabilityName.SEARCH_CANDIDATES, CapabilityName.REFINE_RESULTS,
        CapabilityName.GET_CANDIDATE_PROFILE, CapabilityName.GET_CANDIDATE_EVIDENCE,
        CapabilityName.ANALYZE_VACANCY,
    }
    create = CAPABILITY_REGISTRY[CapabilityName.CREATE_JOB]
    rank = CAPABILITY_REGISTRY[CapabilityName.RANK_JOB_CANDIDATES]
    assert (create.side_effect, create.execution, create.confirmation) == (
        SideEffect.BUSINESS_MUTATION, ExecutionMode.HUMAN_ACTION_ONLY,
        ConfirmationPolicy.EXPLICIT_HUMAN_ROUTE,
    )
    assert (rank.side_effect, rank.execution) == (
        SideEffect.DERIVED_RECORDS, ExecutionMode.HUMAN_ACTION_ONLY,
    )
    assert create.live_context == frozenset({LiveContextReq.PENDING_DRAFT})
    assert rank.live_context == frozenset({LiveContextReq.CONFIRMED_JOB_IN_SESSION})
    assert CAPABILITY_REGISTRY[CapabilityName.ANALYZE_VACANCY].model_proposable is False
    # Accepted D-092 Amendment A3.3: RANK is registered but not proposable.
    assert rank.model_proposable is False
    assert create.model_proposable is True
    assert CAPABILITY_REGISTRY[CapabilityName.SEARCH_CANDIDATES].produces == frozenset(
        {LiveContextReq.ACTIVE_RESULT_SET}
    )
    versions = [d.policy_version for d in CAPABILITY_REGISTRY.values()]
    assert len(set(versions)) == 7 and all(v.startswith("cap-") for v in versions)


def test_legacy_tool_names_and_capability_names_stay_compatible() -> None:
    legacy = {
        name: definition.legacy_tool_name for name, definition in CAPABILITY_REGISTRY.items()
    }
    assert legacy == {
        CapabilityName.SEARCH_CANDIDATES: AgentActionType.SEARCH_CANDIDATES,
        CapabilityName.REFINE_RESULTS: AgentActionType.REFINE_CANDIDATE_RESULTS,
        CapabilityName.GET_CANDIDATE_PROFILE: AgentActionType.GET_CANDIDATE_PROFILE,
        CapabilityName.GET_CANDIDATE_EVIDENCE: AgentActionType.GET_CANDIDATE_EVIDENCE,
        CapabilityName.ANALYZE_VACANCY: AgentActionType.DRAFT_JOB_CRITERIA,
        CapabilityName.CREATE_JOB: None,
        CapabilityName.RANK_JOB_CANDIDATES: None,
    }


def test_every_executor_is_a_module_level_capabilities_function() -> None:
    for definition in CAPABILITY_REGISTRY.values():
        executor = definition.executor
        assert executor.__module__ == "meyar.agent.capabilities.executors"
        assert getattr(capability_executors, executor.__name__) is executor
        assert inspect.iscoroutinefunction(executor)


async def _foreign_executor(ctx, step):  # noqa: ANN001, ANN202 - lives in tests/, not capabilities
    raise AssertionError


@pytest.mark.parametrize(
    "mutation",
    ["missing-key", "lambda", "foreign-module", "nested", "mutation-in-turn", "derived-in-turn",
     "extra-allowed"],
)
def test_registry_invariants_fail_closed(mutation: str) -> None:
    definitions = dict(CAPABILITY_REGISTRY)
    search = definitions[CapabilityName.SEARCH_CANDIDATES]
    if mutation == "missing-key":
        del definitions[CapabilityName.RANK_JOB_CANDIDATES]
    elif mutation == "lambda":
        definitions[search.name] = replace(search, executor=lambda ctx, step: None)
    elif mutation == "foreign-module":
        definitions[search.name] = replace(search, executor=_foreign_executor)
    elif mutation == "nested":
        async def nested(ctx, step):  # noqa: ANN001, ANN202
            raise AssertionError

        nested.__module__ = "meyar.agent.capabilities.executors"
        definitions[search.name] = replace(search, executor=nested)
    elif mutation == "mutation-in-turn":
        create = definitions[CapabilityName.CREATE_JOB]
        definitions[create.name] = replace(create, execution=ExecutionMode.IN_TURN)
    elif mutation == "derived-in-turn":
        rank = definitions[CapabilityName.RANK_JOB_CANDIDATES]
        definitions[rank.name] = replace(rank, execution=ExecutionMode.IN_TURN)
    else:
        from pydantic import BaseModel

        class Loose(BaseModel):
            pass

        definitions[search.name] = replace(search, input_schema=Loose)
    with pytest.raises(RegistryInvariantError):
        assert_registry_invariants(definitions)


# ---------------------------------------------------------------------------
# Layer 1 — pure validator (§22 items 1–8, 16, 17)
# ---------------------------------------------------------------------------


def test_validator_is_pure_and_synchronous() -> None:
    assert not inspect.iscoroutinefunction(validate_plan)
    parameters = inspect.signature(validate_plan).parameters
    assert "db" not in parameters and "llm" not in parameters
    for module in (capabilities_pkg.validator, capabilities_pkg.grounding):
        source = inspect.getsource(module)
        for forbidden in ("AsyncSession", "await ", "record_event", "executor("):
            assert forbidden not in source


@pytest.mark.parametrize(
    "capability",
    ["DELETE_CANDIDATE", "meyar.agent.service._dispatch_search", "_dispatch_search",
     "execute_search_candidates", "search_candidates", "", 7, None],
)
def test_unknown_capability_or_python_name_is_rejected_with_zero_execution(
    capability: object,
) -> None:
    spy = _SpyExecutors()
    result = validate_plan(
        _plan("CANDIDATE_SEARCH", {"capability": capability, "args": {}}),
        origin=PlanOrigin.MODEL, source_text=MESSAGE, ctx=_ctx(), offered=ALL_OFFERED,
        registry=spy.registry,
    )
    _rejected(result, PlanRejectionCode.UNKNOWN_CAPABILITY)
    assert spy.calls == []


def test_registered_capability_outside_the_per_call_subset_is_unknown() -> None:
    """D-092 §8.1 / scenario H: a registered name that was not OFFERED in
    this call fails closed exactly like an unknown one."""
    offered = frozenset({CapabilityName.SEARCH_CANDIDATES})
    _rejected(
        _validate(_plan("RESULT_FOLLOWUP", _profile("1-ci")), offered=offered),
        PlanRejectionCode.UNKNOWN_CAPABILITY,
    )
    _rejected(
        _validate(_plan("CANDIDATE_SEARCH", SEARCH), source=MESSAGE, offered=frozenset()),
        PlanRejectionCode.UNKNOWN_CAPABILITY,
    )
    assert isinstance(
        _validate(_plan("CANDIDATE_SEARCH", SEARCH), source=MESSAGE, offered=offered),
        ExecutablePlan,
    )


@pytest.mark.parametrize(
    "step",
    [
        # The retired model-authored fields are not accepted any more.
        _step("GET_CANDIDATE_PROFILE", candidate_ref=1),
        _step("GET_CANDIDATE_EVIDENCE", ref_quote=_q("1-ci"), evidence_topic="Python"),
        _step("REFINE_RESULTS", filter_query="SQL"),
        _step("REFINE_RESULTS", limit=2),
        _step("SEARCH_CANDIDATES", search_query="Python"),
        # Wrong types / missing / over-long.
        _step("GET_CANDIDATE_PROFILE", ref_quote="1-ci"),
        _step("GET_CANDIDATE_PROFILE", ref_quote={"quote": 1}),
        _step("GET_CANDIDATE_PROFILE", ref_quote={"quote": ""}),
        _step("GET_CANDIDATE_PROFILE"),
        _step("GET_CANDIDATE_EVIDENCE", ref_quote=_q("1-ci"), topic_quote=_q("x" * 501)),
        _step("REFINE_RESULTS"),
        _step("REFINE_RESULTS", limit_quote=_q("x" * 501)),
        _step("SEARCH_CANDIDATES", source={"mode": "QUOTES"}),
        _step("SEARCH_CANDIDATES", source={"mode": "QUOTES", "quotes": [_q("1-ci")] * 5}),
        _step("SEARCH_CANDIDATES", source={"mode": "WHOLE_MESSAGE", "quotes": [_q("1-ci")]}),
        _step("SEARCH_CANDIDATES", source={"mode": "whole_message"}),
        _step("SEARCH_CANDIDATES"),
        # A model plan can never use a SERVER-only argument shape.
        _step("REFINE_RESULTS", limit=3),
    ],
    ids=lambda step: json.dumps(step["args"])[:40],
)
def test_invalid_arguments_reject_the_whole_plan(step: dict) -> None:
    spy = _SpyExecutors()
    result = validate_plan(
        _plan("CANDIDATE_SEARCH", step), origin=PlanOrigin.MODEL, source_text=REF_MESSAGE,
        ctx=_ctx(), offered=ALL_OFFERED, registry=spy.registry,
    )
    _rejected(result, PlanRejectionCode.INVALID_ARGUMENTS)
    assert spy.calls == []


FORBIDDEN_AUTHORITY_KEYS = [
    "tenant_id", "browser_session_id", "session_id", "conversation_id", "result_set_id",
    "active_result_set_id", "candidate_id", "candidate_ref", "draft_id", "task_id",
    "clarification_id", "job_id", "job_criteria_version_id", "scope", "scopes", "confirmed",
    "score", "weight", "as_of_date", "evaluation_date", "search_query", "filter_query",
    "limit", "evidence_topic", "module", "function", "candidate_name", "email", "phone",
]


@pytest.mark.parametrize("key", FORBIDDEN_AUTHORITY_KEYS)
def test_authority_or_identity_fields_fail_closed_at_every_level(key: str) -> None:
    value = str(uuid.uuid4())
    in_args = _step("GET_CANDIDATE_PROFILE", ref_quote=_q("1-ci"), **{key: value})
    _rejected(_validate(_plan("RESULT_FOLLOWUP", in_args)), PlanRejectionCode.INVALID_ARGUMENTS)
    in_step = {**_profile("1-ci"), key: value}
    _rejected(_validate(_plan("RESULT_FOLLOWUP", in_step)), PlanRejectionCode.SHAPE_INVALID)
    top = {**_plan("RESULT_FOLLOWUP", _profile("1-ci")), key: value}
    _rejected(_validate(top), PlanRejectionCode.SHAPE_INVALID)


def test_uuid_shaped_candidate_reference_is_never_an_ordinal() -> None:
    forged = str(uuid.uuid4())
    # Not in the message -> not grounded at all.
    _rejected(
        _validate(_plan("RESULT_FOLLOWUP", _profile(forged))),
        PlanRejectionCode.SOURCE_NOT_GROUNDED,
    )
    # Even typed by HR, a UUID is outside the closed ordinal vocabulary.
    message = f"{forged} namizədini aç"
    _rejected(
        _validate(_plan("RESULT_FOLLOWUP", _profile(forged)), source=message),
        PlanRejectionCode.REFERENCE_NOT_GROUNDED,
    )


def test_over_long_plan_is_rejected_whole_and_step_one_never_runs() -> None:
    spy = _SpyExecutors()
    steps = [_profile(ref) for ref in ("1-ci", "2-ci", "3-cü")]
    four = _plan("CANDIDATE_SEARCH", SEARCH, *steps)
    result = validate_plan(
        four, origin=PlanOrigin.MODEL, source_text=COMBO_MESSAGE, ctx=_ctx(),
        offered=ALL_OFFERED, registry=spy.registry,
    )
    _rejected(result, PlanRejectionCode.PLAN_TOO_LONG)
    assert spy.calls == []
    # Effective bound is min(MAX_PLAN_STEPS, agent_max_tool_calls).
    two = _plan("CANDIDATE_SEARCH", SEARCH, steps[0])
    _rejected(
        _validate(two, source=COMBO_MESSAGE, max_tool_calls=1), PlanRejectionCode.PLAN_TOO_LONG
    )


def test_duplicate_step_is_rejected() -> None:
    profile = _profile("1-ci")
    _rejected(
        _validate(_plan("RESULT_FOLLOWUP", profile, profile)), PlanRejectionCode.DUPLICATE_STEP
    )


@pytest.mark.parametrize(
    ("scopes", "proposal", "source", "origin"),
    [
        # unknown role -> no scopes
        (frozenset(), _plan("CANDIDATE_SEARCH", SEARCH), MESSAGE, PlanOrigin.MODEL),
        (frozenset({"candidates:read"}), _plan("VACANCY_ANALYSIS", _step("CREATE_JOB")), "x",
         PlanOrigin.MODEL),
        # RANK is not model-proposable (A3.3); its registry scopes still hold.
        (frozenset({"jobs:read", "candidates:read"}),
         _server("VACANCY_ANALYSIS", _step("RANK_JOB_CANDIDATES")), "x", PlanOrigin.SERVER),
    ],
)
def test_missing_scope_is_rejected(
    scopes: frozenset[str], proposal: dict, source: str, origin: PlanOrigin
) -> None:
    result = _validate(
        proposal, source=source, origin=origin, principal_scopes=scopes,
        pending_draft_live=True, confirmed_job_in_session=True,
    )
    _rejected(result, PlanRejectionCode.SCOPE_MISSING)


def test_task_type_conflict_is_rejected() -> None:
    _rejected(
        _validate(_plan("RESULT_FOLLOWUP", SEARCH), source=MESSAGE),
        PlanRejectionCode.TASK_TYPE_CONFLICT,
    )
    _rejected(
        _validate(_plan("CANDIDATE_SEARCH", _step("CREATE_JOB")), pending_draft_live=True),
        PlanRejectionCode.TASK_TYPE_CONFLICT,
    )


def test_analyze_vacancy_is_server_only() -> None:
    model = _plan("VACANCY_ANALYSIS", _step("ANALYZE_VACANCY"))
    _rejected(_validate(model, source=MESSAGE), PlanRejectionCode.NOT_MODEL_PROPOSABLE)
    proposal = vacancy_plan(start=0, end=len(MESSAGE))
    # A server-shaped plan can never be submitted as a MODEL plan.
    _rejected(_validate(proposal, source=MESSAGE), PlanRejectionCode.UNSUPPORTED_VERSION)
    server = _validate(proposal, source=MESSAGE, origin=PlanOrigin.SERVER)
    assert isinstance(server, ExecutablePlan)
    assert server.steps[0].resolved_text == MESSAGE
    # Out-of-range or blank server spans never resolve.
    _rejected(
        _validate(
            vacancy_plan(start=0, end=len(MESSAGE) + 1), source=MESSAGE,
            origin=PlanOrigin.SERVER,
        ),
        PlanRejectionCode.INVALID_ARGUMENTS,
    )


def test_server_limit_is_the_routers_parsed_count() -> None:
    plan = _validate(result_limit_plan(3), source="ilk 3", origin=PlanOrigin.SERVER)
    assert isinstance(plan, ExecutablePlan)
    assert (plan.steps[0].limit, plan.steps[0].resolved_text) == (3, None)


@pytest.mark.parametrize("topic", ["gender", "cins", "din", "marital status"])
def test_protected_attribute_evidence_topic_is_rejected(topic: str) -> None:
    message = f"1-ci namizəd üzrə {topic} sübutunu göstər"
    _rejected(
        _validate(_plan("RESULT_FOLLOWUP", _evidence("1-ci", topic)), source=message),
        PlanRejectionCode.PROHIBITED_ATTRIBUTE,
    )


def test_dependency_on_a_later_step_is_rejected() -> None:
    _rejected(
        _validate(_plan("CANDIDATE_SEARCH", _profile("1-ci"), SEARCH), source=COMBO_MESSAGE),
        PlanRejectionCode.DEPENDENCY_INVALID,
    )


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (ResultSetStatus.NONE, PlanRejectionCode.RESULT_CONTEXT_REQUIRED),
        (ResultSetStatus.STALE, PlanRejectionCode.RESULT_SET_STALE),
        (ResultSetStatus.EXPIRED, PlanRejectionCode.RESULT_SET_EXPIRED),
    ],
)
@pytest.mark.parametrize(
    "step",
    [LIMIT_2, _profile("1-ci"), _evidence("1-ci")],
    ids=["refine", "profile", "evidence"],
)
def test_missing_stale_or_expired_result_set_fails_closed(
    status: ResultSetStatus, code: PlanRejectionCode, step: dict
) -> None:
    spy = _SpyExecutors()
    result = validate_plan(
        _plan("RESULT_FOLLOWUP", step), origin=PlanOrigin.MODEL, source_text=REF_MESSAGE,
        ctx=_ctx(pre_existing_result_set=ResultSetContext(status)), offered=ALL_OFFERED,
        registry=spy.registry,
    )
    _rejected(result, code)
    assert spy.calls == []
    # An earlier producer in the plan satisfies the dependency statically.
    produced = _validate(
        _plan("CANDIDATE_SEARCH", SEARCH, step),
        source=COMBO_MESSAGE,
        pre_existing_result_set=ResultSetContext(status),
    )
    assert isinstance(produced, ExecutablePlan)


def test_candidate_ref_out_of_range_only_where_statically_knowable() -> None:
    known = ResultSetContext(ResultSetStatus.VALID, member_count=2)
    beyond = _profile("3-cü")
    rejection = _validate(_plan("RESULT_FOLLOWUP", beyond), pre_existing_result_set=known)
    _rejected(rejection, PlanRejectionCode.CANDIDATE_REF_OUT_OF_RANGE)
    assert rejection.candidate_ref == 3  # the SERVER-parsed ordinal
    # Against a set produced by an earlier step: Layer 2's job, not Layer 1.
    assert isinstance(
        _validate(
            _plan("CANDIDATE_SEARCH", SEARCH, beyond),
            source=COMBO_MESSAGE, pre_existing_result_set=known,
        ),
        ExecutablePlan,
    )


def test_member_scoped_staleness_follows_issue_86() -> None:
    """Only the REFERENCED member's staleness rejects a reference; a whole-
    snapshot consumer (refinement) is stale if any member changed."""
    one_changed = ResultSetContext(
        ResultSetStatus.VALID, member_count=3, stale_ordinals=frozenset({2})
    )
    assert isinstance(
        _validate(_plan("RESULT_FOLLOWUP", _profile("1-ci")), pre_existing_result_set=one_changed),
        ExecutablePlan,
    )
    _rejected(
        _validate(
            _plan("RESULT_FOLLOWUP", _evidence("2-ci")), pre_existing_result_set=one_changed
        ),
        PlanRejectionCode.RESULT_SET_STALE,
    )
    assert isinstance(
        _validate(_plan("RESULT_FOLLOWUP", LIMIT_2), pre_existing_result_set=one_changed),
        ExecutablePlan,
    )
    _rejected(
        _validate(
            _plan("RESULT_FOLLOWUP", LIMIT_2),
            pre_existing_result_set=ResultSetContext(
                ResultSetStatus.VALID, member_count=3, snapshot_stale=True
            ),
        ),
        PlanRejectionCode.RESULT_SET_STALE,
    )


def test_server_inspects_exactly_what_the_plan_consumes() -> None:
    from meyar.agent.capabilities.validator import pre_existing_result_set_requirements

    def needs(proposal: object, source: str = REF_MESSAGE) -> tuple[frozenset[int], bool]:
        return pre_existing_result_set_requirements(proposal, source_text=source)

    assert needs(_plan("RESULT_FOLLOWUP", _profile("2-ci"), _evidence("5-ci"))) == (
        frozenset({2, 5}), False,
    )
    assert needs(_plan("RESULT_FOLLOWUP", LIMIT_2)) == (frozenset(), True)
    # A step after an in-plan producer consumes THAT set, not the old one.
    assert needs(_plan("CANDIDATE_SEARCH", SEARCH, _profile("2-ci")), COMBO_MESSAGE) == (
        frozenset(), False,
    )
    assert needs(_plan("CANDIDATE_SEARCH", SEARCH), MESSAGE) == (frozenset(), False)
    # Malformed / forged / ungrounded input only narrows reads; never raises.
    for junk in ("x", None, {"steps": "x"}, _plan("RESULT_FOLLOWUP", _profile("9-cu")),
                 _plan("RESULT_FOLLOWUP", _profile(str(uuid.uuid4())))):
        assert needs(junk) == (frozenset(), False)


@pytest.mark.parametrize(
    ("capability", "target", "origin"),
    [
        ("CREATE_JOB", "pending_draft_live", PlanOrigin.MODEL),
        # A3.3: RANK keeps its registry/affordance concept, but only a
        # SERVER plan could carry it — never a model plan (below).
        ("RANK_JOB_CANDIDATES", "confirmed_job_in_session", PlanOrigin.SERVER),
    ],
)
def test_human_action_only_needs_live_target_and_becomes_an_affordance(
    capability: str, target: str, origin: PlanOrigin
) -> None:
    build = _plan if origin == PlanOrigin.MODEL else _server
    proposal = build("VACANCY_ANALYSIS", _step(capability))
    _rejected(_validate(proposal, origin=origin), PlanRejectionCode.CONFIRMATION_REQUIRED)
    plan = _validate(proposal, origin=origin, **{target: True})
    assert isinstance(plan, ExecutablePlan)
    assert plan.steps == ()  # never an executable step
    (affordance,) = plan.affordances
    assert affordance.capability == CapabilityName(capability)
    assert set(affordance.model_dump()) == {"capability", "live_target"}  # no ids


@pytest.mark.parametrize("confirmed", [False, True])
def test_model_rank_proposal_is_never_proposable_even_with_a_confirmed_job(
    confirmed: bool,
) -> None:
    """Accepted A3.3: whatever the session holds, a MODEL plan naming RANK is
    NOT_MODEL_PROPOSABLE with zero execution (no affordance, no ranking)."""
    spy = _SpyExecutors()
    result = validate_plan(
        _plan("VACANCY_ANALYSIS", _step("RANK_JOB_CANDIDATES")), origin=PlanOrigin.MODEL,
        source_text="namizədləri sırala", ctx=_ctx(confirmed_job_in_session=confirmed),
        offered=ALL_OFFERED | {CapabilityName.RANK_JOB_CANDIDATES}, registry=spy.registry,
    )
    _rejected(result, PlanRejectionCode.NOT_MODEL_PROPOSABLE)
    assert spy.calls == []


def test_candidate_content_policy_requires_local_inference() -> None:
    _rejected(
        _validate(
            _plan("CANDIDATE_SEARCH", SEARCH), source=MESSAGE, candidate_content_local_only=False
        ),
        PlanRejectionCode.CANDIDATE_CONTENT_POLICY,
    )


@pytest.mark.parametrize(
    ("proposal", "origin"),
    [
        ({**_plan("CANDIDATE_SEARCH", SEARCH), "schema_version": "agent-plan-v0"},
         PlanOrigin.MODEL),
        ({**_plan("CANDIDATE_SEARCH", SEARCH), "schema_version": None}, PlanOrigin.MODEL),
        (_server("CANDIDATE_SEARCH", SEARCH), PlanOrigin.MODEL),
        ({**_server("CANDIDATE_SEARCH", SEARCH), "schema_version": AGENT_PLAN_SCHEMA_VERSION},
         PlanOrigin.SERVER),
        (_server("CANDIDATE_SEARCH", {**SEARCH, "policy_version": "cap-search-v0"}),
         PlanOrigin.SERVER),
    ],
)
def test_unsupported_versions_are_rejected(proposal: dict, origin: PlanOrigin) -> None:
    _rejected(
        _validate(proposal, source=MESSAGE, origin=origin), PlanRejectionCode.UNSUPPORTED_VERSION
    )


@pytest.mark.parametrize(
    "proposal",
    [
        "SEARCH_CANDIDATES",
        [SEARCH],
        _plan("CANDIDATE_SEARCH"),
        _plan("UNDETERMINED", SEARCH),
        _plan("HIRE", SEARCH),
        _plan(None, SEARCH),
        {**_plan("CANDIDATE_SEARCH"), "steps": "SEARCH_CANDIDATES"},
        _plan("CANDIDATE_SEARCH", {"capability": "SEARCH_CANDIDATES"}),
        _plan("CANDIDATE_SEARCH", {"capability": "SEARCH_CANDIDATES", "args": []}),
        # A model may not pin a policy version, and kind/field combos are closed.
        _plan("CANDIDATE_SEARCH", {**SEARCH, "policy_version": "cap-search-v1"}),
        _plan("CANDIDATE_SEARCH", SEARCH, kind="CLARIFY"),
        _plan("CANDIDATE_SEARCH", SEARCH, kind="CONVERSE"),
        {**_plan("CANDIDATE_SEARCH", SEARCH), "clarification_code": "NEED_MORE_DETAIL"},
        {**_plan("CANDIDATE_SEARCH", SEARCH), "response_code": "GREETING"},
    ],
)
def test_invalid_plan_shape_is_rejected(proposal: object) -> None:
    _rejected(_validate(proposal, source=MESSAGE), PlanRejectionCode.SHAPE_INVALID)


# ---------------------------------------------------------------------------
# Grounded inputs (the slice-B transitional adapter is retired in slice C)
# ---------------------------------------------------------------------------


def test_validated_steps_carry_only_server_resolved_values() -> None:
    plan = _validate(
        _plan(
            "CANDIDATE_SEARCH",
            _step("SEARCH_CANDIDATES", source={"mode": "QUOTES", "quotes": [_q("Python bilən")]}),
            _evidence("1-ci", "SQL"),
        ),
        source="Python bilən namizədlər tap, 1-ci namizədin SQL sübutunu göstər",
    )
    assert isinstance(plan, ExecutablePlan), plan
    search, evidence = plan.steps
    assert search.resolved_text == "Python bilən"
    assert (evidence.candidate_ref, evidence.topic) == (1, "SQL")
    assert not hasattr(search, "args")
    # The transitional AgentDecision adapter no longer exists.
    with pytest.raises(ModuleNotFoundError):
        __import__("meyar.agent.capabilities.adapter")


def _walk_fields(model: type) -> set[str]:  # noqa: ANN001
    import typing

    names: set[str] = set()
    for name, info in getattr(model, "model_fields", {}).items():
        names.add(name)
        for arg in (info.annotation, *typing.get_args(info.annotation)):
            for inner in (arg, *typing.get_args(arg)):
                if hasattr(inner, "model_fields"):
                    names |= _walk_fields(inner)
    return names


def test_capability_inputs_carry_no_identity_or_authority_fields() -> None:
    import re

    from meyar.agent.capabilities.contracts import AgentPlanContext, AgentPlanProposal

    for model in (
        *(d.input_schema for d in CAPABILITY_REGISTRY.values()),
        AgentPlanProposal,
        AgentPlanContext,
    ):
        fields = _walk_fields(model)
        assert not {
            name for name in fields
            if re.search(
                r"(full_name|candidate_name|email|phone|identity|_id$|tenant|session_|score|"
                r"weight|_date$|scope|token)",
                name,
            )
        }, (model, fields)


def test_capabilities_package_has_no_network_or_ranking_authority() -> None:
    import pathlib

    root = pathlib.Path(capabilities_pkg.__file__).parent
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for forbidden in (
            "httpx", "requests", "openai", "anthropic", "urllib",
            "rank_candidates_for_job", "evaluate_and_score_candidate", "create_job(",
        ):
            assert forbidden not in text, (path.name, forbidden)


# ---------------------------------------------------------------------------
# Execution: registry dispatch, Layer 2, atomic activation (DB)
# ---------------------------------------------------------------------------


async def _seeded(db_session: AsyncSession, tenant_and_user, *, profiles=(PYTHON_PROFILE,)):  # noqa: ANN001, ANN202
    tenant, user, _password, membership = tenant_and_user
    candidates = []
    for content in profiles:
        candidate, _ = await seed_candidate_with_profile(
            db_session, tenant_id=tenant.id, profile_content=content
        )
        candidates.append(candidate)
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    await db_session.commit()
    return tenant, conversation, context, candidates


def _execution_ctx(db_session, tenant, context, llm) -> ExecutionContext:  # noqa: ANN001
    return ExecutionContext(
        db=db_session,
        llm=llm,
        tenant_id=tenant.id,
        session_context=TurnSessionState.of(context),
        as_of_date=AS_OF_DATE,
        embedding_config=_embedding_config(),
        embedding_provider=None,
    )


def _server_plan(source: str, *steps: dict, goal: str = "CANDIDATE_SEARCH") -> ExecutablePlan:
    # Fixture plans consume only what an earlier step of the plan produces.
    plan = validate_plan(
        _server(goal, *steps), origin=PlanOrigin.SERVER, source_text=source,
        ctx=_ctx(pre_existing_result_set=ResultSetContext(ResultSetStatus.NONE)),
    )
    assert isinstance(plan, ExecutablePlan), plan
    return plan


def _python_llm() -> FakeLLMProvider:
    return FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"]))
    )


async def _incomplete_events(db_session: AsyncSession, tenant_id) -> list[dict]:  # noqa: ANN001
    rows = (
        await db_session.scalars(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenant_id, AuditEvent.event_type == "agent.plan.incomplete"
            )
        )
    ).all()
    return [dict(row.event_metadata) for row in rows]


async def test_completed_multi_step_plan_activates_the_produced_result_set(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, _conversation, context, (candidate,) = await _seeded(db_session, tenant_and_user)
    ctx = _execution_ctx(db_session, tenant, context, _python_llm())
    plan = _server_plan("Python haqqında məlumat ver, 1-ci namizəd", SEARCH, _profile("1-ci"))
    execution = await execute_plan(plan, ctx)
    assert execution.status == PlanStatus.COMPLETED
    search, profile = execution.outcomes
    assert search.produced_result_set_id is not None
    assert execution.activated_result_set_id == search.produced_result_set_id
    assert ctx.session_context.active_result_set_id == search.produced_result_set_id
    assert profile.tool_result is not None and profile.tool_result.profile is not None
    assert profile.tool_result.profile.candidate_id == candidate.id
    assert await _incomplete_events(db_session, tenant.id) == []


@pytest.mark.parametrize(
    ("profiles", "ref", "reason"),
    [
        ((PYTHON_PROFILE,), 5, StepFailureReason.CANDIDATE_REF_OUT_OF_RANGE),
        # D-092 scenario M: an ordinal against a zero-member produced set is
        # out of range (slice C names it; the dependent step never runs).
        ((), 1, StepFailureReason.CANDIDATE_REF_OUT_OF_RANGE),
    ],
    ids=["ordinal-beyond-actual-count", "zero-result-producer"],
)
async def test_layer2_failure_keeps_produced_set_inert_and_previous_pointer_exact(
    db_session: AsyncSession, tenant_and_user, profiles: tuple, ref: int,
    reason: StepFailureReason,
) -> None:
    tenant, conversation, context, candidates = await _seeded(
        db_session, tenant_and_user, profiles=(PYTHON_PROFILE, *profiles)
    )
    # A PRE-EXISTING live ResultSet over the first candidate.
    previous = await seed_active_result_set(
        db_session, tenant_id=tenant.id, browser_session_id=context.browser_session_id,
        session_context=context, candidate_ids=[candidates[0].id],
    )
    await db_session.commit()
    llm = (
        _python_llm() if profiles else FakeLLMProvider(
            planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["NoMatch"]))
        )
    )
    ctx = _execution_ctx(db_session, tenant, context, llm)
    profile_calls: list[int] = []
    real_profile = agent_service._dispatch_profile

    async def spy_profile(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        profile_calls.append(1)
        return await real_profile(*args, **kwargs)

    topic = "Python" if profiles else "NoMatch"
    source = f"{topic} haqqında məlumat ver, {ref}-ci namizəd"
    plan = _server_plan(source, SEARCH, _profile(f"{ref}-ci"))
    sets_before = await db_session.scalar(select(func.count()).select_from(AgentResultSet))
    import meyar.agent.service as service_module

    original = service_module._dispatch_profile
    service_module._dispatch_profile = spy_profile
    try:
        execution = await execute_plan(plan, ctx)
    finally:
        service_module._dispatch_profile = original
    assert execution.status == PlanStatus.INCOMPLETE
    assert (execution.failed_step_index, execution.failure_reason) == (1, reason)
    assert execution.activated_result_set_id is None
    assert profile_calls == []  # the consuming step never executed
    # The produced row exists physically but is inert; the previous pointer
    # is restored exactly.
    (produced,) = [o.produced_result_set_id for o in execution.outcomes]
    assert produced is not None and produced != previous.id
    assert await db_session.get(AgentResultSet, produced) is not None
    assert await db_session.scalar(
        select(func.count()).select_from(AgentResultSet)
    ) == sets_before + 1
    assert ctx.session_context.active_result_set_id == previous.id
    assert await _incomplete_events(db_session, tenant.id) == [
        {"step_index": 1, "reason_code": reason.value}
    ]

    # Phase-B-shaped commit of the incomplete plan: submission-style normal
    # completion, PLAN_INCOMPLETE outcome, fixed copy, nothing activated.
    result = agent_service.plan_incomplete_result(llm, execution)
    commit = agent_service._finish_turn(
        ctx.session_context, user_turn={"role": "user", "text": source}, result=result
    )
    await agent_service.apply_agent_turn_commit(
        db_session, conversation, context, tenant_id=tenant.id, commit=commit
    )
    await db_session.commit()
    await db_session.refresh(context)
    assert context.active_result_set_id == previous.id
    assert result.outcome == AgentTurnOutcome.PLAN_INCOMPLETE and result.tool_results == []
    copy = agent_turn_outcome_message(result.outcome, result.message)
    assert "aktivləşdirilmədi" in copy
    for leaked in ("PLAN_INCOMPLETE", "RESULT_SET", "GET_CANDIDATE", str(produced)):
        assert leaked not in copy
    assert conversation.turns[-1]["outcome"] == "PLAN_INCOMPLETE"


async def test_single_step_zero_result_search_is_a_normal_empty_search(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, conversation, context, _ = await _seeded(db_session, tenant_and_user, profiles=())
    llm = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["NoMatch"]))
    )
    ctx = _execution_ctx(db_session, tenant, context, llm)
    execution = await execute_plan(_server_plan("NoMatch haqqında məlumat ver", SEARCH), ctx)
    assert execution.status == PlanStatus.COMPLETED
    assert execution.activated_result_set_id is not None  # an empty, valid set
    assert await _incomplete_events(db_session, tenant.id) == []
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation,
        session_context=context, message="NoMatch bilən namizədləri göstər",
    )
    assert result.outcome == AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT
    assert result.tool_results[0].search is not None


async def test_single_step_failure_is_its_own_truthful_outcome(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, _conversation, context, (candidate,) = await _seeded(db_session, tenant_and_user)
    previous = await seed_active_result_set(
        db_session, tenant_id=tenant.id, browser_session_id=context.browser_session_id,
        session_context=context, candidate_ids=[candidate.id],
    )
    await db_session.commit()
    # An ungrounded planner draft makes the single search non-executable.
    llm = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Kotlin"]))
    )
    ctx = _execution_ctx(db_session, tenant, context, llm)
    plan = _server_plan("Python haqqında məlumat ver", SEARCH)
    execution = await execute_plan(plan, ctx)
    assert execution.status == PlanStatus.STEP_FAILED
    assert ctx.session_context.active_result_set_id == previous.id
    assert await _incomplete_events(db_session, tenant.id) == []


async def test_human_action_only_never_executes_or_mutates(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, _conversation, context, _ = await _seeded(db_session, tenant_and_user)
    ctx = _execution_ctx(db_session, tenant, context, _python_llm())
    for capability in (CapabilityName.CREATE_JOB, CapabilityName.RANK_JOB_CANDIDATES):
        definition = CAPABILITY_REGISTRY[capability]
        forged = ExecutablePlan(
            schema_version=CAPABILITY_PLAN_SCHEMA_VERSION,
            origin=PlanOrigin.SERVER,
            goal=next(iter(definition.allowed_task_types)),
            steps=(
                ValidatedStep(
                    index=0, capability=capability, policy_version=definition.policy_version
                ),
            ),
        )
        with pytest.raises(HumanActionOnlyError):
            await execute_plan(forged, ctx)
        with pytest.raises(HumanActionOnlyError):
            await definition.executor(ctx, forged.steps[0])
    for model in (Job, JobCriteriaVersion, Evaluation):
        assert await db_session.scalar(select(func.count()).select_from(model)) == 0


# ---------------------------------------------------------------------------
# WHOLE_MESSAGE grounding + registry dispatch through the real turn
# ---------------------------------------------------------------------------


def _planner_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    received: list[str] = []
    real = planner_service.plan_candidate_search

    async def spy(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        received.append(kwargs["natural_language_request"])
        return await real(*args, **kwargs)

    # Both bindings: search plans via planner_service's own global, the
    # refine dispatcher via meyar.agent.service's imported name.
    monkeypatch.setattr(planner_service, "plan_candidate_search", spy)
    monkeypatch.setattr(agent_service, "plan_candidate_search", spy)
    return received


async def test_adversarial_model_search_quote_never_reaches_the_planner(
    db_session: AsyncSession, tenant_and_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§22 item 24 at the validator -> executor boundary: HR wrote Python, the
    model quoted Java — SOURCE_NOT_GROUNDED, zero executions; the planner
    never receives "Java". A WHOLE_MESSAGE plan stays bound to Python."""
    received = _planner_spy(monkeypatch)
    tenant, _conversation, context, _ = await _seeded(db_session, tenant_and_user)
    spy = _SpyExecutors()
    injected = _plan(
        "CANDIDATE_SEARCH",
        _step("SEARCH_CANDIDATES", source={"mode": "QUOTES", "quotes": [_q("Java bilən")]}),
    )
    rejection = validate_plan(
        injected, origin=PlanOrigin.MODEL, source_text=MESSAGE, offered=ALL_OFFERED,
        ctx=_ctx(pre_existing_result_set=ResultSetContext(ResultSetStatus.NONE)),
        registry=spy.registry,
    )
    _rejected(rejection, PlanRejectionCode.SOURCE_NOT_GROUNDED)
    assert spy.calls == [] and received == []
    plan = _validate(
        _plan("CANDIDATE_SEARCH", SEARCH), source=MESSAGE,
        pre_existing_result_set=ResultSetContext(ResultSetStatus.NONE),
    )
    assert isinstance(plan, ExecutablePlan)
    llm = _python_llm()
    execution = await execute_plan(plan, _execution_ctx(db_session, tenant, context, llm))
    assert received == [MESSAGE]
    assert all("Java" not in text for text in received + llm.planner_requests)
    (outcome,) = execution.outcomes
    result_set = await db_session.get(AgentResultSet, outcome.produced_result_set_id)
    assert result_set is not None
    assert result_set.request_sha256 == hashlib.sha256(MESSAGE.encode()).hexdigest()


async def test_model_routed_turn_searches_the_whole_message_end_to_end(
    db_session: AsyncSession, tenant_and_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    received = _planner_spy(monkeypatch)
    dispatched: list[str] = []
    real_dispatch = agent_service._dispatch_search

    async def spy_dispatch(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        dispatched.append(kwargs["natural_language_request"])
        return await real_dispatch(*args, **kwargs)

    # Executors resolve the dispatcher at call time: proves registry
    # dispatch reaches the unchanged domain function.
    monkeypatch.setattr(agent_service, "_dispatch_search", spy_dispatch)
    plans: list[ExecutablePlan] = []
    real_execute = agent_service.execute_plan

    async def spy_execute(plan, ctx, **kwargs):  # noqa: ANN001, ANN003, ANN202
        plans.append(plan)
        return await real_execute(plan, ctx, **kwargs)

    monkeypatch.setattr(agent_service, "execute_plan", spy_execute)
    tenant, conversation, context, (candidate,) = await _seeded(db_session, tenant_and_user)
    message = "Python haqqında məlumat ver"
    llm = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"])),
        agent_plan=search_plan(),
    )
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation,
        session_context=context, message=message,
    )
    # Genuinely model-routed, and exactly ONE proposal: no post-tool
    # "what next" call (slice C replaced the closing FINAL_ANSWER decision).
    assert llm.agent_call_count == 1
    assert received == [message] and dispatched == [message]
    assert [(p.origin, p.steps[0].capability) for p in plans] == [
        (PlanOrigin.MODEL, CapabilityName.SEARCH_CANDIDATES)
    ]
    assert result.outcome == AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT
    (tool,) = result.tool_results
    assert tool.tool_name == AgentActionType.SEARCH_CANDIDATES  # unchanged tag
    assert tool.search is not None
    assert [r.candidate_id for r in tool.search.response.search_response.results] == [
        candidate.id
    ]
    executed = (
        await db_session.scalars(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenant.id, AuditEvent.event_type == "agent.tool.executed"
            )
        )
    ).all()
    assert [dict(e.event_metadata) for e in executed] == [
        {
            "tool_name": "SEARCH_CANDIDATES",
            "tool_call_index": 1,
            "capability": "SEARCH_CANDIDATES",
            "capability_version": "cap-search-v1",
        }
    ]


async def test_forced_search_is_unchanged(
    db_session: AsyncSession, tenant_and_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    received = _planner_spy(monkeypatch)
    tenant, conversation, context, _ = await _seeded(db_session, tenant_and_user)
    llm = _python_llm()
    message = "Python bilən namizədləri göstər"
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation,
        session_context=context, message=message,
    )
    assert llm.agent_call_count == 0
    assert received == [message]
    assert result.outcome == AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT


async def test_refine_filter_is_a_grounded_source_slice_and_audits_capability(
    db_session: AsyncSession, tenant_and_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice C closes the slice-B residual: the refine filter is the exact
    quoted slice of HR's message (never model text); a fabricated filter is
    SOURCE_NOT_GROUNDED before the planner is ever called."""
    received = _planner_spy(monkeypatch)
    tenant, conversation, context, (candidate,) = await _seeded(db_session, tenant_and_user)
    await seed_active_result_set(
        db_session, tenant_id=tenant.id, browser_session_id=context.browser_session_id,
        session_context=context, candidate_ids=[candidate.id],
    )
    await db_session.commit()
    fabricated = FakeLLMProvider(agent_plan=refine_plan("Java bilənlər"))
    rejected = await _run(
        db_session, fabricated, tenant_id=tenant.id, conversation=conversation,
        session_context=context, message="bunlardan Python bilənlər",
    )
    assert rejected.outcome == AgentTurnOutcome.CLARIFICATION_REQUESTED
    assert received == [] and rejected.tool_results == []

    llm = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"])),
        agent_plan=refine_plan("Python bilənlər"),
    )
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation,
        session_context=context, message="bunlardan Python bilənlər",
    )
    assert received == ["Python bilənlər"]
    (tool,) = result.tool_results
    assert tool.tool_name == AgentActionType.REFINE_CANDIDATE_RESULTS
    executed = (
        await db_session.scalars(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenant.id, AuditEvent.event_type == "agent.tool.executed"
            )
        )
    ).all()
    (metadata,) = [dict(e.event_metadata) for e in executed]
    assert metadata == {
        "tool_name": "REFINE_CANDIDATE_RESULTS",
        "tool_call_index": 1,
        "capability": "REFINE_RESULTS",
        "capability_version": "cap-refine-v1",
    }
    snapshot = ConversationSnapshot.of(conversation)
    assert snapshot.turns[-1]["outcome"] == AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT.value


async def test_model_draft_proposal_stays_rejected_and_audited(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, conversation, context, _ = await _seeded(db_session, tenant_and_user)
    llm = FakeLLMProvider(agent_plan=vacancy_proposal())
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation,
        session_context=context, message="salam, necəsən?",
    )
    assert result.outcome == AgentTurnOutcome.CLARIFICATION_REQUESTED
    assert result.tool_results == [] and llm.jd_draft_call_count == 0
    events = (
        await db_session.scalars(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenant.id,
                AuditEvent.event_type.in_(
                    ["agent.plan.rejected", "agent.entry.action_rejected", "agent.tool.executed"]
                ),
            )
        )
    ).all()
    by_type = {e.event_type: dict(e.event_metadata) for e in events}
    assert by_type["agent.plan.rejected"] == {
        "reason_code": "NOT_MODEL_PROPOSABLE", "schema_version": AGENT_PLAN_SCHEMA_VERSION,
    }
    assert "agent.entry.action_rejected" in by_type and "agent.tool.executed" not in by_type


async def test_protected_evidence_topic_is_rejected_in_turn_with_fixed_copy(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, conversation, context, (candidate,) = await _seeded(db_session, tenant_and_user)
    await seed_active_result_set(
        db_session, tenant_id=tenant.id, browser_session_id=context.browser_session_id,
        session_context=context, candidate_ids=[candidate.id],
    )
    await db_session.commit()
    llm = FakeLLMProvider(agent_plan=evidence_plan("birincinin", "cins"))
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation,
        session_context=context, message="birincinin cins sübutlarını göstər",
    )
    assert result.outcome == AgentTurnOutcome.CLARIFICATION_REQUESTED
    assert result.message == agent_service.PLAN_REJECTED_COPY
    assert result.tool_results == []
    for leaked in ("PROHIBITED", "GET_CANDIDATE_EVIDENCE", "cins"):
        assert leaked not in (result.message or "")
