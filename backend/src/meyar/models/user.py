import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from meyar.db import Base


class User(Base):
    """A human identity, independent of any tenant. Authorization for a
    specific tenant is granted only through a TenantMembership row — a
    User by itself grants no access. Only an Argon2id hash is ever
    persisted; see meyar.core.password. Never carries email/phone/PII —
    `username` is a login identifier, not CandidateIdentity data, and is
    never used as a matching/ranking signal (it isn't candidate data at
    all)."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
