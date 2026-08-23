from typing import Protocol

from meyar.extraction.view import ProfessionalDocumentView
from meyar.schemas.candidate_identity import CandidateIdentityExtraction
from meyar.schemas.candidate_profile import CandidateProfileExtraction


class LLMProviderError(Exception):
    code = "MODEL_UNAVAILABLE"


class ModelUnavailableError(LLMProviderError):
    code = "MODEL_UNAVAILABLE"


class ModelTimeoutError(LLMProviderError):
    code = "MODEL_TIMEOUT"


class ModelSchemaInvalidError(LLMProviderError):
    code = "MODEL_SCHEMA_INVALID"


class LLMProvider(Protocol):
    """The only boundary application/domain code may depend on for local
    inference — never a concrete provider's raw HTTP response. See
    docs/MASTER_SPEC.md §3 and docs/DECISIONS.md D-001."""

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

    async def health(self) -> dict:
        """Best-effort reachability/model-availability check. Never
        exposed directly to external customers — internal use only."""
        ...
