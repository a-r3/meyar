from httpx import AsyncClient


def _auth(plaintext: str) -> dict:
    return {"Authorization": f"Bearer {plaintext}"}


def _valid_criteria() -> list[dict]:
    return [
        {
            "id": "python_exp",
            "kind": "SKILL",
            "type": "MUST_HAVE",
            "label": "Python backend development",
            "value": "Python",
            "weight": 2.0,
            "evidence_required": True,
        },
        {
            "id": "min_experience",
            "kind": "EXPERIENCE",
            "type": "MUST_HAVE",
            "label": "Minimum years of professional experience",
            "min_years": 3,
        },
    ]


async def test_create_job_with_criteria_v1(client: AsyncClient, tenant_and_key) -> None:
    _tenant, _key, plaintext = tenant_and_key
    resp = await client.post(
        "/api/v1/jobs",
        headers=_auth(plaintext),
        json={"title": "Backend Engineer", "criteria": _valid_criteria()},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["title"] == "Backend Engineer"
    assert body["current_criteria_version"]["version_number"] == 1
    assert len(body["current_criteria_version"]["criteria"]) == 2


async def test_job_requires_auth(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/jobs", json={"title": "X", "criteria": _valid_criteria()}
    )
    assert resp.status_code == 401


async def test_job_requires_at_least_one_criterion(
    client: AsyncClient, tenant_and_key
) -> None:
    _tenant, _key, plaintext = tenant_and_key
    resp = await client.post(
        "/api/v1/jobs", headers=_auth(plaintext), json={"title": "X", "criteria": []}
    )
    assert resp.status_code == 422


async def test_duplicate_criterion_ids_rejected(client: AsyncClient, tenant_and_key) -> None:
    _tenant, _key, plaintext = tenant_and_key
    criteria = _valid_criteria()
    criteria.append(dict(criteria[0]))  # duplicate id "python_exp"
    resp = await client.post(
        "/api/v1/jobs", headers=_auth(plaintext), json={"title": "X", "criteria": criteria}
    )
    assert resp.status_code == 422


async def test_prohibited_sensitive_criterion_rejected(
    client: AsyncClient, tenant_and_key
) -> None:
    _tenant, _key, plaintext = tenant_and_key
    criteria = [
        {
            "id": "gender_pref",
            "kind": "SKILL",
            "type": "PREFERRED",
            "label": "Prefers female candidates",
            "value": "female",
        }
    ]
    resp = await client.post(
        "/api/v1/jobs", headers=_auth(plaintext), json={"title": "X", "criteria": criteria}
    )
    assert resp.status_code == 422


async def test_experience_criterion_requires_min_years(
    client: AsyncClient, tenant_and_key
) -> None:
    _tenant, _key, plaintext = tenant_and_key
    criteria = [
        {
            "id": "exp",
            "kind": "EXPERIENCE",
            "type": "MUST_HAVE",
            "label": "Years of experience",
        }
    ]
    resp = await client.post(
        "/api/v1/jobs", headers=_auth(plaintext), json={"title": "X", "criteria": criteria}
    )
    assert resp.status_code == 422


async def test_criteria_versioning_never_mutates_prior_version(
    client: AsyncClient, tenant_and_key
) -> None:
    _tenant, _key, plaintext = tenant_and_key
    create_resp = await client.post(
        "/api/v1/jobs",
        headers=_auth(plaintext),
        json={"title": "Backend Engineer", "criteria": _valid_criteria()},
    )
    job_id = create_resp.json()["id"]

    v2_criteria = _valid_criteria()
    v2_criteria.append(
        {
            "id": "aws_cert",
            "kind": "CERTIFICATION",
            "type": "PREFERRED",
            "label": "AWS certification",
            "value": "AWS Certified Developer",
        }
    )
    v2_resp = await client.post(
        f"/api/v1/jobs/{job_id}/criteria",
        headers=_auth(plaintext),
        json={"criteria": v2_criteria},
    )
    assert v2_resp.status_code == 201
    assert v2_resp.json()["version_number"] == 2
    assert len(v2_resp.json()["criteria"]) == 3

    # current now resolves to v2
    current_resp = await client.get(f"/api/v1/jobs/{job_id}", headers=_auth(plaintext))
    assert current_resp.json()["current_criteria_version"]["version_number"] == 2

    # v1 is still retrievable, unmutated
    v1_resp = await client.get(
        f"/api/v1/jobs/{job_id}/criteria/1", headers=_auth(plaintext)
    )
    assert v1_resp.status_code == 200
    assert v1_resp.json()["version_number"] == 1
    assert len(v1_resp.json()["criteria"]) == 2

    # full history is listable
    list_resp = await client.get(f"/api/v1/jobs/{job_id}/criteria", headers=_auth(plaintext))
    assert [v["version_number"] for v in list_resp.json()] == [1, 2]


async def test_unknown_criteria_version_is_404(client: AsyncClient, tenant_and_key) -> None:
    _tenant, _key, plaintext = tenant_and_key
    create_resp = await client.post(
        "/api/v1/jobs",
        headers=_auth(plaintext),
        json={"title": "Backend Engineer", "criteria": _valid_criteria()},
    )
    job_id = create_resp.json()["id"]
    resp = await client.get(f"/api/v1/jobs/{job_id}/criteria/99", headers=_auth(plaintext))
    assert resp.status_code == 404


async def test_unknown_job_is_404(client: AsyncClient, tenant_and_key) -> None:
    _tenant, _key, plaintext = tenant_and_key
    resp = await client.get(
        "/api/v1/jobs/00000000-0000-0000-0000-000000000000", headers=_auth(plaintext)
    )
    assert resp.status_code == 404
