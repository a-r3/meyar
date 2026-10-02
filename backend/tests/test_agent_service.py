"""Slice 2 — bounded read-only agent orchestration (issue #31, D-035).
Service-level tests: no HTTP, direct meyar.agent.service.run_agent_turn
calls against a real Postgres session with FakeLLMProvider doubles."""

from datetime import date

import pytest
from agent_plans import (
    clarify,
    converse,
    evidence_plan,
    profile_plan,
    refine_plan,
    search_plan,
    vacancy_proposal,
)
from fakes import FakeLLMProvider
from pydantic import ValidationError
from search_helpers import (
    active_result_set_candidate_ids,
    open_test_conversation,
    seed_active_result_set,
    seed_candidate_with_profile,
)
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.agent.schemas import (
    AgentActionType,
    AgentTurnOutcome,
)
from meyar.agent.service import run_agent_turn
from meyar.llm.provider import ModelTimeoutError, ModelUnavailableError
from meyar.models.agent_result_set import AgentResultSet, AgentResultSetMember
from meyar.search.schemas import EmbeddingSearchConfig
from meyar.services.browser_session_repo import create_browser_session

AS_OF_DATE = date(2026, 1, 1)
OWNER_COMPOUND_QUERY = (
    "Ən az 5 il Python təcrübəsi olan və ingilis dili B2 "
    "və ya daha yüksək olan 5 namizəd göstər."
)
EMPTY_PROFILE = {
    "skills": [],
    "employment_history": [],
    "education": [],
    "certifications": [],
    "languages": [],
    "projects": [],
}


def _profile(*skills: str, quote: str = "Synthetic evidence") -> dict:
    def skill_quote(skill: str) -> str:
        return quote if skill.lower() in quote.lower() else f"{quote}: {skill}"

    return {
        **EMPTY_PROFILE,
        "skills": [
            {
                "name": skill,
                "category": None,
                "evidence": [{"page": 1, "block_index": 0, "quote": skill_quote(skill)}],
            }
            for skill in skills
        ],
        "employment_history": [
            {
                "title": "Backend Developer",
                "organization": "Synthetic Co",
                "start_date": "2020",
                "end_date": None,
                "is_current": True,
                "evidence": [
                    {
                        "page": 1,
                        "block_index": 0,
                        "quote": "Backend Developer at Synthetic Co since 2020; current.",
                    }
                ],
            }
        ],
    }


def _embedding_config() -> EmbeddingSearchConfig:
    return EmbeddingSearchConfig(
        provider="fake-embedding",
        model_name="fake-embedding-model-v1",
        model_revision="",
        serializer_version="v1",
        embedding_dimensions=8,
    )


async def test_agent_owner_compound_search_preserves_filters_and_returns_positive_profile(
    db_session: AsyncSession, tenant_and_user
) -> None:
    from sqlalchemy import select

    from meyar.models.audit_event import AuditEvent

    tenant, user, _password, membership = tenant_and_user
    content = _profile("Python")
    content["employment_history"] = [
        {
            "title": "Engineer",
            "organization": "Synthetic Co",
            "start_date": "2019",
            "end_date": "2025",
            "is_current": False,
            "evidence": [
                {"page": 1, "block_index": 0, "quote": "Engineer Synthetic Co 2019 2025"}
            ],
        }
    ]
    content["skill_experience"] = [
        {
            "skill_name": "Python",
            "employment_index": 0,
            "start_date": "2019",
            "end_date": "2025",
            "is_current": False,
            "evidence": [
                {"page": 1, "block_index": 0, "quote": "Python Engineer Synthetic Co 2019 - 2025"}
            ],
        }
    ]
    content["languages"] = [
        {
            "language": "English",
            "proficiency": "B2",
            "evidence": [{"page": 1, "block_index": 0, "quote": "English B2"}],
        }
    ]
    candidate, _ = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=content
    )
    await db_session.commit()

    llm = FakeLLMProvider(
        agent_plans=[
            search_plan(),
        ]
    )
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message=OWNER_COMPOUND_QUERY,
    )
    search = result.tool_results[0].search
    assert search is not None
    planned = search.response
    assert planned.plan.executable
    assert planned.plan.reason_codes == []
    request = planned.plan.search_request
    assert request is not None
    assert request.limit == 5
    assert request.as_of_date == AS_OF_DATE
    assert [(item.value, item.min_years) for item in request.required_filters.skill_experience] == [
        ("Python", 5.0)
    ]
    assert [
        (item.value, item.required_level) for item in request.required_filters.language_levels
    ] == [
        ("English", "B2")
    ]
    assert planned.search_response is not None
    assert {item.candidate_id for item in planned.search_response.results} == {candidate.id}
    assert "MANDATORY_REQUIREMENT_DOWNGRADED" not in (result.message or "")

    events = await db_session.scalars(select(AuditEvent).where(AuditEvent.tenant_id == tenant.id))
    metadata = " ".join(str(event.event_metadata) for event in events)
    assert OWNER_COMPOUND_QUERY not in metadata
    assert "Python Engineer Synthetic Co" not in metadata


async def _new_conversation(db_session: AsyncSession, tenant, user, membership):
    session, _raw_token = await create_browser_session(
        db_session, user_id=user.id, tenant_membership_id=membership.id, ttl_hours=8
    )
    await db_session.flush()
    conversation, context = await open_test_conversation(
        db_session,
        tenant_id=tenant.id,
        user_id=user.id,
        membership_id=membership.id,
        browser_session_id=session.id,
    )
    return conversation, context


async def _run(
    db_session,
    llm,
    *,
    tenant_id,
    conversation,
    session_context,
    message,
    max_tool_calls=3,
    max_context_turns=8,
):
    return await run_agent_turn(
        db_session,
        llm,
        tenant_id=tenant_id,
        conversation=conversation,
        session_context=session_context,
        user_message=message,
        as_of_date=AS_OF_DATE,
        embedding_config=_embedding_config(),
        embedding_provider=None,
        max_tool_calls=max_tool_calls,
        max_context_turns=max_context_turns,
    )


def _proposal(**fields):
    from agent_plans import proposal

    return proposal(**fields)


def test_agent_plan_schema_rejects_mismatched_and_model_authored_arguments() -> None:
    """Issue #88 slice C (replaces the AgentDecision shape test): the model
    contract has no field for model-authored search text, ordinals, limits
    or topics — the retired AgentDecision fields fail ``extra="forbid"``."""
    plan_step = {"capability": "SEARCH_CANDIDATES", "args": {"source": {"mode": "WHOLE_MESSAGE"}}}
    for bad_args in (
        {"search_query": "Python"},
        {"source": {"mode": "WHOLE_MESSAGE"}, "search_query": "Python"},
        {"source": {"mode": "QUOTES"}},  # QUOTES needs >= 1 quote
        {"source": {"mode": "WHOLE_MESSAGE", "quotes": [{"quote": "x"}]}},
    ):
        with pytest.raises(ValidationError):
            _proposal(kind="PLAN", goal="CANDIDATE_SEARCH",
                      steps=[{"capability": "SEARCH_CANDIDATES", "args": bad_args}])
    for capability, bad_args in (
        ("GET_CANDIDATE_PROFILE", {"candidate_ref": 1}),
        ("GET_CANDIDATE_PROFILE", {"ref_quote": {"quote": "birinci"}, "candidate_ref": 1}),
        ("GET_CANDIDATE_EVIDENCE", {"ref_quote": {"quote": "1"}, "evidence_topic": "Python"}),
        ("REFINE_RESULTS", {"filter_query": "SQL"}),
        ("REFINE_RESULTS", {"limit": 3}),
        ("REFINE_RESULTS", {}),  # at least one of filter_source / limit_quote
    ):
        with pytest.raises(ValidationError):
            _proposal(kind="PLAN", goal="RESULT_FOLLOWUP",
                      steps=[{"capability": capability, "args": bad_args}])
    # Extra unknown fields are rejected everywhere (extra=forbid).
    with pytest.raises(ValidationError):
        _proposal(kind="CONVERSE", response_code="ACKNOWLEDGEMENT", unexpected_field="x")
    with pytest.raises(ValidationError):
        _proposal(kind="PLAN", goal="CANDIDATE_SEARCH", steps=[{**plan_step, "extra": 1}])
    # Over-long plans fail the schema bound itself.
    with pytest.raises(ValidationError):
        _proposal(kind="PLAN", goal="CANDIDATE_SEARCH", steps=[plan_step] * 4)


def test_converse_schema_rejects_model_authored_candidate_fact() -> None:
    # The removed free-text channel is structural: schema-valid model
    # output cannot carry a candidate assertion at all.
    with pytest.raises(ValidationError):
        _proposal(kind="CONVERSE", message="The first candidate has 20 years of Python.")


def test_converse_schema_rejects_model_authored_hiring_recommendation() -> None:
    with pytest.raises(ValidationError):
        _proposal(kind="CONVERSE", message="The first candidate should be hired.")


def test_clarify_schema_rejects_model_authored_candidate_fact() -> None:
    with pytest.raises(ValidationError):
        _proposal(kind="CLARIFY", message="The first candidate has 20 years of Python.")


def test_quote_max_length_stays_within_the_ollama_grammar_safe_bound() -> None:
    """Regression guard for D-035: a Pydantic string field's max_length
    above ~2000 in a `format`-constrained-decoding JSON schema made a real
    local Ollama daemon fail with HTTP 500 — not a validation-time symptom,
    so no fake-provider test can catch a regression here. Pins every string
    bound of the agent-plan-v1 schema at or below the verified-safe one."""
    import json

    from meyar.agent.capabilities.contracts import CapabilityName, agent_plan_json_schema
    from meyar.agent.schemas import MAX_AGENT_SEARCH_QUERY_LENGTH

    assert MAX_AGENT_SEARCH_QUERY_LENGTH <= 2000
    schema = json.dumps(agent_plan_json_schema(list(CapabilityName)))
    import re

    assert all(int(n) <= 2000 for n in re.findall(r'"maxLength": (\d+)', schema))
    search_plan("x" * 500)
    with pytest.raises(ValidationError):
        search_plan("x" * 501)


async def test_search_candidates_tool_is_tenant_scoped_and_evidence_grounded(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    candidate, _profile_v = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    foreign_tenant_candidate_tenant = tenant  # placeholder for readability
    del foreign_tenant_candidate_tenant
    await db_session.commit()

    from meyar.search.planner_schemas import PlannerDraft
    from meyar.search.schemas import RequiredFilters

    llm = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"])),
        agent_plans=[
            search_plan(),
        ],
    )
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message="Python bilən namizədləri göstər",
    )
    assert result.tool_call_count == 1
    assert len(result.tool_results) == 1
    search = result.tool_results[0].search
    assert search is not None
    assert search.response.plan.executable is True
    assert search.response.search_response is not None
    assert search.response.search_response.result_count == 1
    assert search.response.search_response.results[0].candidate_id == candidate.id
    # No CandidateIdentity field anywhere in the model-facing tool payload.
    dumped = search.model_dump_json()
    assert "full_name" not in dumped and "email" not in dumped and "phone" not in dumped

    await db_session.refresh(conversation)

    await db_session.refresh(context)
    assert context.active_result_set_id is not None
    assert await active_result_set_candidate_ids(
        db_session, result_set_id=context.active_result_set_id
    ) == [str(candidate.id)]


async def test_cross_tenant_search_result_never_leaks(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    from meyar.services.tenant_repo import create_tenant

    foreign_tenant = await create_tenant(db_session, name="Foreign")
    await seed_candidate_with_profile(
        db_session, tenant_id=foreign_tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()

    from meyar.search.planner_schemas import PlannerDraft
    from meyar.search.schemas import RequiredFilters

    llm = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"])),
        agent_plans=[
            search_plan(),
        ],
    )
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message="Python bilən namizədləri göstər",
    )
    search = result.tool_results[0].search
    assert search is not None
    assert search.response.search_response.result_count == 0


async def test_multi_turn_ordinal_reference_resolves_server_side(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    first, _pv1 = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    second, _pv2 = await seed_candidate_with_profile(
        db_session,
        tenant_id=tenant.id,
        profile_content=_profile("Python", quote="Second candidate evidence"),
    )
    await db_session.commit()

    from meyar.search.planner_schemas import PlannerDraft
    from meyar.search.schemas import RequiredFilters

    llm = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"])),
        agent_plans=[
            search_plan(),
        ],
    )
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    search_result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message="Python bilən namizədləri göstər",
    )
    ordered_ids = sorted([first.id, second.id], key=str)
    await db_session.refresh(conversation)
    await db_session.refresh(context)
    assert context.active_result_set_id is not None
    assert await active_result_set_candidate_ids(
        db_session, result_set_id=context.active_result_set_id
    ) == [str(cid) for cid in ordered_ids]

    llm2 = FakeLLMProvider(
        agent_plan=profile_plan("birincinin")
    )
    profile_turn = await _run(
        db_session,
        llm2,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message="birincinin təcrübəsini izah et",
    )
    assert profile_turn.outcome == AgentTurnOutcome.ANSWERED or profile_turn.tool_results
    profile_result = profile_turn.tool_results[0].profile
    assert profile_result is not None
    assert profile_result.found is True
    assert profile_result.profile is not None
    assert profile_result.profile.employment_history[0].organization == "Synthetic Co"
    del search_result


async def test_evidence_tool_never_fabricates_and_is_grounded(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    candidate, _pv = await seed_candidate_with_profile(
        db_session,
        tenant_id=tenant.id,
        profile_content=_profile("Python", quote="5 years of Python at Synthetic Co"),
    )
    await db_session.commit()

    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    await seed_active_result_set(
        db_session,
        tenant_id=tenant.id,
        browser_session_id=context.browser_session_id,
        session_context=context,
        candidate_ids=[candidate.id],
    )

    llm = FakeLLMProvider(
        # Slice C: the reference must be grounded in the user's own words
        # (the old model invented candidate_ref=1 for a message naming none).
        agent_plan=evidence_plan("birincinin", "Python")
    )
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message="birincinin Python bacarığını sübut et",
    )
    evidence = result.tool_results[0].evidence
    assert evidence is not None
    assert evidence.found is True
    assert len(evidence.matches) == 1
    assert evidence.matches[0].evidence[0].quote == "5 years of Python at Synthetic Co"


async def test_unknown_candidate_ref_is_never_silently_accepted(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    # No prior search this conversation — the grounded ordinal (3) cannot
    # resolve: Layer 1 RESULT_CONTEXT_REQUIRED keeps today's outward
    # CANDIDATE_REF_NOT_FOUND card while ZERO executors run.
    llm = FakeLLMProvider(
        agent_plan=profile_plan("üçüncünü")
    )
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation,
            session_context=context, message="üçüncünü aç"
    )
    assert result.outcome == AgentTurnOutcome.CANDIDATE_REF_NOT_FOUND
    assert result.tool_results[0].profile.found is False


async def test_candidate_ref_from_another_tenants_conversation_cannot_resolve(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    from meyar.services.tenant_repo import create_tenant

    foreign_tenant = await create_tenant(db_session, name="Foreign")
    foreign_candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=foreign_tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()

    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    # Even if this tenant's own conversation somehow pointed its
    # active_result_set_id at a real AgentResultSet row that actually
    # belongs to another tenant (it never would via normal search, since
    # create_result_set_from_search is already tenant-scoped), resolution
    # must still fail closed — step 2 of resolve_active_candidate_ref
    # filters the AgentResultSet lookup by BOTH id and tenant_id in one
    # query, so a foreign-tenant row is never even fetched to compare.
    from meyar.search.schemas import CandidateSearchRequest, SearchMode
    from meyar.services.agent_result_set_repo import SNAPSHOT_POLICY_VERSION
    from meyar.services.browser_session_repo import get_browser_session_by_id

    session = await get_browser_session_by_id(
        db_session, browser_session_id=context.browser_session_id
    )
    assert session is not None
    foreign_result_set = AgentResultSet(
        tenant_id=foreign_tenant.id,
        browser_session_id=context.browser_session_id,
        conversation_id=context.conversation_id,
        context_epoch=context.context_epoch,
        request_sha256="0" * 64,
        canonical_search_request=CandidateSearchRequest(
            mode=SearchMode.STRUCTURED_ONLY
        ).model_dump(mode="json"),
        planner_policy_version="test",
        planner_prompt_version="test",
        planner_schema_version="test",
        planner_model_provider="test",
        planner_model_name="test",
        planner_model_revision="",
        search_policy_version="test",
        search_mode=SearchMode.STRUCTURED_ONLY.value,
        result_count=1,
        corpus_fingerprint_sha256=None,
        snapshot_policy_version=SNAPSHOT_POLICY_VERSION,
        expires_at=session.expires_at,
    )
    db_session.add(foreign_result_set)
    await db_session.flush()
    db_session.add(
        AgentResultSetMember(
            result_set_id=foreign_result_set.id,
            ordinal=1,
            candidate_id=foreign_candidate.id,
            candidate_profile_version_id=_pv.id,
            candidate_embedding_version_id=None,
            relevance_score=1.0,
            structured_score=None,
            semantic_score=None,
        )
    )
    context.active_result_set_id = foreign_result_set.id
    await db_session.flush()

    llm = FakeLLMProvider(
        agent_plan=profile_plan("birincini")
    )
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation,
            session_context=context, message="birincini aç"
    )
    assert result.outcome == AgentTurnOutcome.CANDIDATE_REF_NOT_FOUND


async def test_two_browser_sessions_never_share_conversation_state(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()

    conversation_a, context_a = await _new_conversation(db_session, tenant, user, membership)
    await seed_active_result_set(
        db_session,
        tenant_id=tenant.id,
        browser_session_id=context_a.browser_session_id,
        session_context=context_a,
        candidate_ids=[candidate.id],
    )

    conversation_b, context_b = await _new_conversation(db_session, tenant, user, membership)
    assert conversation_b.id != conversation_a.id
    assert context_b.active_result_set_id is None

    llm = FakeLLMProvider(
        agent_plan=profile_plan("birincini")
    )
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation_b,
            session_context=context_b, message="birincini aç"
    )
    assert result.outcome == AgentTurnOutcome.CANDIDATE_REF_NOT_FOUND


async def test_plan_longer_than_the_effective_bound_executes_nothing(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """Issue #88 slice C (replaces the AgentDecision loop-bound test): there
    is no loop to bound any more — the effective step bound is
    min(MAX_PLAN_STEPS, agent_max_tool_calls), checked by Layer 1 BEFORE
    step 1. A 2-step plan with max_tool_calls=1 is PLAN_TOO_LONG: zero
    executors (the planner is never called) and exactly one proposal."""
    from agent_plans import plan, profile_step, search_step
    from sqlalchemy import select

    from meyar.models.audit_event import AuditEvent
    from meyar.search.planner_schemas import PlannerDraft
    from meyar.search.schemas import RequiredFilters

    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    message = "Kotlin bilən namizəd tap və birincinin profilini göstər"
    llm = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Kotlin"])),
        agent_plan=plan(search_step(), profile_step("birincinin")),
    )
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation,
        session_context=context, message=message, max_tool_calls=1,
    )
    assert result.outcome == AgentTurnOutcome.CLARIFICATION_REQUESTED
    assert result.tool_call_count == 0
    assert result.tool_results == []
    assert llm.agent_call_count == 1
    assert llm.planner_requests == []
    assert llm.agent_plan_contexts[0].max_plan_steps == 1
    events = list(
        await db_session.scalars(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenant.id,
                AuditEvent.event_type == "agent.plan.rejected",
            )
        )
    )
    assert [event.event_metadata["reason_code"] for event in events] == ["PLAN_TOO_LONG"]


async def test_model_search_is_whole_message_with_exactly_one_proposal(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """The model cannot author search text: a model-routed WHOLE_MESSAGE
    search hands the planner exactly the user's message, after exactly ONE
    plan proposal — there is no post-tool "what next" call."""
    from meyar.search.planner_schemas import PlannerDraft
    from meyar.search.schemas import RequiredFilters

    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    llm = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["NoMatch"])),
        agent_plans=[search_plan(), converse("ACKNOWLEDGEMENT")],
    )
    message = "NoMatch haqqında məlumat ver"
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message=message,
        max_tool_calls=3,
    )
    assert result.outcome == AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT
    assert result.tool_call_count == 1
    assert llm.planner_requests == [message]
    assert llm.agent_call_count == 1


async def test_duplicate_identical_steps_are_rejected_before_any_execution(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """Real-Ollama Slice 2 acceptance testing (PR #40) found a small model
    re-issuing an identical SEARCH. Slice C replaces the per-turn
    ``searched_queries`` guard with Layer 1 DUPLICATE_STEP over the
    resolved grounded arguments: nothing executes."""
    from agent_plans import plan, search_step

    from meyar.search.planner_schemas import PlannerDraft
    from meyar.search.schemas import RequiredFilters

    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    llm = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["NoMatch"])),
        agent_plan=plan(search_step(), search_step()),
    )
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message="NoMatch haqqında məlumat ver",
        max_tool_calls=3,
    )
    assert result.outcome == AgentTurnOutcome.CLARIFICATION_REQUESTED
    assert result.tool_results == []
    assert llm.planner_requests == []
    assert llm.agent_call_count == 1


@pytest.mark.parametrize(
    "error", [ModelTimeoutError("simulated timeout"), ModelUnavailableError("simulated outage")]
)
async def test_plan_provider_failure_abandons_the_turn(
    db_session: AsyncSession, tenant_and_user, error
) -> None:
    """Issue #88 slice C (#85): the one plan-proposal call failing on
    infrastructure abandons the turn — it raises before any transcript,
    plan execution, pointer or task change (the router audits
    agent.turn.abandoned and renders the not-run copy)."""
    from meyar.agent.service import AgentPlanProviderError

    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    turns_before = list(conversation.turns)
    llm = FakeLLMProvider(agent_error=error)
    with pytest.raises(AgentPlanProviderError):
        await _run(
            db_session, llm, tenant_id=tenant.id, conversation=conversation,
            session_context=context, message="salam",
        )
    assert llm.agent_call_count == 1  # infrastructure failure is not repaired
    assert conversation.turns == turns_before
    assert context.active_result_set_id is None


async def test_zero_tool_final_answer_uses_only_server_owned_copy(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    llm = FakeLLMProvider(
        agent_plan=converse("GREETING")
    )

    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation,
            session_context=context, message="salam"
    )

    assert result.tool_call_count == 0
    assert result.tool_results == []
    assert result.message == (
        "Salam! Namizəd axtarışı, profil sübutları və vakansiya meyarları ilə bağlı "
        "kömək edə bilərəm."
    )
    await db_session.refresh(conversation)
    await db_session.refresh(context)
    assert conversation.turns[-1]["text"] == result.message
    assert conversation.turns[-1]["text_authority"] == "SERVER_VALIDATED"


async def test_hiring_request_uses_server_owned_human_decision_copy(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    llm = FakeLLMProvider(
        agent_plan=clarify("HIRING_DECISION_REQUIRES_HUMAN")
    )

    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message="Who should be hired?",
    )

    assert result.tool_call_count == 0
    assert result.message == (
        "MEYAR sübutları və deterministik qiymətləndirməni təqdim edir; işə qəbul "
        "qərarını səlahiyyətli insan verir."
    )


async def test_repeated_schema_invalid_output_is_a_safe_typed_failure(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """Scenario H: still schema-invalid after the ONE repair ->
    MALFORMED_MODEL_OUTPUT (today's outcome), nothing executed."""
    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    llm = FakeLLMProvider(agent_fail_first_n_calls=99, agent_plan=converse("ACKNOWLEDGEMENT"))
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation,
            session_context=context, message="salam"
    )
    assert result.outcome == AgentTurnOutcome.MALFORMED_MODEL_OUTPUT
    assert result.tool_results == []
    assert llm.agent_repairs == [False, True]


async def test_one_repair_recovers_a_schema_invalid_first_proposal(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    llm = FakeLLMProvider(agent_fail_first_n_calls=1, agent_plan=converse("GREETING"))
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation,
        session_context=context, message="salam",
    )
    assert result.outcome == AgentTurnOutcome.ANSWERED
    assert llm.agent_repairs == [False, True]


async def test_skill_specific_duration_is_never_silently_weakened(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """Skill duration stays a typed, evidence-backed skill-duration filter;
    it never becomes skill + total career duration."""
    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()

    from meyar.search.planner_schemas import PlannerDraft
    from meyar.search.schemas import RequiredFilters

    draft = PlannerDraft(
        required_filters=RequiredFilters(skills=["Java"], min_total_experience_years=5.0)
    )
    llm = FakeLLMProvider(
        planner_draft=draft,
        agent_plans=[
            search_plan(),
        ],
    )
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message="5 il Java təcrübəsi olanları göstər",
    )
    search = result.tool_results[0].search
    assert search is not None
    assert search.response.plan.executable is True
    request = search.response.plan.search_request
    assert request is not None
    assert request.required_filters.skills == []
    assert request.required_filters.min_total_experience_years is None
    assert [(item.value, item.min_years) for item in request.required_filters.skill_experience] == [
        ("Java", 5.0)
    ]


async def test_prohibited_attribute_in_search_query_is_rejected(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()

    from meyar.search.planner_schemas import PlannerDraft
    from meyar.search.schemas import RequiredFilters

    draft = PlannerDraft(required_filters=RequiredFilters(skills=["qadın"]))
    llm = FakeLLMProvider(
        planner_draft=draft,
        agent_plans=[
            search_plan(),
        ],
    )
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message="qadın namizədləri göstər",
    )
    search = result.tool_results[0].search
    assert search is not None
    assert search.response.plan.executable is False
    from meyar.search.planner_schemas import PlannerOutcome

    assert search.response.plan.outcome == PlannerOutcome.PROHIBITED_REQUEST


async def test_agent_turn_never_writes_a_candidate_or_evaluation_row(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """No mutation path exists — the agent module never imports a
    create/update/delete repository function for any tenant-owned
    business row (only its own conversation state)."""
    import inspect

    import meyar.agent.service as agent_service

    source = inspect.getsource(agent_service)
    for forbidden in (
        "create_candidate",
        "create_job",
        "create_profile_version",
        "create_evaluation",
        "delete_candidate",
        "archive_job",
    ):
        assert forbidden not in source


# --- D-036 regression tests. Issue #88 slice C retired the post-tool "what
# next" decision whose failure D-036 guarded against: a search turn now makes
# exactly ONE proposal, so a grounded result can never be followed by a
# failing framing call. ---


async def test_successful_model_search_has_no_followup_model_call(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()

    from meyar.search.planner_schemas import PlannerDraft
    from meyar.search.schemas import RequiredFilters

    llm = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"])),
        agent_plan=search_plan(),
    )
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message="Python haqqında məlumat ver",
    )
    assert result.outcome == AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT
    assert result.message is None
    assert llm.agent_call_count == 1
    assert len(result.tool_results) == 1
    search = result.tool_results[0].search
    assert search is not None
    assert search.response.plan.executable is True
    assert search.response.search_response.result_count == 1
    assert search.response.search_response.results[0].candidate_id == candidate.id


async def test_no_result_model_failure_stays_a_safe_failure_with_no_tool_results(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """A proposal that never becomes a valid plan is never dressed up as a
    success: schema-invalid -> MALFORMED_MODEL_OUTPUT with no tool results;
    provider outage -> the turn is abandoned (#85)."""
    from meyar.agent.service import AgentPlanProviderError

    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)

    llm_malformed = FakeLLMProvider(agent_fail_first_n_calls=99, agent_plan=converse())
    result = await _run(
        db_session, llm_malformed, tenant_id=tenant.id, conversation=conversation,
            session_context=context, message="salam"
    )
    assert result.outcome == AgentTurnOutcome.MALFORMED_MODEL_OUTPUT
    assert result.tool_results == []

    conversation2, context2 = await _new_conversation(db_session, tenant, user, membership)
    llm_outage = FakeLLMProvider(agent_error=ModelUnavailableError("simulated outage"))
    with pytest.raises(AgentPlanProviderError):
        await _run(
            db_session, llm_outage, tenant_id=tenant.id, conversation=conversation2,
            session_context=context2, message="salam"
        )


async def test_stored_assistant_turn_is_never_blank_across_all_outcomes(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """Every outcome that can leave ``message`` as None must still resolve
    to non-empty display text when replayed through
    meyar.ui.presentation.agent_turn_outcome_message — mirrors what
    meyar.ui.router._agent_turn_log_views does for history rendering."""
    from meyar.ui.presentation import agent_turn_outcome_message

    for outcome in AgentTurnOutcome:
        text = agent_turn_outcome_message(outcome.value, None)
        assert text, f"AgentTurnOutcome.{outcome.name} has no non-empty fallback text"


async def test_get_candidate_profile_success_has_no_empty_turn_outcome(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """A successful GET_CANDIDATE_PROFILE (no model framing at all) must
    never be tagged plain ANSWERED with message=None — it must resolve to
    ANSWERED_FROM_TOOL_RESULT, which has a real fallback string."""
    tenant, user, _password, membership = tenant_and_user
    candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    await seed_active_result_set(
        db_session,
        tenant_id=tenant.id,
        browser_session_id=context.browser_session_id,
        session_context=context,
        candidate_ids=[candidate.id],
    )

    llm = FakeLLMProvider(
        agent_plan=profile_plan("birincini")
    )
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation,
            session_context=context, message="birincini aç"
    )
    assert result.outcome == AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT
    assert result.message is None


async def test_ordinal_reference_resolves_against_the_previous_turns_search(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """Rule E: the search turn's active_result_set_id is the authority a
    later 'birincini aç' resolves against (server-parsed ordinal)."""
    tenant, user, _password, membership = tenant_and_user
    candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()

    from meyar.search.planner_schemas import PlannerDraft
    from meyar.search.schemas import RequiredFilters

    llm_search = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"])),
        agent_plan=search_plan(),
    )
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    first_result = await _run(
        db_session,
        llm_search,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message="Python bilən namizədləri göstər",
    )
    assert first_result.outcome == AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT
    await db_session.refresh(conversation)
    await db_session.refresh(context)
    assert context.active_result_set_id is not None
    assert await active_result_set_candidate_ids(
        db_session, result_set_id=context.active_result_set_id
    ) == [str(candidate.id)]

    llm_profile = FakeLLMProvider(
        agent_plan=profile_plan("birincini")
    )
    second_result = await _run(
        db_session,
        llm_profile,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message="birincini aç",
    )
    assert second_result.outcome == AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT
    assert second_result.tool_results[0].profile.found is True
    assert second_result.tool_results[0].profile.candidate_id == candidate.id


# --- D-038 regression tests: the server, not the model, is authoritative
# over every span of factual text in a grounded answer. The model only
# selects/orders GroundedFact ids (and an optional closed-enum caveat) —
# it has no field through which it could author "he managed a team" or
# any other unsupported predicate. See docs/DECISIONS.md D-038 for the
# owner's factuality-hardening report this closes. ---


def test_grounded_selection_schema_has_no_free_text_answer_field() -> None:
    """Structural proof the vulnerability class is closed: there is no
    field on GroundedSelection a model could use to author prose at all
    (extra='forbid' rejects any attempt to smuggle one in), unlike the
    superseded D-037 GroundedAnswer.answer field."""
    from meyar.agent.schemas import GroundedSelection

    with pytest.raises(ValidationError):
        GroundedSelection.model_validate({"used_facts": [0], "answer": "He managed a team there."})
    # The only two fields that exist at all:
    assert set(GroundedSelection.model_fields) == {"used_facts", "caveat"}


def test_build_profile_facts_never_includes_identity_fields() -> None:
    """Direct unit test on the fact-flattening helper: no PII field can
    ever exist on GroundedFact by construction, and _build_profile_facts
    reads only CandidateProfileExtraction — this asserts it never even
    touches an identity source."""
    from meyar.agent.service import _build_profile_facts
    from meyar.schemas.candidate_profile import CandidateProfileExtraction

    profile = CandidateProfileExtraction.model_validate(
        _profile("Python", "SQL", quote="Backend Developer - Python - 2021-2025")
    )
    facts = _build_profile_facts(profile)
    assert facts
    dumped = " ".join(f"{f.title} {f.detail or ''}" for f in facts)
    for pii in ("@", "full_name", "email", "phone"):
        assert pii not in dumped


def test_render_grounded_answer_rejects_unknown_fact_refs() -> None:
    from meyar.agent.schemas import GroundedFact, GroundedSelection
    from meyar.agent.service import render_grounded_answer

    facts = [GroundedFact(id=0, category="skills", title="Python", detail=None)]

    # Fact id never supplied to the model.
    assert render_grounded_answer(GroundedSelection(used_facts=[7]), facts) is None
    # Nothing selected and no caveat set — nothing to say.
    assert render_grounded_answer(GroundedSelection(used_facts=[]), facts) is None


def test_render_grounded_answer_builds_deterministic_sentence_from_selected_facts() -> None:
    """Supported role/company/date facts and supported skills each render
    correctly — the output is EXACTLY the fixed template applied to the
    verbatim fact values, in the model's chosen order (employment first,
    then the skill), never a paraphrase or an added claim."""
    from meyar.agent.schemas import GroundedFact, GroundedSelection
    from meyar.agent.service import _render_fact_clause, render_grounded_answer

    employment = GroundedFact(
        id=1,
        category="employment_history",
        title="Data Analyst — Caspian Analytics",
        detail="2021 — 2025",
    )
    skill = GroundedFact(id=0, category="skills", title="Python", detail="Backend")
    facts = [skill, employment]

    rendered = render_grounded_answer(GroundedSelection(used_facts=[1, 0]), facts)
    expected = f"Məlum faktlar: {_render_fact_clause(employment)}; {_render_fact_clause(skill)}."
    assert rendered == expected
    assert "Data Analyst — Caspian Analytics" in rendered
    assert "2021 — 2025" in rendered
    assert "Python" in rendered


def test_render_grounded_answer_never_contains_unsupported_claim() -> None:
    """The exact owner-reported vulnerability: given only an employment
    fact, the rendered sentence can never say anything like 'managed a
    team' — there is no channel for that text to appear, since the model
    only ever supplies fact ids, never sentence content."""
    from meyar.agent.schemas import GroundedFact, GroundedSelection
    from meyar.agent.service import render_grounded_answer

    facts = [
        GroundedFact(
            id=0,
            category="employment_history",
            title="Data Analyst — Caspian Analytics",
            detail="2021 — 2025",
        )
    ]
    rendered = render_grounded_answer(GroundedSelection(used_facts=[0]), facts)
    assert rendered is not None
    for forbidden in ("managed", "team", "rəhbərlik", "komanda"):
        assert forbidden not in rendered.casefold()


def test_render_grounded_answer_duration_caveat_never_states_a_number() -> None:
    """Unsupported duration stays absent: the caveat sentence is a fixed
    string naming no number at all, regardless of what the question
    asked or which facts were selected."""
    from meyar.agent.schemas import GroundedCaveat, GroundedFact, GroundedSelection
    from meyar.agent.service import render_grounded_answer

    facts = [GroundedFact(id=0, category="skills", title="Python", detail=None)]
    rendered = render_grounded_answer(
        GroundedSelection(used_facts=[0], caveat=GroundedCaveat.DURATION_NOT_PROVEN), facts
    )
    assert rendered is not None
    assert "Mövcud sübut bu mövzu üzrə konkret təcrübə müddətini əsaslandırmır." in rendered
    assert not any(char.isdigit() for char in rendered.split("Mövcud sübut")[-1])

    # A caveat alone (nothing relevant selected) is also renderable.
    caveat_only = render_grounded_answer(
        GroundedSelection(used_facts=[], caveat=GroundedCaveat.DURATION_NOT_PROVEN), facts
    )
    assert caveat_only == "Mövcud sübut bu mövzu üzrə konkret təcrübə müddətini əsaslandırmır."


async def test_grounded_experience_explanation_uses_only_supplied_facts(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """End-to-end: a validated GroundedSelection becomes a server-rendered
    message for a successful GET_CANDIDATE_PROFILE lookup."""
    from meyar.agent.schemas import GroundedSelection

    tenant, user, _password, membership = tenant_and_user
    candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python", "SQL")
    )
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    await seed_active_result_set(
        db_session,
        tenant_id=tenant.id,
        browser_session_id=context.browser_session_id,
        session_context=context,
        candidate_ids=[candidate.id],
    )

    llm = FakeLLMProvider(
        agent_plan=profile_plan("birincinin"),
        grounded_selection=GroundedSelection(used_facts=[2, 0]),  # employment fact, Python skill
    )
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message="birincinin təcrübəsini izah et",
    )
    assert result.outcome == AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT
    assert result.message is not None
    assert "Backend Developer" in result.message
    assert "Python" in result.message
    # The question the model was asked to answer is the user's own turn text.
    assert llm.last_grounded_question == "birincinin təcrübəsini izah et"
    assert llm.last_grounded_facts is not None
    assert len(llm.last_grounded_facts) >= 2  # 2 skills + 1 employment fact from _profile()


async def test_grounded_selection_citing_unknown_fact_id_falls_back_safely(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """The model cannot introduce a fact absent from the supplied list —
    an out-of-range used_facts id is rejected, not silently trusted."""
    from meyar.agent.schemas import GroundedSelection

    tenant, user, _password, membership = tenant_and_user
    candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    await seed_active_result_set(
        db_session,
        tenant_id=tenant.id,
        browser_session_id=context.browser_session_id,
        session_context=context,
        candidate_ids=[candidate.id],
    )

    llm = FakeLLMProvider(
        agent_plan=profile_plan("birincinin"),
        grounded_selection=GroundedSelection(used_facts=[999]),
    )
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message="birincinin təcrübəsini izah et",
    )
    assert result.outcome == AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT
    assert result.message is None


async def test_grounded_synthesis_failure_falls_back_to_deterministic_message(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """FAILURE requirement: if grounded final-answer generation fails
    after a successful tool call, use the deterministic fallback, keep
    the tool results, and never produce a contradictory error state."""
    tenant, user, _password, membership = tenant_and_user
    candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    await seed_active_result_set(
        db_session,
        tenant_id=tenant.id,
        browser_session_id=context.browser_session_id,
        session_context=context,
        candidate_ids=[candidate.id],
    )

    llm = FakeLLMProvider(
        agent_plan=profile_plan("birincinin"),
        grounded_error=ModelTimeoutError("simulated grounded-synthesis timeout"),
    )
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message="birincinin təcrübəsini izah et",
    )
    assert result.outcome == AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT
    assert result.message is None
    assert result.tool_results[0].profile.found is True
    assert result.tool_results[0].profile.candidate_id == candidate.id


async def test_grounded_synthesis_does_not_disturb_ordinal_resolution(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """Ordinal resolution (rule E from D-036) must remain correct
    regardless of whether grounded synthesis succeeds, fails, or is
    rejected — active_result_set_id is untouched by this feature."""
    from meyar.agent.schemas import GroundedSelection

    tenant, user, _password, membership = tenant_and_user
    first, _pv1 = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    second, _pv2 = await seed_candidate_with_profile(
        db_session,
        tenant_id=tenant.id,
        profile_content=_profile("Python", quote="Second candidate evidence"),
    )
    await db_session.commit()
    ordered_ids = sorted([first.id, second.id], key=str)
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    result_set = await seed_active_result_set(
        db_session,
        tenant_id=tenant.id,
        browser_session_id=context.browser_session_id,
        session_context=context,
        candidate_ids=ordered_ids,
    )

    llm = FakeLLMProvider(
        agent_plan=profile_plan("ikincini"),
        grounded_selection=GroundedSelection(used_facts=[0]),
    )
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation,
            session_context=context, message="ikincini aç"
    )
    assert result.tool_results[0].profile.candidate_id == ordered_ids[1]
    await db_session.refresh(conversation)
    await db_session.refresh(context)
    assert context.active_result_set_id == result_set.id
    assert await active_result_set_candidate_ids(
        db_session, result_set_id=context.active_result_set_id
    ) == [str(cid) for cid in ordered_ids]


# --- Candidate-factuality P0: no model-authored response prose channel. ---


async def test_prompt_leaking_clarify_message_is_rejected_and_retried(
    db_session: AsyncSession, tenant_and_user
) -> None:
    from meyar.agent.prompts import AGENT_SYSTEM_PROMPT

    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    leaked_sentence = AGENT_SYSTEM_PROMPT.splitlines()[3].strip()
    assert len(leaked_sentence) >= 40
    with pytest.raises(ValidationError):
        _proposal(kind="CLARIFY", message=leaked_sentence)
    llm = FakeLLMProvider(
        agent_plan=clarify("CANDIDATE_REFERENCE_REQUIRED")
    )
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation,
            session_context=context, message="salam"
    )
    assert result.outcome == AgentTurnOutcome.CLARIFICATION_REQUESTED
    assert result.message == (
        "Əvvəlcə namizədləri axtarın, sonra nəticə sırasındakı namizədi göstərin."
    )
    assert leaked_sentence not in (result.message or "")
    assert llm.agent_call_count == 1


async def test_prompt_leak_persisting_through_every_retry_falls_back_safely(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    leaked_sentence = "The first candidate has 20 years of Python experience and should be hired."
    conversation.turns = [{"role": "assistant", "text": leaked_sentence, "outcome": "ANSWERED"}]

    from meyar.ui.router import _agent_turn_log_views

    rendered = _agent_turn_log_views(conversation)
    assert rendered[0].text == "Sorğu tamamlandı."
    assert leaked_sentence not in rendered[0].text


# --- Slice 4 (issue #33, D-030/D-032): DRAFT_JOB_CRITERIA ---


async def test_draft_job_criteria_builds_valid_criteria_from_llm_draft(
    db_session: AsyncSession, tenant_and_user
) -> None:
    from meyar.agent.schemas import JDCriteriaDraft, JDDraftCriterionItem
    from meyar.schemas.criteria import CriterionKind, CriterionType

    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    jd_text = "Vakansiya: Baş Backend Mühəndisi.\nPython bilməlidir.\nAWS üstünlükdür."
    llm = FakeLLMProvider(
        agent_plan=vacancy_proposal(),
        jd_draft=JDCriteriaDraft(
            title="Baş Backend Mühəndisi",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind=CriterionKind.SKILL,
                    requirement="Python",
                    source_text="Python bilməlidir",
                )
            ],
            preferred=[
                JDDraftCriterionItem(
                    span_id="req-0002",
                    kind=CriterionKind.SKILL,
                    requirement="AWS",
                    source_text="AWS üstünlükdür",
                )
            ],
        ),
    )
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation,
            session_context=context, message=jd_text
    )
    assert result.outcome == AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT
    assert len(result.tool_results) == 1
    draft = result.tool_results[0].job_draft
    assert draft is not None
    assert draft.title == "Baş Backend Mühəndisi"
    assert [c.label for c in draft.must_have] == ["Python"]
    assert draft.must_have[0].type == CriterionType.MUST_HAVE
    assert [c.label for c in draft.preferred] == ["AWS"]
    assert draft.preferred[0].type == CriterionType.PREFERRED
    assert draft.unsupported == []
    assert draft.prohibited_count == 0
    # Nothing is persisted — this is a draft only (D-032).
    from sqlalchemy import select

    from meyar.models.job import Job

    jobs = (await db_session.execute(select(Job).where(Job.tenant_id == tenant.id))).scalars().all()
    assert jobs == []
    # Turn-terminal like GET_CANDIDATE_PROFILE/EVIDENCE — no second decision call.
    assert llm.agent_call_count == 0


async def test_pending_draft_followup_is_modified_before_search_routing(
    db_session: AsyncSession, tenant_and_user
) -> None:
    from meyar.agent.schemas import JDCriteriaDraft

    tenant, user, _password, membership = tenant_and_user
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    initial = await _run(
        db_session,
        FakeLLMProvider(jd_draft=JDCriteriaDraft(title="Backend")),
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message=(
            "Bu vakansiya elanını analiz et: Python minimum 5 il tələb olunur. "
            "10 nəfər göstər."
        ),
    )
    assert initial.tool_results[0].job_draft is not None
    await db_session.refresh(conversation)
    await db_session.refresh(context)

    routing_probe = FakeLLMProvider(
        agent_plan=search_plan()
    )
    modified = await _run(
        db_session,
        routing_probe,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message="10 yox, 5 nəfər göstər.",
    )
    assert routing_probe.agent_call_count == 0
    assert modified.outcome == AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT
    assert modified.tool_results[0].tool_name == AgentActionType.DRAFT_JOB_CRITERIA
    draft = modified.tool_results[0].job_draft
    assert draft is not None and draft.result_limit == 5


async def test_pending_draft_modality_followups_apply_and_persist_both_directions(
    db_session: AsyncSession, tenant_and_user
) -> None:
    from meyar.agent.schemas import AgentJobDraftToolResult, JDCriteriaDraft

    tenant, user, _password, membership = tenant_and_user
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    initial_result = await _run(
        db_session,
        FakeLLMProvider(jd_draft=JDCriteriaDraft(title="Platform")),
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message="Vakansiya: Platform.\nClickHouse tələb olunur.\nCOBIT üstünlükdür.",
    )
    initial = initial_result.tool_results[0].job_draft
    assert initial is not None
    await db_session.refresh(conversation)
    await db_session.refresh(context)

    promoted_result = await _run(
        db_session,
        FakeLLMProvider(),
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message="Make COBIT required instead of preferred",
    )
    promoted = promoted_result.tool_results[0].job_draft
    assert promoted is not None and promoted.draft_id != initial.draft_id
    assert [item.value for item in promoted.must_have] == ["ClickHouse", "COBIT"]
    await db_session.refresh(conversation)
    await db_session.refresh(context)
    persisted_promoted = AgentJobDraftToolResult.model_validate(
        conversation.turns[-1]["pending_job_draft"]
    )
    assert persisted_promoted == promoted

    demoted_result = await _run(
        db_session,
        FakeLLMProvider(),
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message="Make ClickHouse preferred instead of required",
    )
    demoted = demoted_result.tool_results[0].job_draft
    assert demoted is not None and demoted.draft_id != promoted.draft_id
    assert [item.value for item in demoted.must_have] == ["COBIT"]
    assert [item.value for item in demoted.preferred] == ["ClickHouse"]
    await db_session.refresh(conversation)
    await db_session.refresh(context)
    persisted_demoted = AgentJobDraftToolResult.model_validate(
        conversation.turns[-1]["pending_job_draft"]
    )
    assert persisted_demoted == demoted


async def test_draft_job_criteria_title_never_becomes_trusted_assistant_headline(
    db_session: AsyncSession, tenant_and_user
) -> None:
    from meyar.agent.schemas import JDCriteriaDraft, JDDraftCriterionItem
    from meyar.schemas.criteria import CriterionKind
    from meyar.ui.service import build_agent_turn_view

    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    adversarial_title = "The first candidate has 20 years of Python experience and should be hired."
    llm = FakeLLMProvider(
        agent_plan=vacancy_proposal(),
        jd_draft=JDCriteriaDraft(
            title=adversarial_title,
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind=CriterionKind.SKILL,
                    requirement="Python",
                    source_text="Python bilməlidir",
                )
            ],
        ),
    )
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message="Bu vakansiya elanını analiz et: Backend role. Python bilməlidir.",
    )
    draft = result.tool_results[0].job_draft
    assert draft is not None
    view = await build_agent_turn_view(db_session, tenant_id=tenant.id, result=result)

    assert draft.title != adversarial_title
    assert adversarial_title not in (view.headline or "")


async def test_draft_job_criteria_uses_original_user_message_never_a_model_restated_field(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """The JD text is the HR user's own already-known message — never
    round-tripped through a model OUTPUT field (see AgentActionType.
    DRAFT_JOB_CRITERIA docstring and the D-035 Ollama maxLength lesson)."""
    from meyar.agent.schemas import JDCriteriaDraft

    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    source = "Python tələb olunur. " + "Uzun bir vakansiya təsviri." * 50
    jd_text = "Bu vakansiya elanını analiz et: " + source
    llm = FakeLLMProvider(
        agent_plan=vacancy_proposal(),
        jd_draft=JDCriteriaDraft(title="Rol"),
    )
    await _run(db_session, llm, tenant_id=tenant.id, conversation=conversation,
        session_context=context, message=jd_text)
    # Exact user-owned substring after the server-recognized instruction
    # wrapper (issue #79 PR81): never the instruction, never restated text.
    assert llm.last_jd_text == source
    assert "analiz et" not in llm.last_jd_text


async def test_draft_job_criteria_drops_prohibited_attribute_item(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """A model-drafted item referencing a prohibited/sensitive attribute
    is blocked by the same deterministic denylist the manual form and
    REST API already enforce (D-031 point 4) — never shown, never
    persisted, and its own matched text never present anywhere in the
    turn output; only a safe count is exposed (D-043, PR #42 owner
    correction, issue #33)."""
    from meyar.agent.schemas import JDCriteriaDraft, JDDraftCriterionItem
    from meyar.schemas.criteria import CriterionKind

    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    llm = FakeLLMProvider(
        agent_plan=vacancy_proposal(),
        jd_draft=JDCriteriaDraft(
            title="Rol",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0002",
                    kind=CriterionKind.SKILL,
                    requirement="Python",
                    source_text="Python bilməlidir",
                ),
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind=CriterionKind.SKILL,
                    requirement="kişi",
                    source_text="kişi olmalıdır",
                ),
            ],
        ),
    )
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message=(
            "Bu vakansiya elanını analiz et: Rol üçün namizəd kişi olmalıdır. "
            "Python bilməlidir."
        ),
    )
    draft = result.tool_results[0].job_draft
    assert draft is not None
    assert [c.label for c in draft.must_have] == ["Python"]
    assert draft.prohibited_count == 1
    assert draft.unsupported == []
    assert draft.ungrounded_count == 0
    rendered = result.model_dump_json()
    assert "kişi" not in rendered


async def test_nationality_misclassified_as_language_never_reaches_db_backed_draft(
    db_session: AsyncSession, tenant_and_user
) -> None:
    from meyar.agent.schemas import JDCriteriaDraft, JDDraftCriterionItem
    from meyar.schemas.criteria import CriterionKind

    tenant, user, _password, membership = tenant_and_user
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    source = (
        "Bu vakansiya elanını analiz et: Namizəd Azərbaycan vətəndaşı olmalıdır."
    )
    llm = FakeLLMProvider(
        agent_plan=vacancy_proposal(),
        jd_draft=JDCriteriaDraft(
            title="Rol",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind=CriterionKind.LANGUAGE,
                    requirement="Azerbaijani",
                    source_text=source,
                )
            ],
        ),
    )
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message=source,
    )
    draft = result.tool_results[0].job_draft
    assert draft is not None
    assert draft.must_have == []
    assert draft.preferred == []
    assert draft.prohibited_count == 1
    assert source not in result.model_dump_json()


async def test_draft_job_criteria_supports_named_experience_without_inventing_duration(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """Named experience without a threshold preserves its family for review;
    it is never weakened to bare skill presence or given an invented duration."""
    from meyar.agent.schemas import JDCriteriaDraft, JDDraftCriterionItem
    from meyar.schemas.criteria import CriterionKind

    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    llm = FakeLLMProvider(
        agent_plan=vacancy_proposal(),
        jd_draft=JDCriteriaDraft(
            title="Rol",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind=CriterionKind.SKILL,
                    requirement="Python",
                    source_text="Python bilməlidir",
                ),
                # No min_years is present in either model or source.
                JDDraftCriterionItem(
                    span_id="req-0002",
                    kind="SKILL_EXPERIENCE",
                    requirement="Backend",
                    source_text="Backend təcrübəsi tələb olunur",
                ),
            ],
        ),
    )
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message=(
            "Bu vakansiya elanını analiz et:\nPython bilməlidir. "
            "Backend təcrübəsi tələb olunur."
        ),
    )
    draft = result.tool_results[0].job_draft
    assert draft is not None
    assert [c.label for c in draft.must_have] == ["Python"]
    assert draft.prohibited_count == 0
    assert draft.ungrounded_count == 0
    assert draft.unsupported == []
    assert len(draft.needs_review) == 1
    # Issue #84: an experience claim without a duration has no
    # server-validated criterion shape, so no canonical kind/subject is
    # exposed; as an explicit MUST_HAVE it blocks confirmation instead.
    assert draft.needs_review[0].kind is None
    assert draft.needs_review[0].subject is None
    assert draft.needs_review[0].blocking is True
    # Disclosed, never persisted.
    from sqlalchemy import select

    from meyar.models.job import Job

    jobs = (await db_session.execute(select(Job).where(Job.tenant_id == tenant.id))).scalars().all()
    assert jobs == []


async def test_draft_job_criteria_other_kind_is_unsupported_never_scored(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """D-045 (PR #42 owner correction, issue #33, item 6): a requirement
    the model explicitly flags as JDDraftCriterionKind.OTHER — real,
    non-sensitive, but outside the deterministic evaluator's five scoring
    dimensions — must be routed to UNSUPPORTED deterministically, never
    coerced into a supported CriterionKind merely to score it, and must
    never appear as a real criterion."""
    from meyar.agent.schemas import JDCriteriaDraft, JDDraftCriterionItem, JDDraftCriterionKind
    from meyar.schemas.criteria import CriterionKind, CriterionType

    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    llm = FakeLLMProvider(
        agent_plan=vacancy_proposal(),
        jd_draft=JDCriteriaDraft(
            title="Rol",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind=CriterionKind.SKILL,
                    requirement="Python",
                    source_text="Python bilməlidir",
                )
            ],
            preferred=[
                JDDraftCriterionItem(
                    span_id="req-0002",
                    kind=JDDraftCriterionKind.OTHER,
                    requirement="Ezamiyyətə hazır olmaq",
                    source_text="Namizəd ezamiyyətə hazır olması üstünlükdür",
                )
            ],
        ),
    )
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message=(
            "Bu vakansiya elanını analiz et:\nPython bilməlidir. "
            "Namizəd ezamiyyətə hazır olması üstünlükdür."
        ),
    )
    draft = result.tool_results[0].job_draft
    assert draft is not None
    assert [c.label for c in draft.must_have] == ["Python"]
    assert draft.preferred == []
    assert draft.prohibited_count == 0
    assert draft.ungrounded_count == 0
    assert len(draft.unsupported) == 1
    assert draft.unsupported[0].requirement == "Namizəd ezamiyyətə hazır olması üstünlükdür"
    assert draft.unsupported[0].criterion_type == CriterionType.PREFERRED


# --- D-046 (PR #42 owner correction, issue #33): JD requirement grounding ---
#
# Real-Ollama acceptance testing (qwen3:1.7b) reproduced a genuine content-
# fabrication defect distinct from D-042/D-045's routing/classification
# findings: given a short, travel-readiness-only JD ("Namizəd ezamiyyətə
# getməyə hazır olmalıdır"), the model can surface an entirely unrelated,
# unstated requirement (reported as "Passing an exam") as if it were a
# genuine JD requirement the system merely cannot score. These tests use
# FakeLLMProvider (deterministic, no real model call) to pin the fix's
# behavior; the real-Ollama reproduction itself is recorded in
# docs/DECISIONS.md D-046, not re-run here. D-055 replaces that lexical
# grounding unit with server-owned occurrence spans.


def test_requirement_authority_is_an_exact_server_owned_occurrence() -> None:
    from meyar.agent.semantic_requirements import analyze_hr_text

    jd_text = "Vakansiya: Regional Satış Nümayəndəsi. Namizəd ezamiyyətə getməyə hazır olmalıdır."
    spans = analyze_hr_text(jd_text).spans
    assert len(spans) == 1
    span = spans[0]
    assert jd_text[span.start_offset : span.end_offset] == span.text
    assert span.text == "Namizəd ezamiyyətə getməyə hazır olmalıdır"
    assert "Passing an exam" not in span.text


async def test_draft_job_criteria_drops_fabricated_unrelated_requirement(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """Reproduces the reported real-Ollama defect end-to-end through
    FakeLLMProvider: a travel-readiness-only JD, and a model draft that
    (as qwen3:1.7b actually did) attaches a completely unrelated,
    unstated requirement — here reproducing the reported "Passing an
    exam" text verbatim as an OTHER item — alongside the one genuine,
    grounded requirement. The fabricated item must never reach
    ``unsupported`` (which HR reads as 'a real JD requirement the system
    cannot score') and its own text must never appear anywhere in the
    turn output — only a safe count (D-046)."""
    from meyar.agent.schemas import JDCriteriaDraft, JDDraftCriterionItem, JDDraftCriterionKind
    from meyar.schemas.criteria import CriterionKind, CriterionType

    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    jd_text = (
        "Bu vakansiya elanını analiz et: Kredit Analitiki. "
        "Namizəd ezamiyyətə getməyə hazır olmalıdır."
    )
    llm = FakeLLMProvider(
        agent_plan=vacancy_proposal(),
        jd_draft=JDCriteriaDraft(
            title="Kredit Analitiki",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind=JDDraftCriterionKind.OTHER,
                    requirement="Ezamiyyətə hazır olmaq",
                    source_text="Namizəd ezamiyyətə getməyə hazır olmalıdır",
                ),
                # The reported real-Ollama hallucination: unrelated to any
                # word in jd_text, but classified OTHER — the exact path
                # that previously reached HR-visible "unsupported" with no
                # grounding check at all.
                JDDraftCriterionItem(
                    span_id="req-9998",
                    kind=JDDraftCriterionKind.OTHER,
                    requirement="Passing an exam",
                    source_text="Passing an exam",
                ),
            ],
            preferred=[
                # Same fabrication class, but on a kind that WOULD have
                # built a real, scored CriterionIn — proving the grounding
                # gate also guards the success path, not only OTHER.
                JDDraftCriterionItem(
                    span_id="req-9999",
                    kind=CriterionKind.LANGUAGE,
                    requirement="İngilis dili",
                    source_text="İngilis dili tələb olunur",
                ),
            ],
        ),
    )
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation,
            session_context=context, message=jd_text
    )
    draft = result.tool_results[0].job_draft
    assert draft is not None
    # The one genuine, grounded requirement survives as UNSUPPORTED
    # (real OTHER-kind disclosure, unchanged D-045 contract).
    assert len(draft.unsupported) == 1
    assert draft.unsupported[0].requirement == "Namizəd ezamiyyətə getməyə hazır olmalıdır"
    assert draft.unsupported[0].criterion_type == CriterionType.MUST_HAVE
    # The two fabricated items never became a scored criterion and never
    # entered the HR-visible "unsupported" disclosure — only a safe count.
    assert draft.must_have == []
    assert draft.preferred == []
    assert draft.prohibited_count == 0
    assert draft.ungrounded_count == 2
    # No invented text anywhere in the turn output, exactly like the
    # existing PROHIBITED non-leak discipline (D-043).
    rendered = result.model_dump_json()
    assert "Passing an exam" not in rendered
    assert "İngilis dili" not in rendered


# --- Issue #79: server-owned deterministic entry routing ---


async def test_server_confirmed_draft_job_criteria_never_calls_the_routing_model(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """An explicit JD analysis request routes without an orchestrator call."""
    from meyar.agent.schemas import AgentActionType, JDCriteriaDraft, JDDraftCriterionItem
    from meyar.schemas.criteria import CriterionKind

    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    jd_text = "Bu vakansiya elanını analiz et: Backend. Python bilməlidir."
    llm = FakeLLMProvider(
        jd_draft=JDCriteriaDraft(
            title="Rol",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind=CriterionKind.SKILL,
                    requirement="Python",
                    source_text="Python bilməlidir",
                )
            ],
        ),
    )
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message=jd_text,
    )
    assert result.outcome == AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT
    assert result.tool_results[0].tool_name == AgentActionType.DRAFT_JOB_CRITERIA
    # No routing-decision call was made at all — deterministic, not model-inferred.
    assert llm.agent_call_count == 0
    assert llm.jd_draft_call_count == 1


async def test_server_confirmed_draft_job_criteria_cannot_become_a_search(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """A strongly identified pasted JD bypasses a wrong model decision."""
    from meyar.agent.schemas import AgentActionType, JDCriteriaDraft, JDDraftCriterionItem
    from meyar.schemas.criteria import CriterionKind

    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    jd_text = "Vakansiya: Backend Mühəndisi. Python bilməlidir."
    llm = FakeLLMProvider(
        agent_plan=search_plan(),
        jd_draft=JDCriteriaDraft(
            title="Backend Mühəndisi",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind=CriterionKind.SKILL,
                    requirement="Python",
                    source_text="Python bilməlidir",
                )
            ],
        ),
    )
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message=jd_text,
    )
    assert result.tool_results[0].tool_name == AgentActionType.DRAFT_JOB_CRITERIA
    assert llm.agent_call_count == 0
    assert llm.call_count == 0  # plan_candidate_search was never reached


async def test_model_proposed_draft_without_server_authority_fails_closed(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """A model DRAFT proposal is not authorization and cannot run a tool."""

    from sqlalchemy import func, select

    from meyar.models.evaluation import Evaluation
    from meyar.models.job import Job
    from meyar.models.job_criteria_version import JobCriteriaVersion

    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    llm = FakeLLMProvider(
        agent_plan=vacancy_proposal()
    )
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        # Model-routed (not a forced search), so the model proposal is
        # actually consulted and must fail closed.
        message="NoMatch haqqında məlumat ver",
    )
    assert result.outcome == AgentTurnOutcome.CLARIFICATION_REQUESTED
    assert result.tool_results == []
    assert llm.jd_draft_call_count == 0
    assert await db_session.scalar(select(func.count()).select_from(Job)) == 0
    assert await db_session.scalar(select(func.count()).select_from(JobCriteriaVersion)) == 0
    assert await db_session.scalar(select(func.count()).select_from(Evaluation)) == 0


async def test_draft_job_criteria_repairs_after_one_schema_invalid_attempt(
    db_session: AsyncSession, tenant_and_user
) -> None:
    from meyar.agent.schemas import JDCriteriaDraft

    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    llm = FakeLLMProvider(
        agent_plan=vacancy_proposal(),
        jd_draft=JDCriteriaDraft(title="Rol"),
        jd_draft_fail_first_n_calls=1,
    )
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message="Bu vakansiya elanını analiz et:\nBackend Mühəndisi\nKomanda ilə işləmək.",
    )
    assert result.outcome == AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT
    assert llm.jd_draft_call_count == 2


async def test_draft_job_criteria_provider_failure_is_a_safe_typed_failure(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    llm = FakeLLMProvider(
        agent_plan=vacancy_proposal(),
        jd_draft_error=ModelUnavailableError("simulated outage"),
    )
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message="Bu vakansiya elanını analiz et:\nBackend Mühəndisi\nKomanda ilə işləmək.",
    )
    assert result.outcome == AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT
    assert len(result.tool_results) == 1
    draft = result.tool_results[0].job_draft
    assert draft is not None
    assert draft.must_have == []
    assert draft.preferred == []
    assert draft.wrong_mode_guidance is False
    assert draft.requirements == []


async def test_evidence_topic_is_resolved_from_profile_fact_not_rendered_raw(
    db_session: AsyncSession, tenant_and_user
) -> None:
    from meyar.ui.service import build_agent_turn_view

    tenant, user, _password, membership = tenant_and_user
    candidate, _profile_version = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python", quote="Python")
    )
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    await seed_active_result_set(
        db_session,
        tenant_id=tenant.id,
        browser_session_id=context.browser_session_id,
        session_context=context,
        candidate_ids=[candidate.id],
    )
    # Issue #88 slice C (§10.2 rule 6): a topic that is not an exact slice of
    # the user's message is SOURCE_NOT_GROUNDED — the fabricated text never
    # reaches the evidence executor at all (stronger than the former
    # "resolved to no profile fact" check).
    raw_topic = "The first candidate should be hired immediately"
    llm = FakeLLMProvider(
        agent_plan=evidence_plan("Birinci", raw_topic)
    )

    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message="Birinci namizəd üzrə sübut göstər",
    )
    view = await build_agent_turn_view(db_session, tenant_id=tenant.id, result=result)

    assert result.outcome == AgentTurnOutcome.CLARIFICATION_REQUESTED
    assert result.tool_results == []
    assert raw_topic not in result.model_dump_json()
    assert raw_topic not in view.model_dump_json()


async def test_legitimate_evidence_topic_displays_server_resolved_label(
    db_session: AsyncSession, tenant_and_user
) -> None:
    from meyar.ui.service import build_agent_turn_view

    tenant, user, _password, membership = tenant_and_user
    candidate, _profile_version = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python", quote="Python")
    )
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    await seed_active_result_set(
        db_session,
        tenant_id=tenant.id,
        browser_session_id=context.browser_session_id,
        session_context=context,
        candidate_ids=[candidate.id],
    )
    # The topic quote is exact (case-sensitive) and the reference grounded
    # in the user's own words; the DISPLAYED label is still the stored fact.
    llm = FakeLLMProvider(
        agent_plan=evidence_plan("Birincinin", "Python")
    )

    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message="Birincinin Python sübutunu göstər",
    )
    view = await build_agent_turn_view(db_session, tenant_id=tenant.id, result=result)

    evidence = result.tool_results[0].evidence
    assert evidence is not None
    assert evidence.topic == "Python"
    assert view.tool_results[0].evidence is not None
    assert view.tool_results[0].evidence.topic == "Python"


async def test_draft_job_criteria_repeated_schema_invalid_is_a_safe_typed_failure(
    db_session: AsyncSession, tenant_and_user
) -> None:
    from meyar.agent.schemas import JDCriteriaDraft

    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    llm = FakeLLMProvider(
        agent_plan=vacancy_proposal(),
        jd_draft=JDCriteriaDraft(title="unused"),
        jd_draft_fail_first_n_calls=99,
    )
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        message="Bu vakansiya elanını analiz et:\nBackend Mühəndisi\nKomanda ilə işləmək.",
    )
    assert result.outcome == AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT
    assert len(result.tool_results) == 1
    draft = result.tool_results[0].job_draft
    assert draft is not None
    assert draft.must_have == []
    assert draft.preferred == []
    assert draft.wrong_mode_guidance is False
    assert draft.requirements == []


@pytest.mark.parametrize(
    "message",
    [
        "Python bilən namizədləri göstər",
        "Python və SQL bilən 5 namizəd göstər",
        "Show candidates with at least 5 years of Java",
    ],
)
async def test_explicit_search_is_server_routed_despite_wrong_orchestrator(
    db_session: AsyncSession, tenant_and_user, message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #79 PR81: a deliberately WRONG orchestration proposal is never
    consulted for an explicit new search; the existing planner runs with
    the user's own text and the validated search result ends the turn."""
    from sqlalchemy import func, select

    import meyar.agent.service as agent_service
    from meyar.models.evaluation import Evaluation
    from meyar.models.job import Job
    from meyar.models.job_criteria_version import JobCriteriaVersion
    from meyar.search.planner_schemas import PlannerDraft
    from meyar.search.schemas import RequiredFilters

    planner_requests: list[str] = []
    original_plan_and_search = agent_service.plan_and_search_candidates

    async def _spy(*args, **kwargs):
        planner_requests.append(kwargs["natural_language_request"])
        return await original_plan_and_search(*args, **kwargs)

    monkeypatch.setattr(agent_service, "plan_and_search_candidates", _spy)

    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation, context = await _new_conversation(db_session, tenant, user, membership)
    llm = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"])),
        agent_plan=refine_plan(whole_filter=True),
    )
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation,
            session_context=context, message=message
    )
    assert llm.agent_call_count == 0
    assert llm.jd_draft_call_count == 0
    assert result.outcome == AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT
    assert result.tool_call_count == 1
    assert [r.tool_name for r in result.tool_results] == [AgentActionType.SEARCH_CANDIDATES]
    search = result.tool_results[0].search
    assert search is not None
    # The existing planner ran exactly once, with the user's own text.
    assert planner_requests == [message]
    assert await db_session.scalar(select(func.count()).select_from(Job)) == 0
    assert await db_session.scalar(select(func.count()).select_from(JobCriteriaVersion)) == 0
    assert await db_session.scalar(select(func.count()).select_from(Evaluation)) == 0
