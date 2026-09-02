import re

from meyar.core.text import fold_az_ascii, normalize_azerbaijani_case

_WHITESPACE_RE = re.compile(r"\s+")
_YEAR_RE = re.compile(r"(19|20)\d{2}")


def _normalize(text: str) -> str:
    """Same casefold/diacritic-fold discipline as
    meyar.core.domain_terms._normalize (D-023 typing-variance matching) —
    kept consistent here so an AZ-typed skill/domain term and its
    plain-Latin-keyboard variant compare equal in both modules."""
    return _WHITESPACE_RE.sub(" ", fold_az_ascii(normalize_azerbaijani_case(text))).strip()


def _year_token(date_text: str | None) -> str | None:
    if not date_text:
        return None
    match = _YEAR_RE.search(date_text)
    return match.group(0) if match else None


def interval_grounded_in_quotes(
    *,
    start_date: str | None,
    end_date: str | None,
    quotes: list[str],
    subject_terms: frozenset[str] | None = None,
) -> bool:
    """Deterministic RELATIONAL grounding guard (issue #32 final review).

    Two earlier, weaker checks are not enough: (a) that a quote is real/
    verbatim, and (b) that the subject and the claimed years each appear
    SOMEWHERE across the item's evidence list. Citing quote A ("Java ilə
    işləyib.") and quote B ("2020–2025 — Data Analyst.") together must
    NOT prove "Java 2020-2025" — the subject and the interval were never
    actually tied together in the source text.

    This function instead requires ONE evidence quote — a single
    "accepted evidence span" — that contains BOTH the subject (any term
    in `subject_terms`, when given) AND every year the item claims
    (`start_date`/`end_date`, each reduced to its 4-digit year). Different
    quotes are never combined to satisfy different parts of the claim.

    `subject_terms`, when given, is the set of acceptable literal terms
    for the subject: a skill's own name, or a domain's curated synonym
    set (meyar.core.domain_terms.accepted_terms_for_domain) — the same
    matching discipline `domain_term_present` already uses, reused here
    so this relational check recognizes the identical synonym forms.

    A bound left unset (None — e.g. an `is_current` claim's `end_date`)
    contributes nothing to require. If NEITHER bound is claimed, this
    returns True trivially: there is no interval to relationally ground
    (the caller/evaluator already treats a fully-unset interval as
    insufficient evidence on its own — see meyar.evaluation.evaluators)."""
    required_years: list[str] = []
    for date_text in (start_date, end_date):
        if date_text is None:
            continue
        year = _year_token(date_text)
        if year is None:
            return False
        required_years.append(year)
    if not required_years:
        return True

    normalized_subject_terms = (
        {_normalize(term) for term in subject_terms if term} if subject_terms else None
    )

    for quote in quotes:
        normalized_quote = _normalize(quote)
        if normalized_subject_terms and not any(
            re.search(rf"(?<!\w){re.escape(term)}(?!\w)", normalized_quote)
            for term in normalized_subject_terms
        ):
            continue
        if all(year in normalized_quote for year in required_years):
            return True
    return False
