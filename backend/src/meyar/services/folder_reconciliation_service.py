import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from meyar.embedding.provider import EmbeddingProvider, EmbeddingProviderError
from meyar.extraction.evidence import EvidenceValidationError
from meyar.extraction.identity_service import (
    IdentityExtractionPreconditionError,
    extract_candidate_identity,
)
from meyar.extraction.service import ExtractionPreconditionError, extract_candidate_profile
from meyar.ingestion.parser import DocumentParser
from meyar.llm.provider import LLMProvider
from meyar.models.candidate_identity_version import IDENTITY_STATUS_COMPLETED
from meyar.models.candidate_profile_version import PROFILE_STATUS_COMPLETED, CandidateProfileVersion
from meyar.services.audit_repo import record_event
from meyar.services.candidate_document_repo import get_candidate_document
from meyar.services.candidate_embedding_repo import list_embedding_versions_for_candidate
from meyar.services.candidate_embedding_service import (
    EmbeddingPreconditionError,
    embed_candidate_profile,
)
from meyar.services.candidate_identity_repo import get_latest_identity_version_for_document
from meyar.services.candidate_photo_service import process_photo_for_document
from meyar.services.candidate_profile_repo import get_latest_profile_version_for_document
from meyar.services.folder_indexed_file_repo import list_folder_indexed_files
from meyar.services.folder_indexer_service import FolderScanSummary, index_folder
from meyar.services.identity_authority import authorize_identity_version
from meyar.services.profile_authority import get_current_authorized_profile
from meyar.storage.base import DocumentStorage
from meyar.storage.dependency import get_photo_storage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconciliationSummary:
    """Downstream (extraction/identity/embedding) processing outcome for
    one folder_source's currently-tracked documents, distinct from
    FolderScanSummary (discovery/ingestion only, unchanged Slice 6)."""

    candidates_considered: int
    already_ready: int
    processed: int
    ready_after: int
    failed: int
    skipped_due_to_limit: int


async def _is_ready(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    candidate_document_id: uuid.UUID,
) -> tuple[bool, CandidateProfileVersion | None]:
    """Read-only readiness derivation from existing provenance — no
    processing-status column/migration needed (see docs/DECISIONS.md
    D-021). READY means: this exact current document has a COMPLETED
    CandidateProfileVersion AND a COMPLETED CandidateIdentityVersion,
    both passing current evidence authority, AND at least one
    CandidateEmbeddingVersion exists for that profile
    version. Returns the profile row too so callers that must proceed
    to extraction/embedding don't re-query it."""
    profile = await get_latest_profile_version_for_document(
        db, tenant_id=tenant_id, candidate_document_id=candidate_document_id
    )
    if profile is None or profile.status != PROFILE_STATUS_COMPLETED:
        return False, profile

    identity = await get_latest_identity_version_for_document(
        db, tenant_id=tenant_id, candidate_document_id=candidate_document_id
    )
    if identity is None or identity.status != IDENTITY_STATUS_COMPLETED:
        return False, profile

    authorized = await get_current_authorized_profile(
        db, tenant_id=tenant_id, candidate_id=candidate_id
    )
    if authorized is None or authorized[0].id != profile.id:
        return False, profile
    try:
        await authorize_identity_version(db, version=identity)
    except EvidenceValidationError:
        return False, profile

    embeddings = await list_embedding_versions_for_candidate(
        db, tenant_id=tenant_id, candidate_id=candidate_id
    )
    has_embedding = any(e.candidate_profile_version_id == profile.id for e in embeddings)
    return has_embedding, profile


async def _process_one_candidate_document(
    db: AsyncSession,
    llm: LLMProvider,
    embedding_provider: EmbeddingProvider,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    candidate_document_id: uuid.UUID,
    model_provider_name: str,
    max_profile_input_chars: int,
    max_identity_input_chars: int,
    max_embedding_input_chars: int,
) -> bool:
    """Drives one candidate's CURRENT document through whichever of
    profile extraction / identity extraction / embedding is not yet
    COMPLETE, reusing the exact Slice 4/7 services unchanged — no
    parallel extraction/embedding logic. Each stage is independently
    attempted so partial progress from a prior failed run is never
    redone: extraction and identity are only (re)run when not already
    COMPLETED for this document; embedding is idempotent by construction
    (candidate_embedding_service.embed_candidate_profile) so calling it
    whenever the profile is COMPLETED is always safe and cheap on reuse.
    Returns True only if the candidate is fully ready after this call."""
    document = await get_candidate_document(
        db, tenant_id=tenant_id, candidate_id=candidate_id, document_id=candidate_document_id
    )
    if document is None:
        return False  # defensive: should not happen for a row created by index_folder

    profile = await get_latest_profile_version_for_document(
        db, tenant_id=tenant_id, candidate_document_id=candidate_document_id
    )
    if profile is None or profile.status != PROFILE_STATUS_COMPLETED:
        try:
            profile = await extract_candidate_profile(
                db,
                llm,
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                candidate_document=document,
                model_provider_name=model_provider_name,
                max_input_chars=max_profile_input_chars,
            )
        except ExtractionPreconditionError:
            profile = None

    identity = await get_latest_identity_version_for_document(
        db, tenant_id=tenant_id, candidate_document_id=candidate_document_id
    )
    if identity is None or identity.status != IDENTITY_STATUS_COMPLETED:
        try:
            identity = await extract_candidate_identity(
                db,
                llm,
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                candidate_document=document,
                model_provider_name=model_provider_name,
                max_input_chars=max_identity_input_chars,
            )
        except IdentityExtractionPreconditionError:
            identity = None

    profile_ok = profile is not None and profile.status == PROFILE_STATUS_COMPLETED
    identity_ok = identity is not None and identity.status == IDENTITY_STATUS_COMPLETED

    embedded = False
    if profile_ok:
        try:
            await embed_candidate_profile(
                db,
                embedding_provider,
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                max_input_chars=max_embedding_input_chars,
            )
            embedded = True
        except (EmbeddingPreconditionError, EmbeddingProviderError):
            embedded = False

    if not (profile_ok and identity_ok and embedded):
        return False
    ready, _ = await _is_ready(
        db, tenant_id=tenant_id, candidate_id=candidate_id,
        candidate_document_id=candidate_document_id,
    )
    return ready


async def process_pending_candidates(
    db: AsyncSession,
    llm: LLMProvider,
    embedding_provider: EmbeddingProvider,
    *,
    tenant_id: uuid.UUID,
    folder_source_id: uuid.UUID,
    model_provider_name: str,
    max_profile_input_chars: int,
    max_identity_input_chars: int,
    max_embedding_input_chars: int,
    limit: int | None = None,
) -> ReconciliationSummary:
    """Slice 14 orchestration: for every currently-tracked FolderIndexedFile
    in this folder_source whose current document is not yet fully
    processed, run profile extraction -> identity extraction ->
    embedding, reusing the exact existing services (no parallel/duplicate
    pipeline). Several dedup-linked FolderIndexedFile rows may share one
    candidate_document_id (Slice 14 exact-content dedup) — each distinct
    document is processed at most once per call.

    Isolation: each candidate is committed independently. A failure
    (expected precondition/provider error, or any unexpected exception)
    is caught, the session is rolled back to a clean state, and the loop
    continues — one candidate's failure never stops another's, and
    already-committed candidates from earlier in this same call are
    never lost. This is a deliberate, documented deviation from the
    caller-controls-the-commit convention used elsewhere (e.g.
    index_folder) — see docs/DECISIONS.md D-021 for why a bounded,
    isolated batch loop needs its own commit boundary.

    limit bounds how many NOT-YET-READY candidates are attempted this
    call (already-ready candidates are free/no-op and never count
    against it) — a large backlog is processed incrementally across
    repeated reconciliation calls rather than forcing one unbounded
    sequential Ollama run.

    Fairness under --limit: never-attempted candidates are processed
    before previously-attempted-but-not-yet-ready ones (deterministically
    tie-broken by relative_path), so a candidate that keeps failing can
    never permanently starve a candidate that has not been tried yet —
    each repeated call re-derives this ordering fresh from current
    provenance, no separate scheduling state needed. See
    docs/DECISIONS.md D-021."""
    rows = await list_folder_indexed_files(
        db, tenant_id=tenant_id, folder_source_id=folder_source_id
    )

    considered = already_ready = processed = ready_after = failed = skipped_due_to_limit = 0
    seen_documents: set[uuid.UUID] = set()
    pending: list[tuple[uuid.UUID, uuid.UUID, str, bool]] = []

    for row in rows:
        if row.candidate_id is None or row.candidate_document_id is None:
            continue  # ingestion-level FAILED row — nothing to process yet
        candidate_id = row.candidate_id
        candidate_document_id = row.candidate_document_id
        if candidate_document_id in seen_documents:
            continue  # a dedup-linked duplicate path — same document, already considered
        seen_documents.add(candidate_document_id)
        considered += 1

        ready, profile = await _is_ready(
            db,
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            candidate_document_id=candidate_document_id,
        )
        if ready:
            already_ready += 1
            continue

        # profile is not None whenever a prior extraction attempt already
        # exists for this exact document (COMPLETED-but-still-not-ready,
        # FAILED, or MANUAL_REVIEW_REQUIRED) — the fairness signal below.
        pending.append(
            (candidate_id, candidate_document_id, row.relative_path, profile is not None)
        )

    pending.sort(key=lambda item: (item[3], item[2]))

    for candidate_id, candidate_document_id, _relative_path, _was_attempted in pending:
        if limit is not None and processed >= limit:
            skipped_due_to_limit += 1
            continue

        processed += 1
        try:
            success = await _process_one_candidate_document(
                db,
                llm,
                embedding_provider,
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                candidate_document_id=candidate_document_id,
                model_provider_name=model_provider_name,
                max_profile_input_chars=max_profile_input_chars,
                max_identity_input_chars=max_identity_input_chars,
                max_embedding_input_chars=max_embedding_input_chars,
            )
            await db.commit()
        except Exception as exc:
            await db.rollback()
            await record_event(
                db,
                tenant_id=tenant_id,
                event_type="FOLDER_RECONCILE_CANDIDATE_FAILED",
                metadata={"candidate_id": str(candidate_id), "error_type": type(exc).__name__},
            )
            await db.commit()
            success = False

        if success:
            ready_after += 1
        else:
            failed += 1

    return ReconciliationSummary(
        candidates_considered=considered,
        already_ready=already_ready,
        processed=processed,
        ready_after=ready_after + already_ready,
        failed=failed,
        skipped_due_to_limit=skipped_due_to_limit,
    )


async def reconcile_folder(
    db: AsyncSession,
    storage: DocumentStorage,
    parser: DocumentParser,
    llm: LLMProvider,
    embedding_provider: EmbeddingProvider,
    *,
    tenant_id: uuid.UUID,
    root_path: str,
    max_bytes: int,
    stability_window_seconds: float,
    model_provider_name: str,
    max_profile_input_chars: int,
    max_identity_input_chars: int,
    max_embedding_input_chars: int,
    limit: int | None,
) -> tuple[FolderScanSummary, ReconciliationSummary]:
    """Slice 14 entry point: the unchanged Slice 6 index_folder (with the
    Slice 14 stability window applied) followed by downstream candidate
    processing for whatever is not yet fully processed for its current
    document. One operator command serves both initial bulk import and
    repeatable reconciliation — safe to re-run at any point, including
    after a partial/interrupted prior run, since every step underneath
    is idempotent by construction."""
    scan_summary = await index_folder(
        db,
        storage,
        parser,
        tenant_id=tenant_id,
        root_path=root_path,
        max_bytes=max_bytes,
        stability_window_seconds=stability_window_seconds,
    )
    # Committed before downstream processing starts so discovery/ingestion
    # results are durable even if a later candidate's processing fails
    # unexpectedly before its own commit.
    await db.commit()

    # Independent, idempotent photo pass. Every document has its own commit;
    # any image failure is terminal and cannot make professional readiness fail.
    photo_storage = get_photo_storage()
    rows = await list_folder_indexed_files(
        db, tenant_id=tenant_id, folder_source_id=scan_summary.folder_source_id
    )
    photo_documents = [(row.candidate_id, row.candidate_document_id) for row in rows]
    seen_photo_documents: set[uuid.UUID] = set()
    for photo_candidate_id, photo_document_id in photo_documents:
        if photo_candidate_id is None or photo_document_id is None:
            continue
        if photo_document_id in seen_photo_documents:
            continue
        seen_photo_documents.add(photo_document_id)
        try:
            await process_photo_for_document(
                db, storage, photo_storage, tenant_id=tenant_id,
                candidate_id=photo_candidate_id, document_id=photo_document_id,
            )
        except Exception:
            await db.rollback()
            logger.warning("Unexpected photo-only failure during folder reconciliation")

    reconciliation_summary = await process_pending_candidates(
        db,
        llm,
        embedding_provider,
        tenant_id=tenant_id,
        folder_source_id=scan_summary.folder_source_id,
        model_provider_name=model_provider_name,
        max_profile_input_chars=max_profile_input_chars,
        max_identity_input_chars=max_identity_input_chars,
        max_embedding_input_chars=max_embedding_input_chars,
        limit=limit,
    )
    return scan_summary, reconciliation_summary
