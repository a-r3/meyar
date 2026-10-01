"""Durable browser submission identity bound to #85's turn reservation."""

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.agent.turn_boundary import TurnReservation
from meyar.models.agent_conversation import (
    AgentConversation,
    AgentConversationSessionContext,
)
from meyar.models.agent_turn_submission import AgentTurnSubmission
from meyar.services.agent_conversation_repo import OwnerPrincipal, get_session_context

SUBMISSION_TTL = timedelta(hours=24)
RETIRE_BATCH = 20


def request_hash(
    message: str,
    *,
    clarification_id: uuid.UUID | None = None,
    clarification_choice: str | None = None,
) -> str:
    """Replay/idempotency evidence for one semantic request — never
    authorization. Issue #88 (D-092 §6.4/§13): the canonical representation
    covers the message AND the clarification button fields, so a replay of
    the same token with another choice or clarification id fails closed."""
    canonical = json.dumps(
        {
            "v": 1,
            "message": message,
            "clarification_id": str(clarification_id) if clarification_id else None,
            "clarification_choice": clarification_choice,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def issue_submission(
    db: AsyncSession, *, owner: OwnerPrincipal, browser_session_id: uuid.UUID,
    conversation: AgentConversation,
) -> AgentTurnSubmission:
    now = datetime.now(UTC)
    # Bounded lazy cleanup. Live PROCESSING rows survive. A crashed turn is
    # collectible only after BOTH its submission and reservation lease lapse.
    old_ids = (
        select(AgentTurnSubmission.id)
        .join(AgentConversation, AgentConversation.id == AgentTurnSubmission.conversation_id)
        .where(
            AgentTurnSubmission.tenant_id == owner.tenant_id,
            AgentTurnSubmission.expires_at < now,
            or_(
                AgentTurnSubmission.status != "PROCESSING",
                and_(
                    AgentTurnSubmission.lease_expires_at < now,
                    or_(
                        AgentConversation.active_turn_id.is_(None),
                        AgentConversation.active_turn_id != AgentTurnSubmission.reservation_id,
                        AgentConversation.active_turn_expires_at < now,
                    ),
                ),
            ),
        )
        .limit(RETIRE_BATCH)
    )
    await db.execute(delete(AgentTurnSubmission).where(AgentTurnSubmission.id.in_(old_ids)))
    context = await get_session_context(
        db, conversation=conversation, browser_session_id=browser_session_id
    )
    if context is None:
        max_epoch = await db.scalar(
            select(func.max(AgentConversationSessionContext.context_epoch)).where(
                AgentConversationSessionContext.tenant_id == owner.tenant_id,
                AgentConversationSessionContext.conversation_id == conversation.id,
            )
        )
        context_epoch = (max_epoch or 0) + 1
    else:
        context_epoch = context.context_epoch
    row = AgentTurnSubmission(
        tenant_id=owner.tenant_id, user_id=owner.user_id,
        membership_id=owner.membership_id, browser_session_id=browser_session_id,
        conversation_id=conversation.id,
        context_epoch=context_epoch,
        status="ISSUED", expires_at=now + SUBMISSION_TTL,
    )
    db.add(row)
    await db.flush()
    return row


async def get_bound_submission(
    db: AsyncSession, *, submission_id: uuid.UUID, owner: OwnerPrincipal,
    browser_session_id: uuid.UUID, conversation_id: uuid.UUID,
    for_update: bool = False,
) -> AgentTurnSubmission | None:
    stmt = select(AgentTurnSubmission).where(
        AgentTurnSubmission.id == submission_id,
        AgentTurnSubmission.tenant_id == owner.tenant_id,
        AgentTurnSubmission.user_id == owner.user_id,
        AgentTurnSubmission.membership_id == owner.membership_id,
        AgentTurnSubmission.browser_session_id == browser_session_id,
        AgentTurnSubmission.conversation_id == conversation_id,
    ).execution_options(populate_existing=True)
    if for_update:
        stmt = stmt.with_for_update()
    return await db.scalar(stmt)


def claim_submission(
    row: AgentTurnSubmission, *, reservation: TurnReservation,
    message_sha256: str, ttl_seconds: int,
) -> str:
    """Call only with the conversation row locked by reserve_agent_turn."""
    now = datetime.now(UTC)
    if row.request_sha256 is not None and row.request_sha256 != message_sha256:
        return "MISMATCH"
    if row.status == "COMPLETED":
        return "COMPLETED"
    if row.expires_at <= now or row.context_epoch != reservation.context_epoch:
        return "INVALID"
    if row.status == "PROCESSING" and row.lease_expires_at and row.lease_expires_at > now:
        return "PROCESSING"
    if row.status not in {"ISSUED", "PROCESSING"}:
        return "INVALID"
    row.status = "PROCESSING"
    row.request_sha256 = message_sha256
    row.reservation_id = reservation.token
    row.lease_expires_at = now + timedelta(seconds=ttl_seconds)
    return "CLAIMED"


def complete_submission(
    row: AgentTurnSubmission, *, reservation: TurnReservation,
    message_sha256: str, completed_turn_version: int,
) -> None:
    if (
        row.status != "PROCESSING" or row.reservation_id != reservation.token
        or row.request_sha256 != message_sha256
    ):
        raise ValueError("Submission authority changed during turn")
    row.status = "COMPLETED"
    row.completed_turn_version = completed_turn_version
    row.lease_expires_at = None
