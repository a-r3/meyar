"""Job/vacancy lifecycle (D-028): ACTIVE/ARCHIVED status (no hard delete),
default-active /ui/jobs listing with an explicit archive view, a
CSRF-protected/tenant-scoped/auth-required "Arxivlə" action, and
canonical-signature duplicate-creation protection scoped to the /ui/jobs
form path (POST /api/v1/jobs is unaffected).

Covers: default-active listing, archived jobs excluded from the default
list but visible in the archive view, archive route auth/CSRF/tenant
isolation, no hard delete, archived-job evaluation-history title
resolution, identical-active-duplicate rejection, same-title-different-
criteria allowed, archived duplicates not blocking a new active job, a
real concurrent-double-submit DB-constraint test (not just the
application-level pre-check), and an API-path regression check.
"""

import re
import uuid

import pytest
from httpx import AsyncClient
from search_helpers import seed_candidate_with_profile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.config import Settings, get_settings
from meyar.main import app
from meyar.models.job import Job
from meyar.models.job_criteria_version import JobCriteriaVersion
from meyar.schemas.criteria import CriterionIn, CriterionKind, CriterionType
from meyar.services.candidate_identity_repo import create_identity_version
from meyar.services.job_criteria_repo import create_criteria_version
from meyar.services.job_repo import archive_job, create_job
from meyar.services.tenant_membership_repo import create_membership
from meyar.services.tenant_repo import create_tenant
from meyar.services.user_repo import create_user
from meyar.ui.service import compute_job_duplicate_signature


@pytest.fixture
def local_ui_settings() -> Settings:
    settings = Settings(ui_cookie_secure=False)
    app.dependency_overrides[get_settings] = lambda: settings
    return settings


async def _login_and_csrf(client: AsyncClient, username: str, password: str) -> str:
    response = await client.post(
        "/ui/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303
    home = await client.get("/ui")
    match = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', home.text)
    assert match is not None
    return match.group(1)


async def _create_restricted_user(db: AsyncSession, *, tenant_id) -> tuple[str, str]:
    """A user with an active membership carrying an unrecognized role —
    meyar.core.roles.permissions_for_role fails closed on any role it
    doesn't recognize, so this deterministically has zero UI permissions.
    Exercises the same require_ui_scopes enforcement path a genuinely
    reduced-permission role would, without inventing a fake product role."""
    password = "restricted-password-1"
    username = f"restricted-{uuid.uuid4().hex[:8]}"
    user = await create_user(db, username=username, plaintext_password=password)
    await create_membership(db, user_id=user.id, tenant_id=tenant_id, role="NO_PERMISSIONS")
    await db.commit()
    return username, password


def _row(
    prefix: str,
    index: int,
    *,
    kind: str,
    requirement: str,
    min_years: str = "",
    weight: str = "",
) -> dict[str, str]:
    return {
        f"{prefix}_kind_{index}": kind,
        f"{prefix}_requirement_{index}": requirement,
        f"{prefix}_min_years_{index}": min_years,
        f"{prefix}_weight_{index}": weight,
    }


def _blank_rows(
    prefix: str, *, count: int = 4, filled: dict[str, str] | None = None
) -> dict[str, str]:
    data: dict[str, str] = {}
    for i in range(count):
        data.update(_row(prefix, i, kind="SKILL", requirement=""))
    if filled:
        data.update(filled)
    return data


def _create_job_form_data(csrf: str, title: str, skill: str) -> dict[str, str]:
    data = {"title": title, "csrf_token": csrf}
    data.update(_blank_rows("must", filled=_row("must", 0, kind="SKILL", requirement=skill)))
    data.update(_blank_rows("pref"))
    return data


async def _job_by_title(db_session: AsyncSession, *, tenant_id: uuid.UUID, title: str) -> Job:
    return (
        await db_session.execute(
            select(Job).where(Job.tenant_id == tenant_id, Job.title == title)
        )
    ).scalar_one()


# ---------------------------------------------------------------------------
# Active/archive listing
# ---------------------------------------------------------------------------


async def test_active_jobs_shown_by_default(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    tenant, user, password, _membership = tenant_and_user
    job = await create_job(db_session, tenant_id=tenant.id, title="Default Active JD")
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
    await _login_and_csrf(client, user.username, password)

    response = await client.get("/ui/jobs")

    assert response.status_code == 200
    assert "Default Active JD" in response.text
    assert "Aktiv" in response.text


async def test_archived_jobs_absent_from_default_list_but_visible_in_archive(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    tenant, user, password, _membership = tenant_and_user
    job = await create_job(db_session, tenant_id=tenant.id, title="Archive View JD")
    await db_session.commit()
    await archive_job(db_session, tenant_id=tenant.id, job_id=job.id)
    await db_session.commit()
    await _login_and_csrf(client, user.username, password)

    active_page = await client.get("/ui/jobs")
    archived_page = await client.get("/ui/jobs?status=archived")

    assert active_page.status_code == 200
    assert "Archive View JD" not in active_page.text
    assert archived_page.status_code == 200
    assert "Archive View JD" in archived_page.text
    assert "Arxivləşdirilib" in archived_page.text
    # archived jobs must not present a rank action, as if still open
    assert "Namizədləri sırala" not in archived_page.text


# ---------------------------------------------------------------------------
# Archive action: auth, CSRF, tenant isolation, no hard delete
# ---------------------------------------------------------------------------


async def test_archive_unauthenticated_is_redirected(client: AsyncClient) -> None:
    response = await client.post(
        f"/ui/jobs/{uuid.uuid4()}/archive",
        data={"csrf_token": "irrelevant"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


async def test_archive_requires_csrf_token(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    tenant, user, password, _membership = tenant_and_user
    job = await create_job(db_session, tenant_id=tenant.id, title="CSRF Archive JD")
    await db_session.commit()
    await _login_and_csrf(client, user.username, password)

    response = await client.post(
        f"/ui/jobs/{job.id}/archive", data={"csrf_token": "wrong"}
    )

    assert response.status_code == 403
    await db_session.refresh(job)
    assert job.status == "ACTIVE"


async def test_archive_requires_jobs_write_scope(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    tenant, _user, _password, _membership = tenant_and_user
    job = await create_job(db_session, tenant_id=tenant.id, title="Scope Archive JD")
    await db_session.commit()
    restricted_username, restricted_password = await _create_restricted_user(
        db_session, tenant_id=tenant.id
    )
    csrf = await _login_and_csrf(client, restricted_username, restricted_password)

    response = await client.post(
        f"/ui/jobs/{job.id}/archive", data={"csrf_token": csrf}
    )

    assert response.status_code == 403
    await db_session.refresh(job)
    assert job.status == "ACTIVE"


async def test_archive_is_tenant_scoped_and_cross_tenant_denied(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    tenant, user, password, _membership = tenant_and_user
    foreign = await create_tenant(db_session, name="Foreign archive tenant")
    foreign_job = await create_job(db_session, tenant_id=foreign.id, title="Foreign JD")
    await db_session.commit()
    csrf = await _login_and_csrf(client, user.username, password)

    response = await client.post(
        f"/ui/jobs/{foreign_job.id}/archive", data={"csrf_token": csrf}
    )

    assert response.status_code == 404
    assert "Foreign JD" not in response.text
    await db_session.refresh(foreign_job)
    assert foreign_job.status == "ACTIVE"


async def test_archive_succeeds_and_never_hard_deletes(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    tenant, user, password, _membership = tenant_and_user
    job = await create_job(db_session, tenant_id=tenant.id, title="Soft Archive JD")
    version = await create_criteria_version(
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
    csrf = await _login_and_csrf(client, user.username, password)

    response = await client.post(
        f"/ui/jobs/{job.id}/archive", data={"csrf_token": csrf}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/jobs"
    reloaded_job = (
        await db_session.execute(select(Job).where(Job.id == job.id))
    ).scalar_one()
    assert reloaded_job.status == "ARCHIVED"
    assert reloaded_job.archived_at is not None
    reloaded_version = (
        await db_session.execute(
            select(JobCriteriaVersion).where(JobCriteriaVersion.id == version.id)
        )
    ).scalar_one()
    assert reloaded_version.criteria[0]["label"] == "Python"


async def test_archived_job_evaluation_history_still_resolves_title(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    tenant, user, password, _membership = tenant_and_user
    candidate, profile = await seed_candidate_with_profile(
        db_session,
        tenant_id=tenant.id,
        profile_content={
            "skills": [
                {
                    "name": "Python",
                    "category": None,
                    "evidence": [{"page": 1, "block_index": 0, "quote": "Python"}],
                }
            ],
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
            "full_name": {"value": "History Candidate", "evidence": []},
            "email": None,
            "phone": None,
        },
    )
    job = await create_job(db_session, tenant_id=tenant.id, title="History JD")
    version = await create_criteria_version(
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
    csrf = await _login_and_csrf(client, user.username, password)

    rank_response = await client.post(
        f"/ui/jobs/{version.id}/rank", data={"csrf_token": csrf}
    )
    assert rank_response.status_code == 200

    archive_response = await client.post(
        f"/ui/jobs/{job.id}/archive", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert archive_response.status_code == 303

    detail_response = await client.get(f"/ui/candidates/{candidate.id}")
    assert detail_response.status_code == 200
    assert "History JD" in detail_response.text


# ---------------------------------------------------------------------------
# Ranking lifecycle enforcement (D-033): an ARCHIVED job is not a current,
# evaluable vacancy — enforced in the shared meyar.scoring.batch service so
# no caller (UI, REST API, CLI, or a future agent tool) can bypass it by
# avoiding the UI's hidden rank button.
# ---------------------------------------------------------------------------


async def _job_with_python_criteria(
    db_session: AsyncSession, *, tenant_id: uuid.UUID, title: str
) -> tuple[Job, JobCriteriaVersion]:
    job = await create_job(db_session, tenant_id=tenant_id, title=title)
    version = await create_criteria_version(
        db_session,
        tenant_id=tenant_id,
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
    return job, version


async def test_active_job_can_still_be_ranked(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    tenant, user, password, _membership = tenant_and_user
    job, version = await _job_with_python_criteria(
        db_session, tenant_id=tenant.id, title="Active Rank JD"
    )
    await db_session.commit()
    csrf = await _login_and_csrf(client, user.username, password)

    response = await client.post(f"/ui/jobs/{version.id}/rank", data={"csrf_token": csrf})

    assert response.status_code == 200
    reloaded_job = (await db_session.execute(select(Job).where(Job.id == job.id))).scalar_one()
    assert reloaded_job.status == "ACTIVE"


async def test_archived_job_direct_stale_rank_post_is_rejected_with_hr_safe_message(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    """Simulates a stale/bookmarked rank URL for a job archived after the
    tab was opened: the button is gone from the UI, but the POST itself
    must still be rejected — not just hidden."""
    tenant, user, password, _membership = tenant_and_user
    candidate, _profile = await seed_candidate_with_profile(
        db_session,
        tenant_id=tenant.id,
        profile_content={
            "skills": [
                {
                    "name": "Python",
                    "category": None,
                    "evidence": [{"page": 1, "block_index": 0, "quote": "Python"}],
                }
            ],
            "employment_history": [],
            "education": [],
            "certifications": [],
            "languages": [],
            "projects": [],
        },
    )
    job, version = await _job_with_python_criteria(
        db_session, tenant_id=tenant.id, title="Archived Rank JD"
    )
    await db_session.commit()
    csrf = await _login_and_csrf(client, user.username, password)

    # Capture plain ids up front: the app's rejected-request rollback below
    # expires every ORM object tracked by this shared session (regardless
    # of expire_on_commit), so later synchronous attribute access on
    # `candidate`/`job`/`version` would itself trigger an async reload
    # outside of an awaited context. Using plain uuid.UUID values instead
    # sidesteps that entirely.
    candidate_id = candidate.id
    job_id = job.id
    version_id = version.id

    # A ranking run while still ACTIVE must remain in history after archiving.
    first_rank = await client.post(f"/ui/jobs/{version_id}/rank", data={"csrf_token": csrf})
    assert first_rank.status_code == 200

    archive_response = await client.post(
        f"/ui/jobs/{job_id}/archive", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert archive_response.status_code == 303

    # Stale/direct POST to the same rank URL after archiving.
    stale_rank = await client.post(f"/ui/jobs/{version_id}/rank", data={"csrf_token": csrf})

    assert stale_rank.status_code == 409
    assert "JOB_ARCHIVED" not in stale_rank.text
    assert "arxivləşdirilib" in stale_rank.text.lower()

    # No new Evaluation was persisted by the rejected attempt, and the
    # historical (pre-archive) evaluation remains readable: the candidate's
    # evaluation-history table must show exactly the one evaluation row
    # created by the earlier, successful ACTIVE-job ranking — never two.
    detail_response = await client.get(f"/ui/candidates/{candidate_id}")
    assert detail_response.status_code == 200
    assert detail_response.text.count("Archived Rank JD") == 1


async def test_archived_job_rank_rejection_does_not_leak_cross_tenant(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    """A foreign tenant's job — archived or not — must resolve the same
    generic not-found outcome; the new lifecycle check must never expose
    that a foreign-tenant criteria version exists or is archived."""
    tenant, user, password, _membership = tenant_and_user
    foreign = await create_tenant(db_session, name="Foreign rank tenant")
    foreign_job, foreign_version = await _job_with_python_criteria(
        db_session, tenant_id=foreign.id, title="Foreign Archived JD"
    )
    foreign_job.status = "ARCHIVED"
    await db_session.commit()
    csrf = await _login_and_csrf(client, user.username, password)

    response = await client.post(f"/ui/jobs/{foreign_version.id}/rank", data={"csrf_token": csrf})

    assert response.status_code == 404
    assert "Foreign Archived JD" not in response.text
    assert "arxivləşdirilib" not in response.text.lower()


# ---------------------------------------------------------------------------
# Duplicate-creation safety
# ---------------------------------------------------------------------------


async def test_identical_active_duplicate_is_rejected(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    tenant, user, password, _membership = tenant_and_user
    csrf = await _login_and_csrf(client, user.username, password)
    first = await client.post(
        "/ui/jobs",
        data=_create_job_form_data(csrf, "Duplicate Guard JD", "Python"),
        follow_redirects=False,
    )
    assert first.status_code == 303

    second = await client.post(
        "/ui/jobs", data=_create_job_form_data(csrf, "Duplicate Guard JD", "Python")
    )

    assert second.status_code == 409
    assert "Eyni tələblərlə aktiv vakansiya artıq mövcuddur." in second.text
    jobs = (
        await db_session.execute(
            select(Job).where(
                Job.tenant_id == tenant.id, Job.title == "Duplicate Guard JD"
            )
        )
    ).scalars().all()
    assert len(jobs) == 1


async def test_same_title_different_criteria_is_allowed(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    tenant, user, password, _membership = tenant_and_user
    csrf = await _login_and_csrf(client, user.username, password)
    first = await client.post(
        "/ui/jobs",
        data=_create_job_form_data(csrf, "Shared Title JD", "Python"),
        follow_redirects=False,
    )
    assert first.status_code == 303

    second = await client.post(
        "/ui/jobs",
        data=_create_job_form_data(csrf, "Shared Title JD", "Java"),
        follow_redirects=False,
    )

    assert second.status_code == 303
    jobs = (
        await db_session.execute(
            select(Job).where(Job.tenant_id == tenant.id, Job.title == "Shared Title JD")
        )
    ).scalars().all()
    assert len(jobs) == 2


async def test_archived_duplicate_does_not_block_new_active_job(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    tenant, user, password, _membership = tenant_and_user
    csrf = await _login_and_csrf(client, user.username, password)
    first = await client.post(
        "/ui/jobs",
        data=_create_job_form_data(csrf, "Reopen JD", "Python"),
        follow_redirects=False,
    )
    assert first.status_code == 303
    original = await _job_by_title(db_session, tenant_id=tenant.id, title="Reopen JD")
    await client.post(f"/ui/jobs/{original.id}/archive", data={"csrf_token": csrf})

    second = await client.post(
        "/ui/jobs",
        data=_create_job_form_data(csrf, "Reopen JD", "Python"),
        follow_redirects=False,
    )

    assert second.status_code == 303
    jobs = (
        await db_session.execute(
            select(Job).where(Job.tenant_id == tenant.id, Job.title == "Reopen JD")
        )
    ).scalars().all()
    assert len(jobs) == 2
    statuses = sorted(job.status for job in jobs)
    assert statuses == ["ACTIVE", "ARCHIVED"]


async def test_concurrent_double_submit_is_rejected_by_db_constraint_not_only_precheck(
    db_session: AsyncSession, tenant_and_user
) -> None:
    """The real concurrency guard: two requests that both pass the
    application-level pre-check (because neither has committed yet) must
    still not both succeed — the partial unique index on
    (tenant_id, duplicate_signature) WHERE status='ACTIVE' is what
    actually prevents the race, not the pre-check alone."""
    tenant, user, _password, _membership = tenant_and_user
    criteria = [
        CriterionIn(
            id="python",
            kind=CriterionKind.SKILL,
            type=CriterionType.MUST_HAVE,
            label="Python",
            value="Python",
        )
    ]
    signature = compute_job_duplicate_signature("Race JD", criteria)

    first_job = await create_job(
        db_session, tenant_id=tenant.id, title="Race JD", duplicate_signature=signature
    )
    await create_criteria_version(
        db_session,
        tenant_id=tenant.id,
        job_id=first_job.id,
        criteria=[c.model_dump(mode="json") for c in criteria],
        created_by_api_key_id=None,
    )
    await db_session.commit()

    with pytest.raises(IntegrityError):
        second_job = await create_job(
            db_session, tenant_id=tenant.id, title="Race JD", duplicate_signature=signature
        )
        await create_criteria_version(
            db_session,
            tenant_id=tenant.id,
            job_id=second_job.id,
            criteria=[c.model_dump(mode="json") for c in criteria],
            created_by_api_key_id=None,
        )
        await db_session.commit()
    await db_session.rollback()


# ---------------------------------------------------------------------------
# API compatibility regression
# ---------------------------------------------------------------------------


async def test_api_job_creation_is_unaffected_by_ui_duplicate_guard(
    client: AsyncClient, db_session: AsyncSession, tenant_and_key
) -> None:
    """POST /api/v1/jobs has no duplicate-signature check — API/CLI
    behavior is unchanged by this UI-only lifecycle/duplicate-safety
    pass. Two API-created jobs with identical title+criteria must both
    succeed exactly as before. Machine access still authenticates with an
    API key, entirely independent of the human /ui/login path."""
    _tenant, _api_key, plaintext = tenant_and_key
    body = {
        "title": "API Regression JD",
        "criteria": [
            {
                "id": "python",
                "kind": "SKILL",
                "type": "MUST_HAVE",
                "label": "Python",
                "value": "Python",
            }
        ],
    }
    first = await client.post(
        "/api/v1/jobs", json=body, headers={"Authorization": f"Bearer {plaintext}"}
    )
    second = await client.post(
        "/api/v1/jobs", json=body, headers={"Authorization": f"Bearer {plaintext}"}
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
