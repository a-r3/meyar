"""Issue #44 structural AZ/EN semantic-boundary regressions."""

import asyncio

import pytest
from fakes import FakeLLMProvider
from pydantic import ValidationError

from meyar.agent.schemas import (
    JDCriteriaDraft,
    JDDraftCriterionItem,
    JDDraftCriterionKind,
    RequirementSpanState,
    SemanticRequirementState,
    SupportedInputLanguage,
)
from meyar.agent.semantic_requirements import analyze_hr_text
from meyar.agent.service import _apply_pending_draft_followup, _dispatch_draft_job_criteria
from meyar.schemas.criteria import CriterionIn, CriterionKind, CriterionType, find_prohibited_term


def _shape(text: str):
    analysis = analyze_hr_text(text)
    return analysis, [
        (
            item.state,
            item.criterion_family,
            item.normalized_subject,
            item.criterion_type,
            item.min_years,
            item.required_level,
        )
        for item in analysis.requirements
    ]


@pytest.mark.parametrize(
    "text",
    [
        "Namizəd qadın olmalıdır.",
        "Namizəd 30 yaşdan aşağı olmalıdır.",
        "Namizəd Azərbaycan vətəndaşı olmalıdır.",
        "Namizəd tam sağlam olmalıdır.",
        "Əlilliyi olmayan namizəd tələb olunur.",
        "Candidate must be female.",
        "Candidate must be under 30 years old.",
        "Candidate must have Azerbaijani nationality.",
        "Candidate must have no health condition.",
        "Candidate must not have a disability.",
    ],
)
def test_supported_protected_classes_are_blocked_before_and_after_parse(text: str) -> None:
    assert find_prohibited_term(text) is not None
    analysis = analyze_hr_text(text)
    assert analysis.requirements
    assert {item.state for item in analysis.requirements} == {SemanticRequirementState.PROHIBITED}


def test_azerbaijani_nationality_can_never_become_a_scorable_language() -> None:
    text = "Namizəd Azərbaycan vətəndaşı olmalıdır."
    analysis = analyze_hr_text(text)
    assert analysis.requirements[0].criterion_family == JDDraftCriterionKind.LANGUAGE
    assert analysis.requirements[0].state == SemanticRequirementState.PROHIBITED

    result = asyncio.run(
        _dispatch_draft_job_criteria(
            FakeLLMProvider(jd_draft=JDCriteriaDraft(title="Rol")), jd_text=text
        )
    )
    assert result is not None and result.job_draft is not None
    draft = result.job_draft
    assert draft.must_have == []
    assert draft.preferred == []
    assert draft.prohibited_count == 1
    assert draft.requirements[0].state == RequirementSpanState.PROHIBITED
    assert draft.requirements[0].text is None


@pytest.mark.parametrize(
    ("text", "subject", "family"),
    [
        ("Snowflake üzrə minimum 4 il təcrübə tələb olunur.", "Snowflake", "SKILL_EXPERIENCE"),
        ("Temenos T24 təcrübəsi üstünlükdür.", "Temenos T24", "SKILL_EXPERIENCE"),
        ("SAP S/4HANA təcrübəsi tələb olunur.", "SAP S/4HANA", "SKILL_EXPERIENCE"),
        ("Oracle FLEXCUBE təcrübəsi tələb olunur.", "Oracle FLEXCUBE", "SKILL_EXPERIENCE"),
        ("ACCA sertifikatı üstünlükdür.", "ACCA", "CERTIFICATION"),
        ("AWS certification required.", "AWS", "CERTIFICATION"),
    ],
)
def test_language_level_wrappers_preserve_novel_subject_occurrence(
    text: str, subject: str, family: str
) -> None:
    analysis = analyze_hr_text(text)
    item = analysis.requirements[0]
    assert item.normalized_subject == subject
    assert item.criterion_family.value == family
    assert item.subject is not None and item.subject.text == subject
    assert text[item.subject.start_offset : item.subject.end_offset] == subject


@pytest.mark.parametrize(
    "text",
    [
        "Python üzrə minimum 5 il təcrübə tələb olunur.",
        "Python üzrə ən azı 5 il təcrübə tələb olunur.",
        "5+ il Python təcrübəsi mütləqdir.",
        "Python üzrə beş il təcrübə tələb olunur.",
        "Python təcrübəsi 5 ildən az olmasın.",
        "at least five years of Python experience is required",
        "Python experience must be 5+ years",
    ],
)
def test_number_words_digits_and_minimum_forms_share_exact_semantics(text: str) -> None:
    analysis = analyze_hr_text(text)
    item = analysis.requirements[0]
    assert (item.criterion_family, item.normalized_subject, item.min_years) == (
        JDDraftCriterionKind.SKILL_EXPERIENCE,
        "Python",
        5.0,
    )
    assert item.comparison == ">="
    assert item.criterion_type == CriterionType.MUST_HAVE


def test_strictly_more_than_is_not_weakened_to_at_least() -> None:
    item = analyze_hr_text("5 ildən çox Python təcrübəsi tələb olunur.").requirements[0]
    assert item.comparison == ">"
    assert item.state == SemanticRequirementState.UNSUPPORTED


@pytest.mark.parametrize(
    "text",
    [
        "Minimum 6 il ümumi təcrübə tələb olunur",
        "At least 7 years total experience required",
    ],
)
def test_total_experience_language_remains_general_experience(text: str) -> None:
    item = analyze_hr_text(text).requirements[0]
    assert item.criterion_family == JDDraftCriterionKind.EXPERIENCE
    assert item.state == SemanticRequirementState.SCORABLE


def test_duration_and_result_count_have_independent_source_slots() -> None:
    analysis = analyze_hr_text("10 il Python təcrübəsi olan 5 namizəd göstər.")
    assert analysis.result_count.requested == 5
    item = analysis.requirements[0]
    assert item.normalized_subject == "Python"
    assert item.min_years == 10
    assert item.duration_or_number is not None
    assert item.duration_or_number.text == "10 il"


def test_top_count_clause_never_becomes_a_professional_requirement() -> None:
    analysis = analyze_hr_text(
        "Minimum 5 years Python experience, English B2, banking təcrübəsi preferred. "
        "Top 10 göstər."
    )
    assert analysis.result_count.requested == 10
    assert all(item.normalized_subject != "Top" for item in analysis.requirements)


@pytest.mark.parametrize(
    ("text", "criterion_type", "state"),
    [
        ("Python mütləqdir.", CriterionType.MUST_HAVE, SemanticRequirementState.SCORABLE),
        ("Python tələb olunur.", CriterionType.MUST_HAVE, SemanticRequirementState.SCORABLE),
        ("Python olsa yaxşıdır.", CriterionType.PREFERRED, SemanticRequirementState.SCORABLE),
        ("Python arzuolunandır.", CriterionType.PREFERRED, SemanticRequirementState.SCORABLE),
        ("Java lazım deyil.", None, SemanticRequirementState.UNSUPPORTED),
        ("Java is not required.", None, SemanticRequirementState.UNSUPPORTED),
    ],
)
def test_modality_and_negation_are_source_bound(text: str, criterion_type, state) -> None:
    item = analyze_hr_text(text).requirements[0]
    assert item.criterion_type == criterion_type
    assert item.state == state
    assert item.modality is not None
    assert item.modality.text in text


def test_bullet_and_paragraph_forms_are_materially_equivalent() -> None:
    bullet = (
        "Senior Backend Developer\n- Python: minimum 5 years\n"
        "- English: B2 required\n- Banking experience: preferred\n"
        "- Kubernetes: preferred\nShow 10 candidates"
    )
    paragraph = (
        "Senior Backend Developer lazımdır. Minimum 5 il Python təcrübəsi və English B2 "
        "tələb olunur. Banking experience və Kubernetes üstünlükdür. 10 namizəd göstər."
    )
    left, left_shape = _shape(bullet)
    right, right_shape = _shape(paragraph)
    assert left.result_count.requested == right.result_count.requested == 10
    assert left_shape == right_shape


def test_supported_and_unsupported_requirements_keep_independent_states() -> None:
    analysis = analyze_hr_text(
        "Python minimum 5 il tələb olunur, English B2 tələb olunur, "
        "gecə növbəsində işləmək arzuolunandır, müştəri yönümlü olsun."
    )
    assert [item.state for item in analysis.requirements] == [
        SemanticRequirementState.SCORABLE,
        SemanticRequirementState.SCORABLE,
        SemanticRequirementState.UNSUPPORTED,
        SemanticRequirementState.UNSUPPORTED,
    ]


def test_russian_fails_closed_before_model_or_criteria() -> None:
    provider = FakeLLMProvider(jd_draft=JDCriteriaDraft(title="unused"))
    result = asyncio.run(
        _dispatch_draft_job_criteria(
            provider,
            jd_text="Нужен разработчик: минимум 5 лет Python. Покажи 10 кандидатов.",
        )
    )
    assert result is not None and result.job_draft is not None
    assert provider.jd_draft_call_count == 0
    assert result.job_draft.unsupported_language == SupportedInputLanguage.UNSUPPORTED
    assert result.job_draft.must_have == []
    assert result.job_draft.preferred == []
    assert result.job_draft.requirements == []


def test_mixed_az_en_professional_language_is_supported() -> None:
    analysis = analyze_hr_text(
        "minimum 5 years Python experience, English B2 required, banking təcrübəsi preferred"
    )
    assert analysis.language == SupportedInputLanguage.MIXED_AZ_EN
    assert all(item.state == SemanticRequirementState.SCORABLE for item in analysis.requirements)
    assert [item.normalized_subject for item in analysis.requirements] == [
        "Python",
        "English",
        "Banking",
    ]


def test_education_family_and_source_are_preserved() -> None:
    item = analyze_hr_text("Bachelor's degree required.").requirements[0]
    assert item.criterion_family == JDDraftCriterionKind.EDUCATION
    assert item.normalized_subject == "Bachelor's degree"
    assert item.subject is not None and item.subject.text == "Bachelor's degree"


def test_model_material_fields_never_override_server_slots() -> None:
    text = "Snowflake üzrə minimum 4 il təcrübə tələb olunur."
    provider = FakeLLMProvider(
        jd_draft=JDCriteriaDraft(
            title="Snowflake",
            must_have=[],
            preferred=[],
        )
    )
    result = asyncio.run(_dispatch_draft_job_criteria(provider, jd_text=text))
    assert result is not None and result.job_draft is not None
    criterion = result.job_draft.must_have[0]
    assert (criterion.kind, criterion.value, criterion.min_years) == (
        CriterionKind.SKILL_EXPERIENCE,
        "Snowflake",
        4.0,
    )


@pytest.mark.parametrize(
    ("followup", "assertion"),
    [
        ("10 yox, 5 nəfər göstər.", lambda draft: draft.result_limit == 5),
        (
            "English B2-ni C1 et.",
            lambda draft: (
                next(
                    item for item in draft.must_have if item.kind == CriterionKind.LANGUAGE
                ).required_level
                == "C1"
            ),
        ),
        (
            "Python 5 yox, minimum 3 il olsun.",
            lambda draft: (
                next(
                    item for item in draft.must_have if item.kind == CriterionKind.SKILL_EXPERIENCE
                ).min_years
                == 3
            ),
        ),
    ],
)
def test_pending_draft_followups_create_new_source_attributed_versions(
    followup: str, assertion
) -> None:
    initial_result = asyncio.run(
        _dispatch_draft_job_criteria(
            FakeLLMProvider(jd_draft=JDCriteriaDraft(title="Backend")),
            jd_text=(
                "Minimum 5 il Python təcrübəsi tələb olunur. English B2 tələb olunur. "
                "Bank təcrübəsi üstünlükdür. 10 nəfər göstər."
            ),
        )
    )
    assert initial_result is not None and initial_result.job_draft is not None
    initial = initial_result.job_draft
    modified = _apply_pending_draft_followup(initial, followup)
    assert modified is not None
    assert modified.draft_id != initial.draft_id
    assert modified.modification_source_text == followup
    assert assertion(modified)


def _generated_protected_cases() -> list[tuple[str, str]]:
    az_subjects = (
        "Namizədin vətəndaşlığı Azərbaycan olmalıdır",
        "Qırx yaşını keçməyən mütəxəssis tələb olunur",
        "İşçi tibbi cəhətdən yararlı olmalıdır",
        "Əlilliyi bulunmayan şəxs üstünlükdür",
        "Yalnız kişi namizədlər nəzərdən keçirilir",
    )
    en_subjects = (
        "The applicant must be below thirty-five",
        "A medically fit analyst is required",
        "Only able-bodied specialists are preferred",
        "Women only may apply",
        "The role requires a passport holder",
    )
    return [("AZ", text) for text in az_subjects] + [("EN", text) for text in en_subjects]


@pytest.mark.parametrize(("language", "text"), _generated_protected_cases())
@pytest.mark.parametrize("wrong_kind", list(JDDraftCriterionKind))
def test_generated_unseen_protected_concepts_never_score_even_with_wrong_model_kind(
    language: str, text: str, wrong_kind: JDDraftCriterionKind
) -> None:
    analysis = analyze_hr_text(text)
    assert analysis.requirements, (language, text)
    assert all(item.state == SemanticRequirementState.PROHIBITED for item in analysis.requirements)
    provider = FakeLLMProvider(
        jd_draft=JDCriteriaDraft(
            title="Role",
            must_have=[
                JDDraftCriterionItem(
                    kind=wrong_kind,
                    requirement="GraphQL",
                    span_id=analysis.spans[0].span_id,
                    source_text="GraphQL required",
                )
            ],
        )
    )
    result = asyncio.run(_dispatch_draft_job_criteria(provider, jd_text=text))
    assert result is not None and result.job_draft is not None
    assert provider.jd_draft_call_count == 0
    assert result.job_draft.must_have == result.job_draft.preferred == []
    assert result.job_draft.prohibited_count >= 1
    assert all(
        item.state == RequirementSpanState.PROHIBITED for item in result.job_draft.requirements
    )


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        (CriterionKind.LANGUAGE, "applicants are suitable"),
        (CriterionKind.SKILL, "five candidates"),
        (CriterionKind.CERTIFICATION, "3 certifications"),
        (CriterionKind.LANGUAGE, "medically fit"),
        (CriterionKind.SKILL, "below thirty years old"),
    ],
)
def test_typed_persistence_evaluator_boundary_rejects_non_professional_subjects(
    kind: CriterionKind, value: str
) -> None:
    with pytest.raises(ValidationError):
        CriterionIn(
            id="unsafe",
            kind=kind,
            type=CriterionType.MUST_HAVE,
            label=value,
            value=value,
        )


@pytest.mark.parametrize(
    ("text", "family", "subject", "state"),
    [
        ("ClickHouse biliyi tələb olunur", "SKILL", "ClickHouse", "SCORABLE"),
        (
            "Minimum 2 il Workday təcrübəsi tələb olunur",
            "SKILL_EXPERIENCE",
            "Workday",
            "SCORABLE",
        ),
        ("COBIT certification is advantageous", "CERTIFICATION", "COBIT", "SCORABLE"),
        ("Spanish dili B2 tələb olunur", "LANGUAGE", "Spanish", "SCORABLE"),
        (
            "Experience in retail lending domain is required",
            "DOMAIN_EXPERIENCE",
            "retail lending",
            "SCORABLE",
        ),
        (
            "Master's degree in Quantitative Finance required",
            "EDUCATION",
            "Master's degree in Quantitative Finance",
            "SCORABLE",
        ),
        (
            "CockroachDB experience is optional",
            "SKILL_EXPERIENCE",
            "CockroachDB",
            "NEEDS_HUMAN_REVIEW",
        ),
    ],
)
def test_unseen_professional_subject_spans_preserve_identity_and_family(
    text: str, family: str, subject: str, state: str
) -> None:
    item = analyze_hr_text(text).requirements[0]
    assert item.criterion_family.value == family
    assert item.normalized_subject == subject
    assert item.state.value == state
    assert item.subject is not None
    assert text[item.subject.start_offset : item.subject.end_offset] == subject


@pytest.mark.parametrize(
    "text",
    [
        "Neo4j biliyi üstünlükdür",
        "DuckDB knowledge is optional",
        "gRPC is nice to have",
        "Elasticsearch is not mandatory but preferred",
        "Redis məcburi deyil, amma üstünlükdür",
    ],
)
def test_natural_optional_preferred_material_is_never_omitted(text: str) -> None:
    analysis = analyze_hr_text(text)
    assert len(analysis.spans) == len(analysis.requirements) == 1
    assert analysis.requirements[0].criterion_type == CriterionType.PREFERRED
    assert analysis.requirements[0].state in {
        SemanticRequirementState.SCORABLE,
        SemanticRequirementState.NEEDS_HUMAN_REVIEW,
    }


@pytest.mark.parametrize(
    ("text", "limit", "duration", "level", "subject", "review_count"),
    [
        (
            "Return 6 candidates with at least 3 years of ClickHouse experience",
            6,
            3,
            None,
            "ClickHouse",
            0,
        ),
        (
            "12 namizəd göstər: 4 il Camunda təcrübəsi tələb olunur",
            12,
            4,
            None,
            "Camunda",
            0,
        ),
        ("List 9 applicants with Spanish language C1 required", 9, None, "C1", "Spanish", 0),
        (
            "Show 8 candidates with 2 years of Neo4j experience and 3 certifications",
            8,
            2,
            None,
            "Neo4j",
            1,
        ),
        (
            "Top 11 profiles; minimum 5 years of gRPC experience required",
            11,
            5,
            None,
            "gRPC",
            0,
        ),
    ],
)
def test_multi_number_result_limit_duration_level_and_quantity_are_isolated(
    text: str,
    limit: int,
    duration: int | None,
    level: str | None,
    subject: str,
    review_count: int,
) -> None:
    analysis = analyze_hr_text(text)
    assert analysis.result_count.requested == limit
    assert all(
        item.normalized_subject not in {str(limit), f"Top {limit}"}
        for item in analysis.requirements
    )
    durations = [item.min_years for item in analysis.requirements if item.min_years is not None]
    levels = [
        item.required_level
        for item in analysis.requirements
        if item.required_level is not None
    ]
    assert durations == ([] if duration is None else [float(duration)])
    assert levels == ([] if level is None else [level])
    assert subject in {item.normalized_subject for item in analysis.requirements}
    assert sum(
        item.state == SemanticRequirementState.NEEDS_HUMAN_REVIEW
        for item in analysis.requirements
    ) == review_count


def test_pending_followup_moves_modality_both_directions_without_mutating_other_fields() -> None:
    initial_result = asyncio.run(
        _dispatch_draft_job_criteria(
            FakeLLMProvider(jd_draft=JDCriteriaDraft(title="Platform")),
            jd_text="Minimum 4 il ClickHouse təcrübəsi tələb olunur. COBIT üstünlükdür.",
        )
    )
    assert initial_result is not None and initial_result.job_draft is not None
    initial = initial_result.job_draft
    required = next(item for item in initial.must_have if item.value == "ClickHouse")
    preferred = next(item for item in initial.preferred if item.value == "COBIT")

    demoted = _apply_pending_draft_followup(
        initial, "Make ClickHouse preferred instead of required"
    )
    promoted = _apply_pending_draft_followup(initial, "Make COBIT required instead of preferred")
    assert demoted is not None and promoted is not None
    demoted_item = next(item for item in demoted.preferred if item.id == required.id)
    promoted_item = next(item for item in promoted.must_have if item.id == preferred.id)
    assert demoted_item.model_copy(update={"type": required.type}) == required
    assert promoted_item.model_copy(update={"type": preferred.type}) == preferred
    assert next(item for item in demoted.preferred if item.id == preferred.id) == preferred
    assert next(item for item in promoted.must_have if item.id == required.id) == required

    assert (
        _apply_pending_draft_followup(
            initial, "Bank təcrübəsini məcburi etmə, üstünlük kimi saxla."
        )
        is None
    )


@pytest.mark.parametrize(
    "text",
    [
        "ClickHouse bilən namizədləri göstər",
        "Show candidates who know ClickHouse",
    ],
)
def test_wrong_mode_guidance_is_deterministic_for_az_and_en_without_model_call(text: str) -> None:
    provider = FakeLLMProvider(jd_draft=JDCriteriaDraft(title="unused"))
    result = asyncio.run(_dispatch_draft_job_criteria(provider, jd_text=text))
    assert result is not None and result.job_draft is not None
    assert provider.jd_draft_call_count == 0
    assert result.job_draft.wrong_mode_guidance
    assert result.job_draft.must_have == result.job_draft.preferred == []
    assert result.job_draft.requirements == []
