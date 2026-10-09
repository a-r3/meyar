"""Real process termination and immutable embedding repair under #46."""

import asyncio
import os
import signal
import subprocess
import sys

import pytest
from conftest import TEST_DATABASE_URL
from fakes import FakeEmbeddingProvider, FakeLLMProvider
from sqlalchemy import select, text
from test_identity_embedding_cli import _seed_document

from meyar.embedding.serializer import SERIALIZER_VERSION, embedding_source
from meyar.extraction.identity_service import extract_candidate_identity
from meyar.extraction.service import extract_candidate_profile
from meyar.models.audit_event import AuditEvent
from meyar.models.candidate_embedding_version import CandidateEmbeddingVersion
from meyar.models.candidate_identity_version import CandidateIdentityVersion
from meyar.models.candidate_profile_version import CandidateProfileVersion
from meyar.schemas.candidate_identity import CandidateIdentityExtraction
from meyar.schemas.candidate_profile import CandidateProfileExtraction, EvidenceRef, SkillItem
from meyar.services.candidate_embedding_repo import EmbeddingCompatibility, create_embedding_version
from meyar.services.candidate_embedding_service import (
    EmbeddingPreconditionError,
    embed_candidate_profile,
)


def providers():
    llm = FakeLLMProvider(
        extraction=CandidateProfileExtraction(
            skills=[
                SkillItem(
                    name="Python",
                    evidence=[EvidenceRef(page=1, block_index=0, quote="Python")],
                )
            ]
        ),
        identity_extraction=CandidateIdentityExtraction(),
    )
    embedding = FakeEmbeddingProvider(vector=[1.0, 0.0])
    compatibility = EmbeddingCompatibility(
        embedding.provider_name,
        embedding.model_name,
        embedding.model_revision,
        SERIALIZER_VERSION,
        2,
    )
    return llm, embedding, compatibility


async def profile(db, tenant, candidate, document, llm):
    return await extract_candidate_profile(
        db,
        llm,
        tenant_id=tenant,
        candidate_id=candidate,
        candidate_document=document,
        model_provider_name="fake",
        max_input_chars=20000,
    )


@pytest.mark.parametrize("stage", ["profile", "identity", "embedding"])
async def test_sigkill_inference_durable_started_no_partial_version_and_retry(
    db_session, tenant_and_key, tmp_path, stage
):
    tenant_id = tenant_and_key[0].id
    candidate, document, _ = await _seed_document(db_session, tenant_id)
    candidate_id, document_id = candidate.id, document.id
    llm, embedding, compatibility = providers()
    await db_session.commit()
    if stage == "embedding":
        await profile(db_session, tenant_id, candidate_id, document, llm)
        await db_session.commit()
    source = """import asyncio,signal,sys,uuid
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from meyar.services.candidate_document_repo import get_candidate_document
from meyar.extraction.service import extract_candidate_profile
from meyar.extraction.identity_service import extract_candidate_identity
from meyar.services.candidate_embedding_service import embed_candidate_profile
from meyar.services.candidate_embedding_repo import EmbeddingCompatibility
from meyar.embedding.serializer import SERIALIZER_VERSION
class Gate:
 provider_name='fake';model_name='synthetic';model_revision=''
 async def wait(self,*args):
  print('INFERENCE',flush=True);signal.pause()
 extract_candidate_profile=wait;extract_candidate_identity=wait;embed=wait
async def run():
 e=create_async_engine(sys.argv[1],connect_args={'server_settings':{'application_name':'meyar_46_kill'}})
 f=async_sessionmaker(e,expire_on_commit=False)
 async with f() as db:
  t,c,d=map(uuid.UUID,sys.argv[2:5]);stage=sys.argv[5]
  document=await get_candidate_document(db,tenant_id=t,candidate_id=c,document_id=d)
  if stage=='embedding':
   await embed_candidate_profile(db,Gate(),tenant_id=t,candidate_id=c,max_input_chars=20000,
    compatibility=EmbeddingCompatibility('fake','synthetic','',SERIALIZER_VERSION,2))
  else:
   function=extract_candidate_profile if stage=='profile' else extract_candidate_identity
   await function(db,Gate(),tenant_id=t,candidate_id=c,candidate_document=document,
    model_provider_name='fake',max_input_chars=20000)
asyncio.run(run())
"""
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            source,
            TEST_DATABASE_URL,
            str(tenant_id),
            str(candidate_id),
            str(document_id),
            stage,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), 10) == "INFERENCE\n"
        state = (
            await db_session.execute(
                text(
                    "SELECT state,xact_start FROM pg_stat_activity "
                    "WHERE application_name='meyar_46_kill'"
                )
            )
        ).one()
        assert state.state == "idle" and state.xact_start is None
        os.kill(child.pid, signal.SIGKILL)
        await asyncio.wait_for(asyncio.to_thread(child.wait), 10)
        assert child.returncode == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
    model = {
        "profile": CandidateProfileVersion,
        "identity": CandidateIdentityVersion,
        "embedding": CandidateEmbeddingVersion,
    }[stage]
    assert (
        await db_session.scalars(select(model).where(model.candidate_id == candidate_id))
    ).all() == []
    event_type = (
        "CANDIDATE_EMBEDDING_STARTED"
        if stage == "embedding"
        else f"CANDIDATE_{stage.upper()}_EXTRACTION_STARTED"
    )
    starts = (
        await db_session.scalars(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenant_id,
                AuditEvent.event_type == event_type,
            )
        )
    ).all()
    assert len(starts) == 1
    from datetime import UTC, datetime, timedelta

    from meyar.services.maintenance import MaintenancePolicy, run_maintenance

    inspection = await run_maintenance(
        db_session, tenant_id=tenant_id, storage_root=tmp_path,
        policy=MaintenancePolicy(), now=datetime.now(UTC) + timedelta(hours=3),
    )
    assert inspection.counts[f"UNFINISHED_{stage.upper()}"] == 1
    assert inspection.exit_code == 0
    await db_session.commit()
    if stage == "embedding":
        result, reused = await embed_candidate_profile(
            db_session,
            embedding,
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            max_input_chars=20000,
            compatibility=compatibility,
        )
        assert not reused and result.embedding_dimensions == 2
    elif stage == "profile":
        assert (
            await profile(db_session, tenant_id, candidate_id, document, llm)
        ).status == "COMPLETED"
    else:
        result = await extract_candidate_identity(
            db_session,
            llm,
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            candidate_document=document,
            model_provider_name="fake",
            max_input_chars=20000,
        )
        assert result.status == "COMPLETED"
    await db_session.commit()


async def test_historical_incompatible_embedding_repair_preserves_immutable_history(
    db_session, tenant_and_key
):
    tenant_id = tenant_and_key[0].id
    candidate, document, _ = await _seed_document(db_session, tenant_id)
    llm, embedding, compatibility = providers()
    version = await profile(db_session, tenant_id, candidate.id, document, llm)
    _, source_hash = embedding_source(version.profile_content)
    historical = await create_embedding_version(
        db_session,
        tenant_id=tenant_id,
        candidate_id=candidate.id,
        candidate_profile_version_id=version.id,
        provider=embedding.provider_name,
        model_name=embedding.model_name,
        model_revision=embedding.model_revision,
        serializer_version=SERIALIZER_VERSION,
        source_sha256=source_hash,
        embedding_dimensions=3,
        embedding=[1.0, 0.0, 0.0],
    )
    await db_session.commit()
    with pytest.raises(EmbeddingPreconditionError) as error:
        await embed_candidate_profile(
            db_session,
            embedding,
            tenant_id=tenant_id,
            candidate_id=candidate.id,
            max_input_chars=20000,
            compatibility=compatibility,
        )
    assert error.value.code == "EMBEDDING_IDENTITY_INCOMPATIBLE"
    # Existing operator extraction creates a new immutable profile identity.
    # Do not rewrite dimensions, delete historical rows, or falsify model revision.
    repaired_profile = await profile(db_session, tenant_id, candidate.id, document, llm)
    await db_session.commit()
    repaired, reused = await embed_candidate_profile(
        db_session,
        embedding,
        tenant_id=tenant_id,
        candidate_id=candidate.id,
        max_input_chars=20000,
        compatibility=compatibility,
    )
    await db_session.commit()
    assert not reused and repaired.embedding_dimensions == 2
    assert repaired.candidate_profile_version_id == repaired_profile.id != version.id
    old = await db_session.get(CandidateEmbeddingVersion, historical.id)
    assert old.embedding_dimensions == 3


@pytest.mark.parametrize("stage", ["profile", "identity", "embedding"])
async def test_concurrent_direct_results_have_one_authoritative_version(
    db_session, tenant_and_key, tmp_path, stage
):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from meyar.extraction.identity_service import IdentityExtractionPreconditionError
    from meyar.extraction.service import ExtractionPreconditionError

    tenant_id = tenant_and_key[0].id
    candidate, document, _ = await _seed_document(db_session, tenant_id)
    candidate_id = candidate.id
    llm, embedding, compatibility = providers()
    await db_session.commit()
    if stage == "embedding":
        await profile(db_session, tenant_id, candidate_id, document, llm)
        await db_session.commit()
    reached, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def gate():
        nonlocal count
        count += 1
        if count == 2:
            reached.set()
        await release.wait()

    class LLM(FakeLLMProvider):
        async def extract_candidate_profile(self, view):
            await gate()
            return await super().extract_candidate_profile(view)

        async def extract_candidate_identity(self, view):
            await gate()
            return await super().extract_candidate_identity(view)

    class Embedding(FakeEmbeddingProvider):
        async def embed(self, value):
            await gate()
            return await super().embed(value)

    engine = create_async_engine(TEST_DATABASE_URL, pool_size=2, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    provider = LLM(extraction=llm._extraction, identity_extraction=llm._identity_extraction)
    embedder = Embedding(vector=[1.0, 0.0])

    async def run():
        async with factory() as db:
            if stage == "embedding":
                result = await embed_candidate_profile(
                    db,
                    embedder,
                    tenant_id=tenant_id,
                    candidate_id=candidate_id,
                    max_input_chars=20000,
                    compatibility=compatibility,
                )
            elif stage == "profile":
                result = await profile(db, tenant_id, candidate_id, document, provider)
            else:
                result = await extract_candidate_identity(
                    db,
                    provider,
                    tenant_id=tenant_id,
                    candidate_id=candidate_id,
                    candidate_document=document,
                    model_provider_name="fake",
                    max_input_chars=20000,
                )
            await db.commit()
            return result

    tasks = [asyncio.create_task(run()) for _ in range(2)]
    try:
        await asyncio.wait_for(reached.wait(), 10)
        assert engine.sync_engine.pool.checkedout() == 0
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)
        if stage == "embedding":
            assert all(not isinstance(r, Exception) for r in results)
            assert results[0][0].id == results[1][0].id
            assert sorted(r[1] for r in results) == [False, True]
        else:
            errors = [r for r in results if isinstance(r, Exception)]
            assert len(errors) == 1
            assert isinstance(
                errors[0], (ExtractionPreconditionError, IdentityExtractionPreconditionError)
            )
            assert errors[0].code == "INFERENCE_AUTHORITY_CHANGED"
        model = {
            "profile": CandidateProfileVersion,
            "identity": CandidateIdentityVersion,
            "embedding": CandidateEmbeddingVersion,
        }[stage]
        assert (
            len(
                (
                    await db_session.scalars(
                        select(model).where(
                            model.candidate_id == candidate_id,
                        )
                    )
                ).all()
            )
            == 1
        )
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()
