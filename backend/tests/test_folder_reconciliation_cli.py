"""CLI surface for Slice 14 (`meyar reconcile-folder`). Routes the CLI's
dependency lookups to the test database session/storage/fake AI providers
instead of the real global engine — same spirit as
test_folder_indexer_cli.py. Never depends on a live Ollama model."""

from pathlib import Path

import pytest
from fakes import FakeEmbeddingProvider, FakeLLMProvider
from sqlalchemy.ext.asyncio import AsyncSession

from meyar import cli
from meyar.ingestion.parsers.local_text_parser import LocalTextParser
from meyar.schemas.candidate_identity import CandidateIdentityExtraction
from meyar.schemas.candidate_profile import CandidateProfileExtraction, EvidenceRef, SkillItem
from meyar.services.tenant_repo import create_tenant
from meyar.storage.local import LocalFilesystemStorage

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "synthetic_cvs"


class _FakeSettings:
    max_upload_bytes = 10 * 1024 * 1024
    folder_stability_seconds = 0
    llm_provider = "fake"
    llm_max_input_chars = 20000
    embedding_max_input_chars = 20000


class _SessionCtx:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _profile_extraction() -> CandidateProfileExtraction:
    return CandidateProfileExtraction(
        skills=[
            SkillItem(
                name="Python",
                evidence=[EvidenceRef(page=1, block_index=0, quote="Skills: Python, SQL, Docker")],
            )
        ],
    )


def _identity_extraction() -> CandidateIdentityExtraction:
    return CandidateIdentityExtraction(full_name=None, email=None, phone=None)


def _patch_cli_dependencies(
    monkeypatch, db_session: AsyncSession, tmp_path: Path, *, llm=None, embedder=None
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(cli, "get_session_factory", lambda: (lambda: _SessionCtx(db_session)))
    monkeypatch.setattr(
        cli, "get_document_storage", lambda: LocalFilesystemStorage(root=str(tmp_path / "storage"))
    )
    monkeypatch.setattr(cli, "get_document_parser", lambda: LocalTextParser())
    default_llm = FakeLLMProvider(
        extraction=_profile_extraction(), identity_extraction=_identity_extraction()
    )
    monkeypatch.setattr(cli, "get_llm_provider", lambda: llm or default_llm)
    monkeypatch.setattr(cli, "get_embedding_provider", lambda: embedder or FakeEmbeddingProvider())


async def test_cli_reconcile_folder_happy_path(
    db_session: AsyncSession, tmp_path: Path, monkeypatch, capsys
) -> None:
    tenant = await create_tenant(db_session, name="T-cli-recon")
    await db_session.commit()
    root = tmp_path / "cvs"
    root.mkdir()
    (root / "candidate.pdf").write_bytes((FIXTURES_DIR / "valid_cv.pdf").read_bytes())

    _patch_cli_dependencies(monkeypatch, db_session, tmp_path)

    await cli._reconcile_folder(str(tenant.id), str(root), None)

    captured = capsys.readouterr()
    assert "Discovered: 1" in captured.out
    assert "Ingestion successful: 1" in captured.out
    assert "Candidates considered: 1" in captured.out
    assert "Ready after this run: 1" in captured.out
    assert "Not fully ready (pending retry): 0" in captured.out
    # PII-safety: no candidate/document identity leaks into CLI output.
    assert "candidate.pdf" not in captured.out


async def test_cli_reconcile_folder_invalid_root_exits_2(
    db_session: AsyncSession, tmp_path: Path, monkeypatch
) -> None:
    tenant = await create_tenant(db_session, name="T-cli-recon-invalid")
    await db_session.commit()
    _patch_cli_dependencies(monkeypatch, db_session, tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        await cli._reconcile_folder(str(tenant.id), str(tmp_path / "does-not-exist"), None)

    assert exc_info.value.code == 2


async def test_cli_reconcile_folder_downstream_failure_exits_1(
    db_session: AsyncSession, tmp_path: Path, monkeypatch, capsys
) -> None:
    from meyar.llm.provider import ModelUnavailableError

    tenant = await create_tenant(db_session, name="T-cli-recon-fail")
    await db_session.commit()
    root = tmp_path / "cvs"
    root.mkdir()
    (root / "candidate.pdf").write_bytes((FIXTURES_DIR / "valid_cv.pdf").read_bytes())

    failing_llm = FakeLLMProvider(error=ModelUnavailableError("simulated outage"))
    _patch_cli_dependencies(monkeypatch, db_session, tmp_path, llm=failing_llm)

    with pytest.raises(SystemExit) as exc_info:
        await cli._reconcile_folder(str(tenant.id), str(root), None)

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Not fully ready (pending retry): 1" in captured.out


async def test_cli_reconcile_folder_limit_flag(
    db_session: AsyncSession, tmp_path: Path, monkeypatch, capsys
) -> None:
    tenant = await create_tenant(db_session, name="T-cli-recon-limit")
    await db_session.commit()
    root = tmp_path / "cvs"
    root.mkdir()
    (root / "a.pdf").write_bytes((FIXTURES_DIR / "valid_cv.pdf").read_bytes())
    (root / "b.pdf").write_bytes((FIXTURES_DIR / "prompt_injection_cv.pdf").read_bytes())

    _patch_cli_dependencies(monkeypatch, db_session, tmp_path)

    await cli._reconcile_folder(str(tenant.id), str(root), 1)

    captured = capsys.readouterr()
    assert "Candidates considered: 2" in captured.out
    assert "Processed this run: 1" in captured.out
    assert "Skipped due to --limit: 1" in captured.out
