"""Issue #88 slice A — the consequential Phase B transaction boundary and
real concurrency over the production ``POST /ui/agent`` path (D-092 §12.2,
§22 items 10, 22, 23).

* Lock order: a SQLAlchemy engine probe records every statement and COMMIT
  per real database transaction of the request engine, and the principal
  revalidation is preceded by a ``pg_locks`` read on the SAME backend.
  Together they prove that the final Phase B starts from a fresh
  transaction holding none of Phase A's conversation/context/submission
  locks, locks principal first and follows principal -> conversation ->
  context -> clarification -> task -> submission — for deterministic
  (no-inference) turns as well as inference turns.
* Resumed-turn failure: busy / cancel / revoke while a RESUMED capability is
  in flight leave the clarification OPEN and every pointer untouched.
* Real concurrency: two independently issued tokens answer one OPEN
  clarification; exactly one resolves.

Gated in-memory transport or the deterministic fake; never a real Ollama.
Synthetic data only."""

import asyncio
import itertools
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import BrowserTestClient as AsyncClient
from fakes import FakeLLMProvider
from httpx import ASGITransport
from sqlalchemy import event, func, select, text
from test_agent_inference_boundary import (
    BUSY_COPY,
    HYBRID_REQUEST,
    GatedOllama,
    _assert_hr_safe,
    _install,
    _install_gated_embedding,
    _login,
    _new_conversation,
    _spawn,
    small_pool,  # noqa: F401 - pytest fixture re-export
)
from test_issue88_slice_a_routes import SOURCE, VSR_TRIGGER, _fake

from meyar.agent import turn_boundary
from meyar.agent.clarification_schemas import ClarificationProposalValue
from meyar.config import Settings, get_settings
from meyar.llm import concurrency
from meyar.llm.dependency import get_llm_provider
from meyar.llm.provider import ModelTimeoutError
from meyar.main import app
from meyar.models.agent_conversation import AgentConversation, AgentConversationSessionContext
from meyar.models.agent_result_set import AgentResultSet
from meyar.models.agent_task import AgentClarification, AgentTask
from meyar.models.agent_turn_submission import AgentTurnSubmission
from meyar.models.audit_event import AuditEvent
from meyar.models.browser_session import BrowserSession
from meyar.search.planner_schemas import PlannerDraft
from meyar.search.schemas import RequiredFilters

# D-092 §12.2: principal -> conversation -> session context -> clarification
# -> task -> submission.
# S1 adds Tenant between User and Membership. Principal rechecks later in
# the same transaction target the same server-owned rows, not new lock owners.
PRINCIPAL = ["users", "tenants", "tenant_memberships", "browser_sessions"]
LOCK_RANK = {table: rank for rank, table in enumerate(PRINCIPAL + [
    "agent_conversations", "agent_conversation_session_contexts",
    "agent_clarifications", "agent_tasks", "agent_turn_submissions",
])}
PHASE_A_TABLES = [
    "agent_conversations", "agent_conversation_session_contexts", "agent_turn_submissions",
]
_LOCKING = re.compile(r"\bFOR (?:NO KEY UPDATE|KEY SHARE|UPDATE|SHARE)\b")
_FROM = re.compile(r'\bFROM\s+"?(\w+)"?')
_TOKEN = re.compile(r'name="submission_id" value="([0-9a-f-]{36})"')


class TransactionProbe:
    """Every statement and COMMIT of the request engine, tagged with the
    real database transaction it ran in (``begin`` assigns a fresh id; one
    DBAPI connection serves one transaction at a time)."""

    def __init__(self, engine) -> None:  # noqa: ANN001 - AsyncEngine
        self._ids = itertools.count(1)
        self.events: list[tuple[int | None, str]] = []
        sync = engine.sync_engine
        event.listen(sync, "begin", self._begin)
        event.listen(sync, "before_cursor_execute", self._statement)
        event.listen(sync, "commit", self._commit)

    def _begin(self, conn) -> None:  # noqa: ANN001
        conn.info["probe_tx"] = next(self._ids)

    def _statement(self, conn, _cursor, statement, *_args) -> None:  # noqa: ANN001, ANN002
        self.events.append((conn.info.get("probe_tx"), statement))

    def _commit(self, conn) -> None:  # noqa: ANN001
        self.events.append((conn.info.get("probe_tx"), "COMMIT"))

    def reset(self) -> None:
        self.events.clear()

    def transactions(self) -> dict[int | None, list[str]]:
        grouped: dict[int | None, list[str]] = {}
        for tx, statement in self.events:
            grouped.setdefault(tx, []).append(statement)
        return grouped

    def index_of(self, tx: int | None, statement: str) -> int:
        return self.events.index((tx, statement))

    def first_index(self, tx: int | None) -> int:
        return next(i for i, (owner, _s) in enumerate(self.events) if owner == tx)


def lock_sequence(statements: list[str]) -> list[str]:
    """Row-lock acquisition order; principal reacquisition and consecutive
    repeats collapse (one server-owned principal per request)."""
    tables: list[str] = []
    for statement in statements:
        if _LOCKING.search(statement):
            match = _FROM.search(statement)
            assert match is not None, statement
            table = match.group(1)
            # S1 auth/reservation/commit rechecks reacquire this request's
            # already-held principal rows. Assert the order of acquisition;
            # keep all non-principal/consequential lock-order assertions.
            if table in PRINCIPAL and table in tables:
                continue
            if not tables or tables[-1] != table:
                tables.append(table)
    return tables


def _writes(statements: list[str], table: str) -> bool:
    return any(
        statement.startswith((f"INSERT INTO {table} ", f"UPDATE {table} "))
        for statement in statements
    )


class _PoolCheckingLLM:
    """Delegates to the fake; records, at the moment each model method runs,
    how many request-engine connections are checked out (must be 0)."""

    def __init__(self, inner: FakeLLMProvider, pool_probe) -> None:  # noqa: ANN001
        self._inner = inner
        self._pool_probe = pool_probe
        self.checked_out_at_call: list[int] = []

    def __getattr__(self, name: str):  # noqa: ANN204
        value = getattr(self._inner, name)
        if not name.startswith(("plan_", "draft_", "resolve_", "decide_", "select_")):
            return value

        async def _call(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            self.checked_out_at_call.append(self._pool_probe.checked_out)
            return await value(*args, **kwargs)

        return _call


async def _token(client: AsyncClient, conversation_id: uuid.UUID) -> str:
    page = await client.get(f"/ui/agent?conversation={conversation_id}")
    match = _TOKEN.search(page.text)
    assert match is not None
    return match.group(1)


async def _post(client, csrf, conversation_id, message, **extra):  # noqa: ANN001, ANN003, ANN202
    return await client.post(
        "/ui/agent",
        data={
            "message": message, "csrf_token": csrf,
            "conversation_id": str(conversation_id), **extra,
        },
        follow_redirects=False,
    )


async def _clarifications(factory, tenant_id) -> list[AgentClarification]:  # noqa: ANN001
    async with factory() as db:
        return list(
            (
                await db.scalars(
                    select(AgentClarification)
                    .where(AgentClarification.tenant_id == tenant_id)
                    .order_by(AgentClarification.attempt)
                )
            ).all()
        )


async def _snapshot(factory, tenant_id, conversation_id) -> dict:  # noqa: ANN001
    async with factory() as db:
        conversation = await db.get(AgentConversation, conversation_id)
        assert conversation is not None
        (context,) = (
            await db.scalars(
                select(AgentConversationSessionContext).where(
                    AgentConversationSessionContext.conversation_id == conversation_id
                )
            )
        ).all()
        clarifications = (
            await db.scalars(
                select(AgentClarification).where(AgentClarification.tenant_id == tenant_id)
            )
        ).all()
        tasks = (await db.scalars(select(AgentTask).where(AgentTask.tenant_id == tenant_id))).all()
        result_sets = await db.scalar(
            select(func.count()).select_from(AgentResultSet).where(
                AgentResultSet.tenant_id == tenant_id
            )
        )
        resolved_audits = await db.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.tenant_id == tenant_id,
                AuditEvent.event_type == "agent.clarification.resolved",
            )
        )
        return {
            "turns": list(conversation.turns),
            "turn_version": conversation.turn_version,
            "active_turn_id": conversation.active_turn_id,
            "pointer": context.active_clarification_id,
            "active_result_set_id": context.active_result_set_id,
            "active_pending_draft_id": context.active_pending_draft_id,
            "clarifications": sorted(
                (str(row.id), row.status, row.attempt) for row in clarifications
            ),
            "tasks": sorted((str(row.id), row.status, row.task_type) for row in tasks),
            "result_sets": result_sets,
            "resolved_audits": resolved_audits,
        }


# ---------------------------------------------------------------------------
# 1. The final Phase B always starts fresh, principal first (D-092 §12.2)
# ---------------------------------------------------------------------------

_SCENARIOS = {
    # name: (setup messages, final kind, final message, expected status,
    #        model calls in the final turn, final Phase B lock sequence)
    "T1-create": ([], "text", SOURCE, 200, 0, PRINCIPAL + [
        "agent_conversations", "agent_conversation_session_contexts",
        "agent_turn_submissions"]),
    # The resumed search over "Python mütləqdir." is planned by the
    # deterministic semantic path: a no-inference resolving turn.
    "T2-label-search": ([SOURCE], "text", "namizəd axtarışı", 200, 0, PRINCIPAL + [
        "agent_conversations", "agent_conversation_session_contexts",
        "agent_clarifications", "agent_tasks", "agent_turn_submissions"]),
    # Classifier inference, then the resumed search: Phase B after inference.
    "T2m-model-search": ([SOURCE], "text", "bəli", 200, 1, PRINCIPAL + [
        "agent_conversations", "agent_conversation_session_contexts",
        "agent_clarifications", "agent_tasks", "agent_turn_submissions"]),
    "T3-button-vacancy": ([SOURCE], "button", "VACANCY_ANALYSIS", 200, 1, PRINCIPAL + [
        "agent_conversations", "agent_conversation_session_contexts",
        "agent_clarifications", "agent_tasks", "agent_turn_submissions"]),
    "T5-vsr-unclear": ([VSR_TRIGGER], "text", "hmm", 200, 0, PRINCIPAL + [
        "agent_conversations", "agent_conversation_session_contexts",
        "agent_clarifications", "agent_tasks", "agent_turn_submissions"]),
    "T6-attempts-exhausted": ([VSR_TRIGGER, "hmm"], "text", "hmm yenə", 200, 0, PRINCIPAL + [
        "agent_conversations", "agent_conversation_session_contexts",
        "agent_clarifications", "agent_tasks", "agent_turn_submissions"]),
    "T8-stale": ([SOURCE], "expire", "namizəd axtarışı", 200, 0, PRINCIPAL + [
        "agent_conversations", "agent_conversation_session_contexts",
        "agent_clarifications", "agent_tasks", "agent_turn_submissions"]),
    "T8a-classifier-failure": ([SOURCE], "text", "bəli", 503, 1, None),
    "T8b-rejected-button": ([SOURCE], "foreign-button", "CANDIDATE_SEARCH", 409, 0, None),
}


@pytest.mark.parametrize("scenario", list(_SCENARIOS))
async def test_final_phase_b_starts_fresh_and_locks_principal_first(
    small_pool, tenant_and_user, monkeypatch, scenario: str  # noqa: F811
) -> None:
    engine, factory, pool_probe = small_pool
    tenant, user, password, _membership = tenant_and_user
    setup, kind, final, expected_status, model_calls, phase_b_locks = _SCENARIOS[scenario]
    app.dependency_overrides[get_settings] = lambda: Settings(ui_cookie_secure=False)
    fake = _fake(
        **(
            {"clarification_error": ModelTimeoutError("t")}
            if scenario == "T8a-classifier-failure"
            else {"clarification_proposals": [ClarificationProposalValue.CANDIDATE_SEARCH]}
        )
    )
    llm = _PoolCheckingLLM(fake, pool_probe)
    app.dependency_overrides[get_llm_provider] = lambda: llm
    probe = TransactionProbe(engine)
    observed: list[tuple[bool, list[tuple[str, str]]]] = []
    original = turn_boundary._principal_is_live

    async def probing(db, reservation):  # noqa: ANN001, ANN202
        # Measured on the SAME backend, immediately before the principal
        # FOR SHARE: is a transaction already open, and which Phase-A
        # relation locks does this backend hold?
        fresh = not db.in_transaction()
        held = (
            await db.execute(
                text(
                    "SELECT c.relname, l.mode FROM pg_locks l "
                    "JOIN pg_class c ON c.oid = l.relation "
                    "WHERE l.pid = pg_backend_pid() AND l.locktype = 'relation' "
                    "AND c.relname = ANY(:tables)"
                ),
                {"tables": PHASE_A_TABLES},
            )
        ).all()
        observed.append((fresh, sorted((row[0], row[1]) for row in held)))
        return await original(db, reservation)

    monkeypatch.setattr(turn_boundary, "_principal_is_live", probing)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        csrf = await _login(client, user.username, password)
        conversation_id = await _new_conversation(client, csrf)
        for message in setup:
            response = await _post(
                client, csrf, conversation_id, message,
                submission_id=await _token(client, conversation_id),
            )
            assert response.status_code == 200
        extra: dict[str, str] = {}
        if kind == "expire":
            async with factory() as db:
                await db.execute(
                    AgentClarification.__table__.update().values(
                        expires_at=datetime.now(UTC) - timedelta(seconds=1)
                    )
                )
                await db.commit()
        if kind in ("button", "foreign-button"):
            (live,) = await _clarifications(factory, tenant.id)
            extra = {
                "clarification_id": str(live.id if kind == "button" else uuid.uuid4()),
                "clarification_choice": final,
            }
        token = await _token(client, conversation_id)
        llm.checked_out_at_call.clear()
        observed.clear()
        probe.reset()
        response = await _post(
            client, csrf, conversation_id,
            "Vakansiya tələbi kimi" if kind == "button" else (
                "Namizəd axtarışı" if kind == "foreign-button" else final
            ),
            submission_id=token, **extra,
        )
    assert response.status_code == expected_status, response.text
    # The scenario really is (or is not) a no-inference turn.
    assert len(llm.checked_out_at_call) == model_calls
    # #85: no request-engine connection is checked out during any model call.
    assert llm.checked_out_at_call == [0] * model_calls

    transactions = probe.transactions()
    # Every transaction of the turn obeys the global order (never a lower-
    # ranked row lock after a higher-ranked one inside one transaction).
    for statements in transactions.values():
        ranks = [LOCK_RANK[table] for table in lock_sequence(statements)]
        assert ranks == sorted(ranks), lock_sequence(statements)
    # Every principal revalidation ran in a fresh transaction holding NO
    # Phase-A conversation/context/submission lock.
    if model_calls or phase_b_locks is not None:
        assert observed, "the turn never revalidated its principal"
    for fresh, held in observed:
        assert fresh, "principal revalidated inside an earlier open transaction"
        assert held == [], f"Phase-A locks still held at principal revalidation: {held}"

    phase_a = [
        tx for tx, statements in transactions.items()
        if any(
            statement.startswith("UPDATE agent_turn_submissions") for statement in statements
        ) and not any(
            statement.startswith("UPDATE agent_conversations") and "turns=" in statement
            for statement in statements
        )
    ]
    phase_b = [
        tx for tx, statements in transactions.items()
        if lock_sequence(statements)[:1] == ["users"]
        and any(
            statement.startswith("UPDATE agent_conversations") and "turns=" in statement
            for statement in statements
        )
    ]
    if phase_b_locks is None:
        # T8a / T8b: abandoned — no consequential Phase B, nothing written.
        assert phase_b == []
        for statements in transactions.values():
            for table in ("agent_clarifications", "agent_tasks"):
                assert not _writes(statements, table), (table, statements)
            assert not any(
                statement.startswith("UPDATE agent_conversations") and "turns=" in statement
                for statement in statements
            )
        return
    (phase_b_tx,) = phase_b
    assert lock_sequence(transactions[phase_b_tx]) == phase_b_locks
    # Phase A (conversation lock + PROCESSING claim) ENDED with a COMMIT
    # before the first statement of the Phase B transaction.
    (phase_a_tx,) = phase_a
    assert phase_a_tx != phase_b_tx
    assert probe.index_of(phase_a_tx, "COMMIT") < probe.first_index(phase_b_tx)
    assert transactions[phase_b_tx][-1] == "COMMIT"


async def test_no_inference_turn_pre_phase_b_commit_persists_nothing_consequential(
    small_pool, tenant_and_user, monkeypatch  # noqa: F811
) -> None:
    """Fault injection at the exact gap the fix opens: a deterministic T1
    turn has committed Phase A; its session is revoked in an independent
    transaction before Phase B revalidates. Phase B fails closed, and the
    pre-Phase-B commit left no transcript, task, clarification or pointer."""
    _engine, factory, _pool_probe = small_pool
    tenant, user, password, _membership = tenant_and_user
    app.dependency_overrides[get_settings] = lambda: Settings(ui_cookie_secure=False)
    fake = _fake()
    app.dependency_overrides[get_llm_provider] = lambda: fake
    original = turn_boundary._principal_is_live

    async def revoke_then_check(db, reservation):  # noqa: ANN001, ANN202
        async with factory() as other:
            session = await other.get(BrowserSession, reservation.browser_session_id)
            assert session is not None
            session.revoked_at = datetime.now(UTC)
            await other.commit()  # would block if Phase A still held locks it needs
        return await original(db, reservation)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        csrf = await _login(client, user.username, password)
        conversation_id = await _new_conversation(client, csrf)
        token = await _token(client, conversation_id)
        monkeypatch.setattr(turn_boundary, "_principal_is_live", revoke_then_check)
        response = await _post(client, csrf, conversation_id, SOURCE, submission_id=token)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"
    state = await _snapshot(factory, tenant.id, conversation_id)
    assert state["turns"] == [] and state["turn_version"] == 0
    assert state["active_turn_id"] is None
    assert state["pointer"] is None and state["clarifications"] == [] and state["tasks"] == []
    assert fake.agent_call_count == 0
    async with factory() as db:
        submission = await db.get(AgentTurnSubmission, uuid.UUID(token))
        assert submission is not None and submission.status == "ABANDONED"
        stale = (
            await db.scalars(
                select(AuditEvent).where(
                    AuditEvent.tenant_id == tenant.id, AuditEvent.event_type == "agent.turn.stale"
                )
            )
        ).all()
        assert [dict(row.event_metadata) for row in stale] == [
            {"reason_code": "PRINCIPAL_REVOKED"}
        ]


# ---------------------------------------------------------------------------
# 2. Busy / cancel / revoke DURING a resumed turn (D-092 §22 item 22)
# ---------------------------------------------------------------------------
# A resumed SEARCH reaches local inference through the hybrid query
# embedding of a model-planned request (HYBRID_REQUEST is ambiguous, so it
# becomes a SEARCH_OR_VACANCY original); a resumed VACANCY through the real
# Ollama JD drafter. Both run through the ONE shared admission gate.


def _install_resumable(capability: str, settings: Settings, gate: GatedOllama) -> str:
    """Wire the gated provider for ``capability``; return its original."""
    app.dependency_overrides[get_settings] = lambda: settings
    if capability == "search":
        app.dependency_overrides[get_llm_provider] = lambda: FakeLLMProvider(
            planner_draft=PlannerDraft(
                required_filters=RequiredFilters(skills=["Java"]),
                semantic_query="backend modernization experience",
            ),
        )
        _install_gated_embedding(settings, gate)
        return HYBRID_REQUEST
    _install(settings, gate)
    return SOURCE


def _resume_answer(capability: str, clarification_id: uuid.UUID) -> dict[str, str]:
    if capability == "search":
        return {"message": "namizəd axtarışı"}
    return {
        "message": "Vakansiya tələbi kimi",
        "clarification_id": str(clarification_id),
        "clarification_choice": "VACANCY_ANALYSIS",
    }


async def _open_clarification(client, csrf, factory, tenant_id, source):  # noqa: ANN001, ANN202
    conversation_id = await _new_conversation(client, csrf)
    created = await _post(
        client, csrf, conversation_id, source,
        submission_id=await _token(client, conversation_id),
    )
    assert created.status_code == 200
    (clarification,) = await _clarifications(factory, tenant_id)
    assert clarification.status == "OPEN"
    return conversation_id, clarification


@pytest.mark.parametrize("capability", ["search", "vacancy"])
@pytest.mark.parametrize("failure", ["busy", "cancel", "revoke"])
async def test_resumed_turn_busy_cancel_or_revoke_leaves_clarification_open(
    small_pool, tenant_and_user, failure: str, capability: str  # noqa: F811
) -> None:
    _engine, factory, pool_probe = small_pool
    tenant, user, password, _membership = tenant_and_user
    settings = Settings(
        ui_cookie_secure=False, inference_concurrency=1,
        inference_queue_max_waiters=0 if failure == "busy" else 2,
    )
    gate = GatedOllama()
    source = _install_resumable(capability, settings, gate)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        csrf = await _login(client, user.username, password)
        conversation_id, clarification = await _open_clarification(
            client, csrf, factory, tenant.id, source
        )
        assert gate.calls == 0  # T1 is deterministic
        before = await _snapshot(factory, tenant.id, conversation_id)
        token = await _token(client, conversation_id)
        answer = _resume_answer(capability, clarification.id)
        if failure == "busy":
            # Another local model call holds the only slot; no waiter room.
            admission = concurrency.get_inference_admission(
                max_active=1, max_queued=0,
                queue_timeout_seconds=settings.inference_queue_timeout_seconds,
            )
            holder = admission.slot()
            await holder.__aenter__()
            try:
                response = await asyncio.wait_for(
                    _post(client, csrf, conversation_id, submission_id=token, **answer),
                    timeout=10,
                )
            finally:
                await holder.__aexit__(None, None, None)
            assert response.status_code == 503
            assert BUSY_COPY in response.text
            _assert_hr_safe(response.text)
            assert gate.calls == 0  # the resumed capability was refused admission
        else:
            resumed = _spawn(_post(client, csrf, conversation_id, submission_id=token, **answer))
            # The resumed capability (query embedding / JD drafter) is running.
            await asyncio.wait_for(gate.entered.wait(), timeout=10)
            assert pool_probe.checked_out == 0
            if failure == "cancel":
                resumed.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await resumed
            else:
                # Independent transaction, committed while the turn infers.
                async with factory() as db:
                    for session in (
                        await db.scalars(
                            select(BrowserSession).where(BrowserSession.user_id == user.id)
                        )
                    ).all():
                        session.revoked_at = datetime.now(UTC)
                    await db.commit()
                gate.release.set()
                response = await asyncio.wait_for(resumed, timeout=10)
                assert response.status_code == 303
                assert response.headers["location"] == "/ui/login"
        gate.release.set()
    after = await _snapshot(factory, tenant.id, conversation_id)
    # Nothing of the failed resumed attempt is durable: same transcript
    # (no duplicate turn), version, pointers, rows and audits.
    assert after == before
    assert after["active_turn_id"] is None
    assert after["clarifications"] == [(str(clarification.id), "OPEN", 1)]
    assert after["pointer"] == clarification.id
    (task,) = after["tasks"]
    assert task[1] == "WAITING_CLARIFICATION"
    assert after["active_result_set_id"] is None and after["active_pending_draft_id"] is None
    assert after["resolved_audits"] == 0
    async with factory() as db:
        submission = await db.get(AgentTurnSubmission, uuid.UUID(token))
        assert submission is not None and submission.status == "ABANDONED"
        assert submission.reservation_id is None


# ---------------------------------------------------------------------------
# 3. Two real concurrent answers to one clarification (D-092 §22 item 10)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("second", ["label", "button"])
async def test_two_tokens_answering_concurrently_resolve_exactly_once(
    small_pool, tenant_and_user, second: str  # noqa: F811
) -> None:
    _engine, factory, _pool_probe = small_pool
    tenant, user, password, _membership = tenant_and_user
    gate = GatedOllama()
    source = _install_resumable(
        "search", Settings(ui_cookie_secure=False, inference_concurrency=1), gate
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        csrf = await _login(client, user.username, password)
        conversation_id, clarification = await _open_clarification(
            client, csrf, factory, tenant.id, source
        )
        before = await _snapshot(factory, tenant.id, conversation_id)
        # Two independently issued, both ISSUED, server tokens.
        first_token = await _token(client, conversation_id)
        second_token = await _token(client, conversation_id)
        assert first_token != second_token
        first = _spawn(_post(
            client, csrf, conversation_id, "namizəd axtarışı", submission_id=first_token
        ))
        # Overlap is coordinated, never slept: the first answer is inside
        # its resumed search's local inference when the second is submitted.
        await asyncio.wait_for(gate.entered.wait(), timeout=10)
        second_answer = (
            {"message": "namizəd axtarışı"} if second == "label" else {
                "message": "Namizəd axtarışı",
                "clarification_id": str(clarification.id),
                "clarification_choice": "CANDIDATE_SEARCH",
            }
        )
        loser = await asyncio.wait_for(
            _post(client, csrf, conversation_id, submission_id=second_token, **second_answer),
            timeout=10,
        )
        assert loser.status_code == 409  # #85 in-progress, fail closed
        assert gate.calls == 1
        gate.release.set()
        winner = await asyncio.wait_for(first, timeout=10)
        assert winner.status_code == 200
    after = await _snapshot(factory, tenant.id, conversation_id)
    assert after["clarifications"] == [(str(clarification.id), "RESOLVED", 1)]
    (task,) = after["tasks"]
    assert task[1:] == ("COMPLETED", "CANDIDATE_SEARCH")
    assert after["resolved_audits"] == 1
    assert after["result_sets"] == 1
    assert len(after["turns"]) == len(before["turns"]) + 2
    assert after["pointer"] is None and after["active_turn_id"] is None
    async with factory() as db:
        resolved = await db.get(AgentClarification, clarification.id)
        assert resolved is not None
        assert str(resolved.resolved_by_submission_id) == first_token
        losing = await db.get(AgentTurnSubmission, uuid.UUID(second_token))
        assert losing is not None and losing.status == "ABANDONED"
        changed = (
            await db.scalars(
                select(AuditEvent).where(
                    AuditEvent.tenant_id == tenant.id,
                    AuditEvent.event_type == "agent.task.state_changed",
                )
            )
        ).all()
        assert [row.event_metadata["to_status"] for row in changed] == ["COMPLETED"]
