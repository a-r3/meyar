"""Process-wide Ollama inference concurrency guard (Slice 2, issue #31).

A fresh ``OllamaLLMProvider`` instance is constructed per request (see
meyar.llm.dependency.get_llm_provider), so the bound must live outside any
one instance — otherwise every browser tab/session would get its own
independent concurrency budget and the setting would do nothing. A single
module-level semaphore, sized once from ``Settings.inference_concurrency``,
is shared by every call through ``OllamaLLMProvider._chat`` (extraction,
identity extraction, NL search planning, and the Slice 2 agent decision
loop alike) so no combination of concurrent HR users/tabs can launch more
simultaneous local-model calls than the configured hardware budget."""

import asyncio

_semaphore: asyncio.Semaphore | None = None
_semaphore_size: int | None = None


def get_inference_semaphore(max_concurrency: int) -> asyncio.Semaphore:
    """Lazily creates the shared semaphore at the first configured size and
    reuses it thereafter. Resizing an in-flight asyncio.Semaphore is not
    safe, so a changed ``max_concurrency`` value only takes effect after
    ``reset_inference_semaphore`` (tests only) or a process restart."""
    global _semaphore, _semaphore_size
    if max_concurrency < 1:
        raise ValueError("inference_concurrency must be >= 1.")
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(max_concurrency)
        _semaphore_size = max_concurrency
    return _semaphore


def reset_inference_semaphore() -> None:
    """Test-only: clears the process-wide singleton so a test can exercise
    a different configured concurrency value or an isolated semaphore."""
    global _semaphore, _semaphore_size
    _semaphore = None
    _semaphore_size = None
