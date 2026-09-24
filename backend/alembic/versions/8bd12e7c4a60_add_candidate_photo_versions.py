"""Add immutable candidate photo outcomes with exact document provenance.

Revision ID: 8bd12e7c4a60
Revises: 6f4c2a9d8e10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "8bd12e7c4a60"
down_revision: str | Sequence[str] | None = "6f4c2a9d8e10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_candidate_document_photo_parent",
        "candidate_documents",
        ["tenant_id", "candidate_id", "id"],
    )
    op.create_table(
        "candidate_photo_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_document_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(64)),
        sa.Column("source_kind", sa.String(16)),
        sa.Column("source_locator", sa.String(255)),
        sa.Column("source_page", sa.Integer()),
        sa.Column("source_image_sha256", sa.String(64)),
        sa.Column("derived_storage_key", sa.String(255)),
        sa.Column("derived_sha256", sa.String(64)),
        sa.Column("mime_type", sa.String(32)),
        sa.Column("width", sa.Integer()),
        sa.Column("height", sa.Integer()),
        sa.Column("extractor_version", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id", "candidate_document_id"],
            ["candidate_documents.tenant_id", "candidate_documents.candidate_id", "candidate_documents.id"],
            ondelete="CASCADE",
            name="fk_photo_exact_document",
        ),
        sa.UniqueConstraint("candidate_id", "version_number", name="uq_candidate_photo_version"),
        sa.UniqueConstraint("candidate_document_id", "extractor_version", name="uq_photo_document_extractor"),
        sa.CheckConstraint("version_number > 0", name="ck_photo_positive_version"),
        sa.CheckConstraint(
            "status IN ('AVAILABLE','NO_PHOTO','AMBIGUOUS','UNUSABLE','EXTRACTION_FAILED')",
            name="ck_photo_status",
        ),
        sa.CheckConstraint(
            "(status = 'AVAILABLE' AND derived_storage_key IS NOT NULL AND derived_sha256 IS NOT NULL AND mime_type = 'image/jpeg' AND width > 0 AND height > 0) OR (status <> 'AVAILABLE' AND derived_storage_key IS NULL AND derived_sha256 IS NULL AND mime_type IS NULL AND width IS NULL AND height IS NULL)",
            name="ck_photo_asset_consistency",
        ),
    )
    op.create_index(
        "ix_photo_document_extractor", "candidate_photo_versions",
        ["tenant_id", "candidate_document_id", "extractor_version"],
    )


def downgrade() -> None:
    op.drop_index("ix_photo_document_extractor", table_name="candidate_photo_versions")
    op.drop_table("candidate_photo_versions")
    op.drop_constraint("uq_candidate_document_photo_parent", "candidate_documents", type_="unique")
