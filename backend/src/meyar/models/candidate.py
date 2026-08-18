import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from meyar.db import Base

CANDIDATE_STATUS_ACTIVE = "ACTIVE"


class Candidate(Base):
    """Deliberately minimal — this slice stores no name/email/phone.
    Identity fields belong to a future CandidateIdentity extension; the CV
    document itself is the source of candidate information. See
    docs/MASTER_SPEC.md §5 (CandidateIdentity vs CandidateProfile)."""

    __tablename__ = "candidates"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=CANDIDATE_STATUS_ACTIVE)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
