"""#46: real PostgreSQL observation while synthetic inference is gated."""

import asyncio

import pytest
from conftest import TEST_DATABASE_URL
from fakes import FakeEmbeddingProvider, FakeLLMProvider
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_identity_embedding_cli import _seed_document

from meyar.embedding.serializer import SERIALIZER_VERSION
from meyar.extraction.identity_service import (
    IdentityExtractionPreconditionError,
    extract_candidate_identity,
)
from meyar.extraction.service import ExtractionPreconditionError, extract_candidate_profile
from meyar.models.candidate import Candidate
from meyar.models.candidate_embedding_version import CandidateEmbeddingVersion
from meyar.models.candidate_identity_version import CandidateIdentityVersion
from meyar.models.candidate_profile_version import CandidateProfileVersion
from meyar.models.canonical_document import CanonicalDocument
from meyar.models.tenant import Tenant
from meyar.schemas.candidate_identity import CandidateIdentityExtraction
from meyar.schemas.candidate_profile import CandidateProfileExtraction, EvidenceRef, SkillItem
from meyar.services.candidate_document_repo import (
    create_candidate_document,
    create_canonical_document,
)
from meyar.services.candidate_embedding_repo import EmbeddingCompatibility
from meyar.services.candidate_embedding_service import (
    EmbeddingPreconditionError,
    embed_candidate_profile,
)
from meyar.services.candidate_profile_repo import create_profile_version
from meyar.services.tenant_authority import TenantInactiveError


@pytest.mark.parametrize("stage", ["profile", "identity", "embedding"])
@pytest.mark.parametrize(
    "mutation", [None, "delete", "document", "canonical", "profile", "candidate", "tenant"]
)
async def test_direct_inference_releases_transaction_connection_and_locks(
    db_session, tenant_and_key, stage, mutation
):
    tenant, _, _ = tenant_and_key
    candidate, document, canonical = await _seed_document(db_session, tenant.id)
    extraction = CandidateProfileExtraction(
        skills=[
            SkillItem(
                name="Python",
                evidence=[EvidenceRef(page=1, block_index=0, quote="Python")],
            )
        ]
    )
    if stage == "embedding":
        await create_profile_version(
            db_session,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
            candidate_document_id=document.id,
            canonical_document_id=canonical.id,
            source_sha256=document.sha256_hash,
            schema_version="candidate-profile-v1",
            prompt_version="synthetic",
            model_provider="fake",
            model_name="synthetic",
            model_metadata={},
            status="COMPLETED",
            profile_content=extraction.model_dump(mode="json"),
        )
    await db_session.commit()
    entered, release = asyncio.Event(), asyncio.Event()

    class GateLLM(FakeLLMProvider):
        async def extract_candidate_profile(self, view):
            entered.set()
            await release.wait()
            return await super().extract_candidate_profile(view)

        async def extract_candidate_identity(self, view):
            entered.set()
            await release.wait()
            return await super().extract_candidate_identity(view)

    class GateEmbedding(FakeEmbeddingProvider):
        async def embed(self, value):
            entered.set()
            await release.wait()
            return await super().embed(value)

    engine = create_async_engine(
        TEST_DATABASE_URL,
        pool_size=1,
        max_overflow=0,
        connect_args={"server_settings": {"application_name": "meyar_46_direct"}},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    llm = GateLLM(extraction=extraction, identity_extraction=CandidateIdentityExtraction())
    embedding = GateEmbedding(vector=[1.0, 0.0])

    async def run():
        async with factory() as db:
            if stage == "embedding":
                result = await embed_candidate_profile(
                    db,
                    embedding,
                    tenant_id=tenant.id,
                    candidate_id=candidate.id,
                    max_input_chars=20000,
                    compatibility=EmbeddingCompatibility(
                        provider=embedding.provider_name,
                        model_name=embedding.model_name,
                        model_revision=embedding.model_revision,
                        serializer_version=SERIALIZER_VERSION,
                        embedding_dimensions=2,
                    ),
                )
            else:
                function = (
                    extract_candidate_profile if stage == "profile" else extract_candidate_identity
                )
                result = await function(
                    db,
                    llm,
                    tenant_id=tenant.id,
                    candidate_id=candidate.id,
                    candidate_document=document,
                    model_provider_name="fake",
                    max_input_chars=20000,
                )
            await db.commit()
            return result

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(entered.wait(), 10)
        rows = (
            await db_session.execute(
                text(
                    "SELECT state, xact_start, "
                    "(SELECT count(*) FROM pg_locks l WHERE l.pid=a.pid "
                    "AND l.locktype IN ('relation','tuple','transactionid')) AS locks "
                    "FROM pg_stat_activity a WHERE application_name='meyar_46_direct'"
                )
            )
        ).all()
        assert rows and all(
            r.state == "idle" and r.xact_start is None and r.locks == 0 for r in rows
        ), rows
        assert engine.sync_engine.pool.checkedout() == 0
        if mutation == "delete":
            await db_session.execute(delete(Candidate).where(Candidate.id == candidate.id))
        elif mutation == "document":
            newer = await create_candidate_document(
                db_session,
                tenant_id=tenant.id,
                candidate_id=candidate.id,
                original_filename="synthetic-new.pdf",
                mime_type="application/pdf",
                byte_size=12,
                sha256_hash="b" * 64,
                storage_key="synthetic/new",
            )
            await create_canonical_document(
                db_session,
                tenant_id=tenant.id,
                candidate_document_id=newer.id,
                parser_name="synthetic",
                parser_version="1",
                language=None,
                content=canonical.content,
            )
        elif mutation == "canonical":
            await db_session.execute(
                update(CanonicalDocument)
                .where(
                    CanonicalDocument.id == canonical.id,
                )
                .values(content={"pages": []})
            )
        elif mutation == "profile":
            await create_profile_version(
                db_session,
                tenant_id=tenant.id,
                candidate_id=candidate.id,
                candidate_document_id=document.id,
                canonical_document_id=canonical.id,
                source_sha256=document.sha256_hash,
                schema_version="candidate-profile-v1",
                prompt_version="synthetic",
                model_provider="fake",
                model_name="synthetic",
                model_metadata={},
                status="FAILED",
            )
        elif mutation == "candidate":
            await db_session.execute(
                update(Candidate).where(Candidate.id == candidate.id).values(status="ARCHIVED")
            )
        elif mutation == "tenant":
            await db_session.execute(
                update(Tenant).where(Tenant.id == tenant.id).values(is_active=False)
            )
        await db_session.commit()
    finally:
        release.set()
        try:
            if mutation:
                with pytest.raises(
                    (
                        ExtractionPreconditionError,
                        IdentityExtractionPreconditionError,
                        EmbeddingPreconditionError,
                        TenantInactiveError,
                    )
                ):
                    await asyncio.wait_for(task, 10)
            else:
                await asyncio.wait_for(task, 10)
        finally:
            await engine.dispose()
    if mutation:
        model = {
            "profile": CandidateProfileVersion,
            "identity": CandidateIdentityVersion,
            "embedding": CandidateEmbeddingVersion,
        }[stage]
        versions = (
            await db_session.scalars(
                select(model).where(
                    model.candidate_id == candidate.id,
                )
            )
        ).all()
        expected = 1 if stage == "embedding" and mutation != "delete" else 0
        # Embedding baseline is a profile, not an embedding row.
        if stage == "embedding":
            expected = 0
        elif stage == "profile" and mutation == "profile":
            expected = 1
        assert len(versions) == expected


@pytest.mark.parametrize("command", ["search", "plan_execute"])
async def test_query_embedding_cli_releases_real_pool_and_pg_locks(
    db_session, tenant_and_key, tmp_path, monkeypatch, command
):
    from meyar import cli
    from meyar.search.planner_schemas import PlannerDraft
    from meyar.search.schemas import CandidateSearchRequest, EmbeddingSearchConfig, SearchMode

    tenant_id = tenant_and_key[0].id
    await db_session.commit()
    entered, release = asyncio.Event(), asyncio.Event()

    class Gate(FakeEmbeddingProvider):
        async def embed(self, value):
            entered.set()
            await release.wait()
            return await super().embed(value)

    provider = Gate(vector=[1.0, 0.0])
    config = EmbeddingSearchConfig(
        provider=provider.provider_name, model_name=provider.model_name,
        model_revision=provider.model_revision, serializer_version=SERIALIZER_VERSION,
        embedding_dimensions=2,
    )
    engine = create_async_engine(
        TEST_DATABASE_URL, pool_size=1, max_overflow=0,
        connect_args={"server_settings": {"application_name": "meyar_46_query_cli"}},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(cli, "get_session_factory", lambda: factory)
    monkeypatch.setattr(cli, "get_embedding_provider", lambda: provider)
    monkeypatch.setattr(cli, "get_embedding_search_config", lambda: config)
    query = "modernizing legacy backend systems"
    monkeypatch.setattr(cli, "get_llm_provider", lambda: FakeLLMProvider(
        planner_draft=PlannerDraft(semantic_query=query),
    ))
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY, semantic_query=query, embedding_config=config,
    )
    path = tmp_path / "synthetic-request.json"
    path.write_text(request.model_dump_json())
    task = asyncio.create_task(
        cli._search_candidates(str(tenant_id), str(path)) if command == "search" else
        cli._plan_search(str(tenant_id), query, "2026-08-23", execute=True)
    )
    try:
        await asyncio.wait_for(entered.wait(), 10)
        rows = (await db_session.execute(text(
            "SELECT state,xact_start,(SELECT count(*) FROM pg_locks l WHERE l.pid=a.pid "
            "AND l.locktype IN ('relation','tuple','transactionid')) AS locks "
            "FROM pg_stat_activity a WHERE application_name='meyar_46_query_cli'"
        ))).all()
        assert rows and all(r.state == "idle" and r.xact_start is None and r.locks == 0
                            for r in rows), rows
        assert engine.sync_engine.pool.checkedout() == 0
    finally:
        release.set()
        await asyncio.wait_for(task, 10)
        await engine.dispose()
