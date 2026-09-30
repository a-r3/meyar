"""Issue #86 — ResultSet validation must not scale with tenant corpus size.

The SAME bounded ResultSet (10 members) is validated in a 16-candidate
tenant and in a 2,000-candidate tenant. SQL statements are counted with a
SQLAlchemy ``before_cursor_execute`` listener attached ONLY around the
operation under test (fixture setup is never counted).

Timings printed here are LOCAL DEVELOPMENT MEASUREMENTS — NOT TARGET MAC
BENCHMARKS. Query count/shape is the gate, never wall-clock time.

Synthetic data only."""

import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import dataclass, field

import pytest
from search_helpers import _canonical_content_from_profile, seed_active_result_set
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.agent.turn_boundary import TurnSessionState
from meyar.core.roles import ROLE_HR_USER
from meyar.models.candidate import Candidate
from meyar.models.candidate_document import CandidateDocument
from meyar.models.candidate_profile_version import CandidateProfileVersion
from meyar.models.canonical_document import CanonicalDocument
from meyar.search.schemas import CandidateSearchRequest, RequiredFilters, SearchMode
from meyar.services.agent_result_set_repo import (
    RefinementResult,
    active_result_set_size,
    create_result_set_from_refinement,
    resolve_active_candidate_ref,
)
from meyar.services.browser_session_repo import create_browser_session
from meyar.services.tenant_membership_repo import create_membership
from meyar.services.tenant_repo import create_tenant
from meyar.services.user_repo import create_user

SMALL_TENANT = 16
LARGE_TENANT = 2_000
RESULT_SET_SIZE = 10


def _profile(skill: str) -> dict:
    return {
        "skills": [
            {
                "name": skill,
                "category": None,
                "evidence": [{"page": 1, "block_index": 0, "quote": f"Synthetic skill {skill}"}],
            }
        ],
        "employment_history": [],
        "education": [],
        "certifications": [],
        "languages": [],
        "projects": [],
    }


async def bulk_seed_candidates(
    db: AsyncSession, *, tenant_id: uuid.UUID, count: int
) -> list[uuid.UUID]:
    """Fast synthetic seeding: Candidate + CandidateDocument +
    CanonicalDocument + COMPLETED CandidateProfileVersion v1 per candidate,
    with evidence that passes the real professional authority check."""
    candidate_ids: list[uuid.UUID] = []
    candidates: list[object] = []
    documents: list[object] = []
    canonicals: list[object] = []
    profiles: list[object] = []
    for index in range(count):
        skill = "Python" if index % 2 == 0 else "Java"
        content = _profile(skill)
        candidate_id, document_id, canonical_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        candidate_ids.append(candidate_id)
        candidates.append(Candidate(id=candidate_id, tenant_id=tenant_id))
        documents.append(
            CandidateDocument(
                id=document_id,
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                original_filename="synthetic.pdf",
                mime_type="application/pdf",
                byte_size=100,
                sha256_hash="a" * 64,
                storage_key=f"scale/{uuid.uuid4().hex}",
            )
        )
        canonicals.append(
            CanonicalDocument(
                id=canonical_id,
                tenant_id=tenant_id,
                candidate_document_id=document_id,
                parser_name="test-parser",
                parser_version="1.0.0",
                language=None,
                content=_canonical_content_from_profile(content, fallback="synthetic"),
            )
        )
        profiles.append(
            CandidateProfileVersion(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                candidate_document_id=document_id,
                canonical_document_id=canonical_id,
                source_sha256="a" * 64,
                version_number=1,
                schema_version="candidate-profile-v1",
                prompt_version="candidate-profile-extraction-v1",
                model_provider="fake",
                model_name="fake-model",
                model_metadata={},
                status="COMPLETED",
                profile_content=content,
            )
        )
    # No ORM relationships between these models: flush table by table so
    # FK order is explicit.
    for batch in (candidates, documents, canonicals, profiles):
        db.add_all(batch)
        await db.flush()
    return candidate_ids


@dataclass
class TenantFixture:
    tenant_id: uuid.UUID
    session_id: uuid.UUID
    context: TurnSessionState
    member_ids: list[uuid.UUID] = field(default_factory=list)


async def build_tenant(db: AsyncSession, *, candidates: int) -> TenantFixture:
    from search_helpers import open_test_conversation

    tenant = await create_tenant(db, name=f"Scale-{uuid.uuid4().hex[:8]}")
    user = await create_user(
        db, username=f"scale-{uuid.uuid4().hex[:8]}", plaintext_password="synthetic-pass-1"
    )
    membership = await create_membership(
        db, user_id=user.id, tenant_id=tenant.id, role=ROLE_HR_USER
    )
    session, _raw = await create_browser_session(
        db, user_id=user.id, tenant_membership_id=membership.id, ttl_hours=8
    )
    await db.flush()
    candidate_ids = await bulk_seed_candidates(db, tenant_id=tenant.id, count=candidates)
    _conversation, context = await open_test_conversation(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        membership_id=membership.id,
        browser_session_id=session.id,
    )
    members = candidate_ids[:RESULT_SET_SIZE]
    await seed_active_result_set(
        db,
        tenant_id=tenant.id,
        browser_session_id=session.id,
        session_context=context,
        candidate_ids=members,
    )
    await db.commit()
    # A plain snapshot (what the #85 turn boundary passes in production),
    # so rollbacks between measured operations never expire it.
    return TenantFixture(tenant.id, session.id, TurnSessionState.of(context), members)


@dataclass
class QueryLog:
    statements: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def count(self) -> int:
        return len(self.statements)

    def touching(self, table: str) -> int:
        return sum(1 for statement in self.statements if table in statement)


@contextmanager
def count_queries(db: AsyncSession):
    log = QueryLog()
    engine = db.bind.sync_engine  # type: ignore[union-attr]

    def _record(_conn, _cursor, statement, *_args) -> None:  # noqa: ANN001
        log.statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    started = time.perf_counter()
    try:
        yield log
    finally:
        log.seconds = time.perf_counter() - started
        event.remove(engine, "before_cursor_execute", _record)


Operation = Callable[[AsyncSession, TenantFixture], Awaitable[object]]


async def _ordinal(db: AsyncSession, fx: TenantFixture) -> object:
    return await resolve_active_candidate_ref(
        db,
        tenant_id=fx.tenant_id,
        browser_session_id=fx.session_id,
        session_context=fx.context,
        candidate_ref=3,
    )


async def _size(db: AsyncSession, fx: TenantFixture) -> object:
    return await active_result_set_size(
        db,
        tenant_id=fx.tenant_id,
        browser_session_id=fx.session_id,
        session_context=fx.context,
    )


async def _limit_refinement(db: AsyncSession, fx: TenantFixture) -> object:
    return await create_result_set_from_refinement(
        db,
        tenant_id=fx.tenant_id,
        browser_session_id=fx.session_id,
        session_context=fx.context,
        filter_request=None,
        requested_limit=5,
    )


async def _filter_refinement(db: AsyncSession, fx: TenantFixture) -> object:
    return await create_result_set_from_refinement(
        db,
        tenant_id=fx.tenant_id,
        browser_session_id=fx.session_id,
        session_context=fx.context,
        filter_request=CandidateSearchRequest(
            mode=SearchMode.STRUCTURED_ONLY,
            required_filters=RequiredFilters(skills=["Python"]),
        ),
        requested_limit=None,
    )


OPERATIONS: dict[str, Operation] = {
    "ordinal": _ordinal,
    "active_size": _size,
    "limit_refinement": _limit_refinement,
    "filter_refinement": _filter_refinement,
}


@pytest.mark.parametrize("operation_name", list(OPERATIONS))
async def test_result_set_validation_query_count_is_independent_of_tenant_size(
    db_session: AsyncSession, operation_name: str
) -> None:
    operation = OPERATIONS[operation_name]
    small = await build_tenant(db_session, candidates=SMALL_TENANT)
    large = await build_tenant(db_session, candidates=LARGE_TENANT)

    logs: dict[str, QueryLog] = {}
    for label, fx in (("small", small), ("large", large)):
        with count_queries(db_session) as log:
            outcome = await operation(db_session, fx)
        await db_session.rollback()
        logs[label] = log
        # The operation genuinely succeeded (valid snapshot, no staleness).
        if operation_name == "active_size":
            assert outcome == RESULT_SET_SIZE
        elif operation_name == "ordinal":
            assert getattr(outcome, "candidate_id", None) == fx.member_ids[2]
        else:
            assert isinstance(outcome, RefinementResult)

    print(  # LOCAL DEVELOPMENT MEASUREMENT — NOT TARGET MAC BENCHMARK
        f"\n[#86 {operation_name}] small={logs['small'].count} stmts "
        f"({logs['small'].seconds * 1000:.1f} ms) "
        f"large={logs['large'].count} stmts ({logs['large'].seconds * 1000:.1f} ms) "
        f"canonical small={logs['small'].touching('canonical_documents')} "
        f"large={logs['large'].touching('canonical_documents')}"
    )
    # Query COUNT does not grow with tenant size.
    assert logs["large"].count == logs["small"].count
    # Small constant bound, and no canonical-document N+1.
    assert logs["large"].count <= 12
    assert logs["large"].touching("canonical_documents") <= 1
    # No statement loads the tenant-wide current-profile set (GROUP BY scan).
    for statement in logs["large"].statements:
        assert "GROUP BY candidate_profile_versions.candidate_id" not in statement
