from urllib.parse import urlparse

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
