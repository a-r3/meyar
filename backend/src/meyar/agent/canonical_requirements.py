"""Canonical professional-requirement boundary for JD drafts (issue #84).

Flow::

    exact source text
    → server-owned source spans + deterministic safety classification
      (meyar.agent.semantic_requirements.analyze_hr_text)
    → optional local-model CanonicalRequirement proposal (JDDraftCriterionItem)
    → server validation against the exact source span (this module)
    → SCORABLE | NEEDS_HUMAN_REVIEW | UNSUPPORTED | PROHIBITED
    → HR confirmation → JobCriterion

Authority rules:

- Deterministic safety states (PROHIBITED, UNSUPPORTED) are never overridden
  by a model proposal.
- A deterministic subject becomes scoring authority only when it is already
  a canonical professional subject (``is_canonical_subject``). Raw grammatical
  remainders ("PostgreSQL ilə işləməyi"), header/title text ("Vakansiya:
  Analitik — Python") and recruitment/person tokens never do.
- A model proposal may close ONLY a subject-normalization gap
  (``SemanticReviewReason.SUBJECT_NOT_CANONICAL`` or
  ``RECRUITMENT_SUBJECT_WITHOUT_CUE``). It must reference a known span id, keep
  the deterministic family/duration/level exactly, and its canonical subject
  must itself be canonical and grounded in the span's exact text. It never
  supplies modality, weight, score, offsets, or new requirements.
- Spans split from one coordinated clause receive symmetric authority: either
  every side is scorable or none is.
- Nothing here persists anything; no raw text, span text or model output is
  audited.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, Field

from meyar.agent.schemas import (
    JDDraftCriterionItem,
    JDDraftCriterionKind,
    RequirementSpan,
    SemanticRequirement,
    SemanticRequirementState,
    SemanticReviewReason,
)
from meyar.agent.semantic_requirements import display_skill_name, language_alias_for
from meyar.core.domain_terms import DOMAIN_SYNONYMS, canonicalize_domain
from meyar.core.text import fold_az_ascii, normalize_azerbaijani_case
from meyar.evaluation.normalization import SKILL_ALIASES, normalize_skill_name
from meyar.schemas.criteria import CriterionType, find_prohibited_term

JD_SEMANTIC_POLICY_VERSION = "jd-semantic-policy-v2"

MAX_CANONICAL_SUBJECT_LENGTH = 80
MAX_CANONICAL_SUBJECT_TOKENS = 5

# Review reasons a validated local-model canonical proposal may close. Every
# other reason is deterministic policy.
LIFTABLE_REVIEW_REASONS = frozenset(
    {
        SemanticReviewReason.SUBJECT_NOT_CANONICAL,
        SemanticReviewReason.RECRUITMENT_SUBJECT_WITHOUT_CUE,
    }
)

_CANONICAL_LANGUAGES = frozenset({"english", "russian", "azerbaijani", "turkish", "german"})
_CEFR_LEVELS = frozenset({"A1", "A2", "B1", "B2", "C1", "C2"})

_TOKEN_RE = re.compile(r"[^\W_]+(?:[+#./-][^\W_]+)*[+#]*|\.[^\W_]+", re.UNICODE)

# Tokens that can never be part of a canonical professional subject. This is a
# fail-closed rejection list (anything matched goes to human review), not a
# vocabulary for understanding meaning.
_HEADER_TOKENS = frozenset({"vakansiya", "vacancy", "position", "vezife", "vezifesi"})
_PERSON_TOKENS_RE = re.compile(
    r"^(?:namized\w*|candidates?|applicants?|holders?|persons?|people|individuals?|"
    r"sexsler|nefer\w*)$"
)
_FUNCTION_TOKENS = frozenset(
    {
        # Azerbaijani (folded) postpositions/conjunctions/relatives
        "ile", "ve", "veya", "ucun", "uzre", "olan", "olaraq", "bilen", "bilenler",
        "sahib", "malik", "kimi", "de", "da", "ise", "ya", "hem", "ancaq",
        # English function words that never belong inside a canonical subject
        "with", "working", "work", "using", "use", "of", "in", "the", "a", "an",
        "and", "or", "as", "to", "for", "be", "is", "are", "must", "should",
        "required", "preferred", "mandatory", "experience", "knowledge", "skills",
        "skill", "ability", "able",
    }
)
# Azerbaijani verbal nouns / infinitives / modal-participle residue
# ("işləməyi", "bilmək", "bacarmalı", "istifadəsi").
_AZ_VERBAL_RESIDUE_RE = re.compile(
    r"^(?:\w+(?:mek|mak|meyi|magi|mesi|masi|meye|maga|meli|mali|malidir|melidir|"
    r"mekle|makla)|isle\w*|bacar\w*|bilme\w*|bilik\w*|istifade\w*|tecrube\w*|"
    r"teleb\w*|mutleq\w*|ustunluk\w*)$"
)


def _fold(text: str) -> str:
    return fold_az_ascii(normalize_azerbaijani_case(text))


def _tokens(text: str) -> list[str]:
    return [token.casefold() for token in _TOKEN_RE.findall(_fold(text))]


def _known_alias(folded_subject: str) -> bool:
    return (
        folded_subject in SKILL_ALIASES
        or folded_subject in SKILL_ALIASES.values()
        or normalize_skill_name(folded_subject) in SKILL_ALIASES.values()
    )


def is_canonical_subject(kind: JDDraftCriterionKind, subject: str | None) -> bool:
    """Deterministic, fail-closed check that ``subject`` is a canonical
    professional term rather than a raw grammatical remainder.

    It never tries to understand arbitrary prose: anything suspicious simply
    fails, sending the requirement to human review / local interpretation.
    """
    if kind == JDDraftCriterionKind.EXPERIENCE:
        # General experience carries no evaluated subject (value is None).
        return True
    if subject is None:
        return False
    stripped = subject.strip()
    if not stripped or len(stripped) > MAX_CANONICAL_SUBJECT_LENGTH:
        return False
    if any(char in stripped for char in ":;—–\"“”«»()[]{}!?"):
        return False
    if find_prohibited_term(stripped):
        return False
    tokens = _tokens(stripped)
    if not tokens or len(tokens) > MAX_CANONICAL_SUBJECT_TOKENS:
        return False
    folded = " ".join(tokens)
    if kind == JDDraftCriterionKind.LANGUAGE:
        return folded in _CANONICAL_LANGUAGES
    if _known_alias(folded):
        return True
    for token in tokens:
        if (
            token in _HEADER_TOKENS
            or token in _FUNCTION_TOKENS
            or _PERSON_TOKENS_RE.match(token)
            or _AZ_VERBAL_RESIDUE_RE.match(token)
        ):
            return False
    # A subject must carry at least one alphabetic identity token.
    return any(re.search(r"[a-z]", token) for token in tokens)


_AZ_CASE_SUFFIXES = (
    "", "da", "de", "dan", "den", "ni", "nu", "nin", "nun", "i", "u", "a", "e",
    "in", "un", "ya", "ye", "la", "le", "ile",
)


def subject_grounded_in_span(
    kind: JDDraftCriterionKind, subject: str, span_text: str
) -> bool:
    """True when the canonical ``subject`` is attributable to the exact span.

    Accepted: the subject's tokens occur contiguously in the span (optionally
    with a bounded Azerbaijani case suffix / hyphen on the last token), or the
    subject is the reviewed canonical alias of a contiguous span n-gram
    (skills, domains, languages). A model can therefore normalize
    "PostgreSQL ilə işləməyi" → "PostgreSQL", but can never introduce a term
    that is absent from the source.
    """
    subject_tokens = _tokens(subject)
    span_tokens = _tokens(span_text)
    if not subject_tokens or not span_tokens:
        return False
    size = len(subject_tokens)
    for index in range(0, len(span_tokens) - size + 1):
        window = span_tokens[index : index + size]
        if window[:-1] != subject_tokens[:-1]:
            continue
        last, expected = window[-1], subject_tokens[-1]
        if last == expected:
            return True
        if last.startswith(expected):
            suffix = last[len(expected) :].lstrip("-")
            if suffix in _AZ_CASE_SUFFIXES:
                return True
    folded_subject = " ".join(subject_tokens)
    for n in range(1, 5):
        for index in range(0, len(span_tokens) - n + 1):
            gram = " ".join(span_tokens[index : index + n])
            if kind in (JDDraftCriterionKind.SKILL, JDDraftCriterionKind.SKILL_EXPERIENCE):
                if gram in SKILL_ALIASES and normalize_skill_name(gram) == normalize_skill_name(
                    folded_subject
                ):
                    return True
            elif kind == JDDraftCriterionKind.DOMAIN_EXPERIENCE:
                gram_domain = "banking" if gram == "bank" else canonicalize_domain(gram)
                subject_domain = (
                    "banking" if folded_subject == "bank" else canonicalize_domain(folded_subject)
                )
                if gram_domain in DOMAIN_SYNONYMS and gram_domain == subject_domain:
                    return True
            elif kind == JDDraftCriterionKind.LANGUAGE:
                alias = language_alias_for(gram)
                if alias is not None and alias.casefold() == folded_subject:
                    return True
    return False


class CanonicalInterpretationSource(StrEnum):
    DETERMINISTIC = "DETERMINISTIC"
    MODEL_VALIDATED = "MODEL_VALIDATED"
    NONE = "NONE"


class CanonicalProposalRejection(StrEnum):
    """Why a local-model proposal was not accepted (structural, never text)."""

    UNKNOWN_SPAN = "UNKNOWN_SPAN"
    SPAN_NOT_INTERPRETABLE = "SPAN_NOT_INTERPRETABLE"
    UNSUPPORTED_KIND = "UNSUPPORTED_KIND"
    KIND_MISMATCH = "KIND_MISMATCH"
    DURATION_MISMATCH = "DURATION_MISMATCH"
    LEVEL_MISMATCH = "LEVEL_MISMATCH"
    SUBJECT_NOT_CANONICAL = "SUBJECT_NOT_CANONICAL"
    SUBJECT_NOT_GROUNDED = "SUBJECT_NOT_GROUNDED"
    PROHIBITED = "PROHIBITED"
    CONFLICTING_PROPOSALS = "CONFLICTING_PROPOSALS"


class CanonicalRequirement(BaseModel):
    """Validated, source-bound interpretation of ONE server span.

    Never persisted as its own entity (no migration): it is the in-memory
    authority from which a draft's CriterionIn rows are built."""

    model_config = {"extra": "forbid", "frozen": True}

    span_id: str = Field(pattern=r"^req-\d{4}$")
    kind: JDDraftCriterionKind | None = None
    canonical_subject: str | None = Field(default=None, max_length=200)
    criterion_type: CriterionType | None = None
    min_years: float | None = Field(default=None, ge=0, le=60)
    required_level: str | None = Field(default=None, max_length=50)
    interpretation_state: SemanticRequirementState
    interpretation_source: CanonicalInterpretationSource
    review_reason: SemanticReviewReason | None = None
    # True when kind/subject/duration/level are all server-validated, so an
    # unresolved state can only be a modality ambiguity (review may offer
    # MUST_HAVE/PREFERRED) — never a free-text-to-criterion transformation.
    shape_validated: bool = False


@dataclass(frozen=True)
class CanonicalizationResult:
    requirements: list[CanonicalRequirement]
    # Model proposals referencing an unknown span id (never source-bound).
    ungrounded_proposal_count: int
    rejected_proposal_count: int
    policy_version: str = JD_SEMANTIC_POLICY_VERSION


def _normalized_level(level: str | None) -> str | None:
    return level.strip().upper() if level else None


def _canonical_display(kind: JDDraftCriterionKind, subject: str) -> str:
    folded = " ".join(_tokens(subject))
    if kind in (JDDraftCriterionKind.SKILL, JDDraftCriterionKind.SKILL_EXPERIENCE):
        if folded in SKILL_ALIASES or folded in SKILL_ALIASES.values():
            return display_skill_name(folded)
    if kind == JDDraftCriterionKind.LANGUAGE:
        return folded.title()
    return " ".join(subject.split())


def _validate_proposal(
    item: JDDraftCriterionItem,
    *,
    semantic: SemanticRequirement,
    span: RequirementSpan,
) -> tuple[str, CanonicalProposalRejection | None]:
    """Return (canonical display subject, rejection)."""
    if item.kind == JDDraftCriterionKind.OTHER:
        return "", CanonicalProposalRejection.UNSUPPORTED_KIND
    if semantic.criterion_family is None or item.kind != semantic.criterion_family:
        return "", CanonicalProposalRejection.KIND_MISMATCH
    if item.min_years != semantic.min_years:
        return "", CanonicalProposalRejection.DURATION_MISMATCH
    if _normalized_level(item.required_level) != _normalized_level(semantic.required_level):
        return "", CanonicalProposalRejection.LEVEL_MISMATCH
    if item.required_level is not None and (
        _normalized_level(item.required_level) not in _CEFR_LEVELS
        and _fold(item.required_level) != _fold(semantic.required_level or "")
    ):
        return "", CanonicalProposalRejection.LEVEL_MISMATCH
    subject = " ".join(item.requirement.split())
    if find_prohibited_term(subject) or find_prohibited_term(span.text):
        return "", CanonicalProposalRejection.PROHIBITED
    if not is_canonical_subject(item.kind, subject):
        return "", CanonicalProposalRejection.SUBJECT_NOT_CANONICAL
    if item.kind != JDDraftCriterionKind.EXPERIENCE and not subject_grounded_in_span(
        item.kind, subject, span.text
    ):
        return "", CanonicalProposalRejection.SUBJECT_NOT_GROUNDED
    return _canonical_display(item.kind, subject), None


def _from_semantic(
    semantic: SemanticRequirement,
    span: RequirementSpan,
) -> CanonicalRequirement:
    """Deterministic interpretation without any model contribution."""
    state = semantic.state
    reason = semantic.review_reason
    if find_prohibited_term(span.text, semantic.normalized_subject or ""):
        state, reason = SemanticRequirementState.PROHIBITED, None
    family = semantic.criterion_family
    subject = semantic.normalized_subject
    if (
        state == SemanticRequirementState.SCORABLE
        and family is not None
        and not is_canonical_subject(family, subject)
    ):
        state, reason = (
            SemanticRequirementState.NEEDS_HUMAN_REVIEW,
            SemanticReviewReason.SUBJECT_NOT_CANONICAL,
        )
    shape_validated = bool(
        family is not None
        and family != JDDraftCriterionKind.OTHER
        and state
        in (SemanticRequirementState.SCORABLE, SemanticRequirementState.NEEDS_HUMAN_REVIEW)
        and (
            state == SemanticRequirementState.SCORABLE
            or reason == SemanticReviewReason.MODALITY_UNKNOWN
        )
        and is_canonical_subject(family, subject)
    )
    return CanonicalRequirement(
        span_id=span.span_id,
        kind=family if state != SemanticRequirementState.PROHIBITED else None,
        canonical_subject=(
            subject
            if state != SemanticRequirementState.PROHIBITED and subject
            else None
        ),
        criterion_type=semantic.criterion_type,
        min_years=semantic.min_years,
        required_level=semantic.required_level,
        interpretation_state=state,
        interpretation_source=(
            CanonicalInterpretationSource.DETERMINISTIC
            if state == SemanticRequirementState.SCORABLE
            else CanonicalInterpretationSource.NONE
        ),
        review_reason=reason if state == SemanticRequirementState.NEEDS_HUMAN_REVIEW else None,
        shape_validated=shape_validated,
    )


def canonicalize_requirements(
    *,
    spans: list[RequirementSpan],
    semantics: list[SemanticRequirement],
    proposals: Iterable[JDDraftCriterionItem] | None,
) -> CanonicalizationResult:
    """Server-validate every span's interpretation. ``proposals`` is the local
    model's (possibly absent/failed) canonical proposal list."""
    spans_by_id = {span.span_id: span for span in spans}
    semantics_by_id = {item.requirement_span_id: item for item in semantics}
    proposals_by_span: dict[str, list[JDDraftCriterionItem]] = {}
    ungrounded = 0
    rejected = 0
    for item in proposals or ():
        if item.span_id not in spans_by_id:
            ungrounded += 1
            continue
        proposals_by_span.setdefault(item.span_id, []).append(item)

    results: dict[str, CanonicalRequirement] = {}
    for span in spans:
        semantic = semantics_by_id[span.span_id]
        base = _from_semantic(semantic, span)
        candidates = proposals_by_span.get(span.span_id, [])
        if (
            base.interpretation_state != SemanticRequirementState.NEEDS_HUMAN_REVIEW
            or base.review_reason not in LIFTABLE_REVIEW_REASONS
        ):
            # Deterministic authority (scorable canonical, unsupported,
            # prohibited, or non-liftable review). Proposals are ignored.
            rejected += len(candidates)
            results[span.span_id] = base
            continue
        accepted: dict[str, str] = {}
        for item in candidates:
            display, rejection = _validate_proposal(item, semantic=semantic, span=span)
            if rejection is not None:
                rejected += 1
                continue
            accepted.setdefault(" ".join(_tokens(display)), display)
        if len(accepted) != 1:
            rejected += len(accepted) if len(accepted) > 1 else 0
            results[span.span_id] = base
            continue
        display = next(iter(accepted.values()))
        state = (
            SemanticRequirementState.SCORABLE
            if semantic.criterion_type is not None
            else SemanticRequirementState.NEEDS_HUMAN_REVIEW
        )
        results[span.span_id] = CanonicalRequirement(
            span_id=span.span_id,
            kind=semantic.criterion_family,
            canonical_subject=display,
            criterion_type=semantic.criterion_type,
            min_years=semantic.min_years,
            required_level=semantic.required_level,
            interpretation_state=state,
            interpretation_source=CanonicalInterpretationSource.MODEL_VALIDATED,
            review_reason=(
                None
                if state == SemanticRequirementState.SCORABLE
                else SemanticReviewReason.MODALITY_UNKNOWN
            ),
            shape_validated=True,
        )

    # Coordination symmetry: sides split from one coordinated clause share
    # authority. If any interpretable side is not scorable, no side is.
    groups: dict[int, list[str]] = {}
    for span in spans:
        if span.coordination_group is not None:
            groups.setdefault(span.coordination_group, []).append(span.span_id)
    for members in groups.values():
        interpretable = [
            results[span_id]
            for span_id in members
            if results[span_id].interpretation_state
            in (SemanticRequirementState.SCORABLE, SemanticRequirementState.NEEDS_HUMAN_REVIEW)
        ]
        if len(interpretable) < 2 or all(
            member.interpretation_state == SemanticRequirementState.SCORABLE
            for member in interpretable
        ):
            continue
        for member in interpretable:
            if member.interpretation_state == SemanticRequirementState.SCORABLE:
                results[member.span_id] = member.model_copy(
                    update={
                        "interpretation_state": SemanticRequirementState.NEEDS_HUMAN_REVIEW,
                        "interpretation_source": CanonicalInterpretationSource.NONE,
                        "review_reason": SemanticReviewReason.COORDINATION_SYMMETRY,
                        "shape_validated": False,
                    }
                )

    return CanonicalizationResult(
        requirements=[results[span.span_id] for span in spans],
        ungrounded_proposal_count=ungrounded,
        rejected_proposal_count=rejected,
    )
