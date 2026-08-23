"""CLI surface for Slice 7 (`meyar extract-identity`, `meyar
embed-candidate`). Routes the CLI's dependency lookups to test doubles,
same spirit as tests/test_folder_indexer_cli.py."""

import uuid

import pytest
from fakes import FakeEmbeddingProvider, FakeLLMProvider
from sqlalchemy.ext.asyncio import AsyncSession

from meyar import cli
from meyar.schemas.candidate_identity import CandidateIdentityExtraction, IdentityFieldItem
from meyar.schemas.candidate_profile import EvidenceRef
from meyar.services.candidate_document_repo import (
    create_candidate_document,
    create_canonical_document,
)
from meyar.services.candidate_profile_repo import create_profile_version
from meyar.services.candidate_repo import create_candidate
from meyar.services.tenant_repo import create_tenant


class _FakeSettings:
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


def _patch_session(monkeypatch, db_session: AsyncSession) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(cli, "get_session_factory", lambda: (lambda: _SessionCtx(db_session)))


async def _seed_document(db_session: AsyncSession, tenant_id: uuid.UUID):
    candidate = await create_candidate(db_session, tenant_id=tenant_id)
    document = await create_candidate_document(
        db_session,
        tenant_id=tenant_id,
        candidate_id=candidate.id,
        original_filename="synthetic.pdf",
        mime_type="application/pdf",
        byte_size=100,
        sha256_hash="a" * 64,
        storage_key=f"test/{uuid.uuid4().hex}",
    )
    canonical = await create_canonical_document(
        db_session,
        tenant_id=tenant_id,
        candidate_document_id=document.id,
        parser_name="test-parser",
        parser_version="1.0.0",
        language=None,
        content={
            "pages": [
                {
                    "page": 1,
                    "blocks": [{"index": 0, "text": "Jane Synthetic Doe, jane@example.com"}],
                }
            ]
        },
    )
    return candidate, document, canonical


async def test_cli_extract_identity_happy_path_is_pii_safe(
    db_session: AsyncSession, monkeypatch, capsys
) -> None:
    tenant = await create_tenant(db_session, name=f"CliIdTenant-{uuid.uuid4().hex[:8]}")
    await db_session.commit()
    candidate, document, _canonical = await _seed_document(db_session, tenant.id)
    await db_session.commit()

    _patch_session(monkeypatch, db_session)
    identity = CandidateIdentityExtraction(
        full_name=IdentityFieldItem(
            value="Jane Synthetic Doe",
            evidence=[EvidenceRef(page=1, block_index=0, quote="Jane Synthetic Doe")],
        ),
        email=IdentityFieldItem(
            value="jane@example.com",
            evidence=[EvidenceRef(page=1, block_index=0, quote="jane@example.com")],
        ),
    )
    monkeypatch.setattr(
        cli, "get_llm_provider", lambda: FakeLLMProvider(identity_extraction=identity)
    )

    await cli._extract_identity(str(tenant.id), str(candidate.id), str(document.id))

    captured = capsys.readouterr()
    assert "Status: COMPLETED" in captured.out
    assert "Jane Synthetic Doe" not in captured.out
    assert "jane@example.com" not in captured.out


async def test_cli_extract_identity_no_document_reports_safely(
    db_session: AsyncSession, monkeypatch, capsys
) -> None:
    tenant = await create_tenant(db_session, name=f"CliIdTenant-{uuid.uuid4().hex[:8]}")
    await db_session.commit()
    _patch_session(monkeypatch, db_session)
    monkeypatch.setattr(cli, "get_llm_provider", lambda: FakeLLMProvider())

    await cli._extract_identity(str(tenant.id), str(uuid.uuid4()), str(uuid.uuid4()))

    captured = capsys.readouterr()
    assert "not found" in captured.out.lower()


async def test_cli_embed_candidate_happy_path_then_reused(
    db_session: AsyncSession, monkeypatch, capsys
) -> None:
    tenant = await create_tenant(db_session, name=f"CliEmbTenant-{uuid.uuid4().hex[:8]}")
    await db_session.commit()
    candidate, document, canonical = await _seed_document(db_session, tenant.id)
    await create_profile_version(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        candidate_document_id=document.id,
        canonical_document_id=canonical.id,
        source_sha256="a" * 64,
        schema_version="candidate-profile-v1",
        prompt_version="candidate-profile-extraction-v1",
        model_provider="fake",
        model_name="fake-model",
        model_metadata={},
        status="COMPLETED",
        profile_content={
            "skills": [{"name": "Python", "category": None}],
            "employment_history": [],
            "education": [],
            "certifications": [],
            "languages": [],
            "projects": [],
        },
    )
    await db_session.commit()

    _patch_session(monkeypatch, db_session)
    provider = FakeEmbeddingProvider(dimensions=8)
    monkeypatch.setattr(cli, "get_embedding_provider", lambda: provider)

    await cli._embed_candidate(str(tenant.id), str(candidate.id))
    first_out = capsys.readouterr().out
    assert "Reused existing embedding: False" in first_out
    assert "Dimensions: 8" in first_out

    await cli._embed_candidate(str(tenant.id), str(candidate.id))
    second_out = capsys.readouterr().out
    assert "Reused existing embedding: True" in second_out
    assert provider.call_count == 1


async def test_cli_embed_candidate_no_profile_exits_2(
    db_session: AsyncSession, monkeypatch, capsys
) -> None:
    tenant = await create_tenant(db_session, name=f"CliEmbTenant-{uuid.uuid4().hex[:8]}")
    await db_session.commit()
    candidate = await create_candidate(db_session, tenant_id=tenant.id)
    await db_session.commit()

    _patch_session(monkeypatch, db_session)
    monkeypatch.setattr(cli, "get_embedding_provider", lambda: FakeEmbeddingProvider())

    with pytest.raises(SystemExit) as exc_info:
        await cli._embed_candidate(str(tenant.id), str(candidate.id))

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "NO_PROFILE_VERSION" in captured.out
