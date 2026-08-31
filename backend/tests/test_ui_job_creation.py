"""HR UI productization — new-vacancy creation
(GET/POST /ui/jobs/new, POST /ui/jobs).

Covers: auth (missing jobs:write scope, unauthenticated redirect), CSRF,
tenant scoping/cross-tenant isolation, successful creation with MUST_HAVE
+ PREFERRED criteria (including an EXPERIENCE criterion), validation
(empty title, blank rows skipped, EXPERIENCE without min_years, sensitive/
prohibited criterion term rejected), no raw id/UUID entry surface, and
that the created vacancy is immediately usable by the existing
deterministic ranking flow. Owner visual-inspection Blocker 2.
"""

import re

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.config import Settings, get_settings
from meyar.main import app
from meyar.models.job import Job
from meyar.models.job_criteria_version import JobCriteriaVersion
from meyar.services.api_key_repo import create_api_key
from meyar.services.tenant_repo import create_tenant


@pytest.fixture
def local_ui_settings() -> Settings:
    settings = Settings(ui_cookie_secure=False)
    app.dependency_overrides[get_settings] = lambda: settings
    return settings


async def _login_and_csrf(client: AsyncClient, plaintext: str) -> str:
    response = await client.post("/ui/login", data={"api_key": plaintext}, follow_redirects=False)
    assert response.status_code == 303
    home = await client.get("/ui")
    match = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', home.text)
    assert match is not None
    return match.group(1)


def _row(
    prefix: str,
    index: int,
    *,
    kind: str,
    label: str,
    value: str = "",
    min_years: str = "",
    weight: str = "",
) -> dict[str, str]:
    return {
        f"{prefix}_kind_{index}": kind,
        f"{prefix}_label_{index}": label,
        f"{prefix}_value_{index}": value,
        f"{prefix}_min_years_{index}": min_years,
        f"{prefix}_weight_{index}": weight,
    }


def _blank_rows(
    prefix: str, *, count: int = 6, filled: dict[str, str] | None = None
) -> dict[str, str]:
    data: dict[str, str] = {}
    for i in range(count):
        data.update(_row(prefix, i, kind="SKILL", label="", value=""))
    if filled:
        data.update(filled)
    return data


async def test_new_job_form_requires_jobs_write_scope(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_key,
    local_ui_settings: Settings,
) -> None:
    tenant, _key, _plaintext = tenant_and_key
    _read_only_key, read_only_plaintext = await create_api_key(
        db_session, tenant_id=tenant.id, env="test", scopes=["jobs:read"]
    )
    await db_session.commit()
    await _login_and_csrf(client, read_only_plaintext)

    response = await client.get("/ui/jobs/new")

    assert response.status_code == 403


async def test_new_job_form_unauthenticated_is_redirected(client: AsyncClient) -> None:
    response = await client.get("/ui/jobs/new", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


async def test_new_job_form_has_no_raw_id_input(
    client: AsyncClient, tenant_and_key, local_ui_settings: Settings
) -> None:
    """The HR user must never type or see a raw criterion/job id — it is
    generated server-side from the label."""
    _tenant, _key, plaintext = tenant_and_key
    await _login_and_csrf(client, plaintext)

    response = await client.get("/ui/jobs/new")

    assert response.status_code == 200
    assert 'name="must_id_0"' not in response.text
    assert 'name="pref_id_0"' not in response.text
    assert re.search(r'name="[a-z_]*\bid\b[a-z_]*"', response.text) is None


async def test_create_job_requires_csrf_token(
    client: AsyncClient, tenant_and_key, local_ui_settings: Settings
) -> None:
    _tenant, _key, plaintext = tenant_and_key
    await _login_and_csrf(client, plaintext)
    data = {"title": "No CSRF JD", "csrf_token": "wrong"}
    data.update(_blank_rows("must"))
    data.update(_blank_rows("pref"))

    response = await client.post("/ui/jobs", data=data)

    assert response.status_code == 403


async def test_create_job_requires_jobs_write_scope(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_key,
    local_ui_settings: Settings,
) -> None:
    tenant, _key, _plaintext = tenant_and_key
    _read_only_key, read_only_plaintext = await create_api_key(
        db_session, tenant_id=tenant.id, env="test", scopes=["jobs:read"]
    )
    await db_session.commit()
    csrf = await _login_and_csrf(client, read_only_plaintext)
    data = {"title": "Forbidden JD", "csrf_token": csrf}
    data.update(
        _blank_rows("must", filled=_row("must", 0, kind="SKILL", label="Python", value="Python"))
    )
    data.update(_blank_rows("pref"))

    response = await client.post("/ui/jobs", data=data)

    assert response.status_code == 403


async def test_empty_title_is_rejected_with_friendly_error(
    client: AsyncClient, tenant_and_key, local_ui_settings: Settings
) -> None:
    _tenant, _key, plaintext = tenant_and_key
    csrf = await _login_and_csrf(client, plaintext)
    data = {"title": "   ", "csrf_token": csrf}
    data.update(
        _blank_rows("must", filled=_row("must", 0, kind="SKILL", label="Python", value="Python"))
    )
    data.update(_blank_rows("pref"))

    response = await client.post("/ui/jobs", data=data)

    assert response.status_code == 422
    assert "Vakansiya başlığı boş ola bilməz." in response.text


async def test_experience_criterion_without_min_years_is_rejected_and_input_preserved(
    client: AsyncClient, tenant_and_key, local_ui_settings: Settings
) -> None:
    _tenant, _key, plaintext = tenant_and_key
    csrf = await _login_and_csrf(client, plaintext)
    data = {"title": "AML JD", "csrf_token": csrf}
    data.update(
        _blank_rows(
            "must",
            filled=_row("must", 0, kind="EXPERIENCE", label="Minimum təcrübə", value=""),
        )
    )
    data.update(_blank_rows("pref"))

    response = await client.post("/ui/jobs", data=data)

    assert response.status_code == 422
    assert "Minimum təcrübə" in response.text
    assert "illik təcrübəni daxil edin" in response.text
    assert 'value="AML JD"' in response.text  # title preserved, not lost


async def test_sensitive_criterion_term_is_rejected(
    client: AsyncClient, tenant_and_key, local_ui_settings: Settings
) -> None:
    _tenant, _key, plaintext = tenant_and_key
    csrf = await _login_and_csrf(client, plaintext)
    data = {"title": "Sensitive JD", "csrf_token": csrf}
    data.update(
        _blank_rows(
            "must", filled=_row("must", 0, kind="SKILL", label="Yaş", value="30 yaşdan aşağı")
        )
    )
    data.update(_blank_rows("pref"))

    response = await client.post("/ui/jobs", data=data)

    assert response.status_code == 422
    assert "prohibited/sensitive attribute" in response.text
    job = (await client.get("/ui/jobs")).text
    assert "Sensitive JD" not in job


async def test_blank_rows_are_silently_skipped(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_key,
    local_ui_settings: Settings,
) -> None:
    tenant, _key, plaintext = tenant_and_key
    csrf = await _login_and_csrf(client, plaintext)
    data = {"title": "Sparse JD", "csrf_token": csrf}
    data.update(
        _blank_rows("must", filled=_row("must", 0, kind="SKILL", label="Python", value="Python"))
    )
    data.update(_blank_rows("pref"))

    response = await client.post("/ui/jobs", data=data, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/jobs"
    job = (
        await db_session.execute(
            select(Job).where(Job.tenant_id == tenant.id, Job.title == "Sparse JD")
        )
    ).scalar_one()
    version = (
        await db_session.execute(
            select(JobCriteriaVersion).where(JobCriteriaVersion.job_id == job.id)
        )
    ).scalar_one()
    assert len(version.criteria) == 1
    assert version.criteria[0]["label"] == "Python"


async def test_create_job_with_must_have_and_preferred_criteria_is_immediately_rankable(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_key,
    local_ui_settings: Settings,
) -> None:
    tenant, _key, plaintext = tenant_and_key
    csrf = await _login_and_csrf(client, plaintext)
    data = {"title": "Senior AML Analyst", "csrf_token": csrf}
    data.update(
        _blank_rows(
            "must",
            filled={
                **_row("must", 0, kind="SKILL", label="AML biliyi", value="AML"),
                **_row("must", 1, kind="EXPERIENCE", label="Minimum təcrübə", min_years="3"),
            },
        )
    )
    data.update(
        _blank_rows(
            "pref",
            filled=_row("pref", 0, kind="CERTIFICATION", label="ACAMS sertifikatı", value="ACAMS"),
        )
    )

    create_response = await client.post("/ui/jobs", data=data, follow_redirects=False)
    assert create_response.status_code == 303
    assert create_response.headers["location"] == "/ui/jobs"

    jobs_page = await client.get("/ui/jobs")
    assert jobs_page.status_code == 200
    assert "Senior AML Analyst" in jobs_page.text
    assert "AML biliyi" in jobs_page.text
    assert "ACAMS sertifikatı" in jobs_page.text

    job = (
        await db_session.execute(
            select(Job).where(Job.tenant_id == tenant.id, Job.title == "Senior AML Analyst")
        )
    ).scalar_one()
    version = (
        await db_session.execute(
            select(JobCriteriaVersion).where(JobCriteriaVersion.job_id == job.id)
        )
    ).scalar_one()
    assert version.version_number == 1
    assert {c["label"] for c in version.criteria} == {
        "AML biliyi",
        "Minimum təcrübə",
        "ACAMS sertifikatı",
    }
    # no raw UUID/slug entry required from HR — ids are server-generated
    assert all(re.fullmatch(r"[a-z0-9_]{1,64}", c["id"]) for c in version.criteria)

    rank_response = await client.post(f"/ui/jobs/{version.id}/rank", data={"csrf_token": csrf})
    assert rank_response.status_code == 200
    assert "Reytinq nəticələri" in rank_response.text


async def test_created_job_is_not_visible_to_a_foreign_tenant(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_key,
    local_ui_settings: Settings,
) -> None:
    tenant, _key, plaintext = tenant_and_key
    csrf = await _login_and_csrf(client, plaintext)
    data = {"title": "Tenant-Scoped JD", "csrf_token": csrf}
    data.update(
        _blank_rows("must", filled=_row("must", 0, kind="SKILL", label="Python", value="Python"))
    )
    data.update(_blank_rows("pref"))
    create_response = await client.post("/ui/jobs", data=data, follow_redirects=False)
    assert create_response.status_code == 303

    foreign = await create_tenant(db_session, name="Foreign HR tenant")
    _foreign_key, foreign_plaintext = await create_api_key(
        db_session, tenant_id=foreign.id, env="test"
    )
    await db_session.commit()
    await client.post("/ui/logout", data={"csrf_token": csrf})
    await _login_and_csrf(client, foreign_plaintext)

    foreign_jobs_page = await client.get("/ui/jobs")

    assert foreign_jobs_page.status_code == 200
    assert "Tenant-Scoped JD" not in foreign_jobs_page.text
