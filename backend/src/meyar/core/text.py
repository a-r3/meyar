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
