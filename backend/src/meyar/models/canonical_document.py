import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from meyar.db import Base


class CanonicalDocument(Base):
    """A parser's structured output for one CandidateDocument. Never
    overwrites the source bytes (see DocumentStorage) — kept as its own
    row, separate from CandidateDocument, so a future reprocessing with a
    different parser/version can add a new row without destroying
    provenance of the original. See docs/MASTER_SPEC.md §12-13."""

    __tablename__ = "canonical_documents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidate_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parser_name: Mapped[str] = mapped_column(String(128), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(32), nullable=False)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    content: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
