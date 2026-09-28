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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.agent.schemas import MAX_CANDIDATE_REF
from meyar.embedding.serializer import build_professional_embedding_text, compute_source_sha256
from meyar.models.agent_conversation import AgentConversation
from meyar.models.agent_result_set import AgentResultSet, AgentResultSetKind, AgentResultSetMember
from meyar.search.planner_schemas import PlannedCandidateSearchResponse
from meyar.search.schemas import CandidateSearchRequest, EmbeddingSearchConfig
from meyar.search.structured import evaluate_required_filters
from meyar.services.audit_repo import record_event
from meyar.services.browser_session_repo import get_browser_session_by_id
from meyar.services.candidate_embedding_repo import list_compatible_embedding_version_ids
from meyar.services.candidate_profile_repo import list_current_profile_versions_for_tenant
from meyar.services.profile_authority import (
    ProfileAuthorityError,
    authorize_profile_version,
    get_current_authorized_profile,
)

# issue #49 PR49-2 — the deterministic policy REFINE_CANDIDATE_RESULTS
# applies: derived membership is always a subset of the active result
# set's own members in their existing order (filter never reorders,
# limit only truncates); bump when that policy itself changes, never for
# an unrelated code change. See create_result_set_from_refinement.
REFINEMENT_POLICY_VERSION = "agent-refinement-policy-v1"


async def compute_corpus_fingerprint(
    db: AsyncSession, *, tenant_id: uuid.UUID, embedding_config: EmbeddingSearchConfig | None
) -> str:
    """Deterministic sha256 over the tenant's current searchable authority
    at this instant — the exact same "current profile version, search-
    authorized" notion meyar.search.service.search_candidates uses (via
    list_current_profile_versions_for_tenant + authorize_profile_version),
    so a result set's own provenance is always compared against a
    corpus definition search itself would agree with.

    STRUCTURED_ONLY (``embedding_config is None``): hashes sorted
    ``"{candidate_id}:{profile_version_id}"`` entries.

    SEMANTIC_ONLY/HYBRID: additionally folds in the one compatible
    embedding version id for that exact ``EmbeddingSearchConfig`` (or the
    literal ``NONE`` when a candidate currently has none), via
    ``"{candidate_id}:{profile_version_id}:{embedding_version_id|NONE}"``.

    Never hashes profile content, CandidateIdentity, evidence text, or CV
    text — only ids. A candidate whose current profile is not currently
    search-authorized (see ProfileAuthorityError) is simply absent, same
    as search's own eligible set."""
    profile_versions = await list_current_profile_versions_for_tenant(db, tenant_id=tenant_id)
    authorized_pairs: list[tuple[uuid.UUID, uuid.UUID]] = []
    authorized_content: dict[uuid.UUID, dict] = {}
    for version in profile_versions:
        try:
            await authorize_profile_version(db, version=version)
        except ProfileAuthorityError:
            continue
        authorized_pairs.append((version.candidate_id, version.id))
        assert version.profile_content is not None
        authorized_content[version.id] = version.profile_content

    if embedding_config is None:
        entries = [
            f"{candidate_id}:{profile_version_id}"
            for candidate_id, profile_version_id in authorized_pairs
        ]
    else:
        profile_version_source_hashes = {
            profile_version_id: compute_source_sha256(
                build_professional_embedding_text(authorized_content[profile_version_id])
            )
            for _candidate_id, profile_version_id in authorized_pairs
        }
        embedding_ids_by_candidate = await list_compatible_embedding_version_ids(
            db,
            tenant_id=tenant_id,
            profile_version_source_hashes=profile_version_source_hashes,
            provider=embedding_config.provider,
            model_name=embedding_config.model_name,
            model_revision=embedding_config.model_revision,
            serializer_version=embedding_config.serializer_version,
            embedding_dimensions=embedding_config.embedding_dimensions,
        )
        entries = [
            f"{candidate_id}:{profile_version_id}:"
            f"{embedding_ids_by_candidate.get(candidate_id, 'NONE')}"
            for candidate_id, profile_version_id in authorized_pairs
        ]
    joined = "\n".join(sorted(entries))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


async def create_result_set_from_search(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    browser_session_id: uuid.UUID,
    context_epoch: int,
    planned: PlannedCandidateSearchResponse,
    previous_result_set_id: uuid.UUID | None,
) -> AgentResultSet:
    """Persists one new AgentResultSet + its ordered AgentResultSetMember
    rows from an executable planner+search result, and fires
    ``agent.result_set.created``. Caller's responsibility to only call this
    when ``planned.plan.executable and planned.search_response is not
    None`` — asserted defensively here too."""
    assert planned.plan.executable and planned.search_response is not None
    request = planned.plan.search_request
    assert request is not None
    session = await get_browser_session_by_id(db, browser_session_id=browser_session_id)
    assert session is not None, "AgentConversation.browser_session_id must reference a live session"

    fingerprint = await compute_corpus_fingerprint(
        db, tenant_id=tenant_id, embedding_config=request.embedding_config
    )
    result_set = AgentResultSet(
        tenant_id=tenant_id,
        browser_session_id=browser_session_id,
        context_epoch=context_epoch,
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
        corpus_fingerprint_sha256=fingerprint,
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
    return result_set


class ResultSetResolutionFailure(StrEnum):
    """Typed internal failure reasons for ``resolve_active_candidate_ref``.
    Every member except ``STALE``/``EXPIRED``/``ORDINAL_OUT_OF_RANGE``
    collapses to a fail-closed "not found" outward outcome (see
    meyar.agent.service's mapping to AgentTurnOutcome) — cross-tenant/
    cross-session/cross-epoch probing must never be able to distinguish
    "exists but not yours" from "does not exist"."""

    NO_ACTIVE_RESULT_SET = "NO_ACTIVE_RESULT_SET"
    NOT_FOUND = "NOT_FOUND"
    SESSION_MISMATCH = "SESSION_MISMATCH"
    CONTEXT_EPOCH_MISMATCH = "CONTEXT_EPOCH_MISMATCH"
    EXPIRED = "EXPIRED"
    STALE = "STALE"
    ORDINAL_OUT_OF_RANGE = "ORDINAL_OUT_OF_RANGE"
    # Defense in depth only — in practice already implied by STALE (a
    # candidate that lost search authorization changes the corpus
    # fingerprint too). Kept as its own check so an authorization edge
    # case the fingerprint does not happen to cover can never resolve a
    # candidate MEYAR would no longer treat as searchable.
    CANDIDATE_NO_LONGER_AUTHORIZED = "CANDIDATE_NO_LONGER_AUTHORIZED"


# Outward audit-safe reason code for a rejected resolution — collapses the
# richer internal enum above into the four categories docs/DECISIONS.md
# documents for ``agent.result_set.reference_rejected``.
_AUDIT_REASON_BY_FAILURE: dict[ResultSetResolutionFailure, str] = {
    ResultSetResolutionFailure.NO_ACTIVE_RESULT_SET: "NOT_FOUND",
    ResultSetResolutionFailure.NOT_FOUND: "NOT_FOUND",
    ResultSetResolutionFailure.SESSION_MISMATCH: "NOT_FOUND",
    ResultSetResolutionFailure.CONTEXT_EPOCH_MISMATCH: "NOT_FOUND",
    ResultSetResolutionFailure.CANDIDATE_NO_LONGER_AUTHORIZED: "NOT_FOUND",
    ResultSetResolutionFailure.EXPIRED: "EXPIRED",
    ResultSetResolutionFailure.STALE: "STALE",
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
    conversation: AgentConversation,
) -> AgentResultSet | ResultSetResolutionFailure:
    """Steps 1-6 of the ordinal-resolution check order shared by EVERY
    consumer of ``conversation.active_result_set_id`` — a candidate_ref
    lookup (``resolve_active_candidate_ref``) and a REFINE_CANDIDATE_RESULTS
    turn (``create_result_set_from_refinement``) both call this ONE
    function rather than re-implementing the check (issue #49 PR49-2,
    docs/DECISIONS.md D-084: "one authoritative validation definition").
    Never fires an audit event itself — callers own their own audit
    semantics (different event types/metadata for a rejected ordinal vs. a
    rejected refinement); see each caller's own reject helper.

    1. ``conversation.active_result_set_id`` is set.
    2. The AgentResultSet row exists AND its own ``tenant_id`` equals
       ``tenant_id`` — filtered in ONE query on both columns together, so
       another tenant's row is never even fetched to compare against.
    3. Its ``browser_session_id`` equals this call's ``browser_session_id``.
    4. Its ``context_epoch`` equals ``conversation.context_epoch``.
    5. ``expires_at`` is still in the future.
    6. Recomputing the corpus fingerprint still matches the one stored at
       creation.
    """
    if conversation.active_result_set_id is None:
        return ResultSetResolutionFailure.NO_ACTIVE_RESULT_SET

    result_set = await db.scalar(
        select(AgentResultSet).where(
            AgentResultSet.id == conversation.active_result_set_id,
            AgentResultSet.tenant_id == tenant_id,
        )
    )
    if result_set is None:
        return ResultSetResolutionFailure.NOT_FOUND
    if result_set.browser_session_id != browser_session_id:
        return ResultSetResolutionFailure.SESSION_MISMATCH
    if result_set.context_epoch != conversation.context_epoch:
        return ResultSetResolutionFailure.CONTEXT_EPOCH_MISMATCH
    now = datetime.now(UTC)
    expires_at = result_set.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if now >= expires_at:
        return ResultSetResolutionFailure.EXPIRED

    embedding_config = CandidateSearchRequest.model_validate(
        result_set.canonical_search_request
    ).embedding_config
    current_fingerprint = await compute_corpus_fingerprint(
        db, tenant_id=tenant_id, embedding_config=embedding_config
    )
    if current_fingerprint != result_set.corpus_fingerprint_sha256:
        return ResultSetResolutionFailure.STALE

    return result_set


async def resolve_active_candidate_ref(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    browser_session_id: uuid.UUID,
    conversation: AgentConversation,
    candidate_ref: int,
) -> ResolvedCandidateRef | ResultSetResolutionFailure:
    """THE ONLY place a candidate_ref becomes a real candidate_id. Exact
    check order (each step fails closed to a typed
    ResultSetResolutionFailure, never an exception, never a partial
    result):

    1-6. See ``_validate_active_result_set``.
    7. ``candidate_ref`` is within the persisted member ordinal range.
    8. The resolved candidate still has a current authorized profile for
       this tenant (defense in depth; see ResultSetResolutionFailure
       docstring).

    Fires ``agent.result_set.reference_resolved`` on success or
    ``agent.result_set.reference_rejected`` on failure — metadata is
    always ids/enums/small ints only, never query/candidate-identity text.
    """
    validated = await _validate_active_result_set(
        db, tenant_id=tenant_id, browser_session_id=browser_session_id, conversation=conversation
    )
    if isinstance(validated, ResultSetResolutionFailure):
        return await _reject(
            db, tenant_id=tenant_id, conversation=conversation,
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
            db, tenant_id=tenant_id, conversation=conversation,
            failure=ResultSetResolutionFailure.ORDINAL_OUT_OF_RANGE, candidate_ref=candidate_ref,
        )

    authorized = await get_current_authorized_profile(
        db, tenant_id=tenant_id, candidate_id=member.candidate_id
    )
    if authorized is None:
        return await _reject(
            db, tenant_id=tenant_id, conversation=conversation,
            failure=ResultSetResolutionFailure.CANDIDATE_NO_LONGER_AUTHORIZED, candidate_ref=None,
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
    conversation: AgentConversation,
    failure: ResultSetResolutionFailure,
    candidate_ref: int | None,
) -> ResultSetResolutionFailure:
    metadata: dict = {
        "reason": _AUDIT_REASON_BY_FAILURE[failure],
        "context_epoch": conversation.context_epoch,
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
    conversation: AgentConversation,
) -> int:
    """How many ordinals are currently legally referenceable — used only
    to bound ``available_candidate_refs`` for the model's own next
    decision. Returns 0 on any failure condition (no active result set,
    stale, expired, wrong session/tenant/epoch) — this is advisory context
    for the model, never itself an authorization decision; the real
    authorization is always resolve_active_candidate_ref, called again
    independently when the model actually references an ordinal. Never
    raises, never leaks which specific failure occurred, and never fires
    an audit event (it is not itself a resolution attempt)."""
    validated = await _validate_active_result_set(
        db, tenant_id=tenant_id, browser_session_id=browser_session_id, conversation=conversation
    )
    if isinstance(validated, ResultSetResolutionFailure):
        return 0
    return min(validated.result_count, MAX_CANDIDATE_REF)


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
    conversation: AgentConversation,
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
            "context_epoch": conversation.context_epoch,
        },
    )


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
    conversation: AgentConversation,
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
    2. Each member's recorded ``candidate_profile_version_id`` must still
       equal the candidate's CURRENT authorized profile version (defense
       in depth beyond the aggregate corpus fingerprint check above) — any
       mismatch fails the whole refinement as STALE too.
    3. When ``filter_request`` is given, a member survives only if
       ``evaluate_required_filters`` (the exact Slice 8 structured-search
       gate) is satisfied against its own current profile — the parent's
       own ordinal order is preserved for every survivor (filtering never
       reorders).
    4. ``requested_limit`` truncates that same preserved order — never
       reorders, never fabricates members when the retained set is
       smaller than requested (``RefinementResult.limit_truncated``).

    The derived AgentResultSet's search-provenance fields
    (canonical_search_request/planner_*/search_policy_version/search_mode/
    corpus_fingerprint_sha256) are copied VERBATIM from the parent — never
    overwritten with the refinement's own filter text — and
    ``expires_at`` is inherited exactly (a refinement never extends
    validity beyond its parent). Fires ``agent.result_set.refined`` on
    success, ``agent.result_set.refine_rejected`` on failure — metadata is
    always ids/enums/counts, never raw filter/query text."""
    validated = await _validate_active_result_set(
        db, tenant_id=tenant_id, browser_session_id=browser_session_id, conversation=conversation
    )
    if isinstance(validated, ResultSetResolutionFailure):
        await _reject_refinement(
            db, tenant_id=tenant_id, conversation=conversation, failure=validated
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

    retained: list[tuple[AgentResultSetMember, list]] = []
    for member in source_members:
        authorized = await get_current_authorized_profile(
            db, tenant_id=tenant_id, candidate_id=member.candidate_id
        )
        if authorized is None or authorized[0].id != member.candidate_profile_version_id:
            # A member's own profile has moved since the source result set
            # was created despite the aggregate corpus fingerprint still
            # matching (defense in depth — see docstring point 2). Fail
            # the whole refinement rather than silently evaluating a
            # different profile version than the one this result set's
            # own provenance recorded.
            await _reject_refinement(
                db, tenant_id=tenant_id, conversation=conversation,
                failure=ResultSetResolutionFailure.STALE,
            )
            return ResultSetResolutionFailure.STALE
        if filter_request is None:
            retained.append((member, []))
            continue
        _version, profile = authorized
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
        corpus_fingerprint_sha256=source_result_set.corpus_fingerprint_sha256,
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
    return RefinementResult(
        result_set=derived,
        members=derived_members,
        source_result_count=source_result_count,
        limit_truncated=limit_truncated,
    )
