"""Server-owned, tenant/session/context-epoch-scoped candidate ordinal
authority (issue #49) — replaces the old ``AgentConversation.
last_search_candidate_ids`` JSON list.

``resolve_active_candidate_ref`` is the ONLY place a model-produced
``candidate_ref`` (a small ordinal) becomes a real ``candidate_id``. It is
never resolved against anything the model asserts about a candidate_id
directly, and never against another tenant's, another session's, or a
previous conversation epoch's rows — see its own docstring for the exact
check order.

This module never imports ``CandidateIdentity`` — membership, ordering,
and staleness are professional-fact/provenance concepts only (see
docs/SECURITY_PRIVACY.md)."""

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from meyar.agent.schemas import MAX_CANDIDATE_REF
from meyar.embedding.serializer import build_professional_embedding_text, compute_source_sha256
from meyar.models.agent_conversation import (
    AgentConversationSessionContext,
    SessionContextAuthority,
)
from meyar.models.agent_result_set import AgentResultSet, AgentResultSetKind, AgentResultSetMember
from meyar.schemas.candidate_profile import CandidateProfileExtraction
from meyar.search.planner_schemas import PlannedCandidateSearchResponse
from meyar.search.schemas import CandidateSearchRequest, SearchMode
from meyar.search.structured import evaluate_required_filters
from meyar.services.audit_repo import record_event, record_events
from meyar.services.browser_session_repo import get_browser_session_by_id
from meyar.services.candidate_embedding_repo import get_embedding_versions_by_ids
from meyar.services.candidate_profile_repo import get_effective_profile_versions_for_candidates
from meyar.services.profile_authority import ProfileAuthorityError, authorize_profile_versions

# issue #49 PR49-2 — the deterministic policy REFINE_CANDIDATE_RESULTS
# applies: derived membership is always a subset of the active result
# set's own members in their existing order (filter never reorders,
# limit only truncates); bump when that policy itself changes, never for
# an unrelated code change. See create_result_set_from_refinement.
REFINEMENT_POLICY_VERSION = "agent-refinement-policy-v1"


# issue #86 (docs/DECISIONS.md D-090): an AgentResultSet is an IMMUTABLE
# SNAPSHOT of the ordered members one accepted search/refinement returned.
# Its validity is its own ownership (tenant/session/conversation/epoch/
# expiry) plus the CURRENT professional authority of its OWN members —
# never "nothing anywhere in this tenant changed since the search". Stored
# on every row so the policy that judges it is explicit; bump only when the
# member-snapshot rules themselves change.
SNAPSHOT_POLICY_VERSION = "member-snapshot-v1"

# issue #86 retention (L-6, ResultSet part) — exact owner contract (D-090):
# per (tenant, conversation, BrowserSession) at most 1 ACTIVE ResultSet
# (the one this scope's own session context points at) + at most this many
# INACTIVE ones. A set some OTHER live session context points at is
# independently protected and never counted or deleted. Holds after a
# create + successful Phase B pointer switch AND when the switch fails or
# goes stale (see ``retire_inactive_result_sets``: creation reserves one
# inactive slot for whichever of {new, superseded} set ends up inactive).
MAX_INACTIVE_RESULT_SETS_PER_CONTEXT = 5

# issue #86: hard cap on how many ResultSets ONE retention pass may select
# and delete (enforced by SQL LIMIT, never by slicing in Python). A normal
# search/refinement runs at most one pass, so a historical backlog never
# makes a turn O(backlog); ``retire_next_result_set_batch`` drains the rest
# (ops CLI ``retire-result-sets``). Steady state needs 1 row per creation.
RESULT_SET_RETIRE_BATCH_SIZE = 20


async def validate_member_snapshots(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    result_set: AgentResultSet,
    members: list[AgentResultSetMember],
) -> list[CandidateProfileExtraction] | None:
    """Member-scoped snapshot authority (issue #86). Returns the authorized
    profiles aligned with ``members`` or ``None`` (STALE, fail closed) if
    ANY member's own authority changed. Query shape — independent of tenant
    size and without per-member N+1 (``members`` <= MAX_SEARCH_LIMIT):

    1 query: current profile version for exactly these candidates;
    1 query: their canonical documents (batch evidence authority);
    +1 query only for SEMANTIC_ONLY/HYBRID: the recorded embedding rows.

    A member is valid only while ALL hold:
    - the candidate still exists in this tenant;
    - its effective professional profile version is still exactly the recorded
      ``candidate_profile_version_id`` (a newer COMPLETED version makes it stale;
      same-document failed/manual-review attempts preserve that snapshot; a new
      document with no accepted completion makes it stale, never rewrites it);
    - that profile still passes the SAME professional evidence authority
      as ``authorize_profile_version`` (shared implementation);
    - SEMANTIC_ONLY/HYBRID only: the exact recorded embedding row still
      exists and matches the member's candidate + profile version, the
      ResultSet's persisted EmbeddingSearchConfig (provider/model/revision/
      serializer/dimensions) and the source hash of the recorded profile's
      canonical professional text. STRUCTURED_ONLY never looks at
      embeddings. CandidateIdentity is never read."""
    if not members:
        return []
    current = await get_effective_profile_versions_for_candidates(
        db, tenant_id=tenant_id, candidate_ids={member.candidate_id for member in members}
    )
    versions = []
    for member in members:
        version = current.get(member.candidate_id)
        if version is None or version.id != member.candidate_profile_version_id:
            return None
        versions.append(version)
    authorized = await authorize_profile_versions(db, tenant_id=tenant_id, versions=versions)
    profiles: list[CandidateProfileExtraction] = []
    for version in versions:
        outcome = authorized[version.id]
        if isinstance(outcome, ProfileAuthorityError):
            return None
        profiles.append(outcome)

    if SearchMode(result_set.search_mode) in (SearchMode.SEMANTIC_ONLY, SearchMode.HYBRID):
        config = CandidateSearchRequest.model_validate(
            result_set.canonical_search_request
        ).embedding_config
        embedding_ids = [member.candidate_embedding_version_id for member in members]
        if config is None or any(embedding_id is None for embedding_id in embedding_ids):
            return None
        rows = await get_embedding_versions_by_ids(
            db,
            tenant_id=tenant_id,
            embedding_version_ids={eid for eid in embedding_ids if eid is not None},
        )
        for member, version in zip(members, versions, strict=True):
            assert member.candidate_embedding_version_id is not None
            row = rows.get(member.candidate_embedding_version_id)
            assert version.profile_content is not None
            expected_source = compute_source_sha256(
                build_professional_embedding_text(version.profile_content)
            )
            if (
                row is None
                or row.candidate_id != member.candidate_id
                or row.candidate_profile_version_id != member.candidate_profile_version_id
                or row.provider != config.provider
                or row.model_name != config.model_name
                or row.model_revision != config.model_revision
                or row.serializer_version != config.serializer_version
                or row.embedding_dimensions != config.embedding_dimensions
                or row.source_sha256 != expected_source
            ):
                return None
    return profiles


def _iso(value: datetime) -> str:
    return (value if value.tzinfo is not None else value.replace(tzinfo=UTC)).isoformat()


def _live_pointer_ids(tenant_id: uuid.UUID):  # noqa: ANN202 — SQLAlchemy Select
    """Every ResultSet id ANY session context of this tenant points at."""
    return select(AgentConversationSessionContext.active_result_set_id).where(
        AgentConversationSessionContext.tenant_id == tenant_id,
        AgentConversationSessionContext.active_result_set_id.is_not(None),
    )


async def _select_retirement_batch(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    browser_session_id: uuid.UUID,
    keep_ids: set[uuid.UUID],
    inactive_slots: int,
) -> list[uuid.UUID]:
    """At most ``RESULT_SET_RETIRE_BATCH_SIZE`` ids, bounded in SQL (ids
    only — no ORM rows, no members). Expired rows first (oldest expiry
    first); the rest of the batch is unexpired rows ranked below the newest
    ``inactive_slots`` (OFFSET), i.e. excess over the policy."""
    scope = [
        AgentResultSet.tenant_id == tenant_id,
        AgentResultSet.conversation_id == conversation_id,
        AgentResultSet.browser_session_id == browser_session_id,
        AgentResultSet.id.not_in(_live_pointer_ids(tenant_id)),
    ]
    if keep_ids:
        scope.append(AgentResultSet.id.not_in(keep_ids))
    now = datetime.now(UTC)
    expired = list(
        (
            await db.scalars(
                select(AgentResultSet.id)
                .where(*scope, AgentResultSet.expires_at <= now)
                .order_by(AgentResultSet.expires_at.asc(), AgentResultSet.id.asc())
                .limit(RESULT_SET_RETIRE_BATCH_SIZE)
            )
        ).all()
    )
    room = RESULT_SET_RETIRE_BATCH_SIZE - len(expired)
    if room <= 0:
        return expired
    excess = list(
        (
            await db.scalars(
                select(AgentResultSet.id)
                .where(*scope, AgentResultSet.expires_at > now)
                .order_by(AgentResultSet.created_at.desc(), AgentResultSet.id.desc())
                .offset(inactive_slots)
                .limit(room)
            )
        ).all()
    )
    return expired + excess


async def retire_inactive_result_sets(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    browser_session_id: uuid.UUID,
    keep_ids: set[uuid.UUID],
    inactive_slots: int,
) -> int:
    """ONE bounded retention pass (issue #86, no background service).

    Scope: exactly one (tenant, conversation, BrowserSession). NEVER deletes
    a ResultSet that ANY session context of the tenant still points at
    (live authority — re-checked inside the DELETE itself), nor one in
    ``keep_ids``. Of the rest ("other inactive"), expired rows are retired
    first and unexpired rows beyond the newest ``inactive_slots`` next —
    at most ``RESULT_SET_RETIRE_BATCH_SIZE`` rows per call, selected and
    deleted in SQL (two LIMITed id SELECTs + one DELETE ... RETURNING +
    one batched audit INSERT), so the cost never grows with a backlog.

    Creation passes ``keep_ids`` = {new set, superseded/source set} and
    ``inactive_slots = MAX_INACTIVE_RESULT_SETS_PER_CONTEXT - 1``: after the
    Phase B pointer switch the superseded set becomes the 5th inactive one;
    if the switch fails or goes stale the NEW set is the 5th instead.
    Either way: 1 active + at most 5 inactive (once any historical backlog
    is drained). The ops sweep passes no keep_ids and the full 5 slots.

    Audit truthfulness: one ``agent.result_set.retired`` event per row the
    DELETE actually removed in this transaction (from RETURNING) — a row
    that became protected between selection and delete is neither deleted
    nor reported. Deleting a ResultSet cascades only its own member rows;
    ``parent_result_set_id`` is a plain snapshot UUID (a derived set carries
    its own copied members). Transcript and AuditEvents are untouched; event
    metadata is structural only (ids, hashes, policy versions, mode, counts,
    epoch, timestamps — never query text, canonical request JSON, CV
    content or CandidateIdentity)."""
    if not 0 <= inactive_slots <= MAX_INACTIVE_RESULT_SETS_PER_CONTEXT:
        raise ValueError("inactive_slots outside the retention policy.")
    retire_ids = await _select_retirement_batch(
        db,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        browser_session_id=browser_session_id,
        keep_ids=keep_ids,
        inactive_slots=inactive_slots,
    )
    if not retire_ids:
        return 0
    # Members go by their own ON DELETE CASCADE FK.
    deleted = (
        await db.execute(
            delete(AgentResultSet)
            .where(
                AgentResultSet.tenant_id == tenant_id,
                AgentResultSet.conversation_id == conversation_id,
                AgentResultSet.browser_session_id == browser_session_id,
                AgentResultSet.id.in_(retire_ids),
                AgentResultSet.id.not_in(_live_pointer_ids(tenant_id)),
            )
            .returning(
                AgentResultSet.id,
                AgentResultSet.result_set_kind,
                AgentResultSet.parent_result_set_id,
                AgentResultSet.conversation_id,
                AgentResultSet.context_epoch,
                AgentResultSet.request_sha256,
                AgentResultSet.refinement_request_sha256,
                AgentResultSet.search_policy_version,
                AgentResultSet.refinement_policy_version,
                AgentResultSet.snapshot_policy_version,
                AgentResultSet.search_mode,
                AgentResultSet.result_count,
                AgentResultSet.created_at,
                AgentResultSet.expires_at,
            )
            .execution_options(synchronize_session=False)
        )
    ).all()
    if not deleted:
        return 0
    await record_events(
        db,
        tenant_id=tenant_id,
        event_type="agent.result_set.retired",
        metadata_rows=[
            {
                "result_set_id": str(row.id),
                "result_set_kind": row.result_set_kind,
                "parent_result_set_id": (
                    str(row.parent_result_set_id) if row.parent_result_set_id else None
                ),
                "conversation_id": str(row.conversation_id),
                "context_epoch": row.context_epoch,
                "request_sha256": row.request_sha256,
                "refinement_request_sha256": row.refinement_request_sha256,
                "search_policy_version": row.search_policy_version,
                "refinement_policy_version": row.refinement_policy_version,
                "snapshot_policy_version": row.snapshot_policy_version,
                "search_mode": row.search_mode,
                "result_count": row.result_count,
                "created_at": _iso(row.created_at),
                "expires_at": _iso(row.expires_at),
            }
            for row in deleted
        ],
    )
    # Never leave a deleted row in the identity map (it would look live).
    for row in deleted:
        stale = db.identity_map.get(Session.identity_key(AgentResultSet, row.id))
        if stale is not None:
            db.expunge(stale)
    return len(deleted)


async def _next_retirement_scope(db: AsyncSession, *, tenant_id: uuid.UUID):  # noqa: ANN202
    now = datetime.now(UTC)
    return (
        await db.execute(
            select(AgentResultSet.conversation_id, AgentResultSet.browser_session_id)
            .where(
                AgentResultSet.tenant_id == tenant_id,
                AgentResultSet.id.not_in(_live_pointer_ids(tenant_id)),
            )
            .group_by(AgentResultSet.conversation_id, AgentResultSet.browser_session_id)
            .having(
                or_(
                    func.count() > MAX_INACTIVE_RESULT_SETS_PER_CONTEXT,
                    func.bool_or(AgentResultSet.expires_at <= now),
                )
            )
            .limit(1)
        )
    ).first()


async def result_set_retirement_pending(db: AsyncSession, *, tenant_id: uuid.UUID) -> bool:
    """True when some scope of THIS tenant is still over the policy."""
    return await _next_retirement_scope(db, tenant_id=tenant_id) is not None


async def retire_next_result_set_batch(db: AsyncSession, *, tenant_id: uuid.UUID) -> int:
    """Agentless ops sweep step (issue #86 backlog drain; CLI
    ``retire-result-sets``): finds ONE (conversation, BrowserSession) scope
    of THIS tenant whose unprotected sets exceed the policy (more than
    ``MAX_INACTIVE_RESULT_SETS_PER_CONTEXT``, or any expired) and runs one
    bounded ``retire_inactive_result_sets`` pass on it with the full 5
    inactive slots (the scope's own active set is pointer-protected).
    Returns rows retired; 0 means nothing is pending for this tenant.
    Tenant-scoped by construction — there is no global mode. The caller
    commits after each call."""
    scope = await _next_retirement_scope(db, tenant_id=tenant_id)
    if scope is None:
        return 0
    return await retire_inactive_result_sets(
        db,
        tenant_id=tenant_id,
        conversation_id=scope.conversation_id,
        browser_session_id=scope.browser_session_id,
        keep_ids=set(),
        inactive_slots=MAX_INACTIVE_RESULT_SETS_PER_CONTEXT,
    )


async def create_result_set_from_search(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    browser_session_id: uuid.UUID,
    session_context: SessionContextAuthority,
    planned: PlannedCandidateSearchResponse,
    previous_result_set_id: uuid.UUID | None,
) -> AgentResultSet:
    """Persists one new AgentResultSet + its ordered AgentResultSetMember
    rows from an executable planner+search result, and fires
    ``agent.result_set.created``. Caller's responsibility to only call this
    when ``planned.plan.executable and planned.search_response is not
    None`` — asserted defensively here too.

    issue #80: the new row is bound to the session context's own durable
    ``conversation_id`` + ``browser_session_id`` + ``context_epoch`` — all
    three are copied from the server-resolved live context, never from a
    caller-supplied value, and the context must belong to this tenant and
    this BrowserSession."""
    assert planned.plan.executable and planned.search_response is not None
    request = planned.plan.search_request
    assert request is not None
    if (
        session_context.tenant_id != tenant_id
        or session_context.browser_session_id != browser_session_id
    ):
        raise ValueError("Session context does not belong to this tenant/session.")
    session = await get_browser_session_by_id(db, browser_session_id=browser_session_id)
    assert session is not None, "session context must reference a live BrowserSession"

    # issue #86: no tenant-wide corpus rescan — the accepted search response
    # already carries every member's profile/embedding snapshot ids.
    result_set = AgentResultSet(
        tenant_id=tenant_id,
        browser_session_id=browser_session_id,
        conversation_id=session_context.conversation_id,
        context_epoch=session_context.context_epoch,
        result_set_kind=AgentResultSetKind.SEARCH.value,
        parent_result_set_id=None,
        request_sha256=planned.plan.request_sha256,
        canonical_search_request=request.model_dump(mode="json"),
        planner_policy_version=planned.plan.planner_policy_version,
        planner_prompt_version=planned.plan.prompt_version,
        planner_schema_version=planned.plan.schema_version,
        planner_model_provider=planned.plan.model_provider,
        planner_model_name=planned.plan.model_name,
        planner_model_revision=planned.plan.model_revision,
        search_policy_version=planned.search_response.policy_version,
        search_mode=request.mode.value,
        result_count=planned.search_response.result_count,
        corpus_fingerprint_sha256=None,
        snapshot_policy_version=SNAPSHOT_POLICY_VERSION,
        expires_at=session.expires_at,
    )
    db.add(result_set)
    await db.flush()

    for item in sorted(planned.search_response.results, key=lambda r: r.rank):
        db.add(
            AgentResultSetMember(
                result_set_id=result_set.id,
                ordinal=item.rank,
                candidate_id=item.candidate_id,
                candidate_profile_version_id=item.candidate_profile_version_id,
                candidate_embedding_version_id=item.candidate_embedding_version_id,
                relevance_score=item.relevance_score,
                structured_score=item.structured_score,
                semantic_score=item.semantic_score,
            )
        )
    await db.flush()

    await record_event(
        db,
        tenant_id=tenant_id,
        event_type="agent.result_set.created",
        metadata={
            "result_set_id": str(result_set.id),
            "conversation_id": str(result_set.conversation_id),
            "previous_result_set_id": (
                str(previous_result_set_id) if previous_result_set_id is not None else None
            ),
            "result_count": result_set.result_count,
            "request_sha256": result_set.request_sha256,
            "search_mode": result_set.search_mode,
            "search_policy_version": result_set.search_policy_version,
            "context_epoch": result_set.context_epoch,
        },
    )
    await retire_inactive_result_sets(
        db,
        tenant_id=tenant_id,
        conversation_id=result_set.conversation_id,
        browser_session_id=browser_session_id,
        keep_ids={result_set.id}
        | ({previous_result_set_id} if previous_result_set_id is not None else set()),
        inactive_slots=MAX_INACTIVE_RESULT_SETS_PER_CONTEXT - 1,
    )
    return result_set


class ResultSetResolutionFailure(StrEnum):
    """Typed internal failure reasons for ``resolve_active_candidate_ref``.
    ``STALE`` (issue #86) means a MEMBER's own snapshot authority changed
    (its profile version moved, it lost evidence authority, it was deleted,
    or — semantic/hybrid — its recorded embedding no longer matches);
    unrelated tenant changes never produce it.
    Every member except ``STALE``/``EXPIRED``/``ORDINAL_OUT_OF_RANGE``
    collapses to a fail-closed "not found" outward outcome (see
    meyar.agent.service's mapping to AgentTurnOutcome) — cross-tenant/
    cross-session/cross-conversation/cross-epoch probing must never be able
    to distinguish "exists but not yours" from "does not exist"."""

    NO_ACTIVE_RESULT_SET = "NO_ACTIVE_RESULT_SET"
    NOT_FOUND = "NOT_FOUND"
    SESSION_MISMATCH = "SESSION_MISMATCH"
    # issue #80: the row's durable conversation binding differs from the
    # resolving session context's own conversation (same tenant, same
    # BrowserSession, same epoch is NOT enough).
    CONVERSATION_MISMATCH = "CONVERSATION_MISMATCH"
    CONTEXT_EPOCH_MISMATCH = "CONTEXT_EPOCH_MISMATCH"
    EXPIRED = "EXPIRED"
    STALE = "STALE"
    # issue #86 (D-090): the row is owned by this context but was written
    # under a snapshot policy this code does not implement (anything other
    # than SNAPSHOT_POLICY_VERSION). Fails closed before any member is read;
    # outward it is the same "re-run the search" STALE UX and the closed
    # audit reason "STALE" — the raw policy string is never exposed.
    UNSUPPORTED_SNAPSHOT_POLICY = "UNSUPPORTED_SNAPSHOT_POLICY"
    ORDINAL_OUT_OF_RANGE = "ORDINAL_OUT_OF_RANGE"


# Outward audit-safe reason code for a rejected resolution — collapses the
# richer internal enum above into the four categories docs/DECISIONS.md
# documents for ``agent.result_set.reference_rejected``.
_AUDIT_REASON_BY_FAILURE: dict[ResultSetResolutionFailure, str] = {
    ResultSetResolutionFailure.NO_ACTIVE_RESULT_SET: "NOT_FOUND",
    ResultSetResolutionFailure.NOT_FOUND: "NOT_FOUND",
    ResultSetResolutionFailure.SESSION_MISMATCH: "NOT_FOUND",
    ResultSetResolutionFailure.CONVERSATION_MISMATCH: "NOT_FOUND",
    ResultSetResolutionFailure.CONTEXT_EPOCH_MISMATCH: "NOT_FOUND",
    ResultSetResolutionFailure.EXPIRED: "EXPIRED",
    ResultSetResolutionFailure.STALE: "STALE",
    ResultSetResolutionFailure.UNSUPPORTED_SNAPSHOT_POLICY: "STALE",
    ResultSetResolutionFailure.ORDINAL_OUT_OF_RANGE: "ORDINAL_OUT_OF_RANGE",
}


@dataclass(frozen=True)
class ResolvedCandidateRef:
    candidate_id: uuid.UUID
    result_set_id: uuid.UUID
    context_epoch: int
    candidate_profile_version_id: uuid.UUID


async def _validate_active_result_set(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    browser_session_id: uuid.UUID,
    session_context: SessionContextAuthority,
) -> AgentResultSet | ResultSetResolutionFailure:
    """Steps 1-7 of the ordinal-resolution check order shared by EVERY
    consumer of ``session_context.active_result_set_id`` — a candidate_ref
    lookup (``resolve_active_candidate_ref``) and a REFINE_CANDIDATE_RESULTS
    turn (``create_result_set_from_refinement``) both call this ONE
    function rather than re-implementing the check (issue #49 PR49-2,
    docs/DECISIONS.md D-084: "one authoritative validation definition").
    Never fires an audit event itself — callers own their own audit
    semantics (different event types/metadata for a rejected ordinal vs. a
    rejected refinement); see each caller's own reject helper.

    0. The live session context itself belongs to this tenant and this
       caller's BrowserSession (issue #80).
    1. ``session_context.active_result_set_id`` is set.
    2. The AgentResultSet row exists AND its own ``tenant_id`` equals
       ``tenant_id`` — filtered in ONE query on both columns together, so
       another tenant's row is never even fetched to compare against.
    3. Its ``browser_session_id`` equals this call's ``browser_session_id``.
    4. Its ``conversation_id`` equals ``session_context.conversation_id``
       (issue #80 — cross-conversation pointer swap inside one
       BrowserSession at the same epoch fails closed here).
    5. Its ``context_epoch`` equals ``session_context.context_epoch``.
    6. ``expires_at`` is still in the future.
    7. ``snapshot_policy_version`` is exactly ``SNAPSHOT_POLICY_VERSION``
       (issue #86 fail-closed: an unknown policy is never interpreted).

    STRUCTURAL ownership only (issue #86): no tenant-wide corpus work. Member
    freshness is judged separately, per member, by
    ``validate_member_snapshots`` wherever members are actually used.
    """
    if (
        session_context.tenant_id != tenant_id
        or session_context.browser_session_id != browser_session_id
    ):
        return ResultSetResolutionFailure.SESSION_MISMATCH
    if session_context.active_result_set_id is None:
        return ResultSetResolutionFailure.NO_ACTIVE_RESULT_SET

    result_set = await db.scalar(
        select(AgentResultSet).where(
            AgentResultSet.id == session_context.active_result_set_id,
            AgentResultSet.tenant_id == tenant_id,
        )
    )
    if result_set is None:
        return ResultSetResolutionFailure.NOT_FOUND
    if result_set.browser_session_id != browser_session_id:
        return ResultSetResolutionFailure.SESSION_MISMATCH
    if result_set.conversation_id != session_context.conversation_id:
        return ResultSetResolutionFailure.CONVERSATION_MISMATCH
    if result_set.context_epoch != session_context.context_epoch:
        return ResultSetResolutionFailure.CONTEXT_EPOCH_MISMATCH
    now = datetime.now(UTC)
    expires_at = result_set.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if now >= expires_at:
        return ResultSetResolutionFailure.EXPIRED
    # Checked only AFTER ownership, so a foreign row's policy is never
    # observable (it already failed as NOT_FOUND/mismatch above).
    if result_set.snapshot_policy_version != SNAPSHOT_POLICY_VERSION:
        return ResultSetResolutionFailure.UNSUPPORTED_SNAPSHOT_POLICY

    return result_set


async def resolve_active_candidate_ref(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    browser_session_id: uuid.UUID,
    session_context: SessionContextAuthority,
    candidate_ref: int,
) -> ResolvedCandidateRef | ResultSetResolutionFailure:
    """THE ONLY place a candidate_ref becomes a real candidate_id. Exact
    check order (each step fails closed to a typed
    ResultSetResolutionFailure, never an exception, never a partial
    result):

    1-7. See ``_validate_active_result_set``.
    7. ``candidate_ref`` is within the persisted member ordinal range.
    8. That ONE member's snapshot is still authoritative
       (``validate_member_snapshots``): unchanged current profile version,
       evidence authority, and — semantic/hybrid — its recorded embedding.
       Otherwise STALE. Bounded queries, independent of tenant size.

    Fires ``agent.result_set.reference_resolved`` on success or
    ``agent.result_set.reference_rejected`` on failure — metadata is
    always ids/enums/small ints only, never query/candidate-identity text.
    """
    validated = await _validate_active_result_set(
        db,
        tenant_id=tenant_id,
        browser_session_id=browser_session_id,
        session_context=session_context,
    )
    if isinstance(validated, ResultSetResolutionFailure):
        return await _reject(
            db, tenant_id=tenant_id, session_context=session_context,
            failure=validated, candidate_ref=None,
        )
    result_set = validated

    member = await db.scalar(
        select(AgentResultSetMember).where(
            AgentResultSetMember.result_set_id == result_set.id,
            AgentResultSetMember.ordinal == candidate_ref,
        )
    )
    if member is None:
        return await _reject(
            db, tenant_id=tenant_id, session_context=session_context,
            failure=ResultSetResolutionFailure.ORDINAL_OUT_OF_RANGE, candidate_ref=candidate_ref,
        )

    # issue #86: only THIS member's own snapshot authority decides — never
    # the rest of the tenant. A changed member fails closed as STALE.
    if (
        await validate_member_snapshots(
            db, tenant_id=tenant_id, result_set=result_set, members=[member]
        )
        is None
    ):
        return await _reject(
            db, tenant_id=tenant_id, session_context=session_context,
            failure=ResultSetResolutionFailure.STALE, candidate_ref=None,
        )

    await record_event(
        db,
        tenant_id=tenant_id,
        event_type="agent.result_set.reference_resolved",
        metadata={
            "result_set_id": str(result_set.id),
            "context_epoch": result_set.context_epoch,
            "candidate_ref": candidate_ref,
            "candidate_id": str(member.candidate_id),
            "candidate_profile_version_id": str(member.candidate_profile_version_id),
        },
    )
    return ResolvedCandidateRef(
        candidate_id=member.candidate_id,
        result_set_id=result_set.id,
        context_epoch=result_set.context_epoch,
        candidate_profile_version_id=member.candidate_profile_version_id,
    )


async def _reject(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    session_context: SessionContextAuthority,
    failure: ResultSetResolutionFailure,
    candidate_ref: int | None,
) -> ResultSetResolutionFailure:
    metadata: dict = {
        "reason": _AUDIT_REASON_BY_FAILURE[failure],
        "context_epoch": session_context.context_epoch,
    }
    # candidate_ref is a small HR/model-supplied ordinal, never identity —
    # safe to disclose only for the ORDINAL_OUT_OF_RANGE case where a
    # candidate was never legitimately identified in the first place.
    if candidate_ref is not None:
        metadata["candidate_ref"] = candidate_ref
    await record_event(
        db, tenant_id=tenant_id, event_type="agent.result_set.reference_rejected", metadata=metadata
    )
    return failure


async def active_result_set_size(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    browser_session_id: uuid.UUID,
    session_context: SessionContextAuthority,
) -> int:
    """ADVISORY (issue #86): how many ordinals the model may reference —
    used only to bound ``available_candidate_refs`` for its next decision.
    Structural checks only (tenant/session/conversation/epoch/expiry/
    existence) and the stored bounded ``result_count``; it never validates
    members. A member that became stale still fails closed when it is
    actually resolved. Returns 0 on any failure condition (no active result set,
    stale, expired, wrong session/tenant/epoch) — this is advisory context
    for the model, never itself an authorization decision; the real
    authorization is always resolve_active_candidate_ref, called again
    independently when the model actually references an ordinal. Never
    raises, never leaks which specific failure occurred, and never fires
    an audit event (it is not itself a resolution attempt)."""
    validated = await _validate_active_result_set(
        db,
        tenant_id=tenant_id,
        browser_session_id=browser_session_id,
        session_context=session_context,
    )
    if isinstance(validated, ResultSetResolutionFailure):
        return 0
    return min(validated.result_count, MAX_CANDIDATE_REF)


@dataclass(frozen=True)
class ResultSetInspection:
    """Read-only, audit-free Layer-1 view of the PRE-EXISTING active
    ResultSet (issue #88 slice B, D-092 §11.1), scoped to exactly what a plan
    consumes so issue #86's member-scoped authority is preserved:

    * ``failure``: the structural ``_validate_active_result_set`` outcome
      (missing/foreign/expired/unsupported policy), else ``None``;
    * ``member_count``: the stored snapshot size when structurally valid;
    * ``stale_ordinals``: requested ordinals whose OWN member snapshot is no
      longer authoritative (an unrelated changed member never appears);
    * ``snapshot_stale``: only when the whole snapshot was requested (a
      refinement is a subset of the entire source snapshot).

    Never a substitute for the executors' own checks, which still run as
    TOCTOU defense in depth."""

    failure: ResultSetResolutionFailure | None
    member_count: int = 0
    stale_ordinals: frozenset[int] = frozenset()
    snapshot_stale: bool = False


async def inspect_active_result_set(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    browser_session_id: uuid.UUID,
    session_context: SessionContextAuthority,
    ordinals: frozenset[int] = frozenset(),
    whole_snapshot: bool = False,
) -> ResultSetInspection:
    """Same structural check order as ``resolve_active_candidate_ref`` /
    ``validate_active_result_set_for_refinement`` and the same shared
    ``validate_member_snapshots`` authority — but fires NO audit event and
    resolves nothing. Bounded: one structural query, then at most one member
    batch per requested ordinal (<= plan length) or one whole-snapshot batch."""
    validated = await _validate_active_result_set(
        db,
        tenant_id=tenant_id,
        browser_session_id=browser_session_id,
        session_context=session_context,
    )
    if isinstance(validated, ResultSetResolutionFailure):
        return ResultSetInspection(failure=validated)
    stale: set[int] = set()
    for ordinal in sorted(ordinals):
        if ordinal > validated.result_count:
            continue  # out of range: statically knowable from member_count
        member = await db.scalar(
            select(AgentResultSetMember).where(
                AgentResultSetMember.result_set_id == validated.id,
                AgentResultSetMember.ordinal == ordinal,
            )
        )
        if member is None or (
            await validate_member_snapshots(
                db, tenant_id=tenant_id, result_set=validated, members=[member]
            )
            is None
        ):
            stale.add(ordinal)
    snapshot_stale = False
    if whole_snapshot:
        members = await _ordered_members(db, result_set_id=validated.id)
        snapshot_stale = (
            await validate_member_snapshots(
                db, tenant_id=tenant_id, result_set=validated, members=members
            )
            is None
        )
    return ResultSetInspection(
        failure=None,
        member_count=validated.result_count,
        stale_ordinals=frozenset(stale),
        snapshot_stale=snapshot_stale,
    )


async def record_reference_rejection(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    session_context: SessionContextAuthority,
    failure: ResultSetResolutionFailure,
    candidate_ref: int,
) -> None:
    """The exact ``agent.result_set.reference_rejected`` event
    ``resolve_active_candidate_ref`` fires, for a reference that Layer 1
    rejected before any resolution (issue #88 slice B)."""
    await _reject(
        db, tenant_id=tenant_id, session_context=session_context, failure=failure,
        candidate_ref=(
            candidate_ref if failure == ResultSetResolutionFailure.ORDINAL_OUT_OF_RANGE else None
        ),
    )


async def record_refinement_rejection(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    session_context: SessionContextAuthority,
    failure: ResultSetResolutionFailure,
) -> None:
    """The exact ``agent.result_set.refine_rejected`` event the refinement
    pre-validation fires, for a refinement Layer 1 rejected."""
    await _reject_refinement(
        db, tenant_id=tenant_id, session_context=session_context, failure=failure
    )


async def _ordered_members(
    db: AsyncSession, *, result_set_id: uuid.UUID
) -> list[AgentResultSetMember]:
    rows = await db.execute(
        select(AgentResultSetMember)
        .where(AgentResultSetMember.result_set_id == result_set_id)
        .order_by(AgentResultSetMember.ordinal.asc())
    )
    return list(rows.scalars())


async def _reject_refinement(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    session_context: SessionContextAuthority,
    failure: ResultSetResolutionFailure,
) -> None:
    """Mirrors ``_reject`` for a rejected REFINE_CANDIDATE_RESULTS turn —
    its own event type/metadata shape (never a candidate_ref, which a
    refinement never carries)."""
    await record_event(
        db,
        tenant_id=tenant_id,
        event_type="agent.result_set.refine_rejected",
        metadata={
            "reason": _AUDIT_REASON_BY_FAILURE[failure],
            "context_epoch": session_context.context_epoch,
        },
    )


async def validate_active_result_set_for_refinement(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    browser_session_id: uuid.UUID,
    session_context: SessionContextAuthority,
) -> AgentResultSet | ResultSetResolutionFailure:
    """REFINE_CANDIDATE_RESULTS's own PRE-validation authority (issue #49
    PR49-2 independent-audit correction). ``meyar.agent.service.
    _dispatch_refine`` calls this FIRST, before ever invoking the search
    planner, so a stale/expired/missing/foreign-session active result set
    is rejected with its own truthful reason before any planner call is
    made — never surfaced as a generic planner-unavailable clarification.
    Reuses the exact same ``_validate_active_result_set`` six-step
    definition ``create_result_set_from_refinement`` revalidates
    internally below (TOCTOU defense-in-depth — that second check is
    intentionally kept, not replaced by this one; see its own docstring).
    Fires the SAME ``agent.result_set.refine_rejected`` audit event the
    internal revalidation path fires on failure, so a rejected refinement
    is audited exactly once regardless of which of the two checks actually
    caught it."""
    validated = await _validate_active_result_set(
        db,
        tenant_id=tenant_id,
        browser_session_id=browser_session_id,
        session_context=session_context,
    )
    if isinstance(validated, ResultSetResolutionFailure):
        await _reject_refinement(
            db, tenant_id=tenant_id, session_context=session_context, failure=validated
        )
        return validated
    # issue #86: refinement is a SUBSET of this snapshot, so every source
    # member must still be authoritative — checked in one bounded batch.
    members = await _ordered_members(db, result_set_id=validated.id)
    if (
        await validate_member_snapshots(
            db, tenant_id=tenant_id, result_set=validated, members=members
        )
        is None
    ):
        await _reject_refinement(
            db, tenant_id=tenant_id, session_context=session_context,
            failure=ResultSetResolutionFailure.STALE,
        )
        return ResultSetResolutionFailure.STALE
    return validated


@dataclass(frozen=True)
class RefinementResult:
    """Everything ``meyar.agent.service._dispatch_refine`` needs to build
    the HR-facing ``AgentRefineToolResult`` — never leaves this module with
    less than a fully persisted, audited derived AgentResultSet."""

    result_set: AgentResultSet
    members: list[AgentResultSetMember] = field(default_factory=list)
    source_result_count: int = 0
    limit_truncated: bool = False


def _canonical_refinement_request_json(
    *, filter_request: CandidateSearchRequest | None, requested_limit: int | None
) -> dict:
    return {
        "filter_request": (
            filter_request.model_dump(mode="json") if filter_request is not None else None
        ),
        "limit": requested_limit,
    }


async def create_result_set_from_refinement(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    browser_session_id: uuid.UUID,
    session_context: SessionContextAuthority,
    filter_request: CandidateSearchRequest | None,
    requested_limit: int | None,
) -> RefinementResult | ResultSetResolutionFailure:
    """REFINE_CANDIDATE_RESULTS's own persistence authority (issue #49
    PR49-2). ``filter_request`` is an ALREADY-VALIDATED, already-planned
    ``CandidateSearchRequest`` (STRUCTURED_ONLY only — the caller,
    meyar.agent.service._dispatch_refine, rejects a semantic/hybrid or
    non-executable plan before ever calling this function) whose
    ``required_filters`` this function evaluates directly against the
    active result set's OWN current members — never a fresh tenant-wide
    search, never touching ``preferred_filters`` (no reranking within this
    PR — see docs/DECISIONS.md D-084).

    Authority order:
    1. The active result set passes the exact same 6-step validation
       ``resolve_active_candidate_ref`` uses (``_validate_active_result_set``)
       — STALE/EXPIRED/etc. fail the WHOLE refinement, never partially.
    2. Every source member's own snapshot is batch-validated
       (``validate_member_snapshots``, issue #86): current profile version
       still the recorded one, evidence authority, and — semantic/hybrid —
       the recorded embedding. Any mismatch fails the whole refinement as
       STALE. Unrelated tenant changes never do: a refinement means
       "refine THESE results", never a fresh tenant-wide search.
    3. When ``filter_request`` is given, a member survives only if
       ``evaluate_required_filters`` (the exact Slice 8 structured-search
       gate) is satisfied against its own current profile — the parent's
       own ordinal order is preserved for every survivor (filtering never
       reorders).
    4. ``requested_limit`` truncates that same preserved order — never
       reorders, never fabricates members when the retained set is
       smaller than requested (``RefinementResult.limit_truncated``).

    The derived AgentResultSet's search-provenance fields
    (canonical_search_request/planner_*/search_policy_version/search_mode)
    are copied VERBATIM from the parent — never
    overwritten with the refinement's own filter text — and
    ``expires_at`` is inherited exactly (a refinement never extends
    validity beyond its parent). Fires ``agent.result_set.refined`` on
    success, ``agent.result_set.refine_rejected`` on failure — metadata is
    always ids/enums/counts, never raw filter/query text."""
    validated = await _validate_active_result_set(
        db,
        tenant_id=tenant_id,
        browser_session_id=browser_session_id,
        session_context=session_context,
    )
    if isinstance(validated, ResultSetResolutionFailure):
        await _reject_refinement(
            db, tenant_id=tenant_id, session_context=session_context, failure=validated
        )
        return validated
    source_result_set = validated

    source_members = await _ordered_members(db, result_set_id=source_result_set.id)
    as_of_year = (
        filter_request.as_of_date.year
        if filter_request is not None and filter_request.as_of_date is not None
        else None
    )
    as_of_date = filter_request.as_of_date if filter_request is not None else None

    # issue #86: ONE batch validation of every source member's own snapshot
    # (bounded by the source size, never the tenant). Any changed member
    # fails the WHOLE refinement as STALE — never a partial subset, never a
    # different profile version than the snapshot recorded, never a
    # candidate from outside the parent ResultSet.
    profiles = await validate_member_snapshots(
        db, tenant_id=tenant_id, result_set=source_result_set, members=source_members
    )
    if profiles is None:
        await _reject_refinement(
            db, tenant_id=tenant_id, session_context=session_context,
            failure=ResultSetResolutionFailure.STALE,
        )
        return ResultSetResolutionFailure.STALE

    retained: list[tuple[AgentResultSetMember, list]] = []
    for member, profile in zip(source_members, profiles, strict=True):
        if filter_request is None:
            retained.append((member, []))
            continue
        evaluation = evaluate_required_filters(
            profile, filter_request.required_filters, as_of_year=as_of_year, as_of_date=as_of_date
        )
        if evaluation.satisfied:
            retained.append((member, evaluation.matches))

    source_result_count = len(source_members)
    limit_truncated = requested_limit is not None and requested_limit > len(retained)
    limited = retained[:requested_limit] if requested_limit is not None else retained

    canonical_refinement_request = _canonical_refinement_request_json(
        filter_request=filter_request, requested_limit=requested_limit
    )
    refinement_request_sha256 = hashlib.sha256(
        json.dumps(canonical_refinement_request, sort_keys=True).encode("utf-8")
    ).hexdigest()

    derived = AgentResultSet(
        tenant_id=tenant_id,
        browser_session_id=browser_session_id,
        # Preserved from the (already conversation-validated) parent.
        conversation_id=source_result_set.conversation_id,
        context_epoch=source_result_set.context_epoch,
        result_set_kind=AgentResultSetKind.REFINEMENT.value,
        parent_result_set_id=source_result_set.id,
        # Root search provenance copied verbatim — never overwritten with
        # this refinement's own filter text. See function docstring.
        request_sha256=source_result_set.request_sha256,
        canonical_search_request=source_result_set.canonical_search_request,
        planner_policy_version=source_result_set.planner_policy_version,
        planner_prompt_version=source_result_set.planner_prompt_version,
        planner_schema_version=source_result_set.planner_schema_version,
        planner_model_provider=source_result_set.planner_model_provider,
        planner_model_name=source_result_set.planner_model_name,
        planner_model_revision=source_result_set.planner_model_revision,
        search_policy_version=source_result_set.search_policy_version,
        search_mode=source_result_set.search_mode,
        result_count=len(limited),
        # Legacy tenant-corpus fingerprint is never recomputed (issue #86);
        # a derived set is judged by the same member-snapshot policy.
        corpus_fingerprint_sha256=None,
        snapshot_policy_version=SNAPSHOT_POLICY_VERSION,
        # Never extend validity beyond the parent's own expiry.
        expires_at=source_result_set.expires_at,
        refinement_request_sha256=refinement_request_sha256,
        canonical_refinement_request=canonical_refinement_request,
        refinement_policy_version=REFINEMENT_POLICY_VERSION,
    )
    db.add(derived)
    await db.flush()

    derived_members: list[AgentResultSetMember] = []
    for ordinal, (member, _matches) in enumerate(limited, start=1):
        new_member = AgentResultSetMember(
            result_set_id=derived.id,
            ordinal=ordinal,
            candidate_id=member.candidate_id,
            candidate_profile_version_id=member.candidate_profile_version_id,
            candidate_embedding_version_id=member.candidate_embedding_version_id,
            # Scores/provenance copied unchanged from the source member —
            # a refinement never rescoring/reranks (see function docstring
            # point 3). The LLM never assigns or modifies these.
            relevance_score=member.relevance_score,
            structured_score=member.structured_score,
            semantic_score=member.semantic_score,
        )
        db.add(new_member)
        derived_members.append(new_member)
    await db.flush()

    await record_event(
        db,
        tenant_id=tenant_id,
        event_type="agent.result_set.refined",
        metadata={
            "source_result_set_id": str(source_result_set.id),
            "result_set_id": str(derived.id),
            "context_epoch": derived.context_epoch,
            "source_result_count": source_result_count,
            "result_count": len(limited),
            "refinement_request_sha256": refinement_request_sha256,
            "refinement_policy_version": REFINEMENT_POLICY_VERSION,
            "requested_limit": requested_limit,
            "has_filter": filter_request is not None,
        },
    )
    await retire_inactive_result_sets(
        db,
        tenant_id=tenant_id,
        conversation_id=derived.conversation_id,
        browser_session_id=browser_session_id,
        keep_ids={derived.id, source_result_set.id},
        inactive_slots=MAX_INACTIVE_RESULT_SETS_PER_CONTEXT - 1,
    )
    return RefinementResult(
        result_set=derived,
        members=derived_members,
        source_result_count=source_result_count,
        limit_truncated=limit_truncated,
    )
