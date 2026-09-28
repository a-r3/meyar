"""Conversational result-set refinement (issue #49 PR49-2) — the
REFINE_CANDIDATE_RESULTS agent action. Covers: filter/limit/filter+limit
semantics, outsider exclusion (derived members are always a subset of the
active result set's own members), preserved ordering + dense ordinals,
chained refinement, zero-result and over-large-limit handling, new-search
vs. refinement context switching, reload/reset invalidation, stale/
expired/cross-tenant/cross-session/cross-epoch fail-closed behavior,
prohibited-attribute rejection without context mutation, provenance
copy-forward from the parent, and audit privacy. Real route/service-level
same-session concurrency serialization lives in test_ui_agent_routes.py
(see test_real_ui_agent_route_serializes_concurrent_same_session_refinements),
matching the PR49-1 precedent."""

import uuid
from datetime import UTC, datetime, timedelta

from fakes import FakeLLMProvider
from pydantic import ValidationError
from search_helpers import (
    seed_active_result_set,
    seed_candidate_with_profile,
    seed_next_profile_version,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.agent.schemas import AgentActionType, AgentDecision, AgentResponseCode
from meyar.agent.service import run_agent_turn
from meyar.models.agent_result_set import AgentResultSet, AgentResultSetKind, AgentResultSetMember
from meyar.models.audit_event import AuditEvent
from meyar.models.evaluation import Evaluation
from meyar.models.job import Job as JobModel
from meyar.models.job_criteria_version import JobCriteriaVersion
from meyar.search.schemas import EmbeddingSearchConfig
from meyar.services.agent_conversation_repo import (
    get_conversation_by_session,
    get_or_create_conversation,
    reset_conversation,
)
from meyar.services.browser_session_repo import create_browser_session

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


async def _new_conversation(db_session, tenant, user, membership):
    session, _raw = await create_browser_session(
        db_session, user_id=user.id, tenant_membership_id=membership.id, ttl_hours=8
    )
    await db_session.flush()
    conversation = await get_or_create_conversation(
        db_session, tenant_id=tenant.id, browser_session_id=session.id
    )
    return conversation, session


async def _refine(
    db_session,
    llm,
    *,
    tenant_id,
    conversation,
    filter_query: str | None = None,
    limit: int | None = None,
    message: str = "refine",
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
        max_tool_calls=3,
        max_context_turns=8,
    )


def _refine_llm(*, filter_query: str | None = None, limit: int | None = None) -> FakeLLMProvider:
    return FakeLLMProvider(
        agent_decision=AgentDecision(
            action=AgentActionType.REFINE_CANDIDATE_RESULTS,
            filter_query=filter_query,
            limit=limit,
        )
    )


def _fake_structured_plan(
    *,
    required_skills: list[str] | None = None,
    preferred_skills: list[str] | None = None,
    result_limit: int | None = None,
    used_default_limit: bool = True,
    request_sha256: str = "f" * 64,
):
    """Builds a monkeypatch replacement for meyar.agent.service.
    plan_candidate_search that returns a fixed, already-EXECUTABLE
    STRUCTURED_ONLY SearchPlanResult with a controlled interpretation
    summary — isolates _dispatch_refine's own preferred-filter/limit-
    reconciliation policy from the real planner's own NL interpretation,
    mirroring test_semantic_or_hybrid_filter_plan_is_rejected_not_downgraded."""
    from meyar.search.planner_schemas import (
        PLANNER_SCHEMA_VERSION,
        PlanInterpretationSummary,
        PlannerOutcome,
        SearchPlanResult,
    )
    from meyar.search.schemas import (
        CandidateSearchRequest,
        PreferredFilters,
        RequiredFilters,
        SearchMode,
    )

    async def _fake_plan(*_args, **_kwargs):
        request = CandidateSearchRequest(
            mode=SearchMode.STRUCTURED_ONLY,
            required_filters=RequiredFilters(skills=required_skills or []),
            preferred_filters=PreferredFilters(skills=preferred_skills or []),
        )
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
            interpretation=PlanInterpretationSummary(
                result_limit=result_limit, used_default_limit=used_default_limit
            ),
        )

    return _fake_plan


async def _member_candidate_ids(db_session: AsyncSession, *, result_set_id: uuid.UUID) -> list[str]:
    rows = (
        await db_session.execute(
            select(AgentResultSetMember)
            .where(AgentResultSetMember.result_set_id == result_set_id)
            .order_by(AgentResultSetMember.ordinal.asc())
        )
    ).scalars()
    return [str(row.candidate_id) for row in rows]


async def _seed_root(db_session, tenant, conversation, session, *candidate_skill_pairs):
    """Seeds candidates with the given (skills...) tuples, then builds a
    root SEARCH-kind AgentResultSet over them in the given order via the
    existing seed_active_result_set test shortcut. Returns (candidates,
    root_result_set)."""
    candidates = []
    for skills in candidate_skill_pairs:
        candidate, _pv = await seed_candidate_with_profile(
            db_session, tenant_id=tenant.id, profile_content=_profile(*skills)
        )
        candidates.append(candidate)
    await db_session.commit()
    root = await seed_active_result_set(
        db_session,
        tenant_id=tenant.id,
        browser_session_id=session.id,
        conversation=conversation,
        candidate_ids=[c.id for c in candidates],
    )
    await db_session.commit()
    return candidates, root


# --- Core semantics: filter / limit / filter+limit -------------------------


async def test_filter_only_keeps_matching_members_in_preserved_order(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    conversation, session = await _new_conversation(db_session, tenant, user, membership)
    (a, b, c), root = await _seed_root(
        db_session, tenant, conversation, session, ("Python",), ("SQL",), ("SQL",)
    )

    result = await _refine(
        db_session,
        _refine_llm(filter_query="SQL bilən namizədləri göstər"),
        tenant_id=tenant.id,
        conversation=conversation,
    )
    assert result.outcome.value == "ANSWERED_FROM_TOOL_RESULT"
    refine = result.tool_results[0].refine
    assert refine is not None
    assert refine.source_result_count == 3
    assert refine.has_filter is True
    assert [str(r.candidate_id) for r in refine.response.results] == [str(b.id), str(c.id)]
    assert [r.rank for r in refine.response.results] == [1, 2]

    await db_session.refresh(conversation)
    derived = await db_session.get(AgentResultSet, conversation.active_result_set_id)
    assert derived is not None
    assert derived.id != root.id
    assert derived.result_set_kind == AgentResultSetKind.REFINEMENT.value
    assert derived.parent_result_set_id == root.id
    assert derived.result_count == 2
    assert await _member_candidate_ids(db_session, result_set_id=derived.id) == [
        str(b.id),
        str(c.id),
    ]


async def test_filter_never_admits_a_candidate_outside_the_parent_set(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """D also knows SQL but was never part of the active result set — it
    must never enter the derived set (derived members ⊆ parent members)."""
    tenant, user, _password, membership = tenant_and_user
    conversation, session = await _new_conversation(db_session, tenant, user, membership)
    # The outsider must already exist in the tenant's searchable corpus
    # BEFORE the root result set is built, so the root's own corpus
    # fingerprint already reflects it — only its ABSENCE from root
    # membership is under test here, not a corpus-drift staleness case.
    outsider, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("SQL")
    )
    await db_session.commit()
    (a, b), root = await _seed_root(
        db_session, tenant, conversation, session, ("Python",), ("SQL",)
    )

    result = await _refine(
        db_session,
        _refine_llm(filter_query="SQL bilən namizədləri göstər"),
        tenant_id=tenant.id,
        conversation=conversation,
    )
    refine = result.tool_results[0].refine
    assert refine is not None
    ids = [str(r.candidate_id) for r in refine.response.results]
    assert ids == [str(b.id)]
    assert str(outsider.id) not in ids


async def test_limit_only_takes_first_n_preserving_order(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    conversation, session = await _new_conversation(db_session, tenant, user, membership)
    candidates, root = await _seed_root(
        db_session,
        tenant,
        conversation,
        session,
        ("Python",),
        ("Java",),
        ("Go",),
        ("Rust",),
        ("Ruby",),
    )

    result = await _refine(
        db_session, _refine_llm(limit=3), tenant_id=tenant.id, conversation=conversation
    )
    refine = result.tool_results[0].refine
    assert refine is not None
    assert refine.has_filter is False
    assert refine.limit_truncated is False
    ids = [str(r.candidate_id) for r in refine.response.results]
    assert ids == [str(c.id) for c in candidates[:3]]
    assert [r.rank for r in refine.response.results] == [1, 2, 3]


async def test_filter_then_limit_preserves_filtered_order_before_truncating(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    conversation, session = await _new_conversation(db_session, tenant, user, membership)
    (a, b, c, d), root = await _seed_root(
        db_session, tenant, conversation, session, ("SQL",), ("SQL",), ("SQL",), ("Python",)
    )

    result = await _refine(
        db_session,
        _refine_llm(filter_query="SQL bilən namizədləri göstər", limit=2),
        tenant_id=tenant.id,
        conversation=conversation,
    )
    refine = result.tool_results[0].refine
    assert refine is not None
    ids = [str(r.candidate_id) for r in refine.response.results]
    assert ids == [str(a.id), str(b.id)]


async def test_zero_result_refinement_is_not_an_error_and_becomes_active(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    conversation, session = await _new_conversation(db_session, tenant, user, membership)
    _candidates, root = await _seed_root(
        db_session, tenant, conversation, session, ("Python",), ("Java",)
    )

    result = await _refine(
        db_session,
        _refine_llm(filter_query="SQL bilən namizədləri göstər"),
        tenant_id=tenant.id,
        conversation=conversation,
    )
    assert result.outcome.value == "ANSWERED_FROM_TOOL_RESULT"
    refine = result.tool_results[0].refine
    assert refine is not None
    assert refine.response.result_count == 0

    await db_session.refresh(conversation)
    derived = await db_session.get(AgentResultSet, conversation.active_result_set_id)
    assert derived is not None
    assert derived.id != root.id
    assert derived.result_count == 0

    # A later ordinal reference against the empty active set fails safely.
    followup = await _refine(
        db_session,
        FakeLLMProvider(
            agent_decision=AgentDecision(
                action=AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=1
            )
        ),
        tenant_id=tenant.id,
        conversation=conversation,
        message="birincini aç",
    )
    assert followup.outcome.value == "CANDIDATE_REF_NOT_FOUND"


async def test_requested_limit_greater_than_current_count_returns_truthful_subset(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    conversation, session = await _new_conversation(db_session, tenant, user, membership)
    (a, b), root = await _seed_root(db_session, tenant, conversation, session, ("A",), ("B",))

    result = await _refine(
        db_session, _refine_llm(limit=10), tenant_id=tenant.id, conversation=conversation
    )
    refine = result.tool_results[0].refine
    assert refine is not None
    assert refine.requested_limit == 10
    assert refine.limit_truncated is True
    ids = [str(r.candidate_id) for r in refine.response.results]
    assert ids == [str(a.id), str(b.id)]


async def test_chained_refinement_ordinal_resolves_against_final_derived_set(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    conversation, session = await _new_conversation(db_session, tenant, user, membership)
    (a, b, c, d, e), root = await _seed_root(
        db_session,
        tenant,
        conversation,
        session,
        ("SQL",),
        ("SQL",),
        ("SQL",),
        ("Python",),
        ("SQL",),
    )

    filtered = await _refine(
        db_session,
        _refine_llm(filter_query="SQL bilən namizədləri göstər"),
        tenant_id=tenant.id,
        conversation=conversation,
    )
    assert [str(r.candidate_id) for r in filtered.tool_results[0].refine.response.results] == [
        str(a.id),
        str(b.id),
        str(c.id),
        str(e.id),
    ]
    await db_session.refresh(conversation)
    derived_1_id = conversation.active_result_set_id

    limited = await _refine(
        db_session, _refine_llm(limit=2), tenant_id=tenant.id, conversation=conversation
    )
    assert [str(r.candidate_id) for r in limited.tool_results[0].refine.response.results] == [
        str(a.id),
        str(b.id),
    ]
    await db_session.refresh(conversation)
    derived_2 = await db_session.get(AgentResultSet, conversation.active_result_set_id)
    assert derived_2 is not None
    assert derived_2.parent_result_set_id == derived_1_id
    assert derived_2.id != derived_1_id

    ordinal_result = await _refine(
        db_session,
        FakeLLMProvider(
            agent_decision=AgentDecision(
                action=AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=2
            )
        ),
        tenant_id=tenant.id,
        conversation=conversation,
        message="ikinci namizəd",
    )
    assert ordinal_result.tool_results[0].profile.candidate_id == b.id


# --- Context switching: refinement vs. independent new search --------------


async def test_new_independent_search_after_refinement_starts_new_root(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    conversation, session = await _new_conversation(db_session, tenant, user, membership)
    _candidates, root = await _seed_root(
        db_session, tenant, conversation, session, ("SQL",), ("Python",)
    )
    await _refine(
        db_session, _refine_llm(limit=1), tenant_id=tenant.id, conversation=conversation
    )
    await db_session.refresh(conversation)
    derived_id = conversation.active_result_set_id
    assert derived_id != root.id

    java_candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Java")
    )
    await db_session.commit()

    from meyar.search.planner_schemas import PlannerDraft
    from meyar.search.schemas import RequiredFilters

    search_llm = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Java"])),
        agent_decisions=[
            AgentDecision(
                action=AgentActionType.SEARCH_CANDIDATES,
                search_query="Java bilən namizədləri göstər",
            ),
            AgentDecision(
                action=AgentActionType.FINAL_ANSWER, response_code=AgentResponseCode.ACKNOWLEDGEMENT
            ),
        ],
    )
    await _refine(
        db_session,
        search_llm,
        tenant_id=tenant.id,
        conversation=conversation,
        message="Java bilən namizədləri göstər",
    )
    await db_session.refresh(conversation)
    new_root = await db_session.get(AgentResultSet, conversation.active_result_set_id)
    assert new_root is not None
    assert new_root.id not in (root.id, derived_id)
    assert new_root.result_set_kind == AgentResultSetKind.SEARCH.value
    assert new_root.parent_result_set_id is None


async def test_refined_context_survives_conversation_reload(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    conversation, session = await _new_conversation(db_session, tenant, user, membership)
    (a, b), root = await _seed_root(db_session, tenant, conversation, session, ("A",), ("B",))
    await _refine(
        db_session, _refine_llm(limit=1), tenant_id=tenant.id, conversation=conversation
    )
    await db_session.commit()

    reloaded = await get_conversation_by_session(
        db_session, tenant_id=tenant.id, browser_session_id=session.id
    )
    assert reloaded is not None
    assert reloaded.active_result_set_id != root.id

    result = await _refine(
        db_session,
        FakeLLMProvider(
            agent_decision=AgentDecision(
                action=AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=1
            )
        ),
        tenant_id=tenant.id,
        conversation=reloaded,
        message="birinci namizəd",
    )
    assert result.tool_results[0].profile.candidate_id == a.id


async def test_new_conversation_reset_invalidates_refined_context(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    conversation, session = await _new_conversation(db_session, tenant, user, membership)
    (a, b), root = await _seed_root(db_session, tenant, conversation, session, ("A",), ("B",))
    await _refine(
        db_session, _refine_llm(limit=1), tenant_id=tenant.id, conversation=conversation
    )
    await db_session.commit()
    await db_session.refresh(conversation)
    old_active = conversation.active_result_set_id
    assert old_active is not None

    await reset_conversation(db_session, conversation)
    await db_session.commit()
    assert conversation.active_result_set_id is None

    # Manually repoint at the (still otherwise valid) old refined set —
    # this must still fail closed under the new epoch.
    conversation.active_result_set_id = old_active
    await db_session.flush()
    result = await _refine(
        db_session,
        FakeLLMProvider(
            agent_decision=AgentDecision(
                action=AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=1
            )
        ),
        tenant_id=tenant.id,
        conversation=conversation,
        message="birinci namizəd",
    )
    assert result.outcome.value == "CANDIDATE_REF_NOT_FOUND"


# --- Staleness / expiry / isolation -----------------------------------------


async def test_stale_source_blocks_refinement_and_leaves_context_untouched(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    conversation, session = await _new_conversation(db_session, tenant, user, membership)
    (candidate,), root = await _seed_root(db_session, tenant, conversation, session, ("Python",))

    # Reprocessing the candidate's profile drifts the corpus fingerprint.
    await seed_next_profile_version(
        db_session,
        tenant_id=tenant.id,
        candidate=candidate,
        profile_content=_profile("Python", "SQL"),
    )
    await db_session.commit()

    result = await _refine(
        db_session, _refine_llm(limit=1), tenant_id=tenant.id, conversation=conversation
    )
    assert result.outcome.value == "RESULT_SET_STALE"
    await db_session.refresh(conversation)
    assert conversation.active_result_set_id == root.id


async def test_expired_source_blocks_refinement(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    conversation, session = await _new_conversation(db_session, tenant, user, membership)
    (_candidate,), root = await _seed_root(db_session, tenant, conversation, session, ("Python",))

    root.expires_at = datetime.now(UTC) - timedelta(hours=1)
    await db_session.flush()
    await db_session.commit()

    result = await _refine(
        db_session, _refine_llm(limit=1), tenant_id=tenant.id, conversation=conversation
    )
    assert result.outcome.value == "RESULT_SET_EXPIRED"
    await db_session.refresh(conversation)
    assert conversation.active_result_set_id == root.id


async def test_no_active_result_set_returns_clarification_not_a_crash(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    conversation, _session = await _new_conversation(db_session, tenant, user, membership)

    result = await _refine(
        db_session, _refine_llm(limit=3), tenant_id=tenant.id, conversation=conversation
    )
    assert result.outcome.value == "CLARIFICATION_REQUESTED"
    assert result.message == (
        "Əvvəlcə namizəd axtarışı aparın, sonra cari nəticələr üzərində əməliyyat "
        "edə bilərsiniz (məsələn, \"ilk üçü\" və ya \"bunlardan SQL bilənlər\")."
    )
    assert result.tool_results == []


async def test_cross_tenant_result_set_pointer_fails_closed(
    db_session: AsyncSession, tenant_and_user
) -> None:
    from meyar.core.roles import ROLE_HR_USER
    from meyar.services.tenant_membership_repo import create_membership
    from meyar.services.tenant_repo import create_tenant
    from meyar.services.user_repo import create_user

    tenant, user, _password, membership = tenant_and_user
    conversation, session = await _new_conversation(db_session, tenant, user, membership)
    (_candidate,), root = await _seed_root(db_session, tenant, conversation, session, ("Python",))

    other_tenant = await create_tenant(db_session, name=f"Other-{uuid.uuid4().hex[:8]}")
    other_user = await create_user(
        db_session,
        username=f"hr-{uuid.uuid4().hex[:8]}",
        plaintext_password="correct-horse-battery-1",
    )
    other_membership = await create_membership(
        db_session, user_id=other_user.id, tenant_id=other_tenant.id, role=ROLE_HR_USER
    )
    await db_session.commit()
    other_conversation, _other_session = await _new_conversation(
        db_session, other_tenant, other_user, other_membership
    )
    # Tamper: point the OTHER tenant's conversation at this tenant's result set.
    other_conversation.active_result_set_id = root.id
    other_conversation.context_epoch = conversation.context_epoch
    await db_session.flush()

    result = await _refine(
        db_session,
        _refine_llm(limit=1),
        tenant_id=other_tenant.id,
        conversation=other_conversation,
    )
    assert result.outcome.value == "CLARIFICATION_REQUESTED"
    assert result.message is not None and "Əvvəlcə namizəd axtarışı" in result.message
    assert other_conversation.active_result_set_id == root.id  # pointer left untouched, unresolved


async def test_cross_session_result_set_pointer_fails_closed(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    conversation_a, session_a = await _new_conversation(db_session, tenant, user, membership)
    (_candidate,), root = await _seed_root(
        db_session, tenant, conversation_a, session_a, ("Python",)
    )

    conversation_b, _session_b = await _new_conversation(db_session, tenant, user, membership)
    conversation_b.active_result_set_id = root.id
    conversation_b.context_epoch = conversation_a.context_epoch
    await db_session.flush()

    result = await _refine(
        db_session, _refine_llm(limit=1), tenant_id=tenant.id, conversation=conversation_b
    )
    assert result.outcome.value == "CLARIFICATION_REQUESTED"


async def test_old_context_epoch_result_set_pointer_fails_closed(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    conversation, session = await _new_conversation(db_session, tenant, user, membership)
    (_candidate,), root = await _seed_root(db_session, tenant, conversation, session, ("Python",))
    await db_session.commit()

    await reset_conversation(db_session, conversation)
    conversation.active_result_set_id = root.id  # tampered repoint under the NEW epoch
    await db_session.flush()
    await db_session.commit()

    result = await _refine(
        db_session, _refine_llm(limit=1), tenant_id=tenant.id, conversation=conversation
    )
    assert result.outcome.value == "CLARIFICATION_REQUESTED"


# --- Prohibited attribute / no silent weakening -----------------------------


async def test_prohibited_attribute_filter_rejected_without_mutating_context(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    conversation, session = await _new_conversation(db_session, tenant, user, membership)
    _candidates, root = await _seed_root(
        db_session, tenant, conversation, session, ("Python",), ("SQL",)
    )

    result = await _refine(
        db_session,
        _refine_llm(filter_query="qadın namizədləri göstər"),
        tenant_id=tenant.id,
        conversation=conversation,
    )
    assert result.outcome.value == "CLARIFICATION_REQUESTED"
    assert result.tool_results == []
    await db_session.refresh(conversation)
    assert conversation.active_result_set_id == root.id

    # No derived result set was ever persisted for the rejected attempt.
    count = await db_session.scalar(
        select(func.count()).select_from(AgentResultSet).where(
            AgentResultSet.tenant_id == tenant.id,
            AgentResultSet.result_set_kind == AgentResultSetKind.REFINEMENT.value,
        )
    )
    assert count == 0


async def test_semantic_or_hybrid_filter_plan_is_rejected_not_downgraded(
    db_session: AsyncSession, tenant_and_user, monkeypatch
) -> None:
    """Defense in depth: even though today's agent NL planner only ever
    produces STRUCTURED_ONLY plans (see docs/DECISIONS.md scope note), a
    future/foreign SEMANTIC_ONLY/HYBRID plan must still be rejected rather
    than silently executed or downgraded — see meyar.agent.service.
    _dispatch_refine."""
    import meyar.agent.service as agent_service
    from meyar.search.planner_schemas import (
        PLANNER_SCHEMA_VERSION,
        PlannerOutcome,
        SearchPlanResult,
    )
    from meyar.search.schemas import CandidateSearchRequest, SearchMode

    tenant, user, _password, membership = tenant_and_user
    conversation, session = await _new_conversation(db_session, tenant, user, membership)
    _candidates, root = await _seed_root(
        db_session, tenant, conversation, session, ("Python",), ("SQL",)
    )

    async def _fake_plan(*_args, **_kwargs):
        request = CandidateSearchRequest(
            mode=SearchMode.SEMANTIC_ONLY,
            semantic_query="engineers",
            embedding_config=_embedding_config(),
        )
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
            request_sha256="a" * 64,
        )

    monkeypatch.setattr(agent_service, "plan_candidate_search", _fake_plan)

    result = await _refine(
        db_session,
        _refine_llm(filter_query="engineers"),
        tenant_id=tenant.id,
        conversation=conversation,
    )
    assert result.outcome.value == "CLARIFICATION_REQUESTED"
    await db_session.refresh(conversation)
    assert conversation.active_result_set_id == root.id


# --- Preferred filters must never be silently applied -----------------------


async def test_preferred_only_filter_plan_is_rejected_not_partially_executed(
    db_session: AsyncSession, tenant_and_user, monkeypatch
) -> None:
    """A STRUCTURED_ONLY plan may legitimately carry preferred filters —
    current-result refinement supports required (hard) filters only, never
    reranking, so ANY populated preferred_filters must reject the WHOLE
    refinement rather than silently retain the parent's own members
    unfiltered (see meyar.agent.service._dispatch_refine)."""
    import meyar.agent.service as agent_service

    tenant, user, _password, membership = tenant_and_user
    conversation, session = await _new_conversation(db_session, tenant, user, membership)
    _candidates, root = await _seed_root(
        db_session, tenant, conversation, session, ("Python",), ("SQL",), ("Go",)
    )

    monkeypatch.setattr(
        agent_service,
        "plan_candidate_search",
        _fake_structured_plan(preferred_skills=["SQL"]),
    )

    result = await _refine(
        db_session,
        _refine_llm(filter_query="SQL üstünlük təşkil edir"),
        tenant_id=tenant.id,
        conversation=conversation,
    )
    assert result.outcome.value == "CLARIFICATION_REQUESTED"
    assert result.message == (
        "Bu sorğu mövcud MEYAR alətləri ilə təhlükəsiz şəkildə icra edilmir."
    )
    assert result.tool_results == []
    await db_session.refresh(conversation)
    assert conversation.active_result_set_id == root.id

    count = await db_session.scalar(
        select(func.count()).select_from(AgentResultSet).where(
            AgentResultSet.tenant_id == tenant.id,
            AgentResultSet.result_set_kind == AgentResultSetKind.REFINEMENT.value,
        )
    )
    assert count == 0


async def test_required_and_preferred_filter_plan_rejects_whole_refinement(
    db_session: AsyncSession, tenant_and_user, monkeypatch
) -> None:
    """A plan combining a required filter with a preferred filter must be
    rejected in full — never partially executed on the required portion
    alone."""
    import meyar.agent.service as agent_service

    tenant, user, _password, membership = tenant_and_user
    conversation, session = await _new_conversation(db_session, tenant, user, membership)
    _candidates, root = await _seed_root(
        db_session, tenant, conversation, session, ("Python",), ("Python",), ("SQL",)
    )

    monkeypatch.setattr(
        agent_service,
        "plan_candidate_search",
        _fake_structured_plan(required_skills=["Python"], preferred_skills=["SQL"]),
    )

    result = await _refine(
        db_session,
        _refine_llm(filter_query="Python bilən, SQL üstünlük təşkil edir"),
        tenant_id=tenant.id,
        conversation=conversation,
    )
    assert result.outcome.value == "CLARIFICATION_REQUESTED"
    assert result.tool_results == []
    await db_session.refresh(conversation)
    assert conversation.active_result_set_id == root.id

    count = await db_session.scalar(
        select(func.count()).select_from(AgentResultSet).where(
            AgentResultSet.tenant_id == tenant.id,
            AgentResultSet.result_set_kind == AgentResultSetKind.REFINEMENT.value,
        )
    )
    assert count == 0


# --- Active-result-set prevalidation happens BEFORE the planner is called --


async def test_stale_filter_refinement_prevalidates_before_planner_invocation(
    db_session: AsyncSession, tenant_and_user, monkeypatch
) -> None:
    import meyar.agent.service as agent_service

    tenant, user, _password, membership = tenant_and_user
    conversation, session = await _new_conversation(db_session, tenant, user, membership)
    (candidate,), root = await _seed_root(db_session, tenant, conversation, session, ("Python",))

    # Reprocessing the candidate's profile drifts the corpus fingerprint,
    # the same trigger test_stale_source_blocks_refinement_and_leaves_
    # context_untouched uses for a limit-only refinement.
    await seed_next_profile_version(
        db_session,
        tenant_id=tenant.id,
        candidate=candidate,
        profile_content=_profile("Python", "SQL"),
    )
    await db_session.commit()

    async def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("planner must not be called when the active result set is stale")

    monkeypatch.setattr(agent_service, "plan_candidate_search", _fail_if_called)

    result = await _refine(
        db_session,
        _refine_llm(filter_query="SQL bilən namizədləri göstər"),
        tenant_id=tenant.id,
        conversation=conversation,
    )
    assert result.outcome.value == "RESULT_SET_STALE"
    await db_session.refresh(conversation)
    assert conversation.active_result_set_id == root.id


async def test_expired_filter_refinement_prevalidates_before_planner_invocation(
    db_session: AsyncSession, tenant_and_user, monkeypatch
) -> None:
    import meyar.agent.service as agent_service

    tenant, user, _password, membership = tenant_and_user
    conversation, session = await _new_conversation(db_session, tenant, user, membership)
    (_candidate,), root = await _seed_root(db_session, tenant, conversation, session, ("Python",))

    root.expires_at = datetime.now(UTC) - timedelta(hours=1)
    await db_session.flush()
    await db_session.commit()

    async def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("planner must not be called when the active result set is expired")

    monkeypatch.setattr(agent_service, "plan_candidate_search", _fail_if_called)

    result = await _refine(
        db_session,
        _refine_llm(filter_query="SQL bilən namizədləri göstər"),
        tenant_id=tenant.id,
        conversation=conversation,
    )
    assert result.outcome.value == "RESULT_SET_EXPIRED"
    await db_session.refresh(conversation)
    assert conversation.active_result_set_id == root.id


async def test_missing_context_filter_refinement_never_invokes_planner(
    db_session: AsyncSession, tenant_and_user, monkeypatch
) -> None:
    import meyar.agent.service as agent_service

    tenant, user, _password, membership = tenant_and_user
    conversation, _session = await _new_conversation(db_session, tenant, user, membership)

    async def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("planner must not be called with no active result set")

    monkeypatch.setattr(agent_service, "plan_candidate_search", _fail_if_called)

    result = await _refine(
        db_session,
        _refine_llm(filter_query="SQL bilən namizədləri göstər"),
        tenant_id=tenant.id,
        conversation=conversation,
    )
    assert result.outcome.value == "CLARIFICATION_REQUESTED"
    assert result.tool_results == []


# --- Effective-limit reconciliation between the planner and AgentDecision --


async def test_planner_explicit_limit_recovered_when_agent_decision_omits_it(
    db_session: AsyncSession, tenant_and_user, monkeypatch
) -> None:
    import meyar.agent.service as agent_service

    tenant, user, _password, membership = tenant_and_user
    conversation, session = await _new_conversation(db_session, tenant, user, membership)
    (a, b, c, _d), root = await _seed_root(
        db_session, tenant, conversation, session, ("SQL",), ("SQL",), ("SQL",), ("SQL",)
    )

    monkeypatch.setattr(
        agent_service,
        "plan_candidate_search",
        _fake_structured_plan(required_skills=["SQL"], result_limit=3, used_default_limit=False),
    )

    result = await _refine(
        db_session,
        _refine_llm(filter_query="ilk 3 SQL bilən namizədləri göstər"),
        tenant_id=tenant.id,
        conversation=conversation,
    )
    assert result.outcome.value == "ANSWERED_FROM_TOOL_RESULT"
    refine = result.tool_results[0].refine
    assert refine is not None
    assert refine.requested_limit == 3
    assert refine.limit_truncated is False
    ids = [str(r.candidate_id) for r in refine.response.results]
    assert ids == [str(a.id), str(b.id), str(c.id)]


async def test_matching_planner_and_agent_explicit_limit_succeeds(
    db_session: AsyncSession, tenant_and_user, monkeypatch
) -> None:
    import meyar.agent.service as agent_service

    tenant, user, _password, membership = tenant_and_user
    conversation, session = await _new_conversation(db_session, tenant, user, membership)
    (a, b, c, _d), root = await _seed_root(
        db_session, tenant, conversation, session, ("SQL",), ("SQL",), ("SQL",), ("SQL",)
    )

    monkeypatch.setattr(
        agent_service,
        "plan_candidate_search",
        _fake_structured_plan(required_skills=["SQL"], result_limit=3, used_default_limit=False),
    )

    result = await _refine(
        db_session,
        _refine_llm(filter_query="ilk 3 SQL bilən namizədləri göstər", limit=3),
        tenant_id=tenant.id,
        conversation=conversation,
    )
    assert result.outcome.value == "ANSWERED_FROM_TOOL_RESULT"
    refine = result.tool_results[0].refine
    assert refine is not None
    assert refine.requested_limit == 3
    ids = [str(r.candidate_id) for r in refine.response.results]
    assert ids == [str(a.id), str(b.id), str(c.id)]


async def test_conflicting_planner_and_agent_explicit_limit_rejects_safely(
    db_session: AsyncSession, tenant_and_user, monkeypatch
) -> None:
    import meyar.agent.service as agent_service

    tenant, user, _password, membership = tenant_and_user
    conversation, session = await _new_conversation(db_session, tenant, user, membership)
    _candidates, root = await _seed_root(
        db_session, tenant, conversation, session, ("SQL",), ("SQL",), ("SQL",), ("SQL",)
    )

    monkeypatch.setattr(
        agent_service,
        "plan_candidate_search",
        _fake_structured_plan(required_skills=["SQL"], result_limit=3, used_default_limit=False),
    )

    result = await _refine(
        db_session,
        _refine_llm(filter_query="ilk 3 SQL bilən namizədləri göstər", limit=2),
        tenant_id=tenant.id,
        conversation=conversation,
    )
    assert result.outcome.value == "CLARIFICATION_REQUESTED"
    assert result.tool_results == []
    await db_session.refresh(conversation)
    assert conversation.active_result_set_id == root.id

    count = await db_session.scalar(
        select(func.count()).select_from(AgentResultSet).where(
            AgentResultSet.tenant_id == tenant.id,
            AgentResultSet.result_set_kind == AgentResultSetKind.REFINEMENT.value,
        )
    )
    assert count == 0


async def test_agent_decision_limit_used_when_planner_finds_no_explicit_count(
    db_session: AsyncSession, tenant_and_user, monkeypatch
) -> None:
    """The planner used its own normal default search limit (no explicit
    count in the HR text) — AgentDecision's own explicit limit still
    applies normally."""
    import meyar.agent.service as agent_service

    tenant, user, _password, membership = tenant_and_user
    conversation, session = await _new_conversation(db_session, tenant, user, membership)
    (a, b, _c, _d), root = await _seed_root(
        db_session, tenant, conversation, session, ("SQL",), ("SQL",), ("SQL",), ("SQL",)
    )

    monkeypatch.setattr(
        agent_service,
        "plan_candidate_search",
        _fake_structured_plan(required_skills=["SQL"], result_limit=20, used_default_limit=True),
    )

    result = await _refine(
        db_session,
        _refine_llm(filter_query="SQL bilən namizədləri göstər", limit=2),
        tenant_id=tenant.id,
        conversation=conversation,
    )
    assert result.outcome.value == "ANSWERED_FROM_TOOL_RESULT"
    refine = result.tool_results[0].refine
    assert refine is not None
    assert refine.requested_limit == 2
    ids = [str(r.candidate_id) for r in refine.response.results]
    assert ids == [str(a.id), str(b.id)]


async def test_planner_default_limit_never_becomes_implicit_refinement_truncation(
    db_session: AsyncSession, tenant_and_user, monkeypatch
) -> None:
    """No explicit count anywhere (neither the HR text nor AgentDecision.
    limit) — the planner's own normal default search limit (e.g. 20) must
    NOT silently truncate the refined result set."""
    import meyar.agent.service as agent_service

    tenant, user, _password, membership = tenant_and_user
    conversation, session = await _new_conversation(db_session, tenant, user, membership)
    (a, b, c), root = await _seed_root(
        db_session, tenant, conversation, session, ("SQL",), ("SQL",), ("SQL",)
    )

    monkeypatch.setattr(
        agent_service,
        "plan_candidate_search",
        _fake_structured_plan(required_skills=["SQL"], result_limit=20, used_default_limit=True),
    )

    result = await _refine(
        db_session,
        _refine_llm(filter_query="SQL bilən namizədləri göstər"),
        tenant_id=tenant.id,
        conversation=conversation,
    )
    assert result.outcome.value == "ANSWERED_FROM_TOOL_RESULT"
    refine = result.tool_results[0].refine
    assert refine is not None
    assert refine.requested_limit is None
    assert refine.limit_truncated is False
    ids = [str(r.candidate_id) for r in refine.response.results]
    assert ids == [str(a.id), str(b.id), str(c.id)]


def test_bare_refinement_intent_with_no_operation_is_schema_rejected() -> None:
    try:
        AgentDecision(action=AgentActionType.REFINE_CANDIDATE_RESULTS)
        raise AssertionError("expected ValidationError")
    except ValidationError:
        pass


# --- Provenance --------------------------------------------------------------


async def test_derived_result_set_copies_root_search_provenance_verbatim(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    conversation, session = await _new_conversation(db_session, tenant, user, membership)
    (a, b), root = await _seed_root(
        db_session, tenant, conversation, session, ("SQL",), ("Python",)
    )

    await _refine(
        db_session,
        _refine_llm(filter_query="SQL bilən namizədləri göstər"),
        tenant_id=tenant.id,
        conversation=conversation,
    )
    await db_session.refresh(conversation)
    derived = await db_session.get(AgentResultSet, conversation.active_result_set_id)
    assert derived is not None
    assert derived.canonical_search_request == root.canonical_search_request
    assert derived.planner_policy_version == root.planner_policy_version
    assert derived.planner_prompt_version == root.planner_prompt_version
    assert derived.planner_schema_version == root.planner_schema_version
    assert derived.planner_model_provider == root.planner_model_provider
    assert derived.planner_model_name == root.planner_model_name
    assert derived.search_policy_version == root.search_policy_version
    assert derived.search_mode == root.search_mode
    assert derived.corpus_fingerprint_sha256 == root.corpus_fingerprint_sha256
    assert derived.expires_at == root.expires_at
    assert derived.refinement_request_sha256 is not None
    assert len(derived.refinement_request_sha256) == 64
    assert derived.canonical_refinement_request is not None
    assert derived.canonical_refinement_request["limit"] is None
    assert derived.canonical_refinement_request["filter_request"] is not None
    assert derived.refinement_policy_version is not None


async def test_derived_expiry_never_exceeds_parent(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    conversation, session = await _new_conversation(db_session, tenant, user, membership)
    (_a,), root = await _seed_root(db_session, tenant, conversation, session, ("SQL",))

    original_expiry = root.expires_at
    await _refine(
        db_session, _refine_llm(limit=1), tenant_id=tenant.id, conversation=conversation
    )
    await db_session.refresh(conversation)
    derived = await db_session.get(AgentResultSet, conversation.active_result_set_id)
    assert derived is not None
    assert derived.expires_at == original_expiry


# --- Audit privacy / no business side effects -------------------------------


async def test_refinement_audit_event_never_leaks_raw_filter_text(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    conversation, session = await _new_conversation(db_session, tenant, user, membership)
    (a, b), root = await _seed_root(
        db_session, tenant, conversation, session, ("SQL",), ("Python",)
    )

    await _refine(
        db_session,
        _refine_llm(filter_query="SQL bilən namizədləri göstər", limit=1),
        tenant_id=tenant.id,
        conversation=conversation,
    )
    await db_session.commit()

    events = list(
        await db_session.scalars(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenant.id,
                AuditEvent.event_type == "agent.result_set.refined",
            )
        )
    )
    assert len(events) == 1
    metadata = events[0].event_metadata
    assert set(metadata.keys()) == {
        "source_result_set_id",
        "result_set_id",
        "context_epoch",
        "source_result_count",
        "result_count",
        "refinement_request_sha256",
        "refinement_policy_version",
        "requested_limit",
        "has_filter",
    }
    serialized = str(metadata)
    assert "SQL" not in serialized
    assert "bilən" not in serialized
    assert str(a.id) not in serialized
    assert str(b.id) not in serialized


async def test_refinement_never_creates_job_or_evaluation_rows(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    conversation, session = await _new_conversation(db_session, tenant, user, membership)
    _candidates, root = await _seed_root(
        db_session, tenant, conversation, session, ("SQL",), ("Python",)
    )

    await _refine(
        db_session,
        _refine_llm(filter_query="SQL bilən namizədləri göstər"),
        tenant_id=tenant.id,
        conversation=conversation,
    )
    await _refine(db_session, _refine_llm(limit=1), tenant_id=tenant.id, conversation=conversation)
    await db_session.commit()

    assert await db_session.scalar(select(func.count()).select_from(JobModel)) == 0
    assert await db_session.scalar(select(func.count()).select_from(JobCriteriaVersion)) == 0
    assert await db_session.scalar(select(func.count()).select_from(Evaluation)) == 0

