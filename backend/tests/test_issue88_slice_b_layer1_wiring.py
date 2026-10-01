"""Issue #88 slice B review correction — Layer 1 and ``agent.plan.validated``
in the REAL ``execute_agent_turn`` / ``POST /ui/agent`` wiring (D-092 §11.1,
§11.3, §19.2).

* The ValidationContext is built from read-only server authority: a missing,
  expired, stale (member-scoped, #86) or statically out-of-range pre-existing
  ResultSet is rejected by Layer 1 with ZERO executor calls and TODAY's
  truthful outward outcome, message, not-found card and domain audit.
* A change after Layer-1 validation but before execution is still caught by
  the existing executor checks (Layer 2 / TOCTOU).
* Every successfully validated plan attempt emits exactly one privacy-safe
  ``agent.plan.validated``.

Synthetic data only; deterministic FakeLLMProvider."""

import json
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import BrowserTestClient as AsyncClient
from fakes import FakeLLMProvider
from search_helpers import (
    seed_active_result_set,
    seed_candidate_with_profile,
    seed_next_profile_version,
)
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_agent_service import _new_conversation, _run
from test_ui_agent_routes import (
    _login_and_csrf,
    local_ui_settings,  # noqa: F401 - pytest fixture re-export
)

from meyar.agent import service as agent_service
from meyar.agent.capabilities import (
    CAPABILITY_PLAN_SCHEMA_VERSION,
    ExecutablePlan,
    PlanOrigin,
    ResultSetContext,
    ResultSetStatus,
    ValidationContext,
    validate_plan,
)
from meyar.agent.capabilities.adapter import plan_for_model_decision, search_plan
from meyar.agent.capabilities.provenance import plan_sha256, plan_validated_metadata
from meyar.agent.schemas import AgentActionType, AgentDecision, AgentTurnOutcome
from meyar.config import Settings
from meyar.llm.dependency import get_llm_provider
from meyar.main import app
from meyar.models.agent_result_set import AgentResultSet
from meyar.models.audit_event import AuditEvent
from meyar.search.planner_schemas import PlannerDraft
from meyar.search.schemas import RequiredFilters
from meyar.ui.presentation import AGENT_TURN_OUTCOME_TEXT

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _profile(skill: str) -> dict:
    return {
        "skills": [
            {
                "name": skill,
                "category": None,
                "evidence": [{"page": 1, "block_index": 0, "quote": f"Synthetic: {skill}"}],
            }
        ],
        "employment_history": [], "education": [], "certifications": [],
        "languages": [], "projects": [],
    }


class _ExecutorSpy:
    """Counts the real domain dispatchers the registry executors call (they
    resolve ``meyar.agent.service._dispatch_*`` at call time) and every
    ``execute_plan`` invocation."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[str] = []
        for name in ("_dispatch_profile", "_dispatch_evidence", "_dispatch_refine",
                     "_dispatch_search", "execute_plan"):
            real = getattr(agent_service, name)

            async def spy(*args, _name=name, _real=real, **kwargs):  # noqa: ANN002, ANN003, ANN202
                self.calls.append(_name)
                return await _real(*args, **kwargs)

            monkeypatch.setattr(agent_service, name, spy)


async def _events(db: AsyncSession, tenant_id, event_type: str) -> list[dict]:  # noqa: ANN001
    rows = (
        await db.scalars(
            select(AuditEvent)
            .where(AuditEvent.tenant_id == tenant_id, AuditEvent.event_type == event_type)
            .order_by(AuditEvent.created_at, AuditEvent.id)
        )
    ).all()
    return [dict(row.event_metadata) for row in rows]


async def _setup(db_session: AsyncSession, tenant_and_user, *skills: str):  # noqa: ANN001, ANN202
    tenant, user, _password, membership = tenant_and_user
    candidates = [
        (await seed_candidate_with_profile(
            db_session, tenant_id=tenant.id, profile_content=_profile(skill)
        ))[0]
        for skill in skills
    ]
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    result_set = None
    if candidates:
        result_set = await seed_active_result_set(
            db_session, tenant_id=tenant.id, browser_session_id=context.browser_session_id,
            session_context=context, candidate_ids=[c.id for c in candidates],
        )
    await db_session.commit()
    return tenant, conversation, context, candidates, result_set


def _decision(action: AgentActionType, **fields) -> FakeLLMProvider:  # noqa: ANN003
    return FakeLLMProvider(agent_decision=AgentDecision(action=action, **fields))


# ---------------------------------------------------------------------------
# Blocker 1 — Layer 1 really runs in execute_agent_turn
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action", [AgentActionType.GET_CANDIDATE_PROFILE, AgentActionType.GET_CANDIDATE_EVIDENCE]
)
@pytest.mark.parametrize(
    ("case", "outcome", "reason", "plan_code"),
    [
        ("missing", AgentTurnOutcome.CANDIDATE_REF_NOT_FOUND, "NOT_FOUND",
         "RESULT_CONTEXT_REQUIRED"),
        ("expired", AgentTurnOutcome.RESULT_SET_EXPIRED, "EXPIRED", "RESULT_SET_EXPIRED"),
        ("stale", AgentTurnOutcome.RESULT_SET_STALE, "STALE", "RESULT_SET_STALE"),
        ("out-of-range", AgentTurnOutcome.CANDIDATE_REF_NOT_FOUND, "ORDINAL_OUT_OF_RANGE",
         "CANDIDATE_REF_OUT_OF_RANGE"),
    ],
)
async def test_layer1_rejects_reference_with_todays_outcome_and_zero_executors(
    db_session: AsyncSession, tenant_and_user, monkeypatch: pytest.MonkeyPatch,
    case: str, outcome: AgentTurnOutcome, reason: str, plan_code: str,
    action: AgentActionType,
) -> None:
    skills = () if case == "missing" else ("Python",)
    tenant, conversation, context, candidates, result_set = await _setup(
        db_session, tenant_and_user, *skills
    )
    ref = 3 if case == "out-of-range" else 1
    if case == "expired":
        await db_session.execute(
            update(AgentResultSet)
            .where(AgentResultSet.id == result_set.id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    if case == "stale":
        await seed_next_profile_version(
            db_session, tenant_id=tenant.id, candidate=candidates[0],
            profile_content=_profile("Python"),
        )
    await db_session.commit()
    spy = _ExecutorSpy(monkeypatch)
    result = await _run(
        db_session, _decision(action, candidate_ref=ref), tenant_id=tenant.id,
        conversation=conversation, session_context=context, message="birincinin profilini aç",
    )
    assert spy.calls == []  # Layer 1: ZERO executors, no execute_plan
    # Today's truthful outward behaviour: outcome, no model text, the same
    # not-found card for the requested ordinal.
    assert result.outcome == outcome
    assert result.message is None
    (card,) = result.tool_results
    payload = card.profile if action == AgentActionType.GET_CANDIDATE_PROFILE else card.evidence
    assert card.tool_name == action and payload is not None
    assert (payload.found, payload.candidate_ref, payload.candidate_id) == (False, ref, None)
    # Domain audit continuity + Layer-1 provenance; nothing executed.
    expected_reference = {"reason": reason, "context_epoch": context.context_epoch}
    if case == "out-of-range":
        expected_reference["candidate_ref"] = ref
    assert await _events(db_session, tenant.id, "agent.result_set.reference_rejected") == [
        expected_reference
    ]
    assert await _events(db_session, tenant.id, "agent.plan.rejected") == [
        {"reason_code": plan_code, "schema_version": CAPABILITY_PLAN_SCHEMA_VERSION}
    ]
    assert await _events(db_session, tenant.id, "agent.plan.validated") == []
    assert await _events(db_session, tenant.id, "agent.tool.executed") == []


async def test_issue86_unrelated_changed_member_never_invalidates_a_reference(
    db_session: AsyncSession, tenant_and_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, conversation, context, (first, second), _rs = await _setup(
        db_session, tenant_and_user, "Python", "Java"
    )
    await seed_next_profile_version(
        db_session, tenant_id=tenant.id, candidate=second, profile_content=_profile("Java")
    )
    await db_session.commit()
    spy = _ExecutorSpy(monkeypatch)
    resolved = await _run(
        db_session, _decision(AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=1),
        tenant_id=tenant.id, conversation=conversation, session_context=context,
        message="birincinin profilini aç",
    )
    # Member 1 is authoritative: the normal executor path ran and found it.
    assert spy.calls == ["execute_plan", "_dispatch_profile"]
    assert resolved.outcome == AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT
    (card,) = resolved.tool_results
    assert card.profile is not None and card.profile.candidate_id == first.id
    spy.calls.clear()
    stale = await _run(
        db_session, _decision(AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=2),
        tenant_id=tenant.id, conversation=conversation, session_context=context,
        message="ikincinin profilini aç",
    )
    assert spy.calls == []
    assert stale.outcome == AgentTurnOutcome.RESULT_SET_STALE


@pytest.mark.parametrize(
    ("case", "outcome", "reason", "plan_code"),
    [
        ("missing", AgentTurnOutcome.CLARIFICATION_REQUESTED, "NOT_FOUND",
         "RESULT_CONTEXT_REQUIRED"),
        ("expired", AgentTurnOutcome.RESULT_SET_EXPIRED, "EXPIRED", "RESULT_SET_EXPIRED"),
        ("stale-other-member", AgentTurnOutcome.RESULT_SET_STALE, "STALE", "RESULT_SET_STALE"),
    ],
)
async def test_layer1_rejects_refinement_with_todays_outcome_and_zero_executors(
    db_session: AsyncSession, tenant_and_user, monkeypatch: pytest.MonkeyPatch,
    case: str, outcome: AgentTurnOutcome, reason: str, plan_code: str,
) -> None:
    """FORCE_RESULT_LIMIT ("ilk 1") is a server-built REFINE plan; a
    refinement is a subset of the WHOLE snapshot, so any changed member
    makes it stale (#86)."""
    skills = () if case == "missing" else ("Python", "Java")
    tenant, conversation, context, candidates, result_set = await _setup(
        db_session, tenant_and_user, *skills
    )
    if case == "expired":
        await db_session.execute(
            update(AgentResultSet)
            .where(AgentResultSet.id == result_set.id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    if case == "stale-other-member":
        await seed_next_profile_version(
            db_session, tenant_id=tenant.id, candidate=candidates[1],
            profile_content=_profile("Java"),
        )
    await db_session.commit()
    spy = _ExecutorSpy(monkeypatch)
    llm = FakeLLMProvider()
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation,
        session_context=context, message="ilk 1",
    )
    assert spy.calls == [] and llm.agent_call_count == 0
    assert result.outcome == outcome and result.tool_results == []
    if case == "missing":
        assert result.message == agent_service._agent_response_text(
            agent_service.AgentResponseCode.RESULT_CONTEXT_REQUIRED
        )
    assert await _events(db_session, tenant.id, "agent.result_set.refine_rejected") == [
        {"reason": reason, "context_epoch": context.context_epoch}
    ]
    assert await _events(db_session, tenant.id, "agent.plan.rejected") == [
        {"reason_code": plan_code, "schema_version": CAPABILITY_PLAN_SCHEMA_VERSION}
    ]


async def test_valid_result_set_takes_the_normal_executor_path(
    db_session: AsyncSession, tenant_and_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, conversation, context, (candidate, _other), _rs = await _setup(
        db_session, tenant_and_user, "Python", "Java"
    )
    spy = _ExecutorSpy(monkeypatch)
    refined = await _run(
        db_session, FakeLLMProvider(), tenant_id=tenant.id, conversation=conversation,
        session_context=context, message="ilk 1",
    )
    assert spy.calls == ["execute_plan", "_dispatch_refine"]
    assert refined.outcome == AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT
    assert await _events(db_session, tenant.id, "agent.plan.rejected") == []
    (validated,) = await _events(db_session, tenant.id, "agent.plan.validated")
    assert validated["capabilities"] == ["REFINE_RESULTS"]


@pytest.mark.parametrize("change", ["member-changed", "expired"])
async def test_change_after_layer1_is_still_caught_by_executor_checks(
    db_session: AsyncSession, tenant_and_user, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """TOCTOU: Layer 1 validated against authority that changes before the
    executor runs; the unchanged executor checks (Layer 2 / defense in
    depth) still fail closed."""
    tenant, conversation, context, (candidate,), result_set = await _setup(
        db_session, tenant_and_user, "Python"
    )
    real_validate = agent_service.validate_plan

    async def mutate() -> None:
        if change == "member-changed":
            await seed_next_profile_version(
                db_session, tenant_id=tenant.id, candidate=candidate,
                profile_content=_profile("Python"),
            )
        else:
            await db_session.execute(
                update(AgentResultSet)
                .where(AgentResultSet.id == result_set.id)
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        await db_session.flush()

    pending: list = []

    def validate_then_change(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        plan = real_validate(*args, **kwargs)
        assert isinstance(plan, ExecutablePlan)  # Layer 1 genuinely passed
        pending.append(mutate)
        return plan

    monkeypatch.setattr(agent_service, "validate_plan", validate_then_change)
    real_execute = agent_service.execute_plan

    async def execute_after_change(plan, ctx, **kwargs):  # noqa: ANN001, ANN003, ANN202
        for step in pending:
            await step()
        return await real_execute(plan, ctx, **kwargs)

    monkeypatch.setattr(agent_service, "execute_plan", execute_after_change)
    dispatched: list[int] = []
    real_profile = agent_service._dispatch_profile

    async def spy_profile(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        dispatched.append(1)
        return await real_profile(*args, **kwargs)

    monkeypatch.setattr(agent_service, "_dispatch_profile", spy_profile)
    result = await _run(
        db_session, _decision(AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=1),
        tenant_id=tenant.id, conversation=conversation, session_context=context,
        message="birincinin profilini aç",
    )
    assert dispatched == [1]  # the executor ran and its own check rejected
    assert result.outcome == (
        AgentTurnOutcome.RESULT_SET_STALE if change == "member-changed"
        else AgentTurnOutcome.RESULT_SET_EXPIRED
    )
    assert len(await _events(db_session, tenant.id, "agent.plan.validated")) == 1
    (rejected,) = await _events(db_session, tenant.id, "agent.result_set.reference_rejected")
    assert rejected["reason"] == ("STALE" if change == "member-changed" else "EXPIRED")


@pytest.mark.parametrize("case", ["missing", "expired", "stale", "out-of-range", "valid"])
async def test_http_route_wiring_runs_layer1(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings, monkeypatch: pytest.MonkeyPatch, case: str,  # noqa: F811
) -> None:
    tenant, user, password, _m = tenant_and_user
    (candidate, _pv) = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()
    search_llm = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"]))
    )
    app.dependency_overrides[get_llm_provider] = lambda: search_llm
    csrf = await _login_and_csrf(client, user.username, password)
    if case != "missing":
        searched = await client.post(
            "/ui/agent", data={"message": "Python bilən namizədləri göstər", "csrf_token": csrf}
        )
        assert searched.status_code == 200
    if case == "expired":
        await db_session.execute(
            update(AgentResultSet).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    if case == "stale":
        await seed_next_profile_version(
            db_session, tenant_id=tenant.id, candidate=candidate,
            profile_content=_profile("Python"),
        )
    await db_session.commit()
    ref = 4 if case == "out-of-range" else 1
    app.dependency_overrides[get_llm_provider] = lambda: _decision(
        AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=ref
    )
    spy = _ExecutorSpy(monkeypatch)
    response = await client.post(
        "/ui/agent", data={"message": "birincinin profilini aç", "csrf_token": csrf}
    )
    assert response.status_code == 200
    expected = {
        "missing": "CANDIDATE_REF_NOT_FOUND", "expired": "RESULT_SET_EXPIRED",
        "stale": "RESULT_SET_STALE", "out-of-range": "CANDIDATE_REF_NOT_FOUND",
    }.get(case)
    if expected is None:
        assert spy.calls == ["execute_plan", "_dispatch_profile"]
        assert await _events(db_session, tenant.id, "agent.plan.rejected") == []
    else:
        assert spy.calls == []
        assert AGENT_TURN_OUTCOME_TEXT[expected] in response.text
        (rejected,) = await _events(db_session, tenant.id, "agent.plan.rejected")
        assert rejected["reason_code"] != "PROHIBITED_ATTRIBUTE"
        # Capability names / plan codes never reach HR (the outcome value is
        # an existing data attribute of the turn markup, unchanged).
        for leaked in ("GET_CANDIDATE_PROFILE", "CANDIDATE_REF_OUT_OF_RANGE",
                       "RESULT_CONTEXT_REQUIRED", "capability-plan-v1"):
            assert leaked not in response.text


# ---------------------------------------------------------------------------
# Blocker 2 — agent.plan.validated
# ---------------------------------------------------------------------------

PLAN_VALIDATED_KEYS = {"plan_sha256", "step_count", "capabilities", "schema_version"}


def _assert_safe(metadata: dict, *forbidden_text: str) -> None:
    assert set(metadata) == PLAN_VALIDATED_KEYS
    assert re.fullmatch(r"[0-9a-f]{64}", metadata["plan_sha256"])
    assert metadata["schema_version"] == CAPABILITY_PLAN_SCHEMA_VERSION
    assert isinstance(metadata["step_count"], int) and 1 <= metadata["step_count"] <= 3
    assert metadata["step_count"] == len(metadata["capabilities"])
    from meyar.agent.capabilities import CapabilityName

    assert set(metadata["capabilities"]) <= {name.value for name in CapabilityName}
    serialized = json.dumps(metadata, ensure_ascii=False)
    assert UUID_RE.search(serialized) is None
    for text in forbidden_text:
        assert text not in serialized


async def test_forced_and_model_searches_each_emit_exactly_one_safe_validated_event(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, conversation, context, _c, _rs = await _setup(db_session, tenant_and_user)
    forced_message = "Python bilən namizədləri göstər"
    await _run(
        db_session,
        FakeLLMProvider(
            planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"]))
        ),
        tenant_id=tenant.id, conversation=conversation, session_context=context,
        message=forced_message,
    )
    (forced,) = await _events(db_session, tenant.id, "agent.plan.validated")
    _assert_safe(forced, forced_message, "Python")
    assert (forced["step_count"], forced["capabilities"]) == (1, ["SEARCH_CANDIDATES"])

    model_message = "Python haqqında məlumat ver"
    llm = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"])),
        agent_decisions=[
            AgentDecision(
                action=AgentActionType.SEARCH_CANDIDATES, search_query="Java bilən namizədlər"
            ),
            AgentDecision(
                action=AgentActionType.SEARCH_CANDIDATES,
                search_query="Java bilən başqa namizədlər",
            ),
            AgentDecision(action=AgentActionType.FINAL_ANSWER, response_code="ACKNOWLEDGEMENT"),
        ],
    )
    await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation,
        session_context=context, message=model_message,
    )
    validated = await _events(db_session, tenant.id, "agent.plan.validated")
    # Both adapted proposals were validated attempts; only ONE search ran.
    # (Events of one transaction share created_at: select by content.)
    assert len(validated) == 3
    model_events = [e for e in validated if e["plan_sha256"] != forced["plan_sha256"]]
    assert len(model_events) == 2
    for metadata in model_events:
        _assert_safe(metadata, model_message, "Java", "Python")
        assert metadata["capabilities"] == ["SEARCH_CANDIDATES"]
    assert model_events[0]["plan_sha256"] == model_events[1]["plan_sha256"]
    executed = await _events(db_session, tenant.id, "agent.tool.executed")
    assert [e["tool_call_index"] for e in executed] == [1, 1]  # one per turn


async def test_rejected_plan_emits_rejected_and_no_validated(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, conversation, context, _c, _rs = await _setup(db_session, tenant_and_user)
    await _run(
        db_session, _decision(AgentActionType.DRAFT_JOB_CRITERIA), tenant_id=tenant.id,
        conversation=conversation, session_context=context, message="salam, necəsən?",
    )
    assert len(await _events(db_session, tenant.id, "agent.plan.rejected")) == 1
    assert await _events(db_session, tenant.id, "agent.plan.validated") == []


def _validated(proposal: dict, *, source: str, origin: PlanOrigin = PlanOrigin.MODEL):  # noqa: ANN202
    plan = validate_plan(
        proposal, origin=origin, source_text=source,
        ctx=ValidationContext(
            principal_scopes=frozenset({"candidates:read"}),
            pre_existing_result_set=ResultSetContext(ResultSetStatus.VALID, member_count=5),
            pending_draft_live=False, confirmed_job_in_session=False, max_tool_calls=3,
        ),
    )
    assert isinstance(plan, ExecutablePlan)
    return plan


def test_plan_sha256_is_deterministic_and_tracks_safe_structural_meaning() -> None:
    message = "Python bilən namizədlər"
    base = _validated(search_plan(), source=message)
    assert plan_sha256(base) == plan_sha256(_validated(search_plan(), source=message))
    # Same model text, different model search_query: identical (it is dropped).
    a = plan_for_model_decision(
        AgentDecision(action=AgentActionType.SEARCH_CANDIDATES, search_query="Java"),
        message=message,
    )
    assert plan_sha256(_validated(a, source=message)) == plan_sha256(base)
    profile_1 = plan_for_model_decision(
        AgentDecision(action=AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=1),
        message=message,
    )
    profile_2 = plan_for_model_decision(
        AgentDecision(action=AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=2),
        message=message,
    )
    evidence_sql = plan_for_model_decision(
        AgentDecision(
            action=AgentActionType.GET_CANDIDATE_EVIDENCE, candidate_ref=1, evidence_topic="SQL"
        ),
        message=message,
    )
    evidence_java = plan_for_model_decision(
        AgentDecision(
            action=AgentActionType.GET_CANDIDATE_EVIDENCE, candidate_ref=1, evidence_topic="Java"
        ),
        message=message,
    )
    hashes = {
        plan_sha256(base),
        plan_sha256(_validated(search_plan(), source="Java bilən namizədlər")),
        plan_sha256(_validated(search_plan(), source=message, origin=PlanOrigin.SERVER)),
        plan_sha256(_validated(profile_1, source=message)),
        plan_sha256(_validated(profile_2, source=message)),
        plan_sha256(_validated(evidence_sql, source=message)),
        plan_sha256(_validated(evidence_java, source=message)),
    }
    assert len(hashes) == 7
    # The validated metadata holds no text, topic or id.
    metadata = plan_validated_metadata(_validated(evidence_sql, source=message))
    _assert_safe(metadata, message, "SQL", "Python")
    assert str(uuid.uuid4())[:8] not in json.dumps(metadata)
