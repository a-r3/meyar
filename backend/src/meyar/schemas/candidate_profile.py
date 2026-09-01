from pydantic import BaseModel, Field, model_validator


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


class SkillExperienceItem(BaseModel):
    """Explicit grounding of a skill claim in ONE attributable employment
    period — the structural link SkillItem/EmploymentItem alone cannot
    express (root-caused in D-027; closed by issue #32). Populated only
    when the source text itself ties this specific skill to this specific
    employment entry's dates (e.g. a bullet under that job, or "5 years of
    Java at Company X"). A skill with no such link simply has no
    SkillExperienceItem — its duration then stays UNKNOWN, never
    substituted with total career experience. `employment_index` is the
    0-based position of the supporting entry in this same extraction's
    `employment_history` list, re-validated below — never a free-floating
    id the model could point anywhere."""

    skill_name: str = Field(min_length=1, max_length=200)
    employment_index: int = Field(ge=0)
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=10)


class DomainExperienceItem(BaseModel):
    """An explicit sector/domain claim (e.g. "banking", "AML") — never
    inferred from an employer's name alone; extraction verification
    (meyar.extraction.evidence) independently checks the cited quotes
    contain an accepted, unambiguous domain/sector term before this can be
    persisted. `employment_index` is optional: set only when the text also
    ties the domain to one specific attributable period (enabling
    deterministic duration reasoning); otherwise the domain is known but
    its duration stays UNKNOWN."""

    domain: str = Field(min_length=1, max_length=100)
    employment_index: int | None = Field(default=None, ge=0)
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
    # Slice 3 (issue #32) evidence-capability completion. Both default to
    # empty so a pre-Slice-3 CandidateProfileVersion.profile_content (JSON,
    # no migration needed) still validates unchanged — see docs/DECISIONS.md.
    skill_experience: list[SkillExperienceItem] = Field(default_factory=list, max_length=100)
    domain_experience: list[DomainExperienceItem] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def _validate_employment_indices(self) -> "CandidateProfileExtraction":
        employment_count = len(self.employment_history)
        for item in self.skill_experience:
            if item.employment_index >= employment_count:
                raise ValueError(
                    f"skill_experience entry for '{item.skill_name}' references "
                    f"employment_index={item.employment_index}, but employment_history "
                    f"has only {employment_count} entries."
                )
        for domain_item in self.domain_experience:
            if (
                domain_item.employment_index is not None
                and domain_item.employment_index >= employment_count
            ):
                raise ValueError(
                    f"domain_experience entry for '{domain_item.domain}' references "
                    f"employment_index={domain_item.employment_index}, but employment_history "
                    f"has only {employment_count} entries."
                )
        return self
