"""Slice 14 — automatic downstream (profile/identity/embedding) processing
for folder-imported CVs. All CV bytes come from `fixtures/synthetic_cvs/`
— never real candidate data. Never depends on a live Ollama model — see
tests/fakes.py and .claude/rules/testing.md."""

from pathlib import Path

import pytest
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


def _write_single_paragraph_docx(dest: Path, text: str) -> None:
    """A minimal real DOCX with exactly one non-empty paragraph, which
    LocalTextParser parses to page=1, block_index=0 — lets tests use
    real, distinguishable, non-fabricated document content instead of a
    fixture file, e.g. to prove a changed CV's new content (not the old
    content) is what search reflects."""
    from docx import Document

    dest.parent.mkdir(parents=True, exist_ok=True)
    document = Document()
    document.add_paragraph(text)
    document.save(str(dest))


def _skill_extraction(skill_name: str, *, quote: str, block_index: int = 0) -> (
    CandidateProfileExtraction
):
    """Like _profile_extraction, but with a caller-chosen skill name and
    evidence quote — used where a test needs distinguishable, real
    document content (see _write_single_paragraph_docx)."""
    return CandidateProfileExtraction(
        skills=[
            SkillItem(
                name=skill_name,
                evidence=[EvidenceRef(page=1, block_index=block_index, quote=quote)],
            )
        ],
    )


class _SelectiveFailureLLMProvider:
    """A minimal LLMProvider stub that fails deterministically only for
    documents whose real parsed text contains fail_marker, and succeeds
    for everything else — used to prove --limit fairness: a candidate
    that keeps failing must not permanently starve a candidate that has
    never been attempted. Inspecting the real view text (rather than
    call count) ties the failure to a specific document regardless of
    processing order."""

    provider_name = "fake-selective"

    def __init__(self, *, fail_marker: str, ok_extraction, ok_identity) -> None:
        self._fail_marker = fail_marker
        self._ok_extraction = ok_extraction
        self._ok_identity = ok_identity

    def _should_fail(self, view) -> bool:
        return any(self._fail_marker in block.text for block in view.blocks)

    async def extract_candidate_profile(self, view):
        if self._should_fail(view):
            raise ModelUnavailableError("simulated persistent failure")
        return self._ok_extraction, "fake-model"

    async def extract_candidate_identity(self, view):
        if self._should_fail(view):
            raise ModelUnavailableError("simulated persistent failure")
        return self._ok_identity, "fake-model"


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


@pytest.mark.parametrize("fault", ["document_lookup", "existing_photo_lookup", "unexpected_call"])
async def test_photo_preflight_failure_does_not_stop_folder_professional_pipeline(
    db_session: AsyncSession, tmp_path: Path, monkeypatch, fault: str
) -> None:
    from meyar.services import candidate_photo_service, folder_reconciliation_service

    tenant = await create_tenant(db_session, name=f"T-photo-{fault}")
    await db_session.commit()
    tenant_id = tenant.id
    root = tmp_path / "cvs"
    _copy_fixture("valid_cv.pdf", root / "one.pdf")
    _copy_fixture("valid_cv.pdf", root / "two.pdf")
    (root / "two.pdf").write_bytes((root / "two.pdf").read_bytes() + b"\n")
    original = (
        folder_reconciliation_service.process_photo_for_document
        if fault == "unexpected_call"
        else getattr(candidate_photo_service, "get_candidate_document" if fault == "document_lookup"
                     else "get_photo_for_document")
    )
    attempts = 0

    async def fail_first(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("synthetic photo-only preflight failure")
        return await original(*args, **kwargs)

    if fault == "unexpected_call":
        monkeypatch.setattr(folder_reconciliation_service, "process_photo_for_document", fail_first)
    else:
        monkeypatch.setattr(
            candidate_photo_service,
            "get_candidate_document" if fault == "document_lookup" else "get_photo_for_document",
            fail_first,
        )
    scan, recon = await reconcile_folder(
        db_session, _storage(tmp_path), _parser(),
        FakeLLMProvider(
            extraction=_profile_extraction(), identity_extraction=_identity_extraction()
        ),
        FakeEmbeddingProvider(), tenant_id=tenant_id, root_path=str(root), max_bytes=MAX_BYTES,
        stability_window_seconds=0, model_provider_name="fake",
        max_profile_input_chars=MAX_INPUT_CHARS, max_identity_input_chars=MAX_INPUT_CHARS,
        max_embedding_input_chars=MAX_INPUT_CHARS, limit=None,
    )
    assert scan.successful == 2
    assert recon.ready_after == 2 and recon.failed == 0
    rows = await list_folder_indexed_files(
        db_session, tenant_id=tenant_id, folder_source_id=scan.folder_source_id
    )
    assert len({row.candidate_id for row in rows}) == 2
    for row in rows:
        assert row.candidate_document_id is not None
        assert await db_session.scalar(select(CandidateProfileVersion).where(
            CandidateProfileVersion.candidate_document_id == row.candidate_document_id
        )) is not None
        assert await db_session.scalar(select(CandidateIdentityVersion).where(
            CandidateIdentityVersion.candidate_document_id == row.candidate_document_id
        )) is not None
        assert await db_session.scalar(select(CandidateEmbeddingVersion).where(
            CandidateEmbeddingVersion.candidate_id == row.candidate_id
        )) is not None


async def test_changed_cv_reconciliation_updates_search_no_stale_state(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """Critical scenario (independent-audit P1 follow-up): D1 says Java,
    reconcile, candidate searchable for Java. The SAME relative path is
    then replaced with D2 saying Python, reconciled again — through
    reconcile_folder only, never a manual extract-profile/
    extract-identity/embed-candidate command. Verifies: the same
    Candidate identity/path semantics are preserved; a new
    CandidateDocument version exists for D2; a new CandidateProfileVersion
    and CandidateIdentityVersion exist for D2; the current
    CandidateEmbeddingVersion corresponds to the D2-derived profile; and
    structured search reflects only D2's current content — a Java query
    no longer matches (stale D1 state is not treated as current), a
    Python query does."""
    tenant = await create_tenant(db_session, name="T-changed-cv-e2e")
    await db_session.commit()
    root = tmp_path / "cvs"
    path = root / "candidate.docx"
    _write_single_paragraph_docx(path, "Skills: Java")
    storage = _storage(tmp_path)
    parser = _parser()

    llm_v1 = FakeLLMProvider(
        extraction=_skill_extraction("Java", quote="Skills: Java"),
        identity_extraction=_identity_extraction(),
    )
    scan1, recon1 = await reconcile_folder(
        db_session, storage, parser, llm_v1, FakeEmbeddingProvider(vector=[1.0, 0.0]),
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
    await db_session.commit()
    assert recon1.ready_after == 1

    rows = await list_folder_indexed_files(
        db_session, tenant_id=tenant.id, folder_source_id=scan1.folder_source_id
    )
    candidate_id = rows[0].candidate_id
    d1_document_id = rows[0].candidate_document_id
    assert candidate_id is not None

    java_before = await search_candidates(
        db_session,
        tenant_id=tenant.id,
        request=CandidateSearchRequest(
            mode=SearchMode.STRUCTURED_ONLY, required_filters=RequiredFilters(skills=["Java"])
        ),
    )
    assert candidate_id in {r.candidate_id for r in java_before.results}

    # Same relative path, different bytes — the D1 -> D2 transition.
    _write_single_paragraph_docx(path, "Skills: Python")

    llm_v2 = FakeLLMProvider(
        extraction=_skill_extraction("Python", quote="Skills: Python"),
        identity_extraction=_identity_extraction(),
    )
    scan2, recon2 = await reconcile_folder(
        db_session, storage, parser, llm_v2, FakeEmbeddingProvider(vector=[0.0, 1.0]),
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
    await db_session.commit()

    assert scan2.changed == 1
    assert recon2.ready_after == 1

    rows2 = await list_folder_indexed_files(
        db_session, tenant_id=tenant.id, folder_source_id=scan1.folder_source_id
    )
    assert rows2[0].candidate_id == candidate_id  # same Candidate identity preserved
    d2_document_id = rows2[0].candidate_document_id
    assert d2_document_id is not None
    assert d2_document_id != d1_document_id  # new CandidateDocument version

    from meyar.services.candidate_identity_repo import get_latest_identity_version_for_document
    from meyar.services.candidate_profile_repo import get_latest_profile_version_for_document

    profile_d2 = await get_latest_profile_version_for_document(
        db_session, tenant_id=tenant.id, candidate_document_id=d2_document_id
    )
    assert profile_d2 is not None
    assert profile_d2.status == "COMPLETED"

    identity_d2 = await get_latest_identity_version_for_document(
        db_session, tenant_id=tenant.id, candidate_document_id=d2_document_id
    )
    assert identity_d2 is not None
    assert identity_d2.status == "COMPLETED"

    embeddings = await db_session.execute(
        select(CandidateEmbeddingVersion).where(CandidateEmbeddingVersion.tenant_id == tenant.id)
    )
    assert any(
        e.candidate_profile_version_id == profile_d2.id for e in embeddings.scalars().all()
    )

    python_after = await search_candidates(
        db_session,
        tenant_id=tenant.id,
        request=CandidateSearchRequest(
            mode=SearchMode.STRUCTURED_ONLY, required_filters=RequiredFilters(skills=["Python"])
        ),
    )
    assert candidate_id in {r.candidate_id for r in python_after.results}

    java_after = await search_candidates(
        db_session,
        tenant_id=tenant.id,
        request=CandidateSearchRequest(
            mode=SearchMode.STRUCTURED_ONLY, required_filters=RequiredFilters(skills=["Java"])
        ),
    )
    assert candidate_id not in {r.candidate_id for r in java_after.results}


async def test_limit_fairness_prevents_permanent_starvation(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """Independent-audit P1 follow-up: candidate A fails every downstream
    attempt; candidate B is valid. With --limit equivalent to 1, A sorts
    first alphabetically and consumes the whole budget on the first
    call — but must not consume it forever. On the next call, A already
    has a recorded failed attempt while B has never been attempted, so
    fairness ordering processes B first — B becomes ready without being
    permanently starved by A's repeated failure."""
    tenant = await create_tenant(db_session, name="T-limit-fairness")
    await db_session.commit()
    root = tmp_path / "cvs"
    _write_single_paragraph_docx(root / "a-always-fails.docx", "Skills: TRIGGER_FAILURE_MARKER")
    _write_single_paragraph_docx(root / "b-valid.docx", "Skills: Python")
    storage = _storage(tmp_path)
    parser = _parser()

    llm = _SelectiveFailureLLMProvider(
        fail_marker="TRIGGER_FAILURE_MARKER",
        ok_extraction=_skill_extraction("Python", quote="Skills: Python"),
        ok_identity=_identity_extraction(),
    )
    embedder = FakeEmbeddingProvider()

    scan = await index_folder(
        db_session, storage, parser,
        tenant_id=tenant.id, root_path=str(root), max_bytes=MAX_BYTES,
    )
    await db_session.commit()

    # Run 1: both candidates never attempted -> tie-break by relative_path
    # puts "a-always-fails.docx" first; it consumes the limit=1 budget
    # and fails.
    first = await process_pending_candidates(
        db_session, llm, embedder,
        tenant_id=tenant.id,
        folder_source_id=scan.folder_source_id,
        model_provider_name="fake",
        max_profile_input_chars=MAX_INPUT_CHARS,
        max_identity_input_chars=MAX_INPUT_CHARS,
        max_embedding_input_chars=MAX_INPUT_CHARS,
        limit=1,
    )
    assert first.candidates_considered == 2
    assert first.processed == 1
    assert first.failed == 1
    assert first.ready_after == 0

    # Run 2: "a" now has a recorded failed attempt (previously attempted);
    # "b" has never been attempted -> fairness processes "b" first this
    # time, even though "a" still sorts first alphabetically.
    second = await process_pending_candidates(
        db_session, llm, embedder,
        tenant_id=tenant.id,
        folder_source_id=scan.folder_source_id,
        model_provider_name="fake",
        max_profile_input_chars=MAX_INPUT_CHARS,
        max_identity_input_chars=MAX_INPUT_CHARS,
        max_embedding_input_chars=MAX_INPUT_CHARS,
        limit=1,
    )
    assert second.processed == 1
    assert second.ready_after == 1  # "b" got through — not starved by "a"
    assert second.failed == 0

    rows = await list_folder_indexed_files(
        db_session, tenant_id=tenant.id, folder_source_id=scan.folder_source_id
    )
    b_row = next(r for r in rows if r.relative_path == "b-valid.docx")
    a_row = next(r for r in rows if r.relative_path == "a-always-fails.docx")

    from meyar.services.candidate_profile_repo import get_latest_profile_version_for_document

    assert b_row.candidate_document_id is not None
    b_profile = await get_latest_profile_version_for_document(
        db_session, tenant_id=tenant.id, candidate_document_id=b_row.candidate_document_id
    )
    assert b_profile is not None
    assert b_profile.status == "COMPLETED"

    assert a_row.candidate_document_id is not None
    a_profile = await get_latest_profile_version_for_document(
        db_session, tenant_id=tenant.id, candidate_document_id=a_row.candidate_document_id
    )
    assert a_profile is not None
    assert a_profile.status == "FAILED"  # a was correctly deprioritized, not retried this run

    b_search = await search_candidates(
        db_session,
        tenant_id=tenant.id,
        request=CandidateSearchRequest(
            mode=SearchMode.STRUCTURED_ONLY, required_filters=RequiredFilters(skills=["Python"])
        ),
    )
    assert b_row.candidate_id in {r.candidate_id for r in b_search.results}
