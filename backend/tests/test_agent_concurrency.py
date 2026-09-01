"""Slice 2 (issue #31) — Ollama concurrency guard. Multiple browser
users/tabs must never be able to launch more simultaneous local-model
calls than MEYAR_INFERENCE_CONCURRENCY, across every OllamaLLMProvider
call site (extraction, planning, and the new agent decision loop alike) —
see meyar.llm.concurrency."""

import asyncio
import json

import httpx
import pytest

from meyar.llm.concurrency import get_inference_semaphore, reset_inference_semaphore
from meyar.llm.ollama_provider import OllamaLLMProvider


@pytest.fixture(autouse=True)
def _isolated_semaphore():
    reset_inference_semaphore()
    yield
    reset_inference_semaphore()


async def test_semaphore_caps_concurrent_holders_at_configured_size() -> None:
    semaphore = get_inference_semaphore(2)
    active = 0
    max_active = 0
    lock = asyncio.Lock()

    async def hold() -> None:
        nonlocal active, max_active
        async with semaphore:
            async with lock:
                active += 1
                max_active = max(max_active, active)
            await asyncio.sleep(0.05)
            async with lock:
                active -= 1

    await asyncio.gather(*(hold() for _ in range(6)))
    assert max_active == 2


async def test_semaphore_singleton_is_shared_across_calls_not_per_instance() -> None:
    """A fresh OllamaLLMProvider instance is constructed per request (see
    meyar.llm.dependency.get_llm_provider) — the guard must not reset to a
    fresh budget on every new instance, or the setting would do nothing
    against concurrent requests."""
    first = get_inference_semaphore(1)
    second = get_inference_semaphore(1)
    assert first is second


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
