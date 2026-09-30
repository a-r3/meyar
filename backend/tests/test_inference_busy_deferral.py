"""Issue #85 correction pass — Blocker B: a transient local-inference
admission refusal (QUEUE_FULL / QUEUE_TIMEOUT) is NOT a model failure.

It must never mint an immutable FAILED CandidateProfileVersion /
CandidateIdentityVersion (which would supersede the accepted COMPLETED one
as the candidate's current professional state). Extraction defers; a later
retry creates the real next version. The REAL OllamaLLMProvider/admission
gate is used for the busy path (in-memory transport, never a real Ollama);
synthetic data only."""

import uuid
from pathlib import Path

import httpx
import pytest
from fakes import FakeEmbeddingProvider, FakeLLMProvider
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from test_candidate_identity import (  # noqa: F401 - pytest fixture import
    _valid_identity_extraction,
    candidate_with_contact_content,
)
from test_candidate_profile_extraction import (  # noqa: F401 - pytest fixture import
    _valid_extraction,
    candidate_with_parsed_cv,
)
from test_folder_reconciliation import (
    MAX_BYTES,
    MAX_INPUT_CHARS,
    _copy_fixture,
    _identity_extraction,
    _parser,
    _profile_extraction,
    _storage,
    _write_single_paragraph_docx,
)

from meyar.embedding.provider import EmbeddingBusyError
from meyar.extraction.deferral import ExtractionDeferredError
from meyar.extraction.identity_service import extract_candidate_identity
from meyar.extraction.service import extract_candidate_profile
from meyar.llm import concurrency
from meyar.llm.ollama_provider import OllamaLLMProvider
from meyar.llm.provider import InferenceBusyError
from meyar.models.audit_event import AuditEvent
from meyar.models.candidate_embedding_version import CandidateEmbeddingVersion
from meyar.models.candidate_identity_version import CandidateIdentityVersion
from meyar.models.candidate_profile_version import CandidateProfileVersion
from meyar.search.planner_schemas import PlannerOutcome, PlannerReasonCode
from meyar.search.planner_service import plan_candidate_search
from meyar.services.candidate_document_repo import get_candidate_document
from meyar.services.candidate_identity_repo import get_current_identity_version
from meyar.services.candidate_profile_repo import get_current_profile_version
from meyar.services.folder_indexer_service import index_folder
from meyar.services.folder_reconciliation_service import process_pending_candidates
from meyar.services.tenant_repo import create_tenant


def _never_called(request: httpx.Request) -> httpx.Response:
    raise AssertionError("A refused call must never reach Ollama.")


class _BusyGate:
    """Holds the ONLY process-wide inference slot so the real provider is
    refused with a genuine QUEUE_FULL (no waiters allowed) or QUEUE_TIMEOUT
    (one waiter, tiny wait)."""

    def __init__(self, reason: str) -> None:
        self.max_queued = 0 if reason == "QUEUE_FULL" else 1
        self.timeout = 0.05
        self._holder = None

    async def __aenter__(self) -> OllamaLLMProvider:
        concurrency.reset_inference_admission()
        admission = concurrency.get_inference_admission(
            max_active=1, max_queued=self.max_queued, queue_timeout_seconds=self.timeout
        )
        self._holder = admission.slot()
        await self._holder.__aenter__()
        return OllamaLLMProvider(
            base_url="http://127.0.0.1:11434",
            model="meyar-test-llm:v1",
            timeout_seconds=5,
            transport=httpx.MockTransport(_never_called),
            max_concurrency=1,
            max_queued=self.max_queued,
            queue_timeout_seconds=self.timeout,
        )

    async def __aexit__(self, *exc) -> None:  # noqa: ANN002
        assert self._holder is not None
        await self._holder.__aexit__(None, None, None)
        concurrency.reset_inference_admission()


async def _events(db: AsyncSession, tenant_id: uuid.UUID, event_type: str) -> list[dict]:
    rows = (
        await db.scalars(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenant_id, AuditEvent.event_type == event_type
            )
        )
    ).all()
    return [dict(row.event_metadata) for row in rows]


@pytest.mark.parametrize("reason", ["QUEUE_FULL", "QUEUE_TIMEOUT"])
async def test_busy_profile_extraction_creates_no_failed_version_and_v1_stays_current(
    db_session: AsyncSession, candidate_with_parsed_cv, reason: str  # noqa: F811
) -> None:
    tenant, _plaintext, candidate_id_text, document_id_text = candidate_with_parsed_cv
    candidate_id, document_id = uuid.UUID(candidate_id_text), uuid.UUID(document_id_text)
    document = await get_candidate_document(
        db_session, tenant_id=tenant.id, candidate_id=candidate_id, document_id=document_id
    )
    # A. completed v1 exists.
    v1 = await extract_candidate_profile(
        db_session,
        FakeLLMProvider(extraction=_valid_extraction()),
        tenant_id=tenant.id,
        candidate_id=candidate_id,
        candidate_document=document,
        model_provider_name="fake",
        max_input_chars=20000,
    )
    await db_session.commit()
    assert (v1.version_number, v1.status) == (1, "COMPLETED")

    # B. next attempt is refused by the shared inference gate.
    async with _BusyGate(reason) as busy_llm:
        with pytest.raises(ExtractionDeferredError) as deferred:
            await extract_candidate_profile(
                db_session,
                busy_llm,
                tenant_id=tenant.id,
                candidate_id=candidate_id,
                candidate_document=document,
                model_provider_name="ollama",
                max_input_chars=20000,
            )
        await db_session.commit()
    assert deferred.value.code == "INFERENCE_BUSY" and deferred.value.reason == reason

    # C. no v2 FAILED (or any) version; D. v1 is still current.
    count = await db_session.scalar(
        select(func.count()).select_from(CandidateProfileVersion).where(
            CandidateProfileVersion.candidate_id == candidate_id
        )
    )
    assert count == 1
    current = await get_current_profile_version(
        db_session, tenant_id=tenant.id, candidate_id=candidate_id
    )
    assert current is not None and current.id == v1.id and current.status == "COMPLETED"
    deferred_events = await _events(
        db_session, tenant.id, "CANDIDATE_PROFILE_EXTRACTION_DEFERRED"
    )
    assert deferred_events == [
        {
            "candidate_id": str(candidate_id),
            "document_id": str(document_id),
            "error_code": "INFERENCE_BUSY",
            "reason_code": reason,
        }
    ]
    assert await _events(db_session, tenant.id, "CANDIDATE_PROFILE_EXTRACTION_FAILED") == []

    # F. a later retry with capacity available creates the real next version.
    v2 = await extract_candidate_profile(
        db_session,
        FakeLLMProvider(extraction=_valid_extraction()),
        tenant_id=tenant.id,
        candidate_id=candidate_id,
        candidate_document=document,
        model_provider_name="fake",
        max_input_chars=20000,
    )
    await db_session.commit()
    assert (v2.version_number, v2.status) == (2, "COMPLETED")


@pytest.mark.parametrize("reason", ["QUEUE_FULL", "QUEUE_TIMEOUT"])
async def test_busy_identity_extraction_creates_no_failed_version_and_v1_stays_current(
    db_session: AsyncSession, candidate_with_contact_content, reason: str  # noqa: F811
) -> None:
    tenant, candidate, document, _canonical = candidate_with_contact_content
    v1 = await extract_candidate_identity(
        db_session,
        FakeLLMProvider(identity_extraction=_valid_identity_extraction()),
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        candidate_document=document,
        model_provider_name="fake",
        max_input_chars=20000,
    )
    await db_session.commit()
    assert (v1.version_number, v1.status) == (1, "COMPLETED")

    async with _BusyGate(reason) as busy_llm:
        with pytest.raises(ExtractionDeferredError) as deferred:
            await extract_candidate_identity(
                db_session,
                busy_llm,
                tenant_id=tenant.id,
                candidate_id=candidate.id,
                candidate_document=document,
                model_provider_name="ollama",
                max_input_chars=20000,
            )
        await db_session.commit()
    assert deferred.value.reason == reason
    count = await db_session.scalar(
        select(func.count()).select_from(CandidateIdentityVersion).where(
            CandidateIdentityVersion.candidate_id == candidate.id
        )
    )
    assert count == 1
    current = await get_current_identity_version(
        db_session, tenant_id=tenant.id, candidate_id=candidate.id
    )
    assert current is not None and current.id == v1.id and current.status == "COMPLETED"
    events = await _events(db_session, tenant.id, "CANDIDATE_IDENTITY_EXTRACTION_DEFERRED")
    assert [event["reason_code"] for event in events] == [reason]
    # Audit carries ids/codes only — never identity content.
    assert "Jane" not in str(events) and "example.com" not in str(events)

    v2 = await extract_candidate_identity(
        db_session,
        FakeLLMProvider(identity_extraction=_valid_identity_extraction()),
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        candidate_document=document,
        model_provider_name="fake",
        max_input_chars=20000,
    )
    await db_session.commit()
    assert (v2.version_number, v2.status) == (2, "COMPLETED")


async def _indexed_folder(db_session: AsyncSession, tmp_path: Path, name: str):  # noqa: ANN202
    tenant = await create_tenant(db_session, name=name)
    await db_session.commit()
    root = tmp_path / "cvs"
    _copy_fixture("valid_cv.pdf", root / "a.pdf")
    # Synthetic single-paragraph DOCX whose block 0 carries the same
    # evidence line, so the shared fake extraction verifies for both.
    _write_single_paragraph_docx(root / "b.docx", "Skills: Python, SQL, Docker")
    scan = await index_folder(
        db_session, _storage(tmp_path), _parser(),
        tenant_id=tenant.id, root_path=str(root), max_bytes=MAX_BYTES,
    )
    await db_session.commit()
    return tenant, scan


async def _reconcile(db_session, tenant, scan, llm, embedder):  # noqa: ANN001, ANN202
    return await process_pending_candidates(
        db_session,
        llm,
        embedder,
        tenant_id=tenant.id,
        folder_source_id=scan.folder_source_id,
        model_provider_name="fake",
        max_profile_input_chars=MAX_INPUT_CHARS,
        max_identity_input_chars=MAX_INPUT_CHARS,
        max_embedding_input_chars=MAX_INPUT_CHARS,
    )


async def test_reconciliation_defers_on_busy_inference_and_retries_later(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    tenant, scan = await _indexed_folder(db_session, tmp_path, "T-recon-busy")
    busy = FakeLLMProvider(error=InferenceBusyError("QUEUE_FULL"))
    summary = await _reconcile(db_session, tenant, scan, busy, FakeEmbeddingProvider())
    assert summary.processed == 1  # only the first candidate was attempted
    assert summary.deferred == 2  # first refused; the rest of this run not attempted
    assert summary.failed == 0
    assert busy.call_count == 1
    assert await db_session.scalar(
        select(func.count()).select_from(CandidateProfileVersion).where(
            CandidateProfileVersion.tenant_id == tenant.id
        )
    ) == 0
    assert await _events(db_session, tenant.id, "FOLDER_RECONCILE_CANDIDATE_FAILED") == []

    ok = FakeLLMProvider(
        extraction=_profile_extraction(), identity_extraction=_identity_extraction()
    )
    retried = await _reconcile(db_session, tenant, scan, ok, FakeEmbeddingProvider())
    assert retried.deferred == 0
    assert retried.processed == 2
    statuses = (
        await db_session.scalars(
            select(CandidateProfileVersion.status).where(
                CandidateProfileVersion.tenant_id == tenant.id
            )
        )
    ).all()
    assert "FAILED" not in statuses and "COMPLETED" in statuses


async def test_reconciliation_busy_embedding_keeps_completed_profile_and_defers(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    tenant, scan = await _indexed_folder(db_session, tmp_path, "T-recon-busy-embed")
    ok_llm = FakeLLMProvider(
        extraction=_profile_extraction(), identity_extraction=_identity_extraction()
    )
    busy_embedder = FakeEmbeddingProvider(error=EmbeddingBusyError("QUEUE_TIMEOUT"))
    summary = await _reconcile(db_session, tenant, scan, ok_llm, busy_embedder)
    assert summary.deferred >= 1 and summary.failed == 0
    completed = (
        await db_session.scalars(
            select(CandidateProfileVersion).where(
                CandidateProfileVersion.tenant_id == tenant.id,
                CandidateProfileVersion.status == "COMPLETED",
            )
        )
    ).all()
    assert len(completed) >= 1  # the completed stage was kept, not rolled back
    assert await db_session.scalar(
        select(func.count()).select_from(CandidateEmbeddingVersion).where(
            CandidateEmbeddingVersion.tenant_id == tenant.id
        )
    ) == 0
    assert await _events(db_session, tenant.id, "CANDIDATE_EMBEDDING_FAILED") == []
    assert len(await _events(db_session, tenant.id, "CANDIDATE_EMBEDDING_DEFERRED")) >= 1

    retried = await _reconcile(db_session, tenant, scan, ok_llm, FakeEmbeddingProvider())
    assert retried.deferred == 0 and retried.failed == 0
    assert await db_session.scalar(
        select(func.count()).select_from(CandidateEmbeddingVersion).where(
            CandidateEmbeddingVersion.tenant_id == tenant.id
        )
    ) == 2


async def test_busy_nl_planner_reports_transient_busy_without_a_model_attempt(
    db_session: AsyncSession,
) -> None:
    from meyar.embedding.serializer import SERIALIZER_VERSION
    from meyar.search.schemas import EmbeddingSearchConfig
    from meyar.ui.presentation import INFERENCE_BUSY_TEXT, planner_outcome_view

    tenant = await create_tenant(db_session, name="T-planner-busy")
    await db_session.commit()
    async with _BusyGate("QUEUE_FULL") as busy_llm:
        result = await plan_candidate_search(
            db_session,
            busy_llm,
            tenant_id=tenant.id,
            natural_language_request="Find experienced banking AML modernization professionals.",
            as_of_date=None,  # type: ignore[arg-type]
            embedding_config=EmbeddingSearchConfig(
                provider="ollama",
                model_name="nomic-embed-text",
                model_revision="",
                serializer_version=SERIALIZER_VERSION,
                embedding_dimensions=768,
            ),
        )
    assert result.outcome == PlannerOutcome.PLANNER_PROVIDER_FAILURE
    assert result.reason_codes == [PlannerReasonCode.INFERENCE_BUSY]
    assert result.attempt_count == 0  # no model attempt was made
    view = planner_outcome_view(result)
    assert (view.title, view.message) == INFERENCE_BUSY_TEXT
