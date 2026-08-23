import re
import unicodedata
from collections.abc import Collection
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
        # Azerbaijani equivalents used by the internal HR surface. These
        # extend the same deterministic authority; Slice 9 does not keep a
        # competing planner-specific protected-trait list.
        r"\bcins(iyyət)?\b",
        r"\bkişi\b",
        r"\bqadın\b",
        r"\byaş\b",
        r"\bdoğum tarix(?:i|ini|inin|inə|ində|indən)\b",
        r"\bmilliyyət\b",
        r"\bdin(i|ə)?\b",
        r"\bmüsəlman\b",
        r"\bxristian\b",
        r"\byəhudi\b",
        r"\bevli\b",
        r"\bsubay\b",
        r"\bhamilə\b",
        r"\bsiyasi\b",
        r"\bəlillik\b",
        r"\bsağlamlıq\b",
    ]
]

# Explicit Azerbaijani inflections for the protected categories above. These
# are intentionally enumerated rather than treated as arbitrary prefixes:
# for example, ``yaşdan`` is an age criterion, while ``yaşıl`` is unrelated.
_AZ_BACK_CONSONANT_NOUN_SUFFIXES = frozenset(
    {"", "ı", "ın", "a", "da", "dan", "lar", "ları", "ların", "lara", "larda", "lardan"}
)
_AZ_FRONT_CONSONANT_NOUN_SUFFIXES = frozenset(
    {"", "i", "in", "ə", "də", "dən", "lər", "ləri", "lərin", "lərə", "lərdə", "lərdən"}
)
_AZ_FRONT_VOWEL_NOUN_SUFFIXES = frozenset(
    {"", "ni", "nin", "yə", "də", "dən", "lər", "ləri", "lərin", "lərə", "lərdə", "lərdən"}
)
_AZ_PROTECTED_TERM_SUFFIXES: tuple[tuple[str, frozenset[str]], ...] = (
    ("cins", _AZ_FRONT_CONSONANT_NOUN_SUFFIXES),
    ("cinsiyyət", _AZ_FRONT_CONSONANT_NOUN_SUFFIXES),
    ("kişi", _AZ_FRONT_VOWEL_NOUN_SUFFIXES),
    ("qadın", _AZ_BACK_CONSONANT_NOUN_SUFFIXES),
    (
        "yaş",
        _AZ_BACK_CONSONANT_NOUN_SUFFIXES
        | {"ını", "ının", "ına", "ında", "ından"},
    ),
    ("milliyyət", _AZ_FRONT_CONSONANT_NOUN_SUFFIXES),
    ("din", _AZ_FRONT_CONSONANT_NOUN_SUFFIXES),
    ("müsəlman", _AZ_BACK_CONSONANT_NOUN_SUFFIXES),
    ("xristian", _AZ_BACK_CONSONANT_NOUN_SUFFIXES),
    ("yəhudi", _AZ_FRONT_VOWEL_NOUN_SUFFIXES),
    ("evli", _AZ_FRONT_VOWEL_NOUN_SUFFIXES),
    ("subay", _AZ_BACK_CONSONANT_NOUN_SUFFIXES),
    ("hamilə", _AZ_FRONT_VOWEL_NOUN_SUFFIXES),
    ("siyasi", _AZ_FRONT_VOWEL_NOUN_SUFFIXES),
    ("əlillik", _AZ_FRONT_CONSONANT_NOUN_SUFFIXES),
    ("sağlamlıq", _AZ_BACK_CONSONANT_NOUN_SUFFIXES),
)
_AZ_CASE_TRANSLATION = str.maketrans("İI", "iı")


def _normalize_az_token(token: str) -> str:
    # Python casefold represents capital dotted İ as ``i`` + combining dot.
    # Translate Azerbaijani I variants first so token equality stays stable.
    lowered = unicodedata.normalize("NFC", token).translate(_AZ_CASE_TRANSLATION).casefold()
    words = re.findall(r"[^\W_]+", unicodedata.normalize("NFC", lowered))
    return words[0] if len(words) == 1 else ""


def matches_term_or_allowed_az_forms(
    token: str, root: str, allowed_suffixes: Collection[str]
) -> bool:
    """Match one Azerbaijani token against only explicitly allowed forms."""
    normalized_token = _normalize_az_token(token)
    normalized_root = _normalize_az_token(root)
    return bool(normalized_token) and any(
        normalized_token == normalized_root + suffix.casefold()
        for suffix in allowed_suffixes
    )


class ProhibitedCriterionError(ValueError):
    def __init__(self, criterion_id: str, matched_term: str) -> None:
        self.criterion_id = criterion_id
        self.matched_term = matched_term
        super().__init__(
            f"Criterion '{criterion_id}' references a prohibited/sensitive "
            f"attribute (matched: '{matched_term}'). See docs/SECURITY_PRIVACY.md."
        )


def find_prohibited_term(*texts: str) -> str | None:
    """Public reuse point for the sensitive/irrelevant-attribute denylist
    (docs/SECURITY_PRIVACY.md) outside of CriterionIn itself — e.g. Slice
    8 search-request validation (structured filter values, semantic query
    text). Returns the first matched term, or None if none of the texts
    match any pattern."""
    for text in texts:
        if not text:
            continue
        for pattern in _SENSITIVE_PATTERNS:
            match = pattern.search(text)
            if match:
                return match.group(0)
        for token in re.findall(r"[^\W_]+", text, flags=re.UNICODE):
            for root, suffixes in _AZ_PROTECTED_TERM_SUFFIXES:
                if matches_term_or_allowed_az_forms(token, root, suffixes):
                    return token
    return None


def _check_not_sensitive(criterion_id: str, *texts: str) -> None:
    term = find_prohibited_term(*texts)
    if term:
        raise ProhibitedCriterionError(criterion_id, term)


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
    required_level: str | None = Field(default=None, max_length=50)
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
