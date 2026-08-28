"""Slice 13 — behavioral privacy guard on audit metadata
(meyar.services.audit_repo.record_event). See docs/SECURITY_PRIVACY.md:
audit metadata may only carry ids/enums/counts/durations/hashes/version
strings — never raw CV text, raw NL/search query text, PII, secrets, raw
LLM output, embedding vectors, or filesystem/storage paths.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.services.audit_repo import record_event
from meyar.services.tenant_repo import create_tenant


@pytest.mark.parametrize(
    "forbidden_metadata",
    [
        {"email": "candidate@example.invalid"},
        {"phone": "+994501234567"},
        {"full_name": "Jane Synthetic Doe"},
        {"raw_query": "python developer baku"},
        {"query": "python developer baku"},
        {"prompt": "You are a helpful assistant..."},
        {"storage_key": "tenant/abcdef1234567890"},
        {"path": "/var/lib/meyar/storage/tenant/file.pdf"},
        {"csrf_token": "deadbeef" * 8},
        {"api_key": "meyar_live_secretvalue"},
        {"embedding_vector": [0.1, 0.2, 0.3]},
        {"llm_output": "raw model completion text"},
    ],
)
async def test_forbidden_metadata_key_is_rejected(
    db_session: AsyncSession, forbidden_metadata: dict
) -> None:
    tenant = await create_tenant(db_session, name=f"AuditGuard-{uuid.uuid4().hex[:8]}")
    await db_session.commit()

    with pytest.raises(ValueError, match="privacy guard"):
        await record_event(
            db_session,
            tenant_id=tenant.id,
            event_type="TEST_EVENT",
            metadata=forbidden_metadata,
        )


async def test_oversized_string_value_is_rejected_even_under_a_safe_key(
    db_session: AsyncSession,
) -> None:
    tenant = await create_tenant(db_session, name=f"AuditGuard-{uuid.uuid4().hex[:8]}")
    await db_session.commit()

    with pytest.raises(ValueError, match="leaked free text"):
        await record_event(
            db_session,
            tenant_id=tenant.id,
            event_type="TEST_EVENT",
            metadata={"error_code": "x" * 301},
        )


async def test_legitimate_ids_hashes_and_enums_are_accepted(
    db_session: AsyncSession,
) -> None:
    tenant = await create_tenant(db_session, name=f"AuditGuard-{uuid.uuid4().hex[:8]}")
    await db_session.commit()

    event = await record_event(
        db_session,
        tenant_id=tenant.id,
        event_type="CANDIDATE_SEARCH_EXECUTED",
        metadata={
            "candidate_id": str(uuid.uuid4()),
            "api_key_id": str(uuid.uuid4()),
            "query_sha256": "a" * 64,
            "prompt_version": "search-planner-v1",
            "mode": "hybrid",
            "result_count": 5,
        },
    )
    assert event.event_metadata["mode"] == "hybrid"
