"""Slice 13 — final synthetic end-to-end MVP acceptance.

One bounded, deterministic scenario across two tenants proving the whole
accepted pipeline holds together, not just each piece in isolation:
multiple candidates with different skills/experience, a MUST_HAVE and a
PREFERRED criterion, a LANGUAGE criterion producing a genuine
insufficient-evidence (`UNKNOWN`, never silently downgraded) case,
structured search, batch ranking with a deterministic repeat, original-CV
retrieval, and strict tenant isolation. No live model dependency — CI must
not depend on nondeterministic LLM output, so profile content is seeded
directly (the same pattern every other Slice 7-10 test uses), and the one
real HTTP upload in this test only exercises the deterministic local
parser, never an LLM call.
"""

import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient
from search_helpers import seed_candidate_with_profile
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.config import Settings, get_settings
from meyar.main import app
from meyar.models.evaluation import Evaluation
from meyar.services.api_key_repo import create_api_key
from meyar.services.tenant_repo import create_tenant

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "synthetic_cvs"


@pytest.fixture
def local_ui_settings() -> Settings:
    settings = Settings(ui_cookie_secure=False)
    app.dependency_overrides[get_settings] = lambda: settings
    return settings

EVIDENCE = [{"page": 1, "block_index": 0, "quote": "Synthetic evidence"}]
EMPTY_PROFILE = {
    "skills": [],
    "employment_history": [],
    "education": [],
    "certifications": [],
    "languages": [],
    "projects": [],
}


def _auth(plaintext: str) -> dict:
    return {"Authorization": f"Bearer {plaintext}"}


def _profile(*, skills: list[str], languages: list[str]) -> dict:
    return {
        **EMPTY_PROFILE,
        "skills": [
            {"name": skill, "category": "Backend", "evidence": EVIDENCE} for skill in skills
        ],
        "employment_history": [
            {
                "title": "Backend Developer",
                "start_date": "2020",
                "end_date": "2025",
                "evidence": EVIDENCE,
            }
        ],
        "languages": [
            {"language": language, "evidence": EVIDENCE} for language in languages
        ],
    }


JOB_CRITERIA = {
    "criteria": [
        {
            "id": "python",
            "kind": "SKILL",
            "type": "MUST_HAVE",
            "label": "Python",
            "value": "Python",
            "weight": 1,
        },
        {
            "id": "aws",
            "kind": "SKILL",
            "type": "PREFERRED",
            "label": "AWS",
            "value": "AWS",
            "weight": 1,
        },
        {
            "id": "english",
            "kind": "LANGUAGE",
            "type": "PREFERRED",
            "label": "English",
            "value": "English",
            "weight": 1,
        },
    ]
}


async def test_synthetic_mvp_scenario_end_to_end(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_key_and_user,
    local_ui_settings: Settings,
) -> None:
    tenant_a, _key_a, plaintext_a, hr_user, hr_password, _membership = tenant_key_and_user
    tenant_b = await create_tenant(db_session, name="E2E-Tenant-B")
    _key_b, plaintext_b = await create_api_key(db_session, tenant_id=tenant_b.id, env="test")
    await db_session.commit()

    # --- Tenant A: three candidates covering MUST_HAVE pass/fail and a
    # genuine insufficient-evidence (UNKNOWN) case. ---
    strong, _ = await seed_candidate_with_profile(
        db_session,
        tenant_id=tenant_a.id,
        profile_content=_profile(skills=["Python", "AWS"], languages=["English"]),
    )
    weak, _ = await seed_candidate_with_profile(
        db_session,
        tenant_id=tenant_a.id,
        profile_content=_profile(skills=["Java"], languages=["English"]),
    )
    insufficient, _ = await seed_candidate_with_profile(
        db_session,
        tenant_id=tenant_a.id,
        profile_content=_profile(skills=["Python"], languages=[]),
    )

    # --- Tenant B: one candidate that must never surface in any Tenant-A
    # response below. ---
    foreign, _ = await seed_candidate_with_profile(
        db_session,
        tenant_id=tenant_b.id,
        profile_content=_profile(skills=["Python", "AWS"], languages=["English"]),
    )
    await db_session.commit()

    # --- Original-CV retrieval: real HTTP upload + UI session, deterministic
    # local parser only, no LLM. ---
    fixture = FIXTURES_DIR / "valid_cv.pdf"
    cv_candidate = await client.post("/api/v1/candidates", headers=_auth(plaintext_a))
    assert cv_candidate.status_code == 201
    cv_candidate_id = cv_candidate.json()["id"]
    upload = await client.post(
        f"/api/v1/candidates/{cv_candidate_id}/documents",
        headers=_auth(plaintext_a),
        files={"file": ("valid_cv.pdf", fixture.read_bytes(), "application/pdf")},
    )
    assert upload.status_code == 201 and upload.json()["parser_status"] == "PARSED"
    document_id = upload.json()["id"]

    login = await client.post(
        "/ui/login",
        data={"username": hr_user.username, "password": hr_password},
        follow_redirects=False,
    )
    assert login.status_code == 303
    original = await client.get(
        f"/ui/candidates/{cv_candidate_id}/documents/{document_id}/original"
    )
    assert original.status_code == 200
    assert original.content == fixture.read_bytes()

    # --- Job + MUST_HAVE/PREFERRED/LANGUAGE criteria. ---
    job = await client.post(
        "/api/v1/jobs",
        headers=_auth(plaintext_a),
        json={"title": "E2E Backend Role", **JOB_CRITERIA},
    )
    assert job.status_code == 201
    job_id = job.json()["id"]
    version_number = job.json()["current_criteria_version"]["version_number"]

    # --- Structured search: only Python-holding Tenant-A candidates, never
    # the foreign Tenant-B candidate (identical skills). ---
    search = await client.post(
        "/api/v1/search",
        headers=_auth(plaintext_a),
        json={"mode": "STRUCTURED_ONLY", "required_filters": {"skills": ["Python"]}},
    )
    assert search.status_code == 200
    result_ids = {r["candidate_id"] for r in search.json()["results"]}
    assert result_ids == {str(strong.id), str(insufficient.id)}
    assert str(foreign.id) not in result_ids
    assert str(weak.id) not in result_ids

    # --- Score every Tenant-A candidate; verify MUST_HAVE fail, and the
    # UNKNOWN (never NOT_MATCHED) insufficient-evidence case. ---
    async def _score(candidate_id: uuid.UUID) -> dict:
        resp = await client.post(
            f"/api/v1/jobs/{job_id}/criteria/{version_number}/score",
            headers=_auth(plaintext_a),
            json={"candidate_id": str(candidate_id), "evaluation_as_of_date": "2026-01-01"},
        )
        assert resp.status_code == 200
        return resp.json()

    strong_score = await _score(strong.id)
    weak_score = await _score(weak.id)
    insufficient_score = await _score(insufficient.id)

    strong_eval = await db_session.get(Evaluation, uuid.UUID(strong_score["evaluation_id"]))
    weak_eval = await db_session.get(Evaluation, uuid.UUID(weak_score["evaluation_id"]))
    insufficient_eval = await db_session.get(
        Evaluation, uuid.UUID(insufficient_score["evaluation_id"])
    )
    assert strong_eval is not None and weak_eval is not None and insufficient_eval is not None

    def _status(evaluation: Evaluation, criterion_id: str) -> str:
        assert evaluation.score_explanation is not None
        entry = next(
            c for c in evaluation.score_explanation["criteria"] if c["criterion_id"] == criterion_id
        )
        return entry["status"]

    # Absence of evidence is never treated as proof of absence: a MUST_HAVE
    # skill with no matching profile entry is UNKNOWN, not a fabricated
    # NOT_MATCHED — the same never-silently-downgraded invariant D-010
    # establishes for every criterion kind.
    assert _status(weak_eval, "python") == "UNKNOWN"
    assert _status(insufficient_eval, "python") == "MATCH"
    assert _status(insufficient_eval, "english") == "UNKNOWN"
    assert float(strong_score["numeric_score"]) > float(weak_score["numeric_score"])
    assert float(strong_score["numeric_score"]) > float(insufficient_score["numeric_score"])

    # --- Batch rank: deterministic order, then an identical repeat proves
    # exact-provenance reuse (reused=true), not recomputation. ---
    rank_body = {"evaluation_as_of_date": "2026-01-01"}
    rank1 = await client.post(
        f"/api/v1/jobs/{job_id}/criteria/{version_number}/rank",
        headers=_auth(plaintext_a),
        json=rank_body,
    )
    assert rank1.status_code == 200
    ranked_ids = [r["candidate_id"] for r in rank1.json()["results"]]
    assert str(foreign.id) not in ranked_ids
    assert ranked_ids.index(str(strong.id)) < ranked_ids.index(str(weak.id))

    rank2 = await client.post(
        f"/api/v1/jobs/{job_id}/criteria/{version_number}/rank",
        headers=_auth(plaintext_a),
        json=rank_body,
    )
    assert rank2.status_code == 200
    assert rank2.json()["reused_count"] == rank2.json()["evaluated_count"]
    assert [r["evaluation_id"] for r in rank1.json()["results"]] == [
        r["evaluation_id"] for r in rank2.json()["results"]
    ]

    # --- Tenant B never sees Tenant A's job/candidates; Tenant A's job is
    # invisible cross-tenant. ---
    foreign_view = await client.get(
        f"/api/v1/jobs/{job_id}", headers=_auth(plaintext_b)
    )
    assert foreign_view.status_code == 404
    foreign_candidate_view = await client.get(
        f"/api/v1/candidates/{strong.id}", headers=_auth(plaintext_b)
    )
    assert foreign_candidate_view.status_code == 404
