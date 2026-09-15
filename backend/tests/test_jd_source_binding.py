"""Issue #44 adversarial coverage for the real JD draft boundary.

These tests exercise _dispatch_draft_job_criteria, the production
LLM-draft -> deterministic-validation -> review-result orchestration step.
They intentionally do not test private lexical helpers in isolation.
"""

import asyncio

from fakes import FakeLLMProvider

from meyar.agent.schemas import JDCriteriaDraft, JDDraftCriterionItem, JDDraftCriterionKind
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


def test_composite_skill_cannot_borrow_one_source_token() -> None:
    draft = _draft(
        "Python required",
        must=[
            JDDraftCriterionItem(
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
                kind="EXPERIENCE", requirement="experience", source_text=source, min_years=5
            ),
            JDDraftCriterionItem(kind="SKILL", requirement="Python", source_text=source),
        ],
    )
    assert draft.must_have == []
    assert [item.requirement for item in draft.needs_review] == [source]


def test_language_level_cannot_be_silently_discarded() -> None:
    source = "English B2 required"
    draft = _draft(
        source,
        must=[JDDraftCriterionItem(kind="LANGUAGE", requirement="English", source_text=source)],
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
            JDDraftCriterionItem(kind="OTHER", requirement="Female", source_text="Female required")
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
            JDDraftCriterionItem(kind="OTHER", requirement="women applicants", source_text=source)
        ],
    )
    assert draft.unsupported == []
    assert draft.prohibited_count == 1


def test_unsupported_source_requirement_remains_visible_and_unscored() -> None:
    source = "Candidate must be willing to travel"
    draft = _draft(
        source,
        must=[
            JDDraftCriterionItem(kind="OTHER", requirement="willing to travel", source_text=source)
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
            JDDraftCriterionItem(kind="SKILL", requirement="Python", source_text="Python required")
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
            JDDraftCriterionItem(kind="SKILL", requirement="Python", source_text="Python required")
        ],
        preferred=[
            JDDraftCriterionItem(kind="SKILL", requirement="SQL", source_text="SQL preferred")
        ],
    )
    assert [item.value for item in draft.must_have] == ["Python"]
    assert [item.value for item in draft.preferred] == ["SQL"]


def test_preferred_requirement_cannot_be_strengthened_to_must_have() -> None:
    draft = _draft(
        "Python preferred",
        must=[
            JDDraftCriterionItem(kind="SKILL", requirement="Python", source_text="Python preferred")
        ],
    )
    assert draft.must_have == []
    assert [item.requirement for item in draft.needs_review] == ["Python preferred"]


def test_kind_must_be_supported_by_the_source_fragment() -> None:
    draft = _draft(
        "Python required",
        must=[
            JDDraftCriterionItem(
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
        must=[JDDraftCriterionItem(kind="SKILL", requirement="Python", source_text=source)],
    )
    assert draft.must_have == []
    assert [item.requirement for item in draft.needs_review] == [source]


def test_valid_supported_numeric_requirements_keep_their_scope() -> None:
    general = _draft(
        "5 years total experience required",
        must=[
            JDDraftCriterionItem(
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
                kind=JDDraftCriterionKind.LANGUAGE,
                requirement="English",
                source_text=source,
                required_level="B2",
            )
        ],
    )
    assert draft.must_have == []
    assert [item.requirement for item in draft.unsupported] == [source]
