from pydantic import BaseModel, Field

from meyar.schemas.candidate_profile import EvidenceRef


class IdentityFieldItem(BaseModel):
    """One extracted identity fact with its own evidence — mirrors the
    profile-extraction items in schemas.candidate_profile. A field the
    model could not find is simply absent (None) at the parent level,
    never fabricated."""

    value: str = Field(min_length=1, max_length=255)
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=5)


class CandidateIdentityExtraction(BaseModel):
    """Strict, schema-validated model output for the identity-only
    extraction pass. Deliberately the ONLY schema in this codebase with
    fields for full_name/email/phone — see docs/MASTER_SPEC.md §5. This
    type must never be imported by extraction/evaluation/embedding code
    that produces or consumes CandidateProfile/search/matching signals."""

    model_config = {"extra": "forbid"}

    full_name: IdentityFieldItem | None = None
    email: IdentityFieldItem | None = None
    phone: IdentityFieldItem | None = None
