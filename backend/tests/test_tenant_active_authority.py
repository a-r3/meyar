"""Issue #46 S1: synthetic suspension, reactivation and processing races."""

import asyncio
import re
import uuid

import pytest
from conftest import FIXTURES_DIR, TEST_DATABASE_URL
from fakes import FakeEmbeddingProvider, FakeLLMProvider
from fastapi import Request
from sqlalchemy import event, func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.util import await_only
from test_folder_reconciliation import _identity_extraction, _profile_extraction
from test_identity_embedding_cli import _patch_session, _seed_document

from meyar import cli
from meyar.config import Settings, get_settings
from meyar.extraction.identity_service import extract_candidate_identity
from meyar.extraction.service import extract_candidate_profile
from meyar.ingestion.dependency import get_document_parser
from meyar.ingestion.parsers.local_text_parser import LocalTextParser
from meyar.main import app
from meyar.models.api_key import ApiKey
from meyar.models.audit_event import AuditEvent
from meyar.models.browser_session import BrowserSession
from meyar.models.candidate import Candidate
from meyar.models.candidate_document import CandidateDocument
from meyar.models.candidate_embedding_version import CandidateEmbeddingVersion
from meyar.models.candidate_identity_version import CandidateIdentityVersion
from meyar.models.candidate_photo_version import CandidatePhotoVersion
from meyar.models.candidate_profile_version import CandidateProfileVersion
from meyar.models.canonical_document import CanonicalDocument
from meyar.models.folder_indexed_file import FolderIndexedFile
from meyar.models.tenant import Tenant
from meyar.schemas.candidate_profile import CandidateProfileExtraction, EvidenceRef, SkillItem
from meyar.services.api_key_repo import create_api_key
from meyar.services.browser_session_repo import create_browser_session, revoke_browser_session_by_id
from meyar.services.candidate_embedding_service import embed_candidate_profile
from meyar.services.candidate_repo import create_candidate
from meyar.services.folder_indexer_service import index_folder
from meyar.services.folder_reconciliation_service import process_pending_candidates
from meyar.services.tenant_authority import (
    TenantInactiveError,
    require_active_tenant,
    set_tenant_active,
    tenant_is_active,
)
from meyar.services.tenant_membership_repo import create_membership
from meyar.services.tenant_repo import create_tenant
from meyar.storage.local import LocalFilesystemStorage
from meyar.ui.auth import UIAccessError, resolve_ui_context


@pytest.fixture
async def authority_factory():
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _state(factory, tenant_id, active):
    async with factory() as db:
        await set_tenant_active(db, tenant_id=tenant_id, is_active=active)
        await db.commit()


def _token(html):
    match = re.search(r'name="token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


@pytest.fixture
def ui_settings(monkeypatch):
    settings = Settings(ui_cookie_secure=False)
    monkeypatch.setitem(app.dependency_overrides, get_settings, lambda: settings)
    return settings


async def test_inactive_rest_reads_writes_last_used_and_other_tenant(
    client, db_session, tenant_and_key, authority_factory
):
    tenant, key, raw = tenant_and_key
    tenant_id, key_id = tenant.id, key.id
    candidate = await create_candidate(db_session, tenant_id=tenant_id)
    candidate_id = candidate.id
    other = await create_tenant(db_session, name="Synthetic unaffected tenant")
    _, other_raw = await create_api_key(db_session, tenant_id=other.id, env="test")
    await db_session.commit()
    headers = {"Authorization": f"Bearer {raw}"}
    assert (
        await client.get(f"/api/v1/candidates/{candidate_id}", headers=headers)
    ).status_code == 200
    last_used = await db_session.scalar(select(ApiKey.last_used_at).where(ApiKey.id == key_id))
    counts = [
        await db_session.scalar(select(func.count()).select_from(m))
        for m in (Candidate, AuditEvent)
    ]
    await db_session.rollback()
    await _state(authority_factory, tenant_id, False)
    for method, path in (
        ("GET", f"/api/v1/candidates/{candidate_id}"),
        ("POST", "/api/v1/candidates"),
        ("DELETE", f"/api/v1/candidates/{candidate_id}"),
    ):
        response = await client.request(method, path, headers=headers)
        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid or missing API key."}
        assert response.headers["www-authenticate"] == "Bearer"
    assert (
        await db_session.scalar(select(ApiKey.last_used_at).where(ApiKey.id == key_id)) == last_used
    )
    assert [
        await db_session.scalar(select(func.count()).select_from(m))
        for m in (Candidate, AuditEvent)
    ] == counts
    assert (
        await client.post("/api/v1/candidates", headers={"Authorization": f"Bearer {other_raw}"})
    ).status_code == 201
    await db_session.rollback()
    await _state(authority_factory, tenant_id, True)
    assert (
        await client.get(f"/api/v1/candidates/{candidate_id}", headers=headers)
    ).status_code == 200
    assert await db_session.scalar(select(ApiKey.revoked_at).where(ApiKey.id == key_id)) is None


async def test_single_membership_inactive_login_uses_generic_error(
    client, db_session, tenant_and_user, authority_factory, ui_settings
):
    tenant, user, password, _ = tenant_and_user
    username, tenant_id = user.username, tenant.id
    await _state(authority_factory, tenant_id, False)
    rejected = await client.post("/ui/login", data={"username": username, "password": password})
    unknown = await client.post(
        "/ui/login", data={"username": "synthetic-unknown", "password": password}
    )
    assert rejected.status_code == unknown.status_code == 401
    assert rejected.text == unknown.text
    assert await db_session.scalar(select(BrowserSession)) is None


async def test_inactive_tenant_not_offered_in_multiple_choices(
    client, db_session, tenant_and_user, authority_factory, ui_settings
):
    tenant, user, password, membership = tenant_and_user
    tenant_id, membership_id, username, user_id = tenant.id, membership.id, user.username, user.id
    for name in ("Synthetic choice B", "Synthetic choice C"):
        other = await create_tenant(db_session, name=name)
        await create_membership(db_session, user_id=user_id, tenant_id=other.id, role="HR_USER")
    await db_session.commit()
    await _state(authority_factory, tenant_id, False)
    response = await client.post("/ui/login", data={"username": username, "password": password})
    assert response.status_code == 200
    assert str(membership_id) not in response.text
    assert "Synthetic choice B" in response.text and "Synthetic choice C" in response.text


async def test_deactivate_reactivate_revokes_only_tenant_sessions_and_claims(
    client, db_session, tenant_and_user, authority_factory, ui_settings
):
    tenant, user, password, membership = tenant_and_user
    tenant_id, user_id, membership_id, username = tenant.id, user.id, membership.id, user.username
    other = await create_tenant(db_session, name="Synthetic other membership")
    other_member = await create_membership(
        db_session, user_id=user_id, tenant_id=other.id, role="HR_USER"
    )
    other_membership_id = other_member.id
    old_session, old_cookie = await create_browser_session(
        db_session, user_id=user_id, tenant_membership_id=membership_id, ttl_hours=8
    )
    _, other_cookie = await create_browser_session(
        db_session, user_id=user_id, tenant_membership_id=other_membership_id, ttl_hours=8
    )
    old_session_id = old_session.id
    await db_session.commit()
    login = await client.post("/ui/login", data={"username": username, "password": password})
    token = _token(login.text)
    await _state(authority_factory, tenant_id, False)
    selected = await client.post(
        "/ui/login/select-tenant", data={"token": token, "membership_id": str(membership_id)}
    )
    assert selected.status_code == 401
    client.cookies.set("meyar_ui_session", old_cookie, path="/ui")
    assert (await client.get("/ui", follow_redirects=False)).status_code == 303
    await db_session.rollback()
    await _state(authority_factory, tenant_id, True)
    assert (
        await db_session.scalar(
            select(BrowserSession.revoked_at).where(BrowserSession.id == old_session_id)
        )
        is not None
    )
    client.cookies.set("meyar_ui_session", old_cookie, path="/ui")
    assert (await client.get("/ui", follow_redirects=False)).status_code == 303
    assert (
        await client.post(
            "/ui/login/select-tenant", data={"token": token, "membership_id": str(membership_id)}
        )
    ).status_code == 401
    client.cookies.set("meyar_ui_session", other_cookie, path="/ui")
    assert (await client.get("/ui", follow_redirects=False)).status_code == 200
    # The same pending token's unrelated membership is still valid.
    assert (
        await client.post(
            "/ui/login/select-tenant",
            data={"token": token, "membership_id": str(other_membership_id)},
        )
    ).status_code == 303


async def test_live_session_and_session_creation_reject_direct_inactive_state(
    client, db_session, tenant_and_user, authority_factory, ui_settings
):
    tenant, user, password, membership = tenant_and_user
    tenant_id, user_id, member_id, username = tenant.id, user.id, membership.id, user.username
    assert (
        await client.post("/ui/login", data={"username": username, "password": password})
    ).status_code == 303
    async with authority_factory() as db:
        await db.execute(update(Tenant).where(Tenant.id == tenant_id).values(is_active=False))
        await db.commit()
    # No session revocation here: resolve_ui_context must read Tenant itself.
    response = await client.get("/ui", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/ui/login"
    assert "Max-Age=0" in response.headers["set-cookie"]
    with pytest.raises(TenantInactiveError):
        await create_browser_session(
            db_session, user_id=user_id, tenant_membership_id=member_id, ttl_hours=8
        )


async def test_pending_selection_checks_tenant_without_session_or_stamp_changes(
    client, db_session, tenant_and_user, authority_factory, ui_settings
):
    tenant, user, password, membership = tenant_and_user
    tenant_id, user_id, member_id, username = tenant.id, user.id, membership.id, user.username
    other = await create_tenant(db_session, name="Synthetic pending other")
    await create_membership(db_session, user_id=user_id, tenant_id=other.id, role="HR_USER")
    await db_session.commit()
    login = await client.post("/ui/login", data={"username": username, "password": password})
    token = _token(login.text)
    async with authority_factory() as db:
        await db.execute(update(Tenant).where(Tenant.id == tenant_id).values(is_active=False))
        await db.commit()
    assert (
        await client.post(
            "/ui/login/select-tenant", data={"token": token, "membership_id": str(member_id)}
        )
    ).status_code == 401
    assert await db_session.scalar(select(BrowserSession)) is None


async def test_upload_deactivated_during_parser_has_no_document_authority(
    client, db_session, tenant_and_key, authority_factory, monkeypatch, tmp_path
):
    tenant, _, raw = tenant_and_key
    tenant_id = tenant.id
    candidate = await create_candidate(db_session, tenant_id=tenant_id)
    candidate_id = candidate.id
    await db_session.commit()

    class DisablingParser:
        async def parse(self, **kwargs):
            assert not db_session.in_transaction()
            await _state(authority_factory, tenant_id, False)
            return await LocalTextParser().parse(**kwargs)

    monkeypatch.setitem(app.dependency_overrides, get_document_parser, DisablingParser)
    response = await client.post(
        f"/api/v1/candidates/{candidate_id}/documents",
        headers={"Authorization": f"Bearer {raw}"},
        files={
            "file": (
                "synthetic.pdf",
                (FIXTURES_DIR / "valid_cv.pdf").read_bytes(),
                "application/pdf",
            )
        },
    )
    assert response.status_code == 401
    for model in (CandidateDocument, CanonicalDocument):
        assert await db_session.scalar(select(model)) is None
    assert not [p for p in (tmp_path / "storage").rglob("*") if p.is_file()]


async def test_folder_deactivated_during_parser_rolls_back_scan(
    db_session, tenant_and_key, authority_factory, tmp_path
):
    tenant_id = tenant_and_key[0].id
    root = tmp_path / "synthetic"
    root.mkdir()
    (root / "synthetic.pdf").write_bytes((FIXTURES_DIR / "valid_cv.pdf").read_bytes())

    class DisablingParser:
        async def parse(self, **kwargs):
            # The existing folder transaction/FK locks do not prevent disable.
            await asyncio.wait_for(_state(authority_factory, tenant_id, False), 10)
            return await LocalTextParser().parse(**kwargs)

    with pytest.raises(TenantInactiveError):
        await index_folder(
            db_session,
            LocalFilesystemStorage(root=str(tmp_path / "storage")),
            DisablingParser(),
            tenant_id=tenant_id,
            root_path=str(root),
            max_bytes=10_000_000,
        )
    await db_session.commit()  # cannot recover rejected scan results by catching the error
    for model in (Candidate, CandidateDocument, CanonicalDocument, FolderIndexedFile):
        assert await db_session.scalar(select(model)) is None


@pytest.mark.parametrize("stage", ["profile", "identity", "embedding"])
async def test_direct_processing_disabled_during_provider_has_no_generated_version(
    db_session, tenant_and_key, authority_factory, stage
):
    tenant_id = tenant_and_key[0].id
    candidate, document, _ = await _seed_document(db_session, tenant_id)
    candidate_id = candidate.id
    profile = CandidateProfileExtraction(
        skills=[
            SkillItem(name="Python", evidence=[EvidenceRef(page=1, block_index=0, quote="Python")])
        ]
    )
    llm = FakeLLMProvider(extraction=profile, identity_extraction=_identity_extraction())
    await db_session.commit()
    if stage == "embedding":
        await extract_candidate_profile(
            db_session,
            llm,
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            candidate_document=document,
            model_provider_name="fake",
            max_input_chars=20_000,
        )
        await db_session.commit()

    class DisablingLLM(FakeLLMProvider):
        async def extract_candidate_profile(self, view):
            await _state(authority_factory, tenant_id, False)
            return await super().extract_candidate_profile(view)

        async def extract_candidate_identity(self, view):
            await _state(authority_factory, tenant_id, False)
            return await super().extract_candidate_identity(view)

    class DisablingEmbedding(FakeEmbeddingProvider):
        async def embed(self, text):
            await _state(authority_factory, tenant_id, False)
            return await super().embed(text)

    with pytest.raises(TenantInactiveError):
        if stage == "embedding":
            await embed_candidate_profile(
                db_session,
                DisablingEmbedding(),
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                max_input_chars=20_000,
                compatibility=DisablingEmbedding().compatibility,
            )
        else:
            process = (
                extract_candidate_profile if stage == "profile" else extract_candidate_identity
            )
            await process(
                db_session,
                DisablingLLM(extraction=profile, identity_extraction=_identity_extraction()),
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                candidate_document=document,
                model_provider_name="fake",
                max_input_chars=20_000,
            )
    await db_session.commit()
    model = {
        "profile": CandidateProfileVersion,
        "identity": CandidateIdentityVersion,
        "embedding": CandidateEmbeddingVersion,
    }[stage]
    assert await db_session.scalar(select(model)) is None


@pytest.mark.parametrize("stage", ["profile", "identity", "embedding"])
async def test_reconciliation_disabled_during_each_stage_rolls_back_whole_candidate(
    db_session, tenant_and_key, authority_factory, tmp_path, stage
):
    tenant_id = tenant_and_key[0].id
    root = tmp_path / "synthetic"
    root.mkdir()
    (root / "synthetic.pdf").write_bytes((FIXTURES_DIR / "valid_cv.pdf").read_bytes())
    summary = await index_folder(
        db_session,
        LocalFilesystemStorage(root=str(tmp_path / "storage")),
        LocalTextParser(),
        tenant_id=tenant_id,
        root_path=str(root),
        max_bytes=10_000_000,
    )
    await db_session.commit()

    class DisablingLLM(FakeLLMProvider):
        async def extract_candidate_profile(self, view):
            if stage == "profile":
                await asyncio.wait_for(_state(authority_factory, tenant_id, False), 10)
            return await super().extract_candidate_profile(view)

        async def extract_candidate_identity(self, view):
            if stage == "identity":
                await asyncio.wait_for(_state(authority_factory, tenant_id, False), 10)
            return await super().extract_candidate_identity(view)

    class DisablingEmbedding(FakeEmbeddingProvider):
        async def embed(self, text):
            await asyncio.wait_for(_state(authority_factory, tenant_id, False), 10)
            return await super().embed(text)

    with pytest.raises(TenantInactiveError):
        await process_pending_candidates(
            db_session,
            DisablingLLM(
                extraction=_profile_extraction(), identity_extraction=_identity_extraction()
            ),
            DisablingEmbedding(),
            tenant_id=tenant_id,
            folder_source_id=summary.folder_source_id,
            model_provider_name="fake",
            max_profile_input_chars=20_000,
            max_identity_input_chars=20_000,
            max_embedding_input_chars=20_000,
        )
    # Issue #46 S9: each stage is its own short committed phase. Stages that committed
    # while the tenant was still active are durable; the disabled stage and every later
    # stage persist nothing (the Phase B tenant re-check refuses it).
    expected = {"profile": 0, "identity": 1, "embedding": 2}[stage]
    models = (CandidateProfileVersion, CandidateIdentityVersion, CandidateEmbeddingVersion)
    for index, model in enumerate(models):
        count = await db_session.scalar(select(func.count()).select_from(model))
        assert count == (1 if index < expected else 0), (stage, model.__name__, count)


async def test_final_commit_rechecks_live_tenant_and_cannot_be_bypassed(
    db_session, tenant_and_key, authority_factory
):
    tenant_id = tenant_and_key[0].id
    await require_active_tenant(db_session, tenant_id)
    await create_candidate(db_session, tenant_id=tenant_id)
    await _state(authority_factory, tenant_id, False)
    for _ in range(2):
        with pytest.raises(TenantInactiveError):
            await db_session.commit()
    await db_session.rollback()
    assert await db_session.scalar(select(Candidate)) is None
    assert not await tenant_is_active(db_session, tenant_id)


async def test_savepoint_does_not_clear_outer_commit_authority(
    db_session, tenant_and_key, authority_factory
):
    tenant_id = tenant_and_key[0].id
    await require_active_tenant(db_session, tenant_id)
    async with db_session.begin_nested():
        await create_candidate(db_session, tenant_id=tenant_id)
    await _state(authority_factory, tenant_id, False)
    with pytest.raises(TenantInactiveError):
        await db_session.commit()
    await db_session.rollback()
    assert await db_session.scalar(select(Candidate)) is None


@pytest.mark.parametrize("disable_first", [True, False])
async def test_outer_commit_serializes_with_tenant_disable(
    tenant_and_key, authority_factory, disable_first
):
    """Real PostgreSQL waits: the hook itself holds authority until commit."""
    from test_agent_inference_boundary import _blocked_on_row_lock

    tenant_id = tenant_and_key[0].id
    async with authority_factory() as work, authority_factory() as mutator:
        await require_active_tenant(work, tenant_id)
        await create_candidate(work, tenant_id=tenant_id)
        if disable_first:
            await set_tenant_active(mutator, tenant_id=tenant_id, is_active=False)
            pid = await work.scalar(text("SELECT pg_backend_pid()"))
            committing = asyncio.create_task(work.commit())
            try:
                assert await _blocked_on_row_lock(authority_factory, pid, committing)
                await mutator.commit()
                with pytest.raises(TenantInactiveError):
                    await asyncio.wait_for(committing, 10)
                await work.rollback()
                assert await work.scalar(select(Candidate)) is None
            finally:
                if not committing.done():
                    committing.cancel()
                await asyncio.gather(committing, return_exceptions=True)
        else:
            checked, release = asyncio.Event(), asyncio.Event()

            @event.listens_for(work.sync_session, "before_commit")
            def pause_after_shared_authority(_session):
                # The class-level authority hook runs before this instance hook.
                checked.set()
                await_only(release.wait())

            committing = asyncio.create_task(work.commit())
            disabling = None
            try:
                await asyncio.wait_for(checked.wait(), 10)
                pid = await mutator.scalar(text("SELECT pg_backend_pid()"))

                async def disable():
                    await set_tenant_active(mutator, tenant_id=tenant_id, is_active=False)
                    await mutator.commit()

                disabling = asyncio.create_task(disable())
                assert await _blocked_on_row_lock(authority_factory, pid, disabling)
                release.set()
                await asyncio.wait_for(committing, 10)
                await asyncio.wait_for(disabling, 10)
                assert await work.scalar(select(Candidate)) is not None
                assert not await tenant_is_active(work, tenant_id)
            finally:
                release.set()
                for task in (committing, disabling):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(
                    *[t for t in (committing, disabling) if t is not None], return_exceptions=True
                )
                event.remove(work.sync_session, "before_commit", pause_after_shared_authority)


async def test_photo_authority_loss_does_not_create_terminal_failure(
    db_session, tenant_and_key, authority_factory, monkeypatch, tmp_path
):
    from meyar.services import candidate_photo_service
    from meyar.storage.photo import LocalPhotoStorage

    tenant_id = tenant_and_key[0].id
    candidate, document, _ = await _seed_document(db_session, tenant_id)
    candidate_id, document_id = candidate.id, document.id
    await db_session.commit()
    storage = LocalFilesystemStorage(root=str(tmp_path / "storage"))

    async def read(**_kwargs):
        return b"synthetic photo extraction input"

    async def disabling_extract(_data, _kind):
        await _state(authority_factory, tenant_id, False)
        return {"status": "EXTRACTION_FAILED", "reason_code": "PROCESSING_FAILED"}

    monkeypatch.setattr(storage, "read", read)
    monkeypatch.setattr(candidate_photo_service, "_extract_isolated", disabling_extract)
    with pytest.raises(TenantInactiveError):
        await candidate_photo_service.process_photo_for_document(
            db_session,
            storage,
            LocalPhotoStorage(root=str(tmp_path / "storage")),
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            document_id=document_id,
        )
    assert await db_session.scalar(select(CandidatePhotoVersion)) is None


@pytest.mark.parametrize(
    ("disable_first", "reactivate"), [(True, False), (True, True), (False, False)]
)
async def test_ui_logout_and_deactivation_preserve_security_lock_order(
    db_session, tenant_and_user, authority_factory, disable_first, reactivate
):
    from test_agent_inference_boundary import _blocked_on_row_lock

    tenant, user, _, membership = tenant_and_user
    tenant_id, user_id, member_id = tenant.id, user.id, membership.id
    session, token = await create_browser_session(
        db_session, user_id=user_id, tenant_membership_id=member_id, ttl_hours=8
    )
    session_id = session.id
    await db_session.commit()
    request = Request(
        {"type": "http", "headers": [(b"cookie", f"meyar_ui_session={token}".encode())]}
    )
    async with authority_factory() as logout, authority_factory() as mutator:
        if disable_first:
            await set_tenant_active(mutator, tenant_id=tenant_id, is_active=False)
            if reactivate:
                await set_tenant_active(mutator, tenant_id=tenant_id, is_active=True)
            pid = await logout.scalar(text("SELECT pg_backend_pid()"))
            resolving = asyncio.create_task(resolve_ui_context(request, logout))
            try:
                assert await _blocked_on_row_lock(authority_factory, pid, resolving)
                await mutator.commit()
                with pytest.raises(UIAccessError) as refused:
                    await asyncio.wait_for(resolving, 10)
                assert refused.value.clear_cookie
            finally:
                if not resolving.done():
                    resolving.cancel()
                await asyncio.gather(resolving, return_exceptions=True)
        else:
            ctx = await resolve_ui_context(request, logout)
            pid = await mutator.scalar(text("SELECT pg_backend_pid()"))

            async def disable():
                await set_tenant_active(mutator, tenant_id=tenant_id, is_active=False)
                await mutator.commit()

            disabling = asyncio.create_task(disable())
            try:
                assert await _blocked_on_row_lock(authority_factory, pid, disabling)
                await revoke_browser_session_by_id(logout, session_id=ctx.session_id)
                await logout.commit()
                await asyncio.wait_for(disabling, 10)
            finally:
                if not disabling.done():
                    disabling.cancel()
                await asyncio.gather(disabling, return_exceptions=True)
        assert (
            await mutator.scalar(
                select(BrowserSession.revoked_at).where(BrowserSession.id == session_id)
            )
            is not None
        )


@pytest.mark.parametrize(
    "command",
    [
        "_extract_profile",
        "_extract_identity",
        "_embed_candidate",
        "_index_folder",
        "_reconcile_folder",
    ],
)
async def test_direct_cli_processing_rejects_inactive_tenant(
    db_session, tenant_and_key, authority_factory, monkeypatch, capsys, command
):
    tenant_id = tenant_and_key[0].id
    await _state(authority_factory, tenant_id, False)
    _patch_session(monkeypatch, db_session)
    monkeypatch.setattr(cli, "get_llm_provider", FakeLLMProvider)
    monkeypatch.setattr(cli, "get_embedding_provider", FakeEmbeddingProvider)
    args = {
        "_extract_profile": (str(uuid.uuid4()), str(uuid.uuid4())),
        "_extract_identity": (str(uuid.uuid4()), str(uuid.uuid4())),
        "_embed_candidate": (str(uuid.uuid4()),),
        "_index_folder": ("/synthetic-unavailable",),
        "_reconcile_folder": ("/synthetic-unavailable", None),
    }
    # Settings may be consulted before session entry but must not run providers.
    monkeypatch.setattr(cli, "get_settings", lambda: Settings())
    with pytest.raises(SystemExit) as refused:
        await getattr(cli, command)(str(tenant_id), *args[command])
    assert refused.value.code == 1
    assert "Tenant application access is unavailable" in capsys.readouterr().out
    assert await db_session.scalar(select(Candidate)) is None
