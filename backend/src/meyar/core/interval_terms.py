import re

_WHITESPACE_RE = re.compile(r"\s+")
_YEAR_RE = re.compile(r"(19|20)\d{2}")


def _normalize(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


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
    subject: str | None = None,
) -> bool:
    """Deterministic guard (issue #32 final review): a valid, verbatim
    evidence quote proves the SKILL/DOMAIN was mentioned — it does not by
    itself prove the specific start_date/end_date a model claims for that
    item. This checks that the SAME year the model claims for each stated
    bound literally appears somewhere in that item's own cited quotes, so
    a quote that only proves "Java was used" (with no date at all, or
    only a narrower/different date) can never ground an arbitrary,
    broader interval such as an entire linked employment period.

    A bound left unset (None — e.g. `is_current=True` with no end_date)
    trivially requires nothing; the caller/evaluator already treats a
    fully-unset interval as insufficient evidence on its own (see
    meyar.evaluation.evaluators). This function only judges bounds that
    ARE claimed.

    `subject`, when given (skill_name — never used for domain, which
    already has its own synonym-aware meyar.core.domain_terms check),
    additionally requires that literal term to appear in the same quotes
    — so a quote that only states a job's own dates, without ever
    mentioning the skill, cannot ground that skill's interval either.

    This is a token-presence heuristic, not semantic verification — no
    LLM verifier is used (issue #32 explicitly forbids one). It reliably
    catches an interval whose claimed year(s) are simply absent from the
    cited text; it cannot catch a quote deceptively engineered to contain
    the right years/subject without actually supporting them together —
    an accepted, documented limit of a purely deterministic check."""
    haystack = _normalize(" ".join(quotes)).casefold()
    if subject:
        subject_normalized = _normalize(subject).casefold()
        if subject_normalized and not re.search(
            rf"(?<!\w){re.escape(subject_normalized)}(?!\w)", haystack
        ):
            return False
    for date_text in (start_date, end_date):
        if date_text is None:
            continue
        year = _year_token(date_text)
        if year is None or year not in haystack:
            return False
    return True
