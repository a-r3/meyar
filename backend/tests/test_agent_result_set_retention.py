"""Issue #86 (L-6, ResultSet part) — bounded ResultSet retention without a
background service (docs/DECISIONS.md D-090).

Exact contract per (tenant, conversation, BrowserSession): 1 active + at
most MAX_INACTIVE_RESULT_SETS_PER_CONTEXT (5) inactive ResultSets, not
counting sets another live context independently points at — after a
successful Phase B pointer switch AND when that switch never happens. Each
pass is SQL-bounded to RESULT_SET_RETIRE_BATCH_SIZE rows (expired first),
so a historical backlog never makes one turn O(backlog); the agentless ops
sweep drains the rest. Member rows cascade; transcript and AuditEvents are
never deleted; each row actually deleted leaves exactly one safe
``agent.result_set.retired`` audit record. Synthetic data only."""

import re
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from search_helpers import (
    open_test_conversation,
    seed_active_result_set,
    seed_candidate_with_profile,
)
from sqlalchemy import event as sa_event
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar import cli
from meyar.models.agent_conversation import AgentConversation
from meyar.models.agent_result_set import AgentResultSet, AgentResultSetMember
from meyar.models.audit_event import AuditEvent
from meyar.services import agent_result_set_repo
from meyar.services.agent_result_set_repo import (
    MAX_INACTIVE_RESULT_SETS_PER_CONTEXT,
    RESULT_SET_RETIRE_BATCH_SIZE,
    RefinementResult,
    create_result_set_from_refinement,
    resolve_active_candidate_ref,
    retire_inactive_result_sets,
    retire_next_result_set_batch,
)
from meyar.services.browser_session_repo import create_browser_session

TURNS = 130
BACKLOG = 2_000
# Documented bound for ONE refinement turn's whole SQL footprint (member
# validation + derived set/members + audit + one bounded retention pass):
# independent of the backlog size. Measured: see the backlog test output.
MAX_STATEMENTS_PER_REFINEMENT = 12


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


async def _setup(db: AsyncSession, tenant_and_user, *, members: int = 3):  # noqa: ANN001, ANN202
    tenant, user, _password, membership = tenant_and_user
    candidates = []
    for index in range(members):
        candidate, _version = await seed_candidate_with_profile(
            db, tenant_id=tenant.id, profile_content=_profile(f"Skill{index}")
        )
        candidates.append(candidate)
    session, _raw = await create_browser_session(
        db, user_id=user.id, tenant_membership_id=membership.id, ttl_hours=8
    )
    await db.flush()
    conversation, context = await open_test_conversation(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        membership_id=membership.id,
        browser_session_id=session.id,
    )
    root = await seed_active_result_set(
        db,
        tenant_id=tenant.id,
        browser_session_id=session.id,
        session_context=context,
        candidate_ids=[candidate.id for candidate in candidates],
    )
    await db.commit()
    return tenant, session, conversation, context, root


async def _refine_and_point(db: AsyncSession, tenant, session, context) -> RefinementResult:  # noqa: ANN001
    """One refinement turn, then the Phase B pointer switch (issue #85)."""
    refined = await create_result_set_from_refinement(
        db,
        tenant_id=tenant.id,
        browser_session_id=session.id,
        session_context=context,
        filter_request=None,
        requested_limit=None,
    )
    assert isinstance(refined, RefinementResult)
    context.active_result_set_id = refined.result_set.id
    await db.commit()
    return refined


async def _sets(db: AsyncSession, conversation_id: uuid.UUID) -> list[AgentResultSet]:
    return list(
        (
            await db.scalars(
                select(AgentResultSet).where(AgentResultSet.conversation_id == conversation_id)
            )
        ).all()
    )


async def _events(db: AsyncSession, tenant_id: uuid.UUID, event_type: str) -> list[dict]:
    rows = (
        await db.scalars(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenant_id, AuditEvent.event_type == event_type
            )
        )
    ).all()
    return [dict(row.event_metadata) for row in rows]


async def test_growth_is_bounded_over_a_130_turn_conversation(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, session, conversation, context, root = await _setup(db_session, tenant_and_user)
    transcript_before = list(conversation.turns)
    for _ in range(TURNS):
        await _refine_and_point(db_session, tenant, session, context)

    remaining = await _sets(db_session, conversation.id)
    # Exact owner contract: 1 active + at most 5 inactive (no other context
    # points into this conversation here, so nothing is independently
    # protected).
    active = [row for row in remaining if row.id == context.active_result_set_id]
    inactive = [row for row in remaining if row.id != context.active_result_set_id]
    assert len(active) == 1
    assert len(inactive) == MAX_INACTIVE_RESULT_SETS_PER_CONTEXT
    assert context.active_result_set_id in {row.id for row in remaining}
    remaining_ids = {row.id for row in remaining}
    member_rows = await db_session.scalar(
        select(func.count()).select_from(AgentResultSetMember)
    )
    assert member_rows == 3 * len(remaining)  # orphans' members cascaded away
    orphan_members = await db_session.scalar(
        select(func.count())
        .select_from(AgentResultSetMember)
        .where(AgentResultSetMember.result_set_id.not_in(remaining_ids))
    )
    assert orphan_members == 0

    # Audit provenance survives; every retired row left a safe record.
    retired = await _events(db_session, tenant.id, "agent.result_set.retired")
    assert len(retired) == TURNS + 1 - len(remaining)
    assert len(await _events(db_session, tenant.id, "agent.result_set.refined")) == TURNS
    assert root.id in {uuid.UUID(event["result_set_id"]) for event in retired}
    for event in retired:
        assert set(event) == {
            "result_set_id", "result_set_kind", "parent_result_set_id", "conversation_id",
            "context_epoch", "request_sha256", "refinement_request_sha256",
            "search_policy_version", "refinement_policy_version", "snapshot_policy_version",
            "search_mode", "result_count", "created_at", "expires_at",
        }
        assert "canonical_search_request" not in str(event)
    # Transcript untouched; the active ResultSet still resolves (its parent
    # chain was retired — a derived set carries its own member snapshot).
    stored = await db_session.get(AgentConversation, conversation.id)
    assert stored.turns == transcript_before
    resolved = await resolve_active_candidate_ref(
        db_session,
        tenant_id=tenant.id,
        browser_session_id=session.id,
        session_context=context,
        candidate_ref=1,
    )
    assert getattr(resolved, "result_set_id", None) == context.active_result_set_id


async def test_expired_inactive_sets_are_retired_immediately_active_never(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, session, conversation, context, root = await _setup(db_session, tenant_and_user)
    first = await _refine_and_point(db_session, tenant, session, context)
    # Expire every existing set, including the ACTIVE one.
    for row in await _sets(db_session, conversation.id):
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()
    # Another conversation turn re-activates the expired pointer? No — the
    # active expired set must still survive cleanup (EXPIRED copy relies on it).
    context.active_result_set_id = first.result_set.id
    await db_session.commit()
    # Trigger cleanup via a new refinement on a fresh unexpired chain.
    fresh = await seed_active_result_set(
        db_session,
        tenant_id=tenant.id,
        browser_session_id=session.id,
        session_context=context,
        candidate_ids=[],
    )
    await db_session.commit()
    await _refine_and_point(db_session, tenant, session, context)
    remaining = {row.id for row in await _sets(db_session, conversation.id)}
    assert root.id not in remaining  # expired + inactive -> retired
    assert first.result_set.id not in remaining  # expired + no longer pointed at
    assert fresh.id in remaining  # unexpired inactive, within the bound
    assert context.active_result_set_id in remaining


async def test_cleanup_is_scoped_to_one_tenant_conversation_and_session(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, session, conversation, context, _root = await _setup(db_session, tenant_and_user)
    # A second BrowserSession on the SAME conversation with its own history.
    _tenant, user, _password, membership = tenant_and_user
    other_session, _raw = await create_browser_session(
        db_session, user_id=user.id, tenant_membership_id=membership.id, ttl_hours=8
    )
    await db_session.flush()
    from meyar.services.agent_conversation_repo import get_or_create_session_context

    other_context = await get_or_create_session_context(
        db_session, conversation=conversation, browser_session_id=other_session.id
    )
    other_sets = []
    for _ in range(MAX_INACTIVE_RESULT_SETS_PER_CONTEXT + 3):
        other_sets.append(
            await seed_active_result_set(
                db_session,
                tenant_id=tenant.id,
                browser_session_id=other_session.id,
                session_context=other_context,
                candidate_ids=[],
            )
        )
    await db_session.commit()
    for _ in range(MAX_INACTIVE_RESULT_SETS_PER_CONTEXT + 3):
        await _refine_and_point(db_session, tenant, session, context)
    surviving_other = {
        row.id
        for row in await _sets(db_session, conversation.id)
        if row.browser_session_id == other_session.id
    }
    # The other BrowserSession's rows are never touched by this session's
    # cleanup (only its own creations prune them).
    assert surviving_other == {row.id for row in other_sets}


async def test_retired_set_pointed_to_by_any_context_is_never_deleted(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """Defense in depth: a set another live context still points at is
    never retired even when it would otherwise fall outside the bound."""
    tenant, session, conversation, context, root = await _setup(db_session, tenant_and_user)
    from meyar.services.agent_conversation_repo import get_or_create_session_context

    _tenant, user, _password, membership = tenant_and_user
    other_session, _raw = await create_browser_session(
        db_session, user_id=user.id, tenant_membership_id=membership.id, ttl_hours=8
    )
    await db_session.flush()
    other_context = await get_or_create_session_context(
        db_session, conversation=conversation, browser_session_id=other_session.id
    )
    other_context.active_result_set_id = root.id  # tampered/foreign pointer
    await db_session.commit()
    for _ in range(MAX_INACTIVE_RESULT_SETS_PER_CONTEXT + 3):
        await _refine_and_point(db_session, tenant, session, context)
    assert await db_session.get(AgentResultSet, root.id) is not None


def _split(rows: list[AgentResultSet], active_id: uuid.UUID | None):  # noqa: ANN202
    active = [row for row in rows if row.id == active_id]
    return active, [row for row in rows if row.id != active_id]


async def test_exact_bound_holds_when_the_pointer_switch_never_happens(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """Phase B failed / went stale: the derived set was created (and
    pruning ran) but the pointer still names the old set. Still exactly 1
    active + at most 5 inactive, on every turn."""
    tenant, session, conversation, context, _root = await _setup(db_session, tenant_and_user)
    for _ in range(MAX_INACTIVE_RESULT_SETS_PER_CONTEXT + 4):
        await _refine_and_point(db_session, tenant, session, context)
    for _ in range(3):
        pointer_before = context.active_result_set_id
        refined = await create_result_set_from_refinement(
            db_session,
            tenant_id=tenant.id,
            browser_session_id=session.id,
            session_context=context,
            filter_request=None,
            requested_limit=None,
        )
        assert isinstance(refined, RefinementResult)
        await db_session.commit()  # no pointer switch
        assert context.active_result_set_id == pointer_before
        active, inactive = _split(
            await _sets(db_session, conversation.id), context.active_result_set_id
        )
        assert len(active) == 1
        assert len(inactive) <= MAX_INACTIVE_RESULT_SETS_PER_CONTEXT
        assert refined.result_set.id in {row.id for row in inactive}
        # ... and a later successful switch keeps the same exact bound.
        await _refine_and_point(db_session, tenant, session, context)
        active, inactive = _split(
            await _sets(db_session, conversation.id), context.active_result_set_id
        )
        assert len(active) == 1
        assert len(inactive) <= MAX_INACTIVE_RESULT_SETS_PER_CONTEXT


async def _seed_backlog(
    db: AsyncSession, *, template: AgentResultSet, count: int
) -> list[uuid.UUID]:
    """``count`` historical inactive ResultSets in the template's scope via
    one bulk INSERT: every other row expired, all older than the template.
    Member-less (retention never reads members)."""
    now = datetime.now(UTC)
    columns = [attr.key for attr in AgentResultSet.__mapper__.column_attrs]
    rows = []
    for index in range(count):
        row = {key: getattr(template, key) for key in columns}
        row["id"] = uuid.uuid4()
        row["created_at"] = now - timedelta(hours=2, seconds=index)
        if index % 2:
            row["expires_at"] = now - timedelta(minutes=5, seconds=index)
        rows.append(row)
    await db.execute(insert(AgentResultSet), rows)
    await db.commit()
    return [row["id"] for row in rows]


class _Instrumented:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.result_set_rows_loaded = 0

    def unbounded_result_set_selects(self) -> list[str]:
        """SELECTs over agent_result_sets that are neither a primary-key
        lookup nor LIMITed in SQL."""
        return [
            s for s in self.statements
            if s.lstrip().upper().startswith("SELECT")
            and "FROM agent_result_sets" in s
            and not re.search(r"agent_result_sets\.id = ", s)
            and not re.search(r"\bLIMIT\b", s)
        ]


@contextmanager
def _instrument(db: AsyncSession):  # noqa: ANN202
    log = _Instrumented()
    engine = db.bind.sync_engine  # type: ignore[union-attr]

    def _record(_conn, _cursor, statement, *_args) -> None:  # noqa: ANN001
        log.statements.append(statement)

    def _loaded(_target, _context) -> None:  # noqa: ANN001
        log.result_set_rows_loaded += 1

    sa_event.listen(engine, "before_cursor_execute", _record)
    sa_event.listen(AgentResultSet, "load", _loaded)
    try:
        yield log
    finally:
        sa_event.remove(engine, "before_cursor_execute", _record)
        sa_event.remove(AgentResultSet, "load", _loaded)


async def _scope_count(db: AsyncSession, conversation_id: uuid.UUID) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(AgentResultSet)
            .where(AgentResultSet.conversation_id == conversation_id)
        )
        or 0
    )


async def test_one_turn_over_a_2000_row_backlog_is_bounded(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, session, conversation, context, root = await _setup(db_session, tenant_and_user)
    backlog = await _seed_backlog(db_session, template=root, count=BACKLOG)
    rows_before = await _scope_count(db_session, conversation.id)
    retired_before = len(await _events(db_session, tenant.id, "agent.result_set.retired"))
    active_before = context.active_result_set_id

    with _instrument(db_session) as log:
        refined = await create_result_set_from_refinement(
            db_session,
            tenant_id=tenant.id,
            browser_session_id=session.id,
            session_context=context,
            filter_request=None,
            requested_limit=None,
        )
    assert isinstance(refined, RefinementResult)
    await db_session.commit()

    rows_after = await _scope_count(db_session, conversation.id)
    retired_events = (
        len(await _events(db_session, tenant.id, "agent.result_set.retired")) - retired_before
    )
    rows_retired = rows_before + 1 - rows_after
    print(
        f"\nbacklog={BACKLOG} batch={RESULT_SET_RETIRE_BATCH_SIZE} "
        f"result_set_orm_rows_loaded={log.result_set_rows_loaded} "
        f"rows_retired={rows_retired} audit_rows={retired_events} "
        f"sql_statements={len(log.statements)}"
    )
    # The retention SELECTs are LIMITed in SQL; no ORM row of the backlog
    # is materialized (only the source set itself is loaded once).
    assert log.result_set_rows_loaded <= 1
    assert log.unbounded_result_set_selects() == []
    assert 0 < rows_retired <= RESULT_SET_RETIRE_BATCH_SIZE
    assert retired_events == rows_retired
    assert len(log.statements) <= MAX_STATEMENTS_PER_REFINEMENT
    # Expired rows are retired first.
    expired = set(backlog[1::2])
    retired_ids = {
        uuid.UUID(e["result_set_id"])
        for e in await _events(db_session, tenant.id, "agent.result_set.retired")
    }
    assert retired_ids <= expired
    # The active set is untouched and still resolves.
    assert context.active_result_set_id == active_before
    assert await db_session.get(AgentResultSet, active_before) is not None
    resolved = await resolve_active_candidate_ref(
        db_session,
        tenant_id=tenant.id,
        browser_session_id=session.id,
        session_context=context,
        candidate_ref=1,
    )
    assert getattr(resolved, "result_set_id", None) == active_before


async def _drain(db: AsyncSession, tenant_id: uuid.UUID, *, limit: int) -> tuple[int, int]:
    batches = retired = 0
    while batches < limit:
        count = await retire_next_result_set_batch(db, tenant_id=tenant_id)
        await db.commit()
        if count == 0:
            break
        assert count <= RESULT_SET_RETIRE_BATCH_SIZE
        batches += 1
        retired += count
    return batches, retired


async def test_repeated_sweeps_drain_the_backlog_to_policy_protected_sets_survive(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, session, conversation, context, root = await _setup(db_session, tenant_and_user)
    backlog = await _seed_backlog(db_session, template=root, count=BACKLOG)
    # A second live context (another BrowserSession) points at one backlog
    # row: independently protected, must survive every sweep.
    from meyar.services.agent_conversation_repo import get_or_create_session_context

    _tenant, user, _password, membership = tenant_and_user
    other_session, _raw = await create_browser_session(
        db_session, user_id=user.id, tenant_membership_id=membership.id, ttl_hours=8
    )
    await db_session.flush()
    other_context = await get_or_create_session_context(
        db_session, conversation=conversation, browser_session_id=other_session.id
    )
    protected = backlog[7]  # an expired row
    other_context.active_result_set_id = protected
    await db_session.commit()

    retired_before = len(await _events(db_session, tenant.id, "agent.result_set.retired"))
    batches, retired = await _drain(db_session, tenant.id, limit=10 * BACKLOG)
    assert await retire_next_result_set_batch(db_session, tenant_id=tenant.id) == 0
    active, inactive = _split(
        await _sets(db_session, conversation.id), context.active_result_set_id
    )
    inactive_unprotected = [row for row in inactive if row.id != protected]
    assert len(active) == 1 and active[0].id == root.id
    assert len(inactive_unprotected) == MAX_INACTIVE_RESULT_SETS_PER_CONTEXT
    assert protected in {row.id for row in inactive}
    assert retired == BACKLOG - MAX_INACTIVE_RESULT_SETS_PER_CONTEXT - 1
    assert batches >= retired / RESULT_SET_RETIRE_BATCH_SIZE
    retired_events = await _events(db_session, tenant.id, "agent.result_set.retired")
    assert len(retired_events) - retired_before == retired
    # Survivors are the NEWEST unexpired backlog rows.
    unexpired_newest = backlog[0::2][:MAX_INACTIVE_RESULT_SETS_PER_CONTEXT]
    assert {row.id for row in inactive_unprotected} == set(unexpired_newest)


async def test_normal_turns_converge_on_a_backlog_one_batch_at_a_time(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, session, conversation, context, root = await _setup(db_session, tenant_and_user)
    await _seed_backlog(db_session, template=root, count=3 * RESULT_SET_RETIRE_BATCH_SIZE)
    before = await _scope_count(db_session, conversation.id)
    await _refine_and_point(db_session, tenant, session, context)
    assert before + 1 - await _scope_count(db_session, conversation.id) == (
        RESULT_SET_RETIRE_BATCH_SIZE
    )
    for _ in range(4):
        await _refine_and_point(db_session, tenant, session, context)
    active, inactive = _split(
        await _sets(db_session, conversation.id), context.active_result_set_id
    )
    assert len(active) == 1
    assert len(inactive) <= MAX_INACTIVE_RESULT_SETS_PER_CONTEXT


async def test_retired_events_match_only_rows_actually_deleted(
    db_session: AsyncSession, tenant_and_user, monkeypatch
) -> None:
    """A selected row that became protected before the DELETE (a pointer
    written in between) and an id that no longer exists are neither
    deleted nor reported: events come from DELETE ... RETURNING only."""
    tenant, session, conversation, context, root = await _setup(db_session, tenant_and_user)
    backlog = await _seed_backlog(db_session, template=root, count=4)
    from meyar.services.agent_conversation_repo import get_or_create_session_context

    _tenant, user, _password, membership = tenant_and_user
    other_session, _raw = await create_browser_session(
        db_session, user_id=user.id, tenant_membership_id=membership.id, ttl_hours=8
    )
    await db_session.flush()
    other_context = await get_or_create_session_context(
        db_session, conversation=conversation, browser_session_id=other_session.id
    )
    other_context.active_result_set_id = backlog[0]  # "raced" protection
    await db_session.commit()
    vanished = uuid.uuid4()

    async def _raced_selection(*_args, **_kwargs) -> list[uuid.UUID]:
        return [backlog[0], backlog[1], backlog[2], vanished, root.id]

    monkeypatch.setattr(agent_result_set_repo, "_select_retirement_batch", _raced_selection)
    retired = await retire_inactive_result_sets(
        db_session,
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        browser_session_id=session.id,
        keep_ids=set(),
        inactive_slots=0,
    )
    await db_session.commit()
    events = await _events(db_session, tenant.id, "agent.result_set.retired")
    reported = {uuid.UUID(e["result_set_id"]) for e in events}
    assert retired == 2
    assert reported == {backlog[1], backlog[2]}
    remaining = {row.id for row in await _sets(db_session, conversation.id)}
    assert {backlog[0], backlog[3], root.id} <= remaining
    assert not reported & remaining


class _SessionCtx:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc: object) -> bool:
        return False


async def test_ops_cli_retire_result_sets_is_tenant_scoped_and_pii_free(
    db_session: AsyncSession, tenant_and_user, monkeypatch, capsys
) -> None:
    tenant, _session, conversation, _context, root = await _setup(db_session, tenant_and_user)
    await _seed_backlog(db_session, template=root, count=3 * RESULT_SET_RETIRE_BATCH_SIZE)
    monkeypatch.setattr(cli, "get_session_factory", lambda: (lambda: _SessionCtx(db_session)))

    await cli._retire_result_sets(str(tenant.id), max_batches=1)
    first = capsys.readouterr().out
    assert f"Retired: {RESULT_SET_RETIRE_BATCH_SIZE}" in first and "Pending: yes" in first

    await cli._retire_result_sets(str(tenant.id), max_batches=100)
    second = capsys.readouterr().out
    assert "Pending: no" in second
    assert await _scope_count(db_session, conversation.id) == (
        1 + MAX_INACTIVE_RESULT_SETS_PER_CONTEXT
    )
    for output in (first, second):
        assert str(conversation.id) not in output
        assert str(root.id) not in output

    other_tenant = uuid.uuid4()
    with pytest.raises(SystemExit) as exit_info:
        await cli._retire_result_sets(str(other_tenant), max_batches=1)
    assert exit_info.value.code == 2
    with pytest.raises(SystemExit) as exit_info:
        await cli._retire_result_sets("*", max_batches=1)
    assert exit_info.value.code == 2
