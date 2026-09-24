import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from meyar.db import Base

PHOTO_AVAILABLE = "AVAILABLE"
PHOTO_NO_PHOTO = "NO_PHOTO"
PHOTO_AMBIGUOUS = "AMBIGUOUS"
PHOTO_UNUSABLE = "UNUSABLE"
PHOTO_EXTRACTION_FAILED = "EXTRACTION_FAILED"
PHOTO_EXTRACTOR_VERSION = "photo-v1"


class CandidatePhotoVersion(Base):
    """Immutable, identity-only presentation outcome for an exact stored CV."""

    __tablename__ = "candidate_photo_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id", "candidate_document_id"],
            [
                "candidate_documents.tenant_id",
                "candidate_documents.candidate_id",
                "candidate_documents.id",
            ],
            ondelete="CASCADE",
            name="fk_photo_exact_document",
        ),
        UniqueConstraint("candidate_id", "version_number", name="uq_candidate_photo_version"),
        UniqueConstraint(
            "candidate_document_id", "extractor_version", name="uq_photo_document_extractor"
        ),
        CheckConstraint("version_number > 0", name="ck_photo_positive_version"),
        CheckConstraint(
            "status IN ('AVAILABLE','NO_PHOTO','AMBIGUOUS','UNUSABLE','EXTRACTION_FAILED')",
            name="ck_photo_status",
        ),
        CheckConstraint(
            "(status = 'AVAILABLE' AND derived_storage_key IS NOT NULL "
            "AND derived_sha256 IS NOT NULL "
            "AND mime_type = 'image/jpeg' AND width > 0 AND height > 0) OR "
            "(status <> 'AVAILABLE' AND derived_storage_key IS NULL AND derived_sha256 IS NULL "
            "AND mime_type IS NULL AND width IS NULL AND height IS NULL)",
            name="ck_photo_asset_consistency",
        ),
        Index(
            "ix_photo_document_extractor", "tenant_id", "candidate_document_id", "extractor_version"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    candidate_document_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(64))
    source_kind: Mapped[str | None] = mapped_column(String(16))
    source_locator: Mapped[str | None] = mapped_column(String(255))
    source_page: Mapped[int | None] = mapped_column(Integer)
    source_image_sha256: Mapped[str | None] = mapped_column(String(64))
    derived_storage_key: Mapped[str | None] = mapped_column(String(255))
    derived_sha256: Mapped[str | None] = mapped_column(String(64))
    mime_type: Mapped[str | None] = mapped_column(String(32))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    extractor_version: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
