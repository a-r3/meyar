import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from meyar.diagnostics import log_failure
from meyar.embedding.provider import (
    EmbeddingBusyError,
    EmbeddingInvalidOutputError,
    EmbeddingProvider,
    EmbeddingProviderError,
)
from meyar.embedding.serializer import SERIALIZER_VERSION
from meyar.extraction.deferral import ExtractionDeferredError
from meyar.extraction.evidence import EvidenceValidationError
from meyar.extraction.identity_service import (
    IdentityExtractionPreconditionError,
    infer_candidate_identity,
    load_identity_view,
    persist_identity_outcome,
)
from meyar.extraction.service import (
    ExtractionPreconditionError,
    infer_candidate_profile,
    load_profile_view,
    persist_profile_outcome,
)
from meyar.extraction.view import ProfessionalDocumentView
from meyar.ingestion.parser import DocumentParser
from meyar.llm.provider import LLMProvider
from meyar.models.candidate_document import CandidateDocument
from meyar.models.candidate_identity_version import IDENTITY_STATUS_COMPLETED
from meyar.models.candidate_profile_version import PROFILE_STATUS_COMPLETED, CandidateProfileVersion
from meyar.search.schemas import EmbeddingSearchConfig
from meyar.services.audit_repo import record_event
from meyar.services.candidate_document_repo import get_candidate_document
from meyar.services.candidate_embedding_repo import (
    EmbeddingCompatibility,
    find_compatible_embedding,
)
from meyar.services.candidate_embedding_service import (
    EmbeddingPreconditionError,
    embedding_source,
    persist_embedding,
    prepare_embedding,
    record_embedding_failure,
    record_embedding_reused,
)
from meyar.services.candidate_identity_repo import get_latest_identity_version_for_document
from meyar.services.candidate_photo_service import process_photo_for_document
from meyar.services.candidate_profile_repo import get_latest_profile_version_for_document
from meyar.services.candidate_repo import get_candidate
from meyar.services.folder_indexed_file_repo import (
    folder_tracks_document,
    list_folder_indexed_files,
)
from meyar.services.folder_indexer_service import FolderScanSummary, index_folder_and_commit
from meyar.services.identity_authority import authorize_identity_version
from meyar.services.profile_authority import ProfileAuthorityError, authorize_profile_version
from meyar.services.tenant_authority import TenantInactiveError, require_active_tenant
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
    # Issue #85: candidates not processed this run because the shared local
    # inference gate was busy (QUEUE_FULL/QUEUE_TIMEOUT). Not failures: no
    # FAILED version was written; a later run retries them.
    deferred: int = 0
    # Issue #46 S9: candidates whose authority vanished mid-run (deleted, or a newer
    # document became current). Nothing was persisted for the stale authority.
    superseded: int = 0


async def _is_ready(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    candidate_document_id: uuid.UUID,
    compatibility: EmbeddingCompatibility,
) -> tuple[bool, CandidateProfileVersion | None]:
    """Read-only readiness derivation from existing provenance — no
    processing-status column/migration needed (see docs/DECISIONS.md D-021).

    READY is DOCUMENT-level processing completion (issue #46 S9, D-111): this exact
    tracked document has a COMPLETED CandidateProfileVersion AND a COMPLETED
    CandidateIdentityVersion, BOTH passing current evidence authority, AND an
    embedding bound to that exact profile version that is COMPATIBLE with the active
    embedding configuration and the current canonical professional serialization
    (issue #46 S10; the same `EmbeddingCompatibility` predicate semantic retrieval uses:
    provider, model, revision, serializer, dimensions, source hash — never "any row").
    It is deliberately NOT "this document's profile is the Candidate's single D-100
    effective profile": a Candidate may have several tracked documents (dedup-linked
    paths that diverged) while search/evaluation still use exactly one effective
    profile (D-100, unchanged).
    Requiring effectiveness made every non-latest tracked document permanently
    "not ready" so repeated no-change reconciliation never quiesced. Returns the
    profile row too so callers that must proceed don't re-query it."""
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

    try:
        authorized = await authorize_profile_version(db, version=profile)
        await authorize_identity_version(db, version=identity)
    except (ProfileAuthorityError, EvidenceValidationError):
        return False, profile

    _, source_sha256 = embedding_source(authorized.model_dump(mode="json"))
    compatible = await find_compatible_embedding(
        db,
        tenant_id=tenant_id,
        candidate_profile_version_id=profile.id,
        source_sha256=source_sha256,
        compatibility=compatibility,
    )
    return compatible is not None, profile


def resolve_embedding_compatibility(
    embedding_provider: EmbeddingProvider, config: EmbeddingSearchConfig | None
) -> EmbeddingCompatibility:
    """The explicit active embedding configuration for one reconciliation invocation.

    Production callers pass the trusted application config (the same object semantic
    search uses). Without one, only the provider's own identity and the serializer are
    constrained and dimensions are unconstrained: tests/embedded callers that have no
    configured dimension. The provider itself is validated against it when embedding."""
    if config is None:
        return EmbeddingCompatibility(
            embedding_provider.provider_name,
            embedding_provider.model_name,
            embedding_provider.model_revision,
            SERIALIZER_VERSION,
            None,
        )
    return EmbeddingCompatibility(
        config.provider,
        config.model_name,
        config.model_revision,
        config.serializer_version,
        config.embedding_dimensions,
    )


SUPERSEDED_CANDIDATE_DELETED = "CANDIDATE_DELETED"
SUPERSEDED_DOCUMENT_NOT_TRACKED = "DOCUMENT_NOT_TRACKED"
SUPERSEDED_PROFILE_NOT_CURRENT = "PROFILE_NOT_CURRENT"


@dataclass(frozen=True)
class _StageResult:
    """superseded: the exact tenant/candidate/current-document authority this run
    was working for no longer exists (deleted, or a newer document is current).
    Nothing of this run was persisted for it; it is not a processing failure."""

    superseded: str | None = None
    version: Any = None


async def _authority(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    folder_source_id: uuid.UUID,
    candidate_id: uuid.UUID,
    document_id: uuid.UUID,
    persist: bool,
) -> tuple[CandidateDocument | None, str | None]:
    """Exact tenant + candidate + FOLDER-TRACKED document authority (issue #46 S9).

    The work item was scheduled by a FolderIndexedFile of ``folder_source_id`` that
    pointed at (candidate_id, document_id). It stays authoritative only while some
    row of that source still points at exactly that pair (D-013/D-021). That is
    deliberately NOT "the Candidate's newest document": a Candidate may legitimately
    have several tracked documents (dedup-linked paths that later diverge) and may
    receive direct-upload documents that never rewrite a folder path's authority.

    persist=False is the short read phase before inference (no lock). persist=True is
    the short persistence phase after inference: Tenant SHARE, then the Candidate row
    FOR UPDATE, held only until the phase's commit. That excludes candidate hard-delete
    (same Candidate UPDATE, same Tenant -> Candidate order as S5/S6), every concurrent
    persister for this candidate and every folder ingestion that would repoint a path
    at this candidate (they take Candidate SHARE BEFORE updating the row, S7/S8). The
    folder-row read below is therefore consistent without a FolderSource lock, so
    FolderSource -> Candidate (S7/S8) is never inverted. Never held across inference."""
    await require_active_tenant(db, tenant_id, lock=persist)
    candidate = await get_candidate(
        db, tenant_id=tenant_id, candidate_id=candidate_id, lock=persist
    )
    if candidate is None:
        return None, SUPERSEDED_CANDIDATE_DELETED
    document = await get_candidate_document(
        db, tenant_id=tenant_id, candidate_id=candidate_id, document_id=document_id
    )
    if document is None or not await folder_tracks_document(
        db, tenant_id=tenant_id, folder_source_id=folder_source_id,
        candidate_id=candidate_id, candidate_document_id=document_id,
    ):
        return None, SUPERSEDED_DOCUMENT_NOT_TRACKED
    return document, None


async def _extraction_stage(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    folder_source_id: uuid.UUID,
    candidate_id: uuid.UUID,
    document_id: uuid.UUID,
    get_latest: Callable[..., Awaitable[Any]],
    completed_status: str,
    load_view: Callable[..., Awaitable[tuple[ProfessionalDocumentView, uuid.UUID]]],
    infer: Callable[[ProfessionalDocumentView], Awaitable[Any]],
    persist_outcome: Callable[..., Awaitable[Any]],
    preconditions: tuple[type[Exception], ...],
) -> _StageResult:
    """Phase A (short, committed) -> local inference with NO SQL transaction,
    pooled connection or row lock -> Phase B (short, revalidated, committed)."""
    document, reason = await _authority(
        db, tenant_id=tenant_id, folder_source_id=folder_source_id,
        candidate_id=candidate_id, document_id=document_id, persist=False,
    )
    if document is None:
        return _StageResult(superseded=reason)
    latest = await get_latest(db, tenant_id=tenant_id, candidate_document_id=document_id)
    if latest is not None and latest.status == completed_status:
        await db.commit()
        return _StageResult(version=latest)
    try:
        view, canonical_id = await load_view(
            db, tenant_id=tenant_id, candidate_id=candidate_id, candidate_document=document
        )
    except preconditions:
        await db.commit()  # keep the audit of the unsupported document
        return _StageResult()
    await db.commit()  # Phase A ends: the connection returns to the pool

    outcome = await infer(view)

    document, reason = await _authority(
        db, tenant_id=tenant_id, folder_source_id=folder_source_id,
        candidate_id=candidate_id, document_id=document_id, persist=True,
    )
    if document is None:
        return _StageResult(superseded=reason)
    latest = await get_latest(db, tenant_id=tenant_id, candidate_document_id=document_id)
    if latest is not None and latest.status == completed_status:
        await db.commit()  # a concurrent run completed this stage: discard our result
        return _StageResult(version=latest)
    version = await persist_outcome(db, document, canonical_id, outcome)
    await db.commit()
    return _StageResult(version=version)


async def _embedding_stage(
    db: AsyncSession,
    embedding_provider: EmbeddingProvider,
    *,
    tenant_id: uuid.UUID,
    folder_source_id: uuid.UUID,
    candidate_id: uuid.UUID,
    document_id: uuid.UUID,
    profile_version: CandidateProfileVersion,
    compatibility: EmbeddingCompatibility,
    max_input_chars: int,
) -> tuple[_StageResult, bool]:
    """Same two-phase shape for embedding of THIS tracked document's own COMPLETED
    profile version (D-111; not the Candidate's effective profile). Returns
    (stage result, embedded)."""
    document, reason = await _authority(
        db, tenant_id=tenant_id, folder_source_id=folder_source_id,
        candidate_id=candidate_id, document_id=document_id, persist=False,
    )
    if document is None:
        return _StageResult(superseded=reason), False
    try:
        plan = await prepare_embedding(
            db, embedding_provider, tenant_id=tenant_id, candidate_id=candidate_id,
            max_input_chars=max_input_chars, profile_version=profile_version,
            compatibility=compatibility,
        )
    except EmbeddingPreconditionError:
        await db.commit()
        return _StageResult(), False
    if plan.existing is not None:
        await record_embedding_reused(db, tenant_id=tenant_id, candidate_id=candidate_id, plan=plan)
        await db.commit()
        return _StageResult(), True
    await db.commit()  # Phase A ends: no connection while the local model runs

    failure: EmbeddingProviderError | None = None
    result = None
    try:
        result = await embedding_provider.embed(plan.text)
    except EmbeddingProviderError as exc:
        failure = exc
    if failure is None and result is not None and (
        (result.provider, result.model_name, result.model_revision)
        != (compatibility.provider, compatibility.model_name, compatibility.model_revision)
        or result.dimensions != len(result.vector)
        or (
            compatibility.embedding_dimensions is not None
            and result.dimensions != compatibility.embedding_dimensions
        )
    ):
        # Fail closed (mirrors the search-side result provenance check): never persist a
        # vector the active configuration's semantic retrieval could not use.
        failure = EmbeddingInvalidOutputError("Embedding result does not match active config.")
        result = None

    document, reason = await _authority(
        db, tenant_id=tenant_id, folder_source_id=folder_source_id,
        candidate_id=candidate_id, document_id=document_id, persist=True,
    )
    if document is None:
        return _StageResult(superseded=reason), False
    if failure is not None:
        await record_embedding_failure(
            db, tenant_id=tenant_id, candidate_id=candidate_id, plan=plan, exc=failure
        )
        await db.commit()
        if isinstance(failure, EmbeddingBusyError):
            raise failure  # transient: defer the candidate, never count it failed
        return _StageResult(), False
    assert result is not None
    latest = await get_latest_profile_version_for_document(
        db, tenant_id=tenant_id, candidate_document_id=document_id
    )
    if latest is None or latest.id != profile_version.id or (
        latest.status != PROFILE_STATUS_COMPLETED
    ):
        await db.commit()  # this document's profile changed while embedding: never bind to it
        return _StageResult(superseded=SUPERSEDED_PROFILE_NOT_CURRENT), False
    try:
        current = await prepare_embedding(
            db, embedding_provider, tenant_id=tenant_id, candidate_id=candidate_id,
            max_input_chars=max_input_chars, profile_version=latest,
            compatibility=compatibility,
        )
    except EmbeddingPreconditionError:
        await db.commit()
        return _StageResult(superseded=SUPERSEDED_PROFILE_NOT_CURRENT), False
    if current.existing is not None:
        await record_embedding_reused(
            db, tenant_id=tenant_id, candidate_id=candidate_id, plan=current
        )
    else:
        await persist_embedding(
            db, tenant_id=tenant_id, candidate_id=candidate_id, plan=current, result=result
        )
    await db.commit()
    return _StageResult(), True


async def _supersede(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    document_id: uuid.UUID,
    reason: str,
) -> None:
    await db.rollback()
    await require_active_tenant(db, tenant_id)
    await record_event(
        db,
        tenant_id=tenant_id,
        event_type="FOLDER_RECONCILE_CANDIDATE_SUPERSEDED",
        metadata={
            "candidate_id": str(candidate_id),
            "document_id": str(document_id),
            "reason_code": reason,
        },
    )
    await db.commit()


async def _process_one_candidate_document(
    db: AsyncSession,
    llm: LLMProvider,
    embedding_provider: EmbeddingProvider,
    *,
    tenant_id: uuid.UUID,
    folder_source_id: uuid.UUID,
    candidate_id: uuid.UUID,
    candidate_document_id: uuid.UUID,
    model_provider_name: str,
    max_profile_input_chars: int,
    max_identity_input_chars: int,
    max_embedding_input_chars: int,
    compatibility: EmbeddingCompatibility,
) -> tuple[bool, str | None]:
    """Drives one candidate's CURRENT document through whichever of profile
    extraction / identity extraction / embedding is not yet COMPLETE.

    Issue #46 S9: every stage is a short committed read phase, local inference with
    no SQL transaction/connection/lock, and a short persistence phase that
    revalidates tenant + candidate + exact current document under the Candidate row
    lock and discards the result if it lost a race. A stage is attempted only when
    not already COMPLETED, so partial progress is never redone. Returns
    (fully ready, superseded reason). A superseded run persisted nothing for the
    document that is no longer authoritative."""
    common = dict(
        tenant_id=tenant_id, folder_source_id=folder_source_id,
        candidate_id=candidate_id, document_id=candidate_document_id,
    )

    async def persist_profile(db_, document, canonical_id, outcome):
        return await persist_profile_outcome(
            db_, tenant_id=tenant_id, candidate_id=candidate_id, candidate_document=document,
            canonical_document_id=canonical_id, model_provider_name=model_provider_name,
            outcome=outcome,
        )

    async def persist_identity(db_, document, canonical_id, outcome):
        return await persist_identity_outcome(
            db_, tenant_id=tenant_id, candidate_id=candidate_id, candidate_document=document,
            canonical_document_id=canonical_id, model_provider_name=model_provider_name,
            outcome=outcome,
        )

    profile_stage = await _extraction_stage(
        db, **common,
        get_latest=get_latest_profile_version_for_document,
        completed_status=PROFILE_STATUS_COMPLETED,
        load_view=load_profile_view,
        infer=lambda view: infer_candidate_profile(
            llm, view, max_input_chars=max_profile_input_chars
        ),
        persist_outcome=persist_profile,
        preconditions=(ExtractionPreconditionError,),
    )
    if profile_stage.superseded:
        return False, profile_stage.superseded

    identity_stage = await _extraction_stage(
        db, **common,
        get_latest=get_latest_identity_version_for_document,
        completed_status=IDENTITY_STATUS_COMPLETED,
        load_view=load_identity_view,
        infer=lambda view: infer_candidate_identity(
            llm, view, max_input_chars=max_identity_input_chars
        ),
        persist_outcome=persist_identity,
        preconditions=(IdentityExtractionPreconditionError,),
    )
    if identity_stage.superseded:
        return False, identity_stage.superseded

    profile = profile_stage.version
    identity = identity_stage.version
    profile_ok = profile is not None and profile.status == PROFILE_STATUS_COMPLETED
    identity_ok = identity is not None and identity.status == IDENTITY_STATUS_COMPLETED

    embedded = False
    if profile_ok:
        assert profile is not None
        embedding_stage, embedded = await _embedding_stage(
            db, embedding_provider, profile_version=profile, compatibility=compatibility,
            max_input_chars=max_embedding_input_chars, **common,
        )
        if embedding_stage.superseded:
            return False, embedding_stage.superseded

    if not (profile_ok and identity_ok and embedded):
        return False, None
    ready, _ = await _is_ready(
        db, tenant_id=tenant_id, candidate_id=candidate_id,
        candidate_document_id=candidate_document_id, compatibility=compatibility,
    )
    return ready, None


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
    embedding_config: EmbeddingSearchConfig | None = None,
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
    await require_active_tenant(db, tenant_id)
    # The active embedding configuration is resolved ONCE per invocation and is
    # authoritative for this whole run (issue #46 S10).
    compatibility = resolve_embedding_compatibility(embedding_provider, embedding_config)
    rows = await list_folder_indexed_files(
        db, tenant_id=tenant_id, folder_source_id=folder_source_id
    )

    considered = already_ready = processed = ready_after = failed = skipped_due_to_limit = 0
    deferred = 0
    superseded = 0
    inference_busy = False
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
            compatibility=compatibility,
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
        if inference_busy:
            # Issue #85: local inference is saturated right now; attempting
            # more candidates this run would only queue/time out again.
            deferred += 1
            continue

        processed += 1
        try:
            success, superseded_reason = await _process_one_candidate_document(
                db,
                llm,
                embedding_provider,
                tenant_id=tenant_id,
                folder_source_id=folder_source_id,
                candidate_id=candidate_id,
                candidate_document_id=candidate_document_id,
                model_provider_name=model_provider_name,
                max_profile_input_chars=max_profile_input_chars,
                max_identity_input_chars=max_identity_input_chars,
                max_embedding_input_chars=max_embedding_input_chars,
                compatibility=compatibility,
            )
            if superseded_reason is not None:
                # Deleted, or a newer document is current: nothing was persisted
                # for the stale authority; a truthful audit, not a failure.
                await _supersede(
                    db, tenant_id=tenant_id, candidate_id=candidate_id,
                    document_id=candidate_document_id, reason=superseded_reason,
                )
                superseded += 1
                continue
            await require_active_tenant(db, tenant_id)
            await db.commit()
        except TenantInactiveError:
            await db.rollback()
            raise
        except (ExtractionDeferredError, EmbeddingBusyError):
            # Transient admission refusal: keep any stage already completed
            # for this candidate plus the deferral audit; no FAILED version
            # was minted, so the next run retries exactly what is missing.
            await require_active_tenant(db, tenant_id)
            await db.commit()
            inference_busy = True
            deferred += 1
            continue
        except Exception as exc:
            await db.rollback()
            log_failure(
                logger, component="folder_reconciliation",
                code="CANDIDATE_PROCESSING_FAILED", exc=exc,
            )
            await require_active_tenant(db, tenant_id)
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
        deferred=deferred,
        superseded=superseded,
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
    embedding_config: EmbeddingSearchConfig | None = None,
) -> tuple[FolderScanSummary, ReconciliationSummary]:
    """Slice 14 entry point: the unchanged Slice 6 index_folder (with the
    Slice 14 stability window applied) followed by downstream candidate
    processing for whatever is not yet fully processed for its current
    document. One operator command serves both initial bulk import and
    repeatable reconciliation — safe to re-run at any point, including
    after a partial/interrupted prior run, since every step underneath
    is idempotent by construction."""
    # Committed before downstream processing starts so discovery/ingestion
    # results are durable even if a later candidate's processing fails
    # unexpectedly before its own commit. A failure up to here removes every
    # original saved by this scan (their DB authority rolls back with it).
    # Same-source runs are serialized and identical content converges (S7).
    scan_summary = await index_folder_and_commit(
        db,
        storage,
        parser,
        tenant_id=tenant_id,
        root_path=root_path,
        max_bytes=max_bytes,
        stability_window_seconds=stability_window_seconds,
    )

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
        embedding_config=embedding_config,
    )
    return scan_summary, reconciliation_summary
