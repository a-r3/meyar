"""Deterministic test doubles. Automated tests must never depend on a
real running LLM — see Slice 4 spec §21."""

from meyar.extraction.view import ProfessionalDocumentView
from meyar.llm.provider import LLMProviderError
from meyar.schemas.candidate_profile import CandidateProfileExtraction


class FakeLLMProvider:
    def __init__(
        self,
        *,
        extraction: CandidateProfileExtraction | None = None,
        error: LLMProviderError | None = None,
        model_name: str = "fake-model-v1",
        fail_first_n_calls: int = 0,
    ) -> None:
        self._extraction = extraction
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

    async def health(self) -> dict:
        return {"reachable": True, "model": self.model_name, "model_available": True}
