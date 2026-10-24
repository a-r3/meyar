"""Job lifecycle and criteria-provenance DTOs (issue #45)."""

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from meyar.agent.semantic_provenance import (
    AgentSemanticProvenance,
    NonScorableRequirementProvenance,
)
from meyar.schemas.read_models import JobView


class ApiJobPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[JobView] = Field(max_length=50)
    page: int
    page_size: int
    total: int
    has_next: bool


class JobArchiveOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    status: str
    archived_at: datetime | None
    already_archived: bool


class AuthoringChannel(StrEnum):
    """How this immutable criteria version was authored, as far as MEYAR can
    truthfully tell. ``HUMAN_OR_LEGACY`` is deliberately not split: manual UI
    versions and pre-provenance agent versions carry the same (absent) record."""

    AGENT_CONFIRMED_DRAFT = "AGENT_CONFIRMED_DRAFT"
    MACHINE_API = "MACHINE_API"
    HUMAN_OR_LEGACY = "HUMAN_OR_LEGACY"


class DraftConfirmationLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_id: uuid.UUID
    confirmed_at: datetime


class CriteriaVersionProvenanceOut(BaseModel):
    """Why this version's criteria exist, and why other source requirements are
    NOT scoring authority. Nothing here is read by the scorer."""

    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    criteria_version_id: uuid.UUID
    version_number: int
    authoring_channel: AuthoringChannel
    # Present only while the independent AgentDraftConfirmation row exists
    # (it follows the Job's own retention/deletion).
    draft_confirmation: DraftConfirmationLink | None = None
    # Strictly validated durable record; None for manual/API/legacy versions —
    # never reconstructed.
    semantic_provenance: AgentSemanticProvenance | None = None
    # False for every version confirmed before non-scorable provenance existed:
    # absence is "not recorded", not "none were left out".
    non_scorable_recorded: bool = False
    non_scorable_requirements: list[NonScorableRequirementProvenance] = Field(
        default_factory=list
    )
    # Legacy human-readable strings kept on the version for display.
    unsupported_requirements: list[str] = Field(default_factory=list)
    needs_review_requirements: list[str] = Field(default_factory=list)
