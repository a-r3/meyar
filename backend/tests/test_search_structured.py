"""Slice 8 — STRUCTURED_ONLY candidate search. Synthetic content only.
See docs/DECISIONS.md (meyar-search-v1) and .claude/rules/testing.md."""

import uuid
from datetime import date

import pytest
from fakes import FakeEmbeddingProvider
from pydantic import ValidationError
from search_helpers import (
    DEFAULT_AS_OF_DATE,
    seed_candidate_with_profile,
    seed_next_profile_version,
    synthetic_evidence,
)
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.schemas.candidate_profile import CandidateProfileExtraction
from meyar.search.schemas import (
    CandidateSearchRequest,
    LanguageLevelFilter,
    NamedDurationFilter,
    PreferredFilterMatch,
    PreferredFilters,
    RequiredFilterMatch,
    RequiredFilters,
    SearchMode,
)
from meyar.search.service import search_candidates
from meyar.search.structured import (
    _certification_present,
    evaluate_preferred_filters,
    evaluate_required_filters,
)
from meyar.services.tenant_repo import create_tenant
from meyar.ui.service import _evidence_views, _requirement_attributable_evidence


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
        "skills": [
            {"name": s, "category": None, "evidence": synthetic_evidence(s)} for s in (skills or [])
        ],
        "employment_history": employment or [],
        "education": [
            {
                "institution": "Synthetic University",
                "degree": degree,
                "field_of_study": field,
                "date": None,
                "evidence": synthetic_evidence("Synthetic University", degree, field),
            }
            for degree, field in (education or [])
        ],
        "certifications": [
            {"name": c, "issuer": None, "date": None, "evidence": synthetic_evidence(c)}
            for c in (certifications or [])
        ],
        "languages": [
            {"language": lang, "proficiency": level, "evidence": synthetic_evidence(lang, level)}
            for lang, level in (languages or [])
        ],
        "projects": [],
    }


def test_curated_acams_alias_matches_only_the_canonical_certification() -> None:
    canonical = CandidateProfileExtraction.model_validate(
        _profile(certifications=["ACAMS Certified Anti-Money Laundering Specialist"])
    )
    unrelated = CandidateProfileExtraction.model_validate(
        _profile(certifications=["ACAMS Advanced CAMS-Risk Management"])
    )
    unproven = CandidateProfileExtraction.model_validate(_profile(certifications=["ACAMS"]))
    assert _certification_present(canonical, "ACAMS")
    assert _certification_present(canonical, "ACAMS Certified Anti-Money Laundering Specialist")
    assert not _certification_present(unrelated, "ACAMS")
    assert not _certification_present(unproven, "ACAMS")
    assert not _certification_present(canonical, "Money Laundering")
    assert _requirement_attributable_evidence(
        canonical, [RequiredFilterMatch(category="certification", value="ACAMS")]
    ) == canonical.certifications[0].evidence
    assert _requirement_attributable_evidence(
        unproven, [RequiredFilterMatch(category="certification", value="ACAMS")]
    ) == []


def _employment(title: str, start: str, end: str | None, *, is_current: bool = False) -> dict:
    return {
        "title": title,
        "organization": "Synthetic Co",
        "start_date": start,
        "end_date": end,
        "is_current": is_current,
        "evidence": synthetic_evidence(
            title, "Synthetic Co", start, end, "present" if is_current else None
        ),
    }


def test_typed_match_evidence_uses_only_duration_and_level_facts() -> None:
    profile = CandidateProfileExtraction.model_validate(
        {
            **_profile(skills=["Python"], languages=[("English", "C1"), ("English", "B1")]),
            "employment_history": [_employment("Engineer", "2010", "2025")],
            "skill_experience": [
                {
                    "skill_name": "Python",
                    "employment_index": 0,
                    "start_date": start,
                    "end_date": end,
                    "evidence": synthetic_evidence("Python", start, end),
                }
                for start, end in (("2019", "2022"), ("2022", "2025"))
            ]
            + [
                {
                    "skill_name": "Java",
                    "employment_index": 0,
                    "start_date": "2019",
                    "end_date": "2025",
                    "evidence": synthetic_evidence("Java", "2019", "2025"),
                }
            ],
        }
    )
    filters = RequiredFilters(
        skill_experience=[NamedDurationFilter(value="Python", min_years=5)],
        language_levels=[LanguageLevelFilter(value="English", required_level="B2")],
    )
    required = evaluate_required_filters(
        profile, filters, as_of_year=2025, as_of_date=date(2025, 12, 31)
    )
    assert required.satisfied
    refs = _requirement_attributable_evidence(profile, required.matches)
    assert {ref.quote for ref in refs} == {
        "Python 2019 2022",
        "Python 2022 2025",
        "English C1",
    }
    preferred = evaluate_preferred_filters(
        profile,
        PreferredFilters.model_validate(filters.model_dump()),
        as_of_year=2025,
        as_of_date=date(2025, 12, 31),
    )
    assert preferred.score == 1.0
    assert _requirement_attributable_evidence(profile, preferred.matches) == refs
    assert [ref.quote for ref in _requirement_attributable_evidence(
        profile, [RequiredFilterMatch(category="language", value="English")]
    )] == ["English C1", "English B1"]


def test_short_skill_duration_cannot_borrow_total_career_evidence() -> None:
    profile = CandidateProfileExtraction.model_validate(
        {
            **_profile(skills=["Python"]),
            "employment_history": [_employment("Engineer", "2010", "2025")],
            "skill_experience": [
                {
                    "skill_name": "Python",
                    "employment_index": 0,
                    "start_date": "2023",
                    "end_date": "2025",
                    "evidence": synthetic_evidence("Python", "2023", "2025"),
                }
            ],
        }
    )
    result = evaluate_required_filters(
        profile,
        RequiredFilters(skill_experience=[NamedDurationFilter(value="Python", min_years=5)]),
        as_of_year=2025,
        as_of_date=date(2025, 12, 31),
    )
    assert not result.satisfied
    assert _requirement_attributable_evidence(profile, result.matches) == []
    assert _requirement_attributable_evidence(
        profile, [PreferredFilterMatch(category="skill_experience", value="Python")]
    ) == []


def test_search_evidence_deduplicates_same_exact_ref() -> None:
    profile = CandidateProfileExtraction.model_validate(
        {
            **_profile(skills=["Python"]),
            "employment_history": [_employment("Engineer", "2019", "2025")],
            "skill_experience": [
                {
                    "skill_name": "Python",
                    "employment_index": 0,
                    "start_date": "2019",
                    "end_date": "2025",
                    "evidence": synthetic_evidence("Python"),
                }
            ],
        }
    )
    matched = evaluate_required_filters(
        profile,
        RequiredFilters(
            skills=["Python"],
            skill_experience=[NamedDurationFilter(value="Python", min_years=5)],
        ),
        as_of_year=2025,
        as_of_date=date(2025, 12, 31),
    )
    assert matched.satisfied
    refs = _requirement_attributable_evidence(profile, matched.matches)
    assert len(refs) == 2
    assert len(_evidence_views(refs, snippets=True)) == 1


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


async def test_duplicate_language_fact_order_does_not_change_search_or_evidence(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session, "LanguageOrder")
    profile = _profile(languages=[("English", "B1"), ("English", "C1")])
    candidate, _ = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=profile
    )
    await db_session.commit()
    required_request = CandidateSearchRequest(
        mode=SearchMode.STRUCTURED_ONLY,
        required_filters=RequiredFilters(
            language_levels=[LanguageLevelFilter(value="English", required_level="B2")]
        ),
    )
    preferred_request = CandidateSearchRequest(
        mode=SearchMode.STRUCTURED_ONLY,
        preferred_filters=PreferredFilters(
            language_levels=[LanguageLevelFilter(value="English", required_level="B2")]
        ),
    )
    bare_request = CandidateSearchRequest(
        mode=SearchMode.STRUCTURED_ONLY,
        required_filters=RequiredFilters(languages=["English"]),
    )

    async def assert_same_result() -> None:
        required = await search_candidates(
            db_session, tenant_id=tenant.id, request=required_request
        )
        preferred = await search_candidates(
            db_session, tenant_id=tenant.id, request=preferred_request
        )
        bare = await search_candidates(db_session, tenant_id=tenant.id, request=bare_request)
        assert [item.candidate_id for item in required.results] == [candidate.id]
        assert [item.candidate_id for item in preferred.results] == [candidate.id]
        assert [item.candidate_id for item in bare.results] == [candidate.id]
        assert preferred.results[0].structured_score == 1.0
        assert [item.category for item in preferred.results[0].preferred_filters_matched] == [
            "language_level"
        ]
        from meyar.services.profile_authority import get_current_authorized_profile

        current = await get_current_authorized_profile(
            db_session, tenant_id=tenant.id, candidate_id=candidate.id
        )
        assert current is not None
        _, authorized_profile = current
        refs = _requirement_attributable_evidence(
            authorized_profile, required.results[0].required_filters_matched
        )
        assert [ref.quote for ref in refs] == ["English C1"]
        preferred_refs = _requirement_attributable_evidence(
            authorized_profile, preferred.results[0].preferred_filters_matched
        )
        assert [ref.quote for ref in preferred_refs] == ["English C1"]
        bare_refs = _requirement_attributable_evidence(
            authorized_profile, bare.results[0].required_filters_matched
        )
        assert {ref.quote for ref in bare_refs} == {"English B1", "English C1"}

    await assert_same_result()
    await seed_next_profile_version(
        db_session,
        tenant_id=tenant.id,
        candidate=candidate,
        profile_content=_profile(languages=[("English", "C1"), ("English", "B1")]),
    )
    await db_session.commit()
    await assert_same_result()


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
