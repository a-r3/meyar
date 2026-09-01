"""Slice 2 — bounded read-only agent orchestration (issue #31, D-035).
Service-level tests: no HTTP, direct meyar.agent.service.run_agent_turn
calls against a real Postgres session with FakeLLMProvider doubles."""

from datetime import date

import pytest
from fakes import FakeLLMProvider
from pydantic import ValidationError
from search_helpers import seed_candidate_with_profile
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.agent.schemas import (
    MAX_CANDIDATE_REF,
    AgentActionType,
    AgentDecision,
    AgentTurnOutcome,
)
from meyar.agent.service import run_agent_turn
from meyar.llm.provider import ModelTimeoutError, ModelUnavailableError
from meyar.search.schemas import EmbeddingSearchConfig
from meyar.services.agent_conversation_repo import get_or_create_conversation
from meyar.services.browser_session_repo import create_browser_session

AS_OF_DATE = date(2026, 1, 1)
EMPTY_PROFILE = {
    "skills": [],
    "employment_history": [],
    "education": [],
    "certifications": [],
    "languages": [],
    "projects": [],
}


def _profile(*skills: str, quote: str = "Synthetic evidence") -> dict:
    return {
        **EMPTY_PROFILE,
        "skills": [
            {
                "name": skill,
                "category": "Backend",
                "evidence": [{"page": 1, "block_index": 0, "quote": quote}],
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
                "evidence": [{"page": 1, "block_index": 0, "quote": quote}],
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


async def _new_conversation(db_session: AsyncSession, tenant, user, membership):
    session, _raw_token = await create_browser_session(
        db_session, user_id=user.id, tenant_membership_id=membership.id, ttl_hours=8
    )
    await db_session.flush()
    conversation = await get_or_create_conversation(
        db_session, tenant_id=tenant.id, browser_session_id=session.id
    )
    return conversation


async def _run(
    db_session,
    llm,
    *,
    tenant_id,
    conversation,
    message,
    max_tool_calls=3,
    max_context_turns=8,
):
    return await run_agent_turn(
        db_session,
        llm,
        tenant_id=tenant_id,
        conversation=conversation,
        user_message=message,
        as_of_date=AS_OF_DATE,
        embedding_config=_embedding_config(),
        embedding_provider=None,
        max_tool_calls=max_tool_calls,
        max_context_turns=max_context_turns,
    )


def test_agent_decision_schema_rejects_mismatched_action_shape() -> None:
    with pytest.raises(ValidationError):
        AgentDecision(action=AgentActionType.SEARCH_CANDIDATES)  # missing search_query
    with pytest.raises(ValidationError):
        AgentDecision(action=AgentActionType.FINAL_ANSWER, candidate_ref=1, message="hi")
    with pytest.raises(ValidationError):
        AgentDecision(action=AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=0)
    with pytest.raises(ValidationError):
        AgentDecision(
            action=AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=1, evidence_topic="x"
        )
    with pytest.raises(ValidationError):
        AgentDecision(
            action=AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=MAX_CANDIDATE_REF + 1
        )
    # Extra unknown field must be rejected (extra=forbid), same discipline as PlannerDraft.
    with pytest.raises(ValidationError):
        AgentDecision.model_validate(
            {"action": "FINAL_ANSWER", "message": "hi", "unexpected_field": "x"}
        )


def test_search_query_max_length_stays_within_the_ollama_grammar_safe_bound() -> None:
    """Regression guard for D-035: empirically, a Pydantic string field's
    max_length above ~2000 in a `format`-constrained-decoding JSON schema
    made a real local Ollama daemon fail every decide_agent_action call
    with HTTP 500 ("failed to load model vocabulary required for
    format") — not a validation-time symptom, so no fake-provider test
    can catch a regression here. This pins the schema's declared bound
    itself so a future edit cannot silently widen it back past the
    verified-safe threshold."""
    from meyar.agent.schemas import MAX_AGENT_SEARCH_QUERY_LENGTH

    assert MAX_AGENT_SEARCH_QUERY_LENGTH <= 2000
    AgentDecision(action=AgentActionType.SEARCH_CANDIDATES, search_query="x" * 2000)
    with pytest.raises(ValidationError):
        AgentDecision(action=AgentActionType.SEARCH_CANDIDATES, search_query="x" * 2001)


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
        agent_decisions=[
            AgentDecision(
                action=AgentActionType.SEARCH_CANDIDATES,
                search_query="Python bilən namizədləri göstər",
            ),
            AgentDecision(action=AgentActionType.FINAL_ANSWER, message="Nəticələr aşağıdadır."),
        ],
    )
    conversation = await _new_conversation(db_session, tenant, user, membership)
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
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
    assert conversation.last_search_candidate_ids == [str(candidate.id)]


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
        agent_decisions=[
            AgentDecision(
                action=AgentActionType.SEARCH_CANDIDATES,
                search_query="Python bilən namizədləri göstər",
            ),
            AgentDecision(action=AgentActionType.FINAL_ANSWER, message="Nəticələr aşağıdadır."),
        ],
    )
    conversation = await _new_conversation(db_session, tenant, user, membership)
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
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
        agent_decisions=[
            AgentDecision(
                action=AgentActionType.SEARCH_CANDIDATES,
                search_query="Python bilən namizədləri göstər",
            ),
            AgentDecision(action=AgentActionType.FINAL_ANSWER, message="Nəticələr aşağıdadır."),
        ],
    )
    conversation = await _new_conversation(db_session, tenant, user, membership)
    search_result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        message="Python bilən namizədləri göstər",
    )
    ordered_ids = sorted([first.id, second.id], key=str)
    await db_session.refresh(conversation)
    assert conversation.last_search_candidate_ids == [str(cid) for cid in ordered_ids]

    llm2 = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=1)
    )
    profile_turn = await _run(
        db_session,
        llm2,
        tenant_id=tenant.id,
        conversation=conversation,
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

    conversation = await _new_conversation(db_session, tenant, user, membership)
    conversation.last_search_candidate_ids = [str(candidate.id)]
    await db_session.flush()

    llm = FakeLLMProvider(
        agent_decision=AgentDecision(
            action=AgentActionType.GET_CANDIDATE_EVIDENCE, candidate_ref=1, evidence_topic="Python"
        )
    )
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        message="Python bacarığını sübut et",
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
    conversation = await _new_conversation(db_session, tenant, user, membership)
    # No prior search this conversation — candidate_ref=1 cannot resolve.
    llm = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=1)
    )
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation, message="üçüncünü aç"
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

    conversation = await _new_conversation(db_session, tenant, user, membership)
    # Even if a conversation row somehow held a foreign candidate_id (it
    # never would via normal search, since search is already
    # tenant-scoped), the profile lookup itself is independently
    # tenant-scoped and must not resolve it.
    conversation.last_search_candidate_ids = [str(foreign_candidate.id)]
    await db_session.flush()

    llm = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=1)
    )
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation, message="birincini aç"
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

    conversation_a = await _new_conversation(db_session, tenant, user, membership)
    conversation_a.last_search_candidate_ids = [str(candidate.id)]
    await db_session.flush()

    conversation_b = await _new_conversation(db_session, tenant, user, membership)
    assert conversation_b.id != conversation_a.id
    assert conversation_b.last_search_candidate_ids == []

    llm = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=1)
    )
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation_b, message="birincini aç"
    )
    assert result.outcome == AgentTurnOutcome.CANDIDATE_REF_NOT_FOUND


async def test_bounded_tool_call_loop_stops_at_configured_maximum(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation = await _new_conversation(db_session, tenant, user, membership)

    # A misbehaving/adversarial model keeps returning TOOL_CALL decisions
    # forever — the loop must still stop at max_tool_calls, never spin
    # unboundedly.
    from meyar.search.planner_schemas import PlannerDraft
    from meyar.search.schemas import RequiredFilters

    llm = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["NoMatch"])),
        agent_decisions=[
            AgentDecision(
                action=AgentActionType.SEARCH_CANDIDATES,
                search_query="NoMatch bilən namizədləri göstər",
            )
        ]
        * 10,
    )
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        message="NoMatch bilən namizədləri göstər",
        max_tool_calls=2,
    )
    assert result.outcome == AgentTurnOutcome.TOOL_CALL_LIMIT_EXCEEDED
    assert result.tool_call_count == 2


async def test_model_timeout_is_a_safe_typed_failure_not_an_exception(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation = await _new_conversation(db_session, tenant, user, membership)
    llm = FakeLLMProvider(agent_error=ModelTimeoutError("simulated timeout"))
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation, message="salam"
    )
    assert result.outcome == AgentTurnOutcome.AGENT_PROVIDER_FAILURE


async def test_model_unavailable_is_a_safe_typed_failure(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation = await _new_conversation(db_session, tenant, user, membership)
    llm = FakeLLMProvider(agent_error=ModelUnavailableError("simulated outage"))
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation, message="salam"
    )
    assert result.outcome == AgentTurnOutcome.AGENT_PROVIDER_FAILURE


async def test_repeated_schema_invalid_output_is_a_safe_typed_failure(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation = await _new_conversation(db_session, tenant, user, membership)
    llm = FakeLLMProvider(agent_fail_first_n_calls=99, agent_decision=None)
    # Give it a decisions list so the fake doesn't assert-fail on success path.
    llm._agent_decisions = [AgentDecision(action=AgentActionType.FINAL_ANSWER, message="unused")]
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation, message="salam"
    )
    assert result.outcome == AgentTurnOutcome.MALFORMED_MODEL_OUTPUT


async def test_skill_specific_duration_is_never_silently_weakened(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """Reuses the existing D-027 planner guarantee through the agent's
    search_candidates tool: 'N years of Java' must never silently become
    'Java skill + N years total experience'."""
    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()

    from meyar.search.planner_schemas import PlannerDraft, PlannerReasonCode
    from meyar.search.schemas import RequiredFilters

    draft = PlannerDraft(
        required_filters=RequiredFilters(skills=["Java"], min_total_experience_years=5.0)
    )
    llm = FakeLLMProvider(
        planner_draft=draft,
        agent_decisions=[
            AgentDecision(
                action=AgentActionType.SEARCH_CANDIDATES,
                search_query="5 il Java təcrübəsi olanları göstər",
            ),
            AgentDecision(
                action=AgentActionType.CLARIFY,
                message="Konkret bacarıq üzrə təcrübə müddətini sübut etmək mümkün deyil.",
            ),
        ],
    )
    conversation = await _new_conversation(db_session, tenant, user, membership)
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        message="5 il Java təcrübəsi olanları göstər",
    )
    search = result.tool_results[0].search
    assert search is not None
    assert search.response.plan.executable is False
    assert (
        PlannerReasonCode.SKILL_SPECIFIC_EXPERIENCE_DURATION_UNSUPPORTED
        in search.response.plan.reason_codes
    )


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
        agent_decisions=[
            AgentDecision(
                action=AgentActionType.SEARCH_CANDIDATES, search_query="qadın namizədləri göstər"
            ),
            AgentDecision(action=AgentActionType.CLARIFY, message="Bu meyar dəstəklənmir."),
        ],
    )
    conversation = await _new_conversation(db_session, tenant, user, membership)
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
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
