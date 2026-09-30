import uuid
from datetime import datetime
from enum import StrEnum

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


class AgentResultSetKind(StrEnum):
    """Distinguishes a root ``SEARCH_CANDIDATES``-produced result set from
    one derived by a ``REFINE_CANDIDATE_RESULTS`` turn (issue #49 PR49-2).
    Every row created before this kind existed is backfilled to ``SEARCH``
    with ``parent_result_set_id=NULL`` (see the chained Alembic migration)
    — a SEARCH row's own search provenance was always genuine root
    provenance, never a refinement's."""

    SEARCH = "SEARCH"
    REFINEMENT = "REFINEMENT"


class AgentResultSet(Base):
    """Server-owned, tenant/session/context-epoch-scoped authority for one
    SEARCH_CANDIDATES tool call's ordered results (issue #49, replaces the
    ordinal-memory column ``AgentConversation.last_search_candidate_ids``),
    OR one REFINE_CANDIDATE_RESULTS turn's derived subset of an existing
    active result set's own members (issue #49 PR49-2, ``result_set_kind ==
    REFINEMENT``).

    A ``candidate_ref`` the model produces is never trusted directly — it
    is resolved through ``AgentResultSetMember.ordinal`` against exactly
    one ``AgentResultSet`` row: the one the BrowserSession-bound
    AgentConversationSessionContext's ``active_result_set_id`` currently
    points at (issue #80), and only while every one of tenant/session/
    conversation/context-epoch/expiry still holds AND the referenced
    member's own snapshot authority is unchanged (issue #86 — an immutable
    search snapshot, never a live mirror of the tenant corpus; see
    meyar.services.agent_result_set_repo.resolve_active_candidate_ref).
    A REFINEMENT row resolves through the EXACT SAME check — it is just
    another AgentResultSet row, never a second authority definition.

    No ``CandidateIdentity`` field is ever stored here or in
    ``AgentResultSetMember`` — membership, ordering, and staleness are
    professional-fact/provenance concepts only (D-0xx, docs/DECISIONS.md).

    ``canonical_search_request``/``planner_*``/``search_policy_version``/
    ``search_mode`` are always the ROOT search's own provenance — for a
    REFINEMENT row these are copied verbatim from the parent at creation
    time (see meyar.services.agent_result_set_repo.
    create_result_set_from_refinement), NEVER overwritten with the
    refinement's own filter text, so "what search produced this context"
    is always answerable from one row without walking the parent chain.
    ``canonical_search_request``/``canonical_refinement_request`` are
    DB-only persisted provenance — never copied into audit metadata (see
    docs/SECURITY_PRIVACY.md)."""

    __tablename__ = "agent_result_sets"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    browser_session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("browser_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # issue #80: explicit durable-conversation binding. One BrowserSession
    # may now hold several conversations' live contexts at the same epoch,
    # so session+epoch alone no longer identify the owning candidate
    # universe — active-set validation additionally requires this to equal
    # the resolving session context's own conversation_id (defense in
    # depth, never a replacement for the session/epoch checks).
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The owning session context's context_epoch AT THE MOMENT this result set
    # was created — resolution requires this to still equal the live
    # session context's own context_epoch (see
    # AgentConversationSessionContext), so a stale/tampered
    # active_result_set_id from another epoch never resolves.
    context_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    # SEARCH (default) for a root SEARCH_CANDIDATES result, REFINEMENT for
    # one derived by REFINE_CANDIDATE_RESULTS. See AgentResultSetKind and
    # the class docstring's provenance-copying rule.
    result_set_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AgentResultSetKind.SEARCH.value,
        server_default=AgentResultSetKind.SEARCH.value,
    )
    # Deliberately a plain immutable UUID snapshot reference — NOT a
    # self-referential ForeignKey (mirrors the AgentResultSetMember
    # candidate_id/profile/embedding snapshot-reference pattern below): a
    # REFINEMENT row's own provenance chain must never be corrupted by, or
    # block, a later deletion of an ancestor row. NULL for a SEARCH row.
    parent_result_set_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
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
    # LEGACY (issue #86, docs/DECISIONS.md D-090): a sha256 over the whole
    # tenant's current searchable corpus, recorded by ResultSets created
    # before #86. It is no longer computed, compared or trusted: validity is
    # member-scoped (``snapshot_policy_version``). Kept NULL for new rows and
    # left untouched on old rows (historical provenance, never fabricated).
    corpus_fingerprint_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # issue #86: the explicit policy that judges this row's validity —
    # ``member-snapshot-v1``: ownership + expiry + each member's OWN current
    # professional authority (meyar.services.agent_result_set_repo.
    # validate_member_snapshots). Unrelated corpus changes never stale it.
    snapshot_policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # REFINEMENT-only provenance below — NULL for a SEARCH row. Never the
    # HR-authored filter text itself in refinement_request_sha256 (a sha256
    # over the canonical serialization, matching request_sha256's own
    # discipline); canonical_refinement_request is the DB-only validated
    # {"filter_request": <CandidateSearchRequest|None>, "limit": <int|None>}
    # shape a refinement was actually computed from — same "never copied
    # into audit metadata" boundary as canonical_search_request.
    refinement_request_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    canonical_refinement_request: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    refinement_policy_version: Mapped[str | None] = mapped_column(String(64), nullable=True)


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
    membership row here — member-scoped snapshot validation (issue #86,
    meyar.services.agent_result_set_repo.validate_member_snapshots) is what
    detects a member whose candidate/profile/embedding no longer exists or
    is no longer current, not a DB foreign key."""

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
