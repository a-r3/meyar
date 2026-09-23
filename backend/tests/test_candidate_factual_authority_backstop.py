"""Adversarial current-authority regressions for legacy completed rows.

These fixtures intentionally bypass the extraction service and insert
shape-valid COMPLETED rows, matching the audit's legacy-profile scenario.
Synthetic content only.
"""

import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fakes import FakeLLMProvider
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.agent.schemas import AgentActionType, AgentDecision, AgentTurnOutcome
from meyar.agent.service import run_agent_turn
from meyar.evaluation.policy import POLICY_ENGINE_VERSION
from meyar.evaluation.service import evaluate_candidate
from meyar.models.candidate_identity_version import IDENTITY_STATUS_COMPLETED
from meyar.schemas.candidate_identity import CandidateIdentityExtraction, IdentityFieldItem
from meyar.schemas.candidate_profile import EvidenceRef
from meyar.schemas.criteria import CriterionIn, CriterionKind, CriterionType
from meyar.scoring.policy import SCORING_POLICY_VERSION
from meyar.search.schemas import (
    CandidateSearchRequest,
    EmbeddingSearchConfig,
    RequiredFilters,
    SearchMode,
)
from meyar.search.service import search_candidates
from meyar.services.agent_conversation_repo import get_or_create_conversation
from meyar.services.browser_session_repo import create_browser_session
from meyar.services.candidate_document_repo import (
    create_candidate_document,
    create_canonical_document,
)
from meyar.services.candidate_identity_repo import create_identity_version
from meyar.services.candidate_profile_repo import create_profile_version
from meyar.services.candidate_repo import create_candidate
from meyar.services.evaluation_repo import create_evaluation
from meyar.services.job_criteria_repo import create_criteria_version
from meyar.services.job_repo import create_job
from meyar.services.tenant_repo import create_tenant
from meyar.ui.service import get_candidate_detail_view

AS_OF_DATE = date(2026, 1, 1)


async def _seed_completed_profile(
    db_session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    canonical_text: str,
    profile_content: dict,
) -> SimpleNamespace:
    candidate = await create_candidate(db_session, tenant_id=tenant_id)
    document = await create_candidate_document(
        db_session,
        tenant_id=tenant_id,
        candidate_id=candidate.id,
        original_filename="synthetic.pdf",
        mime_type="application/pdf",
        byte_size=100,
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        storage_key=f"test/{uuid.uuid4().hex}",
    )
    canonical = await create_canonical_document(
        db_session,
        tenant_id=tenant_id,
        candidate_document_id=document.id,
        parser_name="test-parser",
        parser_version="1.0.0",
        language=None,
        content={"pages": [{"page": 1, "blocks": [{"index": 0, "text": canonical_text}]}]},
    )
    profile = await create_profile_version(
        db_session,
        tenant_id=tenant_id,
        candidate_id=candidate.id,
        candidate_document_id=document.id,
        canonical_document_id=canonical.id,
        source_sha256=document.sha256_hash,
        schema_version="candidate-profile-v1",
        prompt_version="legacy-pre-current-validator",
        model_provider="fake",
        model_name="fake-model",
        model_metadata={},
        status="COMPLETED",
        profile_content=profile_content,
    )
    return SimpleNamespace(
        candidate=candidate,
        document=document,
        canonical=canonical,
        profile=profile,
    )


def _ref(quote: str) -> dict:
    return {"page": 1, "block_index": 0, "quote": quote}


def _python_claim_profile(quote: str) -> dict:
    return {
        "skills": [{"name": "Python", "category": None, "evidence": [_ref(quote)]}],
        "employment_history": [],
        "education": [],
        "certifications": [],
        "languages": [],
        "projects": [],
    }


def _profile_with(*, category: str, item: dict) -> dict:
    profile = {
        "skills": [],
        "employment_history": [],
        "education": [],
        "certifications": [],
        "languages": [],
        "projects": [],
        "skill_experience": [],
        "domain_experience": [],
    }
    profile[category] = [item]
    return profile


def _skill_criterion(value: str) -> CriterionIn:
    return CriterionIn(
        id=f"skill_{uuid.uuid4().hex[:8]}",
        kind=CriterionKind.SKILL,
        type=CriterionType.MUST_HAVE,
        label=value,
        value=value,
    )


def _embedding_config() -> EmbeddingSearchConfig:
    return EmbeddingSearchConfig(
        provider="fake-embedding",
        model_name="fake-embedding-model-v1",
        model_revision="",
        serializer_version="v1",
        embedding_dimensions=8,
    )


async def test_legacy_completed_python_claim_with_excel_only_evidence_is_not_authority(
    db_session: AsyncSession, tenant_and_user
) -> None:
    tenant, user, _password, membership = tenant_and_user
    seeded = await _seed_completed_profile(
        db_session,
        tenant_id=tenant.id,
        canonical_text="Advanced Excel",
        profile_content=_python_claim_profile("Advanced Excel"),
    )
    job = await create_job(db_session, tenant_id=tenant.id, title="Python role")
    criteria = await create_criteria_version(
        db_session,
        tenant_id=tenant.id,
        job_id=job.id,
        criteria=[_skill_criterion("Python").model_dump(mode="json")],
        created_by_api_key_id=None,
    )
    legacy_evaluation = await create_evaluation(
        db_session,
        tenant_id=tenant.id,
        candidate_id=seeded.candidate.id,
        candidate_profile_version_id=seeded.profile.id,
        job_id=job.id,
        job_criteria_version_id=criteria.id,
        status="COMPLETED",
        policy_engine_version=POLICY_ENGINE_VERSION,
        evaluation_as_of_date=AS_OF_DATE,
        numeric_score=Decimal("100.00"),
        scoring_policy_version=SCORING_POLICY_VERSION,
        score_explanation={"legacy": True},
        overall_result="STRONG_MATCH",
        criterion_results=[{"reason_code": "SKILL_EXPLICIT_MATCH"}],
    )
    browser_session, _raw = await create_browser_session(
        db_session, user_id=user.id, tenant_membership_id=membership.id, ttl_hours=8
    )
    conversation = await get_or_create_conversation(
        db_session, tenant_id=tenant.id, browser_session_id=browser_session.id
    )
    conversation.last_search_candidate_ids = [str(seeded.candidate.id)]
    await db_session.commit()

    search_response = await search_candidates(
        db_session,
        tenant_id=tenant.id,
        request=CandidateSearchRequest(
            mode=SearchMode.STRUCTURED_ONLY,
            required_filters=RequiredFilters(skills=["Python"]),
        ),
    )
    evaluation = await evaluate_candidate(
        db_session,
        tenant_id=tenant.id,
        candidate_id=seeded.candidate.id,
        candidate_profile_version_id=seeded.profile.id,
        job_id=job.id,
        job_criteria_version_id=criteria.id,
        evaluation_as_of_date=AS_OF_DATE,
    )
    agent_result = await run_agent_turn(
        db_session,
        FakeLLMProvider(
            agent_decision=AgentDecision(
                action=AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=1
            )
        ),
        tenant_id=tenant.id,
        conversation=conversation,
        user_message="Birinci namizədin profilini göstər",
        as_of_date=AS_OF_DATE,
        embedding_config=_embedding_config(),
        embedding_provider=None,
        max_tool_calls=3,
        max_context_turns=8,
    )
    detail = await get_candidate_detail_view(
        db_session, tenant_id=tenant.id, candidate_id=seeded.candidate.id
    )

    assert search_response.result_count == 0
    assert evaluation.status == "FAILED"
    assert evaluation.error_code == "PROFILE_EVIDENCE_UNSUPPORTED"
    assert evaluation.id != legacy_evaluation.id
    assert agent_result.outcome == AgentTurnOutcome.CANDIDATE_REF_NOT_FOUND
    assert agent_result.tool_results[0].profile is not None
    assert agent_result.tool_results[0].profile.found is False
    assert detail is not None
    assert detail.profile_status == "UNAVAILABLE"
    legacy_history = next(
        item for item in detail.evaluations if item.evaluation_id == legacy_evaluation.id
    )
    assert legacy_history.status == "UNAVAILABLE"
    assert legacy_history.numeric_score is None
    assert legacy_history.fit_band is None


async def test_valid_completed_profile_remains_searchable_and_evaluable(
    db_session: AsyncSession,
) -> None:
    tenant = await create_tenant(db_session, name=f"AuthorityTenant-{uuid.uuid4().hex[:8]}")
    seeded = await _seed_completed_profile(
        db_session,
        tenant_id=tenant.id,
        canonical_text="Python",
        profile_content=_python_claim_profile("Python"),
    )
    job = await create_job(db_session, tenant_id=tenant.id, title="Python role")
    criteria = await create_criteria_version(
        db_session,
        tenant_id=tenant.id,
        job_id=job.id,
        criteria=[_skill_criterion("Python").model_dump(mode="json")],
        created_by_api_key_id=None,
    )
    await db_session.commit()

    search_response = await search_candidates(
        db_session,
        tenant_id=tenant.id,
        request=CandidateSearchRequest(
            mode=SearchMode.STRUCTURED_ONLY,
            required_filters=RequiredFilters(skills=["Python"]),
        ),
    )
    evaluation = await evaluate_candidate(
        db_session,
        tenant_id=tenant.id,
        candidate_id=seeded.candidate.id,
        candidate_profile_version_id=seeded.profile.id,
        job_id=job.id,
        job_criteria_version_id=criteria.id,
        evaluation_as_of_date=AS_OF_DATE,
    )

    assert search_response.result_count == 1
    assert evaluation.status == "COMPLETED"
    assert evaluation.overall_result == "STRONG_MATCH"


@pytest.mark.parametrize(
    ("category", "bad_quote", "good_quote", "item_fields", "required_filters"),
    [
        (
            "certifications",
            "No AWS certification",
            "AWS certification",
            {"name": "AWS certification", "issuer": None, "date": None},
            RequiredFilters(certifications=["AWS certification"]),
        ),
        (
            "languages",
            "English proficiency is not C1",
            "English proficiency is C1",
            {"language": "English", "proficiency": "C1"},
            RequiredFilters(languages=["English"]),
        ),
        (
            "education",
            "No BSc degree",
            "BSc degree",
            {
                "institution": None,
                "degree": "BSc",
                "field_of_study": None,
                "date": None,
            },
            RequiredFilters(education=["BSc"]),
        ),
        (
            "employment_history",
            "Not a Backend Developer at Acme, 2021-2025",
            "Backend Developer at Acme, 2021-2025",
            {
                "title": "Backend Developer",
                "organization": "Acme",
                "start_date": "2021",
                "end_date": "2025",
                "is_current": False,
            },
            RequiredFilters(min_total_experience_years=3),
        ),
    ],
    ids=["certification", "language-proficiency", "education", "employment-aggregate"],
)
async def test_contradictory_legacy_claims_fail_search_while_positive_evidence_works(
    db_session: AsyncSession,
    category: str,
    bad_quote: str,
    good_quote: str,
    item_fields: dict,
    required_filters: RequiredFilters,
) -> None:
    tenant = await create_tenant(db_session, name=f"ClaimMatrix-{uuid.uuid4().hex[:8]}")
    bad = await _seed_completed_profile(
        db_session,
        tenant_id=tenant.id,
        canonical_text=bad_quote,
        profile_content=_profile_with(
            category=category,
            item={**item_fields, "evidence": [_ref(bad_quote)]},
        ),
    )
    good = await _seed_completed_profile(
        db_session,
        tenant_id=tenant.id,
        canonical_text=good_quote,
        profile_content=_profile_with(
            category=category,
            item={**item_fields, "evidence": [_ref(good_quote)]},
        ),
    )
    await db_session.commit()

    response = await search_candidates(
        db_session,
        tenant_id=tenant.id,
        request=CandidateSearchRequest(
            mode=SearchMode.STRUCTURED_ONLY,
            required_filters=required_filters,
            as_of_date=(
                AS_OF_DATE
                if required_filters.min_total_experience_years is not None
                else None
            ),
        ),
    )

    assert [result.candidate_id for result in response.results] == [good.candidate.id]
    assert all(result.candidate_id != bad.candidate.id for result in response.results)


async def test_contradictory_banking_domain_cannot_reach_domain_explicit_match(
    db_session: AsyncSession,
) -> None:
    tenant = await create_tenant(db_session, name=f"DomainMatrix-{uuid.uuid4().hex[:8]}")
    bad_quote = "No banking experience"
    good_quote = "Banking experience"
    bad = await _seed_completed_profile(
        db_session,
        tenant_id=tenant.id,
        canonical_text=bad_quote,
        profile_content=_profile_with(
            category="domain_experience",
            item={
                "domain": "banking",
                "employment_index": None,
                "start_date": None,
                "end_date": None,
                "is_current": False,
                "evidence": [_ref(bad_quote)],
            },
        ),
    )
    good = await _seed_completed_profile(
        db_session,
        tenant_id=tenant.id,
        canonical_text=good_quote,
        profile_content=_profile_with(
            category="domain_experience",
            item={
                "domain": "banking",
                "employment_index": None,
                "start_date": None,
                "end_date": None,
                "is_current": False,
                "evidence": [_ref(good_quote)],
            },
        ),
    )
    job = await create_job(db_session, tenant_id=tenant.id, title="Banking role")
    criterion = CriterionIn(
        id="banking_domain",
        kind=CriterionKind.DOMAIN_EXPERIENCE,
        type=CriterionType.MUST_HAVE,
        label="Banking",
        value="banking",
    )
    criteria = await create_criteria_version(
        db_session,
        tenant_id=tenant.id,
        job_id=job.id,
        criteria=[criterion.model_dump(mode="json")],
        created_by_api_key_id=None,
    )
    await db_session.commit()

    bad_evaluation = await evaluate_candidate(
        db_session,
        tenant_id=tenant.id,
        candidate_id=bad.candidate.id,
        candidate_profile_version_id=bad.profile.id,
        job_id=job.id,
        job_criteria_version_id=criteria.id,
        evaluation_as_of_date=AS_OF_DATE,
    )
    good_evaluation = await evaluate_candidate(
        db_session,
        tenant_id=tenant.id,
        candidate_id=good.candidate.id,
        candidate_profile_version_id=good.profile.id,
        job_id=job.id,
        job_criteria_version_id=criteria.id,
        evaluation_as_of_date=AS_OF_DATE,
    )

    assert bad_evaluation.status == "FAILED"
    assert bad_evaluation.error_code == "PROFILE_EVIDENCE_UNSUPPORTED"
    assert good_evaluation.status == "COMPLETED"
    assert good_evaluation.criterion_results[0]["reason_code"] == "DOMAIN_EXPLICIT_MATCH"


async def test_legacy_identity_with_unrelated_quote_is_not_presented(
    db_session: AsyncSession,
) -> None:
    tenant = await create_tenant(db_session, name=f"IdentityBackstop-{uuid.uuid4().hex[:8]}")
    seeded = await _seed_completed_profile(
        db_session,
        tenant_id=tenant.id,
        canonical_text="Jane Synthetic Doe",
        profile_content={
            "skills": [],
            "employment_history": [],
            "education": [],
            "certifications": [],
            "languages": [],
            "projects": [],
        },
    )
    await create_identity_version(
        db_session,
        tenant_id=tenant.id,
        candidate_id=seeded.candidate.id,
        candidate_document_id=seeded.document.id,
        canonical_document_id=seeded.canonical.id,
        source_sha256=seeded.document.sha256_hash,
        schema_version="candidate-identity-v1",
        prompt_version="legacy-pre-current-validator",
        model_provider="fake",
        model_name="fake-model",
        status=IDENTITY_STATUS_COMPLETED,
        identity_content=CandidateIdentityExtraction(
            full_name=IdentityFieldItem(
                value="Mallory Example", evidence=[EvidenceRef(**_ref("Jane Synthetic Doe"))]
            )
        ).model_dump(mode="json"),
    )
    await db_session.commit()

    view = await get_candidate_detail_view(
        db_session, tenant_id=tenant.id, candidate_id=seeded.candidate.id
    )

    assert view is not None
    assert view.full_name is None
