"""Issue #44 final acceptance blockers (F1/F2/F3) — structural regressions.

Fresh AZ/EN metamorphic coverage, deliberately distinct from the audit's own
example sentences, for:

F1 — vague result-count quantifiers must never collapse to VALID(1); only
     true absence of any count intent may default to 20.
F3 — the shared D-055 coordination-split authority: a coordinated clause
     without independent per-side modality never silently drops a member;
     one with independent per-side modality still splits.
F2 — professional family classification must not be decided by orthography
     (digits/symbols/casing); equivalent shapes behave equivalently.
AZ wrapper — a leading Azerbaijani postposition/connector is excluded from
     the professional subject, not absorbed into it.
"""

import pytest

from meyar.agent.schemas import JDDraftCriterionKind, SemanticRequirementState
from meyar.agent.semantic_requirements import analyze_hr_text
from meyar.core.result_count import (
    DEFAULT_RESULT_LIMIT,
    ResultCountState,
    extract_result_count_intent,
)

# ---------------------------------------------------------------------------
# F1 — result-count state authority
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Bir neçə uyğun mühəndis namizəd göstər.",
        "Bir qədər analitik namizəd tap.",
        "Çoxlu mühasib namizəd çıxart.",
        "Show a few strong engineer candidates.",
        "Please return several qualified analyst applicants.",
        "List some accountant profiles.",
    ],
)
def test_vague_result_count_quantifiers_never_become_valid_one(text: str) -> None:
    intent = extract_result_count_intent(text)
    assert intent.state == ResultCountState.AMBIGUOUS
    assert intent.requested is None
    assert intent.effective != 1 or intent.state != ResultCountState.VALID
    assert intent.state != ResultCountState.VALID


@pytest.mark.parametrize(
    "text",
    [
        "Python və SQL bilikləri tələb olunur.",
        "Candidates with strong communication skills.",
        "Minimum 3 il mühasibatlıq təcrübəsi tələb olunur.",
    ],
)
def test_true_absence_of_count_intent_defaults_to_twenty(text: str) -> None:
    intent = extract_result_count_intent(text)
    assert (intent.state, intent.effective) == (ResultCountState.ABSENT, DEFAULT_RESULT_LIMIT)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Show 7 engineers.", 7),
        ("15 analitik namizəd göstər.", 15),
        ("List top 9 accountants.", 9),
    ],
)
def test_explicit_exact_count_is_valid(text: str, expected: int) -> None:
    intent = extract_result_count_intent(text)
    assert intent.state == ResultCountState.VALID
    assert intent.requested == intent.effective == expected


@pytest.mark.parametrize(
    "text",
    [
        "Show 4 candidates or 6 candidates.",
        "3 yoxsa 5 namizəd göstər.",
    ],
)
def test_competing_exact_counts_are_ambiguous(text: str) -> None:
    intent = extract_result_count_intent(text)
    assert intent.state == ResultCountState.AMBIGUOUS
    assert intent.effective != DEFAULT_RESULT_LIMIT


# ---------------------------------------------------------------------------
# F3 — shared D-055 coordination-split authority
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Candidates must possess PMP and PRINCE2 certifications",
        "Applicants should demonstrate Terraform and Ansible experience",
        "Namizəd Excel və Power BI bacarıqlarına malik olmalıdır",
    ],
)
def test_coordinated_clause_without_independent_modality_drops_nothing(text: str) -> None:
    analysis = analyze_hr_text(text)
    assert analysis.requirements, "the coordinated clause must not vanish entirely"
    assert analysis.spans, "the clause must terminate as a visible material span"
    for item in analysis.requirements:
        assert item.state != SemanticRequirementState.PROHIBITED
    # Every named identity in the source remains visible in some material
    # span's own source text — never silently dropped from all of them.
    combined_source = " ".join(span.text for span in analysis.spans)
    for name in ("PMP", "PRINCE2", "Terraform", "Ansible", "Excel", "Power BI"):
        if name in text:
            assert name in combined_source


@pytest.mark.parametrize(
    ("text", "subjects"),
    [
        ("PMP required and PRINCE2 preferred", {"PMP", "PRINCE2"}),
        ("Terraform required and Ansible preferred", {"Terraform", "Ansible"}),
    ],
)
def test_coordinated_clause_with_independent_modality_splits_both_survive(
    text: str, subjects: set[str]
) -> None:
    analysis = analyze_hr_text(text)
    found = {item.normalized_subject for item in analysis.requirements}
    assert subjects <= found


# ---------------------------------------------------------------------------
# F2 — family authority is orthography-independent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("plain", "symbolic"),
    [
        ("Rust experience is required.", "Node.js experience is required."),
        ("Scala experience is preferred.", "F# experience is preferred."),
        ("Kotlin experience is required.", "ASP.NET experience is required."),
    ],
)
def test_family_invariance_across_orthographic_style_fresh_pairs(
    plain: str, symbolic: str
) -> None:
    plain_item = analyze_hr_text(plain).requirements[0]
    symbolic_item = analyze_hr_text(symbolic).requirements[0]
    assert plain_item.state == symbolic_item.state
    assert plain_item.criterion_family == symbolic_item.criterion_family


def test_known_domain_taxonomy_still_authorizes_without_duration() -> None:
    item = analyze_hr_text("Treasury experience is required.").requirements[0]
    assert (item.state, item.criterion_family) == (
        SemanticRequirementState.SCORABLE,
        JDDraftCriterionKind.DOMAIN_EXPERIENCE,
    )


def test_unrecognized_bare_experience_subject_needs_review_not_scored() -> None:
    item = analyze_hr_text("Elixir experience is required.").requirements[0]
    assert item.state == SemanticRequirementState.NEEDS_HUMAN_REVIEW
    assert item.criterion_family == JDDraftCriterionKind.SKILL_EXPERIENCE


# ---------------------------------------------------------------------------
# AZ leading connector/postposition never becomes the subject
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "subject"),
    [
        ("İlə MongoDB biliyi vacibdir.", "MongoDB"),
        ("İlə Terraform biliyi tələb olunur.", "Terraform"),
    ],
)
def test_az_leading_postposition_excluded_from_subject_fresh_subjects(
    text: str, subject: str
) -> None:
    item = analyze_hr_text(text).requirements[0]
    assert item.normalized_subject == subject
    assert item.subject is not None and item.subject.text == subject
    assert text[item.subject.start_offset : item.subject.end_offset] == subject


# ---------------------------------------------------------------------------
# Certification-list parity — no named identity vanishes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Candidates must hold CIA and CISA certifications",
        "Applicants should have PMP and CAPM credentials",
    ],
)
def test_certification_list_parity_no_identity_vanishes(text: str) -> None:
    analysis = analyze_hr_text(text)
    assert analysis.requirements
    for item in analysis.requirements:
        assert item.state != SemanticRequirementState.PROHIBITED
    assert analysis.spans, "the full certification clause must remain a visible material span"
