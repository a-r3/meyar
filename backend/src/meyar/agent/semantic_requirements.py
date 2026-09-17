"""Source-bound Azerbaijani/English HR requirement interpretation.

This module is deliberately a grammatical/slot boundary rather than a
technology dictionary.  It recognizes bounded HR syntax (modality, duration,
proficiency and criterion-family wrappers), preserves the remaining source
occurrence as the professional subject, and applies curated aliases only after
that occurrence has been established.  Unknown safe professional subjects are
therefore preserved instead of rejected or guessed by the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from meyar.agent.schemas import (
    JDDraftCriterionKind,
    RequirementSpan,
    SemanticRequirement,
    SemanticRequirementState,
    SourceOccurrence,
    SupportedInputLanguage,
)
from meyar.core.domain_terms import DOMAIN_SYNONYMS, canonicalize_domain
from meyar.core.result_count import (
    ResultCountIntent,
    extract_result_count_intent,
    is_result_count_only,
)
from meyar.core.text import fold_az_ascii, normalize_azerbaijani_case
from meyar.evaluation.normalization import SKILL_ALIASES, normalize_skill_name
from meyar.schemas.criteria import CriterionType, find_prohibited_term

_CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")
_AZ_CHAR_RE = re.compile(r"[əƏçÇşŞöÖüÜğĞıİ]")
_AZ_WORD_RE = re.compile(
    r"\b(?:teleb|tecrube|namized|il|dil|uzre|vacib|lazim|ustunluk|sert)\b", re.I
)
_EN_MARKERS_RE = re.compile(
    r"\b(?:required|preferred|experience|years?|candidates?|show|find|degree|certification)\b",
    re.I,
)

_NUMBER_WORDS = {
    "bir": 1,
    "iki": 2,
    "uc": 3,
    "dord": 4,
    "bes": 5,
    "alti": 6,
    "yeddi": 7,
    "sekkiz": 8,
    "doqquz": 9,
    "on": 10,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_NUMBER_TOKEN = r"(?:\d+(?:[.,]\d+)?|" + "|".join(_NUMBER_WORDS) + r")"

_NEGATIVE_RE = re.compile(
    r"\b(?:not\s+(?:required|mandatory|needed)|no\s+.+?required|isn['’]?t\s+required|"
    r"need\s+not|without\s+requiring|lazim\s+deyil|teleb\s+olunmur|"
    r"vacib\s+deyil|mecburi\s+deyil)\b",
    re.I,
)
_PREFERRED_RE = re.compile(
    r"\b(?:preferred|optional|desirable|advantageous|nice\s+to\s+have|"
    r"considered\s+(?:an?\s+)?asset|is\s+(?:a\s+)?plus|plus(?:dir|\s+sayilir)?|"
    r"ustunluk(?:dur|du|\s+verilir|\s+sayilsin)?|elave\s+ustunluk|"
    r"arzuolunan(?:dir)?|arzu\s+edilir|olsa\s+yaxsi(?:dir|di)?|yaxsi\s+olar)\b",
    re.I,
)
_REQUIRED_RE = re.compile(
    r"\b(?:required|must(?:\s+have|\s+be)?|mandatory|is\s+required|"
    r"teleb\s+olunur|telebdir|mutleqdir|mutleq|mecburi(?:dir)?|sertdir|vacibdir|lazimdir|"
    r"olmalidir|olmalidi|olmasin|olsun|\w+m[ae]lidir)\b",
    re.I,
)
_MINIMUM_RE = re.compile(
    rf"\b(?:at\s+least|minimum|min\.?|en\s+azi)\s+(?P<number>{_NUMBER_TOKEN})\b|"
    rf"\b(?P<plus>{_NUMBER_TOKEN})\s*\+\s*(?:years?|yrs?|il)\b|"
    rf"\b(?P<not_less>{_NUMBER_TOKEN})\s*(?:years?|yrs?|il)(?:den)?\s+az\s+olmasin\b",
    re.I,
)
_STRICT_GREATER_RE = re.compile(
    rf"\b(?:more\s+than|over)\s+(?P<a>{_NUMBER_TOKEN})\s*(?:years?|yrs?)\b|"
    rf"\b(?P<b>{_NUMBER_TOKEN})\s*il(?:den)?\s+cox\b",
    re.I,
)
_DURATION_RE = re.compile(rf"\b(?P<number>{_NUMBER_TOKEN})\s*(?:\+\s*)?(?:years?|yrs?|il)\b", re.I)
_LEVEL_RE = re.compile(
    r"\b(?:a1|a2|b1|b2|c1|c2|beginner|elementary|intermediate|"
    r"upper[- ]intermediate|advanced|fluent|native|selis|ana\s+dili)\b",
    re.I,
)
_LANGUAGE_RE = re.compile(
    r"\b(?:english|ingilis(?:ce|\s+dili)?|russian|rus(?:ca|\s+dili)?|"
    r"azerbaijani|azerbaycan(?:ca|\s+dili)?|turkish|turk(?:ce|\s+dili)?|"
    r"german|deutsch|alman(?:ca|\s+dili)?)\b",
    re.I,
)
_LANGUAGE_WRAPPER_RE = re.compile(
    r"(?i)\b(?P<subject>[^,.;:\d]{2,60}?)\s+(?:language|dili)\b"
)
_CERT_RE = re.compile(r"\b(?:certificat(?:e|ion|ed)?s?|sertifikat\w*)\b", re.I)
_CERTIFICATION_NAMES = frozenset({"acca", "acams", "cfa", "cia", "cisa", "pmp"})
_EDUCATION_RE = re.compile(
    r"\b(?:education|degree|bachelor(?:['’]s)?|master(?:['’]s)?|university|"
    r"tehsil\w*|bakalavr\w*|magistr\w*)\b",
    re.I,
)
_EDUCATION_SUBJECT_RE = re.compile(
    r"\b(?:bachelor(?:['’]s)?(?:\s+degree)?|master(?:['’]s)?(?:\s+degree)?|"
    r"university\s+degree|bakalavr\w*|magistr\w*)"
    r"(?:\s+(?:in|uzre)\s+[a-z0-9əçşöüğ\s&+./-]+?)?"
    r"(?=\s+(?:is\s+)?(?:required|preferred|optional|mandatory|teleb\w*|"
    r"ustunluk\w*|mecburi\w*)\b|[.,;:]|$)",
    re.I,
)
_EXPERIENCE_RE = re.compile(
    r"\b(?:experience|experienced|background|exposure|tecrube\w*|islemis|isley\w*|worked)\b",
    re.I,
)
_TOTAL_RE = re.compile(r"\b(?:total|overall|general|professional|umumi|pesekar)\b", re.I)
_SOFT_UNSUPPORTED_RE = re.compile(
    r"\b(?:license|licence|vesiqe\w*|shift|novbe\w*|travel|relocation|ezamiyyet\w*|"
    r"communication|unsiyyet|musteri\s+yonumlu|flexib\w*|cevik\s+dusun\w*)\b",
    re.I,
)
_UNSCORABLE_SCOPE_RE = re.compile(r"\b(?:project|layihe)\w*\b", re.I)
_SEARCH_PREAMBLE_RE = re.compile(
    r"^(?:(?:please\s+)?(?:find|show|display|list|return)\b.*?\b"
    r"(?:candidates?|applicants?|namized\w*)\s+(?:with|who\s+have)\s+|"
    r"(?:\d+|bir|iki|uc|dord|bes|alti|yeddi|sekkiz|doqquz|on)\s+"
    r"(?:nefer\s+)?namized\w*\s+(?:goster|tap|cixart)\w*\s*:?\s*)",
    re.I,
)
_ROLE_ONLY_RE = re.compile(
    r"\b(?:axtaririq|axtariram|lazimdir|needed|hiring|looking\s+for)\b", re.I
)
_ROLE_REQUEST_RE = re.compile(
    r"\b(?:axtaririq|axtariram|hiring|looking\s+for)\b|"
    r"\b(?:developer|auditor|accountant|analyst|manager|backendci)\b.*\blazimdir\b",
    re.I,
)
_GENERIC_SEARCH_SUBJECT_RE = re.compile(
    r"(?i)^(?:(?:en\s+)?(?:uygun|yaxsi)|suitable|best)\s*"
    r"(?:namized\w*|candidate\w*)?$|^(?:namized\w*|candidate\w*)$"
)
_PROFESSIONAL_CUE_RE = re.compile(
    r"(?i)\b(?:knowledge(?:\s+of)?|command\s+of|proficien(?:t|cy)\s+in|familiar(?:ity)?\s+with|"
    r"skilled\s+in|competence\s+in|bilik\w*|biliy\w*|bilm\w*|bils\w*|bacariq\w*|"
    r"istifade\w*)\b"
)
_COUNT_ENTITY_RE = re.compile(
    rf"(?i)\b(?:top\s*)?{_NUMBER_TOKEN}\s+"
    r"(?:candidates?|applicants?|results?|profiles?|namized\w*|nefer)\b|"
    rf"\b(?:show|find|display|list|return|goster\w*|tap\w*|cixart\w*)\s+"
    rf"(?:up\s+to\s+|at\s+most\s+|en\s+cox\s+)?{_NUMBER_TOKEN}\b"
)
_KNOWLEDGE_QUALIFIER_RE = re.compile(
    r"(?i)\b(?:good|strong|solid|working|advanced|yaxsi|ela|guclu)\b"
)
_SUBJECT_SYNTAX_PATTERNS = (
    re.compile(
        r"(?i)^\s*(?:(?:applicants?|candidates?)\s+(?:(?:are|must|should)\s+)?"
        r"(?:be\s+|have\s+)?)?(?:proficient|skilled|experienced)\s+in\s+"
        r"(?P<subject>.+?)(?=\s+(?:and\s+)?it\s+is\s+"
        r"(?:preferred|required|optional)\s*$|\s*$)"
    ),
    re.compile(
        r"(?i)^\s*(?:(?:applicants?|candidates?)\s+(?:(?:are|must|should)\s+)?"
        r"have\s+)?(?:knowledge|command|familiarity)\s+(?:of|with)\s+"
        r"(?P<subject>.+?)(?=\s+(?:is\s+)?(?:required|preferred|optional|mandatory)"
        r"\b|\s*$)"
    ),
    re.compile(r"(?i)^\s*(?P<subject>.+?)\s+knowledge\b"),
    re.compile(
        r"(?i)^\s*(?P<subject>.+?)(?:-(?:dan|den))?\s+istifade\w*\b"
    ),
    re.compile(
        r"(?i)^\s*(?:(?:amma|lakin|hemcinin|ve|but|however|and)\s+)?"
        r"(?P<subject>.+?)(?:-(?:ni|nu|n[uü]|i|ı|u|ü))?\s+"
        r"(?:(?:yaxsi|ela|guclu)\s+)?(?:biliy\w*|bilm\w*|bils\w*|bacariq\w*)\b"
    ),
    re.compile(
        r"(?i)^\s*(?:(?:amma|lakin|hemcinin|ve|but|however|and)\s+)?"
        r"(?P<subject>.+?)\s+(?:uzre|sahesinde|sektorunda)\s+"
        r"(?:tecrube\w*|islemis|isley\w*)\b"
    ),
    re.compile(
        r"(?i)^\s*(?:experience|background|exposure)\s+in\s+"
        r"(?P<subject>.+?)(?:\s+(?:domain|sector))?(?:\s+is)?\s+"
        r"(?:required|preferred|optional|mandatory)\b"
    ),
    re.compile(
        r"(?i)^\s*(?P<subject>.+?)\s+(?:language|dili)\b"
    ),
)
_PREFERRED_CONTRAST_RE = re.compile(
    r"\b(?:not\s+(?:mandatory|required)\s*,?\s*but\s+(?:it\s+)?(?:is\s+)?preferred|"
    r"mecburi\s+deyil\s*,?\s*(?:amma|lakin)\s+ustunluk\w*)\b",
    re.I,
)
_BOUNDARY_RE = re.compile(r"(?:\r?\n)+|(?<=[.!?;])\s+")
_BULLET_RE = re.compile(r"\s*(?:[-*•]|\d+[.)])\s*")
_COORD_RE = re.compile(r"\s+(?:and|ve(?:\s+ya)?|or)\s+", re.I)
_RESULT_TAIL_RE = re.compile(
    r"\s+(?:(?:olan|bilen)\w*\s+)?(?:\d+\s+)?"
    r"(?:nefer\s+)?(?:namized\w*\s+)?(?:goster|tap|cixart)\w*\s*$|"
    r"\s+\d+\s+(?:nefer|namized\w*)\s*$",
    re.I,
)

_LANGUAGE_ALIASES = {
    "english": "English",
    "ingilis": "English",
    "ingilisce": "English",
    "ingilis dili": "English",
    "russian": "Russian",
    "rus": "Russian",
    "rusca": "Russian",
    "rus dili": "Russian",
    "azerbaijani": "Azerbaijani",
    "azerbaycan": "Azerbaijani",
    "azerbaycanca": "Azerbaijani",
    "azerbaycan dili": "Azerbaijani",
    "turkish": "Turkish",
    "turk": "Turkish",
    "turkce": "Turkish",
    "turk dili": "Turkish",
    "german": "German",
    "deutsch": "German",
    "alman": "German",
    "almanca": "German",
    "alman dili": "German",
}

_DOMAIN_HEADS = frozenset(
    {
        "accounting",
        "audit",
        "bank",
        "banking",
        "cybersecurity",
        "data analytics",
        "fmcg",
        "finance",
        "ifrs",
        "muhasibat",
        "project management",
        "risk",
        "sales",
        "satis",
        "operations",
    }
)


@dataclass(frozen=True)
class SemanticAnalysis:
    language: SupportedInputLanguage
    spans: list[RequirementSpan]
    requirements: list[SemanticRequirement]
    result_count: ResultCountIntent
    result_count_needs_review: bool = False


def _fold(text: str) -> str:
    # Azerbaijani folding is one-codepoint-to-one-codepoint.  Keeping spacing
    # intact lets regex match offsets remain exact source offsets.
    return fold_az_ascii(normalize_azerbaijani_case(text))


def classify_supported_language(text: str) -> SupportedInputLanguage:
    if _CYRILLIC_RE.search(text):
        return SupportedInputLanguage.UNSUPPORTED
    folded = _fold(text)
    az = bool(_AZ_CHAR_RE.search(text) or _AZ_WORD_RE.search(folded))
    en = bool(_EN_MARKERS_RE.search(folded))
    if az and en:
        return SupportedInputLanguage.MIXED_AZ_EN
    return SupportedInputLanguage.AZERBAIJANI if az else SupportedInputLanguage.ENGLISH


def _number_value(token: str) -> float:
    normalized = _fold(token)
    if normalized in _NUMBER_WORDS:
        return float(_NUMBER_WORDS[normalized])
    return float(normalized.replace(",", "."))


def _occurrence(jd_text: str, start: int, end: int) -> SourceOccurrence:
    return SourceOccurrence(start_offset=start, end_offset=end, text=jd_text[start:end])


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and (text[start].isspace() or text[start] in "-*•"):
        start += 1
    bullet = _BULLET_RE.match(text, start, end)
    if bullet:
        start = bullet.end()
    while end > start and (text[end - 1].isspace() or text[end - 1] in ".;,"):
        end -= 1
    return start, end


def _sentences(text: str) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    cursor = 0
    for match in _BOUNDARY_RE.finditer(text):
        end = match.start()
        if match.group(0).lstrip().startswith((".", "!", "?", ";")):
            end += 1
        start, end = _trim(text, cursor, end)
        if start < end:
            result.append((start, end))
        cursor = match.end()
    start, end = _trim(text, cursor, len(text))
    if start < end:
        result.append((start, end))
    return result


def _material(text: str) -> bool:
    folded = _fold(text)
    return bool(
        find_prohibited_term(text)
        or _NEGATIVE_RE.search(folded)
        or _PREFERRED_RE.search(folded)
        or _REQUIRED_RE.search(folded)
        or _MINIMUM_RE.search(folded)
        or _STRICT_GREATER_RE.search(folded)
        or _EXPERIENCE_RE.search(folded)
        or _CERT_RE.search(folded)
        or _EDUCATION_RE.search(folded)
        or _LEVEL_RE.search(folded)
        or _SOFT_UNSUPPORTED_RE.search(folded)
        or _PROFESSIONAL_CUE_RE.search(folded)
        or _COUNT_ENTITY_RE.search(folded)
    )


def _split_units(text: str) -> list[tuple[int, int, tuple[int, int] | None]]:
    """Return clause offsets plus a sentence-level shared modality occurrence."""
    units: list[tuple[int, int, tuple[int, int] | None]] = []
    for sentence_start, sentence_end in _sentences(text):
        sentence = text[sentence_start:sentence_end]
        folded_sentence = _fold(sentence)
        modality_matches = (
            list(_NEGATIVE_RE.finditer(folded_sentence))
            + list(_PREFERRED_RE.finditer(folded_sentence))
            + list(_REQUIRED_RE.finditer(folded_sentence))
        )
        shared: tuple[int, int] | None = None
        if len(modality_matches) == 1 and not _PREFERRED_RE.search(modality_matches[0].group(0)):
            match = modality_matches[0]
            # Folding preserves character count for the supported alphabet.
            shared = (sentence_start + match.start(), sentence_start + match.end())
        if shared is None and re.search(
            r"(?i)\b(?:namized\w*\s+(?:goster|tap|cixart)|"
            r"(?:olan|bilen)\w*\s+(?:goster|tap|cixart)|"
            r"(?:goster|tap|cixart)\w*\s+namized\w*)\b",
            folded_sentence,
        ):
            search_link = re.search(r"(?i)\b(?:olan|bilen|with)\b", folded_sentence)
            action = search_link or re.search(
                r"(?i)\b(?:goster\w*|tap\w*|cixart\w*)\b", folded_sentence
            )
            if action:
                shared = (sentence_start + action.start(), sentence_start + action.end())
        if (
            shared is None
            and extract_result_count_intent(sentence).requested is not None
            and (action := re.search(r"(?i)\b(?:goster|tap|cixart)\w*\b", folded_sentence))
        ):
            shared = (sentence_start + action.start(), sentence_start + action.end())

        clause_ranges: list[tuple[int, int]] = []
        if _PREFERRED_CONTRAST_RE.search(folded_sentence):
            clause_ranges.append((sentence_start, sentence_end))
            units.extend((start, end, None) for start, end in clause_ranges)
            continue
        cursor = 0
        for comma in re.finditer(r",", sentence):
            start, end = _trim(text, sentence_start + cursor, sentence_start + comma.start())
            if start < end:
                clause_ranges.append((start, end))
            cursor = comma.end()
        start, end = _trim(text, sentence_start + cursor, sentence_end)
        if start < end:
            clause_ranges.append((start, end))

        for clause_start, clause_end in clause_ranges:
            clause = text[clause_start:clause_end]
            preamble = _SEARCH_PREAMBLE_RE.match(_fold(clause))
            if preamble:
                clause_start += preamble.end()
                clause = text[clause_start:clause_end]
            colon = clause.find(":")
            role_prefix = _fold(clause[:colon]) if colon >= 0 else ""
            if colon >= 0 and (
                _ROLE_REQUEST_RE.search(role_prefix)
                or re.search(
                    r"(?i)\b(?:developer|analyst|manager|auditor|accountant|"
                    r"specialist|engineer|requirements?|telebler)\b$",
                    role_prefix.strip(),
                )
            ):
                clause_start, clause_end = _trim(text, clause_start + colon + 1, clause_end)
                clause = text[clause_start:clause_end]

            local_shared = shared
            local_folded = _fold(clause)
            local_modalities = (
                list(_NEGATIVE_RE.finditer(local_folded))
                + list(_PREFERRED_RE.finditer(local_folded))
                + list(_REQUIRED_RE.finditer(local_folded))
            )
            if local_shared is None and len(local_modalities) == 1:
                local_match = local_modalities[0]
                local_shared = (
                    clause_start + local_match.start(),
                    clause_start + local_match.end(),
                )

            coordinated = [
                item
                for item in _COORD_RE.finditer(_fold(clause))
                if not re.match(r"(?i)higher\b", _fold(clause)[item.end() :])
            ]
            if re.search(r"(?i)\bve\s+plus\s+sayilir\b", _fold(clause)):
                coordinated = []
            if re.search(
                r"(?i)\b(?:and|ve)\s+it\s+is\s+(?:preferred|required|optional)\b",
                _fold(clause),
            ):
                coordinated = []
            should_split = bool(coordinated) and (
                len(coordinated) == 1
                and (
                    _material(clause[: coordinated[0].start()])
                    or _material(clause[coordinated[0].end() :])
                )
            )
            if not should_split:
                units.append((clause_start, clause_end, local_shared))
                continue
            if local_shared is None and preamble:
                with_match = re.search(r"(?i)\b(?:with|who\s+have)\b", sentence)
                if with_match:
                    local_shared = (
                        sentence_start + with_match.start(),
                        sentence_start + with_match.end(),
                    )
            part_cursor = 0
            for separator in coordinated:
                part_start, part_end = _trim(
                    text, clause_start + part_cursor, clause_start + separator.start()
                )
                if part_start < part_end:
                    units.append((part_start, part_end, local_shared))
                part_cursor = separator.end()
            part_start, part_end = _trim(text, clause_start + part_cursor, clause_end)
            if part_start < part_end:
                units.append((part_start, part_end, local_shared))
    return units


def _modality(
    jd_text: str, start: int, end: int, shared: tuple[int, int] | None
) -> tuple[CriterionType | None, SourceOccurrence | None, bool]:
    source = jd_text[start:end]
    folded = _fold(source)
    contrast = _PREFERRED_CONTRAST_RE.search(folded)
    if contrast:
        preferred = _PREFERRED_RE.search(folded[contrast.start() : contrast.end()])
        assert preferred is not None
        occurrence_start = start + contrast.start() + preferred.start()
        return (
            CriterionType.PREFERRED,
            _occurrence(jd_text, occurrence_start, occurrence_start + len(preferred.group(0))),
            False,
        )
    negative = _NEGATIVE_RE.search(folded)
    if negative:
        return None, _occurrence(jd_text, start + negative.start(), start + negative.end()), True
    preferred = _PREFERRED_RE.search(folded)
    required = _REQUIRED_RE.search(folded)
    minimum = _MINIMUM_RE.search(folded)
    if preferred and not required:
        return (
            CriterionType.PREFERRED,
            _occurrence(jd_text, start + preferred.start(), start + preferred.end()),
            False,
        )
    if required or minimum:
        match = required or minimum
        assert match is not None
        return (
            CriterionType.MUST_HAVE,
            _occurrence(jd_text, start + match.start(), start + match.end()),
            False,
        )
    level_requirement = re.search(
        r"(?i)\b(?:minimum|at\s+least|en\s+azi)\s+"
        r"(?:a1|a2|b1|b2|c1|c2|beginner|intermediate|advanced|fluent|native)\b",
        folded,
    )
    if level_requirement:
        return (
            CriterionType.MUST_HAVE,
            _occurrence(
                jd_text,
                start + level_requirement.start(),
                start + level_requirement.end(),
            ),
            False,
        )
    if shared is not None:
        shared_text = _fold(jd_text[shared[0] : shared[1]])
        if _PREFERRED_RE.search(shared_text):
            return CriterionType.PREFERRED, _occurrence(jd_text, *shared), False
        if _REQUIRED_RE.search(shared_text):
            return CriterionType.MUST_HAVE, _occurrence(jd_text, *shared), False
        if re.fullmatch(r"(?i)(?:with|who\s+have)", shared_text):
            return CriterionType.MUST_HAVE, _occurrence(jd_text, *shared), False
        if re.fullmatch(r"(?i)(?:olan|bilen|goster\w*|tap\w*|cixart\w*)", shared_text):
            return CriterionType.MUST_HAVE, _occurrence(jd_text, *shared), False
    return None, None, False


def _duration(
    jd_text: str, start: int, end: int
) -> tuple[float | None, SourceOccurrence | None, str | None]:
    folded = _fold(jd_text[start:end])
    strict = _STRICT_GREATER_RE.search(folded)
    if strict:
        token = strict.group("a") or strict.group("b")
        occurrence = _occurrence(jd_text, start + strict.start(), start + strict.end())
        return _number_value(token), occurrence, ">"
    minimum = _MINIMUM_RE.search(folded)
    if minimum:
        token = minimum.group("number") or minimum.group("plus") or minimum.group("not_less")
        return (
            _number_value(token),
            _occurrence(jd_text, start + minimum.start(), start + minimum.end()),
            ">=",
        )
    duration = _DURATION_RE.search(folded)
    if duration:
        return (
            _number_value(duration.group("number")),
            _occurrence(jd_text, start + duration.start(), start + duration.end()),
            ">=",
        )
    return None, None, None


def _subject_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """Remove only anchored HR grammar wrappers; never delete arbitrary tokens."""
    raw = text[start:end]
    folded = _fold(raw)
    for pattern in _SUBJECT_SYNTAX_PATTERNS:
        match = pattern.search(folded)
        if match:
            subject_start = start + match.start("subject")
            subject_end = start + match.end("subject")
            while subject_end > subject_start and text[subject_end - 1] in " .;,:":
                subject_end -= 1
            hyphen_case = re.search(
                r"(?i)-(?:ni|nu|n[uü]|i|ı|u|ü)$", text[subject_start:subject_end]
            )
            if hyphen_case:
                subject_end = subject_start + hyphen_case.start()
            return subject_start, subject_end
    masks: list[tuple[int, int]] = []
    for pattern in (
        _NEGATIVE_RE,
        _PREFERRED_RE,
        _REQUIRED_RE,
        _MINIMUM_RE,
        _STRICT_GREATER_RE,
        _DURATION_RE,
        _LEVEL_RE,
        _CERT_RE,
        _EDUCATION_RE,
        _EXPERIENCE_RE,
        _PREFERRED_CONTRAST_RE,
        _RESULT_TAIL_RE,
        _COUNT_ENTITY_RE,
        _PROFESSIONAL_CUE_RE,
        _KNOWLEDGE_QUALIFIER_RE,
    ):
        masks.extend((match.start(), match.end()) for match in pattern.finditer(folded))
    chars = list(raw)
    for mask_start, mask_end in masks:
        for index in range(mask_start, min(mask_end, len(chars))):
            chars[index] = " "
    # Language-level grammatical wrappers only.  Mask in place (rather than
    # substituting) so the surviving subject offsets remain exact.
    wrapper_patterns = (
        re.compile(
            r"(?i)^\s*(?:(?:but|however|and|amma|lakin|hemcinin)\s+)?"
            r"(?:minimum|at\s+least|en\s+azi|find|show|mene|applicants?|namizəd\w*|"
            r"namized\w*|candidates?)\s+(?:are\s+|must\s+(?:be\s+|have\s+)?|"
            r"should\s+(?:be\s+|have\s+)?)?"
        ),
        re.compile(
            r"(?i)^\s*(?:(?:but|however|and|amma|lakin|hemcinin)\s+|"
            r"(?:are|be|have)\s+|(?:proficient|skilled|experienced)\s+in\s+|"
            r"(?:knowledge|command)\s+of\s+|(?:familiarity|familiar)\s+with\s+)+"
        ),
        re.compile(
            r"(?i)\s+(?:uzre|ile|sahesi|sahesinde|sektorunda(?:\s+is)?|"
            r"sector\s+experience|bilen(?:lere)?|olan|olan\s+namized\w*|"
            r"olan\s+namized\w*\s+(?:goster|tap|cixart)\w*|at|or\s+higher|higher)\s*$"
        ),
        re.compile(
            r"(?i)\s+(?:(?:bilen|olan)\w*\s+(?:namized\w*\s+)?"
            r"(?:goster|tap|cixart)\w*)\s*$"
        ),
        re.compile(r"(?i)\s+bilen\s+[a-z]+(?:den|dan)\s*$"),
        re.compile(
            r"(?i)\s+(?:is|are|be|iş|is|olaraq|olunur|sayılsın|sayilsin|"
            r"verilir|ver|et)\s*$"
        ),
        re.compile(r"(?i)^\s*(?:of|in|with)\s+"),
        re.compile(r"(?i)\s+(?:but\s+it|is|it\s+is)\s*$"),
        re.compile(r"(?i)^\s*(?:de|da)\s+"),
    )
    changed = True
    while changed:
        changed = False
        for pattern in wrapper_patterns:
            residual_folded = _fold("".join(chars))
            match = pattern.search(residual_folded)
            if match:
                for index in range(match.start(), min(match.end(), len(chars))):
                    chars[index] = " "
                changed = True
    residual = "".join(chars)
    match = re.search(r"\S(?:.*\S)?", residual)
    if not match:
        return start, start
    local_start = match.start()
    local_end = match.end()
    # A subject must be one contiguous source occurrence.  If an HR wrapper
    # begins after the subject (``Banking experience``, ``ACCA sertifikatı``),
    # stop at that exact wrapper boundary instead of returning a range that
    # merely spans across masked words.
    following_masks = sorted(mask_start for mask_start, _ in masks if mask_start > local_start)
    if following_masks:
        local_end = min(local_end, following_masks[0])
    while local_end > local_start and (
        raw[local_end - 1].isspace() or raw[local_end - 1] in ".;,:"
    ):
        local_end -= 1
    subject_start = start + local_start
    subject_end = start + local_end
    # Azerbaijani locative/ablative on one leading professional token
    # (Pythonda, SQL-dan, Bankda) is a grammatical wrapper, not subject text.
    token = text[subject_start:subject_end]
    locative = re.fullmatch(
        r"(?i)([A-Za-zƏəÇçŞşÖöÜüĞğİı0-9+#./-]+?)(?P<hyphen>-?)(?:da|de|dan|den)",
        token,
    )
    if locative:
        base = _fold(locative.group(1))
        # Without a hyphen, a word ending in -da/-dan may be the entity
        # itself (Camunda), not Azerbaijani case grammar. Strip only when the
        # boundary is explicit or the base is already a reviewed canonical
        # alias/domain; unknown professional identities remain intact.
        if (
            locative.group("hyphen")
            or base in SKILL_ALIASES
            or base in SKILL_ALIASES.values()
            or base in _DOMAIN_HEADS
            or canonicalize_domain(base) in DOMAIN_SYNONYMS
        ):
            subject_end = subject_start + len(locative.group(1))
    return subject_start, subject_end


def _language_subject(source: str) -> str | None:
    match = _LANGUAGE_RE.search(_fold(source))
    if match:
        return _LANGUAGE_ALIASES.get(match.group(0).casefold())
    generic = _LANGUAGE_WRAPPER_RE.search(_fold(source))
    if generic:
        return " ".join(source[generic.start("subject") : generic.end("subject")].split())
    return None


def _family(subject: str, source: str) -> JDDraftCriterionKind:
    folded = _fold(source)
    if _SOFT_UNSUPPORTED_RE.search(folded):
        return JDDraftCriterionKind.OTHER
    if _UNSCORABLE_SCOPE_RE.search(folded) and "project management" not in folded:
        return JDDraftCriterionKind.OTHER
    if _EXPERIENCE_RE.search(folded) or _DURATION_RE.search(folded):
        if _TOTAL_RE.search(folded):
            return JDDraftCriterionKind.EXPERIENCE
        normalized = _fold(subject).strip(" .,:;-")
        if not normalized:
            return JDDraftCriterionKind.EXPERIENCE
        domain = canonicalize_domain(normalized)
        if (
            re.search(r"\b(?:sahe\w*|sector\w*|domain)\b", folded)
            or normalized in _DOMAIN_HEADS
            or domain in DOMAIN_SYNONYMS
            or any(normalized.endswith(f" {suffix}") for suffix in _DOMAIN_HEADS)
        ):
            return JDDraftCriterionKind.DOMAIN_EXPERIENCE
        return JDDraftCriterionKind.SKILL_EXPERIENCE
    if _CERT_RE.search(folded) or _fold(subject).strip(" .,:;-") in _CERTIFICATION_NAMES:
        return JDDraftCriterionKind.CERTIFICATION
    if _EDUCATION_RE.search(folded):
        return JDDraftCriterionKind.EDUCATION
    if _LANGUAGE_RE.search(folded) or _LANGUAGE_WRAPPER_RE.search(folded):
        return JDDraftCriterionKind.LANGUAGE
    if re.search(r"\b(?:sahe\w*|sector\w*|domain)\b", folded):
        return JDDraftCriterionKind.DOMAIN_EXPERIENCE
    return JDDraftCriterionKind.SKILL


def _normalized_subject(family: JDDraftCriterionKind, source_subject: str, source: str) -> str:
    if family == JDDraftCriterionKind.LANGUAGE:
        return _language_subject(source) or _fold(source_subject).title()
    normalized = " ".join(
        re.sub(r"^[\s.,:;\-_/').(\"]+|[\s.,:;\-_/').(\"]+$", "", source_subject).split()
    )
    if family in (JDDraftCriterionKind.SKILL, JDDraftCriterionKind.SKILL_EXPERIENCE):
        folded = _fold(normalized)
        if folded in SKILL_ALIASES:
            canonical = normalize_skill_name(folded)
            display_aliases = {
                "javascript": "JavaScript",
                "typescript": "TypeScript",
                "postgresql": "PostgreSQL",
                "python": "Python",
                "kubernetes": "Kubernetes",
                "go": "Go",
            }
            return display_aliases.get(canonical, canonical)
        return normalized
    if family == JDDraftCriterionKind.DOMAIN_EXPERIENCE:
        canonical = canonicalize_domain(_fold(normalized))
        normalized = re.sub(r"(?i)\s+(?:domain|sector)$", "", normalized).strip()
        return "Banking" if canonical == "banking" or _fold(normalized) == "bank" else normalized
    return normalized


def analyze_hr_text(jd_text: str) -> SemanticAnalysis:
    language = classify_supported_language(jd_text)
    result_count = extract_result_count_intent(jd_text)
    folded_jd = _fold(jd_text)
    explicit_result_count = bool(
        re.search(
            r"(?i)(?:\btop\s*\d+\b|\b\d+\s+(?:nefer|namized\w*|candidates?)\b|"
            r"\b(?:show|find|goster|cixart)\s+(?:the\s+best\s+)?\d+\b)",
            folded_jd,
        )
    )
    result_count_needs_review = explicit_result_count and result_count.requested is None
    if language == SupportedInputLanguage.UNSUPPORTED:
        return SemanticAnalysis(
            language, [], [], result_count, result_count_needs_review=result_count_needs_review
        )

    spans: list[RequirementSpan] = []
    requirements: list[SemanticRequirement] = []
    seen_ranges: set[tuple[int, int]] = set()
    for start, end, shared in _split_units(jd_text):
        source = jd_text[start:end]
        if not source.strip() or is_result_count_only(source):
            continue
        if _ROLE_REQUEST_RE.search(_fold(source)):
            continue
        if not _material(source) and shared is None:
            # A role/search preface is workflow intent, not a ranking
            # requirement.  It must never become a nonsense SKILL row.
            continue
        if (start, end) in seen_ranges:
            continue
        seen_ranges.add((start, end))
        span_id = f"req-{len(spans) + 1:04d}"
        prohibited = find_prohibited_term(source) is not None
        modality_type, modality_occurrence, negated = _modality(jd_text, start, end, shared)
        min_years, duration_occurrence, comparison = _duration(jd_text, start, end)
        subject_start, subject_end = _subject_bounds(jd_text, start, end)
        subject_text = jd_text[subject_start:subject_end].strip()
        family = _family(subject_text, source)
        if family == JDDraftCriterionKind.EDUCATION:
            education = _EDUCATION_SUBJECT_RE.search(_fold(source))
            if education:
                subject_start = start + education.start()
                subject_end = start + education.end()
                subject_text = jd_text[subject_start:subject_end]
        normalized_subject = (
            _normalized_subject(family, subject_text, source) if subject_text else ""
        )
        if family == JDDraftCriterionKind.SKILL and _GENERIC_SEARCH_SUBJECT_RE.fullmatch(
            _fold(normalized_subject)
        ):
            # Search workflow words are not professional requirements.  With
            # no material subject, leave the request to the established
            # ambiguity path instead of manufacturing a generic skill.
            continue
        level_match = _LEVEL_RE.search(_fold(source))
        level_occurrence = (
            _occurrence(jd_text, start + level_match.start(), start + level_match.end())
            if level_match
            else None
        )
        required_level = level_match.group(0).upper() if level_match else None

        if prohibited:
            state = SemanticRequirementState.PROHIBITED
        elif negated:
            state = SemanticRequirementState.UNSUPPORTED
        elif _COUNT_ENTITY_RE.search(_fold(source)):
            # Candidate/result quantities are workflow control, never a
            # professional criterion. Ambiguous count wording stays visible
            # for review; an unambiguous count-only unit was already consumed
            # above by is_result_count_only().
            state = SemanticRequirementState.NEEDS_HUMAN_REVIEW
        elif re.search(r"(?i)\s+d[ea]\s+", _fold(source)):
            # Azerbaijani additive particles can coordinate multiple subjects
            # under one trailing modality. Without a distinct source-bound
            # subject occurrence for each side, the clause must not become one
            # synthetic combined skill identity.
            state = SemanticRequirementState.NEEDS_HUMAN_REVIEW
        elif comparison == ">":
            state = SemanticRequirementState.UNSUPPORTED
        elif family == JDDraftCriterionKind.OTHER:
            state = SemanticRequirementState.UNSUPPORTED
        elif modality_type is None:
            state = SemanticRequirementState.NEEDS_HUMAN_REVIEW
        elif not normalized_subject and family != JDDraftCriterionKind.EXPERIENCE:
            state = SemanticRequirementState.NEEDS_HUMAN_REVIEW
        elif (
            family
            in (
                JDDraftCriterionKind.EXPERIENCE,
                JDDraftCriterionKind.SKILL_EXPERIENCE,
            )
            and min_years is None
        ):
            # Preserve the source family. A named experience claim without a
            # duration cannot be weakened to bare skill presence merely to fit
            # an evaluator shape.
            state = SemanticRequirementState.NEEDS_HUMAN_REVIEW
        elif family == JDDraftCriterionKind.CERTIFICATION and (
            not normalized_subject
            or re.fullmatch(r"\d+(?:[.,]\d+)?", normalized_subject)
            or re.fullmatch(
                r"(?i)(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)?\s*"
                r"(?:certificat(?:e|ion)s?|sertifikat\w*)",
                _fold(normalized_subject),
            )
        ):
            # Quantity/generic certification wording is not a certification
            # identity. Keep it visible without inventing a named credential.
            state = SemanticRequirementState.NEEDS_HUMAN_REVIEW
        else:
            state = SemanticRequirementState.SCORABLE

        span = RequirementSpan(
            span_id=span_id,
            start_offset=start,
            end_offset=end,
            text=source,
            normalized=_fold(source),
            segmentation_needs_review=False,
        )
        spans.append(span)
        requirements.append(
            SemanticRequirement(
                requirement_span_id=span_id,
                criterion_family=family,
                subject=(
                    _occurrence(jd_text, subject_start, subject_end)
                    if subject_start < subject_end
                    else None
                ),
                normalized_subject=normalized_subject or None,
                modality=modality_occurrence,
                criterion_type=modality_type,
                duration_or_number=duration_occurrence,
                min_years=min_years,
                proficiency=level_occurrence,
                required_level=required_level,
                state=state,
                comparison=comparison,
            )
        )
    # Construction invariant: every server-owned material span terminates in
    # exactly one explicit semantic state. Any future parser branch that adds,
    # drops, or duplicates one side fails here instead of silently weakening a
    # vacancy draft.
    span_ids = [span.span_id for span in spans]
    requirement_ids = [item.requirement_span_id for item in requirements]
    if len(span_ids) != len(set(span_ids)) or requirement_ids != span_ids:
        raise RuntimeError("Material requirement reconciliation invariant failed.")
    return SemanticAnalysis(
        language,
        spans,
        requirements,
        result_count,
        result_count_needs_review=result_count_needs_review,
    )
