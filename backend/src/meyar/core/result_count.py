import re
from dataclasses import dataclass

from meyar.core.text import fold_az_ascii, normalize_azerbaijani_case

MIN_RESULT_LIMIT = 1
MAX_RESULT_LIMIT = 100
DEFAULT_RESULT_LIMIT = 20

_RESULT_COUNT_PATTERNS = (
    re.compile(r"(?i)\btop[ -]?(?P<count>\d{1,4})\b"),
    re.compile(
        r"(?i)\b(?:show|display|list|return)\s+(?:the\s+)?(?:top\s+)?"
        r"(?P<count>\d{1,4})\s+(?:best\s+)?candidates?\b"
    ),
    re.compile(
        r"(?i)\b(?P<count>\d{1,4})\s+(?:best\s+)?candidates?\s+"
        r"(?:show|display|list|return)\b"
    ),
    re.compile(
        r"(?i)\b(?P<count>\d{1,4})\s+(?:nefer\s+)?namized(?:i|e|ler|leri)?\s+"
        r"(?:goster|gosterin|qaytar)\b"
    ),
    re.compile(
        r"(?i)\b(?P<count>\d{1,4})\s+nefer(?:\s+namized)?\s+"
        r"(?:goster|gosterin|qaytar)\b"
    ),
)


@dataclass(frozen=True)
class ResultCountIntent:
    requested: int | None
    effective: int
    was_bounded: bool


def extract_result_count_intent(text: str) -> ResultCountIntent:
    normalized = fold_az_ascii(normalize_azerbaijani_case(text))
    values = {
        int(match.group("count"))
        for pattern in _RESULT_COUNT_PATTERNS
        for match in pattern.finditer(normalized)
    }
    if len(values) != 1:
        return ResultCountIntent(requested=None, effective=DEFAULT_RESULT_LIMIT, was_bounded=False)
    requested = values.pop()
    effective = min(max(requested, MIN_RESULT_LIMIT), MAX_RESULT_LIMIT)
    return ResultCountIntent(
        requested=requested,
        effective=effective,
        was_bounded=effective != requested,
    )


def is_result_count_only(text: str) -> bool:
    intent = extract_result_count_intent(text)
    if intent.requested is None:
        return False
    normalized = fold_az_ascii(normalize_azerbaijani_case(text))
    remainder = normalized
    for pattern in _RESULT_COUNT_PATTERNS:
        remainder = pattern.sub(" ", remainder)
    remainder = re.sub(r"[^a-z0-9]+", " ", remainder).strip()
    return not remainder
