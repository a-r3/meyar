"""Deterministic test doubles. Automated tests must never depend on a
real running LLM or embedding model — see Slice 4 spec §21."""

from meyar.embedding.provider import EmbeddingProviderError, EmbeddingResult
from meyar.extraction.view import ProfessionalDocumentView
from meyar.llm.provider import LLMProviderError
from meyar.schemas.candidate_identity import CandidateIdentityExtraction
from meyar.schemas.candidate_profile import CandidateProfileExtraction


class FakeLLMProvider:
    def __init__(
        self,
        *,
        extraction: CandidateProfileExtraction | None = None,
        identity_extraction: CandidateIdentityExtraction | None = None,
        error: LLMProviderError | None = None,
        model_name: str = "fake-model-v1",
        fail_first_n_calls: int = 0,
    ) -> None:
        self._extraction = extraction
        self._identity_extraction = identity_extraction
        self._error = error
        self.model_name = model_name
        self._fail_first_n_calls = fail_first_n_calls
        self.call_count = 0

    async def extract_candidate_profile(
        self, view: ProfessionalDocumentView
    ) -> tuple[CandidateProfileExtraction, str]:
        self.call_count += 1
        if self.call_count <= self._fail_first_n_calls:
            from meyar.llm.provider import ModelSchemaInvalidError

            raise ModelSchemaInvalidError("Simulated transient schema-invalid output.")
        if self._error is not None:
            raise self._error
        assert self._extraction is not None
        return self._extraction, self.model_name

    async def extract_candidate_identity(
        self, view: ProfessionalDocumentView
    ) -> tuple[CandidateIdentityExtraction, str]:
        self.call_count += 1
        if self.call_count <= self._fail_first_n_calls:
            from meyar.llm.provider import ModelSchemaInvalidError

            raise ModelSchemaInvalidError("Simulated transient schema-invalid output.")
        if self._error is not None:
            raise self._error
        assert self._identity_extraction is not None
        return self._identity_extraction, self.model_name

    async def health(self) -> dict:
        return {"reachable": True, "model": self.model_name, "model_available": True}


class FakeEmbeddingProvider:
    provider_name = "fake-embedding"

    def __init__(
        self,
        *,
        vector: list[float] | None = None,
        dimensions: int = 8,
        model_name: str = "fake-embedding-model-v1",
        model_revision: str = "",
        error: EmbeddingProviderError | None = None,
    ) -> None:
        self._vector = vector if vector is not None else [0.1 * i for i in range(dimensions)]
        self._error = error
        self.model_name = model_name
        self.model_revision = model_revision
        self.call_count = 0

    async def embed(self, text: str) -> EmbeddingResult:
        self.call_count += 1
        if self._error is not None:
            raise self._error
        return EmbeddingResult(
            vector=self._vector,
            dimensions=len(self._vector),
            provider=self.provider_name,
            model_name=self.model_name,
            model_revision=self.model_revision,
        )
