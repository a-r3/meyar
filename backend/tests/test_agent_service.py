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
    AgentResponseCode,
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
    explicit_action=None,
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
        explicit_action=explicit_action,
    )


def test_agent_decision_schema_rejects_mismatched_action_shape() -> None:
    with pytest.raises(ValidationError):
        AgentDecision(action=AgentActionType.SEARCH_CANDIDATES)  # missing search_query
    with pytest.raises(ValidationError):
        AgentDecision(
            action=AgentActionType.FINAL_ANSWER,
            candidate_ref=1,
            response_code=AgentResponseCode.ACKNOWLEDGEMENT,
        )
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
            {
                "action": "FINAL_ANSWER",
                "response_code": "ACKNOWLEDGEMENT",
                "unexpected_field": "x",
            }
        )


def test_final_answer_schema_rejects_model_authored_candidate_fact() -> None:
    # The removed free-text channel is structural: schema-valid model
    # output cannot carry a candidate assertion at all.
    with pytest.raises(ValidationError):
        AgentDecision.model_validate(
            {
                "action": "FINAL_ANSWER",
                "message": "The first candidate has 20 years of Python experience.",
            }
        )


def test_final_answer_schema_rejects_model_authored_hiring_recommendation() -> None:
    with pytest.raises(ValidationError):
        AgentDecision.model_validate(
            {"action": "FINAL_ANSWER", "message": "The first candidate should be hired."}
        )


def test_clarify_schema_rejects_model_authored_candidate_fact() -> None:
    with pytest.raises(ValidationError):
        AgentDecision.model_validate(
            {
                "action": "CLARIFY",
                "message": "The first candidate has 20 years of Python experience.",
            }
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
            AgentDecision(
                action=AgentActionType.FINAL_ANSWER, response_code=AgentResponseCode.ACKNOWLEDGEMENT
            ),
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
            AgentDecision(
                action=AgentActionType.FINAL_ANSWER, response_code=AgentResponseCode.ACKNOWLEDGEMENT
            ),
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
            AgentDecision(
                action=AgentActionType.FINAL_ANSWER, response_code=AgentResponseCode.ACKNOWLEDGEMENT
            ),
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
    # unboundedly. Each decision uses a DISTINCT query so this test
    # exercises the true unbounded-loop guard rather than the separate
    # identical-repeated-query dedup guard (see
    # test_repeated_identical_search_finalizes_instead_of_looping below).
    from meyar.search.planner_schemas import PlannerDraft
    from meyar.search.schemas import RequiredFilters

    llm = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["NoMatch"])),
        agent_decisions=[
            AgentDecision(
                action=AgentActionType.SEARCH_CANDIDATES,
                search_query=f"NoMatch bilən namizədləri göstər #{i}",
            )
            for i in range(10)
        ],
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


async def test_repeated_identical_search_finalizes_instead_of_looping(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """Real-Ollama Slice 2 acceptance testing (PR #40): once made fast by
    disabling qwen3's default hidden-thinking mode (D-039), the model would
    re-issue an identical SEARCH_CANDIDATES call for a request it had
    already fully answered, eventually co-rendering a contradictory
    TOOL_CALL_LIMIT_EXCEEDED ("simplify your query") banner above several
    duplicated result blocks. An identical repeated query within one turn
    must finalize on the existing results instead of repeating the work."""
    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation = await _new_conversation(db_session, tenant, user, membership)

    from meyar.search.planner_schemas import PlannerDraft
    from meyar.search.schemas import RequiredFilters

    llm = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"])),
        agent_decisions=[
            AgentDecision(
                action=AgentActionType.SEARCH_CANDIDATES,
                search_query="Python bilən namizədləri göstər",
            )
        ]
        * 5,
    )
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        message="Python bilən namizədləri göstər",
        max_tool_calls=3,
    )
    assert result.outcome == AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT
    assert result.tool_call_count == 1
    assert len(result.tool_results) == 1


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


async def test_zero_tool_final_answer_uses_only_server_owned_copy(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation = await _new_conversation(db_session, tenant, user, membership)
    llm = FakeLLMProvider(
        agent_decision=AgentDecision(
            action=AgentActionType.FINAL_ANSWER,
            response_code=AgentResponseCode.GREETING,
        )
    )

    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation, message="salam"
    )

    assert result.tool_call_count == 0
    assert result.tool_results == []
    assert result.message == (
        "Salam! Namizəd axtarışı, profil sübutları və vakansiya meyarları ilə bağlı "
        "kömək edə bilərəm."
    )
    await db_session.refresh(conversation)
    assert conversation.turns[-1]["text"] == result.message
    assert conversation.turns[-1]["text_authority"] == "SERVER_VALIDATED"


async def test_hiring_request_uses_server_owned_human_decision_copy(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation = await _new_conversation(db_session, tenant, user, membership)
    llm = FakeLLMProvider(
        agent_decision=AgentDecision(
            action=AgentActionType.CLARIFY,
            response_code=AgentResponseCode.HIRING_DECISION_REQUIRES_HUMAN,
        )
    )

    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
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
    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation = await _new_conversation(db_session, tenant, user, membership)
    llm = FakeLLMProvider(agent_fail_first_n_calls=99, agent_decision=None)
    # Give it a decisions list so the fake doesn't assert-fail on success path.
    llm._agent_decisions = [
        AgentDecision(
            action=AgentActionType.FINAL_ANSWER, response_code=AgentResponseCode.ACKNOWLEDGEMENT
        )
    ]
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
                response_code=AgentResponseCode.NEED_MORE_DETAIL,
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
            AgentDecision(
                action=AgentActionType.CLARIFY, response_code=AgentResponseCode.UNSUPPORTED_REQUEST
            ),
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


# --- D-036 regression tests: a successful tool result must never co-occur
# with a fatal-looking outcome, and a stored assistant turn must never be
# blank. See docs/DECISIONS.md D-036 for the live-inspection bug report
# and root cause (a follow-up "what next" decision failing after
# SEARCH_CANDIDATES already succeeded was returned as MALFORMED_MODEL_OUTPUT
# while still carrying the successful tool_results). ---


async def test_successful_search_with_failed_followup_framing_is_not_fatal(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """The exact owner-reported repro: SEARCH_CANDIDATES succeeds, then the
    loop's follow-up decision call fails (schema-invalid on both attempts).
    Must be ANSWERED_FROM_TOOL_RESULT (never MALFORMED_MODEL_OUTPUT or
    AGENT_PROVIDER_FAILURE) with the grounded search result intact and no
    model message (a deterministic UI fallback covers it — see
    test_ui_agent_routes.py for the rendered-page assertion)."""
    tenant, user, _password, membership = tenant_and_user
    candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()

    from meyar.search.planner_schemas import PlannerDraft
    from meyar.search.schemas import RequiredFilters

    llm = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"])),
        agent_decision=AgentDecision(
            action=AgentActionType.SEARCH_CANDIDATES, search_query="Python bilən namizədləri göstər"
        ),
        agent_fail_after_n_calls=1,  # decision #1 (the search) succeeds; every call after fails
    )
    conversation = await _new_conversation(db_session, tenant, user, membership)
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        message="Python bilən namizədləri göstər",
    )
    assert result.outcome == AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT
    assert result.outcome not in (
        AgentTurnOutcome.MALFORMED_MODEL_OUTPUT,
        AgentTurnOutcome.AGENT_PROVIDER_FAILURE,
    )
    assert result.message is None
    assert len(result.tool_results) == 1
    search = result.tool_results[0].search
    assert search is not None
    assert search.response.plan.executable is True
    assert search.response.search_response.result_count == 1
    assert search.response.search_response.results[0].candidate_id == candidate.id


async def test_successful_search_with_followup_provider_outage_is_not_fatal(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """Same as above, but the follow-up call fails with a provider-level
    error (timeout/unavailable) rather than repeated schema-invalid
    output — must reach the same non-fatal ANSWERED_FROM_TOOL_RESULT."""
    tenant, user, _password, membership = tenant_and_user
    await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()

    from meyar.search.planner_schemas import PlannerDraft
    from meyar.search.schemas import RequiredFilters

    llm = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"])),
        agent_decision=AgentDecision(
            action=AgentActionType.SEARCH_CANDIDATES, search_query="Python bilən namizədləri göstər"
        ),
        agent_fail_after_n_calls=1,
        agent_error=ModelTimeoutError("simulated follow-up timeout"),
    )
    conversation = await _new_conversation(db_session, tenant, user, membership)
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        message="Python bilən namizədləri göstər",
    )
    assert result.outcome == AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT
    assert len(result.tool_results) == 1
    assert result.tool_results[0].search.response.search_response.result_count == 1


async def test_no_result_model_failure_stays_a_safe_failure_with_no_tool_results(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """The FIRST decision failing (no tool ever ran) must remain a genuine
    fatal outcome with an empty tool_results list — never dressed up as a
    success. Covers both the repeated-schema-invalid and provider-outage
    shapes."""
    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation = await _new_conversation(db_session, tenant, user, membership)

    llm_malformed = FakeLLMProvider(agent_fail_first_n_calls=99, agent_decision=None)
    llm_malformed._agent_decisions = [
        AgentDecision(
            action=AgentActionType.FINAL_ANSWER, response_code=AgentResponseCode.ACKNOWLEDGEMENT
        )
    ]
    result = await _run(
        db_session, llm_malformed, tenant_id=tenant.id, conversation=conversation, message="salam"
    )
    assert result.outcome == AgentTurnOutcome.MALFORMED_MODEL_OUTPUT
    assert result.tool_results == []

    conversation2 = await _new_conversation(db_session, tenant, user, membership)
    llm_outage = FakeLLMProvider(agent_error=ModelUnavailableError("simulated outage"))
    result2 = await _run(
        db_session, llm_outage, tenant_id=tenant.id, conversation=conversation2, message="salam"
    )
    assert result2.outcome == AgentTurnOutcome.AGENT_PROVIDER_FAILURE
    assert result2.tool_results == []


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
    conversation = await _new_conversation(db_session, tenant, user, membership)
    conversation.last_search_candidate_ids = [str(candidate.id)]
    await db_session.flush()

    llm = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=1)
    )
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation, message="birincini aç"
    )
    assert result.outcome == AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT
    assert result.message is None


async def test_ordinal_reference_still_resolves_after_followup_framing_failure(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """Rule E: last_search_candidate_ids must survive a turn whose
    follow-up framing step failed, so a later 'birincini aç' still
    resolves — the grounded search result is not undone by the framing
    failure."""
    tenant, user, _password, membership = tenant_and_user
    candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()

    from meyar.search.planner_schemas import PlannerDraft
    from meyar.search.schemas import RequiredFilters

    llm_search = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"])),
        agent_decision=AgentDecision(
            action=AgentActionType.SEARCH_CANDIDATES, search_query="Python bilən namizədləri göstər"
        ),
        agent_fail_after_n_calls=1,
    )
    conversation = await _new_conversation(db_session, tenant, user, membership)
    first_result = await _run(
        db_session,
        llm_search,
        tenant_id=tenant.id,
        conversation=conversation,
        message="Python bilən namizədləri göstər",
    )
    assert first_result.outcome == AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT
    await db_session.refresh(conversation)
    assert conversation.last_search_candidate_ids == [str(candidate.id)]

    llm_profile = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=1)
    )
    second_result = await _run(
        db_session,
        llm_profile,
        tenant_id=tenant.id,
        conversation=conversation,
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
    conversation = await _new_conversation(db_session, tenant, user, membership)
    conversation.last_search_candidate_ids = [str(candidate.id)]
    await db_session.flush()

    llm = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=1),
        grounded_selection=GroundedSelection(used_facts=[2, 0]),  # employment fact, Python skill
    )
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
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
    conversation = await _new_conversation(db_session, tenant, user, membership)
    conversation.last_search_candidate_ids = [str(candidate.id)]
    await db_session.flush()

    llm = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=1),
        grounded_selection=GroundedSelection(used_facts=[999]),
    )
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
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
    conversation = await _new_conversation(db_session, tenant, user, membership)
    conversation.last_search_candidate_ids = [str(candidate.id)]
    await db_session.flush()

    llm = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=1),
        grounded_error=ModelTimeoutError("simulated grounded-synthesis timeout"),
    )
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
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
    rejected — last_search_candidate_ids is untouched by this feature."""
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
    conversation = await _new_conversation(db_session, tenant, user, membership)
    conversation.last_search_candidate_ids = [str(cid) for cid in ordered_ids]
    await db_session.flush()

    llm = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=2),
        grounded_selection=GroundedSelection(used_facts=[0]),
    )
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation, message="ikincini aç"
    )
    assert result.tool_results[0].profile.candidate_id == ordered_ids[1]
    await db_session.refresh(conversation)
    assert conversation.last_search_candidate_ids == [str(cid) for cid in ordered_ids]


# --- Candidate-factuality P0: no model-authored response prose channel. ---


async def test_prompt_leaking_clarify_message_is_rejected_and_retried(
    db_session: AsyncSession, tenant_and_user
) -> None:
    from meyar.agent.prompts import AGENT_SYSTEM_PROMPT

    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation = await _new_conversation(db_session, tenant, user, membership)
    leaked_sentence = AGENT_SYSTEM_PROMPT.splitlines()[3].strip()
    assert len(leaked_sentence) >= 40
    with pytest.raises(ValidationError):
        AgentDecision.model_validate({"action": "CLARIFY", "message": leaked_sentence})
    llm = FakeLLMProvider(
        agent_decision=AgentDecision(
            action=AgentActionType.CLARIFY,
            response_code=AgentResponseCode.CANDIDATE_REFERENCE_REQUIRED,
        )
    )
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation, message="salam"
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
    conversation = await _new_conversation(db_session, tenant, user, membership)
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
    conversation = await _new_conversation(db_session, tenant, user, membership)
    jd_text = "Baş Backend Mühəndisi axtarırıq. Python bilməlidir. AWS üstünlükdür."
    llm = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.DRAFT_JOB_CRITERIA),
        jd_draft=JDCriteriaDraft(
            title="Baş Backend Mühəndisi",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind=CriterionKind.SKILL, requirement="Python", source_text="Python bilməlidir"
                )
            ],
            preferred=[
                JDDraftCriterionItem(
                    span_id="req-0002",
                    kind=CriterionKind.SKILL, requirement="AWS", source_text="AWS üstünlükdür"
                )
            ],
        ),
    )
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation, message=jd_text
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
    assert llm.agent_call_count == 1


async def test_draft_job_criteria_title_never_becomes_trusted_assistant_headline(
    db_session: AsyncSession, tenant_and_user
) -> None:
    from meyar.agent.schemas import JDCriteriaDraft, JDDraftCriterionItem
    from meyar.schemas.criteria import CriterionKind
    from meyar.ui.service import build_agent_turn_view

    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation = await _new_conversation(db_session, tenant, user, membership)
    adversarial_title = "The first candidate has 20 years of Python experience and should be hired."
    llm = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.DRAFT_JOB_CRITERIA),
        jd_draft=JDCriteriaDraft(
            title=adversarial_title,
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind=CriterionKind.SKILL, requirement="Python", source_text="Python bilməlidir"
                )
            ],
        ),
    )
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        message="Backend role. Python bilməlidir.",
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
    conversation = await _new_conversation(db_session, tenant, user, membership)
    jd_text = "Uzun bir vakansiya təsviri." * 50
    llm = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.DRAFT_JOB_CRITERIA),
        jd_draft=JDCriteriaDraft(title="Rol"),
    )
    await _run(db_session, llm, tenant_id=tenant.id, conversation=conversation, message=jd_text)
    assert llm.last_jd_text == jd_text


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
    conversation = await _new_conversation(db_session, tenant, user, membership)
    llm = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.DRAFT_JOB_CRITERIA),
        jd_draft=JDCriteriaDraft(
            title="Rol",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0002",
                    kind=CriterionKind.SKILL, requirement="Python", source_text="Python bilməlidir"
                ),
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind=CriterionKind.SKILL, requirement="kişi", source_text="kişi olmalıdır"
                ),
            ],
        ),
    )
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        message="Rol üçün namizəd kişi olmalıdır. Python bilməlidir.",
    )
    draft = result.tool_results[0].job_draft
    assert draft is not None
    assert [c.label for c in draft.must_have] == ["Python"]
    assert draft.prohibited_count == 1
    assert draft.unsupported == []
    assert draft.ungrounded_count == 0
    rendered = result.model_dump_json()
    assert "kişi" not in rendered


async def test_draft_job_criteria_discloses_unsupported_non_sensitive_item(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """A non-sensitive requirement that CriterionIn cannot represent (here:
    an EXPERIENCE item the JD text gave no derivable duration for) must
    remain visible to HR as an unsupported requirement — never silently
    lost like a PROHIBITED one's own text (D-043, PR #42 owner correction,
    issue #33's ACAMS example)."""
    from meyar.agent.schemas import JDCriteriaDraft, JDDraftCriterionItem
    from meyar.schemas.criteria import CriterionKind, CriterionType

    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation = await _new_conversation(db_session, tenant, user, membership)
    llm = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.DRAFT_JOB_CRITERIA),
        jd_draft=JDCriteriaDraft(
            title="Rol",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind=CriterionKind.SKILL, requirement="Python", source_text="Python bilməlidir"
                ),
                    # Skill-scoped experience without a duration cannot be
                    # round-tripped by the current agent review form.
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
        message="Python bilməlidir. Backend təcrübəsi tələb olunur.",
    )
    draft = result.tool_results[0].job_draft
    assert draft is not None
    assert [c.label for c in draft.must_have] == ["Python"]
    assert draft.prohibited_count == 0
    assert draft.ungrounded_count == 0
    assert len(draft.unsupported) == 1
    assert draft.unsupported[0].requirement == "Backend təcrübəsi tələb olunur"
    assert draft.unsupported[0].criterion_type == CriterionType.MUST_HAVE
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
    conversation = await _new_conversation(db_session, tenant, user, membership)
    llm = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.DRAFT_JOB_CRITERIA),
        jd_draft=JDCriteriaDraft(
            title="Rol",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind=CriterionKind.SKILL, requirement="Python", source_text="Python bilməlidir"
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
        message="Python bilməlidir. Namizəd ezamiyyətə hazır olması üstünlükdür.",
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
    from meyar.agent.jd_authority import segment_requirement_spans

    jd_text = "Vakansiya: Regional Satış Nümayəndəsi. Namizəd ezamiyyətə getməyə hazır olmalıdır."
    spans = segment_requirement_spans(jd_text)
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
    conversation = await _new_conversation(db_session, tenant, user, membership)
    jd_text = "Vakansiya: Kredit Analitiki. Namizəd ezamiyyətə getməyə hazır olmalıdır."
    llm = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.DRAFT_JOB_CRITERIA),
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
        db_session, llm, tenant_id=tenant.id, conversation=conversation, message=jd_text
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


# --- D-043 (PR #42 owner correction, issue #33): explicit_action deterministic routing ---


async def test_explicit_draft_job_criteria_action_never_calls_the_routing_model(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """The first-class "JD-dən meyar hazırla" affordance must route
    deterministically — no dependence on a small local model correctly
    inferring DRAFT_JOB_CRITERIA from arbitrary pasted text (D-042 point
    6's documented unreliability). ``agent_decision`` is deliberately left
    unset: if the code fell back to calling llm.decide_agent_action, the
    FakeLLMProvider would raise (no decision configured), failing the
    test loudly rather than silently routing correctly by coincidence."""
    from meyar.agent.schemas import AgentActionType, JDCriteriaDraft, JDDraftCriterionItem
    from meyar.schemas.criteria import CriterionKind

    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation = await _new_conversation(db_session, tenant, user, membership)
    jd_text = "Bir mətn, heç bir açıq açar söz olmadan. Python bilməlidir."
    llm = FakeLLMProvider(
        jd_draft=JDCriteriaDraft(
            title="Rol",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind=CriterionKind.SKILL, requirement="Python", source_text="Python bilməlidir"
                )
            ],
        ),
    )
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        message=jd_text,
        explicit_action=AgentActionType.DRAFT_JOB_CRITERIA,
    )
    assert result.outcome == AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT
    assert result.tool_results[0].tool_name == AgentActionType.DRAFT_JOB_CRITERIA
    # No routing-decision call was made at all — deterministic, not model-inferred.
    assert llm.agent_call_count == 0
    assert llm.jd_draft_call_count == 1


async def test_explicit_draft_job_criteria_action_cannot_become_a_search(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """Even when the configured routing decision would misroute a pasted
    JD to SEARCH_CANDIDATES (the exact D-042 point 6 failure mode), the
    explicit affordance must still deterministically draft criteria — the
    routing model's own (unused) decision can never leak through."""
    from meyar.agent.schemas import AgentActionType, JDCriteriaDraft, JDDraftCriterionItem
    from meyar.schemas.criteria import CriterionKind

    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation = await _new_conversation(db_session, tenant, user, membership)
    jd_text = "Vakansiya: Backend Mühəndisi. Python bilməlidir."
    llm = FakeLLMProvider(
        agent_decision=AgentDecision(
            action=AgentActionType.SEARCH_CANDIDATES, search_query=jd_text
        ),
        jd_draft=JDCriteriaDraft(
            title="Backend Mühəndisi",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind=CriterionKind.SKILL, requirement="Python", source_text="Python bilməlidir"
                )
            ],
        ),
    )
    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        message=jd_text,
        explicit_action=AgentActionType.DRAFT_JOB_CRITERIA,
    )
    assert result.tool_results[0].tool_name == AgentActionType.DRAFT_JOB_CRITERIA
    assert llm.agent_call_count == 0
    assert llm.call_count == 0  # plan_candidate_search was never reached


async def test_run_agent_turn_rejects_unsupported_explicit_action(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """explicit_action is a server-controlled value (meyar.ui.router), not
    a client-supplied one — but defensively reject anything other than
    the one supported bare action rather than silently ignoring it."""
    from meyar.agent.schemas import AgentActionType

    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation = await _new_conversation(db_session, tenant, user, membership)
    llm = FakeLLMProvider()
    with pytest.raises(ValueError):
        await _run(
            db_session,
            llm,
            tenant_id=tenant.id,
            conversation=conversation,
            message="salam",
            explicit_action=AgentActionType.GET_CANDIDATE_PROFILE,
        )


async def test_draft_job_criteria_repairs_after_one_schema_invalid_attempt(
    db_session: AsyncSession, tenant_and_user
) -> None:
    from meyar.agent.schemas import JDCriteriaDraft

    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation = await _new_conversation(db_session, tenant, user, membership)
    llm = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.DRAFT_JOB_CRITERIA),
        jd_draft=JDCriteriaDraft(title="Rol"),
        jd_draft_fail_first_n_calls=1,
    )
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation, message="JD mətni"
    )
    assert result.outcome == AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT
    assert llm.jd_draft_call_count == 2


async def test_draft_job_criteria_provider_failure_is_a_safe_typed_failure(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation = await _new_conversation(db_session, tenant, user, membership)
    llm = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.DRAFT_JOB_CRITERIA),
        jd_draft_error=ModelUnavailableError("simulated outage"),
    )
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation, message="JD mətni"
    )
    assert result.outcome == AgentTurnOutcome.JOB_DRAFT_FAILED
    assert result.tool_results == []


async def test_evidence_topic_is_resolved_from_profile_fact_not_rendered_raw(
    db_session: AsyncSession, tenant_and_user
) -> None:
    from meyar.ui.service import build_agent_turn_view

    tenant, user, _password, membership = tenant_and_user
    candidate, _profile_version = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python", quote="Python")
    )
    await db_session.commit()
    conversation = await _new_conversation(db_session, tenant, user, membership)
    conversation.last_search_candidate_ids = [str(candidate.id)]
    raw_topic = "The first candidate should be hired immediately"
    llm = FakeLLMProvider(
        agent_decision=AgentDecision(
            action=AgentActionType.GET_CANDIDATE_EVIDENCE,
            candidate_ref=1,
            evidence_topic=raw_topic,
        )
    )

    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        message="Birinci namizəd üzrə sübut göstər",
    )
    view = await build_agent_turn_view(db_session, tenant_id=tenant.id, result=result)

    evidence = result.tool_results[0].evidence
    assert evidence is not None
    assert evidence.topic is None
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
    conversation = await _new_conversation(db_session, tenant, user, membership)
    conversation.last_search_candidate_ids = [str(candidate.id)]
    llm = FakeLLMProvider(
        agent_decision=AgentDecision(
            action=AgentActionType.GET_CANDIDATE_EVIDENCE,
            candidate_ref=1,
            evidence_topic="python",
        )
    )

    result = await _run(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        message="Python sübutunu göstər",
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
    conversation = await _new_conversation(db_session, tenant, user, membership)
    llm = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.DRAFT_JOB_CRITERIA),
        jd_draft=JDCriteriaDraft(title="unused"),
        jd_draft_fail_first_n_calls=99,
    )
    result = await _run(
        db_session, llm, tenant_id=tenant.id, conversation=conversation, message="JD mətni"
    )
    assert result.outcome == AgentTurnOutcome.JOB_DRAFT_FAILED
    assert result.tool_results == []
