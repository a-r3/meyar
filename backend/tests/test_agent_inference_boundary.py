"""Issue #85 — agent inference transaction boundary and overload control.

These tests drive the production ``POST /ui/agent`` path (router → phase A
reservation → orchestration → phase B commit) with the REAL
``OllamaLLMProvider`` (and therefore the real process-wide inference
admission gate) talking to a gated in-memory httpx transport — never a real
Ollama. Every request gets its own ``AsyncSession`` from a deliberately tiny
connection pool, so a request that keeps a PostgreSQL connection checked out
while waiting for, or running, local inference starves every other route.

Synthetic data only."""

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncGenerator, Callable

import httpx
import pytest
from agent_plans import converse, plan, profile_plan, profile_step, search_plan, search_step
from conftest import TEST_DATABASE_URL
from conftest import BrowserTestClient as AsyncClient
from httpx import ASGITransport
from search_helpers import seed_candidate_with_profile
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from meyar.config import Settings, get_settings
from meyar.db import get_db
from meyar.llm import concurrency
from meyar.llm.dependency import get_llm_provider
from meyar.llm.ollama_provider import OllamaLLMProvider
from meyar.main import app
from meyar.models.agent_conversation import AgentConversation
from meyar.storage.dependency import get_document_storage, get_photo_storage
from meyar.storage.local import LocalFilesystemStorage
from meyar.storage.photo import LocalPhotoStorage

MODEL = "meyar-test-llm:v1"
# Issue #88 slice C: canned agent-plan-v1 proposals as the raw Ollama JSON.
GREETING_DECISION = converse("GREETING").model_dump(mode="json")
FIRST_PROFILE_PLAN = profile_plan("birincinin").model_dump(mode="json")


def _empty_plan_context():  # noqa: ANN202
    from meyar.agent.capabilities.contracts import AgentPlanContext

    return AgentPlanContext(
        recent_turns=[],
        available_capabilities=[],
        max_plan_steps=1,
        active_result_context_present=False,
        available_candidate_refs=[],
        pending_vacancy_confirmation=False,
    )


_EMPTY_PLAN_CONTEXT = _empty_plan_context()

# Unrelated DB-backed routes must answer inside this budget while the model
# is blocked. Pool checkout timeout below is deliberately LONGER than this
# budget so pool starvation is observed as a budget violation, not masked.
UNRELATED_ROUTE_BUDGET_SECONDS = 5.0
POOL_TIMEOUT_SECONDS = 8.0


class GatedOllama:
    """In-memory stand-in for the local Ollama ``/api/chat`` and
    ``/api/embeddings`` endpoints.

    Calls after the first ``pass_through`` block on ``release`` so a test can
    observe the system while inference is genuinely in flight. ``on_enter``
    runs synchronously the moment a blocking call is actually executing
    (i.e. after admission). ``decisions`` are returned in order (the last
    one repeats)."""

    def __init__(
        self,
        decision: dict | None = None,
        *,
        decisions: list[dict] | None = None,
        pass_through: int = 0,
    ) -> None:
        self.release = asyncio.Event()
        self.entered = asyncio.Event()
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.on_enter: Callable[[], None] | None = None
        self._decisions = decisions or [decision or GREETING_DECISION]
        self._pass_through = pass_through

    async def handler(self, request: httpx.Request) -> httpx.Response:
        index = self.calls
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if index >= self._pass_through:
                if self.on_enter is not None:
                    self.on_enter()
                self.entered.set()
                await self.release.wait()
        finally:
            self.active -= 1
        if request.url.path.endswith("/api/embeddings"):
            return httpx.Response(200, json={"embedding": [0.1 * (i + 1) for i in range(8)]})
        decision = self._decisions[min(index, len(self._decisions) - 1)]
        return httpx.Response(
            200, json={"model": MODEL, "message": {"content": json.dumps(decision)}}
        )


_GATES: list["GatedOllama"] = []
_TASKS: list[asyncio.Task] = []


def _spawn(coro) -> asyncio.Task:  # noqa: ANN001
    task = asyncio.create_task(coro)
    _TASKS.append(task)
    return task


class PoolProbe:
    """Tracks real PostgreSQL pool checkouts for the test engine."""

    def __init__(self, engine) -> None:  # noqa: ANN001 - AsyncEngine
        self.checked_out = 0
        pool = engine.sync_engine.pool

        @event.listens_for(pool, "checkout")
        def _checkout(*_args) -> None:  # noqa: ANN002
            self.checked_out += 1

        @event.listens_for(pool, "checkin")
        def _checkin(*_args) -> None:  # noqa: ANN002
            self.checked_out -= 1


@pytest.fixture
async def small_pool(db_session: AsyncSession, tmp_path):  # noqa: ANN201
    """A per-request-session app wired to a tiny, bounded pool."""
    engine = create_async_engine(
        TEST_DATABASE_URL, pool_size=2, max_overflow=0, pool_timeout=POOL_TIMEOUT_SECONDS
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    probe = PoolProbe(engine)

    async def _get_db() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    storage = LocalFilesystemStorage(root=str(tmp_path / "storage"))
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_document_storage] = lambda: storage
    app.dependency_overrides[get_photo_storage] = lambda: LocalPhotoStorage(
        root=str(tmp_path / "storage")
    )
    concurrency.reset_inference_admission()
    try:
        yield engine, factory, probe
    finally:
        # Never leave a blocked model call (and whatever it holds) behind.
        for gate in _GATES:
            gate.release.set()
        for task in _TASKS:
            if not task.done():
                task.cancel()
        await asyncio.gather(*_TASKS, return_exceptions=True)
        _GATES.clear()
        _TASKS.clear()
        app.dependency_overrides.clear()
        concurrency.reset_inference_admission()
        await engine.dispose()


def _install(settings: Settings, gate: GatedOllama) -> None:
    _GATES.append(gate)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_llm_provider] = lambda: OllamaLLMProvider(
        base_url="http://127.0.0.1:11434",
        model=MODEL,
        timeout_seconds=30.0,
        transport=httpx.MockTransport(gate.handler),
        max_concurrency=settings.inference_concurrency,
        max_queued=settings.inference_queue_max_waiters,
        queue_timeout_seconds=settings.inference_queue_timeout_seconds,
    )


async def _login(client: AsyncClient, username: str, password: str) -> str:
    response = await client.post(
        "/ui/login", data={"username": username, "password": password}, follow_redirects=False
    )
    assert response.status_code == 303
    home = await client.get("/ui")
    match = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', home.text)
    assert match is not None
    return match.group(1)


async def _new_conversation(client: AsyncClient, csrf: str) -> uuid.UUID:
    response = await client.post(
        "/ui/agent/reset", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert response.status_code == 303
    return uuid.UUID(response.headers["location"].rsplit("=", 1)[1])


async def _timed(coro) -> tuple[httpx.Response, float]:  # noqa: ANN001
    started = time.monotonic()
    response = await asyncio.wait_for(coro, timeout=UNRELATED_ROUTE_BUDGET_SECONDS)
    return response, time.monotonic() - started


async def test_model_call_holds_no_db_connection_and_unrelated_route_stays_fast(
    small_pool, tenant_and_user
) -> None:
    """Acceptance A / #85 mandatory point: while the agent's model call is
    blocked, the agent request holds NO pooled connection, an unrelated
    DB-backed route succeeds within budget, and the turn commits once the
    model is released."""
    engine, factory, probe = small_pool
    _tenant, user, password, _membership = tenant_and_user
    gate = GatedOllama()
    _install(Settings(ui_cookie_secure=False, inference_concurrency=1), gate)
    observed: dict[str, object] = {}

    def _observe() -> None:
        observed["checked_out_during_model"] = probe.checked_out

    gate.on_enter = _observe
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        csrf = await _login(client, user.username, password)
        conversation_id = await _new_conversation(client, csrf)
        baseline = probe.checked_out
        turn = _spawn(
            client.post(
                "/ui/agent",
                data={
                    "message": "salam",
                    "csrf_token": csrf,
                    "conversation_id": str(conversation_id),
                },
            )
        )
        await asyncio.wait_for(gate.entered.wait(), timeout=10)
        assert observed["checked_out_during_model"] == baseline
        assert probe.checked_out == baseline

        library, elapsed = await _timed(client.get("/ui/library"))
        assert library.status_code == 200
        assert elapsed < UNRELATED_ROUTE_BUDGET_SECONDS

        gate.release.set()
        response = await asyncio.wait_for(turn, timeout=10)
    assert response.status_code == 200
    async with factory() as db:
        stored = await db.get(AgentConversation, conversation_id)
        assert stored is not None
        assert [t["role"] for t in stored.turns] == ["user", "assistant"]
        assert stored.turns[0]["text"] == "salam"


async def test_plan_proposal_and_its_repair_hold_no_db_connection(
    small_pool, tenant_and_user
) -> None:
    """Issue #88 slice C (#85, §22 item 23): the ONE plan proposal and its
    ONE repair both run with no pooled connection checked out, and the turn
    then commits normally."""
    engine, factory, probe = small_pool
    _tenant, user, password, _membership = tenant_and_user
    gate = GatedOllama(decisions=[{"not": "an agent plan"}, GREETING_DECISION])
    _install(Settings(ui_cookie_secure=False, inference_concurrency=1), gate)
    during_model: list[int] = []
    gate.on_enter = lambda: during_model.append(probe.checked_out)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        csrf = await _login(client, user.username, password)
        conversation_id = await _new_conversation(client, csrf)
        baseline = probe.checked_out
        turn = _spawn(_agent_post(client, csrf, conversation_id, "salam"))
        await asyncio.wait_for(gate.entered.wait(), timeout=10)
        gate.release.set()
        response = await asyncio.wait_for(turn, timeout=10)
    assert response.status_code == 200
    assert gate.calls == 2  # proposal + exactly one repair, no third call
    assert during_model == [baseline, baseline]
    stored = await _stored(factory, conversation_id)
    assert [t["role"] for t in stored.turns] == ["user", "assistant"]
    assert stored.turns[-1]["outcome"] == "ANSWERED"


async def test_twenty_slow_turns_on_distinct_conversations_do_not_starve_db_routes(
    small_pool, tenant_and_user, db_session: AsyncSession
) -> None:
    """Acceptance B: 20+ model-bound turns on DISTINCT conversations with a
    blocked model, a 2-connection pool, and inference concurrency 1. The
    library, candidate detail and login routes keep answering (no 500 from
    pool exhaustion) within the budget; excess AI work degrades instead."""
    engine, factory, probe = small_pool
    tenant, user, password, _membership = tenant_and_user
    candidate, _version = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content={
            "skills": [], "employment_history": [], "education": [],
            "certifications": [], "languages": [], "projects": [],
        }
    )
    await db_session.commit()
    gate = GatedOllama()
    _install(Settings(ui_cookie_secure=False, inference_concurrency=1), gate)
    turns_count = 22
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        csrf = await _login(client, user.username, password)
        conversations = [await _new_conversation(client, csrf) for _ in range(turns_count)]
        tasks = [
            _spawn(
                client.post(
                    "/ui/agent",
                    data={"message": "salam", "csrf_token": csrf, "conversation_id": str(cid)},
                )
            )
            for cid in conversations
        ]
        await asyncio.wait_for(gate.entered.wait(), timeout=10)
        # Let every turn reach its steady state (queued, rejected, or
        # blocked in the model).
        await asyncio.sleep(0.5)

        library, library_elapsed = await _timed(client.get("/ui/library"))
        detail, detail_elapsed = await _timed(client.get(f"/ui/candidates/{candidate.id}"))
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as other:
            login, login_elapsed = await _timed(
                other.post(
                    "/ui/login",
                    data={"username": user.username, "password": password},
                    follow_redirects=False,
                )
            )
        assert library.status_code == 200, library.status_code
        assert detail.status_code == 200, detail.status_code
        assert login.status_code == 303, login.status_code
        for elapsed in (library_elapsed, detail_elapsed, login_elapsed):
            assert elapsed < UNRELATED_ROUTE_BUDGET_SECONDS
        # Inference capacity is still honoured: at most one model call.
        assert gate.max_active == 1

        gate.release.set()
        responses = await asyncio.wait_for(asyncio.gather(*tasks), timeout=60)
    statuses = [response.status_code for response in responses]
    assert 500 not in statuses
    # Every turn either completed or received the truthful BUSY outcome.
    assert set(statuses) <= {200, 503}
    assert statuses.count(200) >= 1
    async with factory() as db:
        stored = (
            await db.scalars(
                select(AgentConversation).where(AgentConversation.id.in_(conversations))
            )
        ).all()
    completed = sum(1 for conversation in stored if conversation.turns)
    assert completed == statuses.count(200)


# ---------------------------------------------------------------------------
# Shared helpers for the acceptance matrix
# ---------------------------------------------------------------------------

BUSY_COPY = "MEYAR hazırda digər sorğuları emal edir. Bir qədər sonra yenidən cəhd edin."
STALE_COPY_FRAGMENT = "söhbətin vəziyyəti dəyişdi"
IN_PROGRESS_COPY_FRAGMENT = "əvvəlki sorğu hələ emal olunur"
FORBIDDEN_INTERNALS = (
    "QUEUE_FULL", "QUEUE_TIMEOUT", "semaphore", "queue", "pool", "Traceback",
    "InferenceBusyError", "AgentInferenceBusyError", "TurnAuthorityLostError",
    "CONTEXT_CHANGED", "PRINCIPAL_REVOKED", "CONVERSATION_CHANGED", "RESERVATION_LOST",
    "Ollama",
)


def _assert_hr_safe(html: str) -> None:
    for token in FORBIDDEN_INTERNALS:
        assert token not in html, token


def _agent_post(client: AsyncClient, csrf: str, conversation_id: uuid.UUID, message: str):
    return client.post(
        "/ui/agent",
        data={"message": message, "csrf_token": csrf, "conversation_id": str(conversation_id)},
    )


async def _stored(factory, conversation_id: uuid.UUID) -> AgentConversation:  # noqa: ANN001
    async with factory() as db:
        stored = await db.get(AgentConversation, conversation_id)
        assert stored is not None
        return stored


async def _audit(factory, tenant_id: uuid.UUID, event_type: str) -> list[dict]:  # noqa: ANN001
    from meyar.models.audit_event import AuditEvent

    async with factory() as db:
        rows = (
            await db.scalars(
                select(AuditEvent).where(
                    AuditEvent.tenant_id == tenant_id, AuditEvent.event_type == event_type
                )
            )
        ).all()
        return [dict(row.event_metadata) for row in rows]


async def _only_context(factory, conversation_id: uuid.UUID):  # noqa: ANN001, ANN202
    from meyar.models.agent_conversation import AgentConversationSessionContext

    async with factory() as db:
        return (
            await db.scalars(
                select(AgentConversationSessionContext).where(
                    AgentConversationSessionContext.conversation_id == conversation_id
                )
            )
        ).one()


async def _block_first_turn(client, csrf, conversation_id, gate):  # noqa: ANN001, ANN202
    task = _spawn(_agent_post(client, csrf, conversation_id, "salam"))
    await asyncio.wait_for(gate.entered.wait(), timeout=10)
    return task


# ---------------------------------------------------------------------------
# C / D — bounded queue: full and timeout -> HR-safe BUSY, never 500
# ---------------------------------------------------------------------------


async def test_queue_full_turn_gets_hr_safe_busy_outcome_and_nothing_is_persisted(
    small_pool, tenant_and_user
) -> None:
    engine, factory, probe = small_pool
    tenant, user, password, _membership = tenant_and_user
    gate = GatedOllama()
    _install(
        Settings(ui_cookie_secure=False, inference_concurrency=1, inference_queue_max_waiters=0),
        gate,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        csrf = await _login(client, user.username, password)
        first_id = await _new_conversation(client, csrf)
        second_id = await _new_conversation(client, csrf)
        first = await _block_first_turn(client, csrf, first_id, gate)

        started = time.monotonic()
        busy = await asyncio.wait_for(
            _agent_post(client, csrf, second_id, "salam, necəsən?"), timeout=5
        )
        assert time.monotonic() - started < UNRELATED_ROUTE_BUDGET_SECONDS
        assert busy.status_code == 503
        assert BUSY_COPY in busy.text
        assert "salam, necəsən?" in busy.text  # HR's own text is not lost
        _assert_hr_safe(busy.text)
        assert gate.calls == 1  # the rejected turn never reached the model

        gate.release.set()
        first_response = await asyncio.wait_for(first, timeout=10)
        assert first_response.status_code == 200, first_response.text

    rejected = await _stored(factory, second_id)
    assert rejected.turns == []  # no fabricated assistant answer
    assert rejected.active_turn_id is None and rejected.active_turn_expires_at is None
    assert rejected.turn_version == 0
    events = await _audit(factory, tenant.id, "agent.turn.busy")
    assert events == [{"reason_code": "QUEUE_FULL"}]
    completed = await _stored(factory, first_id)
    assert [t["role"] for t in completed.turns] == ["user", "assistant"]
    assert completed.active_turn_id is None


async def test_queue_timeout_turn_gets_hr_safe_busy_outcome(
    small_pool, tenant_and_user
) -> None:
    engine, factory, probe = small_pool
    tenant, user, password, _membership = tenant_and_user
    gate = GatedOllama()
    _install(
        Settings(
            ui_cookie_secure=False,
            inference_concurrency=1,
            inference_queue_max_waiters=1,
            inference_queue_timeout_seconds=0.3,
        ),
        gate,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        csrf = await _login(client, user.username, password)
        first_id = await _new_conversation(client, csrf)
        second_id = await _new_conversation(client, csrf)
        first = await _block_first_turn(client, csrf, first_id, gate)
        waited = await asyncio.wait_for(_agent_post(client, csrf, second_id, "salam"), timeout=5)
        assert waited.status_code == 503
        assert BUSY_COPY in waited.text
        _assert_hr_safe(waited.text)
        admission = concurrency.current_inference_admission()
        assert admission is not None and admission.queued == 0
        gate.release.set()
        assert (await asyncio.wait_for(first, timeout=10)).status_code == 200
    assert (await _stored(factory, second_id)).turns == []
    assert await _audit(factory, tenant.id, "agent.turn.busy") == [{"reason_code": "QUEUE_TIMEOUT"}]


# ---------------------------------------------------------------------------
# E / F — cancellation (client gone) while queued / while active
# ---------------------------------------------------------------------------


async def test_cancelled_request_while_queued_releases_queue_and_reservation(
    small_pool, tenant_and_user
) -> None:
    engine, factory, probe = small_pool
    _tenant, user, password, _membership = tenant_and_user
    gate = GatedOllama()
    _install(
        Settings(ui_cookie_secure=False, inference_concurrency=1, inference_queue_max_waiters=2),
        gate,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        csrf = await _login(client, user.username, password)
        first_id = await _new_conversation(client, csrf)
        queued_id = await _new_conversation(client, csrf)
        first = await _block_first_turn(client, csrf, first_id, gate)
        queued = asyncio.create_task(_agent_post(client, csrf, queued_id, "salam"))
        admission = concurrency.current_inference_admission()
        assert admission is not None
        for _ in range(200):
            if admission.queued == 1:
                break
            await asyncio.sleep(0.01)
        assert admission.queued == 1
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert admission.queued == 0
        assert probe.checked_out == 0  # nothing leaked by the cancelled turn
        gate.release.set()
        assert (await asyncio.wait_for(first, timeout=10)).status_code == 200
    assert gate.calls == 1  # the cancelled turn never ran inference later
    abandoned = await _stored(factory, queued_id)
    assert abandoned.turns == []
    assert abandoned.active_turn_id is None  # shielded abandon cleared it
    assert (admission.active, admission.queued) == (0, 0)


async def test_cancelled_request_while_model_active_releases_inference_slot(
    small_pool, tenant_and_user
) -> None:
    engine, factory, probe = small_pool
    _tenant, user, password, _membership = tenant_and_user
    gate = GatedOllama()
    _install(Settings(ui_cookie_secure=False, inference_concurrency=1), gate)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        csrf = await _login(client, user.username, password)
        conversation_id = await _new_conversation(client, csrf)
        active = asyncio.create_task(_agent_post(client, csrf, conversation_id, "salam"))
        await asyncio.wait_for(gate.entered.wait(), timeout=10)
        admission = concurrency.current_inference_admission()
        assert admission is not None and admission.active == 1
        active.cancel()
        with pytest.raises(asyncio.CancelledError):
            await active
        assert admission.active == 0
        assert gate.active == 0  # the in-flight model HTTP call was cancelled
        # Capacity is immediately reusable.
        gate.release.set()
        again = await asyncio.wait_for(
            _agent_post(client, csrf, conversation_id, "salam"), timeout=10
        )
        assert again.status_code == 200
    stored = await _stored(factory, conversation_id)
    assert [t["text"] for t in stored.turns if t["role"] == "user"] == ["salam"]
    assert stored.active_turn_id is None


async def test_client_disconnect_cancels_queued_work_without_detached_tasks() -> None:
    """The route's disconnect watcher: when the ASGI client goes away, the
    queued/active work is cancelled (admission released), nothing keeps
    running in the background, and a typed error is raised."""
    from meyar.agent.turn_boundary import ClientDisconnectedError, run_until_client_disconnects

    gate = concurrency.InferenceAdmission(max_active=1, max_queued=1, queue_timeout_seconds=30)
    holder = gate.slot()
    await holder.__aenter__()
    finished = False

    class _Request:
        polls = 0

        async def is_disconnected(self) -> bool:
            self.polls += 1
            return self.polls >= 2

    async def work() -> str:
        nonlocal finished
        async with gate.slot():
            finished = True
            return "never"

    before = {task for task in asyncio.all_tasks()}
    with pytest.raises(ClientDisconnectedError):
        await run_until_client_disconnects(_Request(), work(), poll_interval_seconds=0.01)
    assert not finished
    assert (gate.active, gate.queued) == (1, 0)  # only the unrelated holder
    await holder.__aexit__(None, None, None)
    assert (gate.active, gate.queued) == (0, 0)
    leftover = {task for task in asyncio.all_tasks() if not task.done()} - before
    assert leftover == set()

    class _Connected:
        async def is_disconnected(self) -> bool:
            return False

    async def quick() -> str:
        return "done"

    assert await run_until_client_disconnects(_Connected(), quick()) == "done"


# ---------------------------------------------------------------------------
# G — same conversation, two BrowserSessions: serialized and coherent
# ---------------------------------------------------------------------------


async def test_three_sessions_same_conversation_one_accepted_turn_others_rejected(
    small_pool, tenant_and_user
) -> None:
    """Blocker C contract (D-089): at most ONE accepted in-flight turn per
    conversation, and no hidden wait queue. Three BrowserSessions submit A,
    B, C to the same conversation in a controlled staggered order while A
    is blocked in the model: B and C get the truthful 409 at once (not
    after a wait), never reach the model, never appear in the transcript
    and never touch A's live authority. After A completes, a retry of B is
    accepted. Repeated rounds give the identical outcome — nothing depends
    on poll or scheduling luck."""
    engine, factory, probe = small_pool
    tenant, user, password, _membership = tenant_and_user
    _install(Settings(ui_cookie_secure=False, inference_concurrency=1), GatedOllama())
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as tab_a,
        AsyncClient(transport=transport, base_url="http://test") as tab_b,
        AsyncClient(transport=transport, base_url="http://test") as tab_c,
    ):
        csrf_a = await _login(tab_a, user.username, password)
        csrf_b = await _login(tab_b, user.username, password)
        csrf_c = await _login(tab_c, user.username, password)
        conversation_id = await _new_conversation(tab_a, csrf_a)
        expected_users: list[str] = []
        # Digit-free, model-routed texts (a digit could read as a count).
        rounds = ("birinci", "ikinci", "üçüncü")
        for round_number in rounds:
            gate = GatedOllama()
            _install(Settings(ui_cookie_secure=False, inference_concurrency=1), gate)
            concurrency.reset_inference_admission()
            a_text = f"salam A {round_number}"
            first = _spawn(_agent_post(tab_a, csrf_a, conversation_id, a_text))
            await asyncio.wait_for(gate.entered.wait(), timeout=10)
            token = (await _stored(factory, conversation_id)).active_turn_id
            assert token is not None
            for client, csrf, text in (
                (tab_b, csrf_b, f"salam B {round_number}"),
                (tab_c, csrf_c, f"salam C {round_number}"),
            ):
                refused, elapsed = await _timed(_agent_post(client, csrf, conversation_id, text))
                assert refused.status_code == 409
                assert IN_PROGRESS_COPY_FRAGMENT in refused.text
                assert text in refused.text  # HR's own text is shown back, not lost
                _assert_hr_safe(refused.text)
                assert elapsed < 2.0  # immediate: no hidden wait queue
            assert gate.calls == 1  # B/C never reached the model
            # A's reservation (and live authority) untouched by B/C.
            assert (await _stored(factory, conversation_id)).active_turn_id == token
            gate.release.set()
            assert (await asyncio.wait_for(first, timeout=10)).status_code == 200
            expected_users.append(a_text)
            # Retry after A finished: accepted.
            retry = await asyncio.wait_for(
                _agent_post(tab_b, csrf_b, conversation_id, f"salam B {round_number} retry"),
                timeout=10,
            )
            assert retry.status_code == 200
            expected_users.append(f"salam B {round_number} retry")
            stored = await _stored(factory, conversation_id)
            assert [t["text"] for t in stored.turns if t["role"] == "user"] == expected_users
            assert stored.active_turn_id is None
    assert len(stored.turns) == 2 * len(expected_users)
    assert len(await _audit(factory, tenant.id, "agent.turn.rejected")) == 6


# ---------------------------------------------------------------------------
# H / L — different conversations independent; deterministic turns fast
# ---------------------------------------------------------------------------


async def test_different_conversations_never_wait_on_each_others_lock_only_on_capacity(
    small_pool, tenant_and_user
) -> None:
    """Conversation serialization != global model capacity. While turn A
    holds the ONLY inference slot, a deterministic turn on conversation B
    completes at once (no conversation lock shared), and a model-routed turn
    on conversation C is admitted only after A leaves the model."""
    engine, factory, probe = small_pool
    _tenant, user, password, _membership = tenant_and_user
    gate = GatedOllama()
    _install(
        Settings(ui_cookie_secure=False, inference_concurrency=1, inference_queue_max_waiters=2),
        gate,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        csrf = await _login(client, user.username, password)
        a_id = await _new_conversation(client, csrf)
        b_id = await _new_conversation(client, csrf)
        c_id = await _new_conversation(client, csrf)
        first = await _block_first_turn(client, csrf, a_id, gate)

        # L: server-routed count follow-up with no active result -> no model.
        deterministic, elapsed = await _timed(_agent_post(client, csrf, b_id, "ilk 3"))
        assert deterministic.status_code == 200
        assert elapsed < UNRELATED_ROUTE_BUDGET_SECONDS
        assert gate.calls == 1  # B never touched the inference gate

        third = _spawn(_agent_post(client, csrf, c_id, "salam"))
        admission = concurrency.current_inference_admission()
        assert admission is not None
        for _ in range(200):
            if admission.queued == 1:
                break
            await asyncio.sleep(0.01)
        assert (admission.active, admission.queued) == (1, 1)  # capacity, not a lock
        gate.release.set()
        responses = await asyncio.wait_for(asyncio.gather(first, third), timeout=10)
    assert [response.status_code for response in responses] == [200, 200]
    assert gate.max_active == 1
    assert len((await _stored(factory, b_id)).turns) == 2


# ---------------------------------------------------------------------------
# I / J / K — authority changed while inferring -> fail closed
# ---------------------------------------------------------------------------


async def _run_with_mutation_during_inference(
    small_pool, tenant_and_user, mutate, *, conversation_setup=None
):  # noqa: ANN001, ANN202
    engine, factory, probe = small_pool
    tenant, user, password, membership = tenant_and_user
    gate = GatedOllama()
    _install(Settings(ui_cookie_secure=False, inference_concurrency=1), gate)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        csrf = await _login(client, user.username, password)
        if conversation_setup is None:
            conversation_id = await _new_conversation(client, csrf)
        else:
            conversation_id = await conversation_setup(factory, tenant, user, membership)
        turn = _spawn(_agent_post(client, csrf, conversation_id, "salam"))
        await asyncio.wait_for(gate.entered.wait(), timeout=10)
        async with factory() as db:
            await mutate(db, conversation_id=conversation_id, user=user, membership=membership)
            await db.commit()
        gate.release.set()
        response = await asyncio.wait_for(turn, timeout=10)
    return factory, tenant, conversation_id, response


async def test_membership_disabled_while_inferring_fails_closed(
    small_pool, tenant_and_user
) -> None:
    from meyar.models.tenant_membership import TenantMembership

    async def disable(db, *, membership, **_):  # noqa: ANN001, ANN003, ANN202
        row = await db.get(TenantMembership, membership.id)
        row.is_active = False

    factory, tenant, conversation_id, response = await _run_with_mutation_during_inference(
        small_pool, tenant_and_user, disable
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"
    stored = await _stored(factory, conversation_id)
    assert stored.turns == []
    assert stored.active_turn_id is None
    assert await _audit(factory, tenant.id, "agent.turn.stale") == [
        {"reason_code": "PRINCIPAL_REVOKED"}
    ]


async def test_browser_session_revoked_while_inferring_fails_closed(
    small_pool, tenant_and_user
) -> None:
    from datetime import UTC, datetime

    from meyar.models.browser_session import BrowserSession

    async def revoke(db, *, user, **_):  # noqa: ANN001, ANN003, ANN202
        for session in (
            await db.scalars(select(BrowserSession).where(BrowserSession.user_id == user.id))
        ).all():
            session.revoked_at = datetime.now(UTC)

    factory, tenant, conversation_id, response = await _run_with_mutation_during_inference(
        small_pool, tenant_and_user, revoke
    )
    assert response.status_code == 303
    assert (await _stored(factory, conversation_id)).turns == []


@pytest.mark.parametrize("change", ["password", "membership", "tenant"])
async def test_issue87_security_change_during_inference_rejects_phase_b(
    small_pool, tenant_and_user, change: str
) -> None:
    from meyar.services.tenant_membership_repo import set_membership_active
    from meyar.services.user_repo import set_password

    async def mutate(db, *, user, membership, **_):  # noqa: ANN001, ANN003, ANN202
        assert small_pool[2].checked_out == 0  # inference holds no DB connection
        if change == "tenant":
            from meyar.services.tenant_authority import set_tenant_active

            await set_tenant_active(db, tenant_id=membership.tenant_id, is_active=False)
        elif change == "password":
            await set_password(
                db, user_id=user.id, plaintext_password="rotated-synthetic-password"
            )
        else:
            await set_membership_active(db, membership_id=membership.id, is_active=False)

    factory, tenant, conversation_id, response = await _run_with_mutation_during_inference(
        small_pool, tenant_and_user, mutate
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"
    stored = await _stored(factory, conversation_id)
    assert stored.turns == []
    assert stored.active_turn_id is None
    from sqlalchemy import func

    from meyar.models.agent_conversation import AgentConversationSessionContext
    from meyar.models.agent_result_set import AgentResultSet
    from meyar.models.candidate import Candidate
    from meyar.models.job import Job

    async with factory() as db:
        context = await db.scalar(select(AgentConversationSessionContext).where(
            AgentConversationSessionContext.conversation_id == conversation_id
        ))
        assert context is not None
        assert context.active_result_set_id is None
        assert context.active_pending_draft_id is None
        for model in (AgentResultSet, Candidate, Job):
            assert await db.scalar(select(func.count()).select_from(model)) == 0
    assert await _audit(factory, tenant.id, "agent.turn.stale") == [
        {"reason_code": "PRINCIPAL_REVOKED"}
    ]


async def test_issue87_simultaneous_same_submission_executes_once(
    small_pool, tenant_and_user
) -> None:
    _engine, factory, _probe = small_pool
    _, user, password, _ = tenant_and_user
    gate = GatedOllama()
    _install(Settings(ui_cookie_secure=False, inference_concurrency=1), gate)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        csrf = await _login(client, user.username, password)
        conversation_id = await _new_conversation(client, csrf)
        page = await client.get(f"/ui/agent?conversation={conversation_id}")
        token_match = re.search(r'name="submission_id" value="([0-9a-f-]{36})"', page.text)
        assert token_match is not None
        data = {
            "csrf_token": csrf, "conversation_id": str(conversation_id),
            "submission_id": token_match.group(1), "message": "salam",
        }
        first = _spawn(client.post("/ui/agent", data=data, follow_redirects=False))
        await asyncio.wait_for(gate.entered.wait(), timeout=10)
        second = await client.post("/ui/agent", data=data, follow_redirects=False)
        assert second.status_code == 409
        assert gate.calls == 1
        gate.release.set()
        assert (await asyncio.wait_for(first, timeout=10)).status_code == 200
        replay = await client.post("/ui/agent", data=data, follow_redirects=False)
        assert replay.status_code == 303
        assert gate.calls == 1
    assert len((await _stored(factory, conversation_id)).turns) == 2


# ---------------------------------------------------------------------------
# #87 — Phase B principal authority is serialized against security changes
# ---------------------------------------------------------------------------
# Deterministic: independent PostgreSQL sessions, and "blocked" is observed
# as the backend actually waiting on a row lock in pg_stat_activity — never
# inferred from a sleep.


async def _security_change(db, change: str, *, user, membership) -> None:  # noqa: ANN001
    from meyar.services.tenant_membership_repo import set_membership_active
    from meyar.services.user_repo import set_password

    if change == "password":
        await set_password(db, user_id=user.id, plaintext_password="rotated-synthetic-password")
    elif change == "tenant":
        from meyar.services.tenant_authority import set_tenant_active

        await set_tenant_active(db, tenant_id=membership.tenant_id, is_active=False)
    else:
        await set_membership_active(db, membership_id=membership.id, is_active=False)


async def _blocked_on_row_lock(observer, pid: int, task: asyncio.Task) -> bool:  # noqa: ANN001
    """True once backend ``pid`` is waiting on a heavyweight lock; False if
    ``task`` finished first (i.e. it was never serialized)."""
    from sqlalchemy import text

    for _ in range(1000):
        if task.done():
            return False
        async with observer() as db:
            waiting = await db.scalar(
                text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": pid},
            )
        if waiting:
            return True
        await asyncio.sleep(0.01)
    return False


async def _phase_a_reserved(client, tenant_and_user, race_factory):  # noqa: ANN001, ANN202
    """Log in, create a conversation, and commit a real Phase A reservation
    for that browser session."""
    from meyar.agent.turn_boundary import reserve_agent_turn
    from meyar.models.browser_session import BrowserSession
    from meyar.services.agent_conversation_repo import OwnerPrincipal

    tenant, user, password, membership = tenant_and_user
    _install(Settings(ui_cookie_secure=False), GatedOllama())
    csrf = await _login(client, user.username, password)
    conversation_id = await _new_conversation(client, csrf)
    owner = OwnerPrincipal(tenant_id=tenant.id, user_id=user.id, membership_id=membership.id)
    async with race_factory() as db:
        session_id = await db.scalar(
            select(BrowserSession.id).where(BrowserSession.user_id == user.id)
        )
        reserved = await reserve_agent_turn(
            db, owner=owner, conversation_id=conversation_id,
            browser_session_id=session_id, ttl_seconds=60,
        )
        assert reserved is not None
        await db.commit()
    return reserved.reservation


@pytest.fixture
async def race_factory():  # noqa: ANN201
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("change", ["password", "membership", "tenant"])
async def test_issue87_phase_b_holding_authority_blocks_security_change_commit(
    small_pool, tenant_and_user, race_factory, change: str
) -> None:
    """Direction B: Phase B validated the principal first, so the security
    change must WAIT (it cannot commit) until Phase B's transaction ends;
    afterwards the old session is revoked for every later request."""
    from sqlalchemy import text

    from meyar.agent.turn_boundary import clear_turn_reservation, revalidate_reserved_turn
    from meyar.models.browser_session import BrowserSession

    _, user, _, membership = tenant_and_user
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        race_factory() as phase_b, race_factory() as mutator,
    ):
        reservation = await _phase_a_reserved(client, tenant_and_user, race_factory)
        conversation, _context = await revalidate_reserved_turn(
            phase_b, reservation, ttl_seconds=60
        )
        pid = await mutator.scalar(text("SELECT pg_backend_pid()"))

        async def change_and_commit() -> None:
            await _security_change(mutator, change, user=user, membership=membership)
            await mutator.commit()

        security = _spawn(change_and_commit())
        assert await _blocked_on_row_lock(race_factory, pid, security), (
            "security change committed while Phase B held principal authority"
        )
        assert not security.done()
        # Phase B finishes its consequential commit; only then may the
        # security change proceed.
        clear_turn_reservation(conversation)
        await phase_b.commit()
        await asyncio.wait_for(security, timeout=10)
        async with race_factory() as db:
            session = await db.get(BrowserSession, reservation.browser_session_id)
            assert session is not None and session.revoked_at is not None
        stale = await client.get("/ui/agent", follow_redirects=False)
        assert stale.status_code == 303


@pytest.mark.parametrize("change", ["password", "membership", "tenant"])
async def test_issue87_uncommitted_security_change_makes_phase_b_wait_then_fail_closed(
    small_pool, tenant_and_user, race_factory, change: str
) -> None:
    """Direction A, the exact race: the security change has already
    written (and row-locked) User/BrowserSession but not yet committed when
    Phase B starts. Phase B must wait for it and then see the revocation —
    never validate the pre-commit state and commit afterwards."""
    from sqlalchemy import text

    from meyar.agent.turn_boundary import (
        TurnAuthorityLostError,
        TurnStaleReason,
        revalidate_reserved_turn,
    )

    _, user, _, membership = tenant_and_user
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        race_factory() as phase_b, race_factory() as mutator,
    ):
        reservation = await _phase_a_reserved(client, tenant_and_user, race_factory)
        await _security_change(mutator, change, user=user, membership=membership)
        pid = await phase_b.scalar(text("SELECT pg_backend_pid()"))
        revalidation = _spawn(revalidate_reserved_turn(phase_b, reservation, ttl_seconds=60))
        assert await _blocked_on_row_lock(race_factory, pid, revalidation), (
            "Phase B validated authority a pending security change is revoking"
        )
        await mutator.commit()
        with pytest.raises(TurnAuthorityLostError) as lost:
            await asyncio.wait_for(revalidation, timeout=10)
        assert lost.value.reason == TurnStaleReason.PRINCIPAL_REVOKED
        await phase_b.rollback()


async def test_session_context_replaced_while_inferring_fails_closed(
    small_pool, tenant_and_user
) -> None:
    from sqlalchemy import delete

    from meyar.models.agent_conversation import AgentConversationSessionContext

    async def replace(db, *, conversation_id, **_):  # noqa: ANN001, ANN003, ANN202
        context = (
            await db.scalars(
                select(AgentConversationSessionContext).where(
                    AgentConversationSessionContext.conversation_id == conversation_id
                )
            )
        ).one()
        replacement = AgentConversationSessionContext(
            tenant_id=context.tenant_id,
            conversation_id=context.conversation_id,
            browser_session_id=context.browser_session_id,
            context_epoch=context.context_epoch + 1,
        )
        await db.execute(
            delete(AgentConversationSessionContext).where(
                AgentConversationSessionContext.id == context.id
            )
        )
        db.add(replacement)

    factory, tenant, conversation_id, response = await _run_with_mutation_during_inference(
        small_pool, tenant_and_user, replace
    )
    assert response.status_code == 409
    assert STALE_COPY_FRAGMENT in response.text
    _assert_hr_safe(response.text)
    assert (await _stored(factory, conversation_id)).turns == []
    assert await _audit(factory, tenant.id, "agent.turn.stale") == [
        {"reason_code": "CONTEXT_CHANGED"}
    ]


async def test_transcript_changed_while_inferring_fails_closed(
    small_pool, tenant_and_user
) -> None:
    from meyar.services.agent_conversation_repo import save_conversation_turns

    async def write(db, *, conversation_id, **_):  # noqa: ANN001, ANN003, ANN202
        conversation = await db.get(AgentConversation, conversation_id)
        await save_conversation_turns(
            db, conversation, turns=[*conversation.turns, {"role": "user", "text": "x"}]
        )

    factory, tenant, conversation_id, response = await _run_with_mutation_during_inference(
        small_pool, tenant_and_user, write
    )
    assert response.status_code == 409
    stored = await _stored(factory, conversation_id)
    assert [t["text"] for t in stored.turns] == ["x"]  # the other writer's update survives
    assert await _audit(factory, tenant.id, "agent.turn.stale") == [
        {"reason_code": "CONVERSATION_CHANGED"}
    ]


async def test_pending_draft_superseded_while_inferring_cannot_commit(
    small_pool, tenant_and_user
) -> None:
    async def supersede(db, *, conversation_id, **_):  # noqa: ANN001, ANN003, ANN202
        from meyar.models.agent_conversation import AgentConversationSessionContext

        context = (
            await db.scalars(
                select(AgentConversationSessionContext).where(
                    AgentConversationSessionContext.conversation_id == conversation_id
                )
            )
        ).one()
        context.active_pending_draft_id = uuid.uuid4()

    factory, tenant, conversation_id, response = await _run_with_mutation_during_inference(
        small_pool, tenant_and_user, supersede
    )
    assert response.status_code == 409
    assert (await _stored(factory, conversation_id)).turns == []


async def test_active_result_set_superseded_while_inferring_cannot_commit_stale_action(
    small_pool, tenant_and_user, db_session: AsyncSession
) -> None:
    """K: the turn starts against ResultSet R1; while the model decides, the
    session's active pointer moves to R2. The model's candidate_ref must not
    be committed against either set, and R2 stays the live pointer."""
    from search_helpers import open_test_conversation, seed_active_result_set

    from meyar.models.browser_session import BrowserSession

    tenant, user, _password, membership = tenant_and_user
    candidate_1, _ = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content={
            "skills": [], "employment_history": [], "education": [],
            "certifications": [], "languages": [], "projects": [],
        }
    )
    candidate_2, _ = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content={
            "skills": [], "employment_history": [], "education": [],
            "certifications": [], "languages": [], "projects": [],
        }
    )
    await db_session.commit()
    holder: dict[str, uuid.UUID] = {}

    async def setup(factory, tenant, user, membership):  # noqa: ANN001, ANN202
        async with factory() as db:
            session = await db.scalar(
                select(BrowserSession)
                .where(BrowserSession.user_id == user.id)
                .order_by(BrowserSession.created_at.desc())
            )
            conversation, context = await open_test_conversation(
                db,
                tenant_id=tenant.id,
                user_id=user.id,
                membership_id=membership.id,
                browser_session_id=session.id,
            )
            r1 = await seed_active_result_set(
                db, tenant_id=tenant.id, browser_session_id=session.id,
                session_context=context, candidate_ids=[candidate_1.id],
            )
            await db.commit()
            holder.update(session=session.id, r1=r1.id, conversation=conversation.id)
            return conversation.id

    async def supersede(db, *, conversation_id, **_):  # noqa: ANN001, ANN003, ANN202
        from meyar.models.agent_conversation import AgentConversationSessionContext

        context = (
            await db.scalars(
                select(AgentConversationSessionContext).where(
                    AgentConversationSessionContext.conversation_id == conversation_id
                )
            )
        ).one()
        r2 = await seed_active_result_set(
            db, tenant_id=tenant.id, browser_session_id=holder["session"],
            session_context=context, candidate_ids=[candidate_2.id],
        )
        holder["r2"] = r2.id

    gate_decision = FIRST_PROFILE_PLAN
    engine, factory, probe = small_pool
    gate = GatedOllama(decision=gate_decision)
    _install(Settings(ui_cookie_secure=False, inference_concurrency=1), gate)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        csrf = await _login(client, user.username, _password)
        conversation_id = await setup(factory, tenant, user, membership)
        turn = _spawn(_agent_post(client, csrf, conversation_id, "birincinin profilini aç"))
        await asyncio.wait_for(gate.entered.wait(), timeout=10)
        async with factory() as db:
            await supersede(db, conversation_id=conversation_id)
            await db.commit()
        gate.release.set()
        response = await asyncio.wait_for(turn, timeout=10)
    assert response.status_code == 409
    assert str(candidate_1.id) not in response.text
    assert str(candidate_2.id) not in response.text
    assert (await _stored(factory, conversation_id)).turns == []
    assert (await _only_context(factory, conversation_id)).active_result_set_id == holder["r2"]
    assert await _audit(factory, tenant.id, "agent.turn.stale") == [
        {"reason_code": "CONTEXT_CHANGED"}
    ]
    # The stale turn never reached profile dispatch or grounded synthesis.
    assert gate.calls == 1


# ---------------------------------------------------------------------------
# M — readiness vs liveness
# ---------------------------------------------------------------------------


async def test_readiness_reports_persistent_inference_saturation_liveness_stays_ok(
    small_pool, monkeypatch,
) -> None:
    from meyar.api.v1 import health

    async def healthy_components(*args):
        return []

    monkeypatch.setattr(health, "readiness_reasons", healthy_components)
    settings = Settings(
        inference_concurrency=1,
        inference_queue_max_waiters=0,
        inference_saturation_grace_seconds=0.2,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    admission = concurrency.get_inference_admission(
        max_active=1, max_queued=0, queue_timeout_seconds=5
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        ready = await client.get("/api/v1/health/ready")
        assert (ready.status_code, ready.json()) == (200, {"status": "ready"})

        holder = admission.slot()
        await holder.__aenter__()  # every slot busy + zero-length queue full
        try:
            momentary = await client.get("/api/v1/health/ready")
            assert momentary.status_code == 200  # inside the grace window
            await asyncio.sleep(0.25)
            saturated = await client.get("/api/v1/health/ready")
            assert saturated.status_code == 503
            assert saturated.json() == {
                "status": "not_ready",
                "reasons": ["INFERENCE_SATURATED"],
            }
            live = await client.get("/api/v1/health")
            assert (live.status_code, live.json()) == (200, {"status": "ok"})
        finally:
            await holder.__aexit__(None, None, None)
        recovered = await client.get("/api/v1/health/ready")
        assert recovered.status_code == 200


async def test_saturation_probe_reports_db_pool_exhaustion() -> None:
    from meyar.core.saturation import SaturationCode, probe_saturation

    settings = Settings(db_pool_size=1, db_max_overflow=0)
    engine = create_async_engine(TEST_DATABASE_URL, pool_size=1, max_overflow=0)
    pool = engine.sync_engine.pool
    try:
        assert probe_saturation(settings, admission=None, pool=pool).ready
        async with engine.connect() as connection:
            await connection.exec_driver_sql("SELECT 1")
            snapshot = probe_saturation(settings, admission=None, pool=pool)
            assert snapshot.reasons == (SaturationCode.DB_POOL_SATURATED,)
            assert not snapshot.ready
        assert probe_saturation(settings, admission=None, pool=pool).ready
    finally:
        await engine.dispose()


def test_settings_validate_queue_and_reservation_bounds() -> None:
    from pydantic import ValidationError

    for bad in (
        {"inference_concurrency": 0},
        {"inference_queue_max_waiters": -1},
        {"inference_queue_timeout_seconds": 0},
        {"db_pool_size": 0},
        # A reservation must outlive one full inference gap.
        {"agent_turn_reservation_seconds": 60, "llm_timeout_seconds": 120.0},
    ):
        with pytest.raises(ValidationError):
            Settings(**bad)
    defaults = Settings()
    assert (defaults.inference_concurrency, defaults.inference_queue_max_waiters) == (1, 4)
    assert defaults.inference_queue_timeout_seconds == 30.0
    assert (defaults.db_pool_size, defaults.db_max_overflow) == (5, 10)


# ---------------------------------------------------------------------------
# Boundary wrappers: every model / embedding call leaves the DB first
# ---------------------------------------------------------------------------


async def test_boundary_wrappers_leave_db_before_and_revalidate_after_every_call() -> None:
    from fakes import FakeEmbeddingProvider, FakeLLMProvider

    from meyar.agent.turn_boundary import (
        AgentInferenceBusyError,
        BoundaryEmbedding,
        BoundaryLLM,
        TurnBoundary,
    )
    from meyar.llm.provider import InferenceBusyError, ModelTimeoutError

    order: list[str] = []

    class _Recorder(TurnBoundary):
        def __init__(self) -> None:
            self.model_calls = 0

        async def leave_db(self) -> None:
            order.append("leave")

        async def reenter(self):  # noqa: ANN202
            order.append("reenter")

    boundary = _Recorder()
    llm = BoundaryLLM(
        FakeLLMProvider(
            agent_plan=converse("GREETING")
        ),
        boundary,
    )
    await llm.propose_agent_plan(context=_EMPTY_PLAN_CONTEXT)
    embedding = BoundaryEmbedding(FakeEmbeddingProvider(), boundary)
    await embedding.embed("Python")
    assert order == ["leave", "reenter", "leave", "reenter"]

    order.clear()
    failing = BoundaryLLM(FakeLLMProvider(agent_error=ModelTimeoutError("t")), boundary)
    with pytest.raises(ModelTimeoutError):
        await failing.propose_agent_plan(context=_EMPTY_PLAN_CONTEXT)
    # A handled model failure continues only on revalidated authority.
    assert order == ["leave", "reenter"]

    order.clear()
    busy = BoundaryLLM(FakeLLMProvider(agent_error=InferenceBusyError("QUEUE_FULL")), boundary)
    with pytest.raises(AgentInferenceBusyError):
        await busy.propose_agent_plan(context=_EMPTY_PLAN_CONTEXT)
    # BUSY abandons the turn: no re-entry, and it is NOT an LLMProviderError
    # any fallback could turn into a degraded "successful" answer.
    assert order == ["leave"]
    from meyar.llm.provider import LLMProviderError

    assert not issubclass(AgentInferenceBusyError, LLMProviderError)


# ---------------------------------------------------------------------------
# N — no cross-tenant / cross-owner regression through the reservation
# ---------------------------------------------------------------------------


async def test_foreign_owner_cannot_observe_or_touch_an_in_flight_turn(
    small_pool, tenant_and_user, db_session: AsyncSession
) -> None:
    """A foreign tenant's request on a conversation whose turn is in flight
    gets the same indistinguishable 404 as a nonexistent id — never the
    409 "still processing" outcome that would reveal existence — and cannot
    clear or take over the reservation."""
    from conftest import DEFAULT_TEST_PASSWORD

    from meyar.core.roles import ROLE_HR_USER
    from meyar.services.tenant_membership_repo import create_membership
    from meyar.services.tenant_repo import create_tenant
    from meyar.services.user_repo import create_user

    engine, factory, probe = small_pool
    _tenant, user, password, _membership = tenant_and_user
    other_tenant = await create_tenant(db_session, name=f"Other-{uuid.uuid4().hex[:8]}")
    other_user = await create_user(
        db_session, username=f"hr-{uuid.uuid4().hex[:8]}", plaintext_password=DEFAULT_TEST_PASSWORD
    )
    await create_membership(
        db_session, user_id=other_user.id, tenant_id=other_tenant.id, role=ROLE_HR_USER
    )
    await db_session.commit()
    gate = GatedOllama()
    _install(Settings(ui_cookie_secure=False, inference_concurrency=1), gate)
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as owner_client,
        AsyncClient(transport=transport, base_url="http://test") as foreign_client,
    ):
        csrf = await _login(owner_client, user.username, password)
        foreign_csrf = await _login(foreign_client, other_user.username, DEFAULT_TEST_PASSWORD)
        conversation_id = await _new_conversation(owner_client, csrf)
        turn = await _block_first_turn(owner_client, csrf, conversation_id, gate)
        reserved_token = (await _stored(factory, conversation_id)).active_turn_id
        assert reserved_token is not None

        foreign = await asyncio.wait_for(
            _agent_post(foreign_client, foreign_csrf, conversation_id, "salam"), timeout=5
        )
        missing = await asyncio.wait_for(
            _agent_post(foreign_client, foreign_csrf, uuid.uuid4(), "salam"), timeout=5
        )
        assert foreign.status_code == missing.status_code == 404
        assert IN_PROGRESS_COPY_FRAGMENT not in foreign.text
        assert (await _stored(factory, conversation_id)).active_turn_id == reserved_token

        gate.release.set()
        assert (await asyncio.wait_for(turn, timeout=10)).status_code == 200
    stored = await _stored(factory, conversation_id)
    assert [t["text"] for t in stored.turns if t["role"] == "user"] == ["salam"]
    assert stored.active_turn_id is None


# ---------------------------------------------------------------------------
# Blocker A — embedding inference: shared gate + no DB checkout
# ---------------------------------------------------------------------------

EMBED_MODEL = "meyar-test-embed:v1"
HYBRID_REQUEST = "Java is required; backend modernization experience is preferred."
# Issue #88: the planner receives the HR user's OWN message (never
# model-authored text), so the hybrid request is the message itself. It is an
# explicit search (FORCE_CANDIDATE_SEARCH), planned by the fake planner as
# Java + the grounded semantic phrase -> HYBRID -> one query embedding.
HYBRID_REQUEST_MESSAGE = "Find Java candidates with backend modernization experience"


def _install_gated_embedding(settings: Settings, gate: GatedOllama) -> None:
    from meyar.embedding.dependency import get_embedding_provider, get_embedding_search_config
    from meyar.embedding.ollama_provider import OllamaEmbeddingProvider
    from meyar.embedding.serializer import SERIALIZER_VERSION
    from meyar.search.schemas import EmbeddingSearchConfig

    _GATES.append(gate)
    app.dependency_overrides[get_embedding_provider] = lambda: OllamaEmbeddingProvider(
        base_url="http://127.0.0.1:11434",
        model=EMBED_MODEL,
        timeout_seconds=30.0,
        transport=httpx.MockTransport(gate.handler),
        max_concurrency=settings.inference_concurrency,
        max_queued=settings.inference_queue_max_waiters,
        queue_timeout_seconds=settings.inference_queue_timeout_seconds,
    )
    app.dependency_overrides[get_embedding_search_config] = lambda: EmbeddingSearchConfig(
        provider="ollama",
        model_name=EMBED_MODEL,
        model_revision="",
        serializer_version=SERIALIZER_VERSION,
        embedding_dimensions=8,
    )


async def test_hybrid_agent_search_holds_no_db_connection_while_embedding_waits_or_runs(
    small_pool, tenant_and_user
) -> None:
    """Blocker A (5): the agent's semantic/hybrid query embedding goes through
    the SHARED admission gate, and neither while it WAITS for the slot (held
    by another model call) nor while it RUNS does the agent request hold a
    pooled connection."""
    from fakes import FakeLLMProvider

    from meyar.search.planner_schemas import PlannerDraft
    from meyar.search.schemas import RequiredFilters

    engine, factory, probe = small_pool
    _tenant, user, password, _membership = tenant_and_user
    settings = Settings(ui_cookie_secure=False, inference_concurrency=1)
    app.dependency_overrides[get_settings] = lambda: settings
    fake_llm = FakeLLMProvider(
        agent_plan=search_plan(),
        planner_draft=PlannerDraft(
            required_filters=RequiredFilters(skills=["Java"]),
            semantic_query="backend modernization experience",
        ),
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake_llm
    embed_gate = GatedOllama()
    _install_gated_embedding(settings, embed_gate)
    observed: dict[str, int] = {}
    embed_gate.on_enter = lambda: observed.setdefault("running", probe.checked_out)
    admission = concurrency.get_inference_admission(
        max_active=1, max_queued=4, queue_timeout_seconds=30.0
    )
    # Another model call (e.g. an LLM turn elsewhere) holds the only slot.
    holder = admission.slot()
    await holder.__aenter__()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        csrf = await _login(client, user.username, password)
        conversation_id = await _new_conversation(client, csrf)
        turn = _spawn(_agent_post(client, csrf, conversation_id, HYBRID_REQUEST_MESSAGE))
        for _ in range(500):
            if admission.queued == 1:
                break
            await asyncio.sleep(0.01)
        # WAITING: the embedding call is queued behind the held slot.
        assert admission.queued == 1
        assert embed_gate.calls == 0  # it did not bypass capacity
        assert probe.checked_out == 0  # and holds no DB connection
        library, _elapsed = await _timed(client.get("/ui/library"))
        assert library.status_code == 200
        await holder.__aexit__(None, None, None)
        # RUNNING: admitted, blocked inside the Ollama embedding call.
        await asyncio.wait_for(embed_gate.entered.wait(), timeout=10)
        assert observed["running"] == 0
        assert (admission.active, admission.queued) == (1, 0)
        embed_gate.release.set()
        response = await asyncio.wait_for(turn, timeout=10)
    assert response.status_code == 200
    assert embed_gate.calls == 1
    stored = await _stored(factory, conversation_id)
    assert [t["role"] for t in stored.turns] == ["user", "assistant"]
    assert (admission.active, admission.queued) == (0, 0)


async def test_hybrid_agent_search_busy_embedding_gate_gives_busy_outcome(
    small_pool, tenant_and_user
) -> None:
    from fakes import FakeLLMProvider

    from meyar.search.planner_schemas import PlannerDraft
    from meyar.search.schemas import RequiredFilters

    engine, factory, probe = small_pool
    tenant, user, password, _membership = tenant_and_user
    settings = Settings(
        ui_cookie_secure=False, inference_concurrency=1, inference_queue_max_waiters=0
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_llm_provider] = lambda: FakeLLMProvider(
        agent_plan=search_plan(),
        planner_draft=PlannerDraft(
            required_filters=RequiredFilters(skills=["Java"]),
            semantic_query="backend modernization experience",
        ),
    )
    embed_gate = GatedOllama()
    _install_gated_embedding(settings, embed_gate)
    admission = concurrency.get_inference_admission(
        max_active=1, max_queued=0, queue_timeout_seconds=30.0
    )
    holder = admission.slot()
    await holder.__aenter__()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            csrf = await _login(client, user.username, password)
            conversation_id = await _new_conversation(client, csrf)
            busy = await asyncio.wait_for(
                _agent_post(client, csrf, conversation_id, HYBRID_REQUEST_MESSAGE), timeout=10
            )
    finally:
        await holder.__aexit__(None, None, None)
    assert busy.status_code == 503
    assert BUSY_COPY in busy.text
    _assert_hr_safe(busy.text)
    assert embed_gate.calls == 0
    stored = await _stored(factory, conversation_id)
    assert stored.turns == [] and stored.active_turn_id is None
    assert await _audit(factory, tenant.id, "agent.turn.busy") == [{"reason_code": "QUEUE_FULL"}]


async def test_readiness_reflects_shared_gate_saturated_by_embedding_calls(
    small_pool, monkeypatch
) -> None:
    """Readiness observes the ONE shared gate: saturation caused by an
    embedding call reports INFERENCE_SATURATED; liveness stays 200."""
    from meyar.api.v1 import health
    from meyar.embedding.ollama_provider import OllamaEmbeddingProvider

    async def healthy_components(*args):
        return []

    monkeypatch.setattr(health, "readiness_reasons", healthy_components)
    settings = Settings(
        inference_concurrency=1,
        inference_queue_max_waiters=0,
        inference_saturation_grace_seconds=0.0,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    gate = GatedOllama()
    _GATES.append(gate)
    provider = OllamaEmbeddingProvider(
        base_url="http://127.0.0.1:11434",
        model=EMBED_MODEL,
        timeout_seconds=30.0,
        transport=httpx.MockTransport(gate.handler),
        max_concurrency=1,
        max_queued=0,
        queue_timeout_seconds=settings.inference_queue_timeout_seconds,
    )
    running = _spawn(provider.embed("synthetic professional text"))
    await asyncio.wait_for(gate.entered.wait(), timeout=10)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        saturated = await client.get("/api/v1/health/ready")
        assert saturated.status_code == 503
        assert saturated.json() == {"status": "not_ready", "reasons": ["INFERENCE_SATURATED"]}
        assert (await client.get("/api/v1/health")).status_code == 200
        gate.release.set()
        await asyncio.wait_for(running, timeout=10)
        assert (await client.get("/api/v1/health/ready")).status_code == 200


# ---------------------------------------------------------------------------
# Early-commit audit: an orphan ResultSet from a stale turn is never authority
# ---------------------------------------------------------------------------


async def test_orphan_result_set_from_stale_turn_never_becomes_live_authority(
    small_pool, tenant_and_user, db_session: AsyncSession
) -> None:
    """A turn searches (its new AgentResultSet row is committed early, before
    the next model call), then goes stale while the model decides. The
    ResultSet row survives as an inert orphan: the live pointer is NOT set,
    and a later candidate_ref follow-up — from the same session or from
    another BrowserSession — cannot resolve against it."""
    from meyar.models.agent_conversation import AgentConversationSessionContext
    from meyar.models.agent_result_set import AgentResultSet

    engine, factory, probe = small_pool
    tenant, user, password, _membership = tenant_and_user
    candidate, _ = await seed_candidate_with_profile(
        db_session,
        tenant_id=tenant.id,
        profile_content={
            "skills": [
                {
                    "name": "Python",
                    "category": None,
                    "evidence": [{"page": 1, "block_index": 0, "quote": "Synthetic: Python"}],
                }
            ],
            "employment_history": [], "education": [], "certifications": [],
            "languages": [], "projects": [],
        },
    )
    await db_session.commit()
    # Issue #88 slice C: ONE two-step plan (call 1); the planner gets the
    # grounded quote (call 2); the search commits its inert ResultSet, the
    # profile step reads it, and the grounded-synthesis call (call 3) blocks.
    # There is no post-tool "what next" call.
    gate = GatedOllama(
        decisions=[
            plan(
                search_step("Python bilən namizəd tap"), profile_step("birincinin")
            ).model_dump(mode="json"),
            {"required_filters": {"skills": ["Python"]}},
            {"used_facts": []},
        ],
        pass_through=2,
    )
    _install(Settings(ui_cookie_secure=False, inference_concurrency=1), gate)
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as tab_a,
        AsyncClient(transport=transport, base_url="http://test") as tab_b,
    ):
        csrf_a = await _login(tab_a, user.username, password)
        csrf_b = await _login(tab_b, user.username, password)
        conversation_id = await _new_conversation(tab_a, csrf_a)
        turn = _spawn(
            _agent_post(
                tab_a, csrf_a, conversation_id,
                "Python bilən namizəd tap və birincinin profilini aç",
            )
        )
        # The search ran and committed its ResultSet; the synthesis call blocks.
        await asyncio.wait_for(gate.entered.wait(), timeout=10)
        async with factory() as db:
            orphan = (
                await db.scalars(
                    select(AgentResultSet).where(
                        AgentResultSet.conversation_id == conversation_id
                    )
                )
            ).one()
            context = (
                await db.scalars(
                    select(AgentConversationSessionContext).where(
                        AgentConversationSessionContext.conversation_id == conversation_id
                    )
                )
            ).one()
            assert context.active_result_set_id is None  # not live before Phase B
            context.active_pending_draft_id = uuid.uuid4()  # make the turn stale
            await db.commit()
        gate.release.set()
        stale = await asyncio.wait_for(turn, timeout=10)
        assert stale.status_code == 409
        assert str(candidate.id) not in stale.text

        # Follow-ups asking for "the first candidate" from both sessions.
        follow_gate = GatedOllama(decision=FIRST_PROFILE_PLAN)
        follow_gate.release.set()
        _install(Settings(ui_cookie_secure=False, inference_concurrency=1), follow_gate)
        async with factory() as db:
            live = await db.get(AgentConversationSessionContext, context.id)
            live.active_pending_draft_id = None
            await db.commit()
        same = await asyncio.wait_for(
            _agent_post(tab_a, csrf_a, conversation_id, "birincinin profilini aç"), timeout=10
        )
        other = await asyncio.wait_for(
            _agent_post(tab_b, csrf_b, conversation_id, "birincinin profilini aç"), timeout=10
        )
    for response in (same, other):
        assert response.status_code == 200
        assert str(candidate.id) not in response.text
    async with factory() as db:
        contexts = (
            await db.scalars(
                select(AgentConversationSessionContext).where(
                    AgentConversationSessionContext.conversation_id == conversation_id
                )
            )
        ).all()
        assert all(c.active_result_set_id != orphan.id for c in contexts)
        assert await db.get(AgentResultSet, orphan.id) is not None  # inert, not deleted


@pytest.mark.parametrize("supported", [True, False])
async def test_tenant_disabled_while_inferring_has_no_agent_consequences(
    small_pool, tenant_and_user, supported
) -> None:
    from meyar.models.agent_result_set import AgentResultSet
    from meyar.models.agent_task import AgentClarification, AgentTask
    from meyar.services.tenant_authority import set_tenant_active

    async def disable(db, *, membership, **_):
        assert small_pool[2].checked_out == 0
        if supported:
            await set_tenant_active(db, tenant_id=membership.tenant_id, is_active=False)
        else:
            from sqlalchemy import update

            from meyar.models.tenant import Tenant

            await db.execute(update(Tenant).where(
                Tenant.id == membership.tenant_id
            ).values(is_active=False))

    factory, tenant, conversation_id, response = await _run_with_mutation_during_inference(
        small_pool, tenant_and_user, disable
    )
    assert response.status_code == 303 and response.headers["location"] == "/ui/login"
    stored = await _stored(factory, conversation_id)
    assert stored.turns == [] and stored.active_turn_id is None and stored.turn_version == 0
    context = await _only_context(factory, conversation_id)
    assert context.active_result_set_id is None
    assert context.active_pending_draft_id is None
    assert context.active_clarification_id is None
    async with factory() as db:
        for model in (AgentTask, AgentClarification, AgentResultSet):
            assert await db.scalar(select(model).where(model.tenant_id == tenant.id)) is None
