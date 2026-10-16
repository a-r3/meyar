"""Issue #45 — candidate / job / evaluation contracts on BOTH surfaces.

Every test in this module is parametrized over the machine API (``/api/v1``,
API key) and the human JSON API (``/ui/api/v1``, session + CSRF): the routes
are one shared factory, and these tests are what prove they stay identical.
Synthetic data only."""

import uuid
from datetime import date
from unittest import mock

import pytest
from api_contract_support import Surface, human_surface, machine_surface, upload_cv
from conftest import BrowserTestClient as AsyncClient
from fakes import FakeEmbeddingProvider, FakeLLMProvider
from search_helpers import (
    seed_candidate_with_profile,
    seed_next_profile_version,
    synthetic_evidence,
)
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_ui_agent_routes import local_ui_settings  # noqa: F401 - pytest fixture re-export

from meyar.config import Settings
from meyar.embedding.dependency import get_embedding_provider, get_embedding_search_config
from meyar.llm.dependency import get_llm_provider
from meyar.main import app
from meyar.models.api_key import ApiKey
from meyar.models.audit_event import AuditEvent
from meyar.models.evaluation import Evaluation
from meyar.schemas.criteria import CriterionIn, CriterionKind, CriterionType
from meyar.search.schemas import EmbeddingSearchConfig
from meyar.services.api_key_repo import create_api_key
from meyar.services.job_criteria_repo import create_criteria_version
from meyar.services.job_repo import create_job
from meyar.services.tenant_repo import create_tenant

EMPTY = {
    "skills": [],
    "employment_history": [],
    "education": [],
    "certifications": [],
    "languages": [],
    "projects": [],
}


def _profile(*skills: str) -> dict:
    return {
        **EMPTY,
        "skills": [{"name": s, "evidence": synthetic_evidence(s)} for s in skills],
    }


def _skill(criterion_id: str, value: str, weight: float = 1.0) -> CriterionIn:
    return CriterionIn(
        id=criterion_id,
        kind=CriterionKind.SKILL,
        type=CriterionType.MUST_HAVE,
        label=value,
        value=value,
        weight=weight,
    )


@pytest.fixture(params=["machine", "human"])
async def surface(
    request,
    client: AsyncClient,
    tenant_key_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> Surface:
    tenant, _key, plaintext, user, password, _membership = tenant_key_and_user
    if request.param == "machine":
        return machine_surface(plaintext)
    return await human_surface(client, user.username, password)


@pytest.fixture
def tenant_id(tenant_key_and_user) -> uuid.UUID:
    return tenant_key_and_user[0].id


@pytest.fixture
def machine_key(tenant_key_and_user) -> str:
    return tenant_key_and_user[2]


async def _job(db: AsyncSession, tenant: uuid.UUID, *criteria: CriterionIn):  # noqa: ANN202
    job = await create_job(db, tenant_id=tenant, title="Contract Job")
    version = await create_criteria_version(
        db,
        tenant_id=tenant,
        job_id=job.id,
        criteria=[c.model_dump(mode="json") for c in criteria],
        created_by_api_key_id=None,
    )
    await db.commit()
    return job.id, version.version_number, version.id


async def _events(db: AsyncSession, tenant: uuid.UUID, event_type: str) -> list[AuditEvent]:
    return list(
        (
            await db.scalars(
                select(AuditEvent)
                .where(AuditEvent.tenant_id == tenant, AuditEvent.event_type == event_type)
                .execution_options(populate_existing=True)
            )
        ).all()
    )


# --- candidate library ------------------------------------------------------


async def test_library_is_bounded_stably_ordered_and_tenant_scoped(
    client: AsyncClient, db_session: AsyncSession, surface: Surface, tenant_id: uuid.UUID
) -> None:
    foreign = await create_tenant(db_session, name=f"Other-{uuid.uuid4().hex[:6]}")
    foreign_id = foreign.id
    for _ in range(3):
        await seed_candidate_with_profile(
            db_session, tenant_id=tenant_id, profile_content=_profile("Python")
        )
    foreign_candidate, _v = await seed_candidate_with_profile(
        db_session, tenant_id=foreign_id, profile_content=_profile("Python")
    )
    foreign_candidate_id = str(foreign_candidate.id)
    await db_session.commit()

    first = (
        await client.get(surface.url("/candidates?page_size=2"), headers=surface.read_headers)
    ).json()
    second = (
        await client.get(
            surface.url("/candidates?page_size=2&page=2"), headers=surface.read_headers
        )
    ).json()
    again = (
        await client.get(surface.url("/candidates?page_size=2"), headers=surface.read_headers)
    ).json()
    assert first == again  # deterministic
    assert first["total"] == 3 and first["has_next"] is True and first["has_previous"] is False
    assert second["has_next"] is False and len(second["items"]) == 1
    ids = [i["candidate_id"] for i in first["items"] + second["items"]]
    assert len(set(ids)) == 3 and foreign_candidate_id not in ids
    assert all(
        item["readiness"]["state"] in {"READY", "PROCESSING", "NEEDS_ATTENTION"}
        for item in first["items"]
    )
    assert (
        await client.get(surface.url("/candidates?page_size=51"), headers=surface.read_headers)
    ).status_code == 422
    assert (
        await client.get(surface.url("/candidates?page=0"), headers=surface.read_headers)
    ).status_code == 422
    unsupported = await client.get(
        surface.url("/candidates?parser_status=NOPE"), headers=surface.read_headers
    )
    assert unsupported.status_code == 422
    assert unsupported.json()["error"]["reason"] == "UNSUPPORTED_FILTER"
    assert (
        await client.get(
            surface.url("/candidates?profile_status=COMPLETED"), headers=surface.read_headers
        )
    ).json()["total"] == 3


async def test_detail_exposes_readiness_provenance_and_hides_foreign_candidates(
    client: AsyncClient, db_session: AsyncSession, surface: Surface, tenant_id: uuid.UUID
) -> None:
    candidate, version = await seed_candidate_with_profile(
        db_session, tenant_id=tenant_id, profile_content=_profile("Python")
    )
    foreign = await create_tenant(db_session, name=f"Other-{uuid.uuid4().hex[:6]}")
    other_candidate, _ = await seed_candidate_with_profile(
        db_session, tenant_id=foreign.id, profile_content=_profile("Python")
    )
    candidate_id, version_id, other_id = str(candidate.id), str(version.id), str(other_candidate.id)
    await db_session.commit()

    response = await client.get(
        surface.url(f"/candidates/{candidate_id}/detail"), headers=surface.read_headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["readiness"]["state"] == "READY"
    assert body["readiness"]["current_profile_available"] is True
    assert body["readiness"]["retryable_stages"] == ["EMBEDDING"]  # no embedding yet
    assert body["provenance"]["profile_version_id"] == version_id
    assert body["provenance"]["profile_version"] == 1
    assert body["provenance"]["extraction_model_name"] == "fake-model"
    assert [s["title"] for s in body["skills"]] == ["Python"]
    assert body["skills"][0]["evidence"][0]["page"] == 1
    for forbidden in ("storage_key", "/home", "sha256_hash"):
        assert forbidden not in response.text

    foreign_detail = await client.get(
        surface.url(f"/candidates/{other_id}/detail"), headers=surface.read_headers
    )
    unknown = await client.get(
        surface.url(f"/candidates/{uuid.uuid4()}/detail"), headers=surface.read_headers
    )
    assert foreign_detail.status_code == unknown.status_code == 404
    assert foreign_detail.json() == unknown.json()


# --- original CV --------------------------------------------------------------


async def test_original_cv_success_is_audited_once_with_channel_and_no_leaks(
    client: AsyncClient,
    db_session: AsyncSession,
    surface: Surface,
    tenant_id: uuid.UUID,
    machine_key: str,
) -> None:
    candidate_id, document_id = await upload_cv(client, machine_key)
    response = await client.get(
        surface.url(f"/candidates/{candidate_id}/documents/{document_id}/original"),
        headers=surface.read_headers,
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    disposition = response.headers["content-disposition"]
    assert disposition == f'attachment; filename="cv-{document_id.replace("-", "")}.pdf"'
    assert "valid_cv" not in disposition  # the uploaded filename is never reused
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    for value in response.headers.values():
        assert "/storage" not in value and "tenant" not in value.lower()

    events = await _events(db_session, tenant_id, "candidate.original_cv.accessed")
    assert len(events) == 1
    event = events[0]
    expected_channel = "API" if surface.name == "machine" else "UI_JSON"
    assert event.event_metadata == {
        "candidate_id": candidate_id,
        "document_id": document_id,
        "channel": expected_channel,
    }
    assert event.actor_type == ("API_KEY" if surface.name == "machine" else "HUMAN_USER")
    assert event.actor_id is not None
    blob = str(event.event_metadata)
    assert "valid_cv" not in blob and "storage" not in blob and ".pdf" not in blob


async def test_original_cv_denials_never_audit_success_and_do_not_leak_existence(
    client: AsyncClient,
    db_session: AsyncSession,
    surface: Surface,
    tenant_id: uuid.UUID,
    machine_key: str,
) -> None:
    candidate_id, document_id = await upload_cv(client, machine_key)
    foreign = await create_tenant(db_session, name=f"Other-{uuid.uuid4().hex[:6]}")
    _key, foreign_plaintext = await create_api_key(db_session, tenant_id=foreign.id, env="test")
    await db_session.commit()
    other_candidate, other_document = await upload_cv(client, foreign_plaintext)

    attempts = [
        f"/candidates/{other_candidate}/documents/{other_document}/original",  # foreign tenant
        f"/candidates/{candidate_id}/documents/{other_document}/original",  # mixed ids
        f"/candidates/{other_candidate}/documents/{document_id}/original",  # wrong candidate
        f"/candidates/{uuid.uuid4()}/documents/{uuid.uuid4()}/original",  # unknown
    ]
    bodies = []
    for path in attempts:
        response = await client.get(surface.url(path), headers=surface.read_headers)
        assert response.status_code == 404, path
        bodies.append(response.json())
    assert all(body == bodies[0] for body in bodies)
    assert await _events(db_session, tenant_id, "candidate.original_cv.accessed") == []
    assert await _events(db_session, foreign.id, "candidate.original_cv.accessed") == []


async def test_original_cv_storage_failure_serves_nothing_and_audits_nothing(
    client: AsyncClient,
    db_session: AsyncSession,
    surface: Surface,
    tenant_id: uuid.UUID,
    machine_key: str,
    tmp_path,
) -> None:
    candidate_id, document_id = await upload_cv(client, machine_key)
    for stored in (tmp_path / "storage").rglob("*"):
        if stored.is_file():
            stored.unlink()
    response = await client.get(
        surface.url(f"/candidates/{candidate_id}/documents/{document_id}/original"),
        headers=surface.read_headers,
    )
    assert response.status_code == 503
    assert response.json()["error"]["reason"] == "ORIGINAL_UNAVAILABLE"
    assert "storage" not in response.text.lower().replace("original_unavailable", "")
    assert await _events(db_session, tenant_id, "candidate.original_cv.accessed") == []


async def test_original_cv_audit_failure_withholds_the_bytes(
    client: AsyncClient, db_session: AsyncSession, surface: Surface, machine_key: str
) -> None:
    from sqlalchemy.exc import SQLAlchemyError

    candidate_id, document_id = await upload_cv(client, machine_key)
    with mock.patch(
        "meyar.application.candidates.record_event", side_effect=SQLAlchemyError("boom")
    ):
        response = await client.get(
            surface.url(f"/candidates/{candidate_id}/documents/{document_id}/original"),
            headers=surface.read_headers,
        )
    assert response.status_code == 503
    assert b"%PDF" not in response.content


async def test_ui_original_cv_download_is_audited_through_the_same_operation(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_key_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, _key, plaintext, user, password, _m = tenant_key_and_user
    tenant_id_value, username = tenant.id, user.username
    candidate_id, document_id = await upload_cv(client, plaintext)
    await client.post("/ui/login", data={"username": username, "password": password})
    ok = await client.get(f"/ui/candidates/{candidate_id}/documents/{document_id}/original")
    assert ok.status_code == 200
    denied = await client.get(f"/ui/candidates/{candidate_id}/documents/{uuid.uuid4()}/original")
    assert denied.status_code == 404
    events = await _events(db_session, tenant_id_value, "candidate.original_cv.accessed")
    assert [e.event_metadata["channel"] for e in events] == ["UI"]
    assert events[0].actor_type == "HUMAN_USER"


async def test_inactive_authority_cannot_download_originals(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_key_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    from meyar.services.tenant_membership_repo import set_membership_active

    tenant, api_key, plaintext, user, password, membership = tenant_key_and_user
    tenant_id_value, membership_id, key_id = tenant.id, membership.id, api_key.id
    candidate_id, document_id = await upload_cv(client, plaintext)
    human = await human_surface(client, user.username, password)
    path = f"/candidates/{candidate_id}/documents/{document_id}/original"
    await set_membership_active(db_session, membership_id=membership_id, is_active=False)
    await db_session.execute(
        update(ApiKey).where(ApiKey.id == key_id).values(revoked_at=mock.ANY if False else None)
    )
    await db_session.commit()
    assert (await client.get(human.url(path), headers=human.read_headers)).status_code == 401
    from datetime import UTC, datetime

    await db_session.execute(
        update(ApiKey).where(ApiKey.id == key_id).values(revoked_at=datetime.now(UTC))
    )
    await db_session.commit()
    machine = machine_surface(plaintext)
    assert (await client.get(machine.url(path), headers=machine.read_headers)).status_code == 401
    assert await _events(db_session, tenant_id_value, "candidate.original_cv.accessed") == []


# --- supported retry ------------------------------------------------------------


async def test_retry_embedding_is_supported_idempotent_and_tenant_scoped(
    client: AsyncClient, db_session: AsyncSession, surface: Surface, tenant_id: uuid.UUID
) -> None:
    candidate, _version = await seed_candidate_with_profile(
        db_session, tenant_id=tenant_id, profile_content=_profile("Python")
    )
    foreign = await create_tenant(db_session, name=f"Other-{uuid.uuid4().hex[:6]}")
    other, _ = await seed_candidate_with_profile(
        db_session, tenant_id=foreign.id, profile_content=_profile("Python")
    )
    candidate_id, other_id = str(candidate.id), str(other.id)
    await db_session.commit()
    provider = FakeEmbeddingProvider(dimensions=8)
    app.dependency_overrides[get_embedding_provider] = lambda: provider
    app.dependency_overrides[get_embedding_search_config] = lambda: EmbeddingSearchConfig(
        provider=provider.provider_name,
        model_name=provider.model_name,
        model_revision="",
        serializer_version=provider.compatibility.serializer_version,
        embedding_dimensions=8,
    )
    first = await client.post(
        surface.url(f"/candidates/{candidate_id}/processing/retry"),
        headers=surface.write_headers,
        json={"stage": "EMBEDDING"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["outcome"] == "COMPLETED" and first.json()["embedding_version_id"]
    second = await client.post(
        surface.url(f"/candidates/{candidate_id}/processing/retry"),
        headers=surface.write_headers,
        json={"stage": "EMBEDDING"},
    )
    assert second.json()["outcome"] == "REUSED"
    assert provider.call_count == 1
    detail = (
        await client.get(
            surface.url(f"/candidates/{candidate_id}/detail"), headers=surface.read_headers
        )
    ).json()
    assert detail["readiness"]["retryable_stages"] == []  # nothing left to retry

    foreign_retry = await client.post(
        surface.url(f"/candidates/{other_id}/processing/retry"),
        headers=surface.write_headers,
        json={"stage": "EMBEDDING"},
    )
    assert foreign_retry.status_code == 404
    assert provider.call_count == 1  # the foreign candidate was never embedded


async def test_retry_profile_extraction_reads_the_authorized_document_only(
    client: AsyncClient,
    db_session: AsyncSession,
    surface: Surface,
    machine_key: str,
    tenant_id: uuid.UUID,
) -> None:
    from meyar.schemas.candidate_profile import CandidateProfileExtraction

    candidate_id, document_id = await upload_cv(client, machine_key)
    fake = FakeLLMProvider(
        extraction=CandidateProfileExtraction.model_validate(
            {**EMPTY, "skills": [{"name": "Python", "evidence": synthetic_evidence("Python")}]}
        )
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    missing_doc = await client.post(
        surface.url(f"/candidates/{candidate_id}/processing/retry"),
        headers=surface.write_headers,
        json={"stage": "PROFILE_EXTRACTION"},
    )
    assert missing_doc.status_code == 422
    assert missing_doc.json()["error"]["reason"] == "DOCUMENT_REQUIRED"
    foreign_doc = await client.post(
        surface.url(f"/candidates/{candidate_id}/processing/retry"),
        headers=surface.write_headers,
        json={"stage": "PROFILE_EXTRACTION", "document_id": str(uuid.uuid4())},
    )
    assert foreign_doc.status_code == 404
    assert fake.call_count == 0  # nothing reached the model
    ok = await client.post(
        surface.url(f"/candidates/{candidate_id}/processing/retry"),
        headers=surface.write_headers,
        json={"stage": "PROFILE_EXTRACTION", "document_id": document_id},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["profile_status"] in {"COMPLETED", "MANUAL_REVIEW_REQUIRED", "FAILED"}
    assert fake.call_count >= 1


async def test_retry_rejects_unsupported_stages_and_authority_fields(
    client: AsyncClient, surface: Surface
) -> None:
    for payload in (
        {"stage": "PARSE"},
        {"stage": "IDENTITY_EXTRACTION"},
        {"stage": "EMBEDDING", "tenant_id": "x"},
        {},
    ):
        response = await client.post(
            surface.url(f"/candidates/{uuid.uuid4()}/processing/retry"),
            headers=surface.write_headers,
            json=payload,
        )
        assert response.status_code == 422, payload


# --- jobs -----------------------------------------------------------------------


async def test_job_list_archive_lifecycle_parity_and_restrictions(
    client: AsyncClient, db_session: AsyncSession, surface: Surface, tenant_id: uuid.UUID
) -> None:
    job_a, version_a, _ = await _job(db_session, tenant_id, _skill("python", "Python"))
    job_b, _vb, _ = await _job(db_session, tenant_id, _skill("java", "Java"))
    foreign = await create_tenant(db_session, name=f"Other-{uuid.uuid4().hex[:6]}")
    foreign_job, _vf, _ = await _job(db_session, foreign.id, _skill("go", "Go"))
    await db_session.commit()

    active = (
        await client.get(surface.url("/jobs?page_size=1"), headers=surface.read_headers)
    ).json()
    assert active["total"] == 2 and active["has_next"] is True and len(active["items"]) == 1
    assert active["items"][0]["status"] == "ACTIVE"
    assert (
        await client.get(surface.url("/jobs?page_size=51"), headers=surface.read_headers)
    ).status_code == 422
    assert (
        await client.get(surface.url("/jobs?status=DELETED"), headers=surface.read_headers)
    ).status_code == 422

    # foreign job: identical 404 to an unknown one
    foreign_archive = await client.post(
        surface.url(f"/jobs/{foreign_job}/archive"), headers=surface.write_headers
    )
    unknown_archive = await client.post(
        surface.url(f"/jobs/{uuid.uuid4()}/archive"), headers=surface.write_headers
    )
    assert foreign_archive.status_code == unknown_archive.status_code == 404

    archived = await client.post(
        surface.url(f"/jobs/{job_a}/archive"), headers=surface.write_headers
    )
    assert archived.status_code == 200 and archived.json()["already_archived"] is False
    stamp = archived.json()["archived_at"]
    again = await client.post(surface.url(f"/jobs/{job_a}/archive"), headers=surface.write_headers)
    assert again.json()["already_archived"] is True and again.json()["archived_at"] == stamp
    assert len(await _events(db_session, tenant_id, "job.archived")) == 1
    event = (await _events(db_session, tenant_id, "job.archived"))[0]
    assert event.actor_type == ("API_KEY" if surface.name == "machine" else "HUMAN_USER")

    listing = (
        await client.get(surface.url("/jobs?status=ARCHIVED"), headers=surface.read_headers)
    ).json()
    assert [i["job_id"] for i in listing["items"]] == [str(job_a)]
    assert (await client.get(surface.url("/jobs"), headers=surface.read_headers)).json()[
        "total"
    ] == 1

    # archived jobs cannot be ranked (existing restriction) ...
    ranked = await client.post(
        surface.url(f"/jobs/{job_a}/criteria/{version_a}/rank"),
        headers=surface.write_headers,
        json={},
    )
    assert ranked.status_code == 409 and ranked.json()["error"]["reason"] == "JOB_ARCHIVED"
    # ... but the active one still can.
    assert (
        await client.post(
            surface.url(f"/jobs/{job_b}/criteria/1/rank"), headers=surface.write_headers, json={}
        )
    ).status_code == 200


async def test_criteria_versions_are_immutable_over_http(
    client: AsyncClient, db_session: AsyncSession, surface: Surface, tenant_id: uuid.UUID
) -> None:
    job, version, _ = await _job(db_session, tenant_id, _skill("python", "Python"))
    for method in ("PUT", "PATCH", "DELETE"):
        response = await client.request(
            method,
            surface.url(f"/jobs/{job}/criteria/{version}"),
            headers=surface.write_headers,
            json={},
        )
        assert response.status_code in (404, 405), method


async def test_machine_api_job_creation_still_permits_duplicates(
    client: AsyncClient, machine_key: str
) -> None:
    """Documented compatibility contract (D-116): the machine API has NO
    duplicate guard (only the human/UI workflow does). #45 must not change it."""
    payload = {
        "title": "Same",
        "criteria": [
            {
                "id": "python",
                "kind": "SKILL",
                "type": "MUST_HAVE",
                "label": "Python",
                "value": "Python",
                "weight": 1.0,
            }
        ],
    }
    headers = {"Authorization": f"Bearer {machine_key}"}
    first = await client.post("/api/v1/jobs", json=payload, headers=headers)
    second = await client.post("/api/v1/jobs", json=payload, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["status"] == "ACTIVE" and first.json()["archived_at"] is None


async def test_machine_created_versions_report_truthful_absent_provenance(
    client: AsyncClient,
    db_session: AsyncSession,
    surface: Surface,
    machine_key: str,
    tenant_id: uuid.UUID,
) -> None:
    created = await client.post(
        "/api/v1/jobs",
        json={
            "title": "API job",
            "criteria": [
                {
                    "id": "python",
                    "kind": "SKILL",
                    "type": "MUST_HAVE",
                    "label": "Python",
                    "value": "Python",
                    "weight": 1.0,
                }
            ],
        },
        headers={"Authorization": f"Bearer {machine_key}"},
    )
    job = created.json()["id"]
    response = await client.get(
        surface.url(f"/jobs/{job}/criteria/1/provenance"), headers=surface.read_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["authoring_channel"] == "MACHINE_API"
    assert body["semantic_provenance"] is None and body["draft_confirmation"] is None
    assert body["non_scorable_recorded"] is False and body["non_scorable_requirements"] == []
    foreign = await create_tenant(db_session, name=f"Other-{uuid.uuid4().hex[:6]}")
    foreign_job, *_ = await _job(db_session, foreign.id, _skill("go", "Go"))
    other = await client.get(
        surface.url(f"/jobs/{foreign_job}/criteria/1/provenance"), headers=surface.read_headers
    )
    unknown = await client.get(
        surface.url(f"/jobs/{uuid.uuid4()}/criteria/1/provenance"), headers=surface.read_headers
    )
    assert other.status_code == unknown.status_code == 404 and other.json() == unknown.json()


# --- evaluations ------------------------------------------------------------------


async def test_evaluation_get_returns_the_full_explanation_and_is_tenant_isolated(
    client: AsyncClient, db_session: AsyncSession, surface: Surface, tenant_id: uuid.UUID
) -> None:
    candidate, version = await seed_candidate_with_profile(
        db_session, tenant_id=tenant_id, profile_content=_profile("Python")
    )
    job, number, _ = await _job(db_session, tenant_id, _skill("python", "Python", 2.0))
    candidate_id, version_id = str(candidate.id), str(version.id)
    foreign = await create_tenant(db_session, name=f"Other-{uuid.uuid4().hex[:6]}")
    foreign_id = foreign.id
    await db_session.commit()

    scored = await client.post(
        surface.url(f"/jobs/{job}/criteria/{number}/score"),
        headers=surface.write_headers,
        json={"candidate_id": candidate_id, "evaluation_as_of_date": "2026-01-01"},
    )
    assert scored.status_code == 200, scored.text
    evaluation_id = scored.json()["evaluation_id"]
    fetched = await client.get(
        surface.url(f"/evaluations/{evaluation_id}"), headers=surface.read_headers
    )
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["numeric_score"] == "100.00" and body["status"] == "COMPLETED"
    assert body["candidate_profile_version_id"] == version_id
    assert body["explanation"] == scored.json()["explanation"]
    assert body["explanation"]["criteria"][0]["criterion_id"] == "python"

    # A foreign principal gets the same 404 as for a random id.
    _fk, foreign_plaintext = await create_api_key(db_session, tenant_id=foreign_id, env="test")
    await db_session.commit()
    foreign_get = await client.get(
        f"/api/v1/evaluations/{evaluation_id}",
        headers={"Authorization": f"Bearer {foreign_plaintext}"},
    )
    random_get = await client.get(
        f"/api/v1/evaluations/{uuid.uuid4()}",
        headers={"Authorization": f"Bearer {foreign_plaintext}"},
    )
    assert foreign_get.status_code == random_get.status_code == 404
    assert foreign_get.json() == random_get.json()


async def test_historical_profile_version_scores_exactly_that_version(
    client: AsyncClient, db_session: AsyncSession, surface: Surface, tenant_id: uuid.UUID
) -> None:
    candidate, v1 = await seed_candidate_with_profile(
        db_session, tenant_id=tenant_id, profile_content=_profile("Python")
    )
    v2 = await seed_next_profile_version(
        db_session, tenant_id=tenant_id, candidate=candidate, profile_content=_profile("Java")
    )
    other, other_version = await seed_candidate_with_profile(
        db_session, tenant_id=tenant_id, profile_content=_profile("Python")
    )
    foreign = await create_tenant(db_session, name=f"Other-{uuid.uuid4().hex[:6]}")
    _fc, foreign_version = await seed_candidate_with_profile(
        db_session, tenant_id=foreign.id, profile_content=_profile("Python")
    )
    job, number, _ = await _job(db_session, tenant_id, _skill("python", "Python"))
    ids = {
        "candidate": str(candidate.id),
        "v1": str(v1.id),
        "v2": str(v2.id),
        "other_version": str(other_version.id),
        "foreign_version": str(foreign_version.id),
    }
    await db_session.commit()
    url = surface.url(f"/jobs/{job}/criteria/{number}/score")
    body = {"candidate_id": ids["candidate"], "evaluation_as_of_date": "2026-01-01"}

    current = await client.post(url, headers=surface.write_headers, json=body)
    assert current.json()["candidate_profile_version_id"] == ids["v2"]
    assert current.json()["numeric_score"] == "0.00"  # the CURRENT profile has Java only
    historical = await client.post(
        url,
        headers=surface.write_headers,
        json={**body, "candidate_profile_version_id": ids["v1"]},
    )
    assert historical.status_code == 200
    assert historical.json()["candidate_profile_version_id"] == ids["v1"]
    assert historical.json()["numeric_score"] == "100.00"
    assert historical.json()["explanation"]["candidate_profile_version_id"] == ids["v1"]

    for bad in (ids["other_version"], ids["foreign_version"], str(uuid.uuid4())):
        refused = await client.post(
            url, headers=surface.write_headers, json={**body, "candidate_profile_version_id": bad}
        )
        assert refused.status_code == 404, bad
        assert refused.json()["detail"] == "Candidate profile not found."

    # Batch ranking keeps using the EFFECTIVE (current) profile only.
    ranked = await client.post(
        surface.url(f"/jobs/{job}/criteria/{number}/rank"),
        headers=surface.write_headers,
        json={"evaluation_as_of_date": "2026-01-01"},
    )
    by_candidate = {r["candidate_id"]: r for r in ranked.json()["results"]}
    assert by_candidate[ids["candidate"]]["candidate_profile_version_id"] == ids["v2"]
    assert by_candidate[ids["candidate"]]["numeric_score"] == "0.00"
    assert by_candidate[str(other.id)]["numeric_score"] == "100.00"
    assert ids["foreign_version"] not in ranked.text


async def test_batch_ranking_exposes_the_full_explanation_per_candidate(
    client: AsyncClient, db_session: AsyncSession, surface: Surface, tenant_id: uuid.UUID
) -> None:
    candidate, version = await seed_candidate_with_profile(
        db_session, tenant_id=tenant_id, profile_content=_profile("Python")
    )
    job, number, _ = await _job(db_session, tenant_id, _skill("python", "Python"))
    version_id = str(version.id)
    await db_session.commit()
    ranked = await client.post(
        surface.url(f"/jobs/{job}/criteria/{number}/rank"),
        headers=surface.write_headers,
        json={"evaluation_as_of_date": "2026-01-01"},
    )
    assert ranked.status_code == 200
    item = ranked.json()["results"][0]
    explanation = item["explanation"]
    assert explanation["numeric_score"] == item["numeric_score"] == "100.00"
    assert explanation["candidate_profile_version_id"] == version_id
    assert explanation["evaluation_id"] == item["evaluation_id"]
    assert explanation["criteria"][0]["evidence_references"] == [{"page": 1, "block_index": 0}]
    fetched = await client.get(
        surface.url(f"/evaluations/{item['evaluation_id']}"), headers=surface.read_headers
    )
    assert fetched.json()["explanation"] == explanation


# --- business date ------------------------------------------------------------------


async def test_omitted_date_is_resolved_exactly_once_and_persisted(
    client: AsyncClient, db_session: AsyncSession, surface: Surface, tenant_id: uuid.UUID
) -> None:
    candidate, _v = await seed_candidate_with_profile(
        db_session, tenant_id=tenant_id, profile_content=_profile("Python")
    )
    job, number, _ = await _job(db_session, tenant_id, _skill("python", "Python"))
    candidate_id = str(candidate.id)
    await db_session.commit()
    fixed = date(2031, 3, 4)
    calls: list[str] = []

    def fake_resolver(timezone_name: str) -> date:
        calls.append(timezone_name)
        return fixed

    with mock.patch("meyar.application.business_date.resolve_business_date", fake_resolver):
        scored = await client.post(
            surface.url(f"/jobs/{job}/criteria/{number}/score"),
            headers=surface.write_headers,
            json={"candidate_id": candidate_id},
        )
        assert calls == ["Asia/Baku"]  # once, from the configured business timezone
        ranked = await client.post(
            surface.url(f"/jobs/{job}/criteria/{number}/rank"),
            headers=surface.write_headers,
            json={},
        )
        assert len(calls) == 2  # one more call for the second request, not one per candidate
    assert scored.json()["evaluation_as_of_date"] == "2031-03-04"
    assert scored.json()["evaluation_date_defaulted"] is True
    assert scored.json()["explanation"]["evaluation_as_of_date"] == "2031-03-04"
    assert ranked.json()["evaluation_as_of_date"] == "2031-03-04"
    assert ranked.json()["evaluation_date_defaulted"] is True
    persisted = (
        await db_session.scalars(select(Evaluation).execution_options(populate_existing=True))
    ).all()
    assert {e.evaluation_as_of_date for e in persisted} == {fixed}


async def test_explicit_date_is_honored_and_the_resolver_is_not_consulted(
    client: AsyncClient, db_session: AsyncSession, surface: Surface, tenant_id: uuid.UUID
) -> None:
    candidate, _v = await seed_candidate_with_profile(
        db_session, tenant_id=tenant_id, profile_content=_profile("Python")
    )
    job, number, _ = await _job(db_session, tenant_id, _skill("python", "Python"))
    candidate_id = str(candidate.id)
    await db_session.commit()

    def forbidden(_timezone_name: str) -> date:
        raise AssertionError("the business clock must not be read for an explicit date")

    with mock.patch("meyar.application.business_date.resolve_business_date", forbidden):
        scored = await client.post(
            surface.url(f"/jobs/{job}/criteria/{number}/score"),
            headers=surface.write_headers,
            json={"candidate_id": candidate_id, "evaluation_as_of_date": "2020-02-29"},
        )
        ranked = await client.post(
            surface.url(f"/jobs/{job}/criteria/{number}/rank"),
            headers=surface.write_headers,
            json={"evaluation_as_of_date": "2020-02-29"},
        )
    assert scored.json()["evaluation_as_of_date"] == "2020-02-29"
    assert scored.json()["evaluation_date_defaulted"] is False
    assert ranked.json()["evaluation_as_of_date"] == "2020-02-29"
    assert ranked.json()["evaluation_date_defaulted"] is False
