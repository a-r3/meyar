import uuid
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.evaluation.service import EvaluationInputError
from meyar.evaluation.service import evaluate_candidate as _evaluate_candidate
from meyar.schemas.criteria import CriterionIn, CriterionKind, CriterionType
from meyar.services.candidate_document_repo import (
    get_candidate_document,
    get_latest_canonical_document,
)
from meyar.services.candidate_profile_repo import create_profile_version
from meyar.services.evaluation_repo import get_evaluation
from meyar.services.job_criteria_repo import create_criteria_version
from meyar.services.job_repo import create_job
from meyar.services.tenant_repo import create_tenant

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "synthetic_cvs"
_AS_OF_DATE = date(2026, 1, 1)


async def evaluate_candidate(db_session: AsyncSession, **kwargs):
    return await _evaluate_candidate(db_session, evaluation_as_of_date=_AS_OF_DATE, **kwargs)


def _auth(plaintext: str) -> dict:
    return {"Authorization": f"Bearer {plaintext}"}


async def _make_setup(
    client: AsyncClient, db_session: AsyncSession, tenant, plaintext
) -> SimpleNamespace:
    cand_resp = await client.post("/api/v1/candidates", headers=_auth(plaintext))
    candidate_id = uuid.UUID(cand_resp.json()["id"])

    data = (FIXTURES_DIR / "valid_cv.pdf").read_bytes()
    upload_resp = await client.post(
        f"/api/v1/candidates/{candidate_id}/documents",
        headers=_auth(plaintext),
        files={"file": ("valid_cv.pdf", data, "application/pdf")},
    )
    document_id = uuid.UUID(upload_resp.json()["id"])
    document = await get_candidate_document(
        db_session, tenant_id=tenant.id, candidate_id=candidate_id, document_id=document_id
    )
    canonical = await get_latest_canonical_document(
        db_session, tenant_id=tenant.id, candidate_document_id=document_id
    )

    job = await create_job(db_session, tenant_id=tenant.id, title="Backend Engineer")
    await db_session.commit()

    return SimpleNamespace(
        tenant=tenant, candidate_id=candidate_id, document=document, canonical=canonical, job=job
    )


async def _profile_version(db_session: AsyncSession, setup: SimpleNamespace, profile_content: dict):
    version = await create_profile_version(
        db_session,
        tenant_id=setup.tenant.id,
        candidate_id=setup.candidate_id,
        candidate_document_id=setup.document.id,
        canonical_document_id=setup.canonical.id,
        source_sha256=setup.document.sha256_hash,
        schema_version="candidate-profile-v1",
        prompt_version="test-fixture",
        model_provider="test",
        model_name="test",
        model_metadata={},
        status="COMPLETED",
        profile_content=profile_content,
    )
    await db_session.commit()
    return version


async def _criteria_version(
    db_session: AsyncSession, setup: SimpleNamespace, criteria: list[CriterionIn]
):
    version = await create_criteria_version(
        db_session,
        tenant_id=setup.tenant.id,
        job_id=setup.job.id,
        criteria=[c.model_dump(mode="json") for c in criteria],
        created_by_api_key_id=None,
    )
    await db_session.commit()
    return version


def _skill_criterion(criterion_id: str, value: str) -> CriterionIn:
    return CriterionIn(
        id=criterion_id,
        kind=CriterionKind.SKILL,
        type=CriterionType.MUST_HAVE,
        label=value,
        value=value,
    )


_PYTHON_EVIDENCE = [{"page": 1, "block_index": 0, "quote": "Python"}]
_MATCHING_PROFILE = {
    "skills": [{"name": "Python", "category": None, "evidence": _PYTHON_EVIDENCE}],
    "employment_history": [],
    "education": [],
    "certifications": [],
    "languages": [],
    "projects": [],
}


@pytest.fixture
async def setup(client: AsyncClient, db_session: AsyncSession, tenant_and_key) -> SimpleNamespace:
    tenant, _key, plaintext = tenant_and_key
    return await _make_setup(client, db_session, tenant, plaintext)


async def test_evaluation_persists_exact_input_version_references(
    db_session: AsyncSession, setup: SimpleNamespace
) -> None:
    profile = await _profile_version(db_session, setup, _MATCHING_PROFILE)
    criteria = await _criteria_version(db_session, setup, [_skill_criterion("python", "Python")])

    evaluation = await evaluate_candidate(
        db_session,
        tenant_id=setup.tenant.id,
        candidate_id=setup.candidate_id,
        candidate_profile_version_id=profile.id,
        job_id=setup.job.id,
        job_criteria_version_id=criteria.id,
    )
    await db_session.commit()

    assert evaluation.candidate_profile_version_id == profile.id
    assert evaluation.job_criteria_version_id == criteria.id
    assert evaluation.candidate_id == setup.candidate_id
    assert evaluation.job_id == setup.job.id
    assert evaluation.status == "COMPLETED"
    assert evaluation.overall_result == "STRONG_MATCH"
    assert evaluation.policy_engine_version == "meyar-policy-v3"


async def test_evaluation_is_immutable_new_profile_version_requires_new_evaluation(
    db_session: AsyncSession, setup: SimpleNamespace
) -> None:
    profile_v1 = await _profile_version(db_session, setup, _MATCHING_PROFILE)
    criteria = await _criteria_version(db_session, setup, [_skill_criterion("python", "Python")])

    eval_1 = await evaluate_candidate(
        db_session,
        tenant_id=setup.tenant.id,
        candidate_id=setup.candidate_id,
        candidate_profile_version_id=profile_v1.id,
        job_id=setup.job.id,
        job_criteria_version_id=criteria.id,
    )
    await db_session.commit()

    empty_profile = {**_MATCHING_PROFILE, "skills": []}
    profile_v2 = await _profile_version(db_session, setup, empty_profile)
    eval_2 = await evaluate_candidate(
        db_session,
        tenant_id=setup.tenant.id,
        candidate_id=setup.candidate_id,
        candidate_profile_version_id=profile_v2.id,
        job_id=setup.job.id,
        job_criteria_version_id=criteria.id,
    )
    await db_session.commit()

    assert eval_1.id != eval_2.id
    assert eval_1.candidate_profile_version_id == profile_v1.id
    assert eval_2.candidate_profile_version_id == profile_v2.id
    assert eval_1.overall_result == "STRONG_MATCH"
    assert eval_2.overall_result == "INSUFFICIENT_EVIDENCE"

    # eval_1 is untouched by eval_2's creation — reload and compare.
    reloaded = await get_evaluation(db_session, tenant_id=setup.tenant.id, evaluation_id=eval_1.id)
    assert reloaded.overall_result == "STRONG_MATCH"
    assert reloaded.candidate_profile_version_id == profile_v1.id


async def test_new_criteria_version_requires_new_evaluation(
    db_session: AsyncSession, setup: SimpleNamespace
) -> None:
    profile = await _profile_version(db_session, setup, _MATCHING_PROFILE)
    criteria_v1 = await _criteria_version(db_session, setup, [_skill_criterion("python", "Python")])
    eval_1 = await evaluate_candidate(
        db_session,
        tenant_id=setup.tenant.id,
        candidate_id=setup.candidate_id,
        candidate_profile_version_id=profile.id,
        job_id=setup.job.id,
        job_criteria_version_id=criteria_v1.id,
    )
    await db_session.commit()

    criteria_v2 = await _criteria_version(
        db_session, setup, [_skill_criterion("python", "Python"), _skill_criterion("rust", "Rust")]
    )
    eval_2 = await evaluate_candidate(
        db_session,
        tenant_id=setup.tenant.id,
        candidate_id=setup.candidate_id,
        candidate_profile_version_id=profile.id,
        job_id=setup.job.id,
        job_criteria_version_id=criteria_v2.id,
    )
    await db_session.commit()

    assert eval_1.id != eval_2.id
    assert eval_1.job_criteria_version_id == criteria_v1.id
    assert eval_2.job_criteria_version_id == criteria_v2.id
    assert eval_1.overall_result == "STRONG_MATCH"
    assert eval_2.overall_result == "INSUFFICIENT_EVIDENCE"  # rust is UNKNOWN now


async def test_cross_tenant_candidate_and_job_mix_rejected(
    client: AsyncClient, db_session: AsyncSession, tenant_and_key
) -> None:
    tenant_a, _key_a, plaintext_a = tenant_and_key
    tenant_b = await create_tenant(db_session, name="Tenant B")
    await db_session.commit()

    setup_a = await _make_setup(client, db_session, tenant_a, plaintext_a)
    profile_a = await _profile_version(db_session, setup_a, _MATCHING_PROFILE)

    # tenant B's own job/criteria (a real, valid criteria version — but
    # tenant B's, not tenant A's)
    job_b = await create_job(db_session, tenant_id=tenant_b.id, title="X")
    setup_b = SimpleNamespace(tenant=tenant_b, job=job_b)
    await db_session.commit()
    criteria_b = await _criteria_version(
        db_session, setup_b, [_skill_criterion("python", "Python")]
    )

    # Attempt: tenant A's candidate profile + tenant B's job criteria,
    # but the call is (correctly) scoped as tenant A's request — tenant
    # B's criteria_version_id simply does not resolve under tenant A.
    with pytest.raises(EvaluationInputError) as exc_info:
        await evaluate_candidate(
            db_session,
            tenant_id=tenant_a.id,
            candidate_id=setup_a.candidate_id,
            candidate_profile_version_id=profile_a.id,
            job_id=setup_b.job.id,
            job_criteria_version_id=criteria_b.id,
        )
    assert exc_info.value.code == "CRITERIA_VERSION_NOT_FOUND"


async def test_tenant_b_cannot_retrieve_tenant_a_evaluation(
    client: AsyncClient, db_session: AsyncSession, tenant_and_key
) -> None:
    tenant_a, _key_a, plaintext_a = tenant_and_key
    tenant_b = await create_tenant(db_session, name="Tenant B")
    await db_session.commit()

    setup_a = await _make_setup(client, db_session, tenant_a, plaintext_a)
    profile_a = await _profile_version(db_session, setup_a, _MATCHING_PROFILE)
    criteria_a = await _criteria_version(
        db_session, setup_a, [_skill_criterion("python", "Python")]
    )

    evaluation = await evaluate_candidate(
        db_session,
        tenant_id=tenant_a.id,
        candidate_id=setup_a.candidate_id,
        candidate_profile_version_id=profile_a.id,
        job_id=setup_a.job.id,
        job_criteria_version_id=criteria_a.id,
    )
    await db_session.commit()

    leaked = await get_evaluation(db_session, tenant_id=tenant_b.id, evaluation_id=evaluation.id)
    assert leaked is None

    own = await get_evaluation(db_session, tenant_id=tenant_a.id, evaluation_id=evaluation.id)
    assert own is not None


async def test_wrong_tenant_profile_id_rejected_even_if_it_exists(
    client: AsyncClient, db_session: AsyncSession, tenant_and_key
) -> None:
    tenant_a, _key_a, plaintext_a = tenant_and_key
    tenant_b = await create_tenant(db_session, name="Tenant B")
    await db_session.commit()

    setup_a = await _make_setup(client, db_session, tenant_a, plaintext_a)
    profile_a = await _profile_version(db_session, setup_a, _MATCHING_PROFILE)
    criteria_a = await _criteria_version(
        db_session, setup_a, [_skill_criterion("python", "Python")]
    )

    with pytest.raises(EvaluationInputError) as exc_info:
        await evaluate_candidate(
            db_session,
            tenant_id=tenant_b.id,  # wrong tenant for this profile/criteria
            candidate_id=setup_a.candidate_id,
            candidate_profile_version_id=profile_a.id,
            job_id=setup_a.job.id,
            job_criteria_version_id=criteria_a.id,
        )
    assert exc_info.value.code == "PROFILE_NOT_FOUND"


async def test_profile_belonging_to_different_candidate_rejected(
    client: AsyncClient, db_session: AsyncSession, tenant_and_key
) -> None:
    tenant, _key, plaintext = tenant_and_key
    setup_1 = await _make_setup(client, db_session, tenant, plaintext)
    setup_2 = await _make_setup(client, db_session, tenant, plaintext)

    profile_1 = await _profile_version(db_session, setup_1, _MATCHING_PROFILE)
    criteria = await _criteria_version(db_session, setup_1, [_skill_criterion("python", "Python")])

    with pytest.raises(EvaluationInputError) as exc_info:
        await evaluate_candidate(
            db_session,
            tenant_id=tenant.id,
            candidate_id=setup_2.candidate_id,  # mismatched candidate
            candidate_profile_version_id=profile_1.id,
            job_id=setup_1.job.id,
            job_criteria_version_id=criteria.id,
        )
    assert exc_info.value.code == "PROFILE_NOT_FOUND"


async def test_no_hidden_criteria_result_count_matches_configured_criteria(
    db_session: AsyncSession, setup: SimpleNamespace
) -> None:
    profile = await _profile_version(db_session, setup, _MATCHING_PROFILE)
    criteria = await _criteria_version(
        db_session, setup, [_skill_criterion("python", "Python"), _skill_criterion("sql", "SQL")]
    )
    evaluation = await evaluate_candidate(
        db_session,
        tenant_id=setup.tenant.id,
        candidate_id=setup.candidate_id,
        candidate_profile_version_id=profile.id,
        job_id=setup.job.id,
        job_criteria_version_id=criteria.id,
    )
    await db_session.commit()

    assert len(evaluation.criterion_results) == 2
    assert {r["criterion_id"] for r in evaluation.criterion_results} == {"python", "sql"}


async def test_repeated_evaluation_with_identical_inputs_is_deterministic(
    db_session: AsyncSession, setup: SimpleNamespace
) -> None:
    profile = await _profile_version(db_session, setup, _MATCHING_PROFILE)
    criteria = await _criteria_version(db_session, setup, [_skill_criterion("python", "Python")])

    eval_1 = await evaluate_candidate(
        db_session,
        tenant_id=setup.tenant.id,
        candidate_id=setup.candidate_id,
        candidate_profile_version_id=profile.id,
        job_id=setup.job.id,
        job_criteria_version_id=criteria.id,
    )
    eval_2 = await evaluate_candidate(
        db_session,
        tenant_id=setup.tenant.id,
        candidate_id=setup.candidate_id,
        candidate_profile_version_id=profile.id,
        job_id=setup.job.id,
        job_criteria_version_id=criteria.id,
    )
    await db_session.commit()

    assert eval_1.overall_result == eval_2.overall_result
    assert eval_1.criterion_results == eval_2.criterion_results
    assert eval_1.id == eval_2.id


async def test_evaluation_does_not_use_identity_fields(
    db_session: AsyncSession, setup: SimpleNamespace
) -> None:
    """Regression: profile_content the policy engine reads must never
    contain identity/protected-attribute keys."""
    profile = await _profile_version(db_session, setup, _MATCHING_PROFILE)
    criteria = await _criteria_version(db_session, setup, [_skill_criterion("python", "Python")])
    evaluation = await evaluate_candidate(
        db_session,
        tenant_id=setup.tenant.id,
        candidate_id=setup.candidate_id,
        candidate_profile_version_id=profile.id,
        job_id=setup.job.id,
        job_criteria_version_id=criteria.id,
    )
    await db_session.commit()

    forbidden = {
        "name",
        "email",
        "phone",
        "gender",
        "religion",
        "ethnicity",
        "marital_status",
        "health",
    }
    result_text = str(evaluation.criterion_results)
    for field in forbidden:
        assert field not in result_text
