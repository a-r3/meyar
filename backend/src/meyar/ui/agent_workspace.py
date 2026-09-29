"""Presentation-only context for the bounded MEYAR AI workspace."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from meyar.config import Settings
from meyar.models.agent_conversation import AgentConversation
from meyar.services.agent_conversation_repo import (
    ConversationSummary,
    OwnerPrincipal,
    list_owned_conversations,
)
from meyar.ui.auth import UIContext

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
    page: int = 1,
) -> dict[str, object]:
    """One owner-scoped summary query; never scans history transcripts."""
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
