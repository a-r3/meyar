"""Slice 2 (issue #31) — Ollama concurrency guard. Multiple browser
users/tabs must never be able to launch more simultaneous local-model
calls than MEYAR_INFERENCE_CONCURRENCY, across every OllamaLLMProvider
call site (extraction, planning, and the new agent decision loop alike) —
see meyar.llm.concurrency."""

import asyncio
import json

import httpx
import pytest

from meyar.llm.concurrency import (
    InferenceAdmission,
    InferenceQueueFullError,
    InferenceQueueTimeoutError,
    InferenceRejectionReason,
    get_inference_admission,
    reset_inference_admission,
)
from meyar.llm.ollama_provider import OllamaLLMProvider
from meyar.llm.provider import InferenceBusyError, ModelUnavailableError


@pytest.fixture(autouse=True)
def _isolated_admission():
    reset_inference_admission()
    yield
    reset_inference_admission()


def _gate(max_active: int = 1, max_queued: int = 4, timeout: float = 5.0) -> InferenceAdmission:
    return InferenceAdmission(
        max_active=max_active, max_queued=max_queued, queue_timeout_seconds=timeout
    )


async def test_admission_caps_concurrent_holders_at_configured_size() -> None:
    gate = _gate(max_active=2, max_queued=10)
    active = 0
    max_active = 0

    async def hold() -> None:
        nonlocal active, max_active
        async with gate.slot():
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.05)
            active -= 1

    await asyncio.gather(*(hold() for _ in range(6)))
    assert max_active == 2
    assert (gate.active, gate.queued) == (0, 0)


async def test_admission_singleton_is_shared_across_calls_not_per_instance() -> None:
    """A fresh OllamaLLMProvider instance is constructed per request (see
    meyar.llm.dependency.get_llm_provider) — the guard must not reset to a
    fresh budget on every new instance, or the setting would do nothing
    against concurrent requests."""
    first = get_inference_admission(max_active=1, max_queued=2, queue_timeout_seconds=1)
    second = get_inference_admission(max_active=1, max_queued=2, queue_timeout_seconds=1)
    assert first is second


async def test_queue_full_rejects_immediately_without_entering() -> None:
    """Test matrix C (unit): one active + a full queue -> the next caller
    is rejected at once, deterministically, and never runs."""
    gate = _gate(max_active=1, max_queued=1)
    release = asyncio.Event()
    entered: list[str] = []

    async def hold(name: str) -> None:
        async with gate.slot():
            entered.append(name)
            await release.wait()

    first = asyncio.create_task(hold("first"))
    await asyncio.sleep(0)
    second = asyncio.create_task(hold("second"))
    await asyncio.sleep(0)
    assert (gate.active, gate.queued, gate.saturated) == (1, 1, True)
    with pytest.raises(InferenceQueueFullError) as info:
        async with gate.slot():
            entered.append("third")
    assert info.value.reason == InferenceRejectionReason.QUEUE_FULL
    release.set()
    await asyncio.gather(first, second)
    assert entered == ["first", "second"]  # FIFO; the rejected one never ran
    assert (gate.active, gate.queued, gate.saturated) == (0, 0, False)


async def test_queue_timeout_rejects_and_frees_the_queue_slot() -> None:
    """Test matrix D (unit): a waiter not admitted in time is rejected and
    leaves the queue; capacity is intact afterwards."""
    gate = _gate(max_active=1, max_queued=1, timeout=0.05)
    release = asyncio.Event()

    async def hold() -> None:
        async with gate.slot():
            await release.wait()

    holder = asyncio.create_task(hold())
    await asyncio.sleep(0)
    with pytest.raises(InferenceQueueTimeoutError) as info:
        async with gate.slot():
            pytest.fail("must not be admitted")
    assert info.value.reason == InferenceRejectionReason.QUEUE_TIMEOUT
    assert (gate.active, gate.queued) == (1, 0)
    release.set()
    await holder
    assert (gate.active, gate.queued) == (0, 0)
    async with gate.slot():
        assert gate.active == 1


async def test_cancellation_while_queued_releases_queue_capacity() -> None:
    """Test matrix E: a cancelled waiter (client gone) leaves the queue at
    once and is never admitted later."""
    gate = _gate(max_active=1, max_queued=1)
    release = asyncio.Event()
    ran: list[str] = []

    async def hold(name: str) -> None:
        async with gate.slot():
            ran.append(name)
            await release.wait()

    holder = asyncio.create_task(hold("holder"))
    await asyncio.sleep(0)
    waiter = asyncio.create_task(hold("cancelled"))
    await asyncio.sleep(0)
    assert gate.queued == 1
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert gate.queued == 0
    # The queue slot is reusable immediately.
    late = asyncio.create_task(hold("late"))
    await asyncio.sleep(0)
    assert gate.queued == 1
    release.set()
    await asyncio.gather(holder, late)
    assert ran == ["holder", "late"]
    assert (gate.active, gate.queued) == (0, 0)


async def test_cancellation_while_active_releases_inference_capacity() -> None:
    """Test matrix F: cancelling a call that holds the slot (model HTTP in
    flight) gives the slot back — no permanent capacity leak."""
    gate = _gate(max_active=1, max_queued=0)

    async def hold_forever() -> None:
        async with gate.slot():
            await asyncio.sleep(3600)

    task = asyncio.create_task(hold_forever())
    await asyncio.sleep(0)
    assert gate.active == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gate.active == 0
    async with gate.slot():
        assert gate.active == 1


async def test_slot_handed_to_a_waiter_that_is_cancelled_is_returned() -> None:
    """Race: the holder hands its slot to the first waiter in the same tick
    the waiter is cancelled. The slot must go back, never leak."""
    gate = _gate(max_active=1, max_queued=2)

    async def hold() -> None:
        async with gate.slot():
            await asyncio.sleep(3600)

    holder = gate.slot()
    await holder.__aenter__()
    waiter = asyncio.create_task(hold())
    await asyncio.sleep(0)
    assert gate.queued == 1
    # Release (synchronously hands the slot to `waiter`) and cancel the
    # waiter in the same event-loop tick, before it can run.
    await holder.__aexit__(None, None, None)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert (gate.active, gate.queued) == (0, 0)


async def test_admission_rejects_invalid_policy() -> None:
    with pytest.raises(ValueError):
        InferenceAdmission(max_active=0, max_queued=1, queue_timeout_seconds=1)
    with pytest.raises(ValueError):
        InferenceAdmission(max_active=1, max_queued=-1, queue_timeout_seconds=1)
    with pytest.raises(ValueError):
        InferenceAdmission(max_active=1, max_queued=1, queue_timeout_seconds=0)


async def test_provider_maps_rejection_to_typed_busy_error_without_http() -> None:
    """A rejected call never reaches Ollama and surfaces as the typed,
    transient InferenceBusyError (INFERENCE_BUSY)."""
    release = asyncio.Event()
    http_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        await release.wait()
        return httpx.Response(
            200, json={"model": "qwen3:0.6b", "message": {"content": "{}"}}
        )

    def provider() -> OllamaLLMProvider:
        return OllamaLLMProvider(
            base_url="http://127.0.0.1:11434",
            model="qwen3:0.6b",
            timeout_seconds=2,
            transport=httpx.MockTransport(handler),
            max_concurrency=1,
            max_queued=0,
            queue_timeout_seconds=1,
        )

    running = asyncio.create_task(provider().plan_candidate_search("Java candidates"))
    while http_calls == 0:
        await asyncio.sleep(0)
    with pytest.raises(InferenceBusyError) as info:
        await provider().plan_candidate_search("Java candidates")
    # Issue #85 correction: a transient admission refusal, never a
    # "model unavailable" failure that callers could persist as FAILED.
    assert not isinstance(info.value, ModelUnavailableError)
    assert info.value.code == "INFERENCE_BUSY"
    assert info.value.reason == "QUEUE_FULL"
    assert http_calls == 1
    release.set()
    await asyncio.gather(running, return_exceptions=True)


async def test_ollama_provider_calls_serialize_under_concurrency_one() -> None:
    active = 0
    max_active = 0
    lock = asyncio.Lock()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, max_active
        async with lock:
            active += 1
            max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        async with lock:
            active -= 1
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={"model": payload["model"], "message": {"content": json.dumps({"age": 1})}},
        )

    provider_a = OllamaLLMProvider(
        base_url="http://127.0.0.1:11434",
        model="qwen3:0.6b",
        timeout_seconds=2,
        transport=httpx.MockTransport(handler),
        max_concurrency=1,
    )
    provider_b = OllamaLLMProvider(
        base_url="http://127.0.0.1:11434",
        model="qwen3:0.6b",
        timeout_seconds=2,
        transport=httpx.MockTransport(handler),
        max_concurrency=1,
    )

    async def call(provider: OllamaLLMProvider) -> None:
        try:
            await provider.plan_candidate_search("Java candidates")
        except Exception:  # noqa: BLE001 - schema-invalid is expected; concurrency is what's under test
            pass

    await asyncio.gather(call(provider_a), call(provider_b))
    assert max_active == 1


async def test_ollama_provider_calls_run_concurrently_when_budget_allows() -> None:
    active = 0
    max_active = 0
    lock = asyncio.Lock()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, max_active
        async with lock:
            active += 1
            max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        async with lock:
            active -= 1
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={"model": payload["model"], "message": {"content": json.dumps({"age": 1})}},
        )

    provider = OllamaLLMProvider(
        base_url="http://127.0.0.1:11434",
        model="qwen3:0.6b",
        timeout_seconds=2,
        transport=httpx.MockTransport(handler),
        max_concurrency=2,
    )

    async def call() -> None:
        try:
            await provider.plan_candidate_search("Java candidates")
        except Exception:  # noqa: BLE001
            pass

    await asyncio.gather(call(), call())
    assert max_active == 2


# ---------------------------------------------------------------------------
# Issue #85 correction (Blocker A): ONE shared gate for LLM + embedding calls
# ---------------------------------------------------------------------------


class _GatedTransport:
    """Blocks every request until ``release`` is set; records calls/endpoints."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.entered = asyncio.Event()
        self.paths: list[str] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        self.entered.set()
        await self.release.wait()
        if request.url.path.endswith("/api/embeddings"):
            return httpx.Response(200, json={"embedding": [0.1, 0.2, 0.3]})
        return httpx.Response(
            200, json={"model": "qwen3:0.6b", "message": {"content": json.dumps({"x": 1})}}
        )


def _llm(transport: _GatedTransport, *, max_queued: int = 4, timeout: float = 5.0):  # noqa: ANN202
    return OllamaLLMProvider(
        base_url="http://127.0.0.1:11434",
        model="qwen3:0.6b",
        timeout_seconds=5,
        transport=httpx.MockTransport(transport.handler),
        max_concurrency=1,
        max_queued=max_queued,
        queue_timeout_seconds=timeout,
    )


def _embedder(transport: _GatedTransport, *, max_queued: int = 4, timeout: float = 5.0):  # noqa: ANN202
    from meyar.embedding.ollama_provider import OllamaEmbeddingProvider

    return OllamaEmbeddingProvider(
        base_url="http://127.0.0.1:11434",
        model="nomic-embed-text",
        timeout_seconds=5,
        transport=httpx.MockTransport(transport.handler),
        max_concurrency=1,
        max_queued=max_queued,
        queue_timeout_seconds=timeout,
    )


async def _until(predicate) -> None:  # noqa: ANN001
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached")


async def test_active_llm_call_prevents_embedding_from_bypassing_capacity() -> None:
    transport = _GatedTransport()
    llm_call = asyncio.create_task(_llm(transport).plan_candidate_search("Java"))
    await asyncio.wait_for(transport.entered.wait(), timeout=5)
    embed_call = asyncio.create_task(_embedder(transport).embed("synthetic text"))
    admission = get_inference_admission(max_active=1, max_queued=4, queue_timeout_seconds=5.0)
    await _until(lambda: admission.queued == 1)
    assert transport.paths == ["/api/chat"]  # the embedding did NOT start
    transport.release.set()
    await asyncio.gather(llm_call, embed_call, return_exceptions=True)
    assert transport.paths == ["/api/chat", "/api/embeddings"]  # strictly after
    assert (admission.active, admission.queued) == (0, 0)


async def test_active_embedding_call_prevents_llm_from_bypassing_capacity() -> None:
    transport = _GatedTransport()
    embed_call = asyncio.create_task(_embedder(transport).embed("synthetic text"))
    await asyncio.wait_for(transport.entered.wait(), timeout=5)
    llm_call = asyncio.create_task(_llm(transport).plan_candidate_search("Java"))
    admission = get_inference_admission(max_active=1, max_queued=4, queue_timeout_seconds=5.0)
    await _until(lambda: admission.queued == 1)
    assert transport.paths == ["/api/embeddings"]
    transport.release.set()
    await asyncio.gather(llm_call, embed_call, return_exceptions=True)
    assert transport.paths == ["/api/embeddings", "/api/chat"]
    assert (admission.active, admission.queued) == (0, 0)


async def test_mixed_queue_full_rejects_both_provider_kinds_with_typed_busy() -> None:
    from meyar.embedding.provider import EmbeddingBusyError, EmbeddingUnavailableError

    transport = _GatedTransport()
    llm_call = asyncio.create_task(_llm(transport, max_queued=1).plan_candidate_search("Java"))
    await asyncio.wait_for(transport.entered.wait(), timeout=5)
    queued_embed = asyncio.create_task(_embedder(transport, max_queued=1).embed("queued"))
    admission = get_inference_admission(max_active=1, max_queued=1, queue_timeout_seconds=5.0)
    await _until(lambda: admission.queued == 1)
    assert admission.saturated
    with pytest.raises(EmbeddingBusyError) as embed_busy:
        await _embedder(transport, max_queued=1).embed("rejected")
    assert embed_busy.value.code == "INFERENCE_BUSY" and embed_busy.value.reason == "QUEUE_FULL"
    assert not isinstance(embed_busy.value, EmbeddingUnavailableError)
    with pytest.raises(InferenceBusyError) as llm_busy:
        await _llm(transport, max_queued=1).plan_candidate_search("rejected")
    assert llm_busy.value.code == "INFERENCE_BUSY" and llm_busy.value.reason == "QUEUE_FULL"
    assert not isinstance(llm_busy.value, ModelUnavailableError)
    transport.release.set()
    await asyncio.gather(llm_call, queued_embed, return_exceptions=True)
    assert len(transport.paths) == 2  # rejected calls never reached Ollama
    assert (admission.active, admission.queued) == (0, 0)


async def test_mixed_queue_timeout_accounting() -> None:
    from meyar.embedding.provider import EmbeddingBusyError

    transport = _GatedTransport()
    embed_call = asyncio.create_task(
        _embedder(transport, max_queued=2, timeout=0.05).embed("holder")
    )
    await asyncio.wait_for(transport.entered.wait(), timeout=5)
    with pytest.raises(InferenceBusyError) as llm_timeout:
        await _llm(transport, max_queued=2, timeout=0.05).plan_candidate_search("Java")
    assert llm_timeout.value.reason == "QUEUE_TIMEOUT"
    with pytest.raises(EmbeddingBusyError) as embed_timeout:
        await _embedder(transport, max_queued=2, timeout=0.05).embed("waiter")
    assert embed_timeout.value.reason == "QUEUE_TIMEOUT"
    admission = get_inference_admission(max_active=1, max_queued=2, queue_timeout_seconds=0.05)
    assert (admission.active, admission.queued) == (1, 0)
    transport.release.set()
    await embed_call
    assert (admission.active, admission.queued) == (0, 0)
    assert transport.paths == ["/api/embeddings"]


async def test_cancelled_embedding_waiter_and_holder_release_capacity() -> None:
    transport = _GatedTransport()
    holder = asyncio.create_task(_embedder(transport).embed("holder"))
    await asyncio.wait_for(transport.entered.wait(), timeout=5)
    waiter = asyncio.create_task(_embedder(transport).embed("waiter"))
    admission = get_inference_admission(max_active=1, max_queued=4, queue_timeout_seconds=5.0)
    await _until(lambda: admission.queued == 1)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert (admission.active, admission.queued) == (1, 0)
    holder.cancel()  # cancel an embedding call while its Ollama HTTP is in flight
    with pytest.raises(asyncio.CancelledError):
        await holder
    assert (admission.active, admission.queued) == (0, 0)
    assert transport.paths == ["/api/embeddings"]  # the cancelled waiter never ran


async def test_llm_and_embedding_policies_can_never_silently_diverge() -> None:
    from meyar.config import Settings
    from meyar.embedding.dependency import embedding_provider_from_settings
    from meyar.llm.concurrency import InferenceAdmissionPolicyMismatchError
    from meyar.llm.dependency import llm_provider_from_settings

    transport = _GatedTransport()
    transport.release.set()
    settings = Settings(inference_concurrency=2, inference_queue_max_waiters=3)
    llm = llm_provider_from_settings(settings)
    embedder = embedding_provider_from_settings(settings)
    # Production factories build both providers from the same Settings.
    assert (llm._max_concurrency, llm._max_queued, llm._queue_timeout_seconds) == (
        embedder._max_concurrency,
        embedder._max_queued,
        embedder._queue_timeout_seconds,
    )
    await _embedder(transport, max_queued=4).embed("first policy wins nothing silently")
    with pytest.raises(InferenceAdmissionPolicyMismatchError):
        await _llm(transport, max_queued=5).plan_candidate_search("Java")
