"""ResultSet member-snapshot policy; legacy corpus fingerprint (issue #86)

Revision ID: f3a9c6d2e815
Revises: e5d7a3c91b04
Create Date: 2026-09-30

``agent_result_sets``:

- adds ``snapshot_policy_version`` (VARCHAR(32) NOT NULL): the explicit
  policy that judges a row's validity. ``member-snapshot-v1`` = ownership +
  expiry + each member's OWN current professional authority (docs/
  DECISIONS.md D-090). No tenant-wide corpus fingerprint is involved.
- makes ``corpus_fingerprint_sha256`` NULLABLE and LEGACY: never computed,
  compared or trusted any more; new rows store NULL.

Backfill (truthful, nothing fabricated): every pre-existing row is set to
``member-snapshot-v1``. This is representable because every pre-#86 row —
SEARCH and REFINEMENT alike — already persists, per member, the immutable
``candidate_id`` + ``candidate_profile_version_id`` (+
``candidate_embedding_version_id`` for semantic/hybrid) snapshot references
that search itself returned, plus its own persisted EmbeddingSearchConfig;
member-snapshot validation needs nothing else. Old rows keep their
historical fingerprint value untouched.

Downgrade policy (fail closed): the pre-#86 code requires a non-null
tenant fingerprint to validate a ResultSet. Rows created after this upgrade
have none, and inventing one would fabricate authority. The downgrade
therefore refuses — before any DDL — while ANY row has a NULL fingerprint;
otherwise it drops the policy column and restores NOT NULL.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f3a9c6d2e815"
down_revision: str | None = "e5d7a3c91b04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SNAPSHOT_POLICY_VERSION = "member-snapshot-v1"


def upgrade() -> None:
    op.add_column(
        "agent_result_sets",
        sa.Column("snapshot_policy_version", sa.String(length=32), nullable=True),
    )
    op.execute(
        sa.text("UPDATE agent_result_sets SET snapshot_policy_version = :policy").bindparams(
            policy=SNAPSHOT_POLICY_VERSION
        )
    )
    op.alter_column("agent_result_sets", "snapshot_policy_version", nullable=False)
    op.alter_column(
        "agent_result_sets",
        "corpus_fingerprint_sha256",
        existing_type=sa.String(length=64),
        nullable=True,
    )


def downgrade() -> None:
    connection = op.get_bind()
    unrepresentable = connection.execute(
        sa.text(
            "SELECT count(*) FROM agent_result_sets WHERE corpus_fingerprint_sha256 IS NULL"
        )
    ).scalar_one()
    if unrepresentable:
        raise RuntimeError(
            f"{unrepresentable} agent_result_sets row(s) were created under the member-"
            "snapshot policy and carry no legacy tenant corpus fingerprint; f3a9c6d2e815 "
            "downgrade would have to fabricate authority (issue #86, D-090) — refusing."
        )
    op.alter_column(
        "agent_result_sets",
        "corpus_fingerprint_sha256",
        existing_type=sa.String(length=64),
        nullable=False,
    )
    op.drop_column("agent_result_sets", "snapshot_policy_version")
