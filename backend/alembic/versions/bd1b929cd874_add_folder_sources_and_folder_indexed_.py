"""add folder_sources and folder_indexed_files

Revision ID: bd1b929cd874
Revises: dbd4552068d0
Create Date: 2026-08-23 00:30:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'bd1b929cd874'
down_revision: str | Sequence[str] | None = 'dbd4552068d0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'folder_sources',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('root_path', sa.String(length=1024), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'root_path', name='uq_folder_sources_tenant_root'),
    )
    op.create_index(op.f('ix_folder_sources_tenant_id'), 'folder_sources', ['tenant_id'], unique=False)

    op.create_table(
        'folder_indexed_files',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('folder_source_id', sa.Uuid(), nullable=False),
        sa.Column('relative_path', sa.String(length=1024), nullable=False),
        sa.Column('document_type', sa.String(length=16), nullable=False),
        sa.Column('byte_size', sa.Integer(), nullable=False),
        sa.Column('sha256_hash', sa.String(length=64), nullable=False),
        sa.Column('index_status', sa.String(length=16), nullable=False),
        sa.Column('failure_code', sa.String(length=64), nullable=True),
        sa.Column('failure_message', sa.String(length=500), nullable=True),
        sa.Column('candidate_id', sa.Uuid(), nullable=True),
        sa.Column('candidate_document_id', sa.Uuid(), nullable=True),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['candidate_document_id'], ['candidate_documents.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['candidate_id'], ['candidates.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['folder_source_id'], ['folder_sources.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'tenant_id', 'folder_source_id', 'relative_path', name='uq_folder_indexed_files_path'
        ),
    )
    op.create_index(
        op.f('ix_folder_indexed_files_candidate_id'), 'folder_indexed_files', ['candidate_id'], unique=False
    )
    op.create_index(
        op.f('ix_folder_indexed_files_folder_source_id'), 'folder_indexed_files', ['folder_source_id'], unique=False
    )
    op.create_index(
        op.f('ix_folder_indexed_files_sha256_hash'), 'folder_indexed_files', ['sha256_hash'], unique=False
    )
    op.create_index(
        op.f('ix_folder_indexed_files_tenant_id'), 'folder_indexed_files', ['tenant_id'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_folder_indexed_files_tenant_id'), table_name='folder_indexed_files')
    op.drop_index(op.f('ix_folder_indexed_files_sha256_hash'), table_name='folder_indexed_files')
    op.drop_index(op.f('ix_folder_indexed_files_folder_source_id'), table_name='folder_indexed_files')
    op.drop_index(op.f('ix_folder_indexed_files_candidate_id'), table_name='folder_indexed_files')
    op.drop_table('folder_indexed_files')
    op.drop_index(op.f('ix_folder_sources_tenant_id'), table_name='folder_sources')
    op.drop_table('folder_sources')
