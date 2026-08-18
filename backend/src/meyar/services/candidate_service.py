import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from meyar.services.candidate_document_repo import list_candidate_documents
from meyar.services.candidate_repo import delete_candidate_row, get_candidate
from meyar.storage.base import DocumentStorage


async def delete_candidate_cascade(
    db: AsyncSession,
    storage: DocumentStorage,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
) -> int | None:
    """Hard-deletes a candidate: removes every stored document's bytes
    from DocumentStorage, then deletes the DB rows (CandidateDocument and
    CanonicalDocument cascade via ondelete=CASCADE). Returns the number of
    documents deleted, or None if the candidate did not exist for this
    tenant. Never leaves an orphaned file after a successful delete."""
    candidate = await get_candidate(db, tenant_id=tenant_id, candidate_id=candidate_id)
    if candidate is None:
        return None

    documents = await list_candidate_documents(db, tenant_id=tenant_id, candidate_id=candidate_id)
    for document in documents:
        await storage.delete(storage_key=document.storage_key)

    await delete_candidate_row(db, tenant_id=tenant_id, candidate_id=candidate_id)
    return len(documents)
