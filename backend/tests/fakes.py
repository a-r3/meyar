"""Deterministic test doubles. Automated tests must never depend on a
real running LLM or embedding model — see Slice 4 spec §21."""

from typing import Any

from meyar.agent.clarification_schemas import (
    ClarificationAnswerProposal,
    ClarificationProposalValue,
)
from meyar.agent.schemas import (
    AgentDecision,
    GroundedFact,
    GroundedSelection,
    JDCriteriaDraft,
    RequirementSpan,
)
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
        agent_decision: AgentDecision | None = None,
        agent_decisions: list[AgentDecision] | None = None,
        agent_error: LLMProviderError | None = None,
        agent_fail_first_n_calls: int = 0,
        agent_fail_after_n_calls: int | None = None,
        grounded_selection: GroundedSelection | None = None,
        grounded_error: LLMProviderError | None = None,
        grounded_fail_first_n_calls: int = 0,
        jd_draft: JDCriteriaDraft | None = None,
        jd_draft_error: LLMProviderError | None = None,
        jd_draft_fail_first_n_calls: int = 0,
        health_result: dict | None = None,
        clarification_proposals: list[ClarificationProposalValue] | None = None,
        clarification_error: LLMProviderError | None = None,
        clarification_fail_first_n_calls: int = 0,
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
        self._agent_decisions = agent_decisions or ([agent_decision] if agent_decision else [])
        self._agent_error = agent_error
        self._agent_fail_first_n_calls = agent_fail_first_n_calls
        self._agent_fail_after_n_calls = agent_fail_after_n_calls
        self.agent_call_count = 0
        self.agent_contexts: list[tuple[bool, list[int]]] = []
        self._grounded_selection = grounded_selection
        self._grounded_error = grounded_error
        self._grounded_fail_first_n_calls = grounded_fail_first_n_calls
        self.grounded_call_count = 0
        self.last_grounded_question: str | None = None
        self.last_grounded_facts: list[GroundedFact] | None = None
        self._jd_draft = jd_draft
        self._jd_draft_error = jd_draft_error
        self._jd_draft_fail_first_n_calls = jd_draft_fail_first_n_calls
        self.jd_draft_call_count = 0
        self.last_jd_text: str | None = None
        self.last_requirement_spans: list[RequirementSpan] | None = None
        self._health_result = health_result
        self._clarification_proposals = clarification_proposals or []
        self._clarification_error = clarification_error
        self._clarification_fail_first_n_calls = clarification_fail_first_n_calls
        # (type, allowed answers, reply text, repair) per call: the ENTIRE
        # model input of the clarification classifier.
        self.clarification_calls: list[tuple[str, list[str], str, bool]] = []

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
        if self._health_result is not None:
            return self._health_result
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

    async def decide_agent_action(
        self,
        *,
        recent_turns: list[tuple[str, str]],
        last_tool_result_summary: dict[str, Any] | None,
        active_result_context_present: bool,
        available_candidate_refs: list[int],
        repair: bool = False,
    ) -> tuple[AgentDecision, LLMResultProvenance]:
        self.agent_call_count += 1
        self.agent_contexts.append(
            (active_result_context_present, list(available_candidate_refs))
        )
        if self.agent_call_count <= self._agent_fail_first_n_calls:
            from meyar.llm.provider import ModelSchemaInvalidError

            raise ModelSchemaInvalidError("Simulated schema-invalid agent output.")
        if (
            self._agent_fail_after_n_calls is not None
            and self.agent_call_count > self._agent_fail_after_n_calls
        ):
            from meyar.llm.provider import ModelSchemaInvalidError

            raise self._agent_error or ModelSchemaInvalidError(
                "Simulated schema-invalid agent output (post-success failure)."
            )
        if self._agent_error is not None and self._agent_fail_after_n_calls is None:
            raise self._agent_error
        success_index = self.agent_call_count - self._agent_fail_first_n_calls - 1
        assert self._agent_decisions
        decision = self._agent_decisions[min(success_index, len(self._agent_decisions) - 1)]
        provenance = self._planner_provenance or LLMResultProvenance(
            provider=self.provider_name,
            model_name=self.model_name,
            model_revision=self.model_revision,
        )
        return decision, provenance

    async def select_grounded_facts(
        self,
        *,
        question: str,
        facts: list[GroundedFact],
        repair: bool = False,
    ) -> tuple[GroundedSelection, LLMResultProvenance]:
        self.grounded_call_count += 1
        self.last_grounded_question = question
        self.last_grounded_facts = facts
        if self.grounded_call_count <= self._grounded_fail_first_n_calls:
            from meyar.llm.provider import ModelSchemaInvalidError

            raise ModelSchemaInvalidError("Simulated schema-invalid grounded-selection output.")
        if self._grounded_error is not None:
            raise self._grounded_error
        if self._grounded_selection is None:
            # Grounded synthesis is best-effort/optional (D-038): a test
            # that never configured it is exercising unrelated behavior
            # and should see exactly the same deterministic-fallback
            # result as if synthesis were simply unavailable — never an
            # unhandled assertion error.
            from meyar.llm.provider import ModelUnavailableError

            raise ModelUnavailableError("FakeLLMProvider: grounded_selection not configured.")
        provenance = self._planner_provenance or LLMResultProvenance(
            provider=self.provider_name,
            model_name=self.model_name,
            model_revision=self.model_revision,
        )
        return self._grounded_selection, provenance

    async def draft_job_criteria(
        self,
        jd_text: str,
        *,
        requirement_spans: list[RequirementSpan],
        span_hints: dict[str, dict[str, str]] | None = None,
        repair: bool = False,
    ) -> tuple[JDCriteriaDraft, LLMResultProvenance]:
        self.jd_draft_call_count += 1
        self.last_jd_text = jd_text
        self.last_requirement_spans = requirement_spans
        self.last_span_hints = span_hints
        if self.jd_draft_call_count <= self._jd_draft_fail_first_n_calls:
            from meyar.llm.provider import ModelSchemaInvalidError

            raise ModelSchemaInvalidError("Simulated schema-invalid JD-draft output.")
        if self._jd_draft_error is not None:
            raise self._jd_draft_error
        assert self._jd_draft is not None
        provenance = self._planner_provenance or LLMResultProvenance(
            provider=self.provider_name,
            model_name=self.model_name,
            model_revision=self.model_revision,
        )
        return self._jd_draft, provenance


    async def resolve_clarification_answer(
        self,
        *,
        clarification_type: str,
        allowed_answers: list[str],
        answer_text: str,
        repair: bool = False,
    ) -> tuple[ClarificationAnswerProposal, LLMResultProvenance]:
        self.clarification_calls.append(
            (clarification_type, list(allowed_answers), answer_text, repair)
        )
        if len(self.clarification_calls) <= self._clarification_fail_first_n_calls:
            from meyar.llm.provider import ModelSchemaInvalidError

            raise ModelSchemaInvalidError("Simulated schema-invalid clarification output.")
        if self._clarification_error is not None:
            raise self._clarification_error
        if not self._clarification_proposals:
            from meyar.llm.provider import ModelUnavailableError

            raise ModelUnavailableError("FakeLLMProvider: clarification not configured.")
        index = len(self.clarification_calls) - self._clarification_fail_first_n_calls - 1
        value = self._clarification_proposals[min(index, len(self._clarification_proposals) - 1)]
        provenance = LLMResultProvenance(
            provider=self.provider_name,
            model_name=self.model_name,
            model_revision=self.model_revision,
        )
        return ClarificationAnswerProposal(value=value), provenance


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
        health_result: dict | None = None,
    ) -> None:
        self._vector = vector if vector is not None else [0.1 * i for i in range(dimensions)]
        self._error = error
        self.model_name = model_name
        self.model_revision = model_revision
        self.call_count = 0
        self._health_result = health_result

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

    async def health(self) -> dict:
        if self._health_result is not None:
            return self._health_result
        return {"reachable": True, "model": self.model_name, "model_available": True}
