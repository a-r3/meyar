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


def _skill_exp(
    skill: str,
    employment_index: int,
    quote: str,
    *,
    start: str | None = None,
    end: str | None = None,
    is_current: bool = False,
) -> dict:
    """A skill_experience item. start/end/is_current are the item's OWN
    attributable interval — deliberately independent of the linked
    employment entry's dates (employment_index is context/provenance
    only; see docs/DECISIONS.md). Omitting start/end/is_current entirely
    models "linked but no attributable interval was ever stated"."""
    return {
        "skill_name": skill,
        "employment_index": employment_index,
        "start_date": start,
        "end_date": end,
        "is_current": is_current,
        "evidence": _evidence(quote),
    }


def _domain_exp(
    domain: str,
    employment_index: int | None,
    quote: str,
    *,
    start: str | None = None,
    end: str | None = None,
    is_current: bool = False,
) -> dict:
    """A domain_experience item — same own-interval discipline as
    _skill_exp above."""
    return {
        "domain": domain,
        "employment_index": employment_index,
        "start_date": start,
        "end_date": end,
        "is_current": is_current,
        "evidence": _evidence(quote),
    }


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
                _skill_exp("Java", 0, "Java backend work 2015-2018", start="2015", end="2018"),
                _skill_exp("Java", 1, "Java backend work 2019-2022", start="2019", end="2022"),
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
                _skill_exp("Java", 1, "Java backend work 2020-2022", start="2020", end="2022"),
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
    unsupported per-skill dates to stay UNKNOWN. The employment entry
    itself has clean, parseable dates (2016-2024) — proving the evaluator
    never falls back to the entry's dates when the SKILL's own declared
    interval is what's garbled."""
    profile = CandidateProfileExtraction.model_validate(
        _profile(
            skills=[{"name": "Java", "category": None, "evidence": _evidence("Java")}],
            employment_history=[_employment("A", "2016", "2024")],
            skill_experience=[
                _skill_exp(
                    "Java",
                    0,
                    "Java work at Acme, sometime - later",
                    start="sometime",
                    end="later",
                )
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
    """Employment entries are deliberately WIDER than the skill's own
    attributable intervals (2015-2023 and 2019-2023), so a passing test
    here also proves attribution: only the item's own dates (2018-2022,
    2020-2021 — one fully nested in the other) drive the sum, not the
    entries' own wider spans."""
    profile = CandidateProfileExtraction.model_validate(
        _profile(
            skills=[{"name": "Java", "category": None, "evidence": _evidence("Java")}],
            employment_history=[
                _employment("FullTime", "2015", "2023"),
                _employment("Freelance", "2019", "2023"),
            ],
            skill_experience=[
                _skill_exp(
                    "Java",
                    0,
                    "Java at full-time job 2018-2022",
                    start="2018",
                    end="2022",
                ),
                _skill_exp("Java", 1, "Java freelance 2020-2021", start="2020", end="2021"),
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


# --- Duration attribution: a linked employment record's full span must ---
# --- never be inherited as the skill/domain's own duration ---------------


def test_regression_1_narrow_skill_evidence_inside_wide_employment_never_inherits_full_span() -> (
    None
):
    """The exact counterexample from the owner's final correctness check:
    employment 2020-2025 (5 years), Java evidence-backed only for a short
    project spanning late 2023 into early 2024. This must NOT become
    "5 years Java". (Date parsing is deliberately year-granularity only —
    the same frozen `parse_year` used everywhere else in this engine — so
    the project's dates are chosen to cross a calendar-year boundary,
    giving a small but non-zero, exactly-computable attributable span.)"""
    profile = CandidateProfileExtraction.model_validate(
        _profile(
            skills=[{"name": "Java", "category": None, "evidence": _evidence("Java")}],
            employment_history=[_employment("A", "2020", "2025")],
            skill_experience=[
                _skill_exp(
                    "Java",
                    0,
                    "Used Java on a short project from late 2023 into early 2024",
                    start="2023-10",
                    end="2024-02",
                )
            ],
        )
    )
    five_year_result = evaluate_criterion(
        _skill_experience_criterion("java5", "Java", 5.0),
        profile,
        evaluation_as_of_date=_AS_OF_DATE,
    )
    assert five_year_result.status == CRITERION_STATUS_NOT_MATCHED
    assert five_year_result.reason_code == "SKILL_DURATION_INSUFFICIENT"
    assert "Computed 1" in five_year_result.explanation  # 1 attributable year, never 5

    one_year_result = evaluate_criterion(
        _skill_experience_criterion("java1", "Java", 1.0),
        profile,
        evaluation_as_of_date=_AS_OF_DATE,
    )
    assert one_year_result.status == CRITERION_STATUS_MATCH  # the real, small, provable span


def test_regression_2_skill_linked_to_employment_with_no_skill_interval_is_unknown() -> None:
    """A skill_experience item that links to a real employment entry
    (context/provenance) but declares no start_date/end_date/is_current of
    its own must be UNKNOWN — never fall back to the employment entry's
    dates."""
    profile = CandidateProfileExtraction.model_validate(
        _profile(
            skills=[{"name": "Java", "category": None, "evidence": _evidence("Java")}],
            employment_history=[_employment("A", "2015", "2025")],  # 10 provable-looking years
            skill_experience=[
                {
                    "skill_name": "Java",
                    "employment_index": 0,
                    "evidence": _evidence("Java"),
                    # no start_date/end_date/is_current at all
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
    assert result.reason_code == "SKILL_DURATION_NO_ATTRIBUTABLE_INTERVAL"


def test_regression_3_explicit_five_plus_attributable_years_match() -> None:
    """Two explicit, non-overlapping attributable Java intervals summing
    to exactly 5 years satisfy a 5-year Java criterion."""
    profile = CandidateProfileExtraction.model_validate(
        _profile(
            skills=[{"name": "Java", "category": None, "evidence": _evidence("Java")}],
            employment_history=[
                _employment("A", "2015", "2018"),
                _employment("B", "2018", "2020"),
            ],
            skill_experience=[
                _skill_exp("Java", 0, "Java 2015-2018", start="2015", end="2018"),
                _skill_exp("Java", 1, "Java 2018-2020", start="2018", end="2020"),
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


def test_regression_4_domain_duration_follows_the_same_attribution_rule() -> None:
    """Domain mirror of regression 1: a 5-year employment record with AML
    evidence backed by only a short attributable sub-period must NOT
    become "5 years AML" (dates cross a calendar-year boundary for the
    same year-granularity-parsing reason as regression 1)."""
    profile = CandidateProfileExtraction.model_validate(
        _profile(
            employment_history=[_employment("Compliance", "2020", "2025")],
            domain_experience=[
                _domain_exp(
                    "AML",
                    0,
                    "AML review engagement from late 2023 into early 2024",
                    start="2023-10",
                    end="2024-02",
                )
            ],
        )
    )
    five_year_result = evaluate_criterion(
        _domain_criterion("aml5", "AML", 5.0), profile, evaluation_as_of_date=_AS_OF_DATE
    )
    assert five_year_result.status == CRITERION_STATUS_NOT_MATCHED
    assert five_year_result.reason_code == "DOMAIN_DURATION_INSUFFICIENT"
    assert "Computed 1" in five_year_result.explanation  # 1 attributable year, never 5

    one_year_result = evaluate_criterion(
        _domain_criterion("aml1", "AML", 1.0), profile, evaluation_as_of_date=_AS_OF_DATE
    )
    assert one_year_result.status == CRITERION_STATUS_MATCH

    # And a domain claim with no attributable interval at all, linked to
    # a real (wide) employment entry, is UNKNOWN — never that entry's span.
    no_interval_profile = CandidateProfileExtraction.model_validate(
        _profile(
            employment_history=[_employment("Compliance", "2015", "2025")],
            domain_experience=[
                {
                    "domain": "AML",
                    "employment_index": 0,
                    "evidence": _evidence("AML"),
                }
            ],
        )
    )
    no_interval_result = evaluate_criterion(
        _domain_criterion("aml5", "AML", 5.0),
        no_interval_profile,
        evaluation_as_of_date=_AS_OF_DATE,
    )
    assert no_interval_result.status == CRITERION_STATUS_UNKNOWN
    assert no_interval_result.reason_code == "DOMAIN_DURATION_NO_ATTRIBUTABLE_INTERVAL"


# --- Scenario E: open/current employment uses the explicit as-of date ----


def test_scenario_e_open_employment_uses_explicit_evaluation_date_deterministically() -> None:
    profile = CandidateProfileExtraction.model_validate(
        _profile(
            skills=[{"name": "Java", "category": None, "evidence": _evidence("Java")}],
            employment_history=[_employment("Current", "2020", None, is_current=True)],
            skill_experience=[
                _skill_exp(
                    "Java",
                    0,
                    "Java, ongoing role since 2020",
                    start="2020",
                    is_current=True,
                )
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
                _domain_exp(
                    "AML",
                    0,
                    "AML compliance analyst, anti-money laundering team, 2019-2023",
                    start="2019",
                    end="2023",
                )
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
    result is UNKNOWN — never a possibly-undercounted NOT_MATCHED. The
    employment entry itself has clean dates, proving the evaluator never
    falls back to them."""
    profile = CandidateProfileExtraction.model_validate(
        _profile(
            employment_history=[_employment("Compliance", "2016", "2024")],
            domain_experience=[
                _domain_exp(
                    "AML",
                    0,
                    "AML compliance work, sometime - later",
                    start="sometime",
                    end="later",
                )
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


# --- Final review: a claimed interval must be GROUNDED by the item's own -
# --- evidence quotes, not merely accompanied by a verbatim-real quote ----


def _employment_view_block(text: str) -> ProfessionalDocumentView:
    return ProfessionalDocumentView(
        canonical_document_id=uuid_mod.uuid4(),
        blocks=[ModelInputBlock(page=1, block_index=0, text=text)],
    )


def test_final_review_1_skill_interval_with_no_dates_in_quote_is_rejected() -> None:
    """Counterexample 1: the quote proves Java usage but supports no
    attributable dates at all — the model must not still be able to claim
    2020-2025 for it."""
    view = _employment_view_block("Backend Developer using Java at Acme")
    extraction = CandidateProfileExtraction.model_validate(
        _profile(
            employment_history=[
                {
                    "title": "Backend Developer",
                    "organization": "Acme",
                    "start_date": "2020",
                    "end_date": "2025",
                    "is_current": False,
                    "evidence": [
                        {
                            "page": 1,
                            "block_index": 0,
                            "quote": "Backend Developer using Java at Acme",
                        }
                    ],
                }
            ],
            skill_experience=[
                _skill_exp(
                    "Java",
                    0,
                    "Backend Developer using Java at Acme",
                    start="2020",
                    end="2025",
                )
            ],
        )
    )
    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_extraction_evidence(view, extraction)
    assert exc_info.value.code == "SKILL_INTERVAL_NOT_EXPLICIT"


def test_final_review_2_skill_interval_wider_than_quoted_subperiod_is_rejected() -> None:
    """Counterexample 2: the quote supports only a short 2023-2024 project,
    but the model claims the full 2020-2025 employment span."""
    view = _employment_view_block(
        "Backend Developer 2020-2025. Used Java on a project from late 2023 into 2024."
    )
    extraction = CandidateProfileExtraction.model_validate(
        _profile(
            employment_history=[
                {
                    "title": "Backend Developer",
                    "organization": "Acme",
                    "start_date": "2020",
                    "end_date": "2025",
                    "is_current": False,
                    "evidence": [
                        {
                            "page": 1,
                            "block_index": 0,
                            "quote": "Backend Developer 2020-2025.",
                        }
                    ],
                }
            ],
            skill_experience=[
                _skill_exp(
                    "Java",
                    0,
                    "Used Java on a project from late 2023 into 2024.",
                    start="2020",  # claims the FULL job span, not the true sub-period
                    end="2025",
                )
            ],
        )
    )
    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_extraction_evidence(view, extraction)
    assert exc_info.value.code == "SKILL_INTERVAL_NOT_EXPLICIT"


def test_final_review_3_skill_cannot_borrow_employment_dates_from_a_different_quote() -> None:
    """Counterexample 3: the employment record's own dates line (2020-2025)
    is real, verbatim, verified text — but it is a DIFFERENT item's
    evidence. The skill's own cited evidence never mentions those years at
    all, so it must not be able to claim that span merely because it
    exists elsewhere in the document."""
    view = _employment_view_block(
        "Backend Developer, Acme, 2020-2025. Responsibilities included Java development."
    )
    extraction = CandidateProfileExtraction.model_validate(
        _profile(
            employment_history=[
                {
                    "title": "Backend Developer",
                    "organization": "Acme",
                    "start_date": "2020",
                    "end_date": "2025",
                    "is_current": False,
                    "evidence": [
                        {
                            "page": 1,
                            "block_index": 0,
                            "quote": "Backend Developer, Acme, 2020-2025.",
                        }
                    ],
                }
            ],
            skill_experience=[
                _skill_exp(
                    "Java",
                    0,
                    "Responsibilities included Java development.",
                    start="2020",
                    end="2025",
                )
            ],
        )
    )
    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_extraction_evidence(view, extraction)
    assert exc_info.value.code == "SKILL_INTERVAL_NOT_EXPLICIT"


def test_final_review_skill_interval_genuinely_grounded_is_accepted() -> None:
    """Positive case: the SAME quote states both the skill and its actual
    years — this must pass."""
    view = _employment_view_block("Used Java from 2020 to 2025 on backend systems at Acme.")
    extraction = CandidateProfileExtraction.model_validate(
        _profile(
            employment_history=[
                {
                    "title": "Backend Developer",
                    "organization": "Acme",
                    "start_date": "2020",
                    "end_date": "2025",
                    "is_current": False,
                    "evidence": [
                        {
                            "page": 1,
                            "block_index": 0,
                            "quote": "Used Java from 2020 to 2025 on backend systems at Acme.",
                        }
                    ],
                }
            ],
            skill_experience=[
                _skill_exp(
                    "Java",
                    0,
                    "Used Java from 2020 to 2025 on backend systems at Acme.",
                    start="2020",
                    end="2025",
                )
            ],
        )
    )
    verify_extraction_evidence(view, extraction)  # must not raise


def test_final_review_skill_interval_grounded_subject_missing_is_rejected() -> None:
    """The dates alone are not enough either — a quote that states the
    right years but never mentions the skill itself must not ground it."""
    view = _employment_view_block("Worked at Acme from 2020 to 2025 in various roles.")
    extraction = CandidateProfileExtraction.model_validate(
        _profile(
            employment_history=[
                {
                    "title": "Backend Developer",
                    "organization": "Acme",
                    "start_date": "2020",
                    "end_date": "2025",
                    "is_current": False,
                    "evidence": [
                        {
                            "page": 1,
                            "block_index": 0,
                            "quote": "Worked at Acme from 2020 to 2025 in various roles.",
                        }
                    ],
                }
            ],
            skill_experience=[
                _skill_exp(
                    "Java",
                    0,
                    "Worked at Acme from 2020 to 2025 in various roles.",
                    start="2020",
                    end="2025",
                )
            ],
        )
    )
    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_extraction_evidence(view, extraction)
    assert exc_info.value.code == "SKILL_INTERVAL_NOT_EXPLICIT"


def test_final_review_domain_interval_not_grounded_is_rejected() -> None:
    """Domain mirror: explicit AML language is present (passes
    domain_term_present), but the claimed 2020-2025 interval is never
    stated in the same evidence."""
    view = _employment_view_block("AML compliance officer at Acme.")
    extraction = CandidateProfileExtraction.model_validate(
        _profile(
            employment_history=[
                {
                    "title": "Compliance Officer",
                    "organization": "Acme",
                    "start_date": "2020",
                    "end_date": "2025",
                    "is_current": False,
                    "evidence": [
                        {"page": 1, "block_index": 0, "quote": "AML compliance officer at Acme."}
                    ],
                }
            ],
            domain_experience=[
                _domain_exp("AML", 0, "AML compliance officer at Acme.", start="2020", end="2025")
            ],
        )
    )
    with pytest.raises(EvidenceValidationError) as exc_info:
        verify_extraction_evidence(view, extraction)
    assert exc_info.value.code == "DOMAIN_INTERVAL_NOT_EXPLICIT"


def test_final_review_domain_interval_genuinely_grounded_is_accepted() -> None:
    view = _employment_view_block("AML compliance officer at Acme, 2020 to 2025.")
    extraction = CandidateProfileExtraction.model_validate(
        _profile(
            employment_history=[
                {
                    "title": "Compliance Officer",
                    "organization": "Acme",
                    "start_date": "2020",
                    "end_date": "2025",
                    "is_current": False,
                    "evidence": [
                        {
                            "page": 1,
                            "block_index": 0,
                            "quote": "AML compliance officer at Acme, 2020 to 2025.",
                        }
                    ],
                }
            ],
            domain_experience=[
                _domain_exp(
                    "AML",
                    0,
                    "AML compliance officer at Acme, 2020 to 2025.",
                    start="2020",
                    end="2025",
                )
            ],
        )
    )
    verify_extraction_evidence(view, extraction)  # must not raise


def test_final_review_open_current_interval_grounding_requires_only_the_start_year() -> None:
    """An is_current claim has no end_date to ground — only the stated
    start year needs to be present. Confirms extraction-time grounding
    verification does not spuriously reject a genuinely open-ended claim;
    the deterministic explicit-evaluation_date computation itself is
    unchanged and covered separately (see Scenario E)."""
    view = _employment_view_block("Java developer since 2020, ongoing.")
    extraction = CandidateProfileExtraction.model_validate(
        _profile(
            employment_history=[
                {
                    "title": "Java Developer",
                    "organization": "Acme",
                    "start_date": "2020",
                    "end_date": None,
                    "is_current": True,
                    "evidence": [
                        {
                            "page": 1,
                            "block_index": 0,
                            "quote": "Java developer since 2020, ongoing.",
                        }
                    ],
                }
            ],
            skill_experience=[
                _skill_exp(
                    "Java",
                    0,
                    "Java developer since 2020, ongoing.",
                    start="2020",
                    is_current=True,
                )
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
                _skill_exp("Java", 0, "Java backend work 2019-2023", start="2019", end="2023")
            ],
            domain_experience=[
                _domain_exp("AML", 0, "AML compliance work 2019-2023", start="2019", end="2023")
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
            employment_history=[_employment("A", "2015", "2025")],  # deliberately wider
            skill_experience=[
                _skill_exp("Java", 0, "Java backend work 2019-2023", start="2019", end="2023")
            ],
            domain_experience=[
                _domain_exp("AML", 0, "AML compliance work 2019-2023", start="2019", end="2023")
            ],
        )
    )
    facts = _build_profile_facts(profile)
    skill_fact = next(f for f in facts if f.category == "skill_experience")
    assert "Java" in skill_fact.title
    # The item's OWN attributable period (2019-2023), never the linked
    # employment entry's wider span (2015-2025).
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
