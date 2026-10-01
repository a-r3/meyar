from typing import Any, Protocol

from pydantic import BaseModel, Field

from meyar.agent.clarification_schemas import ClarificationAnswerProposal
from meyar.agent.schemas import (
    AgentDecision,
    GroundedFact,
    GroundedSelection,
    JDCriteriaDraft,
    RequirementSpan,
)
from meyar.extraction.view import ProfessionalDocumentView
from meyar.schemas.candidate_identity import CandidateIdentityExtraction
from meyar.schemas.candidate_profile import CandidateProfileExtraction
from meyar.search.planner_schemas import PlannerDraft


class LLMProviderError(Exception):
    code = "MODEL_UNAVAILABLE"


class ModelUnavailableError(LLMProviderError):
    code = "MODEL_UNAVAILABLE"


class ModelTimeoutError(LLMProviderError):
    code = "MODEL_TIMEOUT"


class ModelSchemaInvalidError(LLMProviderError):
    code = "MODEL_SCHEMA_INVALID"


class InferenceBusyError(LLMProviderError):
    """Issue #85: the process-wide local-inference admission gate rejected
    the call (queue full, or not admitted within the bounded queue wait).

    A TRANSIENT admission outcome: NO model attempt was made, Ollama is not
    necessarily unavailable, and nothing about the input failed. It is
    therefore deliberately NOT a ``ModelUnavailableError``/
    ``ModelTimeoutError``: callers that persist immutable failure versions
    must not treat it as a model failure (they defer instead — see
    meyar.extraction.service.ExtractionDeferredError). Carries only the
    closed reason code (``QUEUE_FULL``/``QUEUE_TIMEOUT``)."""

    code = "INFERENCE_BUSY"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Local inference busy: {reason}.")


class LLMResultProvenance(BaseModel):
    """Provider-reported metadata for one structured local inference call."""

    model_config = {"extra": "forbid"}

    provider: str = Field(min_length=1, max_length=32)
    model_name: str = Field(min_length=1, max_length=128)
    model_revision: str = Field(default="", max_length=128)


class LLMProvider(Protocol):
    """The only boundary application/domain code may depend on for local
    inference — never a concrete provider's raw HTTP response. See
    docs/MASTER_SPEC.md §3 and docs/DECISIONS.md D-001."""

    provider_name: str
    model_name: str
    model_revision: str

    async def extract_candidate_profile(
        self, view: ProfessionalDocumentView
    ) -> tuple[CandidateProfileExtraction, str]:
        """Returns (validated extraction, model_name actually used).
        Raises ModelUnavailableError / ModelTimeoutError /
        ModelSchemaInvalidError on failure — never returns a partially
        valid result."""
        ...

    async def extract_candidate_identity(
        self, view: ProfessionalDocumentView
    ) -> tuple[CandidateIdentityExtraction, str]:
        """Same contract as extract_candidate_profile, for the separate
        identity-only schema. view must come from
        meyar.extraction.view.build_identity_document_view (unredacted),
        never the redacted professional view."""
        ...

    async def plan_candidate_search(
        self, natural_language_request: str, *, repair: bool = False
    ) -> tuple[PlannerDraft, LLMResultProvenance]:
        """Return a strict Slice 9 draft and actual call provenance.

        ``repair`` selects the single bounded repair prompt. Raw invalid
        output and validation detail never cross this provider boundary.
        """
        ...

    async def decide_agent_action(
        self,
        *,
        recent_turns: list[tuple[str, str]],
        last_tool_result_summary: dict[str, Any] | None,
        active_result_context_present: bool,
        available_candidate_refs: list[int],
        repair: bool = False,
    ) -> tuple[AgentDecision, "LLMResultProvenance"]:
        """One bounded orchestration step for Slice 2's read-only agent
        (meyar.agent.service). Returns a strict AgentDecision and actual
        call provenance — never raw model output. ``repair`` selects the
        single bounded repair prompt, mirroring plan_candidate_search."""
        ...

    async def select_grounded_facts(
        self,
        *,
        question: str,
        facts: list[GroundedFact],
        repair: bool = False,
    ) -> tuple[GroundedSelection, "LLMResultProvenance"]:
        """One bounded, narrow selection call for Slice 2's D-038 grounded
        profile/evidence explanation (meyar.agent.service). ``facts`` is
        the exact, already-fetched, already-tenant-scoped fact list the
        model may select from — never raw CV text, never identity. The
        model authors no sentence text at all: the caller independently
        re-validates the returned GroundedSelection's fact ids against
        ``facts`` and builds the actual displayed sentence itself
        (meyar.agent.service.render_grounded_answer) — this method only
        guarantees schema shape, never factual content."""
        ...

    async def draft_job_criteria(
        self,
        jd_text: str,
        *,
        requirement_spans: list[RequirementSpan],
        span_hints: dict[str, dict[str, str]] | None = None,
        repair: bool = False,
    ) -> tuple[JDCriteriaDraft, "LLMResultProvenance"]:
        """Slice 4 (issue #33, D-030/D-032): one bounded drafting call that
        turns a JD/role description's own text into a strict
        JDCriteriaDraft. ``jd_text`` is the HR user's own already-known
        message text. ``requirement_spans`` is segmented and identified by
        the server before inference; every item must reference one of those
        occurrence ids. The model drafts; it is never scoring/persistence
        authority — see meyar.agent.service._dispatch_draft_job_criteria,
        which re-validates every drafted item before it is ever shown.

        Issue #84: each item is a canonical-requirement PROPOSAL for one
        server span (``requirement`` = canonical professional subject). The
        optional ``span_hints`` carry only safe deterministic structure
        (modality/family/duration/level) — never permissions, weights or
        policy. meyar.agent.canonical_requirements validates every proposal
        against the exact span text."""
        ...

    async def resolve_clarification_answer(
        self,
        *,
        clarification_type: str,
        allowed_answers: list[str],
        answer_text: str,
        repair: bool = False,
    ) -> tuple[ClarificationAnswerProposal, "LLMResultProvenance"]:
        """Issue #88 slice A (D-092 §6.4 step 4): classify one typed reply to
        an open server-typed clarification into exactly one closed code
        (an allowed answer, NEW_REQUEST or UNCLEAR). Receives ONLY the reply
        text, the clarification type and the allowed codes — never the
        source text, transcript, ids or identity. Raises the usual
        LLMProviderError subclasses; the caller treats any failure (after
        one repair) as classifier unavailability, never as UNCLEAR."""
        ...

    async def health(self) -> dict:
        """Best-effort reachability/model-availability check. Never
        exposed directly to external customers — internal use only."""
        ...
