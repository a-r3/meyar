"""Metamorphic acceptance for structural source-role authority (issue #44)."""

import pytest

from meyar.agent.schemas import (
    JDDraftCriterionKind,
    SemanticRequirementState,
    SourceSpanOwner,
    SourceSpanRole,
)
from meyar.agent.semantic_requirements import analyze_hr_text
from meyar.core.result_count import DEFAULT_RESULT_LIMIT, ResultCountState
from meyar.schemas.criteria import CriterionIn, CriterionKind, CriterionType

AZ_FRESH_CASES = (
    ("Xarici pasportu daşıyan şəxslər arzuolunandır.", "safe"),
    ("Vətəndaşlıq sənədinə malik şəxslər vacibdir.", "safe"),
    ("Biometrik pasport OCR biliyi tələb olunur.", "technical"),
    ("Elektron pasport PKI biliyi üstünlükdür.", "technical"),
    ("Doqquz ən yaxşı profili siyahıya al.", "count"),
    ("4 yaxud 6 namizəd qaytar.", "ambiguous"),
    ("Bir neçə vacibdir.", "generic"),
    ("Çoxlu nəticələr üstünlükdür.", "generic"),
    ("Beş peşəkar sertifikat arzuolunandır.", "generic"),
    ("Scrum Master sertifikatı tələb olunur.", "material"),
    ("Namizədlər OpenSearch biliyinə sahib olmalıdır.", "material"),
    ("Həmçinin Apache Pulsar biliyi üstünlükdür.", "material"),
    ("Aerokosmik sahəsində təcrübə tələb olunur.", "material"),
    ("OpenSearch üzrə minimum 3 il təcrübə vacibdir.", "material"),
    ("İspan dili C1 səviyyəsində olmalıdır.", "material"),
    ("Altı profil qaytar: minimum 2 il Neo4j təcrübəsi tələb olunur.", "count_material"),
    ("Pasport sənədlərinin OCR emalı biliyi vacibdir.", "technical"),
    ("Bir cüt bulud sertifikatı üstünlükdür.", "generic"),
    ("Otuz yaşını aşmayan şəxslər tələb olunur.", "safe"),
    ("Logistika sektorunda təcrübə arzuolunandır.", "material"),
)


EN_FRESH_CASES = (
    ("Individuals possessing a national travel document are required.", "safe"),
    ("Owners of citizenship papers are preferred.", "safe"),
    ("Knowledge of biometric passport OCR is mandatory.", "technical"),
    ("E-passport PKI knowledge is desirable.", "technical"),
    ("List eight highest-ranked profiles.", "count"),
    ("Return 2 or 7 applicants.", "ambiguous"),
    ("Many are mandatory.", "generic"),
    ("Several results are desirable.", "generic"),
    ("Multiple professional credentials are preferred.", "generic"),
    ("Scrum Master certification is required.", "material"),
    ("Applicants are expected to have Dynamics AX experience.", "review_clean"),
    ("Whereas Apache Beam knowledge is preferred.", "material"),
    ("Experience within the maritime industry is required.", "material"),
    ("At least 4 years of YugabyteDB experience is required.", "material"),
    ("Portuguese language at C1 is required.", "material"),
    ("Return ten profiles with 3 years of GraphQL experience.", "count_material"),
    ("Passport document signing knowledge is mandatory.", "technical"),
    ("A pair of cloud certifications is preferred.", "generic"),
    ("People below the retirement threshold are preferred.", "safe"),
    ("Hospitality sector background is desirable.", "material"),
)


@pytest.mark.parametrize(("text", "case"), AZ_FRESH_CASES)
def test_fresh_az_structural_authority(text: str, case: str) -> None:
    _assert_fresh_case(text, case)


@pytest.mark.parametrize(("text", "case"), EN_FRESH_CASES)
def test_fresh_en_structural_authority(text: str, case: str) -> None:
    _assert_fresh_case(text, case)


def _assert_fresh_case(text: str, case: str) -> None:
    analysis = analyze_hr_text(text)
    scored = [item for item in analysis.requirements if item.state == "SCORABLE"]
    if case == "count":
        assert analysis.result_count.state == ResultCountState.VALID
        assert analysis.result_count.effective != DEFAULT_RESULT_LIMIT
        assert not scored
    elif case == "ambiguous":
        assert analysis.result_count.state == ResultCountState.AMBIGUOUS
        assert analysis.result_count.effective != DEFAULT_RESULT_LIMIT
        assert not scored
    elif case in {"generic", "safe"}:
        assert not scored
    elif case == "technical":
        assert all(item.state != "PROHIBITED" for item in analysis.requirements)
        assert analysis.requirements
    elif case == "review_clean":
        assert analysis.requirements[0].normalized_subject == "Dynamics AX"
        assert analysis.requirements[0].state == SemanticRequirementState.NEEDS_HUMAN_REVIEW
    else:
        assert scored
        if case == "count_material":
            assert analysis.result_count.state == ResultCountState.VALID
    for role in analysis.role_assignments:
        assert text[role.start_offset : role.end_offset] == role.text


def _material_shape(text: str) -> set[tuple[str, str, float | None, str | None]]:
    return {
        (
            item.criterion_family.value,
            item.normalized_subject or "",
            item.min_years,
            item.required_level,
        )
        for item in analyze_hr_text(text).requirements
        if item.state == SemanticRequirementState.SCORABLE
    }


@pytest.mark.parametrize("preamble", ["We need ", "Applicants should have "])
def test_preamble_invariance(preamble: str) -> None:
    base = _material_shape("minimum 3 years of ScyllaDB experience is required")
    assert _material_shape(preamble + "minimum 3 years of ScyllaDB experience") == base


def test_clause_order_invariance() -> None:
    left = _material_shape("DuckDB knowledge is required. Experience in aviation is preferred.")
    right = _material_shape("Experience in aviation is preferred. DuckDB knowledge is required.")
    assert left == right


@pytest.mark.parametrize("connective", ["and", "but", "while", "whereas", "həmçinin", "isə"])
def test_connective_invariance(connective: str) -> None:
    item = analyze_hr_text(f"{connective} Apache Druid knowledge is preferred").requirements[0]
    assert item.normalized_subject == "Apache Druid"


def test_top_k_paraphrase_changes_only_result_limit() -> None:
    requests = (
        "Show 6 candidates with 2 years of GraphQL experience",
        "List top six profiles with 2 years of GraphQL experience",
        "Return the best 6 applicants with 2 years of GraphQL experience",
    )
    analyses = [analyze_hr_text(text) for text in requests]
    assert {item.result_count.requested for item in analyses} == {6}
    assert all(_material_shape(text) == _material_shape(requests[0]) for text in requests[1:])


@pytest.mark.parametrize("quantity", ["two", "three", "several"])
def test_quantity_substitution_never_creates_identity(quantity: str) -> None:
    analysis = analyze_hr_text(f"{quantity} professional certifications are preferred")
    assert not any(
        item.state == SemanticRequirementState.SCORABLE for item in analysis.requirements
    )


def test_protected_paraphrase_and_technical_passport_separate() -> None:
    personal = analyze_hr_text("Custodians of a national passport are preferred.")
    technical = analyze_hr_text("National passport signature validation knowledge is preferred.")
    assert not any(
        item.state == SemanticRequirementState.SCORABLE for item in personal.requirements
    )
    assert all(item.state != SemanticRequirementState.PROHIBITED for item in technical.requirements)


@pytest.mark.parametrize("subject", ["DragonflyDB", "Apache Iceberg"])
def test_novel_subject_invariance(subject: str) -> None:
    item = analyze_hr_text(f"Minimum 2 years of {subject} experience is required").requirements[0]
    assert (
        item.criterion_family,
        item.normalized_subject,
        item.min_years,
        item.state,
    ) == (
        JDDraftCriterionKind.SKILL_EXPERIENCE,
        subject,
        2.0,
        SemanticRequirementState.SCORABLE,
    )


def test_role_ownership_consumes_control_without_material_overlap() -> None:
    text = "Return top 13 profiles with 4 years of ClickHouse experience"
    analysis = analyze_hr_text(text)
    controls = [
        item
        for item in analysis.role_assignments
        if item.role == SourceSpanRole.CONTROL_RESULT_COUNT
    ]
    assert controls and {item.owner for item in controls} == {SourceSpanOwner.WORKFLOW_CONTROL}
    for control in controls:
        assert all(
            control.end_offset <= span.start_offset or span.end_offset <= control.start_offset
            for span in analysis.spans
        )


@pytest.mark.parametrize(
    ("text", "subject"),
    [
        ("Scrum Master certification is required.", "Scrum Master"),
        ("Scrum Master sertifikatı tələb olunur.", "Scrum Master"),
        ("Namizədlər OpenSearch biliyinə sahib olmalıdır.", "OpenSearch"),
    ],
)
def test_relation_centered_subject_does_not_truncate_or_keep_preamble(
    text: str, subject: str
) -> None:
    item = analyze_hr_text(text).requirements[0]
    assert item.normalized_subject == subject
    assert item.subject is not None and item.subject.text == subject


def test_az_fallback_count_consumption_preserves_following_skill_duration() -> None:
    analysis = analyze_hr_text(
        "Altı profil qaytar: minimum 2 il Neo4j təcrübəsi tələb olunur."
    )
    assert analysis.result_count.requested == 6
    item = analysis.requirements[0]
    assert (
        item.criterion_family,
        item.normalized_subject,
        item.min_years,
        item.state,
    ) == (
        JDDraftCriterionKind.SKILL_EXPERIENCE,
        "Neo4j",
        2.0,
        SemanticRequirementState.SCORABLE,
    )


@pytest.mark.parametrize(
    "value",
    [
        "many",
        "several",
        "multiple results",
        "bir neçə",
        "çoxlu",
        "nəticələr",
        "namizədlər",
        "a pair of cloud",
    ],
)
@pytest.mark.parametrize(
    "kind",
    [
        CriterionKind.SKILL,
        CriterionKind.SKILL_EXPERIENCE,
        CriterionKind.DOMAIN_EXPERIENCE,
        CriterionKind.CERTIFICATION,
        CriterionKind.EDUCATION,
        CriterionKind.LANGUAGE,
    ],
)
def test_typed_boundary_requires_professional_identity(value: str, kind: CriterionKind) -> None:
    kwargs = {"min_years": 1} if kind == CriterionKind.SKILL_EXPERIENCE else {}
    with pytest.raises(ValueError):
        CriterionIn(
            id="generic",
            kind=kind,
            type=CriterionType.MUST_HAVE,
            label=value,
            value=value,
            **kwargs,
        )


@pytest.mark.parametrize("value", ["Three audit", "2 cloud", "A pair of cloud"])
def test_certification_quantity_is_never_named_identity(value: str) -> None:
    with pytest.raises(ValueError):
        CriterionIn(
            id="certificate_quantity",
            kind=CriterionKind.CERTIFICATION,
            type=CriterionType.PREFERRED,
            label=value,
            value=value,
        )
