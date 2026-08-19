import re

_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Any digit-ish run of 9+ chars, optionally starting with '+'. Deliberately
# broad at the candidate-match stage — _looks_like_phone below filters out
# short date ranges (e.g. "2021-2025") so employment dates survive intact.
_PHONE_CANDIDATE_PATTERN = re.compile(r"(?<!\d)(\+?\d[\d\-\s().]{6,}\d)(?!\d)")

_YEAR_RANGE_PATTERN = re.compile(
    r"^(19|20)\d{2}\s*[-–—/]\s*((19|20)\d{2}|present|current|now|ongoing)$",
    re.IGNORECASE,
)

_LABELED_FIELD_PATTERN = re.compile(
    r"^\s*(date of birth|d\.?\s?o\.?\s?b\.?|birth\s?date|gender|sex|"
    r"marital status|religion)\s*[:\-]\s*.*$",
    re.IGNORECASE | re.MULTILINE,
)


def _looks_like_phone(candidate: str) -> bool:
    stripped = candidate.strip()
    if _YEAR_RANGE_PATTERN.match(stripped):
        return False
    digit_count = sum(1 for ch in candidate if ch.isdigit())
    return digit_count >= 9


def _redact_phones(text: str) -> str:
    def _sub(match: re.Match[str]) -> str:
        return "[REDACTED_PHONE]" if _looks_like_phone(match.group(0)) else match.group(0)

    return _PHONE_CANDIDATE_PATTERN.sub(_sub, text)


def redact_block_text(text: str) -> str:
    """Deterministic, conservative pre-LLM redaction. Removes email
    addresses, phone-number-shaped strings, and explicitly labeled
    DOB/gender/marital-status/religion lines. Does NOT touch employment
    dates or attempt to infer/redact names or protected characteristics —
    see docs/DECISIONS.md and docs/SECURITY_PRIVACY.md for the documented
    scope/limitation of this pass."""
    text = _EMAIL_PATTERN.sub("[REDACTED_EMAIL]", text)
    text = _redact_phones(text)
    text = _LABELED_FIELD_PATTERN.sub("[REDACTED_FIELD]", text)
    return text
