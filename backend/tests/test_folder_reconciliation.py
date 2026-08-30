"""Slice 14 — automatic downstream (profile/identity/embedding) processing
for folder-imported CVs. All CV bytes come from `fixtures/synthetic_cvs/`
— never real candidate data. Never depends on a live Ollama model — see
tests/fakes.py and .claude/rules/testing.md."""

from pathlib import Path

from fakes import FakeEmbeddingProvider, FakeLLMProvider
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.embedding.provider import EmbeddingProviderError
from meyar.ingestion.parsers.local_text_parser import LocalTextParser
from meyar.llm.provider import ModelUnavailableError
from meyar.models.audit_event import AuditEvent
from meyar.models.candidate_embedding_version import CandidateEmbeddingVersion
from meyar.models.candidate_identity_version import CandidateIdentityVersion
from meyar.models.candidate_profile_version import CandidateProfileVersion
from meyar.schemas.candidate_identity import CandidateIdentityExtraction
from meyar.schemas.candidate_profile import CandidateProfileExtraction, EvidenceRef, SkillItem
from meyar.search.schemas import CandidateSearchRequest, RequiredFilters, SearchMode
from meyar.search.service import search_candidates
from meyar.services.folder_indexed_file_repo import list_folder_indexed_files
from meyar.services.folder_indexer_service import index_folder
from meyar.services.folder_reconciliation_service import (
    process_pending_candidates,
    reconcile_folder,
)
from meyar.services.tenant_repo import create_tenant
from meyar.storage.local import LocalFilesystemStorage

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "synthetic_cvs"
MAX_BYTES = 10 * 1024 * 1024
MAX_INPUT_CHARS = 20000


def _parser() -> LocalTextParser:
    return LocalTextParser()


def _storage(tmp_path: Path) -> LocalFilesystemStorage:
    return LocalFilesystemStorage(root=str(tmp_path / "storage"))


def _copy_fixture(fixture_name: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes((FIXTURES_DIR / fixture_name).read_bytes())


def _profile_extraction(block_index: int = 0) -> CandidateProfileExtraction:
    """Evidence must verify against the real parsed text of whichever
    fixture is used — "Skills: Python, SQL, Docker" is block_index=0 in
    valid_cv.pdf (matching test_candidate_profile_extraction.py) and
    block_index=3 in valid_cv.docx."""
    return CandidateProfileExtraction(
        skills=[
            SkillItem(
                name="Python",
                evidence=[
                    EvidenceRef(
                        page=1, block_index=block_index, quote="Skills: Python, SQL, Docker"
                    )
                ],
            )
        ],
    )


def _identity_extraction() -> CandidateIdentityExtraction:
    return CandidateIdentityExtraction(
        full_name=None, email=None, phone=None
    )


async def _process(
    db_session: AsyncSession,
    tmp_path: Path,
    tenant_id,
    folder_source_id,
    *,
    llm: FakeLLMProvider | None = None,
    embedder: FakeEmbeddingProvider | None = None,
    limit: int | None = None,
):
    default_llm = FakeLLMProvider(
        extraction=_profile_extraction(), identity_extraction=_identity_extraction()
    )
    return await process_pending_candidates(
        db_session,
        llm or default_llm,
        embedder or FakeEmbeddingProvider(),
        tenant_id=tenant_id,
        folder_source_id=folder_source_id,
        model_provider_name="fake",
        max_profile_input_chars=MAX_INPUT_CHARS,
        max_identity_input_chars=MAX_INPUT_CHARS,
        max_embedding_input_chars=MAX_INPUT_CHARS,
        limit=limit,
    )


async def test_fresh_pdf_reconciled_produces_profile(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    tenant = await create_tenant(db_session, name="T-recon-pdf")
    await db_session.commit()
    root = tmp_path / "cvs"
    _copy_fixture("valid_cv.pdf", root / "candidate.pdf")

    scan = await index_folder(
        db_session, _storage(tmp_path), _parser(),
        tenant_id=tenant.id, root_path=str(root), max_bytes=MAX_BYTES,
    )
    await db_session.commit()

    summary = await _process(db_session, tmp_path, tenant.id, scan.folder_source_id)

    assert summary.candidates_considered == 1
    assert summary.already_ready == 0
    assert summary.processed == 1
    assert summary.ready_after == 1
    assert summary.failed == 0

    result = await db_session.execute(
        select(CandidateProfileVersion).where(CandidateProfileVersion.tenant_id == tenant.id)
    )
    profiles = result.scalars().all()
    assert len(profiles) == 1
    assert profiles[0].status == "COMPLETED"


async def test_fresh_docx_reconciled_produces_profile(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    tenant = await create_tenant(db_session, name="T-recon-docx")
    await db_session.commit()
    root = tmp_path / "cvs"
    _copy_fixture("valid_cv.docx", root / "candidate.docx")

    scan = await index_folder(
        db_session, _storage(tmp_path), _parser(),
        tenant_id=tenant.id, root_path=str(root), max_bytes=MAX_BYTES,
    )
    await db_session.commit()

    llm = FakeLLMProvider(
        extraction=_profile_extraction(block_index=3), identity_extraction=_identity_extraction()
    )
    summary = await _process(db_session, tmp_path, tenant.id, scan.folder_source_id, llm=llm)
    assert summary.ready_after == 1


async def test_identity_version_created(db_session: AsyncSession, tmp_path: Path) -> None:
    tenant = await create_tenant(db_session, name="T-recon-identity")
    await db_session.commit()
    root = tmp_path / "cvs"
    _copy_fixture("valid_cv.pdf", root / "candidate.pdf")

    scan = await index_folder(
        db_session, _storage(tmp_path), _parser(),
        tenant_id=tenant.id, root_path=str(root), max_bytes=MAX_BYTES,
    )
    await db_session.commit()
    await _process(db_session, tmp_path, tenant.id, scan.folder_source_id)

    result = await db_session.execute(
        select(CandidateIdentityVersion).where(CandidateIdentityVersion.tenant_id == tenant.id)
    )
    identities = result.scalars().all()
    assert len(identities) == 1
    assert identities[0].status == "COMPLETED"


async def test_embedding_version_created(db_session: AsyncSession, tmp_path: Path) -> None:
    tenant = await create_tenant(db_session, name="T-recon-embed")
    await db_session.commit()
    root = tmp_path / "cvs"
    _copy_fixture("valid_cv.pdf", root / "candidate.pdf")

    scan = await index_folder(
        db_session, _storage(tmp_path), _parser(),
        tenant_id=tenant.id, root_path=str(root), max_bytes=MAX_BYTES,
    )
    await db_session.commit()
    await _process(db_session, tmp_path, tenant.id, scan.folder_source_id)

    result = await db_session.execute(
        select(CandidateEmbeddingVersion).where(CandidateEmbeddingVersion.tenant_id == tenant.id)
    )
    embeddings = result.scalars().all()
    assert len(embeddings) == 1


async def test_resulting_candidate_is_searchable(db_session: AsyncSession, tmp_path: Path) -> None:
    """Full synthetic E2E: folder -> reconcile -> extraction -> embedding
    -> search result. Proves the product claim without any manual
    per-candidate extract/embed command."""
    tenant = await create_tenant(db_session, name="T-recon-search")
    await db_session.commit()
    root = tmp_path / "cvs"
    _copy_fixture("valid_cv.pdf", root / "candidate.pdf")

    scan = await index_folder(
        db_session, _storage(tmp_path), _parser(),
        tenant_id=tenant.id, root_path=str(root), max_bytes=MAX_BYTES,
    )
    await db_session.commit()
    await _process(db_session, tmp_path, tenant.id, scan.folder_source_id)
    await db_session.commit()

    rows = await list_folder_indexed_files(
        db_session, tenant_id=tenant.id, folder_source_id=scan.folder_source_id
    )
    candidate_id = rows[0].candidate_id
    assert candidate_id is not None

    request = CandidateSearchRequest(
        mode=SearchMode.STRUCTURED_ONLY,
        required_filters=RequiredFilters(skills=["Python"]),
    )
    result = await search_candidates(db_session, tenant_id=tenant.id, request=request)
    result_candidate_ids = {r.candidate_id for r in result.results}
    assert candidate_id in result_candidate_ids


async def test_unchanged_reconciliation_no_duplicate_candidate(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    tenant = await create_tenant(db_session, name="T-recon-idempotent")
    await db_session.commit()
    root = tmp_path / "cvs"
    _copy_fixture("valid_cv.pdf", root / "candidate.pdf")
    storage = _storage(tmp_path)
    parser = _parser()

    scan = await index_folder(
        db_session, storage, parser,
        tenant_id=tenant.id, root_path=str(root), max_bytes=MAX_BYTES,
    )
    await db_session.commit()
    first = await _process(db_session, tmp_path, tenant.id, scan.folder_source_id)
    assert first.ready_after == 1

    # Re-scan (no changes) then reconcile again.
    scan2 = await index_folder(
        db_session, storage, parser,
        tenant_id=tenant.id, root_path=str(root), max_bytes=MAX_BYTES,
    )
    await db_session.commit()
    second = await _process(db_session, tmp_path, tenant.id, scan2.folder_source_id)

    assert second.already_ready == 1
    assert second.processed == 0

    from meyar.models.candidate import Candidate

    candidates = await db_session.execute(
        select(Candidate).where(Candidate.tenant_id == tenant.id)
    )
    assert len(candidates.scalars().all()) == 1


async def test_unchanged_reconciliation_no_duplicate_downstream_versions(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    tenant = await create_tenant(db_session, name="T-recon-no-dup-versions")
    await db_session.commit()
    root = tmp_path / "cvs"
    _copy_fixture("valid_cv.pdf", root / "candidate.pdf")
    storage = _storage(tmp_path)
    parser = _parser()

    scan = await index_folder(
        db_session, storage, parser,
        tenant_id=tenant.id, root_path=str(root), max_bytes=MAX_BYTES,
    )
    await db_session.commit()
    await _process(db_session, tmp_path, tenant.id, scan.folder_source_id)

    scan2 = await index_folder(
        db_session, storage, parser,
        tenant_id=tenant.id, root_path=str(root), max_bytes=MAX_BYTES,
    )
    await db_session.commit()
    await _process(db_session, tmp_path, tenant.id, scan2.folder_source_id)

    for model in (CandidateProfileVersion, CandidateIdentityVersion, CandidateEmbeddingVersion):
        result = await db_session.execute(select(model).where(model.tenant_id == tenant.id))
        assert len(result.scalars().all()) == 1, f"{model.__name__} was duplicated"


async def test_downstream_extraction_failure_does_not_abort_next_candidate(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    tenant = await create_tenant(db_session, name="T-recon-isolation")
    await db_session.commit()
    root = tmp_path / "cvs"
    _copy_fixture("valid_cv.pdf", root / "a.pdf")
    _copy_fixture("prompt_injection_cv.pdf", root / "b.pdf")

    scan = await index_folder(
        db_session, _storage(tmp_path), _parser(),
        tenant_id=tenant.id, root_path=str(root), max_bytes=MAX_BYTES,
    )
    await db_session.commit()

    # Fails every extraction attempt for both candidates (permanent-style
    # failure), proving one candidate's failure never blocks another's
    # independent processing.
    failing_llm = FakeLLMProvider(error=ModelUnavailableError("simulated model outage"))
    summary = await _process(
        db_session, tmp_path, tenant.id, scan.folder_source_id, llm=failing_llm
    )

    assert summary.candidates_considered == 2
    assert summary.processed == 2
    assert summary.failed == 2
    assert summary.ready_after == 0


async def test_downstream_embedding_failure_retries_safely_later(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    tenant = await create_tenant(db_session, name="T-recon-embed-retry")
    await db_session.commit()
    root = tmp_path / "cvs"
    _copy_fixture("valid_cv.pdf", root / "candidate.pdf")
    storage = _storage(tmp_path)
    parser = _parser()

    scan = await index_folder(
        db_session, storage, parser,
        tenant_id=tenant.id, root_path=str(root), max_bytes=MAX_BYTES,
    )
    await db_session.commit()

    failing_embedder = FakeEmbeddingProvider(
        error=EmbeddingProviderError("simulated provider outage")
    )
    first = await _process(
        db_session, tmp_path, tenant.id, scan.folder_source_id, embedder=failing_embedder
    )
    assert first.failed == 1
    assert first.ready_after == 0

    # Profile/identity are already COMPLETED — a retry only needs to
    # succeed at the embedding stage this time.
    working_embedder = FakeEmbeddingProvider()
    second = await _process(
        db_session, tmp_path, tenant.id, scan.folder_source_id, embedder=working_embedder
    )
    assert second.ready_after == 1
    assert working_embedder.call_count == 1

    result = await db_session.execute(
        select(CandidateProfileVersion).where(CandidateProfileVersion.tenant_id == tenant.id)
    )
    # No duplicate profile extraction happened during the retry.
    assert len(result.scalars().all()) == 1


async def test_limit_bounds_downstream_processing(db_session: AsyncSession, tmp_path: Path) -> None:
    tenant = await create_tenant(db_session, name="T-recon-limit")
    await db_session.commit()
    root = tmp_path / "cvs"
    _copy_fixture("valid_cv.pdf", root / "a.pdf")
    _copy_fixture("prompt_injection_cv.pdf", root / "b.pdf")

    scan = await index_folder(
        db_session, _storage(tmp_path), _parser(),
        tenant_id=tenant.id, root_path=str(root), max_bytes=MAX_BYTES,
    )
    await db_session.commit()

    summary = await _process(db_session, tmp_path, tenant.id, scan.folder_source_id, limit=1)

    assert summary.candidates_considered == 2
    assert summary.processed == 1
    assert summary.skipped_due_to_limit == 1


async def test_reconciliation_audit_output_has_no_pii(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    tenant = await create_tenant(db_session, name="T-recon-audit")
    await db_session.commit()
    root = tmp_path / "cvs"
    _copy_fixture("valid_cv.pdf", root / "candidate.pdf")

    scan = await index_folder(
        db_session, _storage(tmp_path), _parser(),
        tenant_id=tenant.id, root_path=str(root), max_bytes=MAX_BYTES,
    )
    await db_session.commit()
    await _process(db_session, tmp_path, tenant.id, scan.folder_source_id)

    result = await db_session.execute(select(AuditEvent).where(AuditEvent.tenant_id == tenant.id))
    for event in result.scalars().all():
        for value in event.event_metadata.values():
            assert "candidate.pdf" not in str(value)
            assert str(root) not in str(value)


async def test_reconcile_folder_full_flow(db_session: AsyncSession, tmp_path: Path) -> None:
    """The single CLI-facing entry point: discovery+ingestion+processing
    in one call, safe to re-run."""
    tenant = await create_tenant(db_session, name="T-recon-full")
    await db_session.commit()
    root = tmp_path / "cvs"
    _copy_fixture("valid_cv.pdf", root / "candidate.pdf")
    storage = _storage(tmp_path)
    parser = _parser()
    llm = FakeLLMProvider(
        extraction=_profile_extraction(), identity_extraction=_identity_extraction()
    )
    embedder = FakeEmbeddingProvider()

    scan_summary, recon_summary = await reconcile_folder(
        db_session, storage, parser, llm, embedder,
        tenant_id=tenant.id,
        root_path=str(root),
        max_bytes=MAX_BYTES,
        stability_window_seconds=0,
        model_provider_name="fake",
        max_profile_input_chars=MAX_INPUT_CHARS,
        max_identity_input_chars=MAX_INPUT_CHARS,
        max_embedding_input_chars=MAX_INPUT_CHARS,
        limit=None,
    )

    assert scan_summary.successful == 1
    assert recon_summary.ready_after == 1

    # Re-running is a safe no-op — restart/repeat-invocation semantics.
    scan_summary_2, recon_summary_2 = await reconcile_folder(
        db_session, storage, parser, llm, embedder,
        tenant_id=tenant.id,
        root_path=str(root),
        max_bytes=MAX_BYTES,
        stability_window_seconds=0,
        model_provider_name="fake",
        max_profile_input_chars=MAX_INPUT_CHARS,
        max_identity_input_chars=MAX_INPUT_CHARS,
        max_embedding_input_chars=MAX_INPUT_CHARS,
        limit=None,
    )
    assert scan_summary_2.unchanged == 1
    assert recon_summary_2.already_ready == 1
    assert recon_summary_2.processed == 0
