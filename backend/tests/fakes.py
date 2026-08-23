"""Deterministic test doubles. Automated tests must never depend on a
real running LLM or embedding model — see Slice 4 spec §21."""

from meyar.embedding.provider import EmbeddingProviderError, EmbeddingResult
from meyar.extraction.view import ProfessionalDocumentView
from meyar.llm.provider import LLMProviderError, LLMResultProvenance
from meyar.schemas.candidate_identity import CandidateIdentityExtraction
from meyar.schemas.candidate_profile import CandidateProfileExtraction
from meyar.search.planner_schemas import PlannerDraft


class FakeLLMProvider:
    provider_name = "fake"

    def __init__(
        self,
        *,
        extraction: CandidateProfileExtraction | None = None,
        identity_extraction: CandidateIdentityExtraction | None = None,
        error: LLMProviderError | None = None,
        model_name: str = "fake-model-v1",
        fail_first_n_calls: int = 0,
        planner_draft: PlannerDraft | None = None,
        planner_drafts: list[PlannerDraft] | None = None,
        model_revision: str = "",
        planner_provenance: LLMResultProvenance | None = None,
    ) -> None:
        self._extraction = extraction
        self._identity_extraction = identity_extraction
        self._error = error
        self.model_name = model_name
        self.model_revision = model_revision
        self._fail_first_n_calls = fail_first_n_calls
        self._planner_drafts = planner_drafts or ([planner_draft] if planner_draft else [])
        self._planner_provenance = planner_provenance
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

    async def plan_candidate_search(
        self, natural_language_request: str, *, repair: bool = False
    ) -> tuple[PlannerDraft, LLMResultProvenance]:
        self.call_count += 1
        if self.call_count <= self._fail_first_n_calls:
            from meyar.llm.provider import ModelSchemaInvalidError

            raise ModelSchemaInvalidError("Simulated schema-invalid planner output.")
        if self._error is not None:
            raise self._error
        success_index = self.call_count - self._fail_first_n_calls - 1
        assert self._planner_drafts
        draft = self._planner_drafts[min(success_index, len(self._planner_drafts) - 1)]
        provenance = self._planner_provenance or LLMResultProvenance(
            provider=self.provider_name,
            model_name=self.model_name,
            model_revision=self.model_revision,
        )
        return draft, provenance


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
