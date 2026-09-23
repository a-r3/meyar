from typing import Protocol

from pydantic import BaseModel


class EmbeddingProviderError(Exception):
    code = "EMBEDDING_UNAVAILABLE"


class EmbeddingUnavailableError(EmbeddingProviderError):
    code = "EMBEDDING_UNAVAILABLE"


class EmbeddingTimeoutError(EmbeddingProviderError):
    code = "EMBEDDING_TIMEOUT"


class EmbeddingInvalidOutputError(EmbeddingProviderError):
    code = "EMBEDDING_INVALID_OUTPUT"


class EmbeddingResult(BaseModel):
    vector: list[float]
    dimensions: int
    provider: str
    model_name: str
    model_revision: str = ""


class EmbeddingProvider(Protocol):
    """The only boundary application/domain code may depend on for local
    embedding generation — never a concrete provider's raw HTTP response,
    same boundary pattern as LLMProvider. See docs/MASTER_SPEC.md §15 and
    .claude/rules/security-privacy.md (local embedding provider only,
    never an external embedding API). provider_name/model_name/
    model_revision are readable before calling embed() so a caller can
    check for an existing CandidateEmbeddingVersion (idempotency) without
    paying for an inference call first."""

    provider_name: str
    model_name: str
    model_revision: str

    async def embed(self, text: str) -> EmbeddingResult:
        """Returns a validated embedding for text. Raises
        EmbeddingUnavailableError / EmbeddingTimeoutError /
        EmbeddingInvalidOutputError on failure — never returns an empty,
        NaN/Inf-containing, or otherwise invalid vector."""
        ...

    async def health(self) -> dict:
        """Best-effort reachability/model-availability check, mirroring
        meyar.llm.provider.LLMProvider.health(). Used by meyar-ops
        readiness/status (issue #35) — never exposed as a public/external
        endpoint."""
        ...
