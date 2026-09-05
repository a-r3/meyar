"""Regression tests for the local Ollama transport-egress P0.

Defect: a loopback-validated Ollama base_url (`require_loopback_url`) is a
*logical URL* check only. httpx independently honors HTTP_PROXY /
HTTPS_PROXY / ALL_PROXY from the process environment and, unless NO_PROXY
is also set correctly, will route a request through an attacker-controlled
proxy transport even though the request's logical URL is 127.0.0.1. A
MockTransport-based test that only asserts `request.url.host == "127.0.0.1"`
cannot detect this — the defect lives one layer below, in which transport
httpx selects to actually dispatch the request (see
`httpx.Client._get_proxy_map` / `Client.__init__`'s `_mounts` table).

Fix: every local-only HTTP client is now built through the single shared
`meyar.llm.loopback.build_local_only_async_client` boundary, which passes
`trust_env=False` (and explicit `follow_redirects=False`) to
`httpx.AsyncClient`. `trust_env=False` disables `_get_proxy_map`'s
environment lookup entirely, so `_mounts` stays empty regardless of
HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY. These tests assert on that
internal `_mounts`/`_trust_env` state directly — the actual transport
selection layer — not just the logical request URL.
"""

import contextlib
from collections.abc import Iterator

import httpx
import pytest

from meyar.embedding.ollama_provider import OllamaEmbeddingProvider
from meyar.llm.loopback import build_local_only_async_client
from meyar.llm.ollama_provider import OllamaLLMProvider
from meyar.llm.provider import ModelUnavailableError

_PROXY_ENV_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)

_ATTACKER_PROXY = "http://attacker.example:8080"


@contextlib.contextmanager
def _clean_proxy_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Removes every proxy-related env var, including NO_PROXY, so each
    test starts from a known-empty baseline and sets only what it means to
    test — in particular, NO_PROXY is absent by default (requirement: the
    fix must not depend on NO_PROXY being set)."""
    for name in _PROXY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    yield


@contextlib.contextmanager
def _captured_async_clients() -> Iterator[list[httpx.AsyncClient]]:
    """Records every real httpx.AsyncClient constructed while active, by
    wrapping the actual __init__ — so tests can inspect the genuine
    internal proxy-mount table built at construction time, not a
    reimplementation of it."""
    captured: list[httpx.AsyncClient] = []
    original_init = httpx.AsyncClient.__init__

    def spy_init(self: httpx.AsyncClient, *args: object, **kwargs: object) -> None:
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]
        captured.append(self)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(httpx.AsyncClient, "__init__", spy_init)
        yield captured


# ---------------------------------------------------------------------------
# Layer 1: the shared construction boundary itself, in isolation.
# ---------------------------------------------------------------------------


def test_httpx_default_construction_is_vulnerable_to_env_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control: proves the scenario described in the audit is
    real for a naively-constructed httpx.AsyncClient (trust_env defaults
    to True), so the assertions below are meaningful and not vacuous."""
    with _clean_proxy_env(monkeypatch):
        monkeypatch.setenv("HTTP_PROXY", _ATTACKER_PROXY)
        vulnerable_client = httpx.AsyncClient(timeout=5.0)
        assert vulnerable_client._trust_env is True
        assert vulnerable_client._mounts != {}


@pytest.mark.parametrize("proxy_var", ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"])
def test_local_only_client_ignores_proxy_env_var(
    monkeypatch: pytest.MonkeyPatch, proxy_var: str
) -> None:
    """Proves proxy selection is disabled at the httpx transport/client
    configuration level (_mounts), not merely that the logical URL is
    loopback — with NO_PROXY absent, exactly the audit scenario."""
    with _clean_proxy_env(monkeypatch):
        monkeypatch.setenv(proxy_var, _ATTACKER_PROXY)
        client = build_local_only_async_client(timeout=5.0)
        assert client._trust_env is False
        assert client._mounts == {}


def test_local_only_client_ignores_proxy_env_even_with_empty_no_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _clean_proxy_env(monkeypatch):
        monkeypatch.setenv("HTTP_PROXY", _ATTACKER_PROXY)
        monkeypatch.setenv("NO_PROXY", "")
        client = build_local_only_async_client(timeout=5.0)
        assert client._trust_env is False
        assert client._mounts == {}


async def test_local_only_client_does_not_follow_redirects() -> None:
    """A redirect response must never be chased outside the boundary —
    the client surfaces the 3xx response as-is instead of dispatching a
    second request to its Location."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(302, headers={"Location": "http://attacker.example/steal"})

    async with build_local_only_async_client(
        timeout=5.0, transport=httpx.MockTransport(handler)
    ) as client:
        resp = await client.get("http://127.0.0.1:11434/api/tags")

    assert resp.status_code == 302
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Layer 2: each real Ollama call path, end to end, still using the
# boundary and still functioning normally under attacker-controlled proxy
# env vars.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("proxy_var", ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"])
async def test_chat_path_transport_ignores_proxy_env(
    monkeypatch: pytest.MonkeyPatch, proxy_var: str
) -> None:
    with _clean_proxy_env(monkeypatch):
        monkeypatch.setenv(proxy_var, _ATTACKER_PROXY)
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(
                200, json={"model": "qwen3:0.6b", "message": {"content": "{}"}}
            )

        provider = OllamaLLMProvider(
            base_url="http://127.0.0.1:11434",
            model="qwen3:0.6b",
            timeout_seconds=5.0,
            transport=httpx.MockTransport(handler),
        )

        with _captured_async_clients() as captured:
            content, provenance = await provider._chat(
                system_prompt="sys", user_prompt="usr", schema={"type": "object"}
            )

        assert content == "{}"
        assert provenance.model_name == "qwen3:0.6b"
        assert len(calls) == 1
        assert calls[0].url.host == "127.0.0.1"
        assert len(captured) == 1
        assert captured[0]._trust_env is False
        assert captured[0]._mounts == {}


@pytest.mark.parametrize("proxy_var", ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"])
async def test_embed_path_transport_ignores_proxy_env(
    monkeypatch: pytest.MonkeyPatch, proxy_var: str
) -> None:
    with _clean_proxy_env(monkeypatch):
        monkeypatch.setenv(proxy_var, _ATTACKER_PROXY)
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json={"embedding": [0.1, 0.2, 0.3]})

        provider = OllamaEmbeddingProvider(
            base_url="http://127.0.0.1:11434",
            model="nomic-embed-text",
            timeout_seconds=5.0,
            transport=httpx.MockTransport(handler),
        )

        with _captured_async_clients() as captured:
            result = await provider.embed("Skills: Python")

        assert result.vector == [0.1, 0.2, 0.3]
        assert len(calls) == 1
        assert calls[0].url.host == "127.0.0.1"
        assert len(captured) == 1
        assert captured[0]._trust_env is False
        assert captured[0]._mounts == {}


@pytest.mark.parametrize("proxy_var", ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"])
async def test_health_path_transport_ignores_proxy_env(
    monkeypatch: pytest.MonkeyPatch, proxy_var: str
) -> None:
    """Health/readiness is a separately constructed client from chat/embed
    (OllamaLLMProvider.health) — verified independently, not assumed to
    inherit the fix from _chat."""
    with _clean_proxy_env(monkeypatch):
        monkeypatch.setenv(proxy_var, _ATTACKER_PROXY)
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json={"models": [{"name": "qwen3:0.6b"}]})

        provider = OllamaLLMProvider(
            base_url="http://127.0.0.1:11434",
            model="qwen3:0.6b",
            timeout_seconds=5.0,
            transport=httpx.MockTransport(handler),
        )

        with _captured_async_clients() as captured:
            result = await provider.health()

        assert result == {"reachable": True, "model": "qwen3:0.6b", "model_available": True}
        assert len(calls) == 1
        assert calls[0].url.host == "127.0.0.1"
        assert len(captured) == 1
        assert captured[0]._trust_env is False
        assert captured[0]._mounts == {}


async def test_chat_path_does_not_follow_redirect_outside_boundary() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(302, headers={"Location": "http://attacker.example/steal"})

    provider = OllamaLLMProvider(
        base_url="http://127.0.0.1:11434",
        model="qwen3:0.6b",
        timeout_seconds=5.0,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ModelUnavailableError):
        await provider._chat(system_prompt="sys", user_prompt="usr", schema={"type": "object"})

    # Exactly one dispatch: the 302 was surfaced as a failure, never
    # chased to the external Location.
    assert len(calls) == 1
    assert calls[0].url.host == "127.0.0.1"


# ---------------------------------------------------------------------------
# Layer 3: the loopback URL guard keeps failing closed and is unaffected
# by (nor weakened by) the transport-level fix.
# ---------------------------------------------------------------------------


def test_non_loopback_llm_endpoint_still_fails_closed_under_proxy_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _clean_proxy_env(monkeypatch):
        monkeypatch.setenv("HTTP_PROXY", _ATTACKER_PROXY)
        with pytest.raises(ValueError, match="loopback"):
            OllamaLLMProvider(
                base_url="http://example.com:11434", model="qwen3:0.6b", timeout_seconds=5.0
            )


def test_non_loopback_embedding_endpoint_still_fails_closed_under_proxy_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _clean_proxy_env(monkeypatch):
        monkeypatch.setenv("HTTP_PROXY", _ATTACKER_PROXY)
        with pytest.raises(ValueError, match="loopback"):
            OllamaEmbeddingProvider(
                base_url="http://example.com:11434",
                model="nomic-embed-text",
                timeout_seconds=5.0,
            )
