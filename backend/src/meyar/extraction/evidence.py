import calendar
import re
from collections.abc import Iterator
from datetime import date

from meyar.core.domain_terms import accepted_terms_for_domain, domain_term_present
from meyar.core.interval_terms import interval_grounded_in_quotes
from meyar.core.text import fold_az_ascii, normalize_azerbaijani_case
from meyar.evaluation.normalization import accepted_skill_terms
from meyar.extraction.view import ProfessionalDocumentView
from meyar.schemas.candidate_identity import CandidateIdentityExtraction
from meyar.schemas.candidate_profile import CandidateProfileExtraction, EmploymentItem, EvidenceRef


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


def _normalize_scope_text(text: str) -> str:
    """Normalize claim text without erasing structural line boundaries."""
    folded = fold_az_ascii(normalize_azerbaijani_case(text)).replace("\r\n", "\n")
    folded = folded.replace("\r", "\n")
    folded = re.sub(r"[^\S\n]+", " ", folded)
    folded = re.sub(r" *\n+ *", "\n", folded)
    return folded.strip()


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


# EvidenceRef has no character offset. All occurrences of the supplied quote
# in its cited block must agree; an ambiguous cropped quote cannot choose the
# favorable occurrence. A longer unique quote can identify one occurrence.
_CONTEXT_CHARS = 200
_SCOPE_BOUNDARY = re.compile(r"\n|[;!?]|\.(?=\s|$)|\b(?:but|however)\b")
_WORD_TOKEN = re.compile(r"\b\w+\b")
_NEGATING_USE_VERBS = frozenset({"use", "uses", "using"})


def _quote_pattern(quote: str) -> re.Pattern[str]:
    words = _normalize_claim_text(quote).split()
    return re.compile(r"\s+".join(re.escape(word) for word in words))


def _source_spans(view: ProfessionalDocumentView, ref: EvidenceRef) -> list[tuple[str, int, int]]:
    verify_evidence(view, ref)
    block = next(b for b in view.blocks if b.page == ref.page and b.block_index == ref.block_index)
    source = _normalize_scope_text(block.text)
    spans = []
    for match in _quote_pattern(ref.quote).finditer(source):
        start = max(0, match.start() - _CONTEXT_CHARS)
        end = min(len(source), match.end() + _CONTEXT_CHARS)
        while start < match.start() and source[start].isspace():
            start += 1
        spans.append((source[start:end].rstrip(), match.start() - start, match.end() - start))
    return spans


def _negated_occurrence(text: str, start: int, end: int) -> bool:
    # A negative governor remains active across a bounded coordinated list;
    # ``and``/``or``/``nor`` do not create or end scope by themselves. The
    # structural boundary above resets it before an independent statement.
    # This deliberately models only the explicit constructs supported here,
    # never general entailment.
    before = _SCOPE_BOUNDARY.split(text[:start])[-1]
    after = _SCOPE_BOUNDARY.split(text[end:])[0]
    words = [match.group() for match in _WORD_TOKEN.finditer(before)]
    governed = False
    for index, word in enumerate(words):
        next_word = words[index + 1] if index + 1 < len(words) else None
        previous_word = words[index - 1] if index else None
        if word in {"no", "without", "neither"}:
            governed = True
        elif (
            word == "not"
            and next_word in _NEGATING_USE_VERBS
            and previous_word in {"do", "does", "did"}
        ):
            governed = True
    local_before = re.split(r"\b(?:and|or|nor)\b", before)[-1]
    negative_prefix = re.search(
        r"(?<!\w)(?:no|without|neither|not)\s+(?:\w+\s+){0,6}$", local_before
    )
    if negative_prefix is not None and re.match(r"not\s+only\b", negative_prefix.group()):
        negative_prefix = None
    return bool(
        governed
        or negative_prefix
        or re.match(
            r"\s+(?:experience\s+)?(?:(?:is|was|are|were)\s+)?not(?!\s+only\b)(?:\W|$)",
            after,
        )
        or re.match(r"\s+(?:is\s+)?(?:absent|unavailable)(?:\W|$)", after)
    )


def _positive_term_present(
    text: str, terms: frozenset[str], *, span: tuple[int, int] | None = None
) -> bool:
    normalized = _normalize_scope_text(text)
    mentioned = False
    for term in sorted(terms):
        term = _normalize_claim_text(term)
        if not term:
            continue
        for match in re.finditer(rf"(?<!\w){re.escape(term)}(?!\w)", normalized):
            if span is not None and not (span[0] <= match.start() and match.end() <= span[1]):
                continue
            mentioned = True
            if _negated_occurrence(normalized, match.start(), match.end()):
                return False
    return mentioned


def _supported_in_source(
    view: ProfessionalDocumentView, ref: EvidenceRef, terms: frozenset[str]
) -> bool:
    spans = _source_spans(view, ref)
    return bool(spans) and all(
        _positive_term_present(context, terms, span=(start, end)) for context, start, end in spans
    )


def _current_relationship_supported(
    quote: str,
    required_terms: tuple[frozenset[str], ...],
    *,
    span: tuple[int, int] | None = None,
) -> bool:
    # Require a positive marker in the fact's own relationship, not an
    # unrelated job later in a large quote. A standalone '; current' is a
    # permitted continuation of the immediately preceding relationship.
    text = _normalize_claim_text(quote)
    clauses = re.finditer(r".+?(?:[;!?]|\.(?=\s|$)|$)", text)
    previous = ""
    for clause in clauses:
        relationship = clause.group().strip().rstrip(";.!?")
        marker_here = any(
            (span is None or span[0] <= marker.start() and marker.end() <= span[1])
            and clause.start() <= marker.start()
            and marker.end() <= clause.end()
            for term in _CURRENT_EMPLOYMENT_TERMS
            for marker in re.finditer(
                rf"(?<!\w){re.escape(_normalize_claim_text(term))}(?!\w)", text
            )
        )
        if marker_here:
            if not all(_any_term_present(relationship, terms) for terms in required_terms):
                marker_only = any(
                    relationship.rstrip(".") == _normalize_claim_text(term)
                    for term in _CURRENT_EMPLOYMENT_TERMS
                )
                if not marker_only:
                    continue
                relationship = previous + " " + relationship
            if (
                all(_positive_term_present(relationship, terms) for terms in required_terms)
                and _positive_term_present(relationship, _CURRENT_EMPLOYMENT_TERMS)
                and not re.search(
                    r"\b(?:ended|terminated|former|previous|ceased)\b|\bno longer\b", relationship
                )
                and not re.search(
                    r"\b(?:19|20)\d{2}\s*(?:[-–—/]|to)\s*(?:19|20)\d{2}\b", relationship
                )
            ):
                return True
        previous = relationship
    return False


def _one_quote_supports(
    view: ProfessionalDocumentView,
    evidence: list[EvidenceRef],
    *,
    required_terms: tuple[frozenset[str], ...],
    skill_terms: frozenset[str] | None = None,
    require_current: bool = False,
) -> bool:
    terms = required_terms + ((skill_terms,) if skill_terms is not None else ())
    for ref in evidence:
        if not all(_supported_in_source(view, ref, group) for group in terms):
            continue
        if require_current and (
            not _supported_in_source(view, ref, _CURRENT_EMPLOYMENT_TERMS)
            or not _current_relationship_supported(ref.quote, terms)
            or not all(
                _current_relationship_supported(context, terms, span=(start, end))
                for context, start, end in _source_spans(view, ref)
            )
        ):
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
    view: ProfessionalDocumentView,
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
        view,
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


def _requires_current_authority(end_date: str | None, is_current: bool) -> bool:
    # The frozen duration parser recognizes these strings even without the
    # flag. Guard every such route before arithmetic can extend to as-of.
    return is_current or bool(re.search(r"present|current|now|ongoing", end_date or "", re.I))


def _require_current_shape(end_date: str | None, is_current: bool) -> None:
    if is_current and end_date and not _any_term_present(end_date, _CURRENT_EMPLOYMENT_TERMS):
        raise EvidenceValidationError(
            "CLAIM_EVIDENCE_UNSUPPORTED", "Current claim has a closed end date."
        )


def _date_bounds(value: str | None) -> tuple[date, date] | None:
    if value is None:
        return None
    match = re.fullmatch(r"((?:19|20)\d{2})(?:[-/](\d{1,2}))?(?:[-/](\d{1,2}))?", value.strip())
    if match is None:
        return None
    year, month, day = (int(v) if v else None for v in match.groups())
    assert year is not None
    try:
        lo = date(year, month or 1, day or 1)
        hi = date(year, month or 12, day or calendar.monthrange(year, month or 12)[1])
    except ValueError:
        return None
    return lo, hi


def _require_compatible_period(
    employment: EmploymentItem, start: str | None, end: str | None, is_current: bool
) -> None:
    # Attribution only: never calculate duration here. Coarse dates denote
    # their possible calendar bounds; a claimed sub-period must fit inside
    # the referenced occurrence. Missing bounds cannot prove a dated link.
    if start is None and end is None and not is_current:
        return
    job_start = _date_bounds(employment.start_date)
    job_end = _date_bounds(employment.end_date)
    job_current = _requires_current_authority(employment.end_date, employment.is_current)
    own_start, own_end = _date_bounds(start), _date_bounds(end)
    compatible = (
        (
            start is None
            or (own_start is not None and job_start is not None and own_start[0] >= job_start[0])
        )
        and (
            end is None
            or (is_current and _requires_current_authority(end, False))
            or (
                own_end is not None
                and (job_current or (job_end is not None and own_end[1] <= job_end[1]))
            )
        )
        and (not is_current or job_current)
        and (own_start is None or job_end is None or own_start[0] <= job_end[1])
        and (own_end is None or job_start is None or own_end[1] >= job_start[0])
        and (own_start is None or own_end is None or own_start[0] <= own_end[1])
    )
    if not compatible:
        raise EvidenceValidationError(
            "CLAIM_EVIDENCE_UNSUPPORTED",
            "Linked employment period is not compatible with the claimed interval.",
        )


def _require_linked_employment_support(
    view: ProfessionalDocumentView,
    *,
    category: str,
    label: str,
    evidence: list[EvidenceRef],
    subject_terms: frozenset[str],
    employment: EmploymentItem,
    start_date: str | None,
    end_date: str | None,
    is_current: bool,
) -> None:
    """Require one cited span to connect subject, interval, and employer.

    ``employment_index`` is only safe presentation context when the item's
    own evidence also names the referenced job/employer. A quote about
    Python at Globex must not be rendered under Acme merely because both
    employment rows exist in the same profile.
    """
    is_current = _requires_current_authority(end_date, is_current)
    _require_current_shape(end_date, is_current)
    _require_compatible_period(employment, start_date, end_date, is_current)
    required_terms = (
        *_material_terms(employment.title, employment.organization, start_date, end_date),
    )
    _require_claim_support(
        view,
        category=category,
        label=label,
        evidence=evidence,
        skill_terms=subject_terms,
        required_terms=required_terms,
        require_current=is_current,
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
            view,
            category="Skill",
            label=skill.name,
            evidence=skill.evidence,
            skill_terms=skill_terms,
            required_terms=_material_terms(skill.category),
        )

    for education in extraction.education:
        _require_claim_support(
            view,
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
            view,
            category="Certification",
            label=certification.name,
            evidence=certification.evidence,
            required_terms=_material_terms(
                certification.name, certification.issuer, certification.date
            ),
        )

    for language in extraction.languages:
        _require_claim_support(
            view,
            category="Language",
            label=language.language,
            evidence=language.evidence,
            required_terms=_material_terms(language.language, language.proficiency),
        )

    for project in extraction.projects:
        _require_claim_support(
            view,
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
            view,
            category="Skill experience",
            label=skill_exp.skill_name,
            evidence=skill_exp.evidence,
            skill_terms=skill_terms,
            required_terms=(),
        )

    for domain_exp in extraction.domain_experience:
        _require_current_shape(domain_exp.end_date, domain_exp.is_current)
        for ref in domain_exp.evidence:
            verify_evidence(view, ref)
        quotes = [ref.quote for ref in domain_exp.evidence]
        domain_terms = accepted_terms_for_domain(domain_exp.domain)
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
            subject_terms=domain_terms,
        ):
            raise EvidenceValidationError(
                "DOMAIN_INTERVAL_NOT_EXPLICIT",
                f"Attributable interval for domain/sector '{domain_exp.domain}' "
                f"(start='{domain_exp.start_date}', end='{domain_exp.end_date}') is not "
                "explicitly supported, together with the domain term itself, by a "
                "single cited evidence quote.",
            )
        _require_claim_support(
            view,
            category="Domain experience",
            label=domain_exp.domain,
            evidence=domain_exp.evidence,
            skill_terms=domain_terms,
            required_terms=_material_terms(domain_exp.start_date, domain_exp.end_date),
            require_current=_requires_current_authority(domain_exp.end_date, domain_exp.is_current),
        )
        if domain_exp.employment_index is not None:
            _require_linked_employment_support(
                view,
                category="Domain employment attribution",
                label=domain_exp.domain,
                evidence=domain_exp.evidence,
                subject_terms=domain_terms,
                employment=extraction.employment_history[domain_exp.employment_index],
                start_date=domain_exp.start_date,
                end_date=domain_exp.end_date,
                is_current=domain_exp.is_current,
            )

    # Validate base employment facts after the more specific linked
    # skill/domain claims so their established error codes remain precise.
    # Either way, no extraction is accepted until every base fact passes.
    for employment in extraction.employment_history:
        _require_current_shape(employment.end_date, employment.is_current)
        _require_claim_support(
            view,
            category="Employment",
            label=employment.title,
            evidence=employment.evidence,
            required_terms=_material_terms(
                employment.title,
                employment.organization,
                employment.start_date,
                employment.end_date,
            ),
            require_current=_requires_current_authority(employment.end_date, employment.is_current),
        )

    for skill_exp in extraction.skill_experience:
        _require_linked_employment_support(
            view,
            category="Skill employment attribution",
            label=skill_exp.skill_name,
            evidence=skill_exp.evidence,
            subject_terms=accepted_skill_terms(skill_exp.skill_name),
            employment=extraction.employment_history[skill_exp.employment_index],
            start_date=skill_exp.start_date,
            end_date=skill_exp.end_date,
            is_current=skill_exp.is_current,
        )


_EMAIL_TOKEN = re.compile(
    r"(?<![\w.!#$%&'*+/=?^`{|}~@-])[\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)+(?![\w@-])"
)
_PHONE_GROUP = r"(?:\([0-9]+\)|[0-9]+)"
_PHONE_TOKEN = re.compile(rf"(?<![\w+])\+?{_PHONE_GROUP}(?:[ -]{_PHONE_GROUP})*(?!\w)")
_PHONE_LABEL = re.compile(
    r"\b(?:phone|mobile|telephone|tel\.?|telefon|mobil|"
    r"contact(?:\s+(?:number|no))?|əlaqə(?:\s+nömrəsi)?)"
    r"(?:\s+(?:number|no))?\s*[:#-]?\s*$"
)
_NON_PHONE_IDENTIFIER_LABEL = re.compile(
    r"\b(?:reference|ref|invoice|employee\s+(?:id|number|no)|account|acct|id|code)"
    r"(?:\s+(?:id|number|no))?\s*[:#-]?\s*$"
)


def _digits(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


def _phone_context_prefix(text: str, start: int) -> str:
    boundary = max(text.rfind(separator, 0, start) for separator in ("\n", "|", ";", ","))
    return text[boundary + 1 : start]


def _phone_like_occurrences(text: str) -> Iterator[re.Match[str]]:
    """Yield authoritative phone occurrences from canonical text.

    Explicit non-phone identifier labels fail closed. Otherwise an occurrence
    needs conventional phone syntax or a directly adjacent phone/contact label;
    an unlabeled uninterrupted digit token has no trustworthy phone provenance.
    """
    for match in _PHONE_TOKEN.finditer(text):
        candidate = match.group()
        groups = re.findall(_PHONE_GROUP, candidate)
        separated = " " in candidate or "-" in candidate
        prefix = _phone_context_prefix(text, match.start())
        if _NON_PHONE_IDENTIFIER_LABEL.search(prefix):
            continue
        if (
            candidate.startswith("+")
            or "(" in candidate
            or (separated and len(groups) >= 3)
            or _PHONE_LABEL.search(prefix)
        ):
            yield match


def _identity_token_supported(
    view: ProfessionalDocumentView, value: str, evidence: list[EvidenceRef], *, phone: bool
) -> bool:
    pattern = _PHONE_TOKEN if phone else _EMAIL_TOKEN
    normalize = _digits if phone else str.lower
    expected = normalize(value.strip())
    if not expected:
        return False
    for ref in evidence:
        # Match complete canonical occurrences, not tokens manufactured by
        # cropping or by concatenating digits across words/punctuation.
        block = next(
            b for b in view.blocks if b.page == ref.page and b.block_index == ref.block_index
        )
        source = block.text.lower()
        quote = ref.quote.lower()
        locations = list(re.finditer(re.escape(quote), source))
        tokens = _phone_like_occurrences(source) if phone else pattern.finditer(source)
        token_matches = list(tokens)
        if locations and all(
            any(
                location.start() <= token.start()
                and token.end() <= location.end()
                and normalize(token.group()) == expected
                for token in token_matches
            )
            for location in locations
        ):
            return True
    return False


def _name_supported(
    view: ProfessionalDocumentView, value: str, evidence: list[EvidenceRef]
) -> bool:
    tokens = re.findall(r"[a-z0-9]+", _normalize_claim_text(value))
    material_tokens = [token for token in tokens if len(token) > 1]
    if not material_tokens:
        material_tokens = [_normalize_claim_text(value)]
    for ref in evidence:
        spans = _source_spans(view, ref)
        # Preserve identity's material-token attribution policy. Professional
        # negation rules must not read a synthetic-data disclaimer as a name
        # contradiction; canonical boundaries alone prevent cropped tokens.
        if spans and all(
            all(
                any(
                    start <= match.start() and match.end() <= end
                    for match in re.finditer(rf"(?<!\w){re.escape(token)}(?!\w)", context)
                )
                for token in material_tokens
            )
            for context, start, end in spans
        ):
            return True
    return False


def _require_identity_value_support(
    view: ProfessionalDocumentView, field_name: str, value: str, evidence: list[EvidenceRef]
) -> None:
    supported = (
        _name_supported(view, value, evidence)
        if field_name == "full_name"
        else _identity_token_supported(view, value, evidence, phone=field_name == "phone")
    )
    if supported:
        return
    raise EvidenceValidationError(
        "CLAIM_EVIDENCE_UNSUPPORTED",
        f"Identity field '{field_name}' is not supported by its own evidence quote.",
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
    if extraction.full_name is not None:
        _require_identity_value_support(
            view, "full_name", extraction.full_name.value, extraction.full_name.evidence
        )
    if extraction.email is not None:
        _require_identity_value_support(
            view, "email", extraction.email.value, extraction.email.evidence
        )
    if extraction.phone is not None:
        _require_identity_value_support(
            view, "phone", extraction.phone.value, extraction.phone.evidence
        )
