import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from meyar.db import Base

INDEX_STATUS_INDEXED = "INDEXED"
INDEX_STATUS_FAILED = "FAILED"
INDEX_STATUS_MISSING = "MISSING"


class FolderIndexedFile(Base):
    """Per-file indexing state for one path discovered under a
    FolderSource. Identity is (tenant_id, folder_source_id,
    relative_path) — enforced by a unique constraint so repeated scans
    are idempotent and never create duplicate active index rows. Content
    identity is tracked separately via sha256_hash (never mtime alone),
    so a scan can distinguish an unchanged file from a changed one.
    Never stores CV body text or a raw absolute path as primary identity
    — document content lives in CandidateDocument/CanonicalDocument,
    reused unchanged from the direct-upload pipeline. A FAILED re-import
    never clears a previously successful candidate_document_id — the
    last known valid evidence is preserved (see docs/DECISIONS.md)."""

    __tablename__ = "folder_indexed_files"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "folder_source_id", "relative_path", name="uq_folder_indexed_files_path"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    folder_source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("folder_sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    document_type: Mapped[str] = mapped_column(String(16), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    index_status: Mapped[str] = mapped_column(String(16), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("candidates.id", ondelete="SET NULL"), nullable=True, index=True
    )
    candidate_document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("candidate_documents.id", ondelete="SET NULL"), nullable=True
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
