"""Slice 10 CLI behavior; synthetic data only, no AI provider."""

import uuid

import pytest
from search_helpers import seed_candidate_with_profile
from sqlalchemy.ext.asyncio import AsyncSession

from meyar import cli
from meyar.services.job_criteria_repo import create_criteria_version
from meyar.services.job_repo import create_job

EVIDENCE = [{"page": 1, "block_index": 0, "quote": "Synthetic Python evidence"}]
PROFILE = {
    "skills": [{"name": "Python", "evidence": EVIDENCE}],
    "employment_history": [],
    "education": [],
    "certifications": [],
    "languages": [],
    "projects": [],
}


class _SessionCtx:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def __aenter__(self) -> AsyncSession:
        return self.session

    async def __aexit__(self, *args) -> None:
        return None


def _patch_session(monkeypatch, db_session: AsyncSession) -> None:
    monkeypatch.setattr(cli, "get_session_factory", lambda: (lambda: _SessionCtx(db_session)))


async def _setup(db_session: AsyncSession, tenant_id):
    candidate, profile = await seed_candidate_with_profile(
        db_session, tenant_id=tenant_id, profile_content=PROFILE
    )
    job = await create_job(db_session, tenant_id=tenant_id, title="CLI Job")
    criteria = await create_criteria_version(
        db_session,
        tenant_id=tenant_id,
        job_id=job.id,
        criteria=[
            {
                "id": "python",
                "kind": "SKILL",
                "type": "MUST_HAVE",
                "label": "Python",
                "value": "Python",
                "weight": 1,
            }
        ],
        created_by_api_key_id=None,
    )
    await db_session.commit()
    return candidate, profile, job, criteria


async def test_evaluate_cli_requires_date_and_prints_safe_score_provenance(
    db_session: AsyncSession, tenant_and_key, monkeypatch, capsys
) -> None:
    tenant, _key, _plaintext = tenant_and_key
    candidate, profile, _job, criteria = await _setup(db_session, tenant.id)
    _patch_session(monkeypatch, db_session)

    await cli._evaluate(str(tenant.id), str(candidate.id), str(criteria.job_id), "2026-01-01")
    output = capsys.readouterr().out
    assert "Score: 100.00" in output
    assert "Fit band: STRONG_MATCH" in output
    assert "Evaluation policy: meyar-policy-v3" in output
    assert "Scoring policy: meyar-score-v1" in output
    assert "As-of date: 2026-01-01" in output
    assert str(profile.id) in output
    assert "Synthetic Python evidence" not in output


async def test_rank_job_cli_prints_uuid_rank_without_identity(
    db_session: AsyncSession, tenant_and_key, monkeypatch, capsys
) -> None:
    tenant, _key, _plaintext = tenant_and_key
    candidate, _profile, _job, criteria = await _setup(db_session, tenant.id)
    _patch_session(monkeypatch, db_session)

    await cli._rank_job(str(tenant.id), str(criteria.id), "2026-01-01")
    output = capsys.readouterr().out
    assert "Evaluated: 1" in output
    assert "Scoring policy: meyar-score-v1" in output
    assert f"#1 candidate={candidate.id}" in output
    assert "name" not in output.lower()
    assert "email" not in output.lower()
    assert "phone" not in output.lower()


async def test_score_and_rank_cli_invalid_input_exits_two(capsys) -> None:
    with pytest.raises(SystemExit) as evaluate_exit:
        await cli._evaluate("bad", "bad", "bad", "not-a-date")
    assert evaluate_exit.value.code == 2

    with pytest.raises(SystemExit) as rank_exit:
        await cli._rank_job("bad", "bad", "not-a-date")
    assert rank_exit.value.code == 2
    assert "YYYY-MM-DD" in capsys.readouterr().out


async def test_evaluate_cli_zero_total_exits_three(
    db_session: AsyncSession, tenant_and_key, monkeypatch
) -> None:
    tenant, _key, _plaintext = tenant_and_key
    candidate, _profile = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=PROFILE
    )
    job = await create_job(db_session, tenant_id=tenant.id, title="Legacy zero")
    await create_criteria_version(
        db_session,
        tenant_id=tenant.id,
        job_id=job.id,
        criteria=[
            {
                "id": "zero",
                "kind": "SKILL",
                "type": "MUST_HAVE",
                "label": "Python",
                "value": "Python",
                "weight": 0,
            }
        ],
        created_by_api_key_id=None,
    )
    await db_session.commit()
    _patch_session(monkeypatch, db_session)

    with pytest.raises(SystemExit) as exc_info:
        await cli._evaluate(str(tenant.id), str(candidate.id), str(job.id), "2026-01-01")
    assert exc_info.value.code == 3


async def test_rank_cli_infrastructure_failure_exits_four(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "get_session_factory",
        lambda: (_ for _ in ()).throw(RuntimeError("synthetic infrastructure failure")),
    )
    with pytest.raises(SystemExit) as exc_info:
        await cli._rank_job(str(uuid.uuid4()), str(uuid.uuid4()), "2026-01-01")
    assert exc_info.value.code == 4
