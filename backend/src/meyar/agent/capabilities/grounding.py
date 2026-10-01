"""Source grounding of model plan inputs (issue #88 slice C, D-092 §10.2).

Pure functions only: no database, no model, no I/O. The model never writes
executable text or numbers; it only POINTS at the user's own canonical
LF-normalized current message ``M`` by exact quotation. Everything that
reaches an executor is resolved here by the server:

* ``resolve_quote`` — a quote must occur EXACTLY ONCE in ``M`` (byte-exact
  after LF canonicalization; no case or diacritic folding, overlapping
  occurrences counted). The server computes offsets and a SHA-256 per span.
* ``resolve_selection`` — WHOLE_MESSAGE resolves to ``M`` exactly; QUOTES
  resolve to non-blank, non-overlapping spans in source order joined by a
  server-owned ``"\\n"``, so no model character enters the planner input.
* ``uncovered_requirement`` — every material requirement of
  ``analyze_hr_text(M)`` (SCORABLE / NEEDS_HUMAN_REVIEW) must overlap some
  grounded span of the plan, per coordinated part of its subject
  (SOURCE_COVERAGE_INCOMPLETE).
* ``has_protected_content`` — PROHIBITED requirement / PROTECTED_CUE /
  denylist term anywhere in ``M`` forbids QUOTES for search/refine
  (SOURCE_SELECTION_FORBIDDEN), so a clean fragment can never launder a
  protected-attribute request past the frozen planner's refusal.
* ``parse_count_quote`` / ``parse_ordinal_quote`` — CLOSED numeric
  vocabularies (§3.1 safety-relevant category, not the frozen intent-regex
  layer). Anything outside them fails closed (REFERENCE_NOT_GROUNDED); there
  is never a model-supplied fallback integer."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from meyar.agent.schemas import MAX_CANDIDATE_REF, SemanticRequirementState, SourceSpanRole
from meyar.agent.semantic_requirements import SemanticAnalysis, count_token_value
from meyar.core.text import fold_az_ascii, normalize_azerbaijani_case
from meyar.schemas.criteria import find_prohibited_term

MAX_QUOTES_PER_SELECTION = 4
MAX_QUOTE_LENGTH = 500
# Server-owned separator between grounded spans of one selection.
SPAN_SEPARATOR = "\n"

_MATERIAL_STATES = frozenset(
    {SemanticRequirementState.SCORABLE, SemanticRequirementState.NEEDS_HUMAN_REVIEW}
)


@dataclass(frozen=True)
class GroundedSpan:
    """Server-computed offsets into ``M`` plus the span's SHA-256. Audited
    and hashed as offsets/hashes only — never the text."""

    start: int
    end: int
    sha256: str

    def overlaps(self, start: int, end: int) -> bool:
        return self.start < end and start < self.end


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def span_of(source: str, start: int, end: int) -> GroundedSpan:
    return GroundedSpan(start=start, end=end, sha256=_sha256(source[start:end]))


def _canonical_lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def resolve_quote(quote: str, source: str) -> GroundedSpan | None:
    """The unique exact occurrence of ``quote`` in ``source``, else None."""
    quote = _canonical_lf(quote)
    if not quote.strip() or len(quote) > MAX_QUOTE_LENGTH:
        return None
    first = source.find(quote)
    if first < 0 or source.find(quote, first + 1) >= 0:
        # Zero or multiple (including overlapping) occurrences.
        return None
    return span_of(source, first, first + len(quote))


def resolve_whole_message(source: str) -> GroundedSpan | None:
    return span_of(source, 0, len(source)) if source.strip() else None


def resolve_quotes(quotes: list[str], source: str) -> tuple[GroundedSpan, ...] | None:
    """1..4 unique, non-overlapping spans in SOURCE order, else None."""
    if not 1 <= len(quotes) <= MAX_QUOTES_PER_SELECTION:
        return None
    spans: list[GroundedSpan] = []
    for quote in quotes:
        span = resolve_quote(quote, source)
        if span is None:
            return None
        spans.append(span)
    spans.sort(key=lambda span: span.start)
    for previous, current in zip(spans, spans[1:], strict=False):
        if current.start < previous.end:
            return None
    return tuple(spans)


def joined_text(spans: tuple[GroundedSpan, ...], source: str) -> str:
    """Planner input: exact source slices joined by the server separator."""
    return SPAN_SEPARATOR.join(source[span.start : span.end] for span in spans)


# ---------------------------------------------------------------------------
# Coverage and protected content
# ---------------------------------------------------------------------------


# Closed coordinators inside ONE analyzed subject occurrence ("Python və
# Java", "SQL/Oracle", "Python, Java"). Matched on the 1:1 ASCII-folded text so
# offsets stay exact source offsets.
_COORDINATOR_RE = re.compile(
    r"\s*(?:,|/|&|;|\b(?:ve|and|or|ya|yaxud|hemcinin|habele)\b)\s*"
)


def _conjuncts(source: str, start: int, end: int) -> list[tuple[int, int]]:
    """The coordinated parts of one subject occurrence (the whole occurrence
    when it has no coordinator)."""
    folded = _fold(source[start:end])
    if len(folded) != end - start:
        # Folding is expected to be 1:1; never guess offsets otherwise.
        return [(start, end)]
    parts: list[tuple[int, int]] = []
    cursor = 0
    for match in _COORDINATOR_RE.finditer(folded):
        if match.start() > cursor:
            parts.append((start + cursor, start + match.start()))
        cursor = match.end()
    if cursor < len(folded):
        parts.append((start + cursor, end))
    return [(left, right) for left, right in parts if source[left:right].strip()] or [
        (start, end)
    ]


def uncovered_requirement(
    analysis: SemanticAnalysis, spans: list[GroundedSpan], source: str
) -> bool:
    """True when some material requirement is not covered: its subject
    occurrence (or, with no subject slot, its whole requirement span) must
    overlap some grounded span. ``analyze_hr_text`` reports a coordinated
    subject ("Python və Java") as ONE occurrence, so the overlap rule is
    applied to EACH coordinated part — a quote of "Python" alone can never
    silently drop "Java" (D-092 §10.2 rule 3, §22 item 25). Strictly
    stronger than whole-occurrence overlap; never weaker."""
    requirement_spans = {span.span_id: span for span in analysis.spans}
    for requirement in analysis.requirements:
        if requirement.state not in _MATERIAL_STATES:
            continue
        if requirement.subject is not None:
            start, end = requirement.subject.start_offset, requirement.subject.end_offset
        else:
            owner = requirement_spans.get(requirement.requirement_span_id)
            if owner is None:
                return True
            start, end = owner.start_offset, owner.end_offset
        for left, right in _conjuncts(source, start, end):
            if not any(span.overlaps(left, right) for span in spans):
                return True
    return False


def protected_ranges(analysis: SemanticAnalysis) -> list[tuple[int, int]]:
    ranges = [
        (assignment.start_offset, assignment.end_offset)
        for assignment in analysis.role_assignments
        if assignment.role == SourceSpanRole.PROTECTED_CUE
    ]
    requirement_spans = {span.span_id: span for span in analysis.spans}
    for requirement in analysis.requirements:
        if requirement.state == SemanticRequirementState.PROHIBITED:
            owner = requirement_spans.get(requirement.requirement_span_id)
            if owner is not None:
                ranges.append((owner.start_offset, owner.end_offset))
    return ranges


def has_protected_content(analysis: SemanticAnalysis, message: str) -> bool:
    """PROHIBITED requirement, PROTECTED_CUE role, or a denylisted term
    anywhere in ``M`` (the last is defense in depth, never weaker)."""
    if any(
        requirement.state == SemanticRequirementState.PROHIBITED
        for requirement in analysis.requirements
    ):
        return True
    if any(
        assignment.role == SourceSpanRole.PROTECTED_CUE
        for assignment in analysis.role_assignments
    ):
        return True
    return find_prohibited_term(message) is not None


# ---------------------------------------------------------------------------
# Closed numeric vocabularies
# ---------------------------------------------------------------------------


def _fold(text: str) -> str:
    return fold_az_ascii(normalize_azerbaijani_case(text))


_TOKEN_RE = re.compile(r"#?\w+(?:-\w+)?")
# Same suffix set FORCE_RESULT_LIMIT strips from a count word ("ilk üçü").
_COUNT_WORD_SUFFIXES = ("", "u", "i", "ni", "nu", "si", "su", "ini", "unu")
# Closed AZ case suffixes after a hyphenated digit ("3-ə", "3-nü").
_DIGIT_CASE_SUFFIXES = frozenset(
    {"e", "a", "ye", "ya", "u", "i", "nu", "ni", "yu", "yi", "de", "da", "den", "dan",
     "nin", "nun", "in", "un"}
)
# A count followed by a duration unit is a duration, never a result count.
# (``pattern.match(text, pos)`` anchors at ``pos``; no ``^``.)
_DURATION_AFTER_RE = re.compile(
    r"\s*(?:\+\s*)?(?:years?|yrs?|months?|mos?|il(?:den|de|lik|ler)?|ay(?:dan|da|liq|lar)?)\b"
)


_VAGUE_AFTER_RE = re.compile(r"\s*nece\b")


def _count_token(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    if "-" in token:
        digits, _, suffix = token.partition("-")
        if digits.isdigit() and suffix in _DIGIT_CASE_SUFFIXES:
            return int(digits)
        return None
    for suffix in _COUNT_WORD_SUFFIXES:
        if suffix and not token.endswith(suffix):
            continue
        value = count_token_value(token[: len(token) - len(suffix)] if suffix else token)
        if value is not None:
            return value
    return None


def parse_count_quote(quote: str) -> int | None:
    """Exactly one count token (digit, ``N-<case>``, or a known count word
    with FORCE_RESULT_LIMIT's suffixes), not a duration, in 1..MAX."""
    folded = _fold(quote)
    values: list[int] = []
    for match in _TOKEN_RE.finditer(folded):
        value = _count_token(match.group(0))
        if value is None:
            continue
        if _DURATION_AFTER_RE.match(folded, match.end()) or _VAGUE_AFTER_RE.match(
            folded, match.end()
        ):
            # A duration ("3 il") or a vague quantifier ("bir neçə") is
            # never a result count: fail closed.
            return None
        values.append(value)
    if len(values) != 1 or not 1 <= values[0] <= MAX_CANDIDATE_REF:
        return None
    return values[0]


# Azerbaijani ordinal stems over the count words, ASCII-folded.
_AZ_ORDINAL_STEMS = {
    "birinci": 1,
    "ikinci": 2,
    "ucuncu": 3,
    "dorduncu": 4,
    "besinci": 5,
    "altinci": 6,
    "yeddinci": 7,
    "sekkizinci": 8,
    "doqquzuncu": 9,
    "onuncu": 10,
}
# Closed AZ case/possessive endings after an ordinal ("birincinin",
# "ikincisini", "üçüncüyə").
_AZ_ORDINAL_SUFFIXES = (
    "", "nin", "nun", "ni", "nu", "ye", "ya", "de", "da", "den", "dan", "ne", "na",
    "nde", "nda", "si", "su", "sinin", "sunun", "sini", "sunu", "sine", "suna",
    "sinde", "sunda", "sinden", "sundan", "i", "u", "e", "a",
)
_EN_ORDINAL_WORDS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
}
_HASH_RE = re.compile(r"^#(\d{1,2})$")
# "3-cü", "1-ci", "10-cu" (folded) with an optional closed case ending.
_AZ_DIGIT_ORDINAL_RE = re.compile(r"^(\d{1,2})-?c[iu](\w*)$")
_EN_DIGIT_ORDINAL_RE = re.compile(r"^(\d{1,2})(st|nd|rd|th)$")
_DIGIT_RE = re.compile(r"^(\d{1,2})(?:-(\w+))?$")


def _english_suffix(value: int) -> str:
    if 10 <= value % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")


def _ordinal_token(token: str) -> int | None:
    if match := _HASH_RE.match(token):
        return int(match.group(1))
    if match := _AZ_DIGIT_ORDINAL_RE.match(token):
        return int(match.group(1)) if match.group(2) in _AZ_ORDINAL_SUFFIXES else None
    if match := _EN_DIGIT_ORDINAL_RE.match(token):
        value = int(match.group(1))
        return value if match.group(2) == _english_suffix(value) else None
    if match := _DIGIT_RE.match(token):
        suffix = match.group(2)
        if suffix is None or suffix in _DIGIT_CASE_SUFFIXES:
            return int(match.group(1))
        return None
    if token in _EN_ORDINAL_WORDS:
        return _EN_ORDINAL_WORDS[token]
    for stem, value in _AZ_ORDINAL_STEMS.items():
        if token.startswith(stem) and token[len(stem):] in _AZ_ORDINAL_SUFFIXES:
            return value
    return None


def parse_ordinal_quote(quote: str) -> int | None:
    """Exactly one closed ordinal token in 1..MAX_CANDIDATE_REF, else None
    (e.g. "sonuncu" / "last" / a pronoun fail closed)."""
    folded = _fold(quote)
    values = [
        value
        for value in (_ordinal_token(match.group(0)) for match in _TOKEN_RE.finditer(folded))
        if value is not None
    ]
    if len(values) != 1 or not 1 <= values[0] <= MAX_CANDIDATE_REF:
        return None
    return values[0]
