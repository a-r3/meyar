import re
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from fakes import FakeLLMProvider
from httpx import AsyncClient
from search_helpers import seed_candidate_with_profile
from sqlalchemy.ext.asyncio import AsyncSession

import meyar.search.planner_service as planner_service
from meyar.config import Settings, get_settings
from meyar.llm.dependency import get_llm_provider
from meyar.llm.provider import ModelUnavailableError
from meyar.main import app
from meyar.models.candidate import Candidate
from meyar.schemas.criteria import CriterionIn, CriterionKind, CriterionType
from meyar.search.planner_schemas import PlannerDraft, PlannerOutcome, PlannerReasonCode
from meyar.search.schemas import RequiredFilters
from meyar.services.candidate_identity_repo import create_identity_version
from meyar.services.candidate_profile_repo import create_profile_version
from meyar.services.job_criteria_repo import create_criteria_version
from meyar.services.job_repo import create_job
from meyar.services.tenant_repo import create_tenant
from meyar.ui.presentation import PLANNER_OUTCOME_TEXT
from meyar.ui.service import UIServiceInputError, list_candidate_library

EVIDENCE = [{"page": 1, "block_index": 0, "quote": "Synthetic evidence"}]
EMPTY_PROFILE = {
    "skills": [],
    "employment_history": [],
    "education": [],
    "certifications": [],
    "languages": [],
    "projects": [],
}


@pytest.fixture
def local_ui_settings() -> Settings:
    settings = Settings(ui_cookie_secure=False)
    app.dependency_overrides[get_settings] = lambda: settings
    return settings


def _profile(*skills: str, quote: str = "Synthetic evidence") -> dict:
    return {
        **EMPTY_PROFILE,
        "skills": [
            {
                "name": skill,
                "category": "Backend",
                "evidence": [{"page": 1, "block_index": 0, "quote": quote}],
            }
            for skill in skills
        ],
    }


async def _login_and_csrf(client: AsyncClient, plaintext: str) -> str:
    response = await client.post(
        "/ui/login", data={"api_key": plaintext}, follow_redirects=False
    )
    assert response.status_code == 303
    home = await client.get("/ui")
    match = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', home.text)
    assert match is not None
    return match.group(1)


async def _identity(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate,
    profile,
    name: str,
    email: str = "synthetic@example.invalid",
) -> None:
    await create_identity_version(
        db,
        tenant_id=tenant_id,
        candidate_id=candidate.id,
        candidate_document_id=profile.candidate_document_id,
        canonical_document_id=profile.canonical_document_id,
        source_sha256=profile.source_sha256,
        schema_version="candidate-identity-v1",
        prompt_version="test",
        model_provider="fake",
        model_name="fake",
        status="COMPLETED",
        identity_content={
            "full_name": {"value": name, "evidence": EVIDENCE},
            "email": {"value": email, "evidence": EVIDENCE},
            "phone": None,
        },
    )


async def test_actual_ui_chat_delegates_and_preserves_backend_order_with_escaped_text(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_key,
    local_ui_settings: Settings,
) -> None:
    tenant, _key, plaintext = tenant_and_key
    payload = "<script>alert(1)</script>"
    first, first_profile = await seed_candidate_with_profile(
        db_session,
        tenant_id=tenant.id,
        profile_content=_profile("Python", quote=payload),
    )
    second, second_profile = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await _identity(
        db_session,
        tenant_id=tenant.id,
        candidate=first,
        profile=first_profile,
        name=payload,
    )
    await _identity(
        db_session,
        tenant_id=tenant.id,
        candidate=second,
        profile=second_profile,
        name="Synthetic B",
    )
    foreign_tenant = await create_tenant(db_session, name="Foreign synthetic")
    foreign_candidate, _foreign_profile = await seed_candidate_with_profile(
        db_session, tenant_id=foreign_tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()

    fake = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"]))
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, plaintext)
    query = f"Python bilən namizədləri göstər. {payload}"
    response = await client.post(
        "/ui/search",
        data={"query": query, "as_of_date": "2026-01-01", "csrf_token": csrf},
    )
    assert response.status_code == 200
    assert fake.call_count == 1
    expected = sorted([first.id, second.id], key=str)
    assert response.text.index(str(expected[0])) < response.text.index(str(expected[1]))
    assert str(foreign_candidate.id) not in response.text
    assert payload not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert "Sorğu icra edildi" in response.text


def test_every_planner_outcome_has_distinct_azerbaijani_presentation() -> None:
    assert set(PLANNER_OUTCOME_TEXT) == set(PlannerOutcome)
    assert len({title for title, _message in PLANNER_OUTCOME_TEXT.values()}) == len(
        PlannerOutcome
    )


async def test_executable_zero_result_is_not_presented_as_infrastructure_error(
    client: AsyncClient,
    tenant_and_key,
    local_ui_settings: Settings,
) -> None:
    _tenant, _key, plaintext = tenant_and_key
    fake = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"]))
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, plaintext)
    response = await client.post(
        "/ui/search",
        data={
            "query": "Python bilən namizədləri göstər.",
            "as_of_date": "2026-01-01",
            "csrf_token": csrf,
        },
    )
    assert response.status_code == 200
    assert "Nəticə tapılmadı" in response.text
    assert "Axtarış xidməti əlçatan deyil" not in response.text


@pytest.mark.parametrize(
    ("query", "draft", "expected", "expected_calls"),
    [
        (
            "30 yaşdan aşağı namizədləri göstər.",
            PlannerDraft(),
            "Sorğu qəbul edilmədi",
            0,
        ),
        (
            "5 il Java təcrübəsi olan namizədləri göstər.",
            PlannerDraft(
                required_filters=RequiredFilters(
                    skills=["Java"], min_total_experience_years=5
                ),
                unsupported_reason_codes=[
                    PlannerReasonCode.SKILL_SPECIFIC_EXPERIENCE_DURATION_UNSUPPORTED
                ],
            ),
            "Sorğunun mənası dəstəklənmir",
            0,
        ),
        (
            "Uyğun namizəd tap.",
            PlannerDraft(),
            "Sorğu qeyri-müəyyəndir",
            1,
        ),
    ],
)
async def test_non_executable_ui_plans_never_search(
    client: AsyncClient,
    tenant_and_key,
    local_ui_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
    draft: PlannerDraft,
    expected: str,
    expected_calls: int,
) -> None:
    _tenant, _key, plaintext = tenant_and_key
    search_calls = 0

    async def forbidden_search(*args, **kwargs):
        nonlocal search_calls
        search_calls += 1
        raise AssertionError("non-executable plan must not search")

    monkeypatch.setattr(planner_service, "search_candidates", forbidden_search)
    fake = FakeLLMProvider(planner_draft=draft)
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, plaintext)
    response = await client.post(
        "/ui/search",
        data={"query": query, "as_of_date": "2026-01-01", "csrf_token": csrf},
    )
    assert response.status_code == 200
    assert expected in response.text
    assert fake.call_count == expected_calls
    assert search_calls == 0


async def test_malformed_planner_output_has_no_fallback_search(
    client: AsyncClient,
    tenant_and_key,
    local_ui_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _tenant, _key, plaintext = tenant_and_key
    search_calls = 0

    async def forbidden_search(*args, **kwargs):
        nonlocal search_calls
        search_calls += 1
        raise AssertionError("malformed plan must not search")

    monkeypatch.setattr(planner_service, "search_candidates", forbidden_search)
    fake = FakeLLMProvider(fail_first_n_calls=2)
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, plaintext)
    response = await client.post(
        "/ui/search",
        data={
            "query": "Python bilən namizədləri göstər.",
            "as_of_date": "2026-01-01",
            "csrf_token": csrf,
        },
    )
    assert response.status_code == 200
    assert "Plan yaradıla bilmədi" in response.text
    assert fake.call_count == 2
    assert search_calls == 0


async def test_local_planner_outage_is_safe_and_library_remains_independent(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_key,
    local_ui_settings: Settings,
) -> None:
    tenant, _key, plaintext = tenant_and_key
    candidate, _profile_row = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()
    fake = FakeLLMProvider(error=ModelUnavailableError("private provider detail"))
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, plaintext)
    response = await client.post(
        "/ui/search",
        data={
            "query": "Python bilən namizədləri göstər.",
            "as_of_date": "2026-01-01",
            "csrf_token": csrf,
        },
    )
    assert response.status_code == 200
    assert "Yerli AI xidməti hazırda əlçatan deyil" in response.text
    assert "private provider detail" not in response.text
    assert (await client.get("/ui/library")).status_code == 200
    assert (await client.get(f"/ui/candidates/{candidate.id}")).status_code == 200


async def test_library_pagination_order_filters_and_current_profile_authority(
    db_session: AsyncSession, tenant_and_key
) -> None:
    tenant, _key, _plaintext = tenant_and_key
    base = datetime(2026, 1, 1, tzinfo=UTC)
    seeded: list[Candidate] = []
    for index in range(55):
        candidate = Candidate(
            id=uuid.UUID(int=index + 1),
            tenant_id=tenant.id,
            created_at=base + timedelta(minutes=index),
            updated_at=base,
        )
        db_session.add(candidate)
        seeded.append(candidate)
    foreign_tenant = await create_tenant(db_session, name="Foreign library tenant")
    db_session.add(
        Candidate(
            id=uuid.UUID(int=1000),
            tenant_id=foreign_tenant.id,
            created_at=base + timedelta(days=1),
            updated_at=base,
        )
    )
    await db_session.flush()
    page = await list_candidate_library(db_session, tenant_id=tenant.id)
    assert page.total == 55
    assert page.page_size == 25
    assert len(page.items) == 25
    assert [item.candidate_id for item in page.items] == [
        item.id for item in reversed(seeded[-25:])
    ]
    maximum = await list_candidate_library(db_session, tenant_id=tenant.id, page_size=999)
    assert maximum.page_size == 50
    assert len(maximum.items) == 50
    empty = await list_candidate_library(
        db_session, tenant_id=tenant.id, profile_status="FAILED"
    )
    assert empty.total == 0 and empty.items == []
    with pytest.raises(UIServiceInputError):
        await list_candidate_library(
            db_session, tenant_id=tenant.id, profile_status="ARCHIVED"
        )

    candidate, profile_v1 = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Stale skill")
    )
    profile_v2 = await create_profile_version(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        candidate_document_id=profile_v1.candidate_document_id,
        canonical_document_id=profile_v1.canonical_document_id,
        source_sha256="b" * 64,
        schema_version="candidate-profile-v1",
        prompt_version="test",
        model_provider="fake",
        model_name="fake",
        model_metadata={},
        status="FAILED",
        error_code="SYNTHETIC_FAILURE",
        profile_content=None,
    )
    failed = await list_candidate_library(
        db_session, tenant_id=tenant.id, profile_status="FAILED", page_size=50
    )
    item = next(item for item in failed.items if item.candidate_id == candidate.id)
    assert item.current_profile_version == profile_v2.version_number
    assert item.current_profile_status == "FAILED"


async def test_cross_tenant_candidate_direct_access_is_safe_404(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_key,
    local_ui_settings: Settings,
) -> None:
    _tenant, _key, plaintext = tenant_and_key
    foreign = await create_tenant(db_session, name="Foreign")
    candidate, _profile_row = await seed_candidate_with_profile(
        db_session, tenant_id=foreign.id, profile_content=_profile("Python")
    )
    await db_session.commit()
    await _login_and_csrf(client, plaintext)
    response = await client.get(f"/ui/candidates/{candidate.id}")
    assert response.status_code == 404
    assert "Namizəd tapılmadı" in response.text
    assert "Foreign" not in response.text


async def test_candidate_detail_escapes_identity_profile_and_evidence(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_key,
    local_ui_settings: Settings,
) -> None:
    tenant, _key, plaintext = tenant_and_key
    payload = "<script>alert(1)</script>"
    candidate, profile = await seed_candidate_with_profile(
        db_session,
        tenant_id=tenant.id,
        profile_content=_profile(payload, quote=payload),
    )
    await _identity(
        db_session,
        tenant_id=tenant.id,
        candidate=candidate,
        profile=profile,
        name=payload,
        email=payload,
    )
    await db_session.commit()
    await _login_and_csrf(client, plaintext)
    response = await client.get(f"/ui/candidates/{candidate.id}")
    assert response.status_code == 200
    assert payload not in response.text
    assert response.text.count("&lt;script&gt;alert(1)&lt;/script&gt;") >= 3
    assert "storage_key" not in response.text


async def test_job_title_xss_is_escaped(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_key,
    local_ui_settings: Settings,
) -> None:
    tenant, _key, plaintext = tenant_and_key
    payload = "<script>alert(1)</script>"
    job = await create_job(db_session, tenant_id=tenant.id, title=payload)
    await create_criteria_version(
        db_session,
        tenant_id=tenant.id,
        job_id=job.id,
        criteria=[
            CriterionIn(
                id="python",
                kind=CriterionKind.SKILL,
                type=CriterionType.MUST_HAVE,
                label="Python",
                value="Python",
            ).model_dump(mode="json")
        ],
        created_by_api_key_id=None,
    )
    await db_session.commit()
    await _login_and_csrf(client, plaintext)
    response = await client.get("/ui/jobs")
    assert payload not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text


async def test_ranking_ui_preserves_slice10_order_not_identity_order(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_key,
    local_ui_settings: Settings,
) -> None:
    tenant, _key, plaintext = tenant_and_key
    candidates = []
    for name, skills in [
        ("Zulu B", ("Python", "AWS")),
        ("Alpha A", ("Python",)),
        ("Aardvark C", ()),
    ]:
        candidate, profile = await seed_candidate_with_profile(
            db_session, tenant_id=tenant.id, profile_content=_profile(*skills)
        )
        await _identity(
            db_session,
            tenant_id=tenant.id,
            candidate=candidate,
            profile=profile,
            name=name,
        )
        candidates.append(candidate)
    job = await create_job(db_session, tenant_id=tenant.id, title="Synthetic JD")
    criteria = await create_criteria_version(
        db_session,
        tenant_id=tenant.id,
        job_id=job.id,
        criteria=[
            CriterionIn(
                id="python",
                kind=CriterionKind.SKILL,
                type=CriterionType.MUST_HAVE,
                label="Python",
                value="Python",
                weight=1,
            ).model_dump(mode="json"),
            CriterionIn(
                id="aws",
                kind=CriterionKind.SKILL,
                type=CriterionType.PREFERRED,
                label="AWS",
                value="AWS",
                weight=1,
            ).model_dump(mode="json"),
        ],
        created_by_api_key_id=None,
    )
    await db_session.commit()
    unavailable = FakeLLMProvider(error=ModelUnavailableError("must not be called"))
    app.dependency_overrides[get_llm_provider] = lambda: unavailable
    csrf = await _login_and_csrf(client, plaintext)
    response = await client.post(
        f"/ui/jobs/{criteria.id}/rank",
        data={"evaluation_as_of_date": "2026-01-01", "csrf_token": csrf},
    )
    assert response.status_code == 200
    assert response.text.index("Zulu B") < response.text.index("Alpha A")
    assert response.text.index("Alpha A") < response.text.index("Aardvark C")
    assert "100.00 / 100" in response.text
    assert "50.00 / 100" in response.text
    assert "Kifayət qədər sübut yoxdur" in response.text
    assert "qəti uyğunsuzluğu demək deyil" in response.text
    assert "Məlumat məlum deyil" in response.text
    assert "HIRE" not in response.text and "REJECT" not in response.text
    assert unavailable.call_count == 0


async def test_manual_review_fit_is_presented_as_human_review_not_decision(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_key,
    local_ui_settings: Settings,
) -> None:
    tenant, _key, plaintext = tenant_and_key
    await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    job = await create_job(db_session, tenant_id=tenant.id, title="Manual Review JD")
    criteria = await create_criteria_version(
        db_session,
        tenant_id=tenant.id,
        job_id=job.id,
        criteria=[
            CriterionIn(
                id="python_review",
                kind=CriterionKind.SKILL,
                type=CriterionType.MUST_HAVE,
                label="Python",
                value="Python",
                manual_review_required=True,
            ).model_dump(mode="json")
        ],
        created_by_api_key_id=None,
    )
    await db_session.commit()
    csrf = await _login_and_csrf(client, plaintext)
    response = await client.post(
        f"/ui/jobs/{criteria.id}/rank",
        data={"evaluation_as_of_date": "2026-01-01", "csrf_token": csrf},
    )
    assert response.status_code == 200
    assert "İnsan baxışı tələb olunur" in response.text
    assert "HIRE" not in response.text and "REJECT" not in response.text


async def test_cross_tenant_criteria_direct_post_is_safe_404(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_key,
    local_ui_settings: Settings,
) -> None:
    _tenant, _key, plaintext = tenant_and_key
    foreign = await create_tenant(db_session, name="Foreign")
    job = await create_job(db_session, tenant_id=foreign.id, title="Foreign JD")
    criteria = await create_criteria_version(
        db_session,
        tenant_id=foreign.id,
        job_id=job.id,
        criteria=[
            CriterionIn(
                id="python",
                kind=CriterionKind.SKILL,
                type=CriterionType.MUST_HAVE,
                label="Python",
                value="Python",
            ).model_dump(mode="json")
        ],
        created_by_api_key_id=None,
    )
    await db_session.commit()
    csrf = await _login_and_csrf(client, plaintext)
    response = await client.post(
        f"/ui/jobs/{criteria.id}/rank",
        data={"evaluation_as_of_date": date(2026, 1, 1).isoformat(), "csrf_token": csrf},
    )
    assert response.status_code == 404
    assert "Foreign JD" not in response.text
