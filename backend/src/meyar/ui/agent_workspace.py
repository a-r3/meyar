"""Presentation-only context for the bounded MEYAR AI workspace."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.config import Settings
from meyar.models.agent_conversation import AgentConversation
from meyar.services.agent_conversation_repo import (
    ConversationSummary,
    OwnerPrincipal,
    get_active_pending_job_draft,
    get_session_context,
    list_owned_conversations,
)
from meyar.ui.auth import UIContext
from meyar.ui.service import build_agent_job_draft_view

HISTORY_PAGE_SIZE = 20
TITLE_LABELS = {
    "NEW": "Yeni söhbət",
    "CANDIDATE_SEARCH": "Namizəd axtarışı",
    "VACANCY_ANALYSIS": "Vakansiya analizi",
    "RESULT_REFINEMENT": "Nəticələrin dəqiqləşdirilməsi",
    "GENERAL": "Ümumi söhbət",
}
_MONTHS = ("yan", "fev", "mar", "apr", "may", "iyn", "iyl", "avq", "sen", "okt", "noy", "dek")


@dataclass(frozen=True)
class ClarificationChoiceView:
    value: str
    label: str


@dataclass(frozen=True)
class ActiveClarificationView:
    """Closed-choice buttons for the ONE answerable clarification (§20).
    Shown only while the live pointer names an OPEN, unexpired question that
    is still the last transcript entry — never for history alone."""

    id: uuid.UUID
    choices: tuple[ClarificationChoiceView, ...]


async def _active_clarification_view(
    db: AsyncSession, *, conversation: AgentConversation, session_context: object
) -> ActiveClarificationView | None:
    from meyar.agent.clarification_schemas import (
        ALLOWED_ANSWERS,
        ClarificationStatus,
        ClarificationType,
    )
    from meyar.agent.dialogue import CHOICE_LABELS
    from meyar.models.agent_task import AgentClarification

    pointer = getattr(session_context, "active_clarification_id", None)
    if pointer is None or not conversation.turns:
        return None
    row = await db.scalar(
        select(AgentClarification).where(
            AgentClarification.id == pointer,
            AgentClarification.tenant_id == conversation.tenant_id,
            AgentClarification.session_context_id == session_context.id,  # type: ignore[attr-defined]
        )
    )
    if (
        row is None
        or row.status != ClarificationStatus.OPEN.value
        or row.expires_at <= datetime.now(UTC)
        or conversation.turns[-1].get("turn_id") != str(row.question_turn_id)
    ):
        return None
    answers = ALLOWED_ANSWERS[ClarificationType(row.clarification_type)]
    return ActiveClarificationView(
        id=row.id,
        choices=tuple(
            ClarificationChoiceView(value=answer.value, label=CHOICE_LABELS[answer])
            for answer in answers
        ),
    )


@dataclass(frozen=True)
class ConversationHistoryItemView:
    id: uuid.UUID
    title: str
    updated_label: str
    is_active: bool


def history_item_view(
    item: ConversationSummary | AgentConversation,
    *,
    current_id: uuid.UUID,
    timezone: str,
    now: datetime | None = None,
) -> ConversationHistoryItemView:
    local_now = (now or datetime.now(UTC)).astimezone(ZoneInfo(timezone))
    local_updated = item.updated_at.astimezone(ZoneInfo(timezone))
    if local_updated.date() == local_now.date():
        date_label = "Bu gün"
    else:
        date_label = f"{local_updated.day} {_MONTHS[local_updated.month - 1]}"
        if local_updated.year != local_now.year:
            date_label += f" {local_updated.year}"
    return ConversationHistoryItemView(
        id=item.id,
        title=TITLE_LABELS.get(item.title_kind, TITLE_LABELS["GENERAL"]),
        updated_label=f"{date_label}, {local_updated:%H:%M}",
        is_active=item.id == current_id,
    )


async def build_agent_workspace_context(
    db: AsyncSession,
    *,
    ctx: UIContext,
    settings: Settings,
    conversation: AgentConversation,
    latest_draft_ids: frozenset[uuid.UUID] = frozenset(),
    page: int = 1,
) -> dict[str, object]:
    """One owner-scoped summary query and one read-only live-context lookup.

    Only the already loaded active conversation can supply a draft payload.
    A missing BrowserSession context never creates authority on a read.
    """
    session_context = await get_session_context(
        db, conversation=conversation, browser_session_id=ctx.session_id
    )
    active_draft = get_active_pending_job_draft(conversation, session_context)
    active_clarification = (
        await _active_clarification_view(
            db, conversation=conversation, session_context=session_context
        )
        if session_context is not None
        else None
    )
    active_draft_view = None
    if active_draft is not None and active_draft.draft_id not in latest_draft_ids:
        active_draft_view = build_agent_job_draft_view(active_draft)
        active_draft_view.can_act = True
    history = await list_owned_conversations(
        db,
        owner=OwnerPrincipal(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            membership_id=ctx.membership_id,
        ),
        page=page,
        page_size=HISTORY_PAGE_SIZE,
    )
    items = [
        history_item_view(row, current_id=conversation.id, timezone=settings.business_timezone)
        for row in history.items
    ]
    active_outside_page = None
    if not any(item.is_active for item in items):
        active_outside_page = history_item_view(
            conversation, current_id=conversation.id, timezone=settings.business_timezone
        )
    return {
        "workspace_active_draft": active_draft_view,
        "workspace_active_clarification": active_clarification,
        "workspace_history": items,
        "workspace_active_outside_page": active_outside_page,
        "workspace_no_past_conversations": (
            page == 1 and len(items) == 1 and not history.has_next
            and items[0].is_active and not conversation.turns
        ),
        "workspace_page": page,
        "workspace_has_previous": page > 1,
        "workspace_has_next": history.has_next,
    }
