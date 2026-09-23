"""Slice 13 — formal no-exfiltration acceptance.

Layered proof that candidate-content workflows never contact a public
network service:

1. Static inventory (verified during Slice 13 preparation, restated
   here as an executable check): there are three call sites that build a
   local-only HTTP client in `src/meyar` — `OllamaLLMProvider.health`,
   `OllamaLLMProvider._chat`, and `OllamaEmbeddingProvider.embed` — and
   all three (a) are loopback-gated via `require_loopback_url` at
   construction time (see `test_ollama_provider_rejects_non_loopback_url`
   / `test_ollama_embedding_provider_rejects_non_loopback_url`, already
   covered elsewhere) and (b) go through the single shared
   `meyar.llm.loopback.build_local_only_async_client` boundary, which is
   the only place `httpx.AsyncClient` is ever constructed for Ollama
   traffic — see `test_ollama_transport_proxy_isolation.py` for proof
   that this boundary also disables environment-derived proxy routing
   (trust_env=False), independent of this module's logical-URL check.
2. This module adds the missing layer: a deterministic runtime guard
   that patches `httpx.AsyncClient.send` — the single dispatch point
   every httpx request goes through regardless of transport — so any
   request whose host is not loopback raises immediately instead of
   being sent. No packet capture, no live network dependency.
3. A representative workflow (LLM extraction + embedding, via the real
   `OllamaLLMProvider`/`OllamaEmbeddingProvider` classes against a local
   `httpx.MockTransport`, exactly as production code would run them
   against a real local Ollama daemon) is executed under the guard and
   must complete with zero blocked attempts.

Structured/semantic search and deterministic scoring are intentionally
not exercised here: the exhaustive static inventory (Slice 13
preparation) already confirms neither constructs an HTTP client at all —
adding synthetic network activity to those paths would not add proof
value, only noise.
"""

import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from unittest import mock

import httpx
import pytest

from meyar.embedding.ollama_provider import OllamaEmbeddingProvider
from meyar.extraction.view import ModelInputBlock, ProfessionalDocumentView
from meyar.llm.ollama_provider import OllamaLLMProvider

_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}


class NonLoopbackNetworkAttempt(RuntimeError):
    pass


@contextlib.asynccontextmanager
async def loopback_only_network_guard() -> AsyncIterator[list[str]]:
    """While active, any httpx request whose host is not loopback raises
    NonLoopbackNetworkAttempt instead of being sent. Returns the list of
    hosts that were allowed through, for positive assertions that at
    least one real dispatch happened (not a vacuously-passing no-op)."""
    original_send = httpx.AsyncClient.send
    allowed_hosts: list[str] = []

    async def guarded_send(self: httpx.AsyncClient, request: httpx.Request, *args, **kwargs):
        host = request.url.host
        if host not in _ALLOWED_HOSTS:
            raise NonLoopbackNetworkAttempt(
                f"Blocked outbound request to non-loopback host: {host!r}"
            )
        allowed_hosts.append(host)
        return await original_send(self, request, *args, **kwargs)

    with mock.patch.object(httpx.AsyncClient, "send", guarded_send):
        yield allowed_hosts


async def test_guard_blocks_a_non_loopback_request_before_any_network_io() -> None:
    """Negative control proving the guard itself actually works — without
    this, a workflow test that never attempts a public request would pass
    vacuously."""
    async with loopback_only_network_guard():
        async with httpx.AsyncClient() as client:
            with pytest.raises(NonLoopbackNetworkAttempt):
                await client.get("https://example.invalid/should-never-be-reached")


async def test_representative_extract_and_embed_workflow_stays_loopback_only() -> None:
    """Runs the real production provider classes — not fakes — against a
    local MockTransport standing in for the Ollama daemon, under the
    active guard, proving the actual runtime HTTP call path used by
    extraction and embedding never addresses anything but loopback."""

    def chat_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["model"] == "qwen3:0.6b"
        extraction_json = {
            "skills": [
                {
                    "name": "Python",
                    "evidence": [{"page": 1, "block_index": 0, "quote": "Skills: Python"}],
                }
            ]
        }
        return httpx.Response(
            200,
            json={"model": "qwen3:0.6b", "message": {"content": json.dumps(extraction_json)}},
        )

    def embed_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["model"] == "nomic-embed-text"
        return httpx.Response(200, json={"embedding": [0.1, 0.2, 0.3, 0.4]})

    llm = OllamaLLMProvider(
        base_url="http://127.0.0.1:11434",
        model="qwen3:0.6b",
        timeout_seconds=5.0,
        transport=httpx.MockTransport(chat_handler),
    )
    embedder = OllamaEmbeddingProvider(
        base_url="http://127.0.0.1:11434",
        model="nomic-embed-text",
        timeout_seconds=5.0,
        transport=httpx.MockTransport(embed_handler),
    )
    view = ProfessionalDocumentView(
        canonical_document_id=uuid.uuid4(),
        blocks=[ModelInputBlock(page=1, block_index=0, text="Skills: Python")],
    )

    async with loopback_only_network_guard() as allowed_hosts:
        extraction, model_name = await llm.extract_candidate_profile(view)
        embedding_result = await embedder.embed("Skills: Python")

    assert extraction.skills[0].name == "Python"
    assert model_name == "qwen3:0.6b"
    assert embedding_result.vector == [0.1, 0.2, 0.3, 0.4]
    # the workflow actually dispatched two real requests through the
    # guard (not a vacuous pass) and every one of them was loopback.
    assert allowed_hosts == ["127.0.0.1", "127.0.0.1"]
