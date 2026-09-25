import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from meyar.db import Base

DOCUMENT_STATUS_ACTIVE = "ACTIVE"

PARSER_STATUS_PENDING = "PENDING"
PARSER_STATUS_PARSED = "PARSED"
PARSER_STATUS_PARSE_FAILED = "PARSE_FAILED"


class CandidateDocument(Base):
    """Metadata for one uploaded CV. The raw bytes live only in
    DocumentStorage, addressed by storage_key — never a real filesystem
    path derived from original_filename. original_filename is retained for
    display only; it never influences storage or parsing behavior."""

    __tablename__ = "candidate_documents"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "candidate_id", "id", name="uq_candidate_document_photo_parent"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    document_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=DOCUMENT_STATUS_ACTIVE
    )
    parser_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=PARSER_STATUS_PENDING
    )
    parser_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    parser_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    parse_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parse_error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
