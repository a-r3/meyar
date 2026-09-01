"""Slice 3 (issue #32) — evidence capability completion.

Covers, without calling the LLM (see .claude/rules/testing.md — policy-
engine logic must be testable against fixed CandidateProfile/criterion
inputs): skill<->employment grounding, domain/sector evidence, the
deterministic duration evaluators built on top of them, and the
extraction-time explicit-evidence guard that keeps domain claims from
being inferred off an opaque employer name. Scenario letters below
mirror the validation scenarios in issue #32.
"""

import uuid as uuid_mod
from datetime import date

import pytest
from pydantic import ValidationError

from meyar.core.domain_terms import canonicalize_domain, domain_term_present
from meyar.evaluation.evaluators import evaluate_criterion
from meyar.extraction.evidence import EvidenceValidationError, verify_extraction_evidence
from meyar.extraction.view import ModelInputBlock, ProfessionalDocumentView
from meyar.schemas.candidate_profile import CandidateProfileExtraction
from meyar.schemas.criteria import CriterionIn, CriterionKind, CriterionType
from meyar.schemas.evaluation import (
    CRITERION_STATUS_MATCH,
    CRITERION_STATUS_NOT_MATCHED,
    CRITERION_STATUS_UNKNOWN,
)

_AS_OF_DATE = date(2026, 1, 1)


def _skill_experience_criterion(criterion_id: str, skill: str, min_years: float) -> CriterionIn:
    return CriterionIn(
        id=criterion_id,
        kind=CriterionKind.SKILL_EXPERIENCE,
        type=CriterionType.MUST_HAVE,
        label=f"{min_years} years {skill}",
        value=skill,
        min_years=min_years,
    )


def _domain_criterion(
    criterion_id: str, domain: str, min_years: float | None = None
) -> CriterionIn:
    return CriterionIn(
        id=criterion_id,
        kind=CriterionKind.DOMAIN_EXPERIENCE,
        type=CriterionType.MUST_HAVE,
        label=domain,
        value=domain,
        min_years=min_years,
    )


def _evidence(quote: str) -> list[dict]:
    return [{"page": 1, "block_index": 0, "quote": quote}]


def _employment(
    index_note: str, start: str, end: str | None = None, is_current: bool = False
) -> dict:
    return {
        "title": f"Engineer {index_note}",
        "organization": "Acme",
        "start_date": start,
        "end_date": end,
        "is_current": is_current,
        "evidence": _evidence(f"Engineer {index_note} {start} - {end or 'Present'}"),
    }


def _profile(**overrides) -> dict:
    base = {
        "skills": [],
        "employment_history": [],
        "education": [],
        "certifications": [],
        "languages": [],
        "projects": [],
        "skill_experience": [],
        "domain_experience": [],
    }
    base.update(overrides)
    return base


# --- Scenario A: >=5 provable Java years across attributable periods -----


def test_scenario_a_provable_skill_years_across_two_periods_satisfies_criterion() -> None:
    profile = CandidateProfileExtraction.model_validate(
        _profile(
            skills=[{"name": "Java", "category": None, "evidence": _evidence("Java")}],
            employment_history=[
                _employment("A", "2015", "2018"),
                _employment("B", "2019", "2022"),
            ],
            skill_experience=[
                {
                    "skill_name": "Java",
                    "employment_index": 0,
                    "evidence": _evidence("Java backend work 2015-2018"),
                },
                {
                    "skill_name": "Java",
                    "employment_index": 1,
                    "evidence": _evidence("Java backend work 2019-2022"),
                },
            ],
        )
    )
    result = evaluate_criterion(
        _skill_experience_criterion("java5", "Java", 5.0),
        profile,
        evaluation_as_of_date=_AS_OF_DATE,
    )
    assert result.status == CRITERION_STATUS_MATCH
    assert result.reason_code == "SKILL_DURATION_SUFFICIENT"
    assert result.evidence  # explains which periods/facts support it (scenario H)


# --- Scenario B: 8 years total career, only 2 provable Java years --------


def test_scenario_b_total_experience_never_substitutes_for_skill_duration() -> None:
    profile = CandidateProfileExtraction.model_validate(
        _profile(
            skills=[{"name": "Java", "category": None, "evidence": _evidence("Java")}],
            employment_history=[
                _employment("A", "2016", "2020"),  # 4 yrs, no Java
                _employment("B", "2020", "2022"),  # 2 yrs, Java
                _employment("C", "2022", "2024"),  # 2 yrs, no Java
            ],
            skill_experience=[
                {
                    "skill_name": "Java",
                    "employment_index": 1,
                    "evidence": _evidence("Java backend work 2020-2022"),
                },
            ],
        )
    )
    # Total career experience is 8 years — confirm that criterion alone
    # would MATCH, to make the SKILL_EXPERIENCE non-substitution explicit.
    total_result = evaluate_criterion(
        CriterionIn(
            id="total5",
            kind=CriterionKind.EXPERIENCE,
            type=CriterionType.MUST_HAVE,
            label="5 years",
            min_years=5.0,
        ),
        profile,
        evaluation_as_of_date=_AS_OF_DATE,
    )
    assert total_result.status == CRITERION_STATUS_MATCH

    skill_result = evaluate_criterion(
        _skill_experience_criterion("java5", "Java", 5.0),
        profile,
        evaluation_as_of_date=_AS_OF_DATE,
    )
    assert skill_result.status == CRITERION_STATUS_NOT_MATCHED
    assert skill_result.reason_code == "SKILL_DURATION_INSUFFICIENT"
    assert "2" in skill_result.explanation


# --- Scenario C: skill present, no attributable dates -> UNKNOWN ---------


def test_scenario_c_skill_with_no_attributable_period_is_unknown_not_zero_or_total() -> None:
    profile = CandidateProfileExtraction.model_validate(
        _profile(
            skills=[{"name": "Java", "category": None, "evidence": _evidence("Java")}],
            employment_history=[_employment("A", "2016", "2024")],
            skill_experience=[],  # never linked to any period
        )
    )
    result = evaluate_criterion(
        _skill_experience_criterion("java5", "Java", 5.0),
        profile,
        evaluation_as_of_date=_AS_OF_DATE,
    )
    assert result.status == CRITERION_STATUS_UNKNOWN
    assert result.reason_code == "SKILL_DURATION_NO_ATTRIBUTABLE_PERIODS"
    assert "0" not in result.explanation.split()  # never phrased as "0 years"


def test_scenario_c_unparseable_linked_dates_also_stay_unknown_not_manual_review() -> None:
    """Distinct from evaluate_experience (MANUAL_REVIEW_REQUIRED on
    unparseable total-career dates) — issue #32 explicitly requires
    unsupported per-skill dates to stay UNKNOWN."""
    profile = CandidateProfileExtraction.model_validate(
        _profile(
            skills=[{"name": "Java", "category": None, "evidence": _evidence("Java")}],
            employment_history=[
                {
                    "title": "Engineer",
                    "organization": "Acme",
                    "start_date": "sometime",
                    "end_date": "later",
                    "is_current": False,
                    "evidence": _evidence("Engineer sometime - later"),
                }
            ],
            skill_experience=[
                {
                    "skill_name": "Java",
                    "employment_index": 0,
                    "evidence": _evidence("Java work at Acme"),
                }
            ],
        )
    )
    result = evaluate_criterion(
        _skill_experience_criterion("java5", "Java", 5.0),
        profile,
        evaluation_as_of_date=_AS_OF_DATE,
    )
    assert result.status == CRITERION_STATUS_UNKNOWN
    assert result.reason_code == "SKILL_DURATION_DATES_UNPARSEABLE"


# --- Scenario D: overlapping Java periods are merged, not double-counted -


def test_scenario_d_overlapping_skill_periods_not_double_counted() -> None:
    profile = CandidateProfileExtraction.model_validate(
        _profile(
            skills=[{"name": "Java", "category": None, "evidence": _evidence("Java")}],
            employment_history=[
                _employment("FullTime", "2018", "2022"),  # 4 yrs
                _employment("Freelance", "2020", "2021"),  # fully inside the above
            ],
            skill_experience=[
                {
                    "skill_name": "Java",
                    "employment_index": 0,
                    "evidence": _evidence("Java at full-time job 2018-2022"),
                },
                {
                    "skill_name": "Java",
                    "employment_index": 1,
                    "evidence": _evidence("Java freelance 2020-2021"),
                },
            ],
        )
    )
    result = evaluate_criterion(
        _skill_experience_criterion("java4", "Java", 4.0),
        profile,
        evaluation_as_of_date=_AS_OF_DATE,
    )
    # Naive sum would be 4 + 1 = 5; merged non-overlapping span is exactly 4.
    assert "4.0" in result.explanation or "4 " in result.explanation
    assert result.status == CRITERION_STATUS_MATCH
    over_result = evaluate_criterion(
        _skill_experience_criterion("java4_5", "Java", 4.5),
        profile,
        evaluation_as_of_date=_AS_OF_DATE,
    )
    assert over_result.status == CRITERION_STATUS_NOT_MATCHED


# --- Scenario E: open/current employment uses the explicit as-of date ----


def test_scenario_e_open_employment_uses_explicit_evaluation_date_deterministically() -> None:
    profile = CandidateProfileExtraction.model_validate(
        _profile(
            skills=[{"name": "Java", "category": None, "evidence": _evidence("Java")}],
            employment_history=[_employment("Current", "2020", None, is_current=True)],
            skill_experience=[
                {
                    "skill_name": "Java",
                    "employment_index": 0,
                    "evidence": _evidence("Java, ongoing role since 2020"),
                }
            ],
        )
    )
    as_of_2026 = evaluate_criterion(
        _skill_experience_criterion("java5", "Java", 5.0),
        profile,
        evaluation_as_of_date=date(2026, 1, 1),
    )
    assert as_of_2026.status == CRITERION_STATUS_MATCH  # 6 years as of 2026

    as_of_2023 = evaluate_criterion(
        _skill_experience_criterion("java5", "Java", 5.0),
        profile,
        evaluation_as_of_date=date(2023, 1, 1),
    )
    assert as_of_2023.status == CRITERION_STATUS_NOT_MATCHED  # only 3 years as of 2023


# --- Scenario F: explicit AML/banking evidence is retrievable/evaluable --


def test_scenario_f_explicit_domain_evidence_matches() -> None:
    profile = CandidateProfileExtraction.model_validate(
        _profile(
            employment_history=[_employment("Compliance", "2019", "2023")],
            domain_experience=[
                {
                    "domain": "AML",
                    "employment_index": 0,
                    "evidence": _evidence("AML compliance analyst, anti-money laundering team"),
                }
            ],
        )
    )
    presence = evaluate_criterion(
        _domain_criterion("aml_any", "AML"), profile, evaluation_as_of_date=_AS_OF_DATE
    )
    assert presence.status == CRITERION_STATUS_MATCH
    assert presence.reason_code == "DOMAIN_EXPLICIT_MATCH"

    duration = evaluate_criterion(
        _domain_criterion("aml3", "AML", 3.0), profile, evaluation_as_of_date=_AS_OF_DATE
    )
    assert duration.status == CRITERION_STATUS_MATCH
    assert duration.reason_code == "DOMAIN_DURATION_SUFFICIENT"


def test_scenario_f_banking_domain_synonym_and_typing_variance_matches() -> None:
    profile = CandidateProfileExtraction.model_validate(
        _profile(
            employment_history=[_employment("Bank", "2018", "2021")],
            domain_experience=[
                {
                    "domain": "bankcilik",  # ASCII-typed variant of "bankçılıq"
                    "employment_index": 0,
                    "evidence": _evidence("3 il bankcilik sektorunda tecrube"),
                }
            ],
        )
    )
    result = evaluate_criterion(
        _domain_criterion("banking_any", "banking"), profile, evaluation_as_of_date=_AS_OF_DATE
    )
    assert result.status == CRITERION_STATUS_MATCH


def test_domain_duration_with_unparseable_linked_dates_stays_unknown() -> None:
    """Same discipline as the skill-duration evaluator: an attributed but
    unparseable period makes the duration claim not fully provable, so the
    result is UNKNOWN — never a possibly-undercounted NOT_MATCHED."""
    profile = CandidateProfileExtraction.model_validate(
        _profile(
            employment_history=[
                {
                    "title": "Compliance Officer",
                    "organization": "Acme",
                    "start_date": "sometime",
                    "end_date": "later",
                    "is_current": False,
                    "evidence": _evidence("Compliance Officer sometime - later"),
                }
            ],
            domain_experience=[
                {
                    "domain": "AML",
                    "employment_index": 0,
                    "evidence": _evidence("AML compliance work"),
                }
            ],
        )
    )
    result = evaluate_criterion(
        _domain_criterion("aml3", "AML", 3.0),
        profile,
        evaluation_as_of_date=_AS_OF_DATE,
    )
    assert result.status == CRITERION_STATUS_UNKNOWN
    assert result.reason_code == "DOMAIN_DURATION_DATES_UNPARSEABLE"


# --- Scenario G: unsupported domain inference stays UNKNOWN --------------


def test_scenario_g_company_name_alone_never_yields_domain_evidence() -> None:
    """No domain_experience item exists at all — extraction never
    populated one because the CV text never named a sector explicitly
    (only an opaque employer name). This is the intended, safe shape."""
    profile = CandidateProfileExtraction.model_validate(
        _profile(employment_history=[_employment("At ABC Bank Holdings LLC", "2018", "2021")])
    )
    result = evaluate_criterion(
        _domain_criterion("banking_any", "banking"), profile, evaluation_as_of_date=_AS_OF_DATE
    )
    assert result.status == CRITERION_STATUS_UNKNOWN
    assert result.reason_code == "DOMAIN_NOT_FOUND_IN_PROFILE"


def test_scenario_g_domain_term_present_rejects_bare_company_name() -> None:
    """meyar.core.domain_terms is the deterministic guard extraction
    verification relies on — a company name containing "Bank" as a
    substring inside an unrelated word must not satisfy it, and even a
    standalone employer name "Bank of Baku" (no descriptive sector
    phrase) must not either."""
    assert domain_term_present("banking", ["Fairbanks Corp, Software Engineer"]) is False
    assert domain_term_present("banking", ["Bank of Baku, Operations Assistant"]) is False
    assert domain_term_present("banking", ["3 years in the banking sector"]) is True


def test_scenario_g_extraction_rejects_domain_claim_without_explicit_term() -> None:
    """Even if a model tried to claim domain=banking from a bare company
    name, evidence verification (the same terminal, no-retry discipline
    as a fabricated quote) rejects it before persistence."""
    view = ProfessionalDocumentView(
        canonical_document_id=uuid_mod.uuid4(),
        blocks=[ModelInputBlock(page=1, block_index=0, text="ABC Bank Holdings LLC, Analyst")],
    )
    extraction = CandidateProfileExtraction.model_validate(
        _profile(
            employment_history=[
                {
                    "title": "Analyst",
                    "organization": "ABC Bank Holdings LLC",
                    "start_date": "2018",
                    "end_date": "2021",
                    "is_current": False,
                    "evidence": [{"page": 1, "block_index": 0, "quote": "ABC Bank Holdings LLC"}],
                }
            ],
            domain_experience=[
                {
                    "domain": "banking",
                    "employment_index": 0,
                    "evidence": [{"page": 1, "block_index": 0, "quote": "ABC Bank Holdings LLC"}],
                }
            ],
        )
    )
    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_extraction_evidence(view, extraction)
    assert exc_info.value.code == "DOMAIN_EVIDENCE_NOT_EXPLICIT"


def test_scenario_g_extraction_accepts_domain_claim_with_explicit_term() -> None:
    view = ProfessionalDocumentView(
        canonical_document_id=uuid_mod.uuid4(),
        blocks=[
            ModelInputBlock(
                page=1, block_index=0, text="Analyst, 3 years in the banking sector at ABC Bank"
            )
        ],
    )
    extraction = CandidateProfileExtraction.model_validate(
        _profile(
            employment_history=[
                {
                    "title": "Analyst",
                    "organization": "ABC Bank",
                    "start_date": "2018",
                    "end_date": "2021",
                    "is_current": False,
                    "evidence": [
                        {
                            "page": 1,
                            "block_index": 0,
                            "quote": "Analyst, 3 years in the banking sector at ABC Bank",
                        }
                    ],
                }
            ],
            domain_experience=[
                {
                    "domain": "banking",
                    "employment_index": 0,
                    "evidence": [
                        {
                            "page": 1,
                            "block_index": 0,
                            "quote": "Analyst, 3 years in the banking sector at ABC Bank",
                        }
                    ],
                }
            ],
        )
    )
    verify_extraction_evidence(view, extraction)  # must not raise


# --- Scenario H: evidence shown to HR explains which periods/facts -------


def test_scenario_h_agent_evidence_tool_surfaces_skill_and_domain_grounding() -> None:
    from meyar.agent.service import _EVIDENCE_CATEGORIES

    profile = CandidateProfileExtraction.model_validate(
        _profile(
            skills=[{"name": "Java", "category": None, "evidence": _evidence("Java")}],
            employment_history=[_employment("A", "2019", "2023")],
            skill_experience=[
                {
                    "skill_name": "Java",
                    "employment_index": 0,
                    "evidence": _evidence("Java backend work 2019-2023"),
                }
            ],
            domain_experience=[
                {
                    "domain": "AML",
                    "employment_index": 0,
                    "evidence": _evidence("AML compliance work"),
                }
            ],
        )
    )
    categories = {name for name, _fn in _EVIDENCE_CATEGORIES}
    assert {"skill_experience", "domain_experience"}.issubset(categories)
    titles = {
        name: [fn(item) for item in getattr(profile, name)] for name, fn in _EVIDENCE_CATEGORIES
    }
    assert titles["skill_experience"] == ["Java"]
    assert titles["domain_experience"] == ["AML"]


def test_scenario_h_grounded_facts_include_attributable_period_detail() -> None:
    from meyar.agent.service import _build_profile_facts

    profile = CandidateProfileExtraction.model_validate(
        _profile(
            employment_history=[_employment("A", "2019", "2023")],
            skill_experience=[
                {
                    "skill_name": "Java",
                    "employment_index": 0,
                    "evidence": _evidence("Java backend work 2019-2023"),
                }
            ],
            domain_experience=[
                {
                    "domain": "AML",
                    "employment_index": 0,
                    "evidence": _evidence("AML compliance work"),
                }
            ],
        )
    )
    facts = _build_profile_facts(profile)
    skill_fact = next(f for f in facts if f.category == "skill_experience")
    assert "Java" in skill_fact.title
    assert skill_fact.detail == "2019 — 2023"

    domain_fact = next(f for f in facts if f.category == "domain_experience")
    assert domain_fact.title == "AML"
    assert domain_fact.detail == "2019 — 2023"


# --- Schema-level guards ---------------------------------------------------


def test_skill_experience_employment_index_out_of_bounds_rejected() -> None:
    with pytest.raises(ValidationError):
        CandidateProfileExtraction.model_validate(
            _profile(
                employment_history=[_employment("A", "2019", "2023")],
                skill_experience=[
                    {"skill_name": "Java", "employment_index": 1, "evidence": _evidence("Java")}
                ],
            )
        )


def test_domain_experience_employment_index_out_of_bounds_rejected() -> None:
    with pytest.raises(ValidationError):
        CandidateProfileExtraction.model_validate(
            _profile(
                employment_history=[],
                domain_experience=[
                    {"domain": "AML", "employment_index": 0, "evidence": _evidence("AML")}
                ],
            )
        )


def test_pre_slice_3_profile_content_still_validates_backward_compatibly() -> None:
    """No migration needed (profile_content is JSON): an old
    CandidateProfileVersion row with no skill_experience/domain_experience
    keys at all must still validate, with both defaulting to empty."""
    legacy_content = {
        "skills": [{"name": "Python", "category": None, "evidence": _evidence("Python")}],
        "employment_history": [],
        "education": [],
        "certifications": [],
        "languages": [],
        "projects": [],
    }
    profile = CandidateProfileExtraction.model_validate(legacy_content)
    assert profile.skill_experience == []
    assert profile.domain_experience == []


def test_criterion_kind_skill_experience_requires_value_and_min_years() -> None:
    with pytest.raises(ValidationError):
        CriterionIn(
            id="bad",
            kind=CriterionKind.SKILL_EXPERIENCE,
            type=CriterionType.MUST_HAVE,
            label="Java",
            value="Java",
        )  # missing min_years
    with pytest.raises(ValidationError):
        CriterionIn(
            id="bad2",
            kind=CriterionKind.SKILL_EXPERIENCE,
            type=CriterionType.MUST_HAVE,
            label="5 years",
            min_years=5.0,
        )  # missing value


def test_criterion_kind_domain_experience_min_years_optional() -> None:
    criterion = CriterionIn(
        id="aml",
        kind=CriterionKind.DOMAIN_EXPERIENCE,
        type=CriterionType.MUST_HAVE,
        label="AML",
        value="AML",
    )
    assert criterion.min_years is None


def test_canonicalize_domain_matches_curated_synonyms() -> None:
    assert canonicalize_domain("Anti-Money Laundering") == "aml"
    assert canonicalize_domain("AML") == "aml"
    assert canonicalize_domain("banking sector") == "banking"
    assert canonicalize_domain("telecom") == "telecom"  # not curated, round-trips as-is
