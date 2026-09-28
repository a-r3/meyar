import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from meyar.db import Base


class AgentResultSet(Base):
    """Server-owned, tenant/session/context-epoch-scoped authority for one
    SEARCH_CANDIDATES tool call's ordered results (issue #49, replaces the
    ordinal-memory column ``AgentConversation.last_search_candidate_ids``).

    A ``candidate_ref`` the model produces is never trusted directly — it
    is resolved through ``AgentResultSetMember.ordinal`` against exactly
    one ``AgentResultSet`` row: the one this conversation's own
    ``active_result_set_id`` currently points at, and only while every one
    of tenant/session/context-epoch/expiry/corpus-freshness still holds
    (see meyar.services.agent_result_set_repo.resolve_active_candidate_ref).

    No ``CandidateIdentity`` field is ever stored here or in
    ``AgentResultSetMember`` — membership, ordering, and staleness are
    professional-fact/provenance concepts only (D-0xx, docs/DECISIONS.md).
    ``canonical_search_request`` is DB-only persisted provenance (the
    validated ``CandidateSearchRequest.model_dump(mode="json")``, including
    the HR-authored semantic_query text) — it must never be copied into
    audit metadata (see docs/SECURITY_PRIVACY.md)."""

    __tablename__ = "agent_result_sets"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    browser_session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("browser_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The owning conversation's context_epoch AT THE MOMENT this result set
    # was created — resolution requires this to still equal the live
    # conversation's own context_epoch (see AgentConversation.context_epoch
    # docstring); a "Yeni söhbət" reset always invalidates every previously
    # created result set for this session, even one still pointed at by a
    # tampered/stale active_result_set_id.
    context_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # DB-only provenance — the validated CandidateSearchRequest this result
    # set was produced from. Never read into an audit metadata dict, never
    # shown to the model. See class docstring.
    canonical_search_request: Mapped[dict] = mapped_column(JSON, nullable=False)
    planner_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    planner_prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    planner_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    planner_model_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    planner_model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    planner_model_revision: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    search_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    search_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # sha256 over sorted (candidate_id, current_profile_version_id[,
    # compatible_embedding_version_id]) tuples for this tenant/mode at
    # creation time — never over profile content/CV text/CandidateIdentity.
    # See meyar.services.agent_result_set_repo.compute_corpus_fingerprint.
    corpus_fingerprint_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AgentResultSetMember(Base):
    """One ranked member of an AgentResultSet — the ordinal a candidate_ref
    resolves against. ``result_set_id`` safely CASCADEs (deleting the
    whole result set legitimately deletes its own membership rows).

    ``candidate_id``, ``candidate_profile_version_id``, and
    ``candidate_embedding_version_id`` are DELIBERATELY plain UUID columns
    with NO ForeignKey constraint against candidates/candidate_profile_
    versions/candidate_embedding_versions: this is an immutable snapshot
    reference to what search returned at creation time, not a live
    relationship. A later hard candidate delete (meyar.services.
    candidate_service.delete_candidate_cascade) must never be blocked by,
    and must never cascade-delete or silently renumber, a historical
    membership row here — the corpus-fingerprint staleness check (see
    AgentResultSet.corpus_fingerprint_sha256) is what detects a member
    whose candidate/profile/embedding no longer exists or is no longer
    current, not a DB foreign key."""

    __tablename__ = "agent_result_set_members"
    __table_args__ = (
        UniqueConstraint("result_set_id", "ordinal", name="uq_agent_result_set_member_ordinal"),
        UniqueConstraint(
            "result_set_id", "candidate_id", name="uq_agent_result_set_member_candidate"
        ),
        CheckConstraint("ordinal >= 1", name="ck_agent_result_set_member_ordinal_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    result_set_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_result_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    # Immutable snapshot references — NOT ForeignKey columns. See class
    # docstring above.
    candidate_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    candidate_profile_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    candidate_embedding_version_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    relevance_score: Mapped[float] = mapped_column(Float, nullable=False)
    structured_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    semantic_score: Mapped[float | None] = mapped_column(Float, nullable=True)
