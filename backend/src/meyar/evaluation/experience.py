import re
from datetime import date

_YEAR_RE = re.compile(r"(19|20)\d{2}")
_PRESENT_RE = re.compile(r"present|current|now|ongoing", re.IGNORECASE)


def parse_year(
    text: str | None, *, evaluation_as_of_date: date, is_current: bool = False
) -> int | None:
    """Deterministically extracts a 4-digit year from free-text date
    strings ("2021", "Jan 2020", "2019 - Present"). Returns None when no
    year can be reliably identified — callers must treat that as
    ambiguous, never guess. "Present"/"current"/"now" resolve only to
    the explicitly supplied evaluation date's year, never the wall clock."""
    if is_current:
        return evaluation_as_of_date.year
    if not text:
        return None
    if _PRESENT_RE.search(text):
        return evaluation_as_of_date.year
    match = _YEAR_RE.search(text)
    return int(match.group(0)) if match else None


def ranges_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]
