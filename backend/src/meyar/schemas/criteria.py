import re
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

# Heuristic denylist enforcing docs/SECURITY_PRIVACY.md / MASTER_SPEC.md §4:
# irrelevant/sensitive attributes must never become matching criteria.
# Matched case-insensitively against a criterion's label/value. This is a
# deliberately simple MVP guard, not exhaustive NLP classification — see
# docs/DECISIONS.md for the tradeoff.
_SENSITIVE_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"\bgender\b",
        r"\bsex\b",
        r"\bmale\b",
        r"\bfemale\b",
        r"\bwoman\b",
        r"\bwomen\b",
        r"\bman\b",
        r"\bmen\b",
        r"\bage\b",
        r"\byears? old\b",
        r"\bdate of birth\b",
        r"\bdob\b",
        r"\bborn\b",
        r"\brace\b",
        r"\bethnicity\b",
        r"\bethnic\b",
        r"\bnationality\b",
        r"\breligio(n|us)\b",
        r"\bchristian\b",
        r"\bmuslim\b",
        r"\bjewish\b",
        r"\bhindu\b",
        r"\bbuddhist\b",
        r"\bmarital\b",
        r"\bmarried\b",
        r"\bsingle\b",
        r"\bdivorced\b",
        r"\bwidow(ed)?\b",
        r"\bpregnan(t|cy)\b",
        r"\bpolitical\b",
        r"\bparty affiliation\b",
        r"\bdisabilit(y|ies)\b",
        r"\bhealth condition\b",
        r"\bhiv\b",
        r"\bmental health\b",
        r"\bsexual orientation\b",
        r"\bgay\b",
        r"\blesbian\b",
        r"\bbisexual\b",
        r"\btransgender\b",
        r"\bphoto(graph)?\b",
    ]
]


class ProhibitedCriterionError(ValueError):
    def __init__(self, criterion_id: str, matched_term: str) -> None:
        self.criterion_id = criterion_id
        self.matched_term = matched_term
        super().__init__(
            f"Criterion '{criterion_id}' references a prohibited/sensitive "
            f"attribute (matched: '{matched_term}'). See docs/SECURITY_PRIVACY.md."
        )


def _check_not_sensitive(criterion_id: str, *texts: str) -> None:
    for text in texts:
        if not text:
            continue
        for pattern in _SENSITIVE_PATTERNS:
            match = pattern.search(text)
            if match:
                raise ProhibitedCriterionError(criterion_id, match.group(0))


class CriterionKind(StrEnum):
    SKILL = "SKILL"
    EXPERIENCE = "EXPERIENCE"
    CERTIFICATION = "CERTIFICATION"
    EDUCATION = "EDUCATION"
    LANGUAGE = "LANGUAGE"


class CriterionType(StrEnum):
    MUST_HAVE = "MUST_HAVE"
    PREFERRED = "PREFERRED"


class CriterionIn(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_]{1,64}$")
    kind: CriterionKind
    type: CriterionType
    label: str = Field(min_length=1, max_length=200)
    value: str | None = Field(default=None, max_length=200)
    min_years: float | None = Field(default=None, ge=0, le=60)
    weight: float = Field(default=1.0, ge=0, le=10)
    evidence_required: bool = True
    manual_review_required: bool = False

    @model_validator(mode="after")
    def _validate_kind_specific_fields(self) -> "CriterionIn":
        if self.kind == CriterionKind.EXPERIENCE:
            if self.min_years is None:
                raise ValueError(
                    f"Criterion '{self.id}': kind EXPERIENCE requires min_years."
                )
        elif not self.value:
            raise ValueError(
                f"Criterion '{self.id}': kind {self.kind.value} requires a non-empty value."
            )
        return self

    @model_validator(mode="after")
    def _validate_not_sensitive(self) -> "CriterionIn":
        _check_not_sensitive(self.id, self.label, self.value or "")
        return self


class CriterionOut(CriterionIn):
    pass


class CriteriaListIn(BaseModel):
    criteria: list[CriterionIn] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _validate_unique_ids(self) -> "CriteriaListIn":
        ids = [c.id for c in self.criteria]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ValueError(f"Duplicate criterion id(s) in one version: {sorted(duplicates)}")
        return self
