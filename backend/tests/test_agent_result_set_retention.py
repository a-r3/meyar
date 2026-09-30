"""Issue #86 (L-6, ResultSet part) — bounded ResultSet retention without a
background service (docs/DECISIONS.md D-090).

Creation-time pruning per (tenant, conversation, BrowserSession): every
ResultSet a live session context points at survives; of the rest, expired
ones are retired at once and at most MAX_INACTIVE_RESULT_SETS_PER_CONTEXT
unexpired ones are kept. Member rows cascade; transcript and AuditEvents
are never deleted; each retired row leaves a safe ``agent.result_set.retired``
audit record. Synthetic data only."""

import uuid
from datetime import UTC, datetime, timedelta

from search_helpers import (
    open_test_conversation,
    seed_active_result_set,
    seed_candidate_with_profile,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.models.agent_conversation import AgentConversation
from meyar.models.agent_result_set import AgentResultSet, AgentResultSetMember
from meyar.models.audit_event import AuditEvent
from meyar.services.agent_result_set_repo import (
    MAX_INACTIVE_RESULT_SETS_PER_CONTEXT,
    RefinementResult,
    create_result_set_from_refinement,
    resolve_active_candidate_ref,
)
from meyar.services.browser_session_repo import create_browser_session

TURNS = 130


def _profile(skill: str) -> dict:
    return {
        "skills": [
            {
                "name": skill,
                "category": None,
                "evidence": [{"page": 1, "block_index": 0, "quote": f"Synthetic skill {skill}"}],
            }
        ],
        "employment_history": [],
        "education": [],
        "certifications": [],
        "languages": [],
        "projects": [],
    }


async def _setup(db: AsyncSession, tenant_and_user, *, members: int = 3):  # noqa: ANN001, ANN202
    tenant, user, _password, membership = tenant_and_user
    candidates = []
    for index in range(members):
        candidate, _version = await seed_candidate_with_profile(
            db, tenant_id=tenant.id, profile_content=_profile(f"Skill{index}")
        )
        candidates.append(candidate)
    session, _raw = await create_browser_session(
        db, user_id=user.id, tenant_membership_id=membership.id, ttl_hours=8
    )
    await db.flush()
    conversation, context = await open_test_conversation(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        membership_id=membership.id,
        browser_session_id=session.id,
    )
    root = await seed_active_result_set(
        db,
        tenant_id=tenant.id,
        browser_session_id=session.id,
        session_context=context,
        candidate_ids=[candidate.id for candidate in candidates],
    )
    await db.commit()
    return tenant, session, conversation, context, root


async def _refine_and_point(db: AsyncSession, tenant, session, context) -> RefinementResult:  # noqa: ANN001
    """One refinement turn, then the Phase B pointer switch (issue #85)."""
    refined = await create_result_set_from_refinement(
        db,
        tenant_id=tenant.id,
        browser_session_id=session.id,
        session_context=context,
        filter_request=None,
        requested_limit=None,
    )
    assert isinstance(refined, RefinementResult)
    context.active_result_set_id = refined.result_set.id
    await db.commit()
    return refined


async def _sets(db: AsyncSession, conversation_id: uuid.UUID) -> list[AgentResultSet]:
    return list(
        (
            await db.scalars(
                select(AgentResultSet).where(AgentResultSet.conversation_id == conversation_id)
            )
        ).all()
    )


async def _events(db: AsyncSession, tenant_id: uuid.UUID, event_type: str) -> list[dict]:
    rows = (
        await db.scalars(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenant_id, AuditEvent.event_type == event_type
            )
        )
    ).all()
    return [dict(row.event_metadata) for row in rows]


async def test_growth_is_bounded_over_a_130_turn_conversation(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, session, conversation, context, root = await _setup(db_session, tenant_and_user)
    transcript_before = list(conversation.turns)
    for _ in range(TURNS):
        await _refine_and_point(db_session, tenant, session, context)

    remaining = await _sets(db_session, conversation.id)
    # Pruning runs at creation time, BEFORE the Phase B pointer switch, so
    # the then-active source is still protected: steady state is 1 active +
    # at most N+1 inactive (the previous active one plus N others).
    assert len(remaining) <= 2 + MAX_INACTIVE_RESULT_SETS_PER_CONTEXT
    assert context.active_result_set_id in {row.id for row in remaining}
    remaining_ids = {row.id for row in remaining}
    member_rows = await db_session.scalar(
        select(func.count()).select_from(AgentResultSetMember)
    )
    assert member_rows == 3 * len(remaining)  # orphans' members cascaded away
    orphan_members = await db_session.scalar(
        select(func.count())
        .select_from(AgentResultSetMember)
        .where(AgentResultSetMember.result_set_id.not_in(remaining_ids))
    )
    assert orphan_members == 0

    # Audit provenance survives; every retired row left a safe record.
    retired = await _events(db_session, tenant.id, "agent.result_set.retired")
    assert len(retired) == TURNS + 1 - len(remaining)
    assert len(await _events(db_session, tenant.id, "agent.result_set.refined")) == TURNS
    assert root.id in {uuid.UUID(event["result_set_id"]) for event in retired}
    for event in retired:
        assert set(event) == {
            "result_set_id", "result_set_kind", "parent_result_set_id", "conversation_id",
            "context_epoch", "request_sha256", "refinement_request_sha256",
            "search_policy_version", "refinement_policy_version", "snapshot_policy_version",
            "search_mode", "result_count", "created_at", "expires_at",
        }
        assert "canonical_search_request" not in str(event)
    # Transcript untouched; the active ResultSet still resolves (its parent
    # chain was retired — a derived set carries its own member snapshot).
    stored = await db_session.get(AgentConversation, conversation.id)
    assert stored.turns == transcript_before
    resolved = await resolve_active_candidate_ref(
        db_session,
        tenant_id=tenant.id,
        browser_session_id=session.id,
        session_context=context,
        candidate_ref=1,
    )
    assert getattr(resolved, "result_set_id", None) == context.active_result_set_id


async def test_expired_inactive_sets_are_retired_immediately_active_never(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, session, conversation, context, root = await _setup(db_session, tenant_and_user)
    first = await _refine_and_point(db_session, tenant, session, context)
    # Expire every existing set, including the ACTIVE one.
    for row in await _sets(db_session, conversation.id):
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()
    # Another conversation turn re-activates the expired pointer? No — the
    # active expired set must still survive cleanup (EXPIRED copy relies on it).
    context.active_result_set_id = first.result_set.id
    await db_session.commit()
    # Trigger cleanup via a new refinement on a fresh unexpired chain.
    fresh = await seed_active_result_set(
        db_session,
        tenant_id=tenant.id,
        browser_session_id=session.id,
        session_context=context,
        candidate_ids=[],
    )
    await db_session.commit()
    await _refine_and_point(db_session, tenant, session, context)
    remaining = {row.id for row in await _sets(db_session, conversation.id)}
    assert root.id not in remaining  # expired + inactive -> retired
    assert first.result_set.id not in remaining  # expired + no longer pointed at
    assert fresh.id in remaining  # unexpired inactive, within the bound
    assert context.active_result_set_id in remaining


async def test_cleanup_is_scoped_to_one_tenant_conversation_and_session(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, session, conversation, context, _root = await _setup(db_session, tenant_and_user)
    # A second BrowserSession on the SAME conversation with its own history.
    _tenant, user, _password, membership = tenant_and_user
    other_session, _raw = await create_browser_session(
        db_session, user_id=user.id, tenant_membership_id=membership.id, ttl_hours=8
    )
    await db_session.flush()
    from meyar.services.agent_conversation_repo import get_or_create_session_context

    other_context = await get_or_create_session_context(
        db_session, conversation=conversation, browser_session_id=other_session.id
    )
    other_sets = []
    for _ in range(MAX_INACTIVE_RESULT_SETS_PER_CONTEXT + 3):
        other_sets.append(
            await seed_active_result_set(
                db_session,
                tenant_id=tenant.id,
                browser_session_id=other_session.id,
                session_context=other_context,
                candidate_ids=[],
            )
        )
    await db_session.commit()
    for _ in range(MAX_INACTIVE_RESULT_SETS_PER_CONTEXT + 3):
        await _refine_and_point(db_session, tenant, session, context)
    surviving_other = {
        row.id
        for row in await _sets(db_session, conversation.id)
        if row.browser_session_id == other_session.id
    }
    # The other BrowserSession's rows are never touched by this session's
    # cleanup (only its own creations prune them).
    assert surviving_other == {row.id for row in other_sets}


async def test_retired_set_pointed_to_by_any_context_is_never_deleted(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """Defense in depth: a set another live context still points at is
    never retired even when it would otherwise fall outside the bound."""
    tenant, session, conversation, context, root = await _setup(db_session, tenant_and_user)
    from meyar.services.agent_conversation_repo import get_or_create_session_context

    _tenant, user, _password, membership = tenant_and_user
    other_session, _raw = await create_browser_session(
        db_session, user_id=user.id, tenant_membership_id=membership.id, ttl_hours=8
    )
    await db_session.flush()
    other_context = await get_or_create_session_context(
        db_session, conversation=conversation, browser_session_id=other_session.id
    )
    other_context.active_result_set_id = root.id  # tampered/foreign pointer
    await db_session.commit()
    for _ in range(MAX_INACTIVE_RESULT_SETS_PER_CONTEXT + 3):
        await _refine_and_point(db_session, tenant, session, context)
    assert await db_session.get(AgentResultSet, root.id) is not None
