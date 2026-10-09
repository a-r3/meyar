"""Short, database-owned authority phases for explicitly requested direct work.

Direct work snapshots the selected document AND the candidate's document/attempt
frontier. It does not borrow folder-path ownership rules (D-110).
"""

import hashlib
import json
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.models.candidate_document import CandidateDocument
from meyar.models.candidate_identity_version import CandidateIdentityVersion
from meyar.models.candidate_profile_version import CandidateProfileVersion
from meyar.models.canonical_document import CanonicalDocument
from meyar.services.candidate_repo import get_candidate
from meyar.services.tenant_authority import require_active_tenant


class DirectInferenceSupersededError(Exception):
    code = "INFERENCE_AUTHORITY_CHANGED"

    def __init__(self) -> None:
        super().__init__("Processing authority changed; retry with current candidate data.")


@dataclass(frozen=True)
class DirectAuthority:
    fingerprint: str
    document_id: uuid.UUID
    canonical_id: uuid.UUID


async def direct_authority(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    document_id: uuid.UUID,
    identity: bool = False,
    lock: bool = False,
) -> tuple[DirectAuthority, CandidateDocument]:
    await require_active_tenant(db, tenant_id, lock=lock)
    candidate = await get_candidate(db, tenant_id=tenant_id, candidate_id=candidate_id, lock=lock)
    if candidate is None or candidate.status != "ACTIVE":
        raise DirectInferenceSupersededError()
    query = (
        select(CandidateDocument)
        .where(
            CandidateDocument.tenant_id == tenant_id,
            CandidateDocument.candidate_id == candidate_id,
            CandidateDocument.id == document_id,
        )
        .execution_options(populate_existing=True)
    )
    canonical_query = (
        select(CanonicalDocument)
        .where(
            CanonicalDocument.tenant_id == tenant_id,
            CanonicalDocument.candidate_document_id == document_id,
        )
        .order_by(CanonicalDocument.created_at.desc(), CanonicalDocument.id.desc())
        .limit(1)
    )
    if lock:
        query = query.with_for_update(read=True)
        canonical_query = canonical_query.with_for_update(read=True)
    document = await db.scalar(query)
    canonical = await db.scalar(canonical_query.execution_options(populate_existing=True))
    if document is None or canonical is None:
        raise DirectInferenceSupersededError()
    newest_document = await db.scalar(
        select(CandidateDocument.id)
        .where(
            CandidateDocument.tenant_id == tenant_id,
            CandidateDocument.candidate_id == candidate_id,
        )
        .order_by(CandidateDocument.created_at.desc(), CandidateDocument.id.desc())
        .limit(1)
    )
    state: list[object] = [
        candidate.status,
        str(candidate.updated_at),
        str(newest_document),
        str(document.id),
        document.sha256_hash,
        document.parser_status,
        str(canonical.id),
        canonical.content,
    ]
    # Professional state is always material; identity is read only by identity work.
    models = [CandidateProfileVersion]
    if identity:
        models.append(CandidateIdentityVersion)  # type: ignore[arg-type]
    for model in models:
        version_query = (
            select(model)
            .where(
                model.tenant_id == tenant_id,
                model.candidate_id == candidate_id,
            )
            .order_by(model.version_number.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        if lock:
            version_query = version_query.with_for_update(read=True)
        version = await db.scalar(version_query)
        if version is None:
            state.append(None)
        else:
            content = (
                version.identity_content
                if isinstance(version, CandidateIdentityVersion)
                else version.profile_content
            )
            state.append([str(version.id), version.status, content])
    digest = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
    return DirectAuthority(digest, document.id, canonical.id), document


async def revalidate_direct_authority(
    db: AsyncSession,
    expected: DirectAuthority,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    identity: bool = False,
) -> CandidateDocument:
    current, document = await direct_authority(
        db,
        tenant_id=tenant_id,
        candidate_id=candidate_id,
        document_id=expected.document_id,
        identity=identity,
        lock=True,
    )
    if current != expected:
        await db.rollback()
        raise DirectInferenceSupersededError()
    return document
