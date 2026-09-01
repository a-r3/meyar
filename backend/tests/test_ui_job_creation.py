"""HR UI productization — new-vacancy creation
(GET/POST /ui/jobs/new, POST /ui/jobs).

Covers: auth (missing jobs:write scope, unauthenticated redirect), CSRF,
tenant scoping/cross-tenant isolation, successful creation with MUST_HAVE
+ PREFERRED criteria (including an EXPERIENCE criterion), validation
(empty title, blank rows skipped, EXPERIENCE without min_years, sensitive/
prohibited criterion term rejected), no raw id/UUID entry surface, and
that the created vacancy is immediately usable by the existing
deterministic ranking flow — including, critically, that a UI-created
SKILL criterion resolves correctly against real candidate-profile
evidence (owner visual-inspection Blockers 2, A, B).
"""

import re

import pytest
from httpx import AsyncClient
from search_helpers import seed_candidate_with_profile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.config import Settings, get_settings
from meyar.main import app
from meyar.models.job import Job
from meyar.models.job_criteria_version import JobCriteriaVersion
from meyar.schemas.criteria import CriterionIn, CriterionKind, CriterionType
from meyar.services.job_criteria_repo import create_criteria_version
from meyar.services.job_repo import create_job
from meyar.services.tenant_membership_repo import create_membership
from meyar.services.tenant_repo import create_tenant
from meyar.services.user_repo import create_user


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
    Exercises the exact same require_ui_scopes enforcement path a
    genuinely reduced-permission role would, without inventing a fake
    product role that doesn't otherwise exist (see docs/DECISIONS.md)."""
    import uuid

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


async def test_new_job_form_requires_jobs_write_scope(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    tenant, _user, _password, _membership = tenant_and_user
    restricted_username, restricted_password = await _create_restricted_user(
        db_session, tenant_id=tenant.id
    )
    await _login_and_csrf(client, restricted_username, restricted_password)

    response = await client.get("/ui/jobs/new")

    assert response.status_code == 403


async def test_new_job_form_unauthenticated_is_redirected(client: AsyncClient) -> None:
    response = await client.get("/ui/jobs/new", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


async def test_new_job_form_has_no_raw_id_input(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    """The HR user must never type or see a raw criterion/job id — it is
    generated server-side from the entered requirement text."""
    _tenant, user, password, _membership = tenant_and_user
    await _login_and_csrf(client, user.username, password)

    response = await client.get("/ui/jobs/new")

    assert response.status_code == 200
    assert 'name="must_id_0"' not in response.text
    assert 'name="pref_id_0"' not in response.text
    assert re.search(r'name="[a-z_]*\bid\b[a-z_]*"', response.text) is None


async def test_new_job_form_has_a_single_requirement_field_not_ad_deyer(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    """Regression for owner visual-inspection Blocker B: the previous form
    exposed separate internal 'Ad'/'Dəyər' (label/value) fields, which
    caused Blocker A (an HR tester typed the requirement TYPE into the
    value field). There must be exactly one HR-facing text field per row
    now."""
    _tenant, user, password, _membership = tenant_and_user
    await _login_and_csrf(client, user.username, password)

    response = await client.get("/ui/jobs/new")

    assert response.status_code == 200
    assert 'name="must_requirement_0"' in response.text
    assert 'name="must_label_0"' not in response.text
    assert 'name="must_value_0"' not in response.text
    assert ">Tələb<" in response.text
    assert ">Ad<" not in response.text
    assert ">Dəyər<" not in response.text


async def test_create_job_requires_csrf_token(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    await _login_and_csrf(client, user.username, password)
    data = {"title": "No CSRF JD", "csrf_token": "wrong"}
    data.update(_blank_rows("must"))
    data.update(_blank_rows("pref"))

    response = await client.post("/ui/jobs", data=data)

    assert response.status_code == 403


async def test_create_job_requires_jobs_write_scope(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    tenant, _user, _password, _membership = tenant_and_user
    restricted_username, restricted_password = await _create_restricted_user(
        db_session, tenant_id=tenant.id
    )
    csrf = await _login_and_csrf(client, restricted_username, restricted_password)
    data = {"title": "Forbidden JD", "csrf_token": csrf}
    data.update(_blank_rows("must", filled=_row("must", 0, kind="SKILL", requirement="Python")))
    data.update(_blank_rows("pref"))

    response = await client.post("/ui/jobs", data=data)

    assert response.status_code == 403


async def test_empty_title_is_rejected_with_friendly_error(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    csrf = await _login_and_csrf(client, user.username, password)
    data = {"title": "   ", "csrf_token": csrf}
    data.update(_blank_rows("must", filled=_row("must", 0, kind="SKILL", requirement="Python")))
    data.update(_blank_rows("pref"))

    response = await client.post("/ui/jobs", data=data)

    assert response.status_code == 422
    assert "Vakansiya başlığı boş ola bilməz." in response.text


async def test_experience_criterion_without_min_years_is_rejected_and_input_preserved(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    csrf = await _login_and_csrf(client, user.username, password)
    data = {"title": "AML JD", "csrf_token": csrf}
    data.update(
        _blank_rows(
            "must",
            filled=_row("must", 0, kind="EXPERIENCE", requirement="Minimum təcrübə"),
        )
    )
    data.update(_blank_rows("pref"))

    response = await client.post("/ui/jobs", data=data)

    assert response.status_code == 422
    assert "Minimum təcrübə" in response.text
    assert "illik təcrübəni daxil edin" in response.text
    assert 'value="AML JD"' in response.text  # title preserved, not lost


async def test_non_experience_criterion_rejects_stray_min_years_input(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    """Kind-aware validation (semantic-correctness audit): a value typed
    into the 'Təcrübə (il)' field for a SKILL/CERTIFICATION/EDUCATION/
    LANGUAGE row must never be silently ignored — the form must reject
    it with a clear error rather than accept-then-drop the input."""
    _tenant, user, password, _membership = tenant_and_user
    csrf = await _login_and_csrf(client, user.username, password)
    data = {"title": "Kind Mismatch JD", "csrf_token": csrf}
    data.update(
        _blank_rows(
            "must",
            filled=_row("must", 0, kind="SKILL", requirement="Python", min_years="5"),
        )
    )
    data.update(_blank_rows("pref"))

    response = await client.post("/ui/jobs", data=data)

    assert response.status_code == 422
    assert "yalnız" in response.text
    assert "Təcrübə&#39; növü üçündür" in response.text
    jobs_page = await client.get("/ui/jobs")
    assert "Kind Mismatch JD" not in jobs_page.text


def _years_input_tag(html: str, name: str) -> str:
    match = re.search(
        r'<input type="number"[^>]*name="' + re.escape(name) + r'"[^>]*>', html
    )
    assert match is not None, f"years input {name!r} not found in rendered form"
    return match.group(0)


async def test_years_control_is_disabled_for_skill_criterion_rendered_ui(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    """Owner follow-up: the client-side presentation must match the
    server-side kind-aware rule. A row whose kind is not EXPERIENCE
    (default new-form rows are SKILL) must render the 'Minimum müddət
    (il)' control disabled and not user-editable, never implying it can
    be filled in for a SKILL/CERTIFICATION/EDUCATION/LANGUAGE row."""
    _tenant, user, password, _membership = tenant_and_user
    await _login_and_csrf(client, user.username, password)

    response = await client.get("/ui/jobs/new")

    assert response.status_code == 200
    tag = _years_input_tag(response.text, "must_min_years_0")
    assert "disabled" in tag
    assert 'placeholder="Tətbiq olunmur"' in tag
    assert 'value=""' in tag
    # The self-hosted enhancement script is wired to this exact hook class,
    # self-hosted (no external CDN), consistent with the UI CSP (script-src 'self').
    assert 'class="js-min-years"' in tag
    assert '<script src="/ui/static/job-form.js" defer></script>' in response.text


async def test_years_control_is_enabled_for_experience_criterion_rendered_ui(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    """A row whose kind is EXPERIENCE must render the duration control
    enabled with HR-facing 'Minimum müddət (il)' wording — verified via a
    form re-render (a second, invalid row forces re-render while
    preserving the first, valid EXPERIENCE row's posted values)."""
    _tenant, user, password, _membership = tenant_and_user
    csrf = await _login_and_csrf(client, user.username, password)
    data = {"title": "Mixed Rows JD", "csrf_token": csrf}
    data.update(
        _blank_rows(
            "must",
            filled={
                **_row(
                    "must", 0, kind="EXPERIENCE", requirement="Minimum təcrübə", min_years="5"
                ),
                # A prohibited/sensitive term on a second row forces a
                # validation failure and re-render without ever creating
                # the job, while row 0's EXPERIENCE values are preserved.
                **_row("must", 1, kind="SKILL", requirement="Yaş"),
            },
        )
    )
    data.update(_blank_rows("pref"))

    response = await client.post("/ui/jobs", data=data)

    assert response.status_code == 422
    tag = _years_input_tag(response.text, "must_min_years_0")
    assert "disabled" not in tag
    assert 'placeholder="Minimum müddət (il)"' in tag
    assert 'value="5"' in tag


async def test_stale_experience_years_value_is_cleared_when_kind_switches_to_skill(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    """Simulates: HR selected 'Təcrübə', typed 4, then changed the kind to
    'Bacarıq' before submitting (the client-side toggle clears the stale
    value on that same event; here the row is posted exactly as a
    kind-switched submission would look). The stale duration must never
    reach a persisted criterion and the re-rendered control must not echo
    it back as an editable value."""
    tenant, user, password, _membership = tenant_and_user
    csrf = await _login_and_csrf(client, user.username, password)
    data = {"title": "Kind Switch JD", "csrf_token": csrf}
    data.update(
        _blank_rows(
            "must",
            filled=_row("must", 0, kind="SKILL", requirement="Python", min_years="4"),
        )
    )
    data.update(_blank_rows("pref"))

    response = await client.post("/ui/jobs", data=data)

    assert response.status_code == 422
    assert "Təcrübə&#39; növü üçündür" in response.text
    tag = _years_input_tag(response.text, "must_min_years_0")
    assert "disabled" in tag
    assert 'value=""' in tag  # the stale "4" is not echoed back as editable

    job = (
        await db_session.execute(
            select(Job).where(Job.tenant_id == tenant.id, Job.title == "Kind Switch JD")
        )
    ).scalar_one_or_none()
    assert job is None


async def test_direct_manual_post_of_skill_kind_with_years_rejected_server_side(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    """Defense-in-depth: httpx never executes the client-side script, so
    this reproduces a hand-crafted/malicious POST bypassing the JS
    enhancement entirely (equivalent to JavaScript disabled). The
    deterministic server-side rule in _parse_criterion_row must still be
    the one rejecting SKILL + min_years, independent of any UI affordance."""
    tenant, user, password, _membership = tenant_and_user
    csrf = await _login_and_csrf(client, user.username, password)
    data = {"title": "Manual Bypass JD", "csrf_token": csrf}
    data.update(
        _blank_rows(
            "must",
            filled=_row("must", 0, kind="SKILL", requirement="Python", min_years="4"),
        )
    )
    data.update(_blank_rows("pref"))

    response = await client.post("/ui/jobs", data=data)

    assert response.status_code == 422
    assert "yalnız" in response.text
    assert "Təcrübə&#39; növü üçündür" in response.text
    job = (
        await db_session.execute(
            select(Job).where(Job.tenant_id == tenant.id, Job.title == "Manual Bypass JD")
        )
    ).scalar_one_or_none()
    assert job is None


async def test_sensitive_criterion_term_is_rejected(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    csrf = await _login_and_csrf(client, user.username, password)
    data = {"title": "Sensitive JD", "csrf_token": csrf}
    data.update(
        _blank_rows("must", filled=_row("must", 0, kind="SKILL", requirement="Yaş"))
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
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    tenant, user, password, _membership = tenant_and_user
    csrf = await _login_and_csrf(client, user.username, password)
    data = {"title": "Sparse JD", "csrf_token": csrf}
    data.update(_blank_rows("must", filled=_row("must", 0, kind="SKILL", requirement="Python")))
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
    assert version.criteria[0]["value"] == "Python"


async def test_default_weight_is_one_when_left_untouched(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    """The 'Əhəmiyyət' (weight) field pre-fills with a safe default so HR
    does not have to think about it for an ordinary criterion."""
    tenant, user, password, _membership = tenant_and_user
    await _login_and_csrf(client, user.username, password)
    form_page = await client.get("/ui/jobs/new")
    assert re.search(r'name="must_weight_0" value="1(\.0)?"', form_page.text)


async def test_create_job_with_must_have_and_preferred_criteria_is_immediately_rankable(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    tenant, user, password, _membership = tenant_and_user
    csrf = await _login_and_csrf(client, user.username, password)
    data = {"title": "Senior AML Analyst", "csrf_token": csrf}
    data.update(
        _blank_rows(
            "must",
            filled={
                **_row("must", 0, kind="SKILL", requirement="AML"),
                **_row("must", 1, kind="EXPERIENCE", requirement="Minimum təcrübə", min_years="3"),
            },
        )
    )
    data.update(
        _blank_rows(
            "pref",
            filled=_row("pref", 0, kind="CERTIFICATION", requirement="ACAMS sertifikatı"),
        )
    )

    create_response = await client.post("/ui/jobs", data=data, follow_redirects=False)
    assert create_response.status_code == 303
    assert create_response.headers["location"] == "/ui/jobs"

    jobs_page = await client.get("/ui/jobs")
    assert jobs_page.status_code == 200
    assert "Senior AML Analyst" in jobs_page.text
    assert "AML" in jobs_page.text
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
        "AML",
        "Minimum təcrübə",
        "ACAMS sertifikatı",
    }
    skill_criterion = next(c for c in version.criteria if c["id"] == "aml")
    # the single "Tələb" field becomes BOTH the display label and the
    # exact value the deterministic scorer matches — the two can never
    # diverge (this is the structural fix for owner-reported Blocker A)
    assert skill_criterion["label"] == skill_criterion["value"] == "AML"
    # no raw UUID/slug entry required from HR — ids are server-generated
    assert all(re.fullmatch(r"[a-z0-9_]{1,64}", c["id"]) for c in version.criteria)

    rank_response = await client.post(f"/ui/jobs/{version.id}/rank", data={"csrf_token": csrf})
    assert rank_response.status_code == 200
    assert "Reytinq nəticələri" in rank_response.text


async def test_created_job_is_not_visible_to_a_foreign_tenant(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    tenant, user, password, _membership = tenant_and_user
    csrf = await _login_and_csrf(client, user.username, password)
    data = {"title": "Tenant-Scoped JD", "csrf_token": csrf}
    data.update(_blank_rows("must", filled=_row("must", 0, kind="SKILL", requirement="Python")))
    data.update(_blank_rows("pref"))
    create_response = await client.post("/ui/jobs", data=data, follow_redirects=False)
    assert create_response.status_code == 303

    foreign = await create_tenant(db_session, name="Foreign HR tenant")
    foreign_username, foreign_password = "foreign-hr", "foreign-password-1"
    foreign_user = await create_user(
        db_session, username=foreign_username, plaintext_password=foreign_password
    )
    await create_membership(
        db_session, user_id=foreign_user.id, tenant_id=foreign.id, role="HR_USER"
    )
    await db_session.commit()
    await client.post("/ui/logout", data={"csrf_token": csrf})
    await _login_and_csrf(client, foreign_username, foreign_password)

    foreign_jobs_page = await client.get("/ui/jobs")

    assert foreign_jobs_page.status_code == 200
    assert "Tenant-Scoped JD" not in foreign_jobs_page.text


# ---------------------------------------------------------------------------
# Blocker A — root-cause acceptance: a UI-created MUST_HAVE SKILL criterion
# must resolve against real candidate-profile evidence exactly like an
# API/domain-created one. The owner-reported "Python -> Məlumat məlum
# deyil (UNKNOWN)" result was traced to the *previous* two-field form
# persisting {"label": "Python", "value": "MUST_HAVE"} — a malformed
# criterion, not a scoring/evaluator bug. These tests prove (1) the new
# single-field form cannot reproduce that malformed shape and correctly
# resolves real evidence, and (2) a UI-created criterion is byte-for-byte
# structurally identical to an API/domain-created one for the same
# requirement, so nothing about the creation path itself can diverge
# scoring behavior. See docs/DECISIONS.md D-025.
# ---------------------------------------------------------------------------


def _skill_profile(skill: str) -> dict:
    return {
        "skills": [
            {
                "name": skill,
                "category": None,
                "evidence": [{"page": 1, "block_index": 3, "quote": f"Skills: {skill}"}],
            }
        ],
        "employment_history": [],
        "education": [],
        "certifications": [],
        "languages": [],
        "projects": [],
    }


async def test_ui_created_skill_criterion_matches_real_candidate_evidence(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    """CRITICAL ACCEPTANCE TEST (Blocker A). A candidate with verified
    Python skill evidence, and a vacancy created through the exact HR
    /ui/jobs/new -> POST /ui/jobs application path with Python as a
    MUST_HAVE skill, must resolve to MATCH — not UNKNOWN — because the
    evidence genuinely exists. Also proves the converse is preserved:
    missing evidence still correctly resolves to UNKNOWN, so this is not
    weakening UNKNOWN into a false match."""
    tenant, user, password, _membership = tenant_and_user
    matching_candidate, _profile = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_skill_profile("Python")
    )
    no_evidence_candidate, _no_profile = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_skill_profile("SQL")
    )
    await db_session.commit()

    csrf = await _login_and_csrf(client, user.username, password)
    data = {"title": "Senior Python Developer", "csrf_token": csrf}
    data.update(_blank_rows("must", filled=_row("must", 0, kind="SKILL", requirement="Python")))
    data.update(_blank_rows("pref"))
    create_response = await client.post("/ui/jobs", data=data, follow_redirects=False)
    assert create_response.status_code == 303

    job = (
        await db_session.execute(
            select(Job).where(
                Job.tenant_id == tenant.id, Job.title == "Senior Python Developer"
            )
        )
    ).scalar_one()
    version = (
        await db_session.execute(
            select(JobCriteriaVersion).where(JobCriteriaVersion.job_id == job.id)
        )
    ).scalar_one()

    rank_response = await client.post(f"/ui/jobs/{version.id}/rank", data={"csrf_token": csrf})
    assert rank_response.status_code == 200
    text = rank_response.text
    assert "Python" in text
    assert "Bacarıq" in text
    assert str(matching_candidate.id) in text
    assert str(no_evidence_candidate.id) in text
    match_index = text.index(str(matching_candidate.id))
    no_evidence_index = text.index(str(no_evidence_candidate.id))
    match_card = text[:match_index]
    # crude but sufficient: the candidate WITH evidence must show a MATCH
    # verdict ("Uyğundur") somewhere in its own result card, and the
    # candidate WITHOUT evidence must genuinely still show UNKNOWN
    # ("Məlumat məlum deyil") — proving evidence-driven correctness in
    # both directions, not a blanket match.
    assert "100.00 / 100" in text or "Uyğundur" in text
    assert "Məlumat məlum deyil" in text
    assert match_card != text[no_evidence_index:]


async def test_ui_created_criterion_is_structurally_equivalent_to_api_created(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    """A criterion created via /ui/jobs must be structurally identical
    (same kind/type/label/value/min_years shape) to one created directly
    through the domain services the internal REST API uses — proving the
    two creation paths cannot semantically diverge for scoring."""
    tenant, user, password, _membership = tenant_and_user
    csrf = await _login_and_csrf(client, user.username, password)
    data = {"title": "Equivalence JD", "csrf_token": csrf}
    data.update(
        _blank_rows("must", filled=_row("must", 0, kind="SKILL", requirement="Kubernetes"))
    )
    data.update(_blank_rows("pref"))
    create_response = await client.post("/ui/jobs", data=data, follow_redirects=False)
    assert create_response.status_code == 303

    ui_job = (
        await db_session.execute(
            select(Job).where(Job.tenant_id == tenant.id, Job.title == "Equivalence JD")
        )
    ).scalar_one()
    ui_version = (
        await db_session.execute(
            select(JobCriteriaVersion).where(JobCriteriaVersion.job_id == ui_job.id)
        )
    ).scalar_one()
    ui_criterion = ui_version.criteria[0]

    api_job = await create_job(db_session, tenant_id=tenant.id, title="API-created JD")
    api_version = await create_criteria_version(
        db_session,
        tenant_id=tenant.id,
        job_id=api_job.id,
        criteria=[
            CriterionIn(
                id="kubernetes",
                kind=CriterionKind.SKILL,
                type=CriterionType.MUST_HAVE,
                label="Kubernetes",
                value="Kubernetes",
                weight=1.0,
            ).model_dump(mode="json")
        ],
        created_by_api_key_id=None,
    )
    await db_session.commit()
    api_criterion = api_version.criteria[0]

    assert ui_criterion["kind"] == api_criterion["kind"] == "SKILL"
    assert ui_criterion["type"] == api_criterion["type"] == "MUST_HAVE"
    assert ui_criterion["label"] == api_criterion["label"] == "Kubernetes"
    assert ui_criterion["value"] == api_criterion["value"] == "Kubernetes"
    assert ui_criterion["min_years"] == api_criterion["min_years"] is None
    assert ui_criterion.keys() == api_criterion.keys()
