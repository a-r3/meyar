"""add human identity (users, tenant_memberships), migrate browser_sessions
to the human principal, add audit actor identity columns

Revision ID: f4a91c2e6b7d
Revises: db7e4523f491
Create Date: 2026-09-01

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f4a91c2e6b7d"
down_revision: str | Sequence[str] | None = "db7e4523f491"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    op.create_table(
        "tenant_memberships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "tenant_id", name="uq_membership_user_tenant"),
    )
    op.create_index("ix_tenant_memberships_user_id", "tenant_memberships", ["user_id"])
    op.create_index("ix_tenant_memberships_tenant_id", "tenant_memberships", ["tenant_id"])

    # browser_sessions previously carried api_key_id (machine-bridged UI
    # login). That exchange path is retired by this slice — /ui/login no
    # longer accepts an API key — so no code path can produce a row this
    # migration would need to translate. Existing rows are session state,
    # not durable identity data: they are short-lived (8h TTL) and fully
    # revocable, so clearing them only forces an affected browser to log
    # in again — a safe, explicit, fail-closed effect, not data loss. See
    # docs/DECISIONS.md for this slice's record.
    op.execute("DELETE FROM browser_sessions")
    op.drop_index("ix_browser_sessions_api_key_id", table_name="browser_sessions")
    op.drop_constraint(
        "browser_sessions_api_key_id_fkey", "browser_sessions", type_="foreignkey"
    )
    op.drop_column("browser_sessions", "api_key_id")
    op.add_column("browser_sessions", sa.Column("user_id", sa.Uuid(), nullable=False))
    op.add_column(
        "browser_sessions", sa.Column("tenant_membership_id", sa.Uuid(), nullable=False)
    )
    op.create_foreign_key(
        "browser_sessions_user_id_fkey",
        "browser_sessions",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "browser_sessions_tenant_membership_id_fkey",
        "browser_sessions",
        "tenant_memberships",
        ["tenant_membership_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_browser_sessions_user_id", "browser_sessions", ["user_id"])
    op.create_index(
        "ix_browser_sessions_tenant_membership_id", "browser_sessions", ["tenant_membership_id"]
    )

    op.add_column("audit_events", sa.Column("actor_type", sa.String(length=32), nullable=True))
    op.add_column("audit_events", sa.Column("actor_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column("audit_events", "actor_id")
    op.drop_column("audit_events", "actor_type")

    op.execute("DELETE FROM browser_sessions")
    op.drop_index("ix_browser_sessions_tenant_membership_id", table_name="browser_sessions")
    op.drop_index("ix_browser_sessions_user_id", table_name="browser_sessions")
    op.drop_constraint(
        "browser_sessions_tenant_membership_id_fkey", "browser_sessions", type_="foreignkey"
    )
    op.drop_constraint("browser_sessions_user_id_fkey", "browser_sessions", type_="foreignkey")
    op.drop_column("browser_sessions", "tenant_membership_id")
    op.drop_column("browser_sessions", "user_id")
    op.add_column("browser_sessions", sa.Column("api_key_id", sa.Uuid(), nullable=False))
    op.create_foreign_key(
        "browser_sessions_api_key_id_fkey",
        "browser_sessions",
        "api_keys",
        ["api_key_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_browser_sessions_api_key_id", "browser_sessions", ["api_key_id"])

    op.drop_index("ix_tenant_memberships_tenant_id", table_name="tenant_memberships")
    op.drop_index("ix_tenant_memberships_user_id", table_name="tenant_memberships")
    op.drop_table("tenant_memberships")

    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")
