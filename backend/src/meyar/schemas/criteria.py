import re
from collections.abc import Collection
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from meyar.core.text import fold_az_ascii, normalize_azerbaijani_case

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
        r"\bcitizenship\b",
        r"\bcitizen\b",
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
        r"\bhealthy\b",
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
        r"\bvətəndaş(?:lıq|ı|lığı)?\b",
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
        r"\bsağlam\b",
        # The same bounded Azerbaijani roots after ordinary ASCII-keyboard
        # folding.  These are needed because the safety scan intentionally
        # runs before semantic language interpretation.
        r"\bcinsiyyet\b",
        r"\bkisi\b",
        r"\bqadin\b",
        r"\byas\b",
        r"\bdogum tarix\w*\b",
        r"\bmilliyyet\b",
        r"\bvetendas(?:liq|i|ligi)?\b",
        r"\belillik\b",
        r"\bsaglamliq\b",
        r"\bsaglam\b",
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
        _AZ_BACK_CONSONANT_NOUN_SUFFIXES | {"ını", "ının", "ına", "ında", "ından"},
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
    ("sağlam", _AZ_BACK_CONSONANT_NOUN_SUFFIXES),
)

# Azerbaijani possessive/case suffixes can soften a final ``q``/``k``.
# Enumerate those ordinary protected-lexeme forms explicitly; do not turn
# the denylist into a substring/prefix matcher (``yaşıl`` must stay safe).
_AZ_PROTECTED_MUTATED_FORMS = frozenset(
    {
        "sağlamlığı",
        "sağlamlığın",
        "sağlamlığa",
        "sağlamlığında",
        "sağlamlığından",
        "əlilliyi",
        "əlilliyin",
        "əlilliyə",
        "əlilliyində",
        "əlilliyindən",
    }
)

# Concept-level policy patterns complement the bounded lexical forms above.
# They describe protected *semantics* (comparison, idiom, and multi-word
# constructions), rather than individual audit sentences.  Both the raw JD
# scan and the final CriterionIn boundary use this same server-owned policy.
_PROTECTED_CONCEPT_PATTERNS = (
    # Nationality / citizenship.
    re.compile(
        r"(?i)\b(?:national(?:ity|ities)|citizen(?:ship)?|passport\s+holder|"
        r"right\s+to\s+citizenship)\b"
    ),
    re.compile(r"(?i)\b(?:vetendas|milliyyet)\w*\b"),
    # Age expressed directly, comparatively, or as an age band.
    re.compile(
        r"(?i)\b(?:younger|older)\s+than\s+(?:\d+|one|two|three|four|five|six|"
        r"seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
        r"seventeen|eighteen|nineteen|twenty(?:[- ](?:one|two|three|four|five|six|"
        r"seven|eight|nine))?|thirty(?:[- ](?:one|two|three|four|five|six|seven|eight|"
        r"nine))?|forty(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?|"
        r"fifty(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?|sixty)\b|"
        r"\b(?:under|below|above|over|aged?)\s+(?:the\s+age\s+of\s+)?"
        r"(?:\d+|ten|twenty|thirty|forty|fifty|sixty)\b|"
        r"\b(?:in\s+(?:his|her|their)\s+)?"
        r"(?:twenties|thirties|forties|fifties|sixties)\b"
    ),
    re.compile(
        r"(?i)\b(?:\d+|[a-z]+)\s*yas(?:inda|dan|a|i)?\b|"
        r"\byas(?:i|ina|inda)?\s+(?:\d+|[a-z]+)\b|"
        r"\b(?:cavan|genc|yuxari|asagi)\s+yas\w*\b"
    ),
    # Health and medical fitness, including common idioms.
    re.compile(
        r"(?i)\b(?:(?:clean|clear|good|sound)\s+(?:bill\s+of\s+)?health|"
        r"medically\s+fit|fit\s+and\s+healthy|"
        r"physical(?:ly)?\s+fit(?:ness)?|medical\s+(?:fitness|condition))\b"
    ),
    re.compile(
        r"(?i)\b(?:sehhet\w*|tibbi\s+(?:cehetden\s+)?yararli|"
        r"fiziki\s+(?:cehetden\s+)?saglam)\b"
    ),
    # Disability and euphemistic equivalents.
    re.compile(
        r"(?i)\b(?:disabled|able[- ]bodied|differently\s+abled|"
        r"special\s+needs?|physical\s+impairment)\b"
    ),
    re.compile(r"(?i)\b(?:mehdud\s+imkan\w*|xususi\s+ehtiyac\w*)\b"),
    # Gender/sex expressed without the literal field label.
    re.compile(
        r"(?i)\b(?:men|women|males?|females?)\s+only\b|"
        r"\b(?:male|female)\s+(?:applicants?|candidates?)\b"
    ),
)


def _matches_protected_concept(text: str) -> str | None:
    folded = fold_az_ascii(normalize_azerbaijani_case(text))
    for pattern in _PROTECTED_CONCEPT_PATTERNS:
        match = pattern.search(folded)
        if match:
            return text[match.start() : match.end()]
    return None


def _normalize_az_token(token: str) -> str:
    words = re.findall(r"[^\W_]+", normalize_azerbaijani_case(token))
    return words[0] if len(words) == 1 else ""


def matches_term_or_allowed_az_forms(
    token: str, root: str, allowed_suffixes: Collection[str]
) -> bool:
    """Match one Azerbaijani token against only explicitly allowed forms."""
    normalized_token = _normalize_az_token(token)
    normalized_root = _normalize_az_token(root)
    return bool(normalized_token) and any(
        normalized_token == normalized_root + normalize_azerbaijani_case(suffix)
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
        concept = _matches_protected_concept(text)
        if concept:
            return concept
        # Run the same bounded policy over both ordinary Unicode text and
        # Azerbaijani ASCII-keyboard folding.  This is a safety boundary, not
        # semantic interpretation: a model cannot evade it by relabelling
        # ``vətəndaşlıq`` as LANGUAGE, and an HR user typing ``vetendasliq``
        # receives the identical result.
        for candidate, _is_ascii_folded in (
            (text, False),
            (fold_az_ascii(normalize_azerbaijani_case(text)), True),
        ):
            for pattern in _SENSITIVE_PATTERNS:
                match = pattern.search(candidate)
                if match:
                    return match.group(0)
        for candidate, is_ascii_folded in (
            (text, False),
            (fold_az_ascii(normalize_azerbaijani_case(text)), True),
        ):
            for token in re.findall(r"[^\W_]+", candidate, flags=re.UNICODE):
                if normalize_azerbaijani_case(token) in _AZ_PROTECTED_MUTATED_FORMS:
                    return token
                for root, suffixes in _AZ_PROTECTED_TERM_SUFFIXES:
                    folded_root = fold_az_ascii(root) if is_ascii_folded else root
                    folded_suffixes = (
                        frozenset(fold_az_ascii(value) for value in suffixes)
                        if is_ascii_folded
                        else suffixes
                    )
                    if matches_term_or_allowed_az_forms(token, folded_root, folded_suffixes):
                        return token
    return None


def _check_not_sensitive(criterion_id: str, *texts: str) -> None:
    term = find_prohibited_term(*texts)
    if term:
        raise ProhibitedCriterionError(criterion_id, term)


_NON_SUBJECT_GRAMMAR_RE = re.compile(
    r"(?i)\b(?:applicants?|candidates?|namized\w*)\s+(?:are|must|should|have|"
    r"olmal\w*|teleb\w*)\b|\b(?:required|mandatory|preferred|optional|"
    r"nice\s+to\s+have|teleb\w*|mecburi\w*|ustunluk\w*)\b"
)
_COUNT_LIKE_SUBJECT_RE = re.compile(
    r"(?i)^\s*(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(?:certificat(?:e|ion)s?|degrees?|skills?|languages?|candidates?|results?)\s*$"
)
_LANGUAGE_SUBJECT_RE = re.compile(
    r"(?i)^[^\W\d_]+(?:[- ][^\W\d_]+){0,2}(?:\s+(?:language|dili))?$"
)


def _check_kind_subject_authority(criterion: "CriterionIn") -> None:
    """Reject values that cannot be the named entity for their family.

    This is deliberately a shape/grammar boundary, not a technology list.
    Unknown professional entities remain valid, while complete HR clauses,
    result/count expressions, and structurally impossible language subjects
    cannot be persisted merely because an upstream model chose a safe kind.
    """
    value = (criterion.value or "").strip()
    if not value:
        return
    folded = fold_az_ascii(normalize_azerbaijani_case(value))
    if _NON_SUBJECT_GRAMMAR_RE.search(folded):
        raise ValueError(
            f"Criterion '{criterion.id}': value must be a bounded professional subject, "
            "not an HR requirement clause."
        )
    if _COUNT_LIKE_SUBJECT_RE.fullmatch(folded):
        raise ValueError(
            f"Criterion '{criterion.id}': a count expression cannot be a criterion subject."
        )
    if criterion.kind == CriterionKind.LANGUAGE and not _LANGUAGE_SUBJECT_RE.fullmatch(value):
        raise ValueError(
            f"Criterion '{criterion.id}': LANGUAGE requires one bounded language name."
        )


class CriterionKind(StrEnum):
    SKILL = "SKILL"
    EXPERIENCE = "EXPERIENCE"
    CERTIFICATION = "CERTIFICATION"
    EDUCATION = "EDUCATION"
    LANGUAGE = "LANGUAGE"
    # Slice 3 (issue #32): a duration claim scoped to one named skill (e.g.
    # "5 years of Java") — distinct from EXPERIENCE, which is unscoped
    # total career duration. Provable only from meyar.schemas.
    # candidate_profile.SkillExperienceItem grounding; otherwise UNKNOWN.
    SKILL_EXPERIENCE = "SKILL_EXPERIENCE"
    # An explicit sector/domain claim (e.g. "banking", "AML"), optionally
    # duration-scoped via min_years. Provable only from explicit
    # DomainExperienceItem evidence, never inferred from an employer name.
    DOMAIN_EXPERIENCE = "DOMAIN_EXPERIENCE"


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
                raise ValueError(f"Criterion '{self.id}': kind EXPERIENCE requires min_years.")
            if self.value is not None:
                raise ValueError(f"Criterion '{self.id}': kind EXPERIENCE does not accept value.")
        elif self.kind == CriterionKind.SKILL_EXPERIENCE:
            if not self.value:
                raise ValueError(
                    f"Criterion '{self.id}': kind SKILL_EXPERIENCE requires a non-empty "
                    "value (the skill name)."
                )
            if self.min_years is None:
                raise ValueError(
                    f"Criterion '{self.id}': kind SKILL_EXPERIENCE requires min_years."
                )
        elif self.kind == CriterionKind.DOMAIN_EXPERIENCE:
            if not self.value:
                raise ValueError(
                    f"Criterion '{self.id}': kind DOMAIN_EXPERIENCE requires a non-empty "
                    "value (the domain/sector name). min_years is optional."
                )
        elif not self.value:
            raise ValueError(
                f"Criterion '{self.id}': kind {self.kind.value} requires a non-empty value."
            )
        if self.required_level is not None and self.kind != CriterionKind.LANGUAGE:
            raise ValueError(f"Criterion '{self.id}': required_level is only valid for LANGUAGE.")
        if self.min_years is not None and self.kind not in (
            CriterionKind.EXPERIENCE,
            CriterionKind.SKILL_EXPERIENCE,
            CriterionKind.DOMAIN_EXPERIENCE,
        ):
            raise ValueError(
                f"Criterion '{self.id}': min_years is not evaluated for {self.kind.value}."
            )
        if not self.evidence_required:
            raise ValueError(f"Criterion '{self.id}': evidence_required must remain true.")
        _check_kind_subject_authority(self)
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

    @model_validator(mode="after")
    def _validate_positive_total_weight(self) -> "CriteriaListIn":
        if not any(criterion.weight > 0 for criterion in self.criteria):
            raise ValueError("At least one criterion must have weight greater than zero.")
        return self
