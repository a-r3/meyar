import uuid

import pytest
from pydantic import ValidationError

from meyar.agent.schemas import (
    AgentJobDraftToolResult,
    JDDraftCriterionKind,
    SemanticRequirementState,
)
from meyar.agent.semantic_requirements import analyze_hr_text
from meyar.core.result_count import DEFAULT_RESULT_LIMIT, ResultCountState
from meyar.schemas.criteria import CriterionIn, CriterionKind, CriterionType, find_prohibited_term
from meyar.ui.service import build_agent_job_draft_view


@pytest.mark.parametrize(
    "text",
    [
        "Applicants holding this country's passport are required.",
        "Only holders of the country's passports may be shortlisted.",
        "People who hold their country's passport are preferred.",
        "Nationals of this country alone should apply.",
        "Yalnız Azərbaycan vətəndaşları qəbul olunur.",
        "Namizədin vətəndaşlığı bu ölkə olmalıdır.",
        "Bu ölkənin milliyyətinə sahib şəxslər tələb olunur.",
        "Azərbaycan vətəndaşı olanlara üstünlük verilir.",
    ],
)
def test_fresh_citizenship_proxy_grammar_is_prohibited(text: str) -> None:
    assert find_prohibited_term(text) is not None
    analysis = analyze_hr_text(text)
    assert analysis.requirements
    assert all(item.state == SemanticRequirementState.PROHIBITED for item in analysis.requirements)


@pytest.mark.parametrize(
    "text",
    [
        "Passport authentication system knowledge is required.",
        "Experience building passport document verification APIs is preferred.",
    ],
)
def test_passport_technical_context_is_not_blindly_prohibited(text: str) -> None:
    assert find_prohibited_term(text) is None
    assert all(
        item.state != SemanticRequirementState.PROHIBITED
        for item in analyze_hr_text(text).requirements
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Ən çox 6 nəticə göstər.", 6),
        ("Maksimum 9 namizəd çıxart.", 9),
        ("Ən uyğun 4 nəfəri qaytar.", 4),
        ("5 namizədi göstər.", 5),
        ("Göstər 8 namizəd.", 8),
        ("Nəticə sayı maksimum 12 olsun.", 12),
        ("3 nəfər namizəd göstər.", 3),
        ("10 nəticə qaytar.", 10),
        ("Ən yaxşı 2 namizəd.", 2),
        ("top 11", 11),
    ],
)
def test_fresh_az_result_controls_are_explicit_and_fully_consumed(
    text: str, expected: int
) -> None:
    analysis = analyze_hr_text(text)
    assert analysis.result_count.state == ResultCountState.VALID
    assert analysis.result_count.requested == analysis.result_count.effective == expected
    assert analysis.result_count.control_spans
    assert analysis.requirements == []
    assert "".join(span.text for span in analysis.result_count.control_spans).strip(". ")


def test_result_limit_state_never_conflates_explicit_ambiguity_with_absence() -> None:
    absent = analyze_hr_text("Python tələb olunur.").result_count
    ambiguous = analyze_hr_text("Show 5 candidates or 7 candidates.").result_count
    bounded = analyze_hr_text("Ən çox 101 nəticə göstər.").result_count
    assert (absent.state, absent.effective) == (ResultCountState.ABSENT, DEFAULT_RESULT_LIMIT)
    assert ambiguous.state == ResultCountState.AMBIGUOUS
    assert ambiguous.effective != DEFAULT_RESULT_LIMIT
    assert bounded.state == ResultCountState.OUT_OF_RANGE
    assert (bounded.requested, bounded.effective, bounded.was_bounded) == (101, 100, True)


def test_ambiguous_explicit_result_limit_blocks_browser_confirmation() -> None:
    view = build_agent_job_draft_view(
        AgentJobDraftToolResult(
            draft_id=uuid.uuid4(),
            result_limit=1,
            result_limit_needs_review=True,
        )
    )
    assert view.result_limit_needs_review
    assert view.requires_resolution


def test_consumed_english_top_k_keeps_distinct_with_modality_and_duration() -> None:
    analysis = analyze_hr_text(
        "Show top 5 candidates with 4 years of Snowflake experience."
    )
    item = analysis.requirements[0]
    assert analysis.result_count.requested == 5
    assert (
        item.state,
        item.criterion_family,
        item.normalized_subject,
        item.min_years,
    ) == (
        SemanticRequirementState.SCORABLE,
        JDDraftCriterionKind.SKILL_EXPERIENCE,
        "Snowflake",
        4.0,
    )
    assert item.modality is not None and item.modality.text == "with"


def test_azerbaijani_degree_head_remains_in_education_identity() -> None:
    item = analyze_hr_text("Magistr dərəcəsi tələb olunur.").requirements[0]
    assert item.state == SemanticRequirementState.SCORABLE
    assert item.criterion_family == JDDraftCriterionKind.EDUCATION
    assert item.normalized_subject == "Magistr dərəcəsi"
    assert item.subject is not None and item.subject.text == "Magistr dərəcəsi"


@pytest.mark.parametrize(
    ("text", "limit", "subject", "years"),
    [
        ("Ən çox 6 nəticə göstər; minimum 4 il Rust təcrübəsi tələb olunur.", 6, "Rust", 4.0),
        ("Maksimum 8 namizəd çıxart, Go üzrə 3 il təcrübə tələb olunur.", 8, "Go", 3.0),
        ("5 nəfər göstər: English C1 tələb olunur.", 5, "English", None),
        ("Top 9 profiles; iki sertifikat üstünlükdür, including CISM.", 9, "CISM", None),
        ("Python üçün 7 il təcrübə tələb olunur; 4 nəticə qaytar.", 4, "Python", 7.0),
        (
            "12 namizəd göstər; SOC 2 tələb olunur və 5 il audit təcrübəsi vacibdir.",
            12,
            "SOC 2",
            None,
        ),
    ],
)
def test_fresh_multi_number_controls_never_supply_material_identity(
    text: str, limit: int, subject: str, years: float | None
) -> None:
    analysis = analyze_hr_text(text)
    assert analysis.result_count.requested == limit
    assert subject in {item.normalized_subject for item in analysis.requirements}
    assert all(
        item.normalized_subject not in {str(limit), "iki"}
        for item in analysis.requirements
        if item.state == SemanticRequirementState.SCORABLE
    )
    if years is not None:
        assert years in {item.min_years for item in analysis.requirements}
    for control in analysis.result_count.control_spans:
        assert all(
            control.end_offset <= span.start_offset or span.end_offset <= control.start_offset
            for span in analysis.spans
        )


@pytest.mark.parametrize(
    ("text", "named", "review_count"),
    [
        ("Two certifications are preferred, including CPA and CISA.", {"CPA", "CISA"}, 1),
        ("Three credentials are desirable, including CISSP and CISM.", {"CISSP", "CISM"}, 1),
        ("CPA preferred; having two relevant certifications is an advantage.", {"CPA"}, 1),
        ("At least one cloud certification is preferred.", set(), 1),
        (
            "Two certifications are required, such as ISO 27001 and Azure Fundamentals.",
            {"ISO 27001", "Azure Fundamentals"},
            1,
        ),
        (
            "One credential is preferred, namely Google Professional Cloud Architect.",
            {"Google Professional Cloud Architect"},
            1,
        ),
    ],
)
def test_fresh_certification_quantity_preserves_only_attributable_identities(
    text: str, named: set[str], review_count: int
) -> None:
    requirements = analyze_hr_text(text).requirements
    scored = {
        item.normalized_subject
        for item in requirements
        if item.state == SemanticRequirementState.SCORABLE
    }
    assert named <= scored
    assert not scored.intersection({"One", "Two", "Three", "one", "two", "three"})
    assert (
        sum(
            item.state == SemanticRequirementState.NEEDS_HUMAN_REVIEW
            for item in requirements
        )
        == review_count
    )


@pytest.mark.parametrize(
    ("text", "subject"),
    [
        ("Aviation experience is required.", "Aviation"),
        ("Hospitality experience is preferred.", "Hospitality"),
        ("Construction experience is required.", "Construction"),
        ("Healthcare experience is preferred.", "Healthcare"),
        ("Manufacturing experience is required.", "Manufacturing"),
        ("Background in logistics is preferred.", "logistics"),
        ("Experience in insurance is required.", "insurance"),
        ("Experience within the energy industry is preferred.", "energy"),
    ],
)
def test_fresh_english_presence_only_domains_are_scorable_without_duration(
    text: str, subject: str
) -> None:
    item = analyze_hr_text(text).requirements[0]
    assert (
        item.state,
        item.criterion_family,
        item.normalized_subject,
        item.min_years,
    ) == (
        SemanticRequirementState.SCORABLE,
        JDDraftCriterionKind.DOMAIN_EXPERIENCE,
        subject,
        None,
    )


@pytest.mark.parametrize(
    ("text", "subject"),
    [
        ("While Terraform is required.", "Terraform"),
        ("Whereas Apache Kafka is preferred.", "Apache Kafka"),
        ("And SOC 2 is required.", "SOC 2"),
        ("But Oracle Cloud is preferred.", "Oracle Cloud"),
        ("Həmçinin Apache Flink biliyi tələb olunur.", "Apache Flink"),
        ("Elasticsearch isə üstünlükdür.", "Elasticsearch"),
        ("Kubernetes də tələb olunur.", "Kubernetes"),
        ("və Power BI biliyi vacibdir.", "Power BI"),
        ("however Google Cloud knowledge is preferred.", "Google Cloud"),
        ("amma Red Hat Ansible biliyi tələb olunur.", "Red Hat Ansible"),
    ],
)
def test_fresh_connective_boundaries_preserve_exact_professional_occurrence(
    text: str, subject: str
) -> None:
    item = analyze_hr_text(text).requirements[0]
    assert item.state == SemanticRequirementState.SCORABLE
    assert item.normalized_subject == subject
    assert item.subject is not None
    assert item.subject.text == subject
    assert text[item.subject.start_offset : item.subject.end_offset] == subject


@pytest.mark.parametrize(
    "value",
    ["Two", "iki", "6 nəfər", "ən çox 4 nəticə", "three candidates", "10"],
)
@pytest.mark.parametrize(
    "kind",
    [
        CriterionKind.SKILL,
        CriterionKind.DOMAIN_EXPERIENCE,
        CriterionKind.CERTIFICATION,
        CriterionKind.EDUCATION,
        CriterionKind.LANGUAGE,
    ],
)
def test_typed_authority_rejects_quantifier_only_professional_subjects(
    value: str, kind: CriterionKind
) -> None:
    with pytest.raises(ValidationError):
        CriterionIn(
            id="quantity_only",
            kind=kind,
            type=CriterionType.MUST_HAVE,
            label=value,
            value=value,
        )
