from typing import Protocol

from pydantic import BaseModel, Field

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

    async def health(self) -> dict:
        """Best-effort reachability/model-availability check. Never
        exposed directly to external customers — internal use only."""
        ...
