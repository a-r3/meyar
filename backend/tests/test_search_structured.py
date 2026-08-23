"""Slice 8 — STRUCTURED_ONLY candidate search. Synthetic content only.
See docs/DECISIONS.md (meyar-search-v1) and .claude/rules/testing.md."""

import uuid

import pytest
from fakes import FakeEmbeddingProvider
from pydantic import ValidationError
from search_helpers import (
    DEFAULT_AS_OF_DATE,
    seed_candidate_with_profile,
    seed_next_profile_version,
)
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.search.schemas import (
    CandidateSearchRequest,
    PreferredFilters,
    RequiredFilters,
    SearchMode,
)
from meyar.search.service import search_candidates
from meyar.services.tenant_repo import create_tenant


def _evidence():
    return [{"page": 1, "block_index": 0, "quote": "synthetic evidence"}]


def _profile(
    *,
    skills: list[str] | None = None,
    certifications: list[str] | None = None,
    languages: list[tuple[str, str | None]] | None = None,
    education: list[tuple[str, str]] | None = None,
    employment: list[dict] | None = None,
) -> dict:
    return {
        "skills": [{"name": s, "category": None, "evidence": _evidence()} for s in (skills or [])],
        "employment_history": employment or [],
        "education": [
            {
                "institution": "Synthetic University",
                "degree": degree,
                "field_of_study": field,
                "date": None,
                "evidence": _evidence(),
            }
            for degree, field in (education or [])
        ],
        "certifications": [
            {"name": c, "issuer": None, "date": None, "evidence": _evidence()}
            for c in (certifications or [])
        ],
        "languages": [
            {"language": lang, "proficiency": level, "evidence": _evidence()}
            for lang, level in (languages or [])
        ],
        "projects": [],
    }


def _employment(title: str, start: str, end: str | None, *, is_current: bool = False) -> dict:
    return {
        "title": title,
        "organization": "Synthetic Co",
        "start_date": start,
        "end_date": end,
        "is_current": is_current,
        "evidence": _evidence(),
    }


async def _tenant(db_session: AsyncSession, name: str = "SearchTenant"):
    tenant = await create_tenant(db_session, name=f"{name}-{uuid.uuid4().hex[:8]}")
    await db_session.commit()
    return tenant


# --- Basic structured filters ------------------------------------------


async def test_no_filters_returns_all_eligible_candidates_bounded(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(skills=["Python"])
    )
    await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(skills=["Rust"])
    )
    await db_session.commit()

    request = CandidateSearchRequest(mode=SearchMode.STRUCTURED_ONLY, limit=10)
    response = await search_candidates(db_session, tenant_id=tenant.id, request=request)

    assert response.eligible_profile_count == 2
    assert response.result_count == 2


async def test_required_skill_match_includes_candidate(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    candidate, _ = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(skills=["Python"])
    )
    await db_session.commit()

    request = CandidateSearchRequest(
        mode=SearchMode.STRUCTURED_ONLY,
        required_filters=RequiredFilters(skills=["Python"]),
    )
    response = await search_candidates(db_session, tenant_id=tenant.id, request=request)

    assert response.result_count == 1
    assert response.results[0].candidate_id == candidate.id


async def test_required_skill_non_match_excludes_candidate(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(skills=["Rust"])
    )
    await db_session.commit()

    request = CandidateSearchRequest(
        mode=SearchMode.STRUCTURED_ONLY,
        required_filters=RequiredFilters(skills=["Python"]),
    )
    response = await search_candidates(db_session, tenant_id=tenant.id, request=request)

    assert response.result_count == 0
    assert response.eligible_profile_count == 0


async def test_multiple_required_skills_all_must_match(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    candidate_both, _ = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(skills=["Python", "SQL"])
    )
    await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(skills=["Python"])
    )
    await db_session.commit()

    request = CandidateSearchRequest(
        mode=SearchMode.STRUCTURED_ONLY,
        required_filters=RequiredFilters(skills=["Python", "SQL"]),
    )
    response = await search_candidates(db_session, tenant_id=tenant.id, request=request)

    assert response.result_count == 1
    assert response.results[0].candidate_id == candidate_both.id


async def test_preferred_skill_affects_score_and_ranking(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    strong, _ = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(skills=["Python", "AWS"])
    )
    weak, _ = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(skills=["Python"])
    )
    await db_session.commit()

    request = CandidateSearchRequest(
        mode=SearchMode.STRUCTURED_ONLY,
        required_filters=RequiredFilters(skills=["Python"]),
        preferred_filters=PreferredFilters(skills=["AWS"]),
    )
    response = await search_candidates(db_session, tenant_id=tenant.id, request=request)

    assert response.result_count == 2
    assert response.results[0].candidate_id == strong.id
    assert response.results[0].structured_score == 1.0
    assert response.results[1].candidate_id == weak.id
    assert response.results[1].structured_score == 0.0


async def test_certification_filter(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    candidate, _ = await seed_candidate_with_profile(
        db_session,
        tenant_id=tenant.id,
        profile_content=_profile(certifications=["AWS Certified Solutions Architect"]),
    )
    await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(certifications=["PMP"])
    )
    await db_session.commit()

    request = CandidateSearchRequest(
        mode=SearchMode.STRUCTURED_ONLY,
        required_filters=RequiredFilters(certifications=["AWS Certified Solutions Architect"]),
    )
    response = await search_candidates(db_session, tenant_id=tenant.id, request=request)

    assert response.result_count == 1
    assert response.results[0].candidate_id == candidate.id


async def test_language_filter(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    candidate, _ = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(languages=[("English", "Fluent")])
    )
    await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(languages=[("French", None)])
    )
    await db_session.commit()

    request = CandidateSearchRequest(
        mode=SearchMode.STRUCTURED_ONLY,
        required_filters=RequiredFilters(languages=["English"]),
    )
    response = await search_candidates(db_session, tenant_id=tenant.id, request=request)

    assert response.result_count == 1
    assert response.results[0].candidate_id == candidate.id


async def test_education_filter(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    candidate, _ = await seed_candidate_with_profile(
        db_session,
        tenant_id=tenant.id,
        profile_content=_profile(education=[("Bachelor", "Computer Science")]),
    )
    await seed_candidate_with_profile(
        db_session,
        tenant_id=tenant.id,
        profile_content=_profile(education=[("Bachelor", "Biology")]),
    )
    await db_session.commit()

    request = CandidateSearchRequest(
        mode=SearchMode.STRUCTURED_ONLY,
        required_filters=RequiredFilters(education=["Computer Science"]),
    )
    response = await search_candidates(db_session, tenant_id=tenant.id, request=request)

    assert response.result_count == 1
    assert response.results[0].candidate_id == candidate.id


async def test_experience_threshold_filter(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    senior, _ = await seed_candidate_with_profile(
        db_session,
        tenant_id=tenant.id,
        profile_content=_profile(employment=[_employment("Engineer", "2016", "2026")]),
    )
    junior, _ = await seed_candidate_with_profile(
        db_session,
        tenant_id=tenant.id,
        profile_content=_profile(employment=[_employment("Engineer", "2024", "2026")]),
    )
    await db_session.commit()

    request = CandidateSearchRequest(
        mode=SearchMode.STRUCTURED_ONLY,
        required_filters=RequiredFilters(min_total_experience_years=5),
        as_of_date=DEFAULT_AS_OF_DATE,
    )
    response = await search_candidates(db_session, tenant_id=tenant.id, request=request)

    result_ids = {r.candidate_id for r in response.results}
    assert senior.id in result_ids
    assert junior.id not in result_ids


async def test_as_of_date_makes_current_employment_deterministic(db_session: AsyncSession) -> None:
    """A candidate with an ongoing ("Present") role must compute the same
    total experience regardless of when the test actually runs — only
    as_of_date drives the reference year, never wall-clock "now"."""
    tenant = await _tenant(db_session)
    candidate, _ = await seed_candidate_with_profile(
        db_session,
        tenant_id=tenant.id,
        profile_content=_profile(
            employment=[_employment("Engineer", "2020", None, is_current=True)]
        ),
    )
    await db_session.commit()

    request = CandidateSearchRequest(
        mode=SearchMode.STRUCTURED_ONLY,
        required_filters=RequiredFilters(min_total_experience_years=6),
        as_of_date=DEFAULT_AS_OF_DATE,  # 2026-01-01 -> exactly 6 years
    )
    response_a = await search_candidates(db_session, tenant_id=tenant.id, request=request)
    response_b = await search_candidates(db_session, tenant_id=tenant.id, request=request)

    assert {r.candidate_id for r in response_a.results} == {candidate.id}
    ids_a = {r.candidate_id for r in response_a.results}
    ids_b = {r.candidate_id for r in response_b.results}
    assert ids_b == ids_a


def test_missing_as_of_date_rejected_when_experience_filter_set() -> None:
    with pytest.raises(ValidationError):
        CandidateSearchRequest(
            mode=SearchMode.STRUCTURED_ONLY,
            required_filters=RequiredFilters(min_total_experience_years=3),
        )


# --- Protected/sensitive criteria ---------------------------------------


def test_protected_criterion_rejected_in_required_filters() -> None:
    with pytest.raises(ValidationError):
        CandidateSearchRequest(
            mode=SearchMode.STRUCTURED_ONLY,
            required_filters=RequiredFilters(skills=["gender"]),
        )


def test_protected_term_rejected_in_semantic_query() -> None:
    with pytest.raises(ValidationError):
        CandidateSearchRequest(
            mode=SearchMode.SEMANTIC_ONLY,
            semantic_query="candidates who are male",
            embedding_config={
                "provider": "fake-embedding",
                "model_name": "fake-embedding-model-v1",
                "serializer_version": "candidate-professional-embedding-text-v1",
                "embedding_dimensions": 8,
            },
        )


# --- Current profile only -----------------------------------------------


async def test_only_current_profile_version_is_searched(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    candidate, _v1 = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(skills=["Python"])
    )
    v2 = await seed_next_profile_version(
        db_session,
        tenant_id=tenant.id,
        candidate=candidate,
        profile_content=_profile(skills=["Rust"]),
    )
    await db_session.commit()

    # Required filter on the OLD (v1) skill must now fail — v1 is stale.
    stale_request = CandidateSearchRequest(
        mode=SearchMode.STRUCTURED_ONLY,
        required_filters=RequiredFilters(skills=["Python"]),
    )
    stale_response = await search_candidates(db_session, tenant_id=tenant.id, request=stale_request)
    assert stale_response.result_count == 0

    # The NEW (v2) skill must match, and the result must reference v2.
    current_request = CandidateSearchRequest(
        mode=SearchMode.STRUCTURED_ONLY,
        required_filters=RequiredFilters(skills=["Rust"]),
    )
    current_response = await search_candidates(
        db_session, tenant_id=tenant.id, request=current_request
    )
    assert current_response.result_count == 1
    assert current_response.results[0].candidate_id == candidate.id
    assert current_response.results[0].candidate_profile_version_id == v2.id


# --- No embedding provider call in STRUCTURED_ONLY ----------------------


async def test_structured_only_never_calls_embedding_provider(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(skills=["Python"])
    )
    await db_session.commit()

    from meyar.embedding.provider import EmbeddingUnavailableError

    failing_provider = FakeEmbeddingProvider(error=EmbeddingUnavailableError("must not be called"))
    request = CandidateSearchRequest(mode=SearchMode.STRUCTURED_ONLY)

    response = await search_candidates(
        db_session, tenant_id=tenant.id, request=request, embedding_provider=failing_provider
    )

    assert response.result_count == 1
    assert failing_provider.call_count == 0


# --- Stable tie-break -----------------------------------------------------


async def test_stable_tie_break_by_candidate_id(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    candidate_a, _ = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(skills=["Python"])
    )
    candidate_b, _ = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(skills=["Python"])
    )
    await db_session.commit()

    request = CandidateSearchRequest(mode=SearchMode.STRUCTURED_ONLY)
    response = await search_candidates(db_session, tenant_id=tenant.id, request=request)

    expected_order = sorted([candidate_a.id, candidate_b.id], key=str)
    assert [r.candidate_id for r in response.results] == expected_order


# --- Tenant isolation -----------------------------------------------------


async def test_tenant_isolation_structured_search(db_session: AsyncSession) -> None:
    tenant_a = await _tenant(db_session, "TenantA")
    tenant_b = await _tenant(db_session, "TenantB")
    await seed_candidate_with_profile(
        db_session, tenant_id=tenant_a.id, profile_content=_profile(skills=["Python"])
    )
    await seed_candidate_with_profile(
        db_session, tenant_id=tenant_b.id, profile_content=_profile(skills=["Python"])
    )
    await db_session.commit()

    request = CandidateSearchRequest(mode=SearchMode.STRUCTURED_ONLY)
    response_a = await search_candidates(db_session, tenant_id=tenant_a.id, request=request)
    response_b = await search_candidates(db_session, tenant_id=tenant_b.id, request=request)

    assert response_a.result_count == 1
    assert response_b.result_count == 1
    assert {r.candidate_id for r in response_a.results}.isdisjoint(
        {r.candidate_id for r in response_b.results}
    )
