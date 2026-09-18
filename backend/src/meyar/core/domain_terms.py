import re

from meyar.core.text import fold_az_ascii, normalize_azerbaijani_case

_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Casefolds and diacritic-folds so "bankçılıq" and "bankcilik" (a
    plain-Latin-keyboard typing variant, D-023) compare equal — same
    typing-variance discipline as the search planner's own matching."""
    return _WHITESPACE_RE.sub(" ", fold_az_ascii(normalize_azerbaijani_case(text))).strip()


# Deterministic, explicitly curated sector/domain synonym sets — same
# discipline as meyar.evaluation.normalization.SKILL_ALIASES. Every entry is
# an unambiguous domain/sector DESCRIPTOR (a phrase or acronym that only
# means the domain itself), deliberately never a bare word that commonly
# appears inside an unrelated proper noun (e.g. a company name containing
# "Bank"). This is the deterministic guard behind issue #32's requirement:
# domain/sector experience must never be inferred merely from an opaque
# employer name — only from text that actually states the sector. Extend
# this table (not the extraction prompt's judgment, not fuzzy matching) to
# add a new recognized domain.
DOMAIN_SYNONYMS: dict[str, frozenset[str]] = {
    "banking": frozenset(
        {
            "banking",
            "banking sector",
            "banking industry",
            "retail banking",
            "corporate banking",
            "commercial banking",
            "investment banking",
            "bank sektoru",
            "bankcilik",
            "bankcilik sektoru",
        }
    ),
    "aml": frozenset(
        {
            "aml",
            "anti-money laundering",
            "anti money laundering",
            "kyc",
            "pulun yuyulmasina qarsi",
            "pulun yuyulmasinin qarsisinin alinmasi",
        }
    ),
    "finance": frozenset(
        {
            "finance",
            "financial sector",
            "finance sector",
            "maliyye",
            "maliyye sahesi",
            "maliyye sektoru",
        }
    ),
}


def canonicalize_domain(text: str) -> str:
    """Normalizes a domain label (a criterion's configured value, or an
    extraction's own `domain` field) into a comparison key. Resolves to a
    curated canonical key when `text` (or one of its synonyms) matches an
    entry in DOMAIN_SYNONYMS; otherwise returns the plain normalized text
    unchanged, so an explicit sector not yet in the curated map can still
    round-trip and match itself exactly."""
    normalized = _normalize(text)
    for canonical, synonyms in DOMAIN_SYNONYMS.items():
        if normalized == canonical or normalized in synonyms:
            return canonical
    return normalized


def accepted_terms_for_domain(domain: str) -> frozenset[str]:
    """The set of literal terms that count as explicit evidence for
    `domain` — its curated synonyms when it resolves to a known canonical
    domain, else just its own normalized text. Shared by
    `domain_term_present` (presence-anywhere-in-evidence check) and
    `meyar.core.interval_terms` (same-quote relational check) so both
    recognize identical accepted forms."""
    canonical = canonicalize_domain(domain)
    return DOMAIN_SYNONYMS.get(canonical, frozenset({canonical}))


def domain_term_present(domain: str, quotes: list[str]) -> bool:
    """True only when an explicit, unambiguous domain/sector term for
    `domain` literally appears (as a whole word/phrase, never a bare
    substring) in the joined evidence quotes. A company name alone (e.g.
    "ABC Solutions MMC") contains none of DOMAIN_SYNONYMS' curated
    phrases, so it can never satisfy this check by itself — see
    docs/DECISIONS.md."""
    accepted_terms = accepted_terms_for_domain(domain)
    haystack = _normalize(" ".join(quotes))
    return any(
        re.search(rf"(?<!\w){re.escape(term)}(?!\w)", haystack) for term in accepted_terms if term
    )
