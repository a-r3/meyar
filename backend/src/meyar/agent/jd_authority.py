"""Deterministic, server-owned authority for JD requirement occurrences.

The local model may classify a requirement and reference ``span_id``.  It
never chooses the text boundary that authorizes a scorable criterion: this
module segments the original HR text first and every later decision is made
against the complete resulting occurrence.
"""

import re

from meyar.agent.schemas import MAX_JD_REQUIREMENT_SPANS, RequirementSpan
from meyar.core.result_count import is_result_count_only
from meyar.core.text import fold_az_ascii, normalize_azerbaijani_case
from meyar.evaluation.normalization import SKILL_ALIASES
from meyar.schemas.criteria import find_prohibited_term

_OUTER_BOUNDARY_RE = re.compile(r"(?:\r?\n)+|(?<=[.!?;])\s+")
_BULLET_RE = re.compile(r"\s*(?:[-*•]|\d+[.)])\s*")
_SAFE_CLAUSE_SEPARATOR_RE = re.compile(r"\s*(?:,|\band\b|\bvə\b)\s*", re.IGNORECASE)
_REVIEWABLE_LANGUAGE_RE = re.compile(
    r"^(?:a1|a2|b1|b2|c1|c2)\s+(?:english|ingilis(?:\s+dili)?|russian|rus(?:\s+dili)?)$",
    re.IGNORECASE,
)

_REQUIRED_RE = re.compile(
    r"\b(?:required|must|mandatory|minimum|teleb\w*|mutleq\w*|vacib\w*|"
    r"olmalidir|olmalidi|lazimdir|[a-z]+m[ae]lidir)\b",
    re.IGNORECASE,
)
_PREFERRED_RE = re.compile(
    r"\b(?:preferred|nice\s+to\s+have|ustunluk\w*|arzuolunan\w*)\b",
    re.IGNORECASE,
)
_PLUS_PREFERENCE_RE = re.compile(r"^(?P<subject>[a-z0-9+#.]+(?:\s+[a-z0-9+#.]+)*)\s+is\s+a\s+plus$")
_NEGATED_REQUIREMENT_RE = re.compile(
    r"\b(?:no|not|without)\b[^.!?;]{0,80}\b(?:requirement|required|must|preferred)\b",
    re.IGNORECASE,
)
_MATERIAL_RE = re.compile(
    r"\b(?:required|must|mandatory|minimum|preferred|nice\s+to\s+have|"
    r"experience|years?|yrs?|proficiency|level|degree|certificat\w*|language|"
    r"license|licence|willing|available|teleb\w*|mutleq\w*|vacib\w*|"
    r"ustunluk\w*|arzuolunan\w*|tecrube\w*|\bil\b|dil\w*|sertifikat\w*|"
    r"bakalavr\w*|magistr\w*|olmalidir|lazimdir|[a-z]+m[ae]lidir|"
    r"is\s+(?:a\s+)?plus)\b|"
    r"\b(?:a1|a2|b1|b2|c1|c2)\b",
    re.IGNORECASE,
)

# A deliberately bounded, reviewable taxonomy used only to recognize the
# English "<professional subject> is a plus" idiom and standalone implicit
# items. It does not add evaluator semantics: implicit items still become
# NEEDS_HUMAN_REVIEW. Curated aliases share the scorer's existing authority.
_KNOWN_PROFESSIONAL_SUBJECTS = frozenset(SKILL_ALIASES) | frozenset(
    SKILL_ALIASES.values()
) | frozenset(
    {
        ".net",
        "acams",
        "angular",
        "aws",
        "azure",
        "c",
        "c#",
        "c++",
        "django",
        "docker",
        "english",
        "excel",
        "fastapi",
        "git",
        "java",
        "javascript",
        "kubernetes",
        "linux",
        "node.js",
        "nodejs",
        "oracle",
        "power bi",
        "project management",
        "python",
        "react",
        "russian",
        "sap",
        "sql",
        "tableau",
        "typescript",
    }
)


def normalize_requirement_text(text: str) -> str:
    return " ".join(fold_az_ascii(normalize_azerbaijani_case(text)).split())


def _recognized_professional_subject(text: str) -> bool:
    return normalize_requirement_text(text).strip(" .;,:") in _KNOWN_PROFESSIONAL_SUBJECTS


def _is_reviewable_implicit_clause(text: str) -> bool:
    """A bounded subject+level shape may be split without inventing modality.

    The clause remains NEEDS_HUMAN_REVIEW until HR chooses required/preferred;
    this only gives the canonical subject and CEFR level their own occurrence.
    """
    return bool(_REVIEWABLE_LANGUAGE_RE.fullmatch(normalize_requirement_text(text)))


def explicit_modality(text: str) -> str | None:
    """Return one unambiguous source modality, otherwise fail closed."""
    normalized = normalize_requirement_text(text)
    if _NEGATED_REQUIREMENT_RE.search(normalized):
        return None
    required = bool(_REQUIRED_RE.search(normalized))
    plus_match = _PLUS_PREFERENCE_RE.fullmatch(normalized.strip(" .;,"))
    supported_plus = bool(
        plus_match and _recognized_professional_subject(plus_match.group("subject"))
    )
    preferred = bool(_PREFERRED_RE.search(normalized)) or supported_plus
    if required == preferred:
        return None
    return "MUST_HAVE" if required else "PREFERRED"


def _trimmed_offsets(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    bullet = _BULLET_RE.match(text, start, end)
    if bullet:
        start = bullet.end()
    while end > start and (text[end - 1].isspace() or text[end - 1] in ".;,"):
        end -= 1
    return start, end


def _base_occurrences(jd_text: str) -> list[tuple[int, int, bool]]:
    occurrences: list[tuple[int, int, bool]] = []
    cursor = 0
    for boundary in _OUTER_BOUNDARY_RE.finditer(jd_text):
        end = boundary.start()
        if boundary.group(0).lstrip().startswith((".", "!", "?", ";")):
            end += 1
        is_bullet = bool(_BULLET_RE.match(jd_text, cursor, end))
        start, end = _trimmed_offsets(jd_text, cursor, end)
        if start < end:
            occurrences.append((start, end, is_bullet))
        cursor = boundary.end()
    is_bullet = bool(_BULLET_RE.match(jd_text, cursor, len(jd_text)))
    start, end = _trimmed_offsets(jd_text, cursor, len(jd_text))
    if start < end:
        occurrences.append((start, end, is_bullet))
    return occurrences


def _split_safe_clauses(jd_text: str, start: int, end: int) -> list[tuple[int, int, bool]]:
    text = jd_text[start:end]
    separators = list(_SAFE_CLAUSE_SEPARATOR_RE.finditer(text))
    if not separators:
        return [(start, end, False)]

    pieces: list[tuple[int, int]] = []
    cursor = 0
    for separator in separators:
        piece_start, piece_end = _trimmed_offsets(
            jd_text, start + cursor, start + separator.start()
        )
        if piece_start < piece_end:
            pieces.append((piece_start, piece_end))
        cursor = separator.end()
    piece_start, piece_end = _trimmed_offsets(jd_text, start + cursor, end)
    if piece_start < piece_end:
        pieces.append((piece_start, piece_end))

    if len(pieces) > 1 and all(
        explicit_modality(jd_text[a:b])
        or _is_reviewable_implicit_clause(jd_text[a:b])
        or is_result_count_only(jd_text[a:b])
        for a, b in pieces
    ):
        return [
            (a, b, False)
            for a, b in pieces
            if not is_result_count_only(jd_text[a:b])
        ]
    # A conjunction with shared or unclear modality is kept whole.  The
    # complete clause remains visible, but cannot become scorable.
    return [(start, end, True)]


def segment_requirement_spans(jd_text: str) -> list[RequirementSpan]:
    """Create stable per-operation identities for material JD occurrences."""
    candidates: list[tuple[int, int, bool]] = []
    for start, end, is_bullet in _base_occurrences(jd_text):
        text = jd_text[start:end]
        if is_result_count_only(text):
            continue
        normalized = normalize_requirement_text(text)
        if not (
            is_bullet
            or _MATERIAL_RE.search(normalized)
            or _recognized_professional_subject(normalized)
            or find_prohibited_term(text)
        ):
            continue
        candidates.extend(_split_safe_clauses(jd_text, start, end))

    if len(candidates) > MAX_JD_REQUIREMENT_SPANS:
        overflow = candidates[MAX_JD_REQUIREMENT_SPANS - 1 :]
        candidates = [
            *candidates[: MAX_JD_REQUIREMENT_SPANS - 1],
            (overflow[0][0], overflow[-1][1], True),
        ]

    spans: list[RequirementSpan] = []
    for occurrence, (start, end, needs_review) in enumerate(candidates, start=1):
        exact = jd_text[start:end]
        spans.append(
            RequirementSpan(
                span_id=f"req-{occurrence:04d}",
                start_offset=start,
                end_offset=end,
                text=exact,
                normalized=normalize_requirement_text(exact),
                segmentation_needs_review=needs_review,
            )
        )
    return spans
