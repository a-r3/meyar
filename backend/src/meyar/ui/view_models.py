import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class EvidenceLocationView(BaseModel):
    page: int
    block_index: int
    snippet: str | None = None


class ProfileFactView(BaseModel):
    title: str
    detail: str | None = None
    evidence: list[EvidenceLocationView] = Field(default_factory=list)


class CandidateLibraryItemView(BaseModel):
    candidate_id: uuid.UUID
    created_at: datetime
    full_name: str | None
    current_role: str | None = None
    top_skills: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    current_profile_version: int | None
    current_profile_status: str | None
    parser_statuses: list[str]
    folder_index_statuses: list[str]


class CandidateLibraryPageView(BaseModel):
    items: list[CandidateLibraryItemView]
    page: int
    page_size: int
    total: int
    has_previous: bool
    has_next: bool


class CandidateDocumentView(BaseModel):
    document_id: uuid.UUID
    mime_type: str
    byte_size: int
    parser_status: str
    parser_name: str | None
    parser_version: str | None
    parse_error_code: str | None
    created_at: datetime


class EvaluationHistoryView(BaseModel):
    evaluation_id: uuid.UUID
    job_id: uuid.UUID
    job_title: str | None
    job_criteria_version_id: uuid.UUID
    evaluation_as_of_date: date | None
    numeric_score: Decimal | None
    fit_band: str | None
    status: str
    created_at: datetime


class CandidateDetailView(BaseModel):
    candidate_id: uuid.UUID
    created_at: datetime
    full_name: str | None
    email: str | None
    phone: str | None
    current_role: str | None = None
    professional_summary: str | None = None
    identity_status: str | None
    identity_version: int | None
    profile_status: str | None
    profile_version: int | None
    skills: list[ProfileFactView]
    experience: list[ProfileFactView]
    education: list[ProfileFactView]
    languages: list[ProfileFactView]
    certifications: list[ProfileFactView]
    projects: list[ProfileFactView]
    documents: list[CandidateDocumentView]
    evaluations: list[EvaluationHistoryView]


class CandidateSearchResultView(BaseModel):
    candidate_id: uuid.UUID
    full_name: str | None
    rank: int
    relevance_score: float
    structured_score: float | None
    semantic_score: float | None
    profile_version_id: uuid.UUID
    required_matches: list[str]
    preferred_matches: list[str]
    professional_summary: str | None
    evidence: list[EvidenceLocationView]


class PlannerOutcomeView(BaseModel):
    outcome: str
    title: str
    message: str
    executable: bool
    reason_codes: list[str]
    mode: str | None = None
    result_count: int | None = None
    infrastructure_error: bool = False


class JobView(BaseModel):
    job_id: uuid.UUID
    title: str
    status: str
    archived_at: datetime | None
    created_at: datetime
    current_criteria_version_id: uuid.UUID | None
    current_criteria_version: int | None
    criteria_count: int
    must_have_labels: list[str] = Field(default_factory=list)
    preferred_labels: list[str] = Field(default_factory=list)


class DocumentPreviewPageView(BaseModel):
    page: int
    text: str


class CandidateDocumentPreviewView(BaseModel):
    document_id: uuid.UUID
    candidate_id: uuid.UUID
    mime_type: str
    available: bool
    pages: list[DocumentPreviewPageView] = Field(default_factory=list)


class ScoreContributionView(BaseModel):
    criterion_id: str
    label: str
    criterion_kind: str
    criterion_type: str
    weight: str
    status: str
    factor: str
    weighted_points: str
    reason_code: str
    manual_review_required: bool
    evidence: list[EvidenceLocationView]


class AgentTurnLogView(BaseModel):
    role: str
    text: str


class AgentCandidateProfileView(BaseModel):
    candidate_id: uuid.UUID
    full_name: str | None
    current_role: str | None = None
    skills: list[ProfileFactView] = Field(default_factory=list)
    experience: list[ProfileFactView] = Field(default_factory=list)
    education: list[ProfileFactView] = Field(default_factory=list)
    languages: list[ProfileFactView] = Field(default_factory=list)
    certifications: list[ProfileFactView] = Field(default_factory=list)
    projects: list[ProfileFactView] = Field(default_factory=list)


class AgentEvidenceMatchView(BaseModel):
    category_label: str
    title: str
    evidence: list[EvidenceLocationView] = Field(default_factory=list)


class AgentEvidenceView(BaseModel):
    candidate_id: uuid.UUID
    full_name: str | None
    topic: str | None
    matches: list[AgentEvidenceMatchView] = Field(default_factory=list)


class CriterionRowView(BaseModel):
    """One already-validated draft/manual criterion row, presentation-ready
    — ``kind_label`` is the HR-facing text (meyar.ui.presentation.
    CRITERION_KIND_LABELS), never the raw CriterionKind enum value."""

    kind: str
    kind_label: str
    requirement: str
    min_years: str
    weight: str


class AgentJobDraftView(BaseModel):
    title: str | None
    must_have_rows: list[CriterionRowView] = Field(default_factory=list)
    preferred_rows: list[CriterionRowView] = Field(default_factory=list)
    # Non-sensitive requirements the deterministic validator could not turn
    # into a real criterion — disclosed verbatim (never silently dropped),
    # split by section so each renders under its own heading. See PR #42
    # owner correction (issue #33).
    unsupported_must_have: list[str] = Field(default_factory=list)
    unsupported_preferred: list[str] = Field(default_factory=list)
    # Source requirements that were omitted or whose material fields could
    # not be deterministically attributed. Visible, but never scorable.
    needs_review: list[str] = Field(default_factory=list)
    # Count only — a prohibited/sensitive-attribute match's own text must
    # never be redisplayed (docs/SECURITY_PRIVACY.md).
    prohibited_count: int = 0
    # Count only — a requirement that failed the deterministic JD-text
    # grounding check (D-046) was never confirmed to actually be in HR's
    # JD, so its own text must never be redisplayed either (that would
    # itself misattribute invented content to the source document).
    ungrounded_count: int = 0


class AgentToolResultView(BaseModel):
    tool_name: str
    search_outcome: PlannerOutcomeView | None = None
    search_results: list[CandidateSearchResultView] = Field(default_factory=list)
    profile: AgentCandidateProfileView | None = None
    evidence: AgentEvidenceView | None = None
    job_draft: AgentJobDraftView | None = None
    not_found_ref: int | None = None


class AgentTurnView(BaseModel):
    outcome: str
    message: str | None
    # A single, deterministic, HR-facing leading sentence for this turn —
    # computed once server-side (meyar.ui.service.build_agent_turn_view) so
    # the template never has to choose between multiple overlapping status
    # banners (D-030 conversational-UX requirement: one meaningful message
    # first, cards/evidence second, no redundant success/status text).
    headline: str | None = None
    tool_results: list[AgentToolResultView] = Field(default_factory=list)


class AgentPageView(BaseModel):
    turns: list[AgentTurnLogView] = Field(default_factory=list)
    latest: AgentTurnView | None = None


class RankedCandidateView(BaseModel):
    candidate_id: uuid.UUID
    full_name: str | None
    rank: int
    numeric_score: Decimal
    fit_band: str
    evaluation_id: uuid.UUID
    evaluation_as_of_date: date
    evaluation_policy_version: str
    scoring_policy_version: str
    contributions: list[ScoreContributionView]
