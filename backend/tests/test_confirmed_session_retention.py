"""Issue #46 L-6: consequential provenance must not make sessions immortal."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from search_helpers import open_test_conversation, seed_active_result_set
from sqlalchemy import select

from meyar.models.agent_conversation import AgentConversation, AgentConversationSessionContext
from meyar.models.agent_draft_confirmation import AgentDraftConfirmation
from meyar.models.agent_result_set import AgentResultSet
from meyar.models.agent_task import AgentClarification, AgentTask
from meyar.models.agent_turn_submission import AgentTurnSubmission
from meyar.models.browser_session import BrowserSession
from meyar.models.job import Job
from meyar.models.job_criteria_version import JobCriteriaVersion
from meyar.services.agent_draft_confirmation_repo import (
    create_draft_confirmation,
    get_draft_confirmation,
)
from meyar.services.browser_session_repo import create_browser_session
from meyar.services.maintenance import MaintenancePolicy, run_maintenance
from meyar.services.tenant_membership_repo import create_membership
from meyar.services.tenant_repo import create_tenant


async def confirmed_session(db, tenant, user, membership):
    session, _ = await create_browser_session(
        db, user_id=user.id, tenant_membership_id=membership.id, ttl_hours=8
    )
    conversation, context = await open_test_conversation(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        membership_id=membership.id,
        browser_session_id=session.id,
    )
    result_set = await seed_active_result_set(
        db,
        tenant_id=tenant.id,
        browser_session_id=session.id,
        session_context=context,
        candidate_ids=[],
    )
    job = Job(tenant_id=tenant.id, title="SYNTHETIC CONFIRMED VACANCY")
    db.add(job)
    await db.flush()
    criteria = JobCriteriaVersion(tenant_id=tenant.id, job_id=job.id, version_number=1, criteria=[])
    db.add(criteria)
    await db.flush()
    confirmation = await create_draft_confirmation(
        db,
        tenant_id=tenant.id,
        browser_session_id=session.id,
        draft_id=uuid.uuid4(),
        job_id=job.id,
        criteria_version_id=criteria.id,
    )
    task = AgentTask(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        session_context_id=context.id,
        owner_user_id=user.id,
        owner_membership_id=membership.id,
        task_type="UNDETERMINED",
        status="WAITING_CLARIFICATION",
        phase="NEEDS_SOURCE",
        policy_version="synthetic",
        created_by_submission_id=uuid.uuid4(),
        expires_at=session.expires_at,
    )
    db.add(task)
    await db.flush()
    clarification = AgentClarification(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        session_context_id=context.id,
        task_id=task.id,
        context_epoch=context.context_epoch,
        clarification_type="VACANCY_SOURCE_REQUIRED",
        answer_schema_version="synthetic",
        question_turn_id=uuid.uuid4(),
        created_from_turn_id=uuid.uuid4(),
        semantic_policy_version="synthetic",
        routing_policy_version="synthetic",
        created_turn_version=0,
        status="OPEN",
        attempt=1,
        expires_at=session.expires_at,
        created_by_submission_id=uuid.uuid4(),
    )
    db.add(clarification)
    db.add(
        AgentTurnSubmission(
            tenant_id=tenant.id,
            user_id=user.id,
            membership_id=membership.id,
            browser_session_id=session.id,
            conversation_id=conversation.id,
            context_epoch=context.context_epoch,
            expires_at=session.expires_at,
        )
    )
    await db.flush()
    context.active_clarification_id = clarification.id
    return session, conversation, context, result_set, job, criteria, confirmation


async def test_expired_confirmed_session_is_retired(db_session, tenant_and_user, tmp_path):
    tenant, user, _, membership = tenant_and_user
    (
        session,
        conversation,
        context,
        result_set,
        job,
        criteria,
        confirmation,
    ) = await confirmed_session(db_session, tenant, user, membership)
    session.expires_at = datetime.now(UTC) - timedelta(days=90)
    session.revoked_at = datetime.now(UTC) - timedelta(days=90)
    await db_session.commit()
    result = await run_maintenance(
        db_session,
        tenant_id=tenant.id,
        storage_root=tmp_path,
        policy=MaintenancePolicy(session_days=7),
        apply=True,
    )
    await db_session.commit()
    retained = {
        "session": await db_session.scalar(
            select(BrowserSession.id).where(BrowserSession.id == session.id)
        ),
        "context": await db_session.scalar(
            select(AgentConversationSessionContext.id).where(
                AgentConversationSessionContext.id == context.id
            )
        ),
        "result_set": await db_session.scalar(
            select(AgentResultSet.id).where(AgentResultSet.id == result_set.id)
        ),
    }
    assert retained == dict.fromkeys(retained), (
        f"Confirmed session remained past explicit cutoff: {retained}; {result.counts}"
    )
    assert result.counts == {"SESSION_RETIRED": 1}
    assert await db_session.scalar(select(AgentTask.id)) is None
    assert await db_session.scalar(select(AgentClarification.id)) is None
    assert await db_session.scalar(select(AgentTurnSubmission.id)) is None
    preserved = await db_session.get(
        AgentDraftConfirmation, confirmation.id, populate_existing=True
    )
    assert preserved.browser_session_id is None
    assert preserved.historical_browser_session_id == session.id
    assert preserved.job_id == job.id and preserved.criteria_version_id == criteria.id
    assert await db_session.get(Job, job.id) is not None
    assert (await db_session.get(JobCriteriaVersion, criteria.id)).criteria == []
    assert await db_session.get(AgentConversation, conversation.id) is not None
    assert (
        await get_draft_confirmation(
            db_session,
            tenant_id=tenant.id,
            browser_session_id=session.id,
            draft_id=preserved.draft_id,
        )
        is None
    )  # Historical UUID never becomes replay authority.
    fresh, _ = await create_browser_session(
        db_session, user_id=user.id, tenant_membership_id=membership.id, ttl_hours=8
    )
    assert (
        await get_draft_confirmation(
            db_session,
            tenant_id=tenant.id,
            browser_session_id=fresh.id,
            draft_id=preserved.draft_id,
        )
        is None
    )  # Relogin cannot reuse retired confirmation authority.


@pytest.mark.parametrize("state", ["live", "young_expired", "young_revoked", "revoked"])
async def test_confirmed_session_age_and_inspection(db_session, tenant_and_user, tmp_path, state):
    tenant, user, _, membership = tenant_and_user
    session, _, _, _, _, _, confirmation = await confirmed_session(
        db_session, tenant, user, membership
    )
    if state == "young_expired":
        session.expires_at = datetime.now(UTC) - timedelta(days=1)
    if state in {"young_revoked", "revoked"}:
        session.revoked_at = datetime.now(UTC) - timedelta(
            days=1 if state == "young_revoked" else 90
        )
    await db_session.commit()
    policy = MaintenancePolicy(session_days=7)
    inspected = await run_maintenance(
        db_session, tenant_id=tenant.id, storage_root=tmp_path, policy=policy, apply=False
    )
    await db_session.commit()  # Even commit cannot make inspection destructive.
    assert (
        await db_session.scalar(select(BrowserSession.id).where(BrowserSession.id == session.id))
        == session.id
    )
    assert (
        await db_session.get(AgentDraftConfirmation, confirmation.id, populate_existing=True)
    ).browser_session_id == session.id
    applied = await run_maintenance(
        db_session, tenant_id=tenant.id, storage_root=tmp_path, policy=policy, apply=True
    )
    await db_session.commit()
    assert (
        inspected.counts == applied.counts == ({"SESSION_RETIRED": 1} if state == "revoked" else {})
    )
    assert (
        await db_session.scalar(select(BrowserSession.id).where(BrowserSession.id == session.id))
        is None
    ) == (state == "revoked")


async def test_confirmed_retirement_tenant_batch_idempotency(db_session, tenant_and_user, tmp_path):
    tenant, user, _, membership = tenant_and_user
    own = [await confirmed_session(db_session, tenant, user, membership) for _ in range(2)]
    other = await create_tenant(db_session, name="SYNTHETIC OTHER TENANT")
    other_member = await create_membership(
        db_session, user_id=user.id, tenant_id=other.id, role="HR_USER"
    )
    foreign = await confirmed_session(db_session, other, user, other_member)
    for rows in [*own, foreign]:
        rows[0].expires_at = datetime.now(UTC) - timedelta(days=90)
    await db_session.commit()
    results = []
    for _ in range(3):
        results.append(
            await run_maintenance(
                db_session,
                tenant_id=tenant.id,
                storage_root=tmp_path,
                policy=MaintenancePolicy(session_days=7, limit=1),
                apply=True,
            )
        )
        await db_session.commit()
    assert [r.counts for r in results] == [{"SESSION_RETIRED": 1}, {"SESSION_RETIRED": 1}, {}]
    assert not results[0].complete and results[-1].complete
    assert (
        await db_session.scalar(select(BrowserSession.id).where(BrowserSession.id == foreign[0].id))
        == foreign[0].id
    )
    assert (
        await db_session.get(AgentDraftConfirmation, foreign[-1].id, populate_existing=True)
    ).browser_session_id == foreign[0].id
    assert len((await db_session.scalars(select(AgentDraftConfirmation))).all()) == 3
    assert all(set(r.counts) <= {"SESSION_RETIRED"} for r in results)


@pytest.mark.parametrize("invalid", ["missing", "foreign", "expired", "revoked"])
async def test_confirmation_creation_requires_owned_live_session(
    db_session, tenant_and_user, invalid
):
    tenant, user, _, membership = tenant_and_user
    session, _, _, _, job, criteria, _ = await confirmed_session(
        db_session, tenant, user, membership
    )
    session_id, tenant_id = session.id, tenant.id
    if invalid == "missing":
        session_id = uuid.uuid4()
    elif invalid == "foreign":
        tenant_id = (await create_tenant(db_session, name="SYNTHETIC FOREIGN")).id
    elif invalid == "expired":
        session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    else:
        session.revoked_at = datetime.now(UTC)
    await db_session.flush()
    with pytest.raises(ValueError, match="owned live browser session"):
        await create_draft_confirmation(
            db_session,
            tenant_id=tenant_id,
            browser_session_id=session_id,
            draft_id=uuid.uuid4(),
            job_id=job.id,
            criteria_version_id=criteria.id,
        )
