"""Issue #86 — ResultSet = immutable search snapshot; member-scoped staleness.

Required matrix (docs/DECISIONS.md D-090):

A unrelated new candidate ingested            -> member still resolves
B unrelated candidate new COMPLETED profile   -> member still resolves
C unrelated candidate FAILED re-extraction    -> member still resolves
D member new COMPLETED profile                -> STALE
E member newer FAILED / manual-review profile -> STALE
F member candidate hard-deleted               -> STALE (delete not blocked)
G member loses canonical evidence authority   -> STALE
H semantic member embedding deleted/incompatible -> STALE
I STRUCTURED_ONLY member embedding changes    -> still valid
J CandidateIdentity changes                   -> still valid
K refinement after unrelated ingestion        -> original snapshot subset only
L refinement after a source member went stale -> whole refinement STALE
M zero-result ResultSet semantics             -> unchanged
N unknown snapshot_policy_version             -> fail closed (size 0, ordinal
  and refinement STALE-class failure, closed audit reason, no raw policy)

Synthetic data only."""

import uuid

import pytest
from search_helpers import (
    SNAPSHOT_POLICY_VERSION,
    open_test_conversation,
    seed_active_result_set,
    seed_candidate_with_profile,
    seed_embedding,
    seed_next_profile_version,
)
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.embedding.serializer import SERIALIZER_VERSION
from meyar.models.agent_result_set import AgentResultSet, AgentResultSetMember
from meyar.models.audit_event import AuditEvent
from meyar.models.candidate import Candidate
from meyar.models.canonical_document import CanonicalDocument
from meyar.search.schemas import (
    CandidateSearchRequest,
    EmbeddingSearchConfig,
    RequiredFilters,
    SearchMode,
)
from meyar.services.agent_result_set_repo import (
    RefinementResult,
    ResultSetResolutionFailure,
    active_result_set_size,
    create_result_set_from_refinement,
    resolve_active_candidate_ref,
    validate_active_result_set_for_refinement,
)
from meyar.services.browser_session_repo import create_browser_session
from meyar.services.candidate_identity_repo import create_identity_version
from meyar.services.candidate_profile_repo import create_profile_version


def _profile(*skills: str) -> dict:
    return {
        "skills": [
            {
                "name": skill,
                "category": None,
                "evidence": [{"page": 1, "block_index": 0, "quote": f"Synthetic skill {skill}"}],
            }
            for skill in skills
        ],
        "employment_history": [],
        "education": [],
        "certifications": [],
        "languages": [],
        "projects": [],
    }


async def _context(db: AsyncSession, tenant, user, membership):  # noqa: ANN001, ANN202
    session, _raw = await create_browser_session(
        db, user_id=user.id, tenant_membership_id=membership.id, ttl_hours=8
    )
    await db.flush()
    _conversation, context = await open_test_conversation(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        membership_id=membership.id,
        browser_session_id=session.id,
    )
    return session, context


async def _structured_snapshot(db: AsyncSession, tenant_and_user, skills=("Python", "Java")):  # noqa: ANN001, ANN202
    tenant, user, _password, membership = tenant_and_user
    members = []
    for skill in skills:
        candidate, version = await seed_candidate_with_profile(
            db, tenant_id=tenant.id, profile_content=_profile(skill)
        )
        members.append((candidate, version))
    session, context = await _context(db, tenant, user, membership)
    result_set = await seed_active_result_set(
        db,
        tenant_id=tenant.id,
        browser_session_id=session.id,
        session_context=context,
        candidate_ids=[candidate.id for candidate, _ in members],
    )
    await db.commit()
    return tenant, session, context, result_set, members


async def _resolve(db: AsyncSession, tenant, session, context, ref: int = 1):  # noqa: ANN001, ANN202
    return await resolve_active_candidate_ref(
        db,
        tenant_id=tenant.id,
        browser_session_id=session.id,
        session_context=context,
        candidate_ref=ref,
    )


async def _unrelated_candidate(db: AsyncSession, tenant):  # noqa: ANN001, ANN202
    candidate, version = await seed_candidate_with_profile(
        db, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db.commit()
    return candidate, version


# ---------------------------------------------------------------- A / B / C


async def test_a_unrelated_new_candidate_keeps_member_resolvable(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, session, context, _rs, members = await _structured_snapshot(
        db_session, tenant_and_user
    )
    await _unrelated_candidate(db_session, tenant)
    resolved = await _resolve(db_session, tenant, session, context, ref=2)
    assert not isinstance(resolved, ResultSetResolutionFailure)
    assert resolved.candidate_id == members[1][0].id
    assert resolved.candidate_profile_version_id == members[1][1].id


async def test_b_unrelated_candidate_reprocessed_keeps_member_resolvable(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, session, context, _rs, members = await _structured_snapshot(
        db_session, tenant_and_user
    )
    other, _v1 = await _unrelated_candidate(db_session, tenant)
    await seed_next_profile_version(
        db_session, tenant_id=tenant.id, candidate=other, profile_content=_profile("Go")
    )
    await db_session.commit()
    resolved = await _resolve(db_session, tenant, session, context)
    assert not isinstance(resolved, ResultSetResolutionFailure)
    assert resolved.candidate_id == members[0][0].id


async def test_c_unrelated_candidate_failed_reextraction_keeps_member_resolvable(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, session, context, _rs, members = await _structured_snapshot(
        db_session, tenant_and_user
    )
    other, v1 = await _unrelated_candidate(db_session, tenant)
    await _newer_version(db_session, tenant, other, v1, status="FAILED")
    resolved = await _resolve(db_session, tenant, session, context)
    assert not isinstance(resolved, ResultSetResolutionFailure)
    assert resolved.candidate_id == members[0][0].id


# ---------------------------------------------------------------- D / E / F / G


async def _newer_version(db: AsyncSession, tenant, candidate, previous, *, status: str):  # noqa: ANN001, ANN202
    version = await create_profile_version(
        db,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        candidate_document_id=previous.candidate_document_id,
        canonical_document_id=previous.canonical_document_id,
        source_sha256="c" * 64,
        schema_version="candidate-profile-v1",
        prompt_version="candidate-profile-extraction-v1",
        model_provider="fake",
        model_name="fake-model",
        model_metadata={},
        status=status,
        profile_content=None,
    )
    await db.commit()
    return version


async def test_d_member_new_completed_profile_is_stale_and_never_served(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, session, context, rs, members = await _structured_snapshot(
        db_session, tenant_and_user
    )
    candidate, v1 = members[0]
    await seed_next_profile_version(
        db_session, tenant_id=tenant.id, candidate=candidate, profile_content=_profile("Rust")
    )
    await db_session.commit()
    assert await _resolve(db_session, tenant, session, context) == (
        ResultSetResolutionFailure.STALE
    )
    # The other, unchanged member still resolves (member-scoped, not global).
    resolved = await _resolve(db_session, tenant, session, context, ref=2)
    assert not isinstance(resolved, ResultSetResolutionFailure)
    # The historical member row is never rewritten to the new version.
    stored = await db_session.scalar(
        select(AgentResultSetMember).where(
            AgentResultSetMember.result_set_id == rs.id, AgentResultSetMember.ordinal == 1
        )
    )
    assert stored.candidate_id == candidate.id
    assert stored.candidate_profile_version_id == v1.id


@pytest.mark.parametrize("status", ["FAILED", "MANUAL_REVIEW_REQUIRED"])
async def test_e_member_newer_failed_or_review_profile_is_stale(
    db_session: AsyncSession, tenant_and_user, status: str
) -> None:
    tenant, session, context, _rs, members = await _structured_snapshot(
        db_session, tenant_and_user
    )
    candidate, v1 = members[0]
    await _newer_version(db_session, tenant, candidate, v1, status=status)
    assert await _resolve(db_session, tenant, session, context) == (
        ResultSetResolutionFailure.STALE
    )


async def test_f_member_hard_delete_is_not_blocked_and_fails_closed(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, session, context, rs, members = await _structured_snapshot(
        db_session, tenant_and_user
    )
    candidate, _v1 = members[0]
    await db_session.execute(
        delete(Candidate).where(Candidate.id == candidate.id, Candidate.tenant_id == tenant.id)
    )
    await db_session.commit()  # not blocked by the historical member row
    assert await _resolve(db_session, tenant, session, context) == (
        ResultSetResolutionFailure.STALE
    )
    # Snapshot row survives unchanged; it never renumbers to another candidate.
    stored = (
        await db_session.scalars(
            select(AgentResultSetMember)
            .where(AgentResultSetMember.result_set_id == rs.id)
            .order_by(AgentResultSetMember.ordinal)
        )
    ).all()
    assert [m.candidate_id for m in stored] == [c.id for c, _ in members]
    refined = await create_result_set_from_refinement(
        db_session,
        tenant_id=tenant.id,
        browser_session_id=session.id,
        session_context=context,
        filter_request=None,
        requested_limit=1,
    )
    assert refined == ResultSetResolutionFailure.STALE


async def test_g_member_losing_canonical_evidence_authority_is_stale(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, session, context, _rs, members = await _structured_snapshot(
        db_session, tenant_and_user
    )
    _candidate, v1 = members[0]
    canonical = await db_session.get(CanonicalDocument, v1.canonical_document_id)
    canonical.content = {"pages": [{"page": 1, "blocks": [{"index": 0, "text": "Unrelated"}]}]}
    await db_session.commit()
    assert await _resolve(db_session, tenant, session, context) == (
        ResultSetResolutionFailure.STALE
    )
    await db_session.execute(
        delete(CanonicalDocument).where(CanonicalDocument.id == v1.canonical_document_id)
    )
    await db_session.commit()
    assert await _resolve(db_session, tenant, session, context) == (
        ResultSetResolutionFailure.STALE
    )


# ---------------------------------------------------------------- H / I / J


def _embedding_config() -> EmbeddingSearchConfig:
    return EmbeddingSearchConfig(
        provider="fake-embedding",
        model_name="fake-embedding-model-v1",
        model_revision="",
        serializer_version=SERIALIZER_VERSION,
        embedding_dimensions=8,
    )


async def _semantic_snapshot(db: AsyncSession, tenant_and_user):  # noqa: ANN001, ANN202
    tenant, user, _password, membership = tenant_and_user
    content = _profile("Python")
    candidate, version = await seed_candidate_with_profile(
        db, tenant_id=tenant.id, profile_content=content
    )
    embedding = await seed_embedding(
        db,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        profile_version_id=version.id,
        vector=[0.1] * 8,
        profile_content=content,
    )
    session, context = await _context(db, tenant, user, membership)
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY,
        semantic_query="python backend",
        embedding_config=_embedding_config(),
    )
    result_set = AgentResultSet(
        tenant_id=tenant.id,
        browser_session_id=session.id,
        conversation_id=context.conversation_id,
        context_epoch=context.context_epoch,
        request_sha256="d" * 64,
        canonical_search_request=request.model_dump(mode="json"),
        planner_policy_version="test",
        planner_prompt_version="test",
        planner_schema_version="test",
        planner_model_provider="test",
        planner_model_name="test",
        planner_model_revision="",
        search_policy_version="test",
        search_mode=SearchMode.SEMANTIC_ONLY.value,
        result_count=1,
        corpus_fingerprint_sha256=None,
        snapshot_policy_version=SNAPSHOT_POLICY_VERSION,
        expires_at=session.expires_at,
    )
    db.add(result_set)
    await db.flush()
    db.add(
        AgentResultSetMember(
            result_set_id=result_set.id,
            ordinal=1,
            candidate_id=candidate.id,
            candidate_profile_version_id=version.id,
            candidate_embedding_version_id=embedding.id,
            relevance_score=0.9,
            structured_score=None,
            semantic_score=0.9,
        )
    )
    context.active_result_set_id = result_set.id
    await db.commit()
    return tenant, session, context, candidate, version, embedding, content


async def test_h_semantic_member_valid_then_stale_when_recorded_embedding_deleted(
    db_session: AsyncSession, tenant_and_user
) -> None:
    from meyar.models.candidate_embedding_version import CandidateEmbeddingVersion

    tenant, session, context, candidate, _version, embedding, _content = (
        await _semantic_snapshot(db_session, tenant_and_user)
    )
    resolved = await _resolve(db_session, tenant, session, context)
    assert not isinstance(resolved, ResultSetResolutionFailure)
    assert resolved.candidate_id == candidate.id
    await db_session.execute(
        delete(CandidateEmbeddingVersion).where(CandidateEmbeddingVersion.id == embedding.id)
    )
    await db_session.commit()
    assert await _resolve(db_session, tenant, session, context) == (
        ResultSetResolutionFailure.STALE
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_name", "other-embedding-model"),
        ("serializer_version", "other-serializer"),
        ("embedding_dimensions", 16),
        ("source_sha256", "f" * 64),
    ],
)
async def test_h_semantic_member_stale_when_recorded_embedding_incompatible(
    db_session: AsyncSession, tenant_and_user, field: str, value: object
) -> None:
    from meyar.models.candidate_embedding_version import CandidateEmbeddingVersion

    tenant, session, context, _candidate, _version, embedding, _content = (
        await _semantic_snapshot(db_session, tenant_and_user)
    )
    row = await db_session.get(CandidateEmbeddingVersion, embedding.id)
    setattr(row, field, value)
    if field == "embedding_dimensions":
        row.embedding = [0.1] * 16
    await db_session.commit()
    assert await _resolve(db_session, tenant, session, context) == (
        ResultSetResolutionFailure.STALE
    )


async def test_h_semantic_new_embedding_row_does_not_replace_recorded_authority(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """A newer embedding row for the member never substitutes for the
    RECORDED one ("latest embedding" is never authority)."""
    tenant, session, context, candidate, version, embedding, content = (
        await _semantic_snapshot(db_session, tenant_and_user)
    )
    await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        profile_version_id=version.id,
        vector=[0.2] * 8,
        model_name="newer-embedding-model",
        profile_content=content,
    )
    await db_session.commit()
    resolved = await _resolve(db_session, tenant, session, context)
    assert not isinstance(resolved, ResultSetResolutionFailure)  # recorded row still valid


async def test_i_structured_member_ignores_embedding_changes(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, session, context, _rs, members = await _structured_snapshot(
        db_session, tenant_and_user
    )
    candidate, version = members[0]
    await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        profile_version_id=version.id,
        vector=[0.3] * 8,
        profile_content=_profile("Python"),
    )
    await db_session.commit()
    resolved = await _resolve(db_session, tenant, session, context)
    assert not isinstance(resolved, ResultSetResolutionFailure)
    assert resolved.candidate_id == candidate.id


async def test_j_candidate_identity_changes_never_affect_validity(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, session, context, _rs, members = await _structured_snapshot(
        db_session, tenant_and_user
    )
    candidate, version = members[0]
    for number in range(2):
        await create_identity_version(
            db_session,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
            candidate_document_id=version.candidate_document_id,
            canonical_document_id=version.canonical_document_id,
            source_sha256="e" * 64,
            schema_version="candidate-identity-v1",
            prompt_version="candidate-identity-extraction-v1",
            model_provider="fake",
            model_name="fake-model",
            status="COMPLETED" if number == 0 else "FAILED",
            identity_content=(
                {"full_name": None, "email": None, "phone": None} if number == 0 else None
            ),
        )
    await db_session.commit()
    resolved = await _resolve(db_session, tenant, session, context)
    assert not isinstance(resolved, ResultSetResolutionFailure)
    assert resolved.candidate_id == candidate.id


# ---------------------------------------------------------------- K / L / M


async def test_k_refinement_after_unrelated_ingestion_uses_original_snapshot_only(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, session, context, rs, members = await _structured_snapshot(
        db_session, tenant_and_user, skills=("Python", "Java", "Python")
    )
    outsider, _v = await _unrelated_candidate(db_session, tenant)  # also a Python CV
    assert not isinstance(
        await validate_active_result_set_for_refinement(
            db_session,
            tenant_id=tenant.id,
            browser_session_id=session.id,
            session_context=context,
        ),
        ResultSetResolutionFailure,
    )
    refined = await create_result_set_from_refinement(
        db_session,
        tenant_id=tenant.id,
        browser_session_id=session.id,
        session_context=context,
        filter_request=CandidateSearchRequest(
            mode=SearchMode.STRUCTURED_ONLY,
            required_filters=RequiredFilters(skills=["Python"]),
        ),
        requested_limit=None,
    )
    assert isinstance(refined, RefinementResult)
    ids = [member.candidate_id for member in refined.members]
    assert ids == [members[0][0].id, members[2][0].id]  # parent order preserved
    assert outsider.id not in ids  # never admits a candidate outside the parent
    assert refined.result_set.parent_result_set_id == rs.id


async def test_l_refinement_fails_whole_when_any_source_member_is_stale(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, session, context, _rs, members = await _structured_snapshot(
        db_session, tenant_and_user, skills=("Python", "Java", "Python")
    )
    candidate, _v1 = members[1]  # a member the Python filter would drop anyway
    await seed_next_profile_version(
        db_session, tenant_id=tenant.id, candidate=candidate, profile_content=_profile("Java")
    )
    await db_session.commit()
    for limit in (None, 1):
        refined = await create_result_set_from_refinement(
            db_session,
            tenant_id=tenant.id,
            browser_session_id=session.id,
            session_context=context,
            filter_request=CandidateSearchRequest(
                mode=SearchMode.STRUCTURED_ONLY,
                required_filters=RequiredFilters(skills=["Python"]),
            ),
            requested_limit=limit,
        )
        assert refined == ResultSetResolutionFailure.STALE
    assert await validate_active_result_set_for_refinement(
        db_session, tenant_id=tenant.id, browser_session_id=session.id, session_context=context
    ) == ResultSetResolutionFailure.STALE
    count = await db_session.scalar(
        select(func.count()).select_from(AgentResultSet).where(
            AgentResultSet.tenant_id == tenant.id
        )
    )
    assert count == 1  # no derived set persisted


async def test_m_zero_result_result_set_semantics_unchanged(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    session, context = await _context(db_session, tenant, user, membership)
    await seed_active_result_set(
        db_session,
        tenant_id=tenant.id,
        browser_session_id=session.id,
        session_context=context,
        candidate_ids=[],
    )
    await db_session.commit()
    assert await active_result_set_size(
        db_session, tenant_id=tenant.id, browser_session_id=session.id, session_context=context
    ) == 0
    assert await _resolve(db_session, tenant, session, context) == (
        ResultSetResolutionFailure.ORDINAL_OUT_OF_RANGE
    )
    refined = await create_result_set_from_refinement(
        db_session,
        tenant_id=tenant.id,
        browser_session_id=session.id,
        session_context=context,
        filter_request=None,
        requested_limit=3,
    )
    assert isinstance(refined, RefinementResult)
    assert refined.members == [] and refined.limit_truncated is True


async def test_active_size_is_advisory_and_resolution_still_fails_closed(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, session, context, _rs, members = await _structured_snapshot(
        db_session, tenant_and_user
    )
    candidate, _v1 = members[0]
    await seed_next_profile_version(
        db_session, tenant_id=tenant.id, candidate=candidate, profile_content=_profile("Rust")
    )
    await db_session.commit()
    # Advisory bound: structural checks only, stored count.
    assert await active_result_set_size(
        db_session, tenant_id=tenant.id, browser_session_id=session.id, session_context=context
    ) == 2
    # Actual authority still fails closed for the changed member.
    assert await _resolve(db_session, tenant, session, context) == (
        ResultSetResolutionFailure.STALE
    )


async def test_cross_session_conversation_epoch_pointer_tampering_still_fails_closed(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """#49/#80 checks are preserved exactly: a pointer copied to another
    BrowserSession / conversation / epoch never resolves, and never
    reveals whether the foreign row exists."""
    from meyar.agent.turn_boundary import TurnSessionState

    tenant, session, context, rs, _members = await _structured_snapshot(
        db_session, tenant_and_user
    )
    base = TurnSessionState.of(context)
    tampered = [
        (TurnSessionState(**{**base.__dict__, "conversation_id": uuid.uuid4()}), session.id),
        (TurnSessionState(**{**base.__dict__, "context_epoch": base.context_epoch + 1}),
         session.id),
        (TurnSessionState(**{**base.__dict__, "browser_session_id": uuid.uuid4()}), None),
    ]
    for state, browser_session_id in tampered:
        outcome = await resolve_active_candidate_ref(
            db_session,
            tenant_id=tenant.id,
            browser_session_id=browser_session_id or state.browser_session_id,
            session_context=state,
            candidate_ref=1,
        )
        assert outcome in (
            ResultSetResolutionFailure.CONVERSATION_MISMATCH,
            ResultSetResolutionFailure.CONTEXT_EPOCH_MISMATCH,
            ResultSetResolutionFailure.SESSION_MISMATCH,
        )
    from meyar.services.tenant_repo import create_tenant

    foreign = await create_tenant(db_session, name=f"Foreign-{uuid.uuid4().hex[:8]}")
    await db_session.commit()
    foreign_tenant_state = TurnSessionState(**{**base.__dict__, "tenant_id": foreign.id})
    assert await resolve_active_candidate_ref(
        db_session,
        tenant_id=foreign_tenant_state.tenant_id,
        browser_session_id=session.id,
        session_context=foreign_tenant_state,
        candidate_ref=1,
    ) in (ResultSetResolutionFailure.NOT_FOUND, ResultSetResolutionFailure.SESSION_MISMATCH)
    assert rs.id is not None


# ---------------------------------------------------------------- N

UNKNOWN_POLICY = "member-snapshot-v999"


async def _audit(db: AsyncSession, tenant_id: uuid.UUID) -> list[AuditEvent]:
    return list(
        (await db.scalars(select(AuditEvent).where(AuditEvent.tenant_id == tenant_id))).all()
    )


async def test_n_current_policy_v1_is_accepted(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, session, context, result_set, _members = await _structured_snapshot(
        db_session, tenant_and_user
    )
    assert result_set.snapshot_policy_version == SNAPSHOT_POLICY_VERSION
    resolved = await _resolve(db_session, tenant, session, context)
    assert not isinstance(resolved, ResultSetResolutionFailure)
    assert await active_result_set_size(
        db_session, tenant_id=tenant.id, browser_session_id=session.id, session_context=context
    ) == 2


async def test_n_unknown_policy_fails_closed_everywhere(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, session, context, result_set, members = await _structured_snapshot(
        db_session, tenant_and_user
    )
    result_set.snapshot_policy_version = UNKNOWN_POLICY
    await db_session.commit()
    before = {row.id for row in await _audit(db_session, tenant.id)}

    assert await active_result_set_size(
        db_session, tenant_id=tenant.id, browser_session_id=session.id, session_context=context
    ) == 0
    for ref in (1, 2, 99):
        assert (
            await _resolve(db_session, tenant, session, context, ref=ref)
            == ResultSetResolutionFailure.UNSUPPORTED_SNAPSHOT_POLICY
        )
    assert (
        await validate_active_result_set_for_refinement(
            db_session, tenant_id=tenant.id, browser_session_id=session.id,
            session_context=context,
        )
        == ResultSetResolutionFailure.UNSUPPORTED_SNAPSHOT_POLICY
    )
    sets_before = await db_session.scalar(select(func.count()).select_from(AgentResultSet))
    assert (
        await create_result_set_from_refinement(
            db_session, tenant_id=tenant.id, browser_session_id=session.id,
            session_context=context, filter_request=None, requested_limit=1,
        )
        == ResultSetResolutionFailure.UNSUPPORTED_SNAPSHOT_POLICY
    )
    await db_session.commit()
    assert await db_session.scalar(select(func.count()).select_from(AgentResultSet)) == (
        sets_before
    )
    assert context.active_result_set_id == result_set.id

    new_events = [row for row in await _audit(db_session, tenant.id) if row.id not in before]
    assert {row.event_type for row in new_events} == {
        "agent.result_set.reference_rejected",
        "agent.result_set.refine_rejected",
    }
    for row in new_events:
        # Closed safe reason only: no candidate id, no ordinal, no raw policy.
        assert row.event_metadata == {"reason": "STALE", "context_epoch": context.context_epoch}
        text = str(row.event_metadata)
        assert UNKNOWN_POLICY not in text
        for candidate, version in members:
            assert str(candidate.id) not in text and str(version.id) not in text


async def test_n_unknown_policy_is_indistinguishable_across_session_and_tenant(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """A foreign context pointing at an unknown-policy row fails exactly like
    one pointing at the same row under the valid policy (ownership is checked
    first; both are the NOT_FOUND class) — the policy of a row that is not
    yours is never observable."""
    from meyar.services.tenant_repo import create_tenant

    tenant, _owner_session, _owner_context, result_set, _members = await _structured_snapshot(
        db_session, tenant_and_user
    )
    result_set.snapshot_policy_version = UNKNOWN_POLICY
    _tenant, user, _password, membership = tenant_and_user
    other_session, other_context = await _context(db_session, tenant, user, membership)
    await db_session.commit()

    async def _probe(target: uuid.UUID) -> tuple[object, list[dict]]:
        other_context.active_result_set_id = target
        await db_session.commit()
        seen = {row.id for row in await _audit(db_session, tenant.id)}
        outcome = await _resolve(db_session, tenant, other_session, other_context)
        size = await active_result_set_size(
            db_session, tenant_id=tenant.id, browser_session_id=other_session.id,
            session_context=other_context,
        )
        await db_session.commit()
        events = [
            dict(row.event_metadata)
            for row in await _audit(db_session, tenant.id)
            if row.id not in seen
        ]
        assert size == 0
        return outcome, events

    unknown_outcome, unknown_events = await _probe(result_set.id)
    result_set.snapshot_policy_version = SNAPSHOT_POLICY_VERSION
    await db_session.commit()
    valid_outcome, valid_events = await _probe(result_set.id)
    assert unknown_outcome == valid_outcome == ResultSetResolutionFailure.SESSION_MISMATCH
    assert unknown_events == valid_events == [
        {"reason": "NOT_FOUND", "context_epoch": other_context.context_epoch}
    ]
    # Cross-tenant: another tenant's call fails at the ownership step (the
    # row query itself also filters on tenant_id) before any policy is read.
    result_set.snapshot_policy_version = UNKNOWN_POLICY
    stranger = await create_tenant(db_session, name="N-foreign-tenant")
    await db_session.commit()
    assert (
        await resolve_active_candidate_ref(
            db_session, tenant_id=stranger.id, browser_session_id=other_session.id,
            session_context=other_context, candidate_ref=1,
        )
        == ResultSetResolutionFailure.SESSION_MISMATCH
    )
