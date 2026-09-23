"""Slice 9 CLI behavior. Synthetic data only; no live Ollama."""

import uuid

import pytest
from fakes import FakeEmbeddingProvider, FakeLLMProvider
from search_helpers import seed_candidate_with_profile, synthetic_evidence
from sqlalchemy.ext.asyncio import AsyncSession

from meyar import cli
from meyar.embedding.provider import EmbeddingUnavailableError
from meyar.llm.provider import ModelUnavailableError
from meyar.search.planner_schemas import PlannerDraft
from meyar.search.schemas import EmbeddingSearchConfig, RequiredFilters
from meyar.services.tenant_repo import create_tenant

_EVIDENCE = [{"page": 1, "block_index": 0, "quote": "synthetic evidence"}]


class _SessionCtx:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def __aenter__(self) -> AsyncSession:
        return self.session

    async def __aexit__(self, *args) -> None:
        return None


def _config() -> EmbeddingSearchConfig:
    return EmbeddingSearchConfig(
        provider="fake-embedding",
        model_name="fake-embedding-model-v1",
        model_revision="",
        serializer_version="candidate-professional-embedding-text-v1",
        embedding_dimensions=8,
    )


def _profile(skills: list[str]) -> dict:
    return {
        "skills": [
            {"name": skill, "category": None, "evidence": synthetic_evidence(skill)}
            for skill in skills
        ],
        "employment_history": [],
        "education": [],
        "certifications": [],
        "languages": [],
        "projects": [],
    }


async def _tenant(db_session: AsyncSession):
    tenant = await create_tenant(
        db_session, name=f"PlannerCLI-{uuid.uuid4().hex[:8]}"
    )
    await db_session.commit()
    return tenant


def _patch_common(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    llm: FakeLLMProvider,
) -> None:
    monkeypatch.setattr(cli, "get_session_factory", lambda: (lambda: _SessionCtx(db_session)))
    monkeypatch.setattr(cli, "get_llm_provider", lambda: llm)
    monkeypatch.setattr(cli, "get_embedding_search_config", _config)


async def test_plan_only_cli_success_is_safe(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tenant = await _tenant(db_session)
    query = "Java bilən namizədləri göstər."
    _patch_common(
        monkeypatch,
        db_session,
        FakeLLMProvider(
            planner_draft=PlannerDraft(
                required_filters=RequiredFilters(skills=["Java"])
            )
        ),
    )
    monkeypatch.setattr(
        cli,
        "get_embedding_provider",
        lambda: (_ for _ in ()).throw(AssertionError("plan-only must not construct provider")),
    )

    await cli._plan_search(str(tenant.id), query, "2026-08-23", execute=False)
    output = capsys.readouterr().out
    assert "Executable: True" in output
    assert "Mode: STRUCTURED_ONLY" in output
    assert "Planner policy: meyar-search-planner-v1" in output
    assert query not in output


async def test_plan_cli_skill_duration_is_executable_without_model(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tenant = await _tenant(db_session)
    llm = FakeLLMProvider()
    _patch_common(monkeypatch, db_session, llm)
    await cli._plan_search(
        str(tenant.id),
        "Java üzrə ən azı 5 il təcrübəsi olan namizədləri göstər",
        "2026-08-23",
        execute=False,
    )
    output = capsys.readouterr().out
    assert "Executable: True" in output
    assert "'value': 'Java'" in output
    assert "'min_years': 5.0" in output
    assert llm.call_count == 0


async def test_plan_cli_provider_failure_exit_3(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await _tenant(db_session)
    _patch_common(
        monkeypatch,
        db_session,
        FakeLLMProvider(error=ModelUnavailableError("local model down")),
    )
    with pytest.raises(SystemExit) as exc_info:
        await cli._plan_search(
            str(tenant.id), "Java candidates", "2026-08-23", execute=False
        )
    assert exc_info.value.code == 3


async def test_execute_cli_reuses_slice8_safe_output_and_does_not_embed_structured(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tenant = await _tenant(db_session)
    candidate, _ = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(["Java"])
    )
    await db_session.commit()
    _patch_common(
        monkeypatch,
        db_session,
        FakeLLMProvider(
            planner_draft=PlannerDraft(
                required_filters=RequiredFilters(skills=["Java"])
            )
        ),
    )
    embedding = FakeEmbeddingProvider(error=EmbeddingUnavailableError("must not run"))
    monkeypatch.setattr(cli, "get_embedding_provider", lambda: embedding)

    await cli._plan_search(
        str(tenant.id), "Java bilən namizədləri göstər.", "2026-08-23", execute=True
    )
    output = capsys.readouterr().out
    assert f"candidate={candidate.id}" in output
    assert "Results (1)" in output
    assert embedding.call_count == 0
