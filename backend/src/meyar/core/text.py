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
