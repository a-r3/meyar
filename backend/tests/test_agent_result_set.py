"""Server-owned result-set authority (issue #49) — replaces the old
``AgentConversation.last_search_candidate_ids`` ordinal-memory column.

Covers: AgentResultSet/AgentResultSetMember creation+provenance, the exact
resolve_active_candidate_ref check order (tenant/session/context_epoch/
expiry/corpus-freshness/ordinal-range/authorization), staleness/expiry
detection, audit privacy, "Yeni söhbət" context-epoch invalidation,
concurrency safety of the owning conversation row lock, no Job/Evaluation
side effects, and the CandidateIdentity exclusion boundary."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from conftest import TEST_DATABASE_URL
from fakes import FakeLLMProvider
from search_helpers import (
    open_test_conversation,
    reload_test_conversation,
    seed_active_result_set,
    seed_candidate_with_profile,
    seed_embedding,
    seed_next_profile_version,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from meyar.agent.schemas import AgentActionType, AgentDecision, AgentResponseCode
from meyar.agent.service import run_agent_turn
from meyar.models.agent_result_set import AgentResultSet, AgentResultSetMember
from meyar.models.audit_event import AuditEvent
from meyar.models.evaluation import Evaluation
from meyar.models.job import Job as JobModel
from meyar.models.job_criteria_version import JobCriteriaVersion
from meyar.search.planner_schemas import (
    PLANNER_SCHEMA_VERSION,
    PlannedCandidateSearchResponse,
    PlannerOutcome,
    SearchPlanResult,
)
from meyar.search.policy import SEARCH_POLICY_VERSION
from meyar.search.schemas import (
    CandidateSearchRequest,
    CandidateSearchResponse,
    CandidateSearchResult,
    EmbeddingSearchConfig,
    RequiredFilters,
    SearchMode,
)
from meyar.services.agent_conversation_repo import (
    OwnerPrincipal,
    get_owned_conversation_for_update,
    get_session_context_for_update,
)
from meyar.services.agent_result_set_repo import (
    ResultSetResolutionFailure,
    active_result_set_size,
    compute_corpus_fingerprint,
    create_result_set_from_search,
    resolve_active_candidate_ref,
)
from meyar.services.browser_session_repo import create_browser_session
from meyar.services.candidate_service import delete_candidate_cascade
from meyar.storage.local import LocalFilesystemStorage
from meyar.storage.photo import LocalPhotoStorage

AS_OF_DATE = datetime(2026, 1, 1, tzinfo=UTC).date()

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
        # The evidence backstop (meyar.extraction.evidence) requires the
        # skill name itself to be attributable in its own quote — mirrors
        # test_agent_service.py's own ``_profile`` helper exactly.
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
    }


def _embedding_config() -> EmbeddingSearchConfig:
    return EmbeddingSearchConfig(
        provider="fake-embedding",
        model_name="fake-embedding-model-v1",
        model_revision="",
        serializer_version="v1",
        embedding_dimensions=8,
    )


def _search_plan(
    request: CandidateSearchRequest, *, request_sha256: str = "a" * 64
) -> SearchPlanResult:
    return SearchPlanResult(
        executable=True,
        outcome=PlannerOutcome.EXECUTABLE,
        search_request=request,
        planner_policy_version="test-planner-policy-v1",
        prompt_version="test-planner-prompt-v1",
        schema_version=PLANNER_SCHEMA_VERSION,
        model_provider="test",
        model_name="test-model",
        model_revision="",
        attempt_count=0,
        request_sha256=request_sha256,
    )


def _planned(
    request: CandidateSearchRequest, results: list[CandidateSearchResult]
) -> PlannedCandidateSearchResponse:
    response = CandidateSearchResponse(
        mode=request.mode,
        policy_version=SEARCH_POLICY_VERSION,
        results=results,
        result_count=len(results),
        eligible_profile_count=len(results),
        compatible_embedding_count=sum(
            1 for r in results if r.candidate_embedding_version_id is not None
        ),
        excluded_missing_embedding_count=0,
        limit=20,
        embedding_config=request.embedding_config,
    )
    return PlannedCandidateSearchResponse(plan=_search_plan(request), search_response=response)


async def _member_rows(
    db_session: AsyncSession, *, result_set_id: uuid.UUID
) -> list[AgentResultSetMember]:
    rows = await db_session.execute(
        select(AgentResultSetMember)
        .where(AgentResultSetMember.result_set_id == result_set_id)
        .order_by(AgentResultSetMember.ordinal.asc())
    )
    return list(rows.scalars())


async def _new_conversation(db_session, tenant, user, membership):
    session, _raw = await create_browser_session(
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
    return conversation, context, session


# --- Creation / provenance -------------------------------------------------


async def test_structured_search_creates_result_set_with_correct_members(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    first, pv1 = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    second, pv2 = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()
    conversation, context, session = await _new_conversation(db_session, tenant, user, membership)

    request = CandidateSearchRequest(
        mode=SearchMode.STRUCTURED_ONLY, required_filters=RequiredFilters(skills=["Python"])
    )
    results = [
        CandidateSearchResult(
            candidate_id=first.id, rank=1, mode=SearchMode.STRUCTURED_ONLY,
            relevance_score=0.9, structured_score=0.9, semantic_score=None,
            candidate_profile_version_id=pv1.id, candidate_embedding_version_id=None,
            search_policy_version=SEARCH_POLICY_VERSION,
        ),
        CandidateSearchResult(
            candidate_id=second.id, rank=2, mode=SearchMode.STRUCTURED_ONLY,
            relevance_score=0.5, structured_score=0.5, semantic_score=None,
            candidate_profile_version_id=pv2.id, candidate_embedding_version_id=None,
            search_policy_version=SEARCH_POLICY_VERSION,
        ),
    ]
    result_set = await create_result_set_from_search(
        db_session,
        tenant_id=tenant.id,
        browser_session_id=session.id,
        session_context=context,
        planned=_planned(request, results),
        previous_result_set_id=None,
    )
    await db_session.commit()

    members = await _member_rows(db_session, result_set_id=result_set.id)
    assert [m.candidate_id for m in members] == [first.id, second.id]
    assert [m.ordinal for m in members] == [1, 2]
    assert members[0].candidate_profile_version_id == pv1.id
    assert members[0].relevance_score == 0.9
    assert members[0].structured_score == 0.9
    assert members[0].semantic_score is None
    assert members[0].candidate_embedding_version_id is None

    events = list(
        await db_session.scalars(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenant.id,
                AuditEvent.event_type == "agent.result_set.created",
            )
        )
    )
    assert len(events) == 1
    assert events[0].event_metadata["result_set_id"] == str(result_set.id)
    assert events[0].event_metadata["previous_result_set_id"] is None
    assert events[0].event_metadata["result_count"] == 2
    assert events[0].event_metadata["search_mode"] == "STRUCTURED_ONLY"


async def test_semantic_search_populates_embedding_version_id(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    content = _profile("Python")
    candidate, pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=content
    )
    await db_session.commit()
    embedding = await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        profile_version_id=pv.id,
        vector=[0.1] * 8,
        profile_content=content,
    )
    await db_session.commit()
    conversation, context, session = await _new_conversation(db_session, tenant, user, membership)

    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY,
        semantic_query="Python engineer",
        embedding_config=_embedding_config(),
    )
    results = [
        CandidateSearchResult(
            candidate_id=candidate.id, rank=1, mode=SearchMode.SEMANTIC_ONLY,
            relevance_score=0.8, structured_score=None, semantic_score=0.8,
            candidate_profile_version_id=pv.id, candidate_embedding_version_id=embedding.id,
            search_policy_version=SEARCH_POLICY_VERSION,
        )
    ]
    result_set = await create_result_set_from_search(
        db_session,
        tenant_id=tenant.id,
        browser_session_id=session.id,
        session_context=context,
        planned=_planned(request, results),
        previous_result_set_id=None,
    )
    await db_session.commit()

    members = await _member_rows(db_session, result_set_id=result_set.id)
    assert members[0].candidate_embedding_version_id == embedding.id
    assert members[0].semantic_score == 0.8
    assert members[0].structured_score is None


async def test_hybrid_search_populates_both_scores(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    content = _profile("Python")
    candidate, pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=content
    )
    await db_session.commit()
    embedding = await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        profile_version_id=pv.id,
        vector=[0.1] * 8,
        profile_content=content,
    )
    await db_session.commit()
    conversation, context, session = await _new_conversation(db_session, tenant, user, membership)

    request = CandidateSearchRequest(
        mode=SearchMode.HYBRID,
        required_filters=RequiredFilters(skills=["Python"]),
        semantic_query="Python engineer",
        embedding_config=_embedding_config(),
    )
    results = [
        CandidateSearchResult(
            candidate_id=candidate.id, rank=1, mode=SearchMode.HYBRID,
            relevance_score=0.85, structured_score=0.9, semantic_score=0.8,
            candidate_profile_version_id=pv.id, candidate_embedding_version_id=embedding.id,
            search_policy_version=SEARCH_POLICY_VERSION,
        )
    ]
    result_set = await create_result_set_from_search(
        db_session,
        tenant_id=tenant.id,
        browser_session_id=session.id,
        session_context=context,
        planned=_planned(request, results),
        previous_result_set_id=None,
    )
    await db_session.commit()

    members = await _member_rows(db_session, result_set_id=result_set.id)
    assert members[0].structured_score == 0.9
    assert members[0].semantic_score == 0.8
    assert members[0].candidate_embedding_version_id == embedding.id


async def test_failed_search_leaves_prior_active_result_set_untouched(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()
    conversation, context, session = await _new_conversation(db_session, tenant, user, membership)
    seeded = await seed_active_result_set(
        db_session,
        tenant_id=tenant.id,
        browser_session_id=session.id,
        session_context=context,
        candidate_ids=[candidate.id],
    )
    await db_session.commit()

    from meyar.search.planner_schemas import PlannerDraft

    llm = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["qadın"])),
        agent_decisions=[
            AgentDecision(
                action=AgentActionType.SEARCH_CANDIDATES, search_query="qadın namizədləri göstər"
            ),
            AgentDecision(
                action=AgentActionType.CLARIFY, response_code=AgentResponseCode.UNSUPPORTED_REQUEST
            ),
        ],
    )
    result = await run_agent_turn(
        db_session,
        llm,
        tenant_id=tenant.id,
        conversation=conversation,
        session_context=context,
        user_message="qadın namizədləri göstər",
        as_of_date=AS_OF_DATE,
        embedding_config=_embedding_config(),
        embedding_provider=None,
        max_tool_calls=3,
        max_context_turns=8,
    )
    assert result.tool_results[0].search.response.plan.executable is False
    await db_session.refresh(conversation)
    await db_session.refresh(context)
    assert context.active_result_set_id == seeded.id


async def test_second_search_creates_new_result_set_and_switches_pointer(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()
    conversation, context, session = await _new_conversation(db_session, tenant, user, membership)

    from meyar.search.planner_schemas import PlannerDraft

    llm = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"])),
        agent_decisions=[
            AgentDecision(action=AgentActionType.SEARCH_CANDIDATES, search_query="Python 1"),
            AgentDecision(
                action=AgentActionType.FINAL_ANSWER, response_code=AgentResponseCode.ACKNOWLEDGEMENT
            ),
        ],
    )
    await run_agent_turn(
        db_session, llm, tenant_id=tenant.id, conversation=conversation,
            session_context=context, user_message="Python 1",
        as_of_date=AS_OF_DATE, embedding_config=_embedding_config(), embedding_provider=None,
        max_tool_calls=3, max_context_turns=8,
    )
    await db_session.refresh(conversation)
    await db_session.refresh(context)
    first_result_set_id = context.active_result_set_id
    assert first_result_set_id is not None

    llm2 = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"])),
        agent_decisions=[
            AgentDecision(action=AgentActionType.SEARCH_CANDIDATES, search_query="Python 2"),
            AgentDecision(
                action=AgentActionType.FINAL_ANSWER, response_code=AgentResponseCode.ACKNOWLEDGEMENT
            ),
        ],
    )
    await run_agent_turn(
        db_session, llm2, tenant_id=tenant.id, conversation=conversation,
            session_context=context, user_message="Python 2",
        as_of_date=AS_OF_DATE, embedding_config=_embedding_config(), embedding_provider=None,
        max_tool_calls=3, max_context_turns=8,
    )
    await db_session.refresh(conversation)
    await db_session.refresh(context)
    second_result_set_id = context.active_result_set_id
    assert second_result_set_id is not None
    assert second_result_set_id != first_result_set_id

    events = list(
        await db_session.scalars(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenant.id,
                AuditEvent.event_type == "agent.result_set.created",
            )
        )
    )
    assert len(events) == 2
    # No ORDER BY is meaningful here (both events share one transaction, so
    # created_at ties); identify each event by its own result_set_id.
    by_result_set = {event.event_metadata["result_set_id"]: event for event in events}
    first_event = by_result_set[str(first_result_set_id)]
    second_event = by_result_set[str(second_result_set_id)]
    assert first_event.event_metadata["previous_result_set_id"] is None
    assert second_event.event_metadata["previous_result_set_id"] == str(first_result_set_id)


# --- Resolution --------------------------------------------------------


async def test_ordinal_resolves_to_the_right_candidate_for_each_position(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    candidates = []
    for i in range(3):
        candidate, _pv = await seed_candidate_with_profile(
            db_session, tenant_id=tenant.id, profile_content=_profile("Python", quote=f"c{i}")
        )
        candidates.append(candidate)
    await db_session.commit()
    conversation, context, session = await _new_conversation(db_session, tenant, user, membership)
    await seed_active_result_set(
        db_session,
        tenant_id=tenant.id,
        browser_session_id=session.id,
        session_context=context,
        candidate_ids=[c.id for c in candidates],
    )
    await db_session.commit()

    for ordinal, candidate in enumerate(candidates, start=1):
        resolved = await resolve_active_candidate_ref(
            db_session,
            tenant_id=tenant.id,
            browser_session_id=session.id,
            session_context=context,
            candidate_ref=ordinal,
        )
        assert not isinstance(resolved, ResultSetResolutionFailure)
        assert resolved.candidate_id == candidate.id


async def test_ordinal_out_of_range_fails(db_session: AsyncSession, tenant_and_user) -> None:
    tenant, user, _password, membership = tenant_and_user
    candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()
    conversation, context, session = await _new_conversation(db_session, tenant, user, membership)
    await seed_active_result_set(
        db_session, tenant_id=tenant.id, browser_session_id=session.id,
        session_context=context, candidate_ids=[candidate.id],
    )
    await db_session.commit()

    resolved = await resolve_active_candidate_ref(
        db_session, tenant_id=tenant.id, browser_session_id=session.id,
        session_context=context, candidate_ref=2,
    )
    assert resolved == ResultSetResolutionFailure.ORDINAL_OUT_OF_RANGE


async def test_active_result_set_size_reflects_member_count_and_zero_on_failure(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    first, _pv1 = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    second, _pv2 = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python", quote="second")
    )
    await db_session.commit()
    conversation, context, session = await _new_conversation(db_session, tenant, user, membership)

    async def _size() -> int:
        return await active_result_set_size(
            db_session,
            tenant_id=tenant.id,
            browser_session_id=session.id,
            session_context=context,
        )

    assert await _size() == 0

    await seed_active_result_set(
        db_session, tenant_id=tenant.id, browser_session_id=session.id,
        session_context=context, candidate_ids=[first.id, second.id],
    )
    await db_session.commit()
    assert await _size() == 2

    # issue #80: "Yeni söhbət" is a NEW durable conversation with its own
    # clean context in the same BrowserSession — it never sees the first
    # conversation's ResultSet, and the first context is left untouched.
    _new_conv, new_context = await open_test_conversation(
        db_session,
        tenant_id=tenant.id,
        user_id=user.id,
        membership_id=membership.id,
        browser_session_id=session.id,
    )
    await db_session.commit()
    assert new_context.active_result_set_id is None
    assert (
        await active_result_set_size(
            db_session,
            tenant_id=tenant.id,
            browser_session_id=session.id,
            session_context=new_context,
        )
        == 0
    )
    assert await _size() == 2


async def test_no_active_result_set_fails(db_session: AsyncSession, tenant_and_user) -> None:
    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    conversation, context, session = await _new_conversation(db_session, tenant, user, membership)

    resolved = await resolve_active_candidate_ref(
        db_session, tenant_id=tenant.id, browser_session_id=session.id,
        session_context=context, candidate_ref=1,
    )
    assert resolved == ResultSetResolutionFailure.NO_ACTIVE_RESULT_SET


async def test_resolution_survives_a_fresh_conversation_fetch(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """Page-reload equivalent: a freshly re-fetched AgentConversation
    object (not the same Python instance created earlier) must still
    resolve — proves resolution is not reliant on any in-process cache."""
    tenant, user, _password, membership = tenant_and_user
    candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()
    conversation, context, session = await _new_conversation(db_session, tenant, user, membership)
    await seed_active_result_set(
        db_session, tenant_id=tenant.id, browser_session_id=session.id,
        session_context=context, candidate_ids=[candidate.id],
    )
    await db_session.commit()
    db_session.expunge(conversation)

    reloaded, reloaded_context = await reload_test_conversation(
        db_session, conversation=conversation, browser_session_id=session.id
    )
    assert reloaded is not conversation
    resolved = await resolve_active_candidate_ref(
        db_session, tenant_id=tenant.id, browser_session_id=session.id,
        session_context=reloaded_context, candidate_ref=1,
    )
    assert not isinstance(resolved, ResultSetResolutionFailure)
    assert resolved.candidate_id == candidate.id


async def test_cross_tenant_result_set_id_fails_closed(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    from meyar.services.tenant_repo import create_tenant

    foreign_tenant = await create_tenant(db_session, name="Foreign")
    foreign_candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=foreign_tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()

    conversation, context, session = await _new_conversation(db_session, tenant, user, membership)

    # Build a real AgentResultSet that legitimately belongs to the FOREIGN
    # tenant (own browser session would normally differ too), then tamper
    # THIS tenant's own conversation to point at it directly.
    foreign_result_set = await seed_active_result_set(
        db_session,
        tenant_id=foreign_tenant.id,
        browser_session_id=session.id,
        session_context=context,
        candidate_ids=[foreign_candidate.id],
    )
    await db_session.commit()

    resolved = await resolve_active_candidate_ref(
        db_session, tenant_id=tenant.id, browser_session_id=session.id,
        session_context=context, candidate_ref=1,
    )
    assert resolved == ResultSetResolutionFailure.NOT_FOUND
    del foreign_result_set


async def test_cross_session_result_set_fails_closed(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()

    conversation_a, context_a, session_a = await _new_conversation(
        db_session, tenant, user, membership
    )
    conversation_b, context_b, session_b = await _new_conversation(
        db_session, tenant, user, membership
    )
    await seed_active_result_set(
        db_session, tenant_id=tenant.id, browser_session_id=session_a.id,
        session_context=context_a, candidate_ids=[candidate.id],
    )
    await db_session.commit()
    # Tamper conversation_b (a DIFFERENT browser session, same tenant) to
    # point at conversation_a's own result set.
    context_b.active_result_set_id = context_a.active_result_set_id
    await db_session.flush()

    resolved = await resolve_active_candidate_ref(
        db_session, tenant_id=tenant.id, browser_session_id=session_b.id,
        session_context=context_b, candidate_ref=1,
    )
    assert resolved == ResultSetResolutionFailure.SESSION_MISMATCH


async def test_context_epoch_advance_invalidates_old_result_set_even_if_repointed(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()
    conversation, context, session = await _new_conversation(db_session, tenant, user, membership)
    result_set = await seed_active_result_set(
        db_session, tenant_id=tenant.id, browser_session_id=session.id,
        session_context=context, candidate_ids=[candidate.id],
    )
    await db_session.commit()

    context.context_epoch += 1
    context.active_result_set_id = None
    await db_session.commit()

    # Tamper: manually re-point active_result_set_id back at the
    # previous-epoch result set (its own context_epoch is now stale).
    context.active_result_set_id = result_set.id
    await db_session.flush()

    resolved = await resolve_active_candidate_ref(
        db_session, tenant_id=tenant.id, browser_session_id=session.id,
        session_context=context, candidate_ref=1,
    )
    assert resolved == ResultSetResolutionFailure.CONTEXT_EPOCH_MISMATCH


async def test_new_search_under_new_epoch_resolves_normally(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()
    conversation, context, session = await _new_conversation(db_session, tenant, user, membership)
    await seed_active_result_set(
        db_session, tenant_id=tenant.id, browser_session_id=session.id,
        session_context=context, candidate_ids=[candidate.id],
    )
    await db_session.commit()
    old_epoch = context.context_epoch

    context.context_epoch += 1
    context.active_result_set_id = None
    await db_session.commit()

    new_result_set = await seed_active_result_set(
        db_session, tenant_id=tenant.id, browser_session_id=session.id,
        session_context=context, candidate_ids=[candidate.id],
    )
    await db_session.commit()
    resolved = await resolve_active_candidate_ref(
        db_session, tenant_id=tenant.id, browser_session_id=session.id,
        session_context=context, candidate_ref=1,
    )
    assert not isinstance(resolved, ResultSetResolutionFailure)
    assert resolved.result_set_id == new_result_set.id
    assert resolved.context_epoch == old_epoch + 1


# --- Staleness / expiry --------------------------------------------------


async def test_profile_reprocessing_makes_ordinal_stale(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    candidate, _pv1 = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()
    conversation, context, session = await _new_conversation(db_session, tenant, user, membership)
    await seed_active_result_set(
        db_session, tenant_id=tenant.id, browser_session_id=session.id,
        session_context=context, candidate_ids=[candidate.id],
    )
    await db_session.commit()

    # Simulate re-processing: a NEW current CandidateProfileVersion for the
    # same candidate.
    await seed_next_profile_version(
        db_session,
        tenant_id=tenant.id,
        candidate=candidate,
        profile_content=_profile("Python", "SQL"),
    )
    await db_session.commit()

    resolved = await resolve_active_candidate_ref(
        db_session, tenant_id=tenant.id, browser_session_id=session.id,
        session_context=context, candidate_ref=1,
    )
    assert resolved == ResultSetResolutionFailure.STALE


async def test_new_searchable_candidate_makes_ordinal_stale(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()
    conversation, context, session = await _new_conversation(db_session, tenant, user, membership)
    await seed_active_result_set(
        db_session, tenant_id=tenant.id, browser_session_id=session.id,
        session_context=context, candidate_ids=[candidate.id],
    )
    await db_session.commit()

    await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Java")
    )
    await db_session.commit()

    resolved = await resolve_active_candidate_ref(
        db_session, tenant_id=tenant.id, browser_session_id=session.id,
        session_context=context, candidate_ref=1,
    )
    assert resolved == ResultSetResolutionFailure.STALE


async def test_embedding_reprocessing_makes_semantic_ordinal_stale(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    content = _profile("Python")
    candidate, pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=content
    )
    await db_session.commit()
    embedding = await seed_embedding(
        db_session, tenant_id=tenant.id, candidate_id=candidate.id,
        profile_version_id=pv.id, vector=[0.1] * 8, profile_content=content,
    )
    await db_session.commit()
    conversation, context, session = await _new_conversation(db_session, tenant, user, membership)

    embedding_config = _embedding_config()
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY,
        semantic_query="Python engineer",
        embedding_config=embedding_config,
    )
    results = [
        CandidateSearchResult(
            candidate_id=candidate.id, rank=1, mode=SearchMode.SEMANTIC_ONLY,
            relevance_score=0.8, structured_score=None, semantic_score=0.8,
            candidate_profile_version_id=pv.id, candidate_embedding_version_id=embedding.id,
            search_policy_version=SEARCH_POLICY_VERSION,
        )
    ]
    result_set = await create_result_set_from_search(
        db_session, tenant_id=tenant.id, browser_session_id=session.id,
        session_context=context, planned=_planned(request, results),
        previous_result_set_id=None,
    )
    context.active_result_set_id = result_set.id
    await db_session.commit()

    # Simulate re-processing: new profile version (same content) + a fresh
    # embedding row tied to that new version — the compatible embedding
    # version id for this candidate changes.
    new_pv = await seed_next_profile_version(
        db_session, tenant_id=tenant.id, candidate=candidate, profile_content=content
    )
    await seed_embedding(
        db_session, tenant_id=tenant.id, candidate_id=candidate.id,
        profile_version_id=new_pv.id, vector=[0.2] * 8, profile_content=content,
    )
    await db_session.commit()

    resolved = await resolve_active_candidate_ref(
        db_session, tenant_id=tenant.id, browser_session_id=session.id,
        session_context=context, candidate_ref=1,
    )
    assert resolved == ResultSetResolutionFailure.STALE


async def test_expired_result_set_is_expired_not_stale(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    candidate, pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()
    conversation, context, session = await _new_conversation(db_session, tenant, user, membership)

    request = CandidateSearchRequest(
        mode=SearchMode.STRUCTURED_ONLY, required_filters=RequiredFilters(skills=["Python"])
    )
    fingerprint = await compute_corpus_fingerprint(
        db_session, tenant_id=tenant.id, embedding_config=None
    )
    result_set = AgentResultSet(
        tenant_id=tenant.id,
        browser_session_id=session.id,
        conversation_id=context.conversation_id,
        context_epoch=context.context_epoch,
        request_sha256="a" * 64,
        canonical_search_request=request.model_dump(mode="json"),
        planner_policy_version="test", planner_prompt_version="test", planner_schema_version="test",
        planner_model_provider="test", planner_model_name="test", planner_model_revision="",
        search_policy_version="test", search_mode=SearchMode.STRUCTURED_ONLY.value,
        result_count=1, corpus_fingerprint_sha256=fingerprint,
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )
    db_session.add(result_set)
    await db_session.flush()
    db_session.add(
        AgentResultSetMember(
            result_set_id=result_set.id, ordinal=1, candidate_id=candidate.id,
            candidate_profile_version_id=pv.id, candidate_embedding_version_id=None,
            relevance_score=1.0, structured_score=None, semantic_score=None,
        )
    )
    context.active_result_set_id = result_set.id
    await db_session.commit()

    resolved = await resolve_active_candidate_ref(
        db_session, tenant_id=tenant.id, browser_session_id=session.id,
        session_context=context, candidate_ref=1,
    )
    assert resolved == ResultSetResolutionFailure.EXPIRED


# --- Audit privacy --------------------------------------------------------


async def test_result_set_audit_events_never_leak_pii_or_query_text(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    pii_quote = "John Doe john@example.com +994501234567"
    candidate, pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python", quote=pii_quote)
    )
    await db_session.commit()
    conversation, context, session = await _new_conversation(db_session, tenant, user, membership)

    secret_query = "John Doe john@example.com +994501234567 Python developer"
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY,
        semantic_query=secret_query,
        embedding_config=_embedding_config(),
    )
    embedding = await seed_embedding(
        db_session, tenant_id=tenant.id, candidate_id=candidate.id, profile_version_id=pv.id,
        vector=[0.1] * 8, profile_content=_profile("Python"),
    )
    results = [
        CandidateSearchResult(
            candidate_id=candidate.id, rank=1, mode=SearchMode.SEMANTIC_ONLY,
            relevance_score=0.8, structured_score=None, semantic_score=0.8,
            candidate_profile_version_id=pv.id, candidate_embedding_version_id=embedding.id,
            search_policy_version=SEARCH_POLICY_VERSION,
        )
    ]
    result_set = await create_result_set_from_search(
        db_session, tenant_id=tenant.id, browser_session_id=session.id,
        session_context=context, planned=_planned(request, results),
        previous_result_set_id=None,
    )
    context.active_result_set_id = result_set.id
    await db_session.commit()

    await resolve_active_candidate_ref(
        db_session, tenant_id=tenant.id, browser_session_id=session.id,
        session_context=context, candidate_ref=1,
    )
    await resolve_active_candidate_ref(
        db_session, tenant_id=tenant.id, browser_session_id=session.id,
        session_context=context, candidate_ref=5,  # triggers reference_rejected
    )
    await db_session.commit()

    events = list(
        await db_session.scalars(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenant.id,
                AuditEvent.event_type.in_(
                    [
                        "agent.result_set.created",
                        "agent.result_set.reference_resolved",
                        "agent.result_set.reference_rejected",
                    ]
                ),
            )
        )
    )
    assert events
    blob = " ".join(str(event.event_metadata) for event in events)
    assert "John Doe" not in blob
    assert "john@example.com" not in blob
    assert "+994501234567" not in blob
    assert secret_query not in blob
    assert "Python developer" not in blob


# --- No Job/Evaluation side effects ---------------------------------------


async def test_result_set_flow_never_creates_job_or_evaluation_rows(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()
    conversation, context, session = await _new_conversation(db_session, tenant, user, membership)

    from meyar.search.planner_schemas import PlannerDraft

    llm = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"])),
        agent_decisions=[
            AgentDecision(action=AgentActionType.SEARCH_CANDIDATES, search_query="Python"),
            AgentDecision(action=AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=1),
        ],
    )
    await run_agent_turn(
        db_session, llm, tenant_id=tenant.id, conversation=conversation,
            session_context=context, user_message="Python",
        as_of_date=AS_OF_DATE, embedding_config=_embedding_config(), embedding_provider=None,
        max_tool_calls=3, max_context_turns=8,
    )
    await db_session.commit()

    assert (
        await db_session.scalars(select(JobModel).where(JobModel.tenant_id == tenant.id))
    ).all() == []
    assert (
        await db_session.scalars(
            select(JobCriteriaVersion).where(JobCriteriaVersion.tenant_id == tenant.id)
        )
    ).all() == []
    assert (
        await db_session.scalars(select(Evaluation).where(Evaluation.tenant_id == tenant.id))
    ).all() == []


# --- Candidate hard-delete is never blocked by membership rows ------------


async def test_candidate_hard_delete_is_not_blocked_by_result_set_membership(
    db_session: AsyncSession, tenant_and_user, tmp_path
) -> None:
    tenant, user, _password, membership = tenant_and_user
    candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()
    conversation, context, session = await _new_conversation(db_session, tenant, user, membership)
    result_set = await seed_active_result_set(
        db_session, tenant_id=tenant.id, browser_session_id=session.id,
        session_context=context, candidate_ids=[candidate.id],
    )
    await db_session.commit()

    storage = LocalFilesystemStorage(root=str(tmp_path / "storage"))
    photo_storage = LocalPhotoStorage(root=str(tmp_path / "storage"))
    deleted_count = await delete_candidate_cascade(
        db_session, storage, tenant_id=tenant.id, candidate_id=candidate.id,
        photo_storage=photo_storage, actor_id=uuid.uuid4(),
    )
    assert deleted_count is not None

    remaining_members = await _member_rows(db_session, result_set_id=result_set.id)
    assert len(remaining_members) == 1
    assert remaining_members[0].candidate_id == candidate.id


# --- Concurrency -----------------------------------------------------------


async def test_concurrent_turns_never_lose_a_context_epoch_update(
    tenant_and_user, db_session
) -> None:
    """Two concurrent 'requests' against the SAME durable conversation, each
    taking the conversation row's SELECT ... FOR UPDATE lock
    (get_owned_conversation_for_update, issue #80) before mutating its
    transcript and live context, must serialize — the final state reflects
    BOTH updates, never a lost update."""
    tenant, user, _password, membership = tenant_and_user
    await db_session.commit()
    session, _raw = await create_browser_session(
        db_session, user_id=user.id, tenant_membership_id=membership.id, ttl_hours=8
    )
    await db_session.commit()
    conversation, context = await open_test_conversation(
        db_session,
        tenant_id=tenant.id,
        user_id=user.id,
        membership_id=membership.id,
        browser_session_id=session.id,
    )
    await db_session.commit()

    engine_a = create_async_engine(TEST_DATABASE_URL)
    engine_b = create_async_engine(TEST_DATABASE_URL)
    factory_a = async_sessionmaker(engine_a, expire_on_commit=False)
    factory_b = async_sessionmaker(engine_b, expire_on_commit=False)

    async def bump(factory) -> None:
        async with factory() as db:
            owner = OwnerPrincipal(
                tenant_id=tenant.id, user_id=user.id, membership_id=membership.id
            )
            conv = await get_owned_conversation_for_update(
                db, owner=owner, conversation_id=conversation.id
            )
            assert conv is not None
            live = await get_session_context_for_update(
                db, conversation=conv, browser_session_id=session.id
            )
            assert live is not None
            await asyncio.sleep(0.05)
            conv.turns = [*conv.turns, {"role": "user", "text": "synthetic"}]
            live.context_epoch += 1
            await db.commit()

    try:
        await asyncio.gather(bump(factory_a), bump(factory_b))
    finally:
        await engine_a.dispose()
        await engine_b.dispose()

    await db_session.refresh(conversation)
    await db_session.refresh(context)
    assert context.context_epoch == 3  # started at 1, two serialized +1 bumps
    assert len(conversation.turns) == 2


# --- Identity boundary ------------------------------------------------


def test_agent_result_set_modules_never_import_candidate_identity() -> None:
    """Docstrings are allowed to explain the exclusion (and do); no actual
    import/usage of a CandidateIdentity-shaped symbol is allowed — checked
    both by the module's own live namespace (no such attribute bound) and
    by scanning for an ``import`` line naming it, source-line by line."""
    from meyar.models import agent_result_set as model_module
    from meyar.services import agent_result_set_repo as repo_module

    for module in (model_module, repo_module):
        assert not any("CandidateIdentity" in name for name in dir(module))
        source_path = module.__file__
        assert source_path is not None
        with open(source_path, encoding="utf-8") as handle:
            import_lines = [
                line for line in handle if line.lstrip().startswith(("import ", "from "))
            ]
        assert not any("CandidateIdentity" in line for line in import_lines)
