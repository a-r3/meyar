"""add candidate identity and embedding versions

Revision ID: 7aae8da26969
Revises: bd1b929cd874
Create Date: 2026-08-23 05:04:25.321647

"""
from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '7aae8da26969'
down_revision: str | Sequence[str] | None = 'bd1b929cd874'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Required for the candidate_embedding_versions.embedding column
    # below (Slice 7 — local embeddings). See docs/DECISIONS.md.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        'candidate_identity_versions',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('candidate_id', sa.Uuid(), nullable=False),
        sa.Column('candidate_document_id', sa.Uuid(), nullable=False),
        sa.Column('canonical_document_id', sa.Uuid(), nullable=False),
        sa.Column('source_sha256', sa.String(length=64), nullable=False),
        sa.Column('version_number', sa.Integer(), nullable=False),
        sa.Column('schema_version', sa.String(length=32), nullable=False),
        sa.Column('prompt_version', sa.String(length=64), nullable=False),
        sa.Column('model_provider', sa.String(length=32), nullable=False),
        sa.Column('model_name', sa.String(length=128), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('error_code', sa.String(length=64), nullable=True),
        sa.Column('error_message', sa.String(length=500), nullable=True),
        sa.Column('identity_content', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['candidate_document_id'], ['candidate_documents.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['candidate_id'], ['candidates.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['canonical_document_id'], ['canonical_documents.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('candidate_id', 'version_number', name='uq_candidate_identity_version'),
    )
    op.create_index(op.f('ix_candidate_identity_versions_candidate_id'), 'candidate_identity_versions', ['candidate_id'], unique=False)
    op.create_index(op.f('ix_candidate_identity_versions_tenant_id'), 'candidate_identity_versions', ['tenant_id'], unique=False)

    op.create_table(
        'candidate_embedding_versions',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('candidate_id', sa.Uuid(), nullable=False),
        sa.Column('candidate_profile_version_id', sa.Uuid(), nullable=False),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('model_name', sa.String(length=128), nullable=False),
        sa.Column('model_revision', sa.String(length=64), nullable=False),
        sa.Column('serializer_version', sa.String(length=64), nullable=False),
        sa.Column('source_sha256', sa.String(length=64), nullable=False),
        sa.Column('embedding_dimensions', sa.Integer(), nullable=False),
        # Dimension-agnostic vector() column — no fixed length. The
        # final production embedding model/dimension is not approved
        # yet; a physical ANN index is deferred until it is (Slice 7).
        sa.Column('embedding', pgvector.sqlalchemy.Vector(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['candidate_id'], ['candidates.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['candidate_profile_version_id'], ['candidate_profile_versions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'tenant_id', 'candidate_profile_version_id', 'provider', 'model_name', 'model_revision',
            'serializer_version', 'source_sha256',
            name='uq_candidate_embedding_version',
        ),
    )
    op.create_index(op.f('ix_candidate_embedding_versions_candidate_id'), 'candidate_embedding_versions', ['candidate_id'], unique=False)
    op.create_index(op.f('ix_candidate_embedding_versions_candidate_profile_version_id'), 'candidate_embedding_versions', ['candidate_profile_version_id'], unique=False)
    op.create_index(op.f('ix_candidate_embedding_versions_tenant_id'), 'candidate_embedding_versions', ['tenant_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_candidate_embedding_versions_tenant_id'), table_name='candidate_embedding_versions')
    op.drop_index(op.f('ix_candidate_embedding_versions_candidate_profile_version_id'), table_name='candidate_embedding_versions')
    op.drop_index(op.f('ix_candidate_embedding_versions_candidate_id'), table_name='candidate_embedding_versions')
    op.drop_table('candidate_embedding_versions')
    op.drop_index(op.f('ix_candidate_identity_versions_tenant_id'), table_name='candidate_identity_versions')
    op.drop_index(op.f('ix_candidate_identity_versions_candidate_id'), table_name='candidate_identity_versions')
    op.drop_table('candidate_identity_versions')
    # Extension intentionally left installed on downgrade — dropping it
    # is unsafe if any other object in this database still depends on
    # the vector type, and CREATE EXTENSION IF NOT EXISTS on the next
    # upgrade is a no-op either way.
