import re

from meyar.core.domain_terms import accepted_terms_for_domain, domain_term_present
from meyar.core.interval_terms import interval_grounded_in_quotes
from meyar.core.text import fold_az_ascii, normalize_azerbaijani_case
from meyar.evaluation.normalization import accepted_skill_terms
from meyar.extraction.view import ProfessionalDocumentView
from meyar.schemas.candidate_identity import CandidateIdentityExtraction
from meyar.schemas.candidate_profile import CandidateProfileExtraction, EvidenceRef


class EvidenceValidationError(Exception):
    """Raised when a model-supplied evidence reference cannot be verified
    against the real ProfessionalDocumentView. Never bypassed — an
    extraction with any unsupported evidence reference must not be
    persisted as a successful CandidateProfileVersion. See
    docs/SECURITY_PRIVACY.md."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _normalize_claim_text(text: str) -> str:
    return re.sub(r"\s+", " ", fold_az_ascii(normalize_azerbaijani_case(text))).strip()


def _term_present(text: str, term: str) -> bool:
    normalized_text = _normalize_claim_text(text)
    normalized_term = _normalize_claim_text(term)
    return (
        bool(normalized_term)
        and re.search(rf"(?<!\w){re.escape(normalized_term)}(?!\w)", normalized_text) is not None
    )


def _any_term_present(text: str, terms: frozenset[str]) -> bool:
    return any(_term_present(text, term) for term in terms)


_CURRENT_EMPLOYMENT_TERMS = frozenset(
    {
        "present",
        "current",
        "currently",
        "ongoing",
        "hazırda",
        "indiyədək",
        "indiyə qədər",
        "по настоящее время",
        "настоящее время",
    }
)


def _positive_skill_term_present(text: str, terms: frozenset[str]) -> bool:
    """Recognize a positive skill mention while rejecting fixed, obvious
    contradiction forms. This is deliberately not general entailment: it
    guarantees only the enumerated ``no/without/not`` constructions and
    otherwise requires a whole-word/phrase accepted skill alias.
    """
    normalized = _normalize_claim_text(text)
    mentioned = False
    for term in sorted(terms):
        normalized_term = _normalize_claim_text(term)
        if not normalized_term or not _term_present(normalized, normalized_term):
            continue
        mentioned = True
        escaped = re.escape(normalized_term)
        negation_patterns = (
            rf"(?<!\w)no\s+(?:\w+\s+){{0,3}}{escaped}(?!\w)",
            rf"(?<!\w)without\s+(?:\w+\s+){{0,3}}{escaped}(?!\w)",
            rf"(?<!\w){escaped}(?!\w)\s+(?:is\s+|was\s+|are\s+|were\s+)?not\s+"
            rf"(?:required|known|used|possessed|held|spoken|experienced)",
            rf"(?<!\w)(?:does|do|did|has|have|had)\s+not\s+"
            rf"(?:know|use|have|possess|speak|hold)\s+(?:\w+\s+){{0,3}}{escaped}(?!\w)",
        )
        if any(re.search(pattern, normalized) for pattern in negation_patterns):
            return False
    return mentioned


def _one_quote_supports(
    evidence: list[EvidenceRef],
    *,
    required_terms: tuple[frozenset[str], ...],
    skill_terms: frozenset[str] | None = None,
    require_current: bool = False,
) -> bool:
    """Require one attributable evidence span to carry every material
    component of one fact. Different quotes are never combined to create a
    relationship the source never states.
    """
    for ref in evidence:
        if skill_terms is not None and not _positive_skill_term_present(ref.quote, skill_terms):
            continue
        if not all(_any_term_present(ref.quote, terms) for terms in required_terms):
            continue
        if require_current and not _any_term_present(ref.quote, _CURRENT_EMPLOYMENT_TERMS):
            continue
        return True
    return False


def _literal_term(value: str | None) -> frozenset[str] | None:
    if value is None or not value.strip():
        return None
    return frozenset({value})


def _material_terms(*values: str | None) -> tuple[frozenset[str], ...]:
    return tuple(term for value in values if (term := _literal_term(value)) is not None)


def _require_claim_support(
    *,
    category: str,
    label: str,
    evidence: list[EvidenceRef],
    required_terms: tuple[frozenset[str], ...],
    skill_terms: frozenset[str] | None = None,
    require_current: bool = False,
) -> None:
    if not required_terms and skill_terms is None:
        raise EvidenceValidationError(
            "CLAIM_EVIDENCE_UNSUPPORTED",
            f"{category} claim '{label}' has no material value to verify.",
        )
    if _one_quote_supports(
        evidence,
        required_terms=required_terms,
        skill_terms=skill_terms,
        require_current=require_current,
    ):
        return
    raise EvidenceValidationError(
        "CLAIM_EVIDENCE_UNSUPPORTED",
        f"{category} claim '{label}' is not supported by one attributable evidence quote.",
    )


def verify_evidence(view: ProfessionalDocumentView, evidence: EvidenceRef) -> None:
    block = next(
        (
            b
            for b in view.blocks
            if b.page == evidence.page and b.block_index == evidence.block_index
        ),
        None,
    )
    if block is None:
        raise EvidenceValidationError(
            "EVIDENCE_INVALID",
            f"Evidence references a nonexistent page/block: page={evidence.page} "
            f"block_index={evidence.block_index}.",
        )
    if _normalize(evidence.quote) not in _normalize(block.text):
        raise EvidenceValidationError(
            "EVIDENCE_INVALID",
            f"Evidence quote not found verbatim in page={evidence.page} "
            f"block_index={evidence.block_index}.",
        )


def verify_extraction_evidence(
    view: ProfessionalDocumentView, extraction: CandidateProfileExtraction
) -> None:
    """Re-verifies every evidence reference in extraction against view —
    the exact ProfessionalDocumentView rebuilt from the CanonicalDocument
    this extraction claims to be about. A reference to another document's
    content cannot pass, because view is always rebuilt from THIS
    document's own canonical rows, never trusted from model output."""
    categories = (
        extraction.skills,
        extraction.employment_history,
        extraction.education,
        extraction.certifications,
        extraction.languages,
        extraction.projects,
    )
    for category in categories:
        for item in category:
            for ref in item.evidence:
                verify_evidence(view, ref)

    # A real quote is necessary but not sufficient: one evidence span from
    # THIS item must also substantiate every material field of the fact the
    # profile would persist and later expose to search/scoring/HR. Requiring
    # one span prevents unrelated quotes in the same item from being joined
    # into a relationship the source did not state. This is deterministic
    # lexical attribution, not natural-language entailment; unsupported or
    # paraphrased claims fail closed at the accepted-extraction boundary.
    for skill in extraction.skills:
        skill_terms = accepted_skill_terms(skill.name)
        _require_claim_support(
            category="Skill",
            label=skill.name,
            evidence=skill.evidence,
            skill_terms=skill_terms,
            required_terms=_material_terms(skill.category),
        )

    for education in extraction.education:
        _require_claim_support(
            category="Education",
            label=education.degree or education.field_of_study or education.institution or "entry",
            evidence=education.evidence,
            required_terms=_material_terms(
                education.institution,
                education.degree,
                education.field_of_study,
                education.date,
            ),
        )

    for certification in extraction.certifications:
        _require_claim_support(
            category="Certification",
            label=certification.name,
            evidence=certification.evidence,
            required_terms=_material_terms(
                certification.name, certification.issuer, certification.date
            ),
        )

    for language in extraction.languages:
        _require_claim_support(
            category="Language",
            label=language.language,
            evidence=language.evidence,
            required_terms=_material_terms(language.language, language.proficiency),
        )

    for project in extraction.projects:
        _require_claim_support(
            category="Project",
            label=project.description,
            evidence=project.evidence,
            required_terms=_material_terms(project.description),
        )

    # Slice 3 (issue #32): skill/employment grounding and domain/sector
    # evidence. Every skill_experience/domain_experience evidence ref is
    # re-verified exactly like any other category above; domain claims get
    # one additional deterministic check (see docs/DECISIONS.md and
    # meyar.core.domain_terms) so an opaque employer name can never satisfy
    # a domain claim by itself. Both also get a deterministic RELATIONAL
    # interval-grounding check (meyar.core.interval_terms): a quote proving
    # the subject was mentioned somewhere, plus a claimed year appearing
    # somewhere else, is NOT enough — one accepted evidence quote must tie
    # the subject and every claimed year together, so citing "Java" in one
    # quote and unrelated dates ("2020-2025 - Data Analyst") in another can
    # never prove "Java 2020-2025".
    for skill_exp in extraction.skill_experience:
        for ref in skill_exp.evidence:
            verify_evidence(view, ref)
        quotes = [ref.quote for ref in skill_exp.evidence]
        skill_terms = accepted_skill_terms(skill_exp.skill_name)
        if not interval_grounded_in_quotes(
            start_date=skill_exp.start_date,
            end_date=skill_exp.end_date,
            quotes=quotes,
            subject_terms=skill_terms,
        ):
            raise EvidenceValidationError(
                "SKILL_INTERVAL_NOT_EXPLICIT",
                f"Attributable interval for skill '{skill_exp.skill_name}' "
                f"(start='{skill_exp.start_date}', end='{skill_exp.end_date}') is not "
                "explicitly supported, together with the skill itself, by a single "
                "cited evidence quote.",
            )
        _require_claim_support(
            category="Skill experience",
            label=skill_exp.skill_name,
            evidence=skill_exp.evidence,
            skill_terms=skill_terms,
            required_terms=(),
        )

    for domain_exp in extraction.domain_experience:
        for ref in domain_exp.evidence:
            verify_evidence(view, ref)
        quotes = [ref.quote for ref in domain_exp.evidence]
        if not domain_term_present(domain_exp.domain, quotes):
            raise EvidenceValidationError(
                "DOMAIN_EVIDENCE_NOT_EXPLICIT",
                f"Domain/sector claim '{domain_exp.domain}' is not explicitly supported "
                "by its cited evidence quotes (no accepted sector/domain term found; an "
                "employer name alone is never sufficient).",
            )
        if not interval_grounded_in_quotes(
            start_date=domain_exp.start_date,
            end_date=domain_exp.end_date,
            quotes=quotes,
            subject_terms=accepted_terms_for_domain(domain_exp.domain),
        ):
            raise EvidenceValidationError(
                "DOMAIN_INTERVAL_NOT_EXPLICIT",
                f"Attributable interval for domain/sector '{domain_exp.domain}' "
                f"(start='{domain_exp.start_date}', end='{domain_exp.end_date}') is not "
                "explicitly supported, together with the domain term itself, by a "
                "single cited evidence quote.",
            )

    # Validate base employment facts after the more specific linked
    # skill/domain claims so their established error codes remain precise.
    # Either way, no extraction is accepted until every base fact passes.
    for employment in extraction.employment_history:
        _require_claim_support(
            category="Employment",
            label=employment.title,
            evidence=employment.evidence,
            required_terms=_material_terms(
                employment.title,
                employment.organization,
                employment.start_date,
                employment.end_date,
            ),
            require_current=employment.is_current,
        )


def verify_identity_evidence(
    view: ProfessionalDocumentView, extraction: CandidateIdentityExtraction
) -> None:
    """Same re-verification discipline as verify_extraction_evidence, for
    the identity schema's three optional fields. view here must be the
    unredacted view from build_identity_document_view — never the
    redacted professional one, which would fail every real match."""
    for field in (extraction.full_name, extraction.email, extraction.phone):
        if field is None:
            continue
        for ref in field.evidence:
            verify_evidence(view, ref)
