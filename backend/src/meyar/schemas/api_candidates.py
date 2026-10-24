"""Client-neutral candidate DTOs for the machine and human APIs (issue #45).

Extends the shared read models (``meyar.schemas.read_models``) with a truthful
readiness contract and exact profile provenance. No storage path/key, raw CV
text, prompt, vector or model output appears here. Identity fields are display
data only: nothing in this module feeds search, scoring or ranking."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from meyar.schemas.read_models import CandidateDetailView, CandidateLibraryItemView


class ReadinessState(StrEnum):
    READY = "READY"
    PROCESSING = "PROCESSING"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"


class RetryStage(StrEnum):
    """The only processing stages MEYAR can re-run on demand. Parsing happens
    synchronously at upload (a failed parse needs a new upload), and folder
    reconciliation is an operator command, so neither is offered here."""

    PROFILE_EXTRACTION = "PROFILE_EXTRACTION"
    EMBEDDING = "EMBEDDING"


class CandidateReadinessOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: ReadinessState
    latest_parser_status: str | None = None
    # Latest extraction ATTEMPT status; "UNAVAILABLE" when it is COMPLETED but
    # its facts are no longer evidence-authorized (never silently "ready").
    profile_status: str | None = None
    current_profile_available: bool = False
    # Present on detail only (it needs per-candidate lookups); None = not computed.
    retryable_stages: list[RetryStage] | None = None


class ApiCandidateLibraryItem(CandidateLibraryItemView):
    readiness: CandidateReadinessOut


class ApiCandidateLibraryPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ApiCandidateLibraryItem] = Field(max_length=50)
    page: int
    page_size: int
    total: int
    has_previous: bool
    has_next: bool


class CandidateProvenanceOut(BaseModel):
    """Exact versions the displayed professional facts came from."""

    model_config = ConfigDict(extra="forbid")

    profile_version_id: uuid.UUID
    profile_version: int
    profile_candidate_document_id: uuid.UUID
    profile_schema_version: str
    extraction_model_provider: str
    extraction_model_name: str
    extraction_prompt_version: str
    profile_created_at: datetime
    latest_attempt_profile_version_id: uuid.UUID | None = None


class ApiCandidateDetailResponse(CandidateDetailView):
    readiness: CandidateReadinessOut
    provenance: CandidateProvenanceOut | None = None


class RetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: RetryStage
    # Required for PROFILE_EXTRACTION (which document to re-read); ignored for
    # EMBEDDING, which always targets the candidate's current effective profile.
    document_id: uuid.UUID | None = None


class RetryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: RetryStage
    outcome: Literal["COMPLETED", "REUSED", "NOT_COMPLETED"]
    profile_version_id: uuid.UUID | None = None
    profile_status: str | None = None
    # Closed machine code of a recorded failed attempt (never raw model output).
    error_code: str | None = None
    embedding_version_id: uuid.UUID | None = None
