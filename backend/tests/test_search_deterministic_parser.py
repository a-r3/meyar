"""Conservative deterministic fast-path parser for explicit HR search
intents (D-026): normal supported queries (skills, languages,
certifications, total experience, simple 'və' combinations) must not
depend on the configured local planner model's interpretation quality.

Covers: the regression matrix requested for this slice (Azerbaijani
diacritics, ASCII typing variants, case, agglutinated suffixes like
"Pythonda", whitespace/CRLF, multiple skills, experience years, language,
certification, Java vs JavaScript non-collision, unsafe/control-character
rejection upstream, ambiguous expressions, unsupported concepts) plus
full-pipeline proof that a fast-path query needs no LLM call, produces a
valid SearchPlan, executes with the same structured-search semantics as
any other plan, and remains tenant-isolated and safely auditable.
"""

import uuid
from datetime import date

import pytest
from fakes import FakeLLMProvider
from search_helpers import seed_candidate_with_profile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.llm.provider import ModelUnavailableError
from meyar.models.audit_event import AuditEvent
from meyar.search.planner_policy import try_deterministic_intent_parse
from meyar.search.planner_schemas import PlannerDraft, PlannerOutcome
from meyar.search.planner_service import (
    DETERMINISTIC_PLANNER_PROVENANCE,
    plan_and_search_candidates,
    plan_candidate_search,
)
from meyar.search.schemas import EmbeddingSearchConfig, SearchMode
from meyar.services.tenant_repo import create_tenant

AS_OF_DATE = date(2026, 8, 31)


def _config() -> EmbeddingSearchConfig:
    return EmbeddingSearchConfig(
        provider="fake-embedding",
        model_name="fake-embedding-model-v1",
        model_revision="",
        serializer_version="candidate-professional-embedding-text-v1",
        embedding_dimensions=8,
    )


def _profile(skills: list[str]) -> dict:
    return {
        "skills": [
            {
                "name": skill,
                "category": None,
                "evidence": [{"page": 1, "block_index": 0, "quote": skill}],
            }
            for skill in skills
        ],
        "employment_history": [],
        "education": [],
        "certifications": [],
        "languages": [],
        "projects": [],
    }


async def _tenant(db_session: AsyncSession, prefix: str = "Deterministic"):
    tenant = await create_tenant(db_session, name=f"{prefix}-{uuid.uuid4().hex[:8]}")
    await db_session.commit()
    return tenant


async def _latest_plan_event(db_session: AsyncSession, tenant_id: uuid.UUID) -> AuditEvent:
    result = await db_session.execute(
        select(AuditEvent)
        .where(
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.event_type.in_(
                ["SEARCH_PLAN_CREATED", "SEARCH_PLAN_REJECTED", "SEARCH_PLAN_FAILED"]
            ),
        )
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
    )
    event = result.scalars().first()
    assert event is not None
    return event


def _never_called() -> FakeLLMProvider:
    return FakeLLMProvider(error=ModelUnavailableError("must not be called"))


# ---------------------------------------------------------------------------
# Regression matrix — pure unit coverage of the parser itself.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Skill-SPECIFIC duration ("N years IN skill X") is never
        # deterministically resolved — CandidateProfile cannot prove it
        # (docs/DECISIONS.md D-027). The fast path must decline (None),
        # never silently read this as "skill + total experience". Covers
        # Azerbaijani diacritics vs ASCII typing, case, whitespace/CRLF,
        # and the agglutinated locative/ablative suffix shape generally.
        "pythonda 5 il təcrübəsi olan",
        "pythonda 5 il tecrubesi olan",
        "PYTHONDA 5 il tecrubesi olan",
        "PythonDA 5 IL tecrubesi OLAN",
        "pythonda 5 il tecrubesi olan\n",
        "pythonda 5 il tecrubesi olan\r\n",
        "  pythonda 5 il tecrubesi olan  ",
        "SQL-dan 3 il təcrübəsi olan",
        "Python üzrə 5 il təcrübəsi olan",
    ],
)
def test_skill_specific_duration_never_deterministically_resolves(text: str) -> None:
    assert try_deterministic_intent_parse(text) is None


def test_multiple_skills_via_conjunction() -> None:
    draft = try_deterministic_intent_parse("Python və SQL bilən")
    assert draft is not None
    assert draft.required_filters.skills == ["Python", "SQL"]


def test_single_skill_bilen() -> None:
    draft = try_deterministic_intent_parse("Java bilən")
    assert draft is not None
    assert draft.required_filters.skills == ["Java"]


def test_language_intent() -> None:
    draft = try_deterministic_intent_parse("ingilis dili olan")
    assert draft is not None
    assert draft.required_filters.languages == ["English"]


def test_certification_intent() -> None:
    draft = try_deterministic_intent_parse("ACAMS sertifikatı olan")
    assert draft is not None
    assert draft.required_filters.certifications == ["ACAMS"]


def test_total_experience_only() -> None:
    draft = try_deterministic_intent_parse("minimum 5 il təcrübəsi olan")
    assert draft is not None
    assert draft.required_filters.min_total_experience_years == 5.0
    assert draft.required_filters.skills == []


def test_simple_conjunction_of_two_distinct_concepts() -> None:
    draft = try_deterministic_intent_parse("Java bilən və ingilis dili olan")
    assert draft is not None
    assert draft.required_filters.skills == ["Java"]
    assert draft.required_filters.languages == ["English"]


def test_java_vs_javascript_are_never_conflated() -> None:
    java = try_deterministic_intent_parse("Java bilən")
    javascript = try_deterministic_intent_parse("Javascript bilən")
    assert java is not None and javascript is not None
    assert java.required_filters.skills == ["Java"]
    assert javascript.required_filters.skills == ["Javascript"]
    assert javascript.required_filters.skills != java.required_filters.skills


@pytest.mark.parametrize(
    ("text", "expected_skills", "expected_languages", "expected_certs", "expected_years"),
    [
        # The exact "keep the fast path for unambiguous intents" list from
        # the semantic-correctness audit — all genuinely representable by
        # the existing SearchPlan, none involve skill-specific duration.
        ("Python bilən", ["Python"], [], [], None),
        ("Java bilən", ["Java"], [], [], None),
        ("Python və SQL bilən", ["Python", "SQL"], [], [], None),
        ("ümumi iş təcrübəsi ən az 5 il olan", [], [], [], 5.0),
        (
            "Python bilən və ümumi iş təcrübəsi ən az 5 il olan",
            ["Python"],
            [],
            [],
            5.0,
        ),
        ("İngilis dili bilən", [], ["English"], [], None),
        ("ACAMS sertifikatı olan", [], [], ["ACAMS"], None),
    ],
)
def test_unambiguous_intents_stay_on_the_fast_path(
    text: str,
    expected_skills: list[str],
    expected_languages: list[str],
    expected_certs: list[str],
    expected_years: float | None,
) -> None:
    draft = try_deterministic_intent_parse(text)
    assert draft is not None, f"expected a deterministic draft for {text!r}"
    assert draft.required_filters.skills == expected_skills
    assert draft.required_filters.languages == expected_languages
    assert draft.required_filters.certifications == expected_certs
    assert draft.required_filters.min_total_experience_years == expected_years


@pytest.mark.parametrize(
    "text",
    [
        # Ambiguous / genuinely out-of-scope for a fixed SearchPlan field:
        # skill-SPECIFIC duration ("N years IN skill X") has no
        # representation distinct from total experience — must not guess.
        "Python üzrə ən az 5 il təcrübəsi olan",
        "5 il Java təcrübəsi olan namizədləri göstər.",
        # An unrecognized qualifier before "təcrübəsi" is not silently
        # dropped — the whole request must not partially match.
        "minimum 5 il backend təcrübəsi olan",
        # Free-text / semantic requests have no fixed-field representation.
        "Find candidates experienced in modernizing legacy backend systems.",
        "Uyğun namizəd tap.",
        # A preferred (not required) marker changes MUST_HAVE/PREFERRED
        # semantics — needs the LLM + existing fidelity nuance.
        "Java üstünlükdür",
        "Python is preferred.",
        # A protected/sensitive attribute — must fall through to the same
        # precheck path as ordinary LLM-bound requests, never matched here.
        "30 yaşdan aşağı namizədləri göstər.",
        # An unsupported language not in the recognized alias catalog.
        "fransız dili olan",
        # Multiple experience mentions in one request — ambiguous, must
        # not silently pick one.
        "Java bilən və minimum 5 il təcrübəsi olan və minimum 3 il təcrübəsi olan",
    ],
)
def test_unsupported_or_ambiguous_requests_decline_to_none(text: str) -> None:
    assert try_deterministic_intent_parse(text) is None


def test_empty_and_oversized_requests_decline() -> None:
    assert try_deterministic_intent_parse("") is None
    assert try_deterministic_intent_parse("   ") is None
    assert try_deterministic_intent_parse("a" * 5000) is None


# ---------------------------------------------------------------------------
# Full-pipeline proof: no LLM call, valid SearchPlan, real execution,
# tenant isolation, safe auditability.
# ---------------------------------------------------------------------------


async def test_deterministic_fast_path_requires_no_llm_call(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    never_called = _never_called()

    result = await plan_candidate_search(
        db_session,
        never_called,
        tenant_id=tenant.id,
        natural_language_request="Java bilən",
        as_of_date=AS_OF_DATE,
        embedding_config=_config(),
    )

    assert never_called.call_count == 0
    assert result.executable
    assert result.outcome == PlannerOutcome.EXECUTABLE
    assert result.attempt_count == 0
    assert result.search_request is not None
    assert result.search_request.mode == SearchMode.STRUCTURED_ONLY
    assert result.search_request.required_filters.skills == ["Java"]
    assert result.model_provider == DETERMINISTIC_PLANNER_PROVENANCE.provider
    assert result.model_name == DETERMINISTIC_PLANNER_PROVENANCE.model_name


async def test_deterministic_fast_path_search_plan_passes_existing_validation(
    db_session: AsyncSession,
) -> None:
    """The produced CandidateSearchRequest is the exact same Pydantic
    model the LLM path constructs — proves no second, weaker schema. Uses
    the explicit-separation phrasing (skill and total-experience stated as
    two distinct clauses), the only case where combining both is honest."""
    tenant = await _tenant(db_session)
    result = await plan_candidate_search(
        db_session,
        _never_called(),
        tenant_id=tenant.id,
        natural_language_request="Python bilən və ümumi iş təcrübəsi ən az 5 il olan",
        as_of_date=AS_OF_DATE,
        embedding_config=_config(),
    )
    assert result.executable
    request = result.search_request
    assert request is not None
    assert request.mode == SearchMode.STRUCTURED_ONLY
    assert request.embedding_config is None
    assert request.as_of_date == AS_OF_DATE
    assert request.required_filters.min_total_experience_years == 5.0
    assert request.required_filters.skills == ["Python"]


async def test_deterministic_fast_path_executes_structured_search_correctly(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session)
    matching, _p1 = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(["Java"])
    )
    non_matching, _p2 = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(["SQL"])
    )
    await db_session.commit()

    response = await plan_and_search_candidates(
        db_session,
        _never_called(),
        tenant_id=tenant.id,
        natural_language_request="Java bilən",
        as_of_date=AS_OF_DATE,
        embedding_config=_config(),
    )

    assert response.plan.executable
    assert response.search_response is not None
    result_ids = {r.candidate_id for r in response.search_response.results}
    assert matching.id in result_ids
    assert non_matching.id not in result_ids


async def test_deterministic_fast_path_preserves_tenant_isolation(
    db_session: AsyncSession,
) -> None:
    tenant_a = await _tenant(db_session, prefix="TenantA")
    tenant_b = await _tenant(db_session, prefix="TenantB")
    candidate_a, _pa = await seed_candidate_with_profile(
        db_session, tenant_id=tenant_a.id, profile_content=_profile(["Java"])
    )
    candidate_b, _pb = await seed_candidate_with_profile(
        db_session, tenant_id=tenant_b.id, profile_content=_profile(["Java"])
    )
    await db_session.commit()

    response = await plan_and_search_candidates(
        db_session,
        _never_called(),
        tenant_id=tenant_a.id,
        natural_language_request="Java bilən",
        as_of_date=AS_OF_DATE,
        embedding_config=_config(),
    )

    assert response.search_response is not None
    result_ids = {r.candidate_id for r in response.search_response.results}
    assert candidate_a.id in result_ids
    assert candidate_b.id not in result_ids


async def test_deterministic_fast_path_is_safely_audited(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    await plan_candidate_search(
        db_session,
        _never_called(),
        tenant_id=tenant.id,
        natural_language_request="ACAMS sertifikatı olan",
        as_of_date=AS_OF_DATE,
        embedding_config=_config(),
    )

    event = await _latest_plan_event(db_session, tenant.id)
    assert event.event_type == "SEARCH_PLAN_CREATED"
    assert event.event_metadata["provider"] == "meyar-deterministic"
    assert event.event_metadata["model_name"] == "meyar-deterministic-parser-v1"
    assert event.event_metadata["attempt_count"] == 0
    # No raw request text, candidate content, or PII in audit metadata —
    # same PII-safety contract as the LLM path (existing D-0xx guarantee).
    serialized = str(event.event_metadata)
    assert "ACAMS sertifikatı olan" not in serialized


async def test_skill_specific_duration_rejected_deterministically_with_audit(
    db_session: AsyncSession,
) -> None:
    """The precheck rejection for skill-specific duration (D-027) still
    goes through zero LLM calls and is still safely audited — proving the
    clarification flow built on top of this (meyar.ui.router) inherits
    the same tenant-isolation and PII-safety guarantees."""
    tenant = await _tenant(db_session)
    result = await plan_candidate_search(
        db_session,
        _never_called(),
        tenant_id=tenant.id,
        natural_language_request="pythonda 5 il tecrübesi olan",
        as_of_date=AS_OF_DATE,
        embedding_config=_config(),
    )
    assert not result.executable
    assert result.outcome == PlannerOutcome.UNSUPPORTED_SEMANTICS
    assert result.attempt_count == 0

    event = await _latest_plan_event(db_session, tenant.id)
    assert event.event_type == "SEARCH_PLAN_REJECTED"
    assert "SKILL_SPECIFIC_EXPERIENCE_DURATION_UNSUPPORTED" in event.event_metadata["reason_codes"]
    assert "pythonda 5 il tecrübesi olan" not in str(event.event_metadata)


async def test_confirmed_explicit_separation_alternative_preserves_tenant_isolation(
    db_session: AsyncSession,
) -> None:
    """The clarification screen's confirmed alternative (skill + total
    experience, explicit-separation phrasing) is itself just an ordinary
    deterministic fast-path request — tenant isolation holds exactly as
    for any other fast-path search."""
    tenant_a = await _tenant(db_session, prefix="ClarifyA")
    tenant_b = await _tenant(db_session, prefix="ClarifyB")
    experienced_profile = {
        **_profile(["Python"]),
        "employment_history": [
            {
                "title": "Backend Engineer",
                "organization": "Synthetic Co",
                "start_date": "2018",
                "end_date": None,
                "is_current": True,
                "evidence": [{"page": 1, "block_index": 0, "quote": "2018-present"}],
            }
        ],
    }
    candidate_a, _pa = await seed_candidate_with_profile(
        db_session, tenant_id=tenant_a.id, profile_content=experienced_profile
    )
    candidate_b, _pb = await seed_candidate_with_profile(
        db_session, tenant_id=tenant_b.id, profile_content=experienced_profile
    )
    await db_session.commit()

    confirmed_query = "python bilən və ümumi iş təcrübəsi 5 il olan"
    response = await plan_and_search_candidates(
        db_session,
        _never_called(),
        tenant_id=tenant_a.id,
        natural_language_request=confirmed_query,
        as_of_date=AS_OF_DATE,
        embedding_config=_config(),
    )
    assert response.plan.executable
    assert response.search_response is not None
    result_ids = {r.candidate_id for r in response.search_response.results}
    assert candidate_a.id in result_ids
    assert candidate_b.id not in result_ids


async def test_declined_deterministic_request_falls_through_to_llm(
    db_session: AsyncSession,
) -> None:
    """An out-of-scope request still reaches the LLM planner exactly as
    before — the fast path never blocks or alters the existing path."""
    tenant = await _tenant(db_session)
    fake = FakeLLMProvider(
        planner_draft=PlannerDraft(semantic_query="modernizing legacy backend systems")
    )
    query = "Find candidates experienced in modernizing legacy backend systems."
    result = await plan_candidate_search(
        db_session,
        fake,
        tenant_id=tenant.id,
        natural_language_request=query,
        as_of_date=AS_OF_DATE,
        embedding_config=_config(),
    )
    assert fake.call_count == 1
    assert result.executable
    assert result.model_provider == "fake"
