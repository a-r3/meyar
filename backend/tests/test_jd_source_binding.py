"""Issue #44 adversarial coverage for the real JD draft boundary.

These tests exercise _dispatch_draft_job_criteria, the production
LLM-draft -> deterministic-validation -> review-result orchestration step.
They intentionally do not test private lexical helpers in isolation.
"""

import asyncio

import pytest
from fakes import FakeLLMProvider

from meyar.agent.jd_authority import segment_requirement_spans
from meyar.agent.schemas import (
    MAX_JD_REQUIREMENT_SPANS,
    JDCriteriaDraft,
    JDDraftCriterionItem,
    JDDraftCriterionKind,
    RequirementSpanState,
)
from meyar.agent.service import _dispatch_draft_job_criteria
from meyar.schemas.criteria import CriterionKind


def _draft(source: str, *, must=(), preferred=()):
    result = asyncio.run(
        _dispatch_draft_job_criteria(
            FakeLLMProvider(
                jd_draft=JDCriteriaDraft(
                    title="Role", must_have=list(must), preferred=list(preferred)
                )
            ),
            jd_text=source,
        )
    )
    assert result is not None and result.job_draft is not None
    return result.job_draft


def _span_id(source: str, occurrence: int = 0) -> str:
    return segment_requirement_spans(source)[occurrence].span_id


def test_composite_skill_cannot_borrow_one_source_token() -> None:
    draft = _draft(
        "Python required",
        must=[
            JDDraftCriterionItem(
                span_id="req-0001",
                kind="SKILL", requirement="Python Kubernetes", source_text="Python required"
            )
        ],
    )
    assert draft.must_have == []
    assert [item.requirement for item in draft.needs_review] == ["Python required"]


def test_skill_cannot_gain_invented_min_years() -> None:
    draft = _draft(
        "Python experience required",
        must=[
            JDDraftCriterionItem(
                span_id="req-0001",
                kind="SKILL",
                requirement="Python",
                source_text="Python experience required",
                min_years=20,
            )
        ],
    )
    assert draft.must_have == []
    assert [item.requirement for item in draft.needs_review] == ["Python experience required"]


def test_numeric_years_cannot_transfer_between_requirements() -> None:
    draft = _draft(
        "Python required. 5 years total experience required.",
        must=[
            JDDraftCriterionItem(
                span_id="req-0001",
                kind="SKILL_EXPERIENCE",
                requirement="Python",
                source_text="Python required",
                min_years=5,
            )
        ],
    )
    assert draft.must_have == []
    assert {item.requirement for item in draft.needs_review} == {
        "Python required",
        "5 years total experience required",
    }


def test_skill_duration_cannot_be_weakened_to_total_experience_plus_skill() -> None:
    source = "5 years of Python experience required"
    draft = _draft(
        source,
        must=[
            JDDraftCriterionItem(
                span_id="req-0001",
                kind="EXPERIENCE", requirement="experience", source_text=source, min_years=5
            ),
            JDDraftCriterionItem(
                span_id="req-0001", kind="SKILL", requirement="Python", source_text=source
            ),
        ],
    )
    assert draft.must_have == []
    assert [item.requirement for item in draft.needs_review] == [source]


def test_language_level_cannot_be_silently_discarded() -> None:
    source = "English B2 required"
    draft = _draft(
        source,
        must=[
            JDDraftCriterionItem(
                span_id="req-0001", kind="LANGUAGE", requirement="English", source_text=source
            )
        ],
    )
    assert draft.must_have == []
    assert [item.requirement for item in draft.needs_review] == [source]


def test_raw_sensitive_requirement_is_blocked_even_when_model_omits_it() -> None:
    draft = _draft("Female required")
    assert draft.must_have == []
    assert draft.unsupported == []
    assert draft.needs_review == []
    assert draft.prohibited_count == 1


def test_other_classification_cannot_bypass_sensitive_policy() -> None:
    draft = _draft(
        "Female required",
        must=[
            JDDraftCriterionItem(
                span_id="req-0001",
                kind="OTHER",
                requirement="Female",
                source_text="Female required",
            )
        ],
    )
    assert draft.unsupported == []
    assert draft.needs_review == []
    assert draft.prohibited_count == 1


def test_rephrased_sensitive_requirement_is_blocked_post_parse() -> None:
    source = "Women applicants required"
    draft = _draft(
        source,
        must=[
            JDDraftCriterionItem(
                span_id="req-0001",
                kind="OTHER",
                requirement="women applicants",
                source_text=source,
            )
        ],
    )
    assert draft.unsupported == []
    assert draft.prohibited_count == 1


def test_unsupported_source_requirement_remains_visible_and_unscored() -> None:
    source = "Candidate must be willing to travel"
    draft = _draft(
        source,
        must=[
            JDDraftCriterionItem(
                span_id="req-0001",
                kind="OTHER",
                requirement="willing to travel",
                source_text=source,
            )
        ],
    )
    assert draft.must_have == []
    assert [item.requirement for item in draft.unsupported] == [source]


def test_model_omission_cannot_delete_material_source_requirement() -> None:
    source = "Candidate must be willing to travel"
    draft = _draft(source)
    assert [item.requirement for item in draft.needs_review] == [source]


def test_valid_source_grounded_skill_still_works() -> None:
    draft = _draft(
        "Python required",
        must=[
            JDDraftCriterionItem(
                span_id="req-0001",
                kind="SKILL",
                requirement="Python",
                source_text="Python required",
            )
        ],
    )
    assert [(item.kind, item.value, item.weight) for item in draft.must_have] == [
        (CriterionKind.SKILL, "Python", 1.0)
    ]
    assert draft.needs_review == []


def test_valid_required_preferred_distinction_still_works() -> None:
    draft = _draft(
        "Python required. SQL preferred.",
        must=[
            JDDraftCriterionItem(
                span_id="req-0001",
                kind="SKILL",
                requirement="Python",
                source_text="Python required",
            )
        ],
        preferred=[
            JDDraftCriterionItem(
                kind="SKILL",
                requirement="SQL",
                span_id="req-0002",
                source_text="SQL preferred",
            )
        ],
    )
    assert [item.value for item in draft.must_have] == ["Python"]
    assert [item.value for item in draft.preferred] == ["SQL"]


def test_preferred_requirement_cannot_be_strengthened_to_must_have() -> None:
    draft = _draft(
        "Python preferred",
        must=[
            JDDraftCriterionItem(
                span_id="req-0001",
                kind="SKILL",
                requirement="Python",
                source_text="Python preferred",
            )
        ],
    )
    assert draft.must_have == []
    assert [item.requirement for item in draft.needs_review] == ["Python preferred"]


def test_kind_must_be_supported_by_the_source_fragment() -> None:
    draft = _draft(
        "Python required",
        must=[
            JDDraftCriterionItem(
                span_id="req-0001",
                kind="CERTIFICATION", requirement="Python", source_text="Python required"
            )
        ],
    )
    assert draft.must_have == []
    assert [item.requirement for item in draft.needs_review] == ["Python required"]


def test_requirement_scope_cannot_be_silently_narrowed() -> None:
    source = "Python for data analysis required"
    draft = _draft(
        source,
        must=[
            JDDraftCriterionItem(
                span_id="req-0001", kind="SKILL", requirement="Python", source_text=source
            )
        ],
    )
    assert draft.must_have == []
    assert [item.requirement for item in draft.needs_review] == [source]


def test_valid_supported_numeric_requirements_keep_their_scope() -> None:
    general = _draft(
        "5 years total experience required",
        must=[
            JDDraftCriterionItem(
                span_id="req-0001",
                kind="EXPERIENCE",
                requirement="total experience",
                source_text="5 years total experience required",
                min_years=5,
            )
        ],
    )
    assert [(item.kind, item.min_years) for item in general.must_have] == [
        (CriterionKind.EXPERIENCE, 5.0)
    ]

    scoped = _draft(
        "5 years Python experience required",
        must=[
            JDDraftCriterionItem(
                span_id="req-0001",
                kind="SKILL_EXPERIENCE",
                requirement="Python",
                source_text="5 years Python experience required",
                min_years=5,
            )
        ],
    )
    assert scoped.must_have == []
    assert [item.requirement for item in scoped.unsupported] == [
        "5 years Python experience required"
    ]


def test_valid_language_level_is_preserved() -> None:
    source = "English B2 required"
    draft = _draft(
        source,
        must=[
            JDDraftCriterionItem(
                span_id="req-0001",
                kind=JDDraftCriterionKind.LANGUAGE,
                requirement="English",
                source_text=source,
                required_level="B2",
            )
        ],
    )
    assert draft.must_have == []
    assert [item.requirement for item in draft.unsupported] == [source]


def test_partial_model_source_cannot_weaken_complete_canonical_requirement() -> None:
    source = "5 years Python experience required"
    draft = _draft(
        source,
        must=[
            JDDraftCriterionItem(
                kind="SKILL",
                requirement="Python",
                span_id=_span_id(source),
                source_text="Python experience required",
            )
        ],
    )
    assert draft.must_have == []
    assert [item.requirement for item in draft.needs_review] == [source]


def test_prefix_collisions_never_authorize_a_different_skill() -> None:
    for source_subject, drafted_subject in (
        ("JavaScript", "Java"),
        ("Java", "JavaScript"),
        ("C++", "C"),
        ("C", "C++"),
        ("SQL", "NoSQL"),
        ("NoSQL", "SQL"),
        ("Django", "Go"),
        ("Go", "Django"),
    ):
        source = f"{source_subject} required"
        draft = _draft(
            source,
            must=[
                JDDraftCriterionItem(
                    kind="SKILL",
                    requirement=drafted_subject,
                    span_id=_span_id(source),
                    source_text=source,
                )
            ],
        )
        assert draft.must_have == [], (source_subject, drafted_subject)
        assert [item.requirement for item in draft.needs_review] == [source]


def test_domain_experience_cannot_be_weakened_to_plain_skill() -> None:
    source = "Banking experience preferred"
    draft = _draft(
        source,
        preferred=[
            JDDraftCriterionItem(
                span_id="req-0001", kind="SKILL", requirement="Banking", source_text=source
            )
        ],
    )
    assert draft.preferred == []
    assert [item.requirement for item in draft.needs_review] == [source]


def test_azerbaijani_inflected_sensitive_terms_are_prohibited_when_omitted() -> None:
    for source in ("Sağlamlığı tələb olunur", "Əlilliyi tələb olunur"):
        draft = _draft(source)
        assert draft.must_have == []
        assert draft.needs_review == []
        assert draft.prohibited_count == 1


def test_is_a_plus_requirement_cannot_disappear_when_model_omits_it() -> None:
    source = "Python is a plus"
    draft = _draft(source)
    assert draft.preferred == []
    assert [item.requirement for item in draft.needs_review] == [source]


@pytest.mark.parametrize(
    ("source_subject", "drafted_subject"),
    (
        ("py", "Python"),
        ("Python", "py"),
        ("k8s", "Kubernetes"),
        ("Kubernetes", "k8s"),
        ("Postgres", "PostgreSQL"),
        ("PostgreSQL", "Postgres"),
    ),
)
def test_curated_complete_subject_aliases_are_authorized(
    source_subject: str, drafted_subject: str
) -> None:
    source = f"{source_subject} required"
    draft = _draft(
        source,
        must=[
            JDDraftCriterionItem(
                kind="SKILL",
                requirement=drafted_subject,
                span_id=_span_id(source),
                source_text="fabricated hint is ignored",
            )
        ],
    )
    assert [criterion.value for criterion in draft.must_have] == [drafted_subject]
    assert [result.state for result in draft.requirements] == [RequirementSpanState.SCORABLE]


def test_domain_experience_with_compatible_kind_stays_visible_unscored() -> None:
    source = "Banking experience preferred"
    draft = _draft(
        source,
        preferred=[
            JDDraftCriterionItem(
                kind="DOMAIN_EXPERIENCE",
                requirement="Banking",
                span_id=_span_id(source),
                source_text=source,
            )
        ],
    )
    assert draft.preferred == []
    assert [item.requirement for item in draft.unsupported] == [source]
    assert [result.state for result in draft.requirements] == [RequirementSpanState.UNSUPPORTED]


def test_required_requirement_cannot_be_weakened_to_preferred() -> None:
    source = "Python required"
    draft = _draft(
        source,
        preferred=[
            JDDraftCriterionItem(
                kind="SKILL",
                requirement="Python",
                span_id=_span_id(source),
                source_text=source,
            )
        ],
    )
    assert draft.preferred == []
    assert [item.requirement for item in draft.needs_review] == [source]


def test_is_a_plus_is_bounded_preferred_modality() -> None:
    source = "Python is a plus"
    draft = _draft(
        source,
        preferred=[
            JDDraftCriterionItem(
                kind="SKILL",
                requirement="Python",
                span_id=_span_id(source),
                source_text=source,
            )
        ],
    )
    assert [criterion.value for criterion in draft.preferred] == ["Python"]
    assert draft.needs_review == []


@pytest.mark.parametrize("subject", ("Python", "Kubernetes"))
def test_is_a_plus_requires_a_source_bound_professional_subject(subject: str) -> None:
    source = f"{subject} is a plus"
    draft = _draft(
        source,
        preferred=[
            JDDraftCriterionItem(
                kind="SKILL",
                requirement=subject,
                span_id=_span_id(source),
                source_text="untrusted",
            )
        ],
    )
    assert [criterion.value for criterion in draft.preferred] == [subject]
    assert [result.state for result in draft.requirements] == [RequirementSpanState.SCORABLE]


@pytest.mark.parametrize(
    ("source", "drafted"),
    (
        ("Price is plus VAT", "Price VAT"),
        ("Salary is plus bonus", "Salary bonus"),
        ("2 + 2 is plus 4", "2 2 4"),
    ),
)
def test_is_plus_non_preference_controls_never_score(source: str, drafted: str) -> None:
    draft = _draft(
        source,
        preferred=[
            JDDraftCriterionItem(
                kind="SKILL",
                requirement=drafted,
                span_id=_span_id(source),
                source_text=source,
            )
        ],
    )
    assert draft.preferred == []
    assert [result.state for result in draft.requirements] == [
        RequirementSpanState.NEEDS_HUMAN_REVIEW
    ]


def test_implicit_modality_fails_closed_to_review() -> None:
    source = "- Python"
    spans = segment_requirement_spans(source)
    assert len(spans) == 1
    draft = _draft(
        source,
        must=[
            JDDraftCriterionItem(
                kind="SKILL",
                requirement="Python",
                span_id=spans[0].span_id,
                source_text=source,
            )
        ],
    )
    assert draft.must_have == []
    assert [item.requirement for item in draft.needs_review] == ["Python"]


@pytest.mark.parametrize("source", ("Python", "English B2", "Banking experience"))
def test_standalone_implicit_professional_item_is_visible_for_review(source: str) -> None:
    draft = _draft(source)
    assert draft.must_have == [] and draft.preferred == []
    assert [item.requirement for item in draft.needs_review] == [source]
    assert [result.state for result in draft.requirements] == [
        RequirementSpanState.NEEDS_HUMAN_REVIEW
    ]


def test_descriptive_prose_is_not_falsely_reconciled_as_a_requirement() -> None:
    source = "Our team uses Python to build reliable services and collaborates every day."
    draft = _draft(source)
    assert draft.requirements == []
    assert draft.must_have == [] and draft.preferred == []
    assert draft.needs_review == []


def test_repeated_implicit_and_explicit_items_keep_distinct_occurrences() -> None:
    source = "Python\nPython required"
    spans = segment_requirement_spans(source)
    assert len(spans) == 2 and spans[0].span_id != spans[1].span_id
    draft = _draft(
        source,
        must=[
            JDDraftCriterionItem(
                kind="SKILL",
                requirement="Python",
                span_id=spans[1].span_id,
                source_text="Python required",
            )
        ],
    )
    assert [result.state for result in draft.requirements] == [
        RequirementSpanState.NEEDS_HUMAN_REVIEW,
        RequirementSpanState.SCORABLE,
    ]


def test_negated_requirement_phrase_does_not_create_a_requirement() -> None:
    draft = _draft("No Python requirement")
    assert draft.must_have == [] and draft.preferred == []
    assert draft.requirements == []


@pytest.mark.parametrize(
    "source",
    (
        "Sağlamlıq tələb olunur",
        "Sağlamlığı tələb olunur",
        "Əlillik tələb olunur",
        "Əlilliyi tələb olunur",
        "Yaş tələb olunur",
        "Yaşı tələb olunur",
        "Milliyyət tələb olunur",
        "Milliyyəti tələb olunur",
        "Qadın tələb olunur",
        "Qadınlar tələb olunur",
    ),
)
def test_azerbaijani_protected_lexeme_families_are_blocked(source: str) -> None:
    draft = _draft(source)
    assert draft.prohibited_count == 1
    assert draft.must_have == []
    assert draft.needs_review == []


def test_azerbaijani_inflection_cannot_survive_other_classification() -> None:
    source = "Sağlamlığı tələb olunur"
    draft = _draft(
        source,
        must=[
            JDDraftCriterionItem(
                kind="OTHER",
                requirement="Sağlamlığı",
                span_id=_span_id(source),
                source_text=source,
            )
        ],
    )
    assert draft.prohibited_count == 1
    assert draft.unsupported == [] and draft.needs_review == []


@pytest.mark.parametrize(
    "source", ("Yaşıl texnologiya təcrübəsi üstünlükdür", "Sağlamlaşdırma bacarığı tələb olunur")
)
def test_azerbaijani_protected_lexeme_matching_has_safe_controls(source: str) -> None:
    assert _draft(source).prohibited_count == 0


def test_repeated_identical_occurrences_reconcile_by_span_id() -> None:
    source = "Python required. Python required."
    spans = segment_requirement_spans(source)
    assert len(spans) == 2 and spans[0].span_id != spans[1].span_id
    draft = _draft(
        source,
        must=[
            JDDraftCriterionItem(
                kind="SKILL",
                requirement="Python",
                span_id=spans[1].span_id,
                source_text="Python required",
            )
        ],
    )
    assert [criterion.value for criterion in draft.must_have] == ["Python"]
    assert [result.state for result in draft.requirements] == [
        RequirementSpanState.NEEDS_HUMAN_REVIEW,
        RequirementSpanState.SCORABLE,
    ]
    assert [item.requirement for item in draft.needs_review] == ["Python required"]


def test_same_sentence_requirements_split_only_with_independent_modalities() -> None:
    source = "Python required and SQL required"
    spans = segment_requirement_spans(source)
    draft = _draft(
        source,
        must=[
            JDDraftCriterionItem(
                kind="SKILL", requirement="Python", span_id=spans[0].span_id, source_text="x"
            ),
            JDDraftCriterionItem(
                kind="SKILL", requirement="SQL", span_id=spans[1].span_id, source_text="y"
            ),
        ],
    )
    assert [criterion.value for criterion in draft.must_have] == ["Python", "SQL"]
    assert all(result.state == RequirementSpanState.SCORABLE for result in draft.requirements)


def test_ambiguous_combined_clause_is_kept_complete_for_review() -> None:
    source = "Python and SQL required"
    span = segment_requirement_spans(source)[0]
    assert span.text == source and span.segmentation_needs_review
    draft = _draft(
        source,
        must=[
            JDDraftCriterionItem(
                kind="SKILL", requirement="Python", span_id=span.span_id, source_text="Python"
            )
        ],
    )
    assert draft.must_have == []
    assert [item.requirement for item in draft.needs_review] == [source]


def test_segmentation_is_bounded_without_dropping_overflow_text() -> None:
    source = "\n".join(f"Skill{index} required" for index in range(70))
    spans = segment_requirement_spans(source)
    assert len(spans) == MAX_JD_REQUIREMENT_SPANS
    overflow = spans[-1]
    assert overflow.segmentation_needs_review
    assert overflow.text.startswith("Skill63 required")
    assert overflow.text.endswith("Skill69 required")
    assert source[overflow.start_offset : overflow.end_offset] == overflow.text


def test_unknown_span_id_and_fabricated_source_text_never_create_authority() -> None:
    source = "Python required"
    draft = _draft(
        source,
        must=[
            JDDraftCriterionItem(
                kind="SKILL",
                requirement="Kubernetes",
                span_id="req-9999",
                source_text="Kubernetes required",
            )
        ],
    )
    assert draft.must_have == []
    assert draft.ungrounded_count == 1
    assert [item.requirement for item in draft.needs_review] == [source]


def test_product_example_preserves_all_unsupported_semantics() -> None:
    source = (
        "Senior Backend Developer axtarırıq.\n"
        "Minimum 5 il Python,\n"
        "B2 English,\n"
        "bank təcrübəsi üstünlükdür.\n"
        "10 nəfər namizəd göstər."
    )
    spans = segment_requirement_spans(source)
    draft = _draft(
        source,
        must=[
            JDDraftCriterionItem(
                kind="SKILL_EXPERIENCE",
                requirement="Python",
                min_years=5,
                span_id=spans[0].span_id,
                source_text=spans[0].text,
            ),
            JDDraftCriterionItem(
                kind="LANGUAGE",
                requirement="English",
                required_level="B2",
                span_id=spans[1].span_id,
                source_text=spans[1].text,
            ),
        ],
        preferred=[
            JDDraftCriterionItem(
                kind="DOMAIN_EXPERIENCE",
                requirement="Banking",
                span_id=spans[2].span_id,
                source_text=spans[2].text,
            )
        ],
    )
    assert draft.must_have == [] and draft.preferred == []
    assert [item.requirement for item in draft.unsupported] == [
        "Minimum 5 il Python",
        "bank təcrübəsi üstünlükdür",
    ]
    assert [item.requirement for item in draft.needs_review] == ["B2 English"]
    assert len(draft.requirements) == 3
