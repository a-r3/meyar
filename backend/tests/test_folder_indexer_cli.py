"""CLI surface for Slice 6 (`meyar index-folder`). Routes the CLI's
dependency lookups to the test database session/storage instead of the
real global engine, same spirit as the `client` fixture's
dependency_overrides for the FastAPI app."""

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meyar import cli
from meyar.ingestion.parsers.local_text_parser import LocalTextParser
from meyar.services.tenant_repo import create_tenant
from meyar.storage.local import LocalFilesystemStorage

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "synthetic_cvs"


class _FakeSettings:
    max_upload_bytes = 10 * 1024 * 1024


class _SessionCtx:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _patch_cli_dependencies(monkeypatch, db_session: AsyncSession, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(cli, "get_session_factory", lambda: (lambda: _SessionCtx(db_session)))
    monkeypatch.setattr(
        cli, "get_document_storage", lambda: LocalFilesystemStorage(root=str(tmp_path / "storage"))
    )
    monkeypatch.setattr(cli, "get_document_parser", lambda: LocalTextParser())


async def test_cli_index_folder_happy_path(
    db_session: AsyncSession, tmp_path: Path, monkeypatch, capsys
) -> None:
    tenant = await create_tenant(db_session, name="T-cli")
    await db_session.commit()
    root = tmp_path / "cvs"
    root.mkdir()
    (root / "candidate.pdf").write_bytes((FIXTURES_DIR / "valid_cv.pdf").read_bytes())

    _patch_cli_dependencies(monkeypatch, db_session, tmp_path)

    await cli._index_folder(str(tenant.id), str(root))

    captured = capsys.readouterr()
    assert "Discovered: 1" in captured.out
    assert "Successful: 1" in captured.out
    assert "Failed: 0" in captured.out
    # PII-safety: no candidate/document identity leaks into CLI output.
    assert "candidate.pdf" not in captured.out


async def test_cli_index_folder_invalid_root_exits_2(
    db_session: AsyncSession, tmp_path: Path, monkeypatch, capsys
) -> None:
    tenant = await create_tenant(db_session, name="T-cli-invalid")
    await db_session.commit()
    _patch_cli_dependencies(monkeypatch, db_session, tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        await cli._index_folder(str(tenant.id), str(tmp_path / "does-not-exist"))

    assert exc_info.value.code == 2


async def test_cli_index_folder_with_failures_exits_1(
    db_session: AsyncSession, tmp_path: Path, monkeypatch, capsys
) -> None:
    tenant = await create_tenant(db_session, name="T-cli-fail")
    await db_session.commit()
    root = tmp_path / "cvs"
    root.mkdir()
    (root / "broken.docx").write_bytes((FIXTURES_DIR / "malformed.docx").read_bytes())
    _patch_cli_dependencies(monkeypatch, db_session, tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        await cli._index_folder(str(tenant.id), str(root))

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Failed: 1" in captured.out
    assert "broken.docx" not in captured.out
