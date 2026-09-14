"""Shared synthetic fixtures for Slice 8 hybrid-search tests. Mirrors the
seeding pattern used in test_candidate_embedding.py (Slice 7) — never a
real CV, always hand-seeded rows."""

import uuid
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from meyar.embedding.serializer import (
    SERIALIZER_VERSION,
    build_professional_embedding_text,
    compute_source_sha256,
)
from meyar.services.candidate_document_repo import (
    create_candidate_document,
    create_canonical_document,
)
from meyar.services.candidate_embedding_repo import create_embedding_version
from meyar.services.candidate_profile_repo import create_profile_version
from meyar.services.candidate_repo import create_candidate

DEFAULT_AS_OF_DATE = date(2026, 1, 1)


def synthetic_evidence(*parts: object) -> list[dict]:
    """Explicitly author a synthetic positive quote for a non-evidence test.

    Never invoked by persistence helpers or on caller-supplied evidence.
    Boundary tests must supply their own independent canonical text/quotes.
    """
    quote = " ".join(str(part) for part in parts if part is not None and part != "")
    return [{"page": 1, "block_index": 0, "quote": quote}]


def _canonical_content_from_profile(profile_content: dict | None, *, fallback: str) -> dict:
    blocks_by_page: dict[int, dict[int, list[str]]] = {}
    if profile_content is not None:
        for value in profile_content.values():
            if not isinstance(value, list):
                continue
            for item in value:
                if not isinstance(item, dict):
                    continue
                for ref in item.get("evidence", []):
                    if not isinstance(ref, dict):
                        continue
                    page = int(ref.get("page", 1))
                    index = int(ref.get("block_index", 0))
                    quote = str(ref.get("quote", "")).strip()
                    if quote:
                        blocks_by_page.setdefault(page, {}).setdefault(index, []).append(quote)
    if not blocks_by_page:
        blocks_by_page = {1: {0: [fallback]}}
    pages: list[dict[str, Any]] = []
    for page, blocks in sorted(blocks_by_page.items()):
        pages.append(
            {
                "page": page,
                "blocks": [
                    {"index": index, "text": "\n".join(quotes)}
                    for index, quotes in sorted(blocks.items())
                ],
            }
        )
    return {"pages": pages}


def current_source_sha256(profile_content: dict) -> str:
    """The exact hash meyar.search now requires an embedding to carry to
    be treated as current — the same computation
    embed_candidate_profile (Slice 7) and meyar.search.service (Slice 8
    fix) both perform: build_professional_embedding_text +
    compute_source_sha256 over the candidate's CURRENT profile_content."""
    return compute_source_sha256(build_professional_embedding_text(profile_content))


async def seed_candidate_with_profile(
    db_session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    profile_content: dict | None,
    status: str = "COMPLETED",
):
    """Creates a Candidate + one document/canonical pair + one
    CandidateProfileVersion (v1) with the given content. Returns
    (candidate, profile_version)."""
    stored_profile_content = (
        profile_content if status == "COMPLETED" else None
    )
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
        content=_canonical_content_from_profile(stored_profile_content, fallback="synthetic"),
    )
    profile_version = await create_profile_version(
        db_session,
        tenant_id=tenant_id,
        candidate_id=candidate.id,
        candidate_document_id=document.id,
        canonical_document_id=canonical.id,
        source_sha256="a" * 64,
        schema_version="candidate-profile-v1",
        prompt_version="candidate-profile-extraction-v1",
        model_provider="fake",
        model_name="fake-model",
        model_metadata={},
        status=status,
        profile_content=stored_profile_content,
    )
    return candidate, profile_version


async def seed_next_profile_version(
    db_session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate,
    profile_content: dict,
):
    """Creates the NEXT CandidateProfileVersion for an existing candidate
    (e.g. v2), reusing a fresh document/canonical pair."""
    stored_profile_content = profile_content
    assert stored_profile_content is not None
    document = await create_candidate_document(
        db_session,
        tenant_id=tenant_id,
        candidate_id=candidate.id,
        original_filename="synthetic-v2.pdf",
        mime_type="application/pdf",
        byte_size=100,
        sha256_hash="b" * 64,
        storage_key=f"test/{uuid.uuid4().hex}",
    )
    canonical = await create_canonical_document(
        db_session,
        tenant_id=tenant_id,
        candidate_document_id=document.id,
        parser_name="test-parser",
        parser_version="1.0.0",
        language=None,
        content=_canonical_content_from_profile(stored_profile_content, fallback="synthetic v2"),
    )
    return await create_profile_version(
        db_session,
        tenant_id=tenant_id,
        candidate_id=candidate.id,
        candidate_document_id=document.id,
        canonical_document_id=canonical.id,
        source_sha256="b" * 64,
        schema_version="candidate-profile-v1",
        prompt_version="candidate-profile-extraction-v1",
        model_provider="fake",
        model_name="fake-model",
        model_metadata={},
        status="COMPLETED",
        profile_content=stored_profile_content,
    )


async def seed_embedding(
    db_session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    profile_version_id: uuid.UUID,
    vector: list[float],
    provider: str = "fake-embedding",
    model_name: str = "fake-embedding-model-v1",
    model_revision: str = "",
    serializer_version: str = SERIALIZER_VERSION,
    profile_content: dict | None = None,
    source_sha256: str | None = None,
):
    """`profile_content` should be the SAME content the candidate's
    current CandidateProfileVersion was seeded with — when given (and
    `source_sha256` is not explicitly overridden), the embedding is
    stamped with the exact current canonical source_sha256, so it is
    treated as fresh/current by meyar.search (see docs/DECISIONS.md
    D-015). Pass an explicit, deliberately WRONG `source_sha256` to
    construct a stale-hash regression fixture instead."""
    if source_sha256 is None:
        source_sha256 = (
            current_source_sha256(profile_content)
            if profile_content is not None
            else uuid.uuid4().hex + "0" * 24
        )
    return await create_embedding_version(
        db_session,
        tenant_id=tenant_id,
        candidate_id=candidate_id,
        candidate_profile_version_id=profile_version_id,
        provider=provider,
        model_name=model_name,
        model_revision=model_revision,
        serializer_version=serializer_version,
        source_sha256=source_sha256,
        embedding_dimensions=len(vector),
        embedding=vector,
    )
