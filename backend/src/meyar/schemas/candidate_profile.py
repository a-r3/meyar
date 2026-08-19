from pydantic import BaseModel, Field


class EvidenceRef(BaseModel):
    """Points back to the exact source location a fact was extracted from.
    Never trusted at face value — meyar.extraction.evidence re-verifies
    every reference against the real CanonicalDocument before persistence.
    """

    page: int = Field(ge=1)
    block_index: int = Field(ge=0)
    quote: str = Field(min_length=1, max_length=500)


class SkillItem(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=100)
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=10)


class EmploymentItem(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    organization: str | None = Field(default=None, max_length=200)
    start_date: str | None = Field(default=None, max_length=50)
    end_date: str | None = Field(default=None, max_length=50)
    is_current: bool = False
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=10)


class EducationItem(BaseModel):
    institution: str | None = Field(default=None, max_length=200)
    degree: str | None = Field(default=None, max_length=200)
    field_of_study: str | None = Field(default=None, max_length=200)
    date: str | None = Field(default=None, max_length=50)
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=10)


class CertificationItem(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    issuer: str | None = Field(default=None, max_length=200)
    date: str | None = Field(default=None, max_length=50)
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=10)


class LanguageItem(BaseModel):
    language: str = Field(min_length=1, max_length=100)
    proficiency: str | None = Field(default=None, max_length=50)
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=10)


class ProjectItem(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=10)


class CandidateProfileExtraction(BaseModel):
    """Strict, schema-validated model output. Every item requires at least
    one evidence reference — a fact with zero evidence simply fails
    validation, never silently persisted. See docs/MASTER_SPEC.md §7 and
    the CandidateProfile/CandidateIdentity boundary in §5: this schema has
    no field for name/email/phone/age/gender/etc. by construction, so the
    model cannot smuggle sensitive attributes in without failing
    validation against an unknown field (extra='forbid')."""

    model_config = {"extra": "forbid"}

    skills: list[SkillItem] = Field(default_factory=list, max_length=100)
    employment_history: list[EmploymentItem] = Field(default_factory=list, max_length=50)
    education: list[EducationItem] = Field(default_factory=list, max_length=50)
    certifications: list[CertificationItem] = Field(default_factory=list, max_length=50)
    languages: list[LanguageItem] = Field(default_factory=list, max_length=50)
    projects: list[ProjectItem] = Field(default_factory=list, max_length=50)
