import math

import httpx

from meyar.embedding.provider import (
    EmbeddingInvalidOutputError,
    EmbeddingResult,
    EmbeddingTimeoutError,
    EmbeddingUnavailableError,
)
from meyar.llm.loopback import require_loopback_url

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
    ) -> None:
        require_loopback_url(base_url, setting_name="MEYAR_OLLAMA_BASE_URL")
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        # Injectable only for deterministic offline unit tests of this
        # provider's own HTTP/validation behavior (httpx.MockTransport) —
        # never used in production, where it stays None (real transport).
        self._transport = transport
        self.model_name = model
        # Ollama's embeddings API does not report a model digest/revision.
        self.model_revision = ""

    async def embed(self, text: str) -> EmbeddingResult:
        payload = {"model": self.model_name, "prompt": text}
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds, transport=self._transport
            ) as client:
                resp = await client.post(f"{self._base_url}/api/embeddings", json=payload)
        except httpx.TimeoutException as exc:
            raise EmbeddingTimeoutError(
                f"Ollama embedding request timed out after {self._timeout_seconds}s."
            ) from exc
        except httpx.HTTPError as exc:
            raise EmbeddingUnavailableError(f"Ollama unreachable: {exc}") from exc

        if resp.status_code != 200:
            raise EmbeddingUnavailableError(f"Ollama returned HTTP {resp.status_code}.")

        raw_vector = resp.json().get("embedding")
        if not isinstance(raw_vector, list) or len(raw_vector) == 0:
            raise EmbeddingInvalidOutputError("Empty or missing embedding vector.")
        if not all(isinstance(v, int | float) and math.isfinite(v) for v in raw_vector):
            raise EmbeddingInvalidOutputError(
                "Embedding vector contains non-numeric or non-finite values."
            )

        vector = [float(v) for v in raw_vector]
        return EmbeddingResult(
            vector=vector,
            dimensions=len(vector),
            provider=self.provider_name,
            model_name=self.model_name,
            model_revision=self.model_revision,
        )
