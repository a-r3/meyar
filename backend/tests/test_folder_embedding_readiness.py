# ruff: noqa: F811
"""Issue #46 S10: folder READY requires an embedding that is compatible with the ACTIVE
embedding configuration (provider / model / revision / serializer / dimensions / canonical
source hash), exactly as semantic retrieval defines compatibility.

Real PostgreSQL, synthetic data, deterministic gated local providers. When only the
embedding side is stale, profile and identity extraction must NOT be rerun and only the
needed embedding is created; historical embeddings are never deleted or flagged.
"""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text, update
from test_folder_downstream_concurrency import (  # noqa: F401  (fixtures + helpers)
    GateEmbedding,
    GateLLM,
    assert_embedding_provenance,
    delete_candidate,
    diverge_dedup_linked_paths,
    env,
    factory,
    folder_rows,
    run_pending,
    seed,
    settled,
    skills_found,
    stop,
    truth,
    version_snapshot,
)

from meyar import cli
from meyar.embedding.serializer import SERIALIZER_VERSION
from meyar.models.candidate_embedding_version import CandidateEmbeddingVersion
from meyar.models.candidate_profile_version import CandidateProfileVersion
from meyar.search.schemas import EmbeddingSearchConfig
from meyar.services.tenant_authority import (
    TenantInactiveError,
    set_tenant_active,
)
from meyar.storage.photo import LocalPhotoStorage


async def processed_once(
    factory, storage, tenant, base, *, model="fake-embedding-model-v1", revision=""
):
    source_id, candidate_id, document_id = await seed(
        factory, storage, tenant.id, base / "cvs", "a.docx", "Java"
    )
    async with factory() as db:
        summary = await run_pending(
            db, GateLLM(), GateEmbedding(model_name=model, model_revision=revision),
            tenant.id, source_id,
        )
    assert summary.ready_after == 1 and summary.failed == 0
    return source_id, candidate_id, document_id


async def embedding_rows(observer, tenant_id):
    await observer.rollback()
    return (
        await observer.scalars(
            select(CandidateEmbeddingVersion).where(
                CandidateEmbeddingVersion.tenant_id == tenant_id
            )
        )
    ).all()


async def converges_with_only_embedding_rework(
    factory, tenant, source_id, *, embedder_kwargs, expected_rows
):
    """First run under the new active config: only the embedding is (re)created; the next
    run quiesces without any inference."""
    async with factory() as db, factory() as observer:
        before = await version_snapshot(observer, tenant.id)
        llm, embedder = GateLLM(), GateEmbedding(**embedder_kwargs)
        summary = await run_pending(db, llm, embedder, tenant.id, source_id)
        assert summary.failed == 0 and summary.superseded == 0, summary
        assert (summary.processed, summary.ready_after) == (1, 1), summary
        assert llm.calls == {"profile": 0, "identity": 0}, "profile/identity were redone"
        assert embedder.call_count == 1, "exactly one compatible embedding is created"
        after = await version_snapshot(observer, tenant.id)
        assert after["CandidateProfileVersion"] == before["CandidateProfileVersion"]
        assert after["CandidateIdentityVersion"] == before["CandidateIdentityVersion"]
        assert len(after["CandidateEmbeddingVersion"]) == expected_rows  # history preserved
        quiet_llm, quiet = GateLLM(), GateEmbedding(**embedder_kwargs)
        again = await run_pending(db, quiet_llm, quiet, tenant.id, source_id)
        assert again.failed == 0 and again.already_ready == again.candidates_considered == 1
        assert quiet_llm.calls == {"profile": 0, "identity": 0} and quiet.call_count == 0
        assert await version_snapshot(observer, tenant.id) == after
        await assert_embedding_provenance(observer, tenant.id)


async def test_old_model_name_is_not_ready_then_reembedded_then_quiesces(env, factory):
    tenant, _, storage, base = env
    source_id, _, _ = await processed_once(factory, storage, tenant, base, model="old-model")
    await converges_with_only_embedding_rework(
        factory, tenant, source_id, embedder_kwargs={"model_name": "new-model"}, expected_rows=2
    )


async def test_changed_model_revision_is_reembedded(env, factory):
    tenant, _, storage, base = env
    source_id, _, _ = await processed_once(factory, storage, tenant, base, revision="")
    await converges_with_only_embedding_rework(
        factory, tenant, source_id, embedder_kwargs={"model_revision": "r2"}, expected_rows=2
    )


@pytest.mark.parametrize("stale", ["serializer", "source_hash"])
async def test_stale_serializer_or_source_hash_is_reembedded(env, factory, stale):
    tenant, _, storage, base = env
    source_id, _, _ = await processed_once(factory, storage, tenant, base)
    async with factory() as db:  # the stored row was produced under an older serialization
        column = (
            {"serializer_version": "candidate-professional-embedding-text-v0"}
            if stale == "serializer"
            else {"source_sha256": "f" * 64}
        )
        await db.execute(update(CandidateEmbeddingVersion).values(**column))
        await db.commit()
    await converges_with_only_embedding_rework(
        factory, tenant, source_id, embedder_kwargs={}, expected_rows=2
    )


async def test_exact_active_embedding_is_ready_with_no_provider_call(env, factory):
    tenant, _, storage, base = env
    source_id, _, _ = await processed_once(factory, storage, tenant, base)
    async with factory() as db, factory() as observer:
        before = await version_snapshot(observer, tenant.id)
        llm, embedder = GateLLM(), GateEmbedding()
        summary = await run_pending(db, llm, embedder, tenant.id, source_id)
        assert summary.already_ready == 1 and summary.failed == 0 and summary.processed == 0
        assert embedder.call_count == 0 and llm.calls == {"profile": 0, "identity": 0}
        assert await version_snapshot(observer, tenant.id) == before


async def test_multiple_historical_embeddings_one_exact_compatible_is_sufficient(env, factory):
    """m1 (old) -> m2 -> m3, then back to m2: m2 is neither newest nor oldest and is enough."""
    tenant, _, storage, base = env
    source_id, _, _ = await processed_once(factory, storage, tenant, base, model="m1")
    for model in ("m2", "m3"):
        async with factory() as db:
            await run_pending(db, GateLLM(), GateEmbedding(model_name=model), tenant.id, source_id)
    async with factory() as db, factory() as observer:
        assert len(await embedding_rows(observer, tenant.id)) == 3
        llm, embedder = GateLLM(), GateEmbedding(model_name="m2")
        summary = await run_pending(db, llm, embedder, tenant.id, source_id)
        assert summary.already_ready == 1 and summary.failed == 0
        assert embedder.call_count == 0 and llm.calls == {"profile": 0, "identity": 0}
        assert len(await embedding_rows(observer, tenant.id)) == 3  # nothing deleted or added


async def test_diverged_tracked_documents_reembed_each_own_profile_and_keep_d100(env, factory):
    tenant, _, storage, base = env
    source_id, candidate_id, _ = await diverge_dedup_linked_paths(
        factory, storage, tenant.id, base, "separate-scans"
    )
    async with factory() as db:
        first = await run_pending(
            db, GateLLM(), GateEmbedding(model_name="old-model"), tenant.id, source_id
        )
        assert first.failed == 0
    async with factory() as db, factory() as observer:
        llm, embedder = GateLLM(), GateEmbedding(model_name="new-model")
        summary = await run_pending(db, llm, embedder, tenant.id, source_id)
        assert summary.failed == 0 and summary.superseded == 0 and summary.processed == 2
        assert embedder.call_count == 2 and llm.calls == {"profile": 0, "identity": 0}
        rows = await embedding_rows(observer, tenant.id)
        assert len(rows) == 4 and {r.model_name for r in rows} == {"old-model", "new-model"}
        for model in ("old-model", "new-model"):  # one per tracked document's own profile
            assert len({r.candidate_profile_version_id for r in rows if r.model_name == model}) == 2
        quiet_llm, quiet = GateLLM(), GateEmbedding(model_name="new-model")
        again = await run_pending(db, quiet_llm, quiet, tenant.id, source_id)
        assert again.failed == 0 and again.already_ready == 2 and quiet.call_count == 0
        # D-100: still exactly one effective profile is searchable.
        python_found = candidate_id in await skills_found(observer, tenant.id, "Python")
        go_found = candidate_id in await skills_found(observer, tenant.id, "Go")
        assert python_found != go_found
        await assert_embedding_provenance(observer, tenant.id)


async def test_embedding_result_with_wrong_dimensions_fails_closed(env, factory):
    from meyar.search.schemas import EmbeddingSearchConfig

    tenant, _, storage, base = env
    source_id, _, _ = await seed(factory, storage, tenant.id, base / "cvs", "a.docx", "Java")
    config = EmbeddingSearchConfig(
        provider="fake-embedding", model_name="fake-embedding-model-v1", model_revision="",
        serializer_version=SERIALIZER_VERSION, embedding_dimensions=8,
    )
    async with factory() as db, factory() as observer:
        summary = await run_pending(
            db, GateLLM(), GateEmbedding(dimensions=4), tenant.id, source_id,
            embedding_config=config,
        )
        assert summary.failed == 1 and summary.ready_after == 0, summary
        assert await embedding_rows(observer, tenant.id) == []  # nothing incompatible persisted
        # Correct dimensions: compatible, ready, and equal to the search compatibility group.
        ok = await run_pending(
            db, GateLLM(), GateEmbedding(dimensions=8), tenant.id, source_id,
            embedding_config=config,
        )
        assert ok.failed == 0 and ok.ready_after == 1
        rows = await embedding_rows(observer, tenant.id)
        assert [r.embedding_dimensions for r in rows] == [8]


@pytest.mark.parametrize("retry_path", ["cli", "folder"])
async def test_direct_cli_wrong_dimensions_does_not_poison_folder_identity(
    env, factory, monkeypatch, capsys, retry_path
):
    """Exercise the production CLI boundary, then the real folder path (PostgreSQL)."""
    tenant, _, storage, base = env
    source_id, candidate_id, _ = await processed_once(
        factory, storage, tenant, base, model="old-model"
    )
    config = EmbeddingSearchConfig(
        provider="fake-embedding", model_name="active-model", model_revision="",
        serializer_version=SERIALIZER_VERSION, embedding_dimensions=8,
    )
    wrong = GateEmbedding(model_name="active-model", dimensions=4)
    monkeypatch.setattr(
        cli, "get_settings", lambda: SimpleNamespace(embedding_max_input_chars=20000)
    )
    monkeypatch.setattr(cli, "get_session_factory", lambda: factory)
    monkeypatch.setattr(cli, "get_embedding_provider", lambda: wrong)
    monkeypatch.setattr(cli, "get_embedding_search_config", lambda: config)
    with pytest.raises(SystemExit) as exc:
        await cli._embed_candidate(str(tenant.id), str(candidate_id))
    assert exc.value.code == 3
    assert capsys.readouterr().out == "Embedding provider failed: EMBEDDING_INVALID_OUTPUT\n"
    assert wrong.call_count == 1
    async with factory() as db, factory() as observer:
        rows = await embedding_rows(observer, tenant.id)
        assert [r.model_name for r in rows] == ["old-model"]  # no poisoned identity
        profile_id = rows[0].candidate_profile_version_id
        before = await version_snapshot(observer, tenant.id)
        from meyar.models.audit_event import AuditEvent
        failures = list(await observer.scalars(select(AuditEvent.event_metadata).where(
            AuditEvent.tenant_id == tenant.id,
            AuditEvent.event_type == "CANDIDATE_EMBEDDING_FAILED",
        )))
        assert failures == [{
            "candidate_id": str(candidate_id),
            "profile_version_id": str(profile_id),
            "error_code": "EMBEDDING_INVALID_OUTPUT",
        }]
        repair = GateEmbedding(model_name="active-model", dimensions=8)
        monkeypatch.setattr(cli, "get_embedding_provider", lambda: repair)
        if retry_path == "cli":
            await cli._embed_candidate(str(tenant.id), str(candidate_id))
            output = capsys.readouterr().out
            assert "Dimensions: 8" in output and "Reused existing embedding: False" in output
        summary = await run_pending(
            db, GateLLM(), repair, tenant.id, source_id, embedding_config=config
        )
        assert (summary.failed, summary.ready_after) == (0, 1)
        assert repair.call_count == 1
        rows = await embedding_rows(observer, tenant.id)
        assert sorted(r.embedding_dimensions for r in rows) == [2, 8]
        after = await version_snapshot(observer, tenant.id)
        for name in ("CandidateProfileVersion", "CandidateIdentityVersion"):
            assert after[name] == before[name]
        await cli._embed_candidate(str(tenant.id), str(candidate_id))
        assert "Reused existing embedding: True" in capsys.readouterr().out
        assert repair.call_count == 1
        quiet = await run_pending(
            db, GateLLM(), repair, tenant.id, source_id, embedding_config=config
        )
        assert quiet.already_ready == 1 and quiet.processed == quiet.failed == 0
        assert await version_snapshot(observer, tenant.id) == after


async def test_stored_embedding_with_wrong_dimensions_is_not_ready_and_not_deleted(env, factory):
    from meyar.search.schemas import EmbeddingSearchConfig

    tenant, _, storage, base = env
    # Historical 2-dimensional row, same immutable identity as the active model.
    source_id, candidate_id, _ = await processed_once(factory, storage, tenant, base)
    config = EmbeddingSearchConfig(
        provider="fake-embedding", model_name="fake-embedding-model-v1", model_revision="",
        serializer_version=SERIALIZER_VERSION, embedding_dimensions=8,
    )
    async with factory() as db, factory() as observer:
        embedder = GateEmbedding(dimensions=8)
        summary = await run_pending(
            db, GateLLM(), embedder, tenant.id, source_id, embedding_config=config
        )
        # Same immutable identity, different dimensions: cannot be replaced (D-014) and is
        # never deleted; truthfully not ready (operator action), no provider call wasted.
        assert summary.failed == 1 and summary.ready_after == 0, summary
        assert embedder.call_count == 0
        assert len(await embedding_rows(observer, tenant.id)) == 1
        from meyar.services.candidate_embedding_repo import EmbeddingCompatibility
        from meyar.services.candidate_embedding_service import (
            EmbeddingPreconditionError,
            embed_candidate_profile,
        )
        with pytest.raises(EmbeddingPreconditionError) as exc:
            await embed_candidate_profile(
                db, embedder, tenant_id=tenant.id, candidate_id=candidate_id,
                max_input_chars=20000,
                compatibility=EmbeddingCompatibility(**config.model_dump()),
            )
        assert exc.value.code == "EMBEDDING_IDENTITY_INCOMPATIBLE"
        assert embedder.call_count == 0


async def test_concurrent_runs_with_stale_embedding_create_one_compatible_embedding(env, factory):
    tenant, _, storage, base = env
    source_id, _, _ = await processed_once(factory, storage, tenant, base, model="old-model")
    first_embedder = GateEmbedding(model_name="new-model", gate=True)
    async with factory() as one, factory() as two, factory() as observer:
        winner = asyncio.create_task(
            run_pending(one, GateLLM(), first_embedder, tenant.id, source_id)
        )
        loser = None
        try:
            await asyncio.wait_for(first_embedder.first.wait(), 15)
            loser = asyncio.create_task(
                run_pending(two, GateLLM(), GateEmbedding(model_name="new-model"),
                            tenant.id, source_id)
            )
            await settled(observer, loser, first_embedder.second)
            first_embedder.release.set()
            results = await asyncio.wait_for(asyncio.gather(winner, loser), 30)
            assert all(r.failed == 0 and r.ready_after == 1 for r in results), results
            rows = await embedding_rows(observer, tenant.id)
            assert sorted(r.model_name for r in rows) == ["new-model", "old-model"]
            assert (await truth(observer, tenant.id))["failed_events"] == 0
        finally:
            first_embedder.release.set()
            await stop(winner, loser)


async def test_delete_during_embedding_inference_discards_stale_output(env, factory):
    tenant, key, storage, base = env
    photos = LocalPhotoStorage(str(base / "storage"))
    source_id, candidate_id, _ = await processed_once(factory, storage, tenant, base, model="old")
    embedder = GateEmbedding(model_name="new", gate=True)
    async with factory() as run_db, factory() as deleter, factory() as observer:
        running = asyncio.create_task(
            run_pending(run_db, GateLLM(), embedder, tenant.id, source_id)
        )
        try:
            await asyncio.wait_for(embedder.first.wait(), 15)
            deleted = await delete_candidate(
                deleter, tenant.id, candidate_id, storage, photos, key.id
            )
            assert deleted == 1
            embedder.release.set()
            summary = await asyncio.wait_for(running, 30)
            assert summary.failed == 0 and summary.superseded == 1, summary
            assert await embedding_rows(observer, tenant.id) == []
        finally:
            embedder.release.set()
            await stop(running)


async def test_tenant_suspension_during_embedding_inference_wins(env, factory):
    tenant, _, storage, base = env
    source_id, _, _ = await processed_once(factory, storage, tenant, base, model="old")
    embedder = GateEmbedding(model_name="new", gate=True)
    async with factory() as run_db, factory() as admin, factory() as observer:
        running = asyncio.create_task(
            run_pending(run_db, GateLLM(), embedder, tenant.id, source_id)
        )
        try:
            await asyncio.wait_for(embedder.first.wait(), 15)
            await set_tenant_active(admin, tenant_id=tenant.id, is_active=False)
            await admin.commit()
            embedder.release.set()
            with pytest.raises(TenantInactiveError):
                await asyncio.wait_for(running, 30)
            rows = await embedding_rows(observer, tenant.id)
            assert [r.model_name for r in rows] == ["old"]
        finally:
            embedder.release.set()
            await stop(running)


async def test_no_transaction_connection_or_lock_during_stale_reembedding(env, factory):
    from test_folder_downstream_concurrency import idle_in_transaction

    tenant, _, storage, base = env
    source_id, _, _ = await processed_once(factory, storage, tenant, base, model="old")
    embedder = GateEmbedding(model_name="new", gate=True)
    async with factory() as run_db, factory() as observer:
        running = asyncio.create_task(
            run_pending(run_db, GateLLM(), embedder, tenant.id, source_id)
        )
        try:
            await asyncio.wait_for(embedder.first.wait(), 15)
            held = await idle_in_transaction(observer)
            locks = await observer.scalar(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'relation' AND granted "
                    "AND pid <> pg_backend_pid() AND database = "
                    "(SELECT oid FROM pg_database WHERE datname = current_database())"
                )
            )
            embedder.release.set()
            await asyncio.wait_for(running, 30)
            assert held == 0 and locks == 0, (held, locks)
        finally:
            embedder.release.set()
            await stop(running)


async def test_only_effective_profile_remains_searchable_after_reembedding(env, factory):
    tenant, _, storage, base = env
    source_id, candidate_id, _ = await processed_once(factory, storage, tenant, base, model="old")
    async with factory() as db, factory() as observer:
        await run_pending(db, GateLLM(), GateEmbedding(model_name="new"), tenant.id, source_id)
        assert candidate_id in await skills_found(observer, tenant.id, "Java")
        await observer.rollback()
        count = await observer.scalar(
            select(func.count()).select_from(CandidateProfileVersion).where(
                CandidateProfileVersion.tenant_id == tenant.id
            )
        )
        assert count == 1
