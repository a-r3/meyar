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


def merge_and_sum_years(ranges: list[tuple[int, int]]) -> float:
    """Merges overlapping/adjacent [start, end) year ranges and returns the
    total non-overlapping span. Used for skill/domain duration aggregation
    (issue #32), where multiple attributable periods for the same skill
    may legitimately overlap (e.g. a full-time job and a concurrent
    freelance project both using the same skill) and must not be
    double-counted — deliberately different from evaluate_experience's
    total-career EXPERIENCE evaluator, which still flags any overlap as
    CONFLICTING_EVIDENCE (frozen, unchanged; see docs/DECISIONS.md)."""
    if not ranges:
        return 0.0
    ordered = sorted(ranges)
    merged: list[list[int]] = [list(ordered[0])]
    for start, end in ordered[1:]:
        last = merged[-1]
        if start <= last[1]:
            last[1] = max(last[1], end)
        else:
            merged.append([start, end])
    return float(sum(max(end - start, 0) for start, end in merged))
