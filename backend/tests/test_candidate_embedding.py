"""Slice 7 — local embeddings + pgvector persistence. Synthetic content
only. See docs/MASTER_SPEC.md §15 and .claude/rules/testing.md."""

import uuid

import httpx
import pytest
from fakes import FakeEmbeddingProvider
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.embedding.ollama_provider import OllamaEmbeddingProvider
from meyar.embedding.provider import (
    EmbeddingInvalidOutputError,
    EmbeddingTimeoutError,
    EmbeddingUnavailableError,
)
from meyar.embedding.serializer import build_professional_embedding_text, compute_source_sha256
from meyar.models.audit_event import AuditEvent
from meyar.models.candidate_embedding_version import CandidateEmbeddingVersion
from meyar.services.candidate_document_repo import (
    create_candidate_document,
    create_canonical_document,
)
from meyar.services.candidate_embedding_repo import (
    create_embedding_version,
    get_embedding_version_by_source,
    list_embedding_versions_for_candidate,
)
from meyar.services.candidate_embedding_service import (
    EmbeddingPreconditionError,
    embed_candidate_profile,
)
from meyar.services.candidate_profile_repo import create_profile_version
from meyar.services.candidate_repo import create_candidate
from meyar.services.tenant_repo import create_tenant

_PROFILE_V1_CONTENT = {
    "skills": [{"name": "Python", "category": None}],
    "employment_history": [
        {
            "title": "Backend Developer",
            "organization": "Acme",
            "start_date": "2021",
            "end_date": "2025",
            "is_current": False,
        }
    ],
    "education": [],
    "certifications": [],
    "languages": [],
    "projects": [],
}

_PROFILE_V2_CONTENT = {
    "skills": [{"name": "Rust", "category": None}],
    "employment_history": [],
    "education": [],
    "certifications": [],
    "languages": [],
    "projects": [],
}


async def _seed_candidate_and_document(db_session: AsyncSession, tenant_id: uuid.UUID):
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
        content={"pages": [{"page": 1, "blocks": [{"index": 0, "text": "Python developer."}]}]},
    )
    return candidate, document, canonical


async def _seed_profile_version(
    db_session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    document_id: uuid.UUID,
    canonical_id: uuid.UUID,
    profile_content: dict,
    status: str = "COMPLETED",
):
    return await create_profile_version(
        db_session,
        tenant_id=tenant_id,
        candidate_id=candidate_id,
        candidate_document_id=document_id,
        canonical_document_id=canonical_id,
        source_sha256="a" * 64,
        schema_version="candidate-profile-v1",
        prompt_version="candidate-profile-extraction-v1",
        model_provider="fake",
        model_name="fake-model",
        model_metadata={},
        status=status,
        profile_content=profile_content if status == "COMPLETED" else None,
    )


@pytest.fixture
async def candidate_with_profile_v1(db_session: AsyncSession):
    tenant = await create_tenant(db_session, name=f"EmbTenant-{uuid.uuid4().hex[:8]}")
    await db_session.commit()
    candidate, document, canonical = await _seed_candidate_and_document(db_session, tenant.id)
    profile_v1 = await _seed_profile_version(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        document_id=document.id,
        canonical_id=canonical.id,
        profile_content=_PROFILE_V1_CONTENT,
    )
    await db_session.commit()
    return tenant, candidate, document, canonical, profile_v1


# --- Serializer -------------------------------------------------------


def test_serializer_is_deterministic_same_content_same_hash() -> None:
    text_a = build_professional_embedding_text(_PROFILE_V1_CONTENT)
    text_b = build_professional_embedding_text(dict(_PROFILE_V1_CONTENT))  # fresh dict, same data
    assert text_a == text_b
    assert compute_source_sha256(text_a) == compute_source_sha256(text_b)


def test_serializer_different_content_different_hash() -> None:
    text_a = build_professional_embedding_text(_PROFILE_V1_CONTENT)
    text_b = build_professional_embedding_text(_PROFILE_V2_CONTENT)
    assert text_a != text_b
    assert compute_source_sha256(text_a) != compute_source_sha256(text_b)


def test_serializer_excludes_identity_fields() -> None:
    """Structural guarantee: CandidateProfileExtraction (the source of
    profile_content) has no name/email/phone field to begin with, so the
    serializer can never emit one even if such a key were smuggled into
    the dict."""
    poisoned = dict(_PROFILE_V1_CONTENT)
    poisoned["full_name"] = "Jane Synthetic Doe"  # not a real schema field
    poisoned["email"] = "jane@example.com"
    text = build_professional_embedding_text(poisoned)
    assert "Jane Synthetic Doe" not in text
    assert "jane@example.com" not in text


# --- Service: idempotency / staleness / history ------------------------


async def test_first_embed_creates_record(
    db_session: AsyncSession, candidate_with_profile_v1
) -> None:
    tenant, candidate, _document, _canonical, profile_v1 = candidate_with_profile_v1
    provider = FakeEmbeddingProvider(dimensions=8)

    version, was_reused = await embed_candidate_profile(
        db_session,
        provider,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        max_input_chars=20000,
    )
    await db_session.commit()

    assert was_reused is False
    assert version.candidate_profile_version_id == profile_v1.id
    assert version.embedding_dimensions == 8
    assert len(version.embedding) == 8
    assert provider.call_count == 1


async def test_identical_rerun_is_idempotent_no_duplicate_call(
    db_session: AsyncSession, candidate_with_profile_v1
) -> None:
    tenant, candidate, _document, _canonical, _profile_v1 = candidate_with_profile_v1
    provider = FakeEmbeddingProvider(dimensions=8)

    first, first_reused = await embed_candidate_profile(
        db_session,
        provider,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        max_input_chars=20000,
    )
    await db_session.commit()

    second, second_reused = await embed_candidate_profile(
        db_session,
        provider,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        max_input_chars=20000,
    )
    await db_session.commit()

    assert first_reused is False
    assert second_reused is True
    assert second.id == first.id
    assert provider.call_count == 1  # provider never called again

    rows = await list_embedding_versions_for_candidate(
        db_session, tenant_id=tenant.id, candidate_id=candidate.id
    )
    assert len(rows) == 1


async def test_db_uniqueness_is_the_concurrency_backstop(
    db_session: AsyncSession, candidate_with_profile_v1
) -> None:
    """Bypasses the service's own pre-check to prove the DB constraint
    itself rejects a duplicate (tenant, profile_version, provider, model,
    revision) row — the final backstop for concurrent/repeated writes."""
    tenant, candidate, _document, _canonical, profile_v1 = candidate_with_profile_v1
    await create_embedding_version(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        candidate_profile_version_id=profile_v1.id,
        provider="fake-embedding",
        model_name="fake-embedding-model-v1",
        model_revision="",
        serializer_version="candidate-professional-embedding-text-v1",
        source_sha256="c" * 64,
        embedding_dimensions=4,
        embedding=[0.1, 0.2, 0.3, 0.4],
    )
    await db_session.commit()

    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        await create_embedding_version(
            db_session,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
            candidate_profile_version_id=profile_v1.id,
            provider="fake-embedding",
            model_name="fake-embedding-model-v1",
            model_revision="",
            serializer_version="candidate-professional-embedding-text-v1",
            source_sha256="d" * 64,
            embedding_dimensions=4,
            embedding=[0.9, 0.9, 0.9, 0.9],
        )
    await db_session.rollback()


async def test_new_profile_version_makes_prior_embedding_stale(
    db_session: AsyncSession, candidate_with_profile_v1
) -> None:
    tenant, candidate, document, canonical, profile_v1 = candidate_with_profile_v1
    provider = FakeEmbeddingProvider(dimensions=8)

    embedding_v1, _ = await embed_candidate_profile(
        db_session,
        provider,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        max_input_chars=20000,
    )
    await db_session.commit()

    # profile v1 -> embedding v1 is current for profile v1.
    current_for_v1 = await get_embedding_version_by_source(
        db_session,
        tenant_id=tenant.id,
        candidate_profile_version_id=profile_v1.id,
        provider=provider.provider_name,
        model_name=provider.model_name,
        model_revision=provider.model_revision,
    )
    assert current_for_v1 is not None
    assert current_for_v1.id == embedding_v1.id

    # A new profile version is created — embedding v1 must NOT be
    # silently treated as current for it.
    profile_v2 = await _seed_profile_version(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        document_id=document.id,
        canonical_id=canonical.id,
        profile_content=_PROFILE_V2_CONTENT,
    )
    await db_session.commit()
    assert profile_v2.version_number == 2

    stale_check = await get_embedding_version_by_source(
        db_session,
        tenant_id=tenant.id,
        candidate_profile_version_id=profile_v2.id,
        provider=provider.provider_name,
        model_name=provider.model_name,
        model_revision=provider.model_revision,
    )
    assert stale_check is None  # no embedding exists yet for the new version

    # Embedding against the new current profile version creates v2.
    embedding_v2, was_reused = await embed_candidate_profile(
        db_session,
        provider,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        max_input_chars=20000,
    )
    await db_session.commit()

    assert was_reused is False
    assert embedding_v2.candidate_profile_version_id == profile_v2.id
    assert embedding_v2.id != embedding_v1.id

    # History preserved: both rows still exist.
    rows = await list_embedding_versions_for_candidate(
        db_session, tenant_id=tenant.id, candidate_id=candidate.id
    )
    assert {r.id for r in rows} == {embedding_v1.id, embedding_v2.id}
    reloaded_v1 = await db_session.get(CandidateEmbeddingVersion, embedding_v1.id)
    assert reloaded_v1 is not None
    assert reloaded_v1.candidate_profile_version_id == profile_v1.id  # untouched


async def test_different_model_creates_distinct_provenance(
    db_session: AsyncSession, candidate_with_profile_v1
) -> None:
    tenant, candidate, _document, _canonical, profile_v1 = candidate_with_profile_v1
    provider_a = FakeEmbeddingProvider(dimensions=8, model_name="model-a")
    provider_b = FakeEmbeddingProvider(dimensions=16, model_name="model-b")

    version_a, _ = await embed_candidate_profile(
        db_session,
        provider_a,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        max_input_chars=20000,
    )
    await db_session.commit()
    version_b, _ = await embed_candidate_profile(
        db_session,
        provider_b,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        max_input_chars=20000,
    )
    await db_session.commit()

    assert version_a.id != version_b.id
    assert version_a.model_name == "model-a"
    assert version_b.model_name == "model-b"
    assert version_a.embedding_dimensions == 8
    assert version_b.embedding_dimensions == 16
    rows = await list_embedding_versions_for_candidate(
        db_session, tenant_id=tenant.id, candidate_id=candidate.id
    )
    assert len(rows) == 2  # not mixed/collapsed despite same profile version


async def test_provider_error_handled_safely_no_row_persisted(
    db_session: AsyncSession, candidate_with_profile_v1
) -> None:
    tenant, candidate, _document, _canonical, _profile_v1 = candidate_with_profile_v1
    provider = FakeEmbeddingProvider(error=EmbeddingUnavailableError("connection refused"))

    with pytest.raises(EmbeddingUnavailableError):
        await embed_candidate_profile(
            db_session,
            provider,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
            max_input_chars=20000,
        )
    await db_session.commit()

    rows = await list_embedding_versions_for_candidate(
        db_session, tenant_id=tenant.id, candidate_id=candidate.id
    )
    assert rows == []


async def test_no_profile_version_precondition_error(db_session: AsyncSession) -> None:
    tenant = await create_tenant(db_session, name=f"EmbTenant-{uuid.uuid4().hex[:8]}")
    await db_session.commit()
    candidate = await create_candidate(db_session, tenant_id=tenant.id)
    await db_session.commit()
    provider = FakeEmbeddingProvider()

    with pytest.raises(EmbeddingPreconditionError) as exc_info:
        await embed_candidate_profile(
            db_session,
            provider,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
            max_input_chars=20000,
        )
    assert exc_info.value.code == "NO_PROFILE_VERSION"


async def test_profile_not_completed_precondition_error(db_session: AsyncSession) -> None:
    tenant = await create_tenant(db_session, name=f"EmbTenant-{uuid.uuid4().hex[:8]}")
    await db_session.commit()
    candidate, document, canonical = await _seed_candidate_and_document(db_session, tenant.id)
    await _seed_profile_version(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        document_id=document.id,
        canonical_id=canonical.id,
        profile_content=_PROFILE_V1_CONTENT,
        status="FAILED",
    )
    await db_session.commit()
    provider = FakeEmbeddingProvider()

    with pytest.raises(EmbeddingPreconditionError) as exc_info:
        await embed_candidate_profile(
            db_session,
            provider,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
            max_input_chars=20000,
        )
    assert exc_info.value.code == "PROFILE_NOT_COMPLETED"


async def test_oversized_input_precondition_error(
    db_session: AsyncSession, candidate_with_profile_v1
) -> None:
    tenant, candidate, _document, _canonical, _profile_v1 = candidate_with_profile_v1
    provider = FakeEmbeddingProvider()

    with pytest.raises(EmbeddingPreconditionError) as exc_info:
        await embed_candidate_profile(
            db_session,
            provider,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
            max_input_chars=1,
        )
    assert exc_info.value.code == "INPUT_TOO_LARGE"
    assert provider.call_count == 0


async def test_tenant_isolation_embeddings_never_leak(
    db_session: AsyncSession, candidate_with_profile_v1
) -> None:
    tenant_a, candidate, _document, _canonical, _profile_v1 = candidate_with_profile_v1
    tenant_b = await create_tenant(db_session, name=f"EmbTenantB-{uuid.uuid4().hex[:8]}")
    await db_session.commit()
    provider = FakeEmbeddingProvider()

    version, _ = await embed_candidate_profile(
        db_session,
        provider,
        tenant_id=tenant_a.id,
        candidate_id=candidate.id,
        max_input_chars=20000,
    )
    await db_session.commit()

    leaked = await get_embedding_version_by_source(
        db_session,
        tenant_id=tenant_b.id,
        candidate_profile_version_id=version.candidate_profile_version_id,
        provider=provider.provider_name,
        model_name=provider.model_name,
        model_revision=provider.model_revision,
    )
    assert leaked is None

    leaked_rows = await list_embedding_versions_for_candidate(
        db_session, tenant_id=tenant_b.id, candidate_id=candidate.id
    )
    assert leaked_rows == []


async def test_embedding_audit_metadata_has_no_pii_or_vector(
    db_session: AsyncSession, candidate_with_profile_v1
) -> None:
    tenant, candidate, _document, _canonical, _profile_v1 = candidate_with_profile_v1
    provider = FakeEmbeddingProvider(dimensions=4, vector=[0.123456, 0.654321, 0.1, 0.9])

    await embed_candidate_profile(
        db_session,
        provider,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        max_input_chars=20000,
    )
    await db_session.commit()

    result = await db_session.execute(select(AuditEvent).where(AuditEvent.tenant_id == tenant.id))
    events = result.scalars().all()
    assert any(e.event_type == "CANDIDATE_EMBEDDING_CREATED" for e in events)
    for event in events:
        metadata_str = str(event.event_metadata)
        assert "0.123456" not in metadata_str
        assert "Python developer" not in metadata_str


# --- Provider: HTTP behavior (mock transport, no real Ollama) ----------


def _mock_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def test_ollama_embedding_provider_rejects_non_loopback_url() -> None:
    with pytest.raises(ValueError, match="loopback"):
        OllamaEmbeddingProvider(
            base_url="http://example.com:11434", model="nomic-embed-text", timeout_seconds=5.0
        )


async def test_ollama_embedding_provider_accepts_loopback_url() -> None:
    OllamaEmbeddingProvider(
        base_url="http://127.0.0.1:11434", model="nomic-embed-text", timeout_seconds=5.0
    )


async def test_ollama_embedding_provider_valid_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embedding": [0.1, 0.2, 0.3]})

    provider = OllamaEmbeddingProvider(
        base_url="http://127.0.0.1:11434",
        model="nomic-embed-text",
        timeout_seconds=5.0,
        transport=_mock_transport(handler),
    )
    result = await provider.embed("some professional text")
    assert result.vector == [0.1, 0.2, 0.3]
    assert result.dimensions == 3
    assert result.provider == "ollama"
    assert result.model_name == "nomic-embed-text"


async def test_ollama_embedding_provider_rejects_empty_vector() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embedding": []})

    provider = OllamaEmbeddingProvider(
        base_url="http://127.0.0.1:11434",
        model="nomic-embed-text",
        timeout_seconds=5.0,
        transport=_mock_transport(handler),
    )
    with pytest.raises(EmbeddingInvalidOutputError):
        await provider.embed("text")


async def test_ollama_embedding_provider_rejects_missing_vector() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    provider = OllamaEmbeddingProvider(
        base_url="http://127.0.0.1:11434",
        model="nomic-embed-text",
        timeout_seconds=5.0,
        transport=_mock_transport(handler),
    )
    with pytest.raises(EmbeddingInvalidOutputError):
        await provider.embed("text")


async def test_ollama_embedding_provider_rejects_nan_values() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # httpx's json= convenience arg rejects NaN at encode time (Python's
        # default json.dumps behavior) — send raw bytes instead, since
        # json.loads (used by response.json()) accepts NaN by default,
        # matching what a real malformed server response could contain.
        return httpx.Response(
            200,
            content=b'{"embedding": [0.1, NaN, 0.3]}',
            headers={"content-type": "application/json"},
        )

    provider = OllamaEmbeddingProvider(
        base_url="http://127.0.0.1:11434",
        model="nomic-embed-text",
        timeout_seconds=5.0,
        transport=_mock_transport(handler),
    )
    with pytest.raises(EmbeddingInvalidOutputError):
        await provider.embed("text")


async def test_ollama_embedding_provider_rejects_non_numeric_values() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embedding": [0.1, "not-a-number", 0.3]})

    provider = OllamaEmbeddingProvider(
        base_url="http://127.0.0.1:11434",
        model="nomic-embed-text",
        timeout_seconds=5.0,
        transport=_mock_transport(handler),
    )
    with pytest.raises(EmbeddingInvalidOutputError):
        await provider.embed("text")


async def test_ollama_embedding_provider_non_200_raises_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    provider = OllamaEmbeddingProvider(
        base_url="http://127.0.0.1:11434",
        model="nomic-embed-text",
        timeout_seconds=5.0,
        transport=_mock_transport(handler),
    )
    with pytest.raises(EmbeddingUnavailableError):
        await provider.embed("text")


async def test_ollama_embedding_provider_connect_error_raises_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    provider = OllamaEmbeddingProvider(
        base_url="http://127.0.0.1:11434",
        model="nomic-embed-text",
        timeout_seconds=5.0,
        transport=_mock_transport(handler),
    )
    with pytest.raises(EmbeddingUnavailableError):
        await provider.embed("text")


async def test_ollama_embedding_provider_timeout_raises_timeout_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    provider = OllamaEmbeddingProvider(
        base_url="http://127.0.0.1:11434",
        model="nomic-embed-text",
        timeout_seconds=5.0,
        transport=_mock_transport(handler),
    )
    with pytest.raises(EmbeddingTimeoutError):
        await provider.embed("text")
