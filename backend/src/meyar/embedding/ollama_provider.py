import math

import httpx

from meyar.embedding.provider import (
    EmbeddingBusyError,
    EmbeddingInvalidOutputError,
    EmbeddingResult,
    EmbeddingTimeoutError,
    EmbeddingUnavailableError,
)
from meyar.llm.concurrency import (
    DEFAULT_INFERENCE_QUEUE_MAX_WAITERS,
    DEFAULT_INFERENCE_QUEUE_TIMEOUT_SECONDS,
    InferenceAdmissionError,
    get_inference_admission,
)
from meyar.llm.loopback import build_local_only_async_client, require_loopback_url

# Labeled explicitly as a development/integration default — NOT an
# approved final production embedding model. Final selection is blocked
# on the target Mac Mini benchmark and multilingual quality validation
# (see docs/DECISIONS.md, docs/MVP_PLAN.md M5/Slice 13).
DEV_INTEGRATION_MODEL = "nomic-embed-text"


class OllamaEmbeddingProvider:
    """Local-only Ollama embedding provider. Candidate professional text
    never leaves this machine — base_url must be a loopback address,
    rejected at construction time otherwise (same guard as
    OllamaLLMProvider, see meyar.llm.loopback)."""

    provider_name = "ollama"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
        max_concurrency: int = 1,
        max_queued: int = DEFAULT_INFERENCE_QUEUE_MAX_WAITERS,
        queue_timeout_seconds: float = DEFAULT_INFERENCE_QUEUE_TIMEOUT_SECONDS,
    ) -> None:
        require_loopback_url(base_url, setting_name="MEYAR_OLLAMA_BASE_URL")
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        # Issue #85: the SAME process-wide admission policy as
        # OllamaLLMProvider — one Ollama daemon, one capacity budget.
        self._max_concurrency = max_concurrency
        self._max_queued = max_queued
        self._queue_timeout_seconds = queue_timeout_seconds
        # Injectable only for deterministic offline unit tests of this
        # provider's own HTTP/validation behavior (httpx.MockTransport) —
        # never used in production, where it stays None (real transport).
        self._transport = transport
        self.model_name = model
        # Ollama's embeddings API does not report a model digest/revision.
        self.model_revision = ""

    async def embed(self, text: str) -> EmbeddingResult:
        payload = {"model": self.model_name, "prompt": text}
        admission = get_inference_admission(
            max_active=self._max_concurrency,
            max_queued=self._max_queued,
            queue_timeout_seconds=self._queue_timeout_seconds,
        )
        try:
            async with admission.slot():
                async with build_local_only_async_client(
                    timeout=self._timeout_seconds, transport=self._transport
                ) as client:
                    resp = await client.post(f"{self._base_url}/api/embeddings", json=payload)
        except InferenceAdmissionError as exc:
            raise EmbeddingBusyError(exc.reason.value) from None
        except httpx.TimeoutException:
            raise EmbeddingTimeoutError(
                f"Ollama embedding request timed out after {self._timeout_seconds}s."
            ) from None
        except httpx.HTTPError:
            raise EmbeddingUnavailableError("Ollama is unavailable.") from None

        if resp.status_code != 200:
            raise EmbeddingUnavailableError(f"Ollama returned HTTP {resp.status_code}.")

        try:
            raw_vector = resp.json().get("embedding")
        except (ValueError, AttributeError):
            raise EmbeddingInvalidOutputError("Ollama embedding response is invalid.") from None
        if not isinstance(raw_vector, list) or len(raw_vector) == 0:
            raise EmbeddingInvalidOutputError("Empty or missing embedding vector.")
        if not all(isinstance(v, int | float) and math.isfinite(v) for v in raw_vector):
            raise EmbeddingInvalidOutputError(
                "Embedding vector contains non-numeric or non-finite values."
            )
        if all(v == 0 for v in raw_vector):
            # A zero-norm vector makes cosine similarity/distance undefined
            # (Slice 8 pgvector search uses cosine_distance) and can never
            # be a genuine embedding of non-empty text — reject at the
            # provider boundary so no zero vector is ever persisted.
            raise EmbeddingInvalidOutputError("Embedding vector is zero-norm (all-zero).")

        vector = [float(v) for v in raw_vector]
        return EmbeddingResult(
            vector=vector,
            dimensions=len(vector),
            provider=self.provider_name,
            model_name=self.model_name,
            model_revision=self.model_revision,
        )

    async def health(self) -> dict:
        """Mirrors OllamaLLMProvider.health() — same Ollama daemon, same
        `/api/tags` reachability/model-availability check, same 5s bound."""
        try:
            async with build_local_only_async_client(
                timeout=min(5.0, self._timeout_seconds), transport=self._transport
            ) as client:
                resp = await client.get(f"{self._base_url}/api/tags")
                resp.raise_for_status()
                tags = [m.get("name") for m in resp.json().get("models", [])]
                return {
                    "reachable": True,
                    "model": self.model_name,
                    "model_available": self.model_name in tags,
                }
        except httpx.HTTPError:
            return {"reachable": False, "model": self.model_name, "model_available": False}
