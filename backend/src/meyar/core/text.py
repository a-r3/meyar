import re
import unicodedata

_AZERBAIJANI_CASE_TRANSLATION = str.maketrans("İI", "iı")


def normalize_azerbaijani_case(text: str) -> str:
    """Return deterministic NFC text with Azerbaijani-aware casing.

    Azerbaijani distinguishes dotted ``İ/i`` from dotless ``I/ı``. Translate
    the uppercase forms before Unicode casefold so the distinction survives
    without relying on a process-global locale.
    """
    normalized = unicodedata.normalize("NFC", text)
    lowered = normalized.translate(_AZERBAIJANI_CASE_TRANSLATION).casefold()
    return unicodedata.normalize("NFC", lowered)


# HR staff routinely type Azerbaijani text on a plain Latin keyboard without
# the dedicated diacritic keys, substituting the nearest ASCII letter (e.g.
# "tecrübə" for "təcrübə"). Shared by the search planner's typing-variance
# matching and by ASCII-only identifier generation (e.g. criterion id
# slugs) — see docs/DECISIONS.md D-023.
_AZ_ASCII_FOLD = str.maketrans(
    {
        "ə": "e", "Ə": "E",
        "ç": "c", "Ç": "C",
        "ş": "s", "Ş": "S",
        "ö": "o", "Ö": "O",
        "ü": "u", "Ü": "U",
        "ğ": "g", "Ğ": "G",
        "ı": "i", "İ": "i",
    }
)


def fold_az_ascii(text: str) -> str:
    """Fold Azerbaijani diacritics to their nearest ASCII base letter."""
    return unicodedata.normalize("NFC", text).translate(_AZ_ASCII_FOLD)


_SLUG_FALLBACK = "meyar"


def combine_degree_and_field(degree: str | None, field_of_study: str | None) -> str | None:
    """Join an education item's degree and field of study for display,
    without repeating field_of_study when it is already part of degree
    (e.g. degree="BSc Data Science", field_of_study="Data Science" must
    render as "BSc Data Science", never "BSc Data Science Data Science").
    Shared by every presentation surface that renders an education title —
    candidate detail, the agent's profile/evidence tool results, and
    grounded-answer synthesis (D-038) — so a fix here fixes all of them at
    once (PR #42 owner correction, issue #33). Comparison is diacritic/
    case-fold-insensitive so a typing-variance duplicate is still caught."""
    degree = (degree or "").strip() or None
    field_of_study = (field_of_study or "").strip() or None
    if degree and field_of_study:
        if fold_az_ascii(normalize_azerbaijani_case(field_of_study)) in fold_az_ascii(
            normalize_azerbaijani_case(degree)
        ):
            return degree
        return f"{degree} {field_of_study}"
    return degree or field_of_study


def slugify_criterion_label(label: str, used_ids: set[str]) -> str:
    """A stable, ASCII-only, collision-free id derived from free-text HR
    (or agent-drafted) requirement wording — shared by the manual
    vacancy-creation form (meyar.ui.service) and the agent's JD-drafted
    criteria (meyar.agent.service) so both paths mint ids the exact same
    way. Never shown to or typed by an HR user — only the deterministic
    policy engine's internal join key. Mutates ``used_ids`` to reserve the
    returned id against a later collision in the same batch."""
    ascii_text = fold_az_ascii(label).lower()
    base = re.sub(r"[^a-z0-9]+", "_", ascii_text).strip("_")[:60] or _SLUG_FALLBACK
    candidate = base
    suffix = 2
    while candidate in used_ids:
        candidate = f"{base}_{suffix}"[:64]
        suffix += 1
    used_ids.add(candidate)
    return candidate
