import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from meyar.db import Base


class FolderSource(Base):
    """A local CV folder an operator has pointed the folder indexer at.
    root_path is a local filesystem path supplied by a trusted local
    operator (CLI) — the same trust boundary as `meyar create-tenant` —
    never a client-controlled value from an untrusted request. One row
    per distinct (tenant_id, root_path); re-running the indexer against
    the same root reuses this row rather than creating a duplicate."""

    __tablename__ = "folder_sources"
    __table_args__ = (
        UniqueConstraint("tenant_id", "root_path", name="uq_folder_sources_tenant_root"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    root_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
