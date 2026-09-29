"""durable AgentConversation ownership + BrowserSession-bound session
context + AgentResultSet conversation binding (issue #80 PR80-1)

Revision ID: b7e3c9d41f28
Revises: 543c60f7efc5
Create Date: 2026-09-29

Separates DURABLE conversation history (owned by tenant + user +
membership) from LIVE session context (BrowserSession-bound ResultSet and
pending-draft authority):

1. ``agent_conversations`` gains ``owner_user_id``/``owner_membership_id``/
   ``title_kind``. Owners are backfilled ONLY through the pre-existing
   ``browser_session_id -> browser_sessions -> (user_id,
   tenant_membership_id)`` link, and only when that membership belongs to
   the same user AND the conversation's own tenant. Any row that cannot be
   attributed this way aborts the migration — ownership is never
   fabricated.
2. ``agent_conversation_session_contexts`` is created and backfilled with
   exactly one row per existing conversation from its current
   ``browser_session_id``/``context_epoch``/``active_result_set_id``. The
   old active ResultSet therefore stays bound ONLY to its original
   BrowserSession's context; no other/new session inherits it.
3. ``agent_result_sets.conversation_id`` is backfilled through the
   pre-migration 1:1 ``browser_session_id`` link (guaranteed by the old
   unique index). Any unattributable row aborts the migration.
4. Only after successful backfill are the new columns made NOT NULL, and
   the old live-context columns (``browser_session_id``,
   ``context_epoch``, ``active_result_set_id``) are dropped from
   ``agent_conversations`` — one authority representation, not two.

Pending-draft policy: every migrated context starts with
``active_pending_draft_id = NULL``. The migration intentionally INVALIDATES
pre-existing unconfirmed pending-draft authority rather than reconstructing
mutation authority from historical transcript JSON; HR must re-analyse the
vacancy before confirmation. Already confirmed Jobs/criteria versions and
AgentDraftConfirmation rows are untouched.

Downgrade is lossy by necessity (N conversations per session cannot map
back onto a 1:1 schema): each BrowserSession keeps its most recently used
conversation; conversations with no mappable session are deleted.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7e3c9d41f28"
down_revision: str | Sequence[str] | None = "543c60f7efc5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TITLE_KINDS = "'NEW', 'CANDIDATE_SEARCH', 'VACANCY_ANALYSIS', 'RESULT_REFINEMENT', 'GENERAL'"


def _abort_if(connection: sa.Connection, sql: str, message: str) -> None:
    count = connection.execute(sa.text(sql)).scalar_one()
    if count:
        raise RuntimeError(f"{message} ({count} row(s)); refusing to guess.")


def upgrade() -> None:
    connection = op.get_bind()

    # Pre-migration invariant the backfill relies on (old unique index).
    _abort_if(
        connection,
        "SELECT count(*) FROM (SELECT browser_session_id FROM agent_conversations "
        "GROUP BY browser_session_id HAVING count(*) > 1) duplicates",
        "agent_conversations is not 1:1 with browser_sessions",
    )

    # 1. Durable ownership.
    op.add_column("agent_conversations", sa.Column("owner_user_id", sa.Uuid(), nullable=True))
    op.add_column(
        "agent_conversations", sa.Column("owner_membership_id", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "agent_conversations",
        sa.Column("title_kind", sa.String(length=32), nullable=False, server_default="NEW"),
    )
    op.execute(
        """
        UPDATE agent_conversations AS ac
        SET owner_user_id = bs.user_id,
            owner_membership_id = bs.tenant_membership_id
        FROM browser_sessions AS bs
        JOIN tenant_memberships AS tm ON tm.id = bs.tenant_membership_id
        WHERE bs.id = ac.browser_session_id
          AND tm.user_id = bs.user_id
          AND tm.tenant_id = ac.tenant_id
        """
    )
    _abort_if(
        connection,
        "SELECT count(*) FROM agent_conversations "
        "WHERE owner_user_id IS NULL OR owner_membership_id IS NULL",
        "agent_conversations owner could not be derived from its browser session "
        "and same-tenant membership",
    )
    # Transcript-bearing migrated conversations are already meaningful
    # history; the closed GENERAL category is used (never text-derived).
    op.execute(
        "UPDATE agent_conversations SET title_kind = 'GENERAL' "
        "WHERE json_array_length(turns) > 0"
    )
    op.alter_column("agent_conversations", "owner_user_id", nullable=False)
    op.alter_column("agent_conversations", "owner_membership_id", nullable=False)
    op.create_foreign_key(
        "fk_agent_conversations_owner_user",
        "agent_conversations",
        "users",
        ["owner_user_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_agent_conversations_owner_membership",
        "agent_conversations",
        "tenant_memberships",
        ["owner_membership_id"],
        ["id"],
    )
    op.create_check_constraint(
        "ck_agent_conversation_title_kind",
        "agent_conversations",
        f"title_kind IN ({_TITLE_KINDS})",
    )
    op.create_index(
        "ix_agent_conversations_owner_user_id", "agent_conversations", ["owner_user_id"]
    )
    op.create_index(
        "ix_agent_conversations_owner_membership_id",
        "agent_conversations",
        ["owner_membership_id"],
    )
    op.create_index(
        "ix_agent_conversations_owner_recent",
        "agent_conversations",
        ["tenant_id", "owner_user_id", "owner_membership_id", "updated_at", "id"],
    )

    # 2. Live BrowserSession-bound context.
    op.create_table(
        "agent_conversation_session_contexts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("browser_session_id", sa.Uuid(), nullable=False),
        sa.Column("context_epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("active_result_set_id", sa.Uuid(), nullable=True),
        sa.Column("active_pending_draft_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["agent_conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["browser_session_id"], ["browser_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["active_result_set_id"], ["agent_result_sets.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id",
            "browser_session_id",
            name="uq_agent_conversation_session_context",
        ),
        sa.CheckConstraint("context_epoch >= 1", name="ck_agent_conversation_context_epoch"),
    )
    for column in ("tenant_id", "conversation_id", "browser_session_id"):
        op.create_index(
            f"ix_agent_conversation_session_contexts_{column}",
            "agent_conversation_session_contexts",
            [column],
        )
    # Pending-draft authority is intentionally NOT migrated (NULL): see
    # module docstring.
    op.execute(
        """
        INSERT INTO agent_conversation_session_contexts
            (id, tenant_id, conversation_id, browser_session_id, context_epoch,
             active_result_set_id, active_pending_draft_id, created_at, updated_at)
        SELECT gen_random_uuid(), ac.tenant_id, ac.id, ac.browser_session_id,
               ac.context_epoch, ac.active_result_set_id, NULL, ac.created_at, ac.updated_at
        FROM agent_conversations AS ac
        """
    )

    # 3. ResultSet -> durable conversation binding.
    op.add_column("agent_result_sets", sa.Column("conversation_id", sa.Uuid(), nullable=True))
    op.execute(
        """
        UPDATE agent_result_sets AS ars
        SET conversation_id = ac.id
        FROM agent_conversations AS ac
        WHERE ac.browser_session_id = ars.browser_session_id
          AND ac.tenant_id = ars.tenant_id
        """
    )
    _abort_if(
        connection,
        "SELECT count(*) FROM agent_result_sets WHERE conversation_id IS NULL",
        "agent_result_sets could not be attributed to exactly one same-tenant conversation",
    )
    op.alter_column("agent_result_sets", "conversation_id", nullable=False)
    op.create_foreign_key(
        "fk_agent_result_sets_conversation",
        "agent_result_sets",
        "agent_conversations",
        ["conversation_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_agent_result_sets_conversation_id", "agent_result_sets", ["conversation_id"]
    )

    # 4. Remove the old live-context columns from the durable row.
    op.drop_constraint(
        "fk_agent_conversations_active_result_set", "agent_conversations", type_="foreignkey"
    )
    op.drop_column("agent_conversations", "active_result_set_id")
    op.drop_column("agent_conversations", "context_epoch")
    op.drop_index("ix_agent_conversations_browser_session_id", table_name="agent_conversations")
    op.drop_column("agent_conversations", "browser_session_id")


def downgrade() -> None:
    op.add_column(
        "agent_conversations", sa.Column("browser_session_id", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "agent_conversations",
        sa.Column("context_epoch", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "agent_conversations", sa.Column("active_result_set_id", sa.Uuid(), nullable=True)
    )
    # Lossy: each BrowserSession keeps only its most recently used
    # conversation (see module docstring).
    op.execute(
        """
        UPDATE agent_conversations AS ac
        SET browser_session_id = picked.browser_session_id,
            context_epoch = picked.context_epoch,
            active_result_set_id = picked.active_result_set_id
        FROM (
            SELECT DISTINCT ON (browser_session_id)
                   conversation_id, browser_session_id, context_epoch, active_result_set_id
            FROM agent_conversation_session_contexts
            ORDER BY browser_session_id, updated_at DESC, id DESC
        ) AS picked
        WHERE picked.conversation_id = ac.id
        """
    )
    op.drop_index("ix_agent_result_sets_conversation_id", table_name="agent_result_sets")
    op.drop_constraint(
        "fk_agent_result_sets_conversation", "agent_result_sets", type_="foreignkey"
    )
    op.drop_column("agent_result_sets", "conversation_id")
    op.execute("DELETE FROM agent_conversations WHERE browser_session_id IS NULL")
    op.drop_table("agent_conversation_session_contexts")
    op.alter_column("agent_conversations", "browser_session_id", nullable=False)
    op.create_foreign_key(
        None,
        "agent_conversations",
        "browser_sessions",
        ["browser_session_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_agent_conversations_browser_session_id",
        "agent_conversations",
        ["browser_session_id"],
        unique=True,
    )
    op.create_foreign_key(
        "fk_agent_conversations_active_result_set",
        "agent_conversations",
        "agent_result_sets",
        ["active_result_set_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.drop_index("ix_agent_conversations_owner_recent", table_name="agent_conversations")
    op.drop_index(
        "ix_agent_conversations_owner_membership_id", table_name="agent_conversations"
    )
    op.drop_index("ix_agent_conversations_owner_user_id", table_name="agent_conversations")
    op.drop_constraint(
        "ck_agent_conversation_title_kind", "agent_conversations", type_="check"
    )
    op.drop_constraint(
        "fk_agent_conversations_owner_membership", "agent_conversations", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_agent_conversations_owner_user", "agent_conversations", type_="foreignkey"
    )
    op.drop_column("agent_conversations", "title_kind")
    op.drop_column("agent_conversations", "owner_membership_id")
    op.drop_column("agent_conversations", "owner_user_id")
