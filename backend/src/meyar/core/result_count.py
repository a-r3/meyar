import re
from dataclasses import dataclass
from enum import StrEnum

from meyar.core.text import fold_az_ascii, normalize_azerbaijani_case

MIN_RESULT_LIMIT = 1
MAX_RESULT_LIMIT = 100
DEFAULT_RESULT_LIMIT = 20

_NUMBER_WORD = (
    r"bir|iki|uc|dord|bes|alti|yeddi|sekkiz|doqquz|on|"
    r"one|two|three|four|five|six|seven|eight|nine|ten"
)
_COUNT = rf"(?P<count>\d{{1,4}}|{_NUMBER_WORD})"
_AZ_RESULT_ENTITY = r"(?:nefer(?:i)?(?:\s+namized\w*)?|namized\w*|netice\w*)"
_AZ_ACTION = r"(?:goster\w*|qaytar\w*|cixart\w*|tap\w*)"
_EN_RESULT_ENTITY = r"(?:candidates?|applicants?|results?|profiles?)"
_EN_ACTION = r"(?:show|display|list|return|find)"

# Each match is the complete source-attributable workflow-control occurrence,
# not merely the number token. Folding preserves supported AZ/EN offsets.
_RESULT_COUNT_PATTERNS = (
    re.compile(
        rf"(?i)\b(?:en\s+(?:cox|uygun|yaxsi)\s+|maksimum\s+)?{_COUNT}\s+"
        rf"{_AZ_RESULT_ENTITY}(?:\s+{_AZ_ACTION})?\b"
    ),
    re.compile(
        rf"(?i)\b{_AZ_ACTION}\s+(?:en\s+(?:cox|uygun|yaxsi)\s+|maksimum\s+)?"
        rf"{_COUNT}\s+{_AZ_RESULT_ENTITY}\b"
    ),
    re.compile(
        rf"(?i)\b(?:netice\w*\s+(?:sayi\s+)?)?(?:en\s+cox\s+|maksimum\s+)"
        rf"{_COUNT}(?:\s+{_AZ_RESULT_ENTITY})?(?:\s+{_AZ_ACTION}|\s+olsun)?\b"
    ),
    re.compile(rf"(?i)\btop[ -]?{_COUNT}(?:\s+{_EN_RESULT_ENTITY})?\b"),
    re.compile(
        rf"(?i)\b{_EN_ACTION}\s+(?:up\s+to\s+|at\s+most\s+|maximum\s+)?"
        rf"(?:the\s+)?(?:best\s+|top\s+)?{_COUNT}(?:\s+(?:best\s+)?{_EN_RESULT_ENTITY})?\b"
    ),
    re.compile(
        rf"(?i)\b(?:up\s+to\s+|at\s+most\s+|maximum\s+)?{_COUNT}\s+"
        rf"(?:best\s+)?{_EN_RESULT_ENTITY}(?:\s+{_EN_ACTION})?\b"
    ),
    re.compile(
        rf"(?i)\b(?:namized\w*|netice\w*)\s+sayi\s+{_COUNT}(?:\s+olsun)?\b"
    ),
)
_AMBIGUOUS_RESULT_CUE_RE = re.compile(
    rf"(?i)\b(?:texminen|bir\s+nece|bir\s+qeder|about|approximately)\s+"
    rf"(?:\d{{1,4}}\s+)?(?:{_AZ_RESULT_ENTITY}|{_EN_RESULT_ENTITY})\b"
)

_NUMBER_VALUES = {
    "bir": 1, "iki": 2, "uc": 3, "dord": 4, "bes": 5,
    "alti": 6, "yeddi": 7, "sekkiz": 8, "doqquz": 9, "on": 10,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


class ResultCountState(StrEnum):
    ABSENT = "ABSENT"
    VALID = "VALID"
    AMBIGUOUS = "AMBIGUOUS"
    OUT_OF_RANGE = "OUT_OF_RANGE"


@dataclass(frozen=True)
class ResultControlSpan:
    start_offset: int
    end_offset: int
    text: str
    value: int


@dataclass(frozen=True)
class ResultCountIntent:
    requested: int | None
    effective: int
    was_bounded: bool
    state: ResultCountState = ResultCountState.ABSENT
    control_spans: tuple[ResultControlSpan, ...] = ()


def _value(token: str) -> int:
    return _NUMBER_VALUES.get(token, int(token) if token.isdigit() else 0)


def extract_result_count_intent(text: str) -> ResultCountIntent:
    normalized = fold_az_ascii(normalize_azerbaijani_case(text))
    matches: dict[tuple[int, int], ResultControlSpan] = {}
    for pattern in _RESULT_COUNT_PATTERNS:
        for match in pattern.finditer(normalized):
            value = _value(match.group("count").casefold())
            matches[(match.start(), match.end())] = ResultControlSpan(
                match.start(), match.end(), text[match.start() : match.end()], value
            )
    candidates = sorted(
        matches.values(),
        key=lambda item: (item.start_offset, -(item.end_offset - item.start_offset)),
    )
    non_overlapping: list[ResultControlSpan] = []
    for candidate in candidates:
        if any(
            existing.start_offset <= candidate.start_offset
            and candidate.end_offset <= existing.end_offset
            and existing.value == candidate.value
            for existing in non_overlapping
        ):
            continue
        non_overlapping.append(candidate)
    spans = tuple(sorted(non_overlapping, key=lambda item: item.start_offset))
    if not spans:
        ambiguous = _AMBIGUOUS_RESULT_CUE_RE.search(normalized)
        if ambiguous:
            return ResultCountIntent(
                requested=None,
                effective=MIN_RESULT_LIMIT,
                was_bounded=False,
                state=ResultCountState.AMBIGUOUS,
                control_spans=(
                    ResultControlSpan(
                        ambiguous.start(),
                        ambiguous.end(),
                        text[ambiguous.start() : ambiguous.end()],
                        0,
                    ),
                ),
            )
        return ResultCountIntent(
            requested=None,
            effective=DEFAULT_RESULT_LIMIT,
            was_bounded=False,
            state=ResultCountState.ABSENT,
        )
    values = {span.value for span in spans}
    if len(values) != 1:
        # An unresolved explicit count is never represented as the default.
        # The caller blocks confirmation and asks HR to clarify this sentinel.
        return ResultCountIntent(
            requested=None,
            effective=MIN_RESULT_LIMIT,
            was_bounded=False,
            state=ResultCountState.AMBIGUOUS,
            control_spans=spans,
        )
    requested = values.pop()
    effective = min(max(requested, MIN_RESULT_LIMIT), MAX_RESULT_LIMIT)
    return ResultCountIntent(
        requested=requested,
        effective=effective,
        was_bounded=effective != requested,
        state=(
            ResultCountState.VALID
            if MIN_RESULT_LIMIT <= requested <= MAX_RESULT_LIMIT
            else ResultCountState.OUT_OF_RANGE
        ),
        control_spans=spans,
    )


def is_result_count_only(text: str) -> bool:
    intent = extract_result_count_intent(text)
    if not intent.control_spans:
        return False
    chars = list(fold_az_ascii(normalize_azerbaijani_case(text)))
    for span in intent.control_spans:
        for index in range(span.start_offset, span.end_offset):
            chars[index] = " "
    remainder = re.sub(r"[^a-z0-9]+", " ", "".join(chars)).strip()
    return not remainder
