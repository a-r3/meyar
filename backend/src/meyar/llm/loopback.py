from urllib.parse import urlparse

import httpx

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def require_loopback_url(base_url: str, *, setting_name: str) -> None:
    """Shared local-only guard for every Ollama-backed provider (LLM
    extraction, embeddings, ...). Candidate document content must never
    leave this machine — base_url is required to be a loopback address;
    anything else is rejected at construction time. See
    docs/MASTER_SPEC.md §16 and Slice 4 spec §3."""
    if urlparse(base_url).hostname not in _LOOPBACK_HOSTS:
        raise ValueError(
            f"{setting_name} ({base_url}) must be a loopback address "
            "(127.0.0.1/localhost) — candidate content must never leave this "
            "machine. See docs/MASTER_SPEC.md §16."
        )


def build_local_only_async_client(
    *,
    timeout: httpx.Timeout | float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Single shared HTTPX client construction boundary for every local-only
    Ollama request (chat/inference, embeddings, health/readiness).

    A loopback-validated base_url (require_loopback_url above) is not
    enough on its own: httpx honors HTTP_PROXY/HTTPS_PROXY/ALL_PROXY from
    the process environment by default and will route a request through a
    proxy transport even though the logical request URL is 127.0.0.1 —
    this is only prevented if NO_PROXY happens to also be set correctly,
    which this boundary must not depend on. trust_env=False disables all
    environment-derived proxy selection, so no candidate-derived request
    can be silently re-routed off-machine by process environment
    configuration. follow_redirects stays explicit False (httpx's own
    default) so a redirect response can never carry a request outside this
    boundary either."""
    return httpx.AsyncClient(
        timeout=timeout,
        transport=transport,
        trust_env=False,
        follow_redirects=False,
    )
