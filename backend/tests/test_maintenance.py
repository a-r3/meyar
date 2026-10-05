"""#46: synthetic storage, real PG locks and real SIGKILL boundaries."""

import asyncio
import hashlib
import os
import signal
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import TEST_DATABASE_URL
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from meyar.models.audit_event import AuditEvent
from meyar.models.candidate import Candidate
from meyar.models.candidate_document import CandidateDocument
from meyar.services.candidate_document_repo import create_candidate_document
from meyar.services.candidate_repo import create_candidate
from meyar.services.maintenance import MaintenancePolicy, run_maintenance
from meyar.services.storage_authority import storage_writer
from meyar.storage.local import LocalFilesystemStorage
from meyar.storage.photo import LocalPhotoStorage

CONTENT = b"SYNTHETIC ORIGINAL ONLY"
POLICY = MaintenancePolicy(orphan_age_seconds=0)


async def seed(db, tenant_id, root):
    candidate = await create_candidate(db, tenant_id=tenant_id)
    key = await LocalFilesystemStorage(str(root)).save(tenant_id=tenant_id, content=CONTENT)
    document = await create_candidate_document(
        db,
        tenant_id=tenant_id,
        candidate_id=candidate.id,
        original_filename="synthetic.pdf",
        mime_type="application/pdf",
        byte_size=len(CONTENT),
        sha256_hash=hashlib.sha256(CONTENT).hexdigest(),
        storage_key=key,
    )
    await db.commit()
    db.expunge(candidate)
    db.expunge(document)
    return candidate, document


async def sweep(db, tenant_id, root, *, apply=True, policy=POLICY):
    result = await run_maintenance(
        db, tenant_id=tenant_id, storage_root=root, policy=policy, apply=apply
    )
    if apply:
        await db.commit()
    else:
        await db.rollback()
    return result


@pytest.mark.parametrize("boundary", ["save", "stage_before_commit", "stage_after_commit"])
async def test_sigkill_storage_recovery_and_idempotent_repeat(
    db_session, tenant_and_key, tmp_path, boundary
):
    tenant, _, _ = tenant_and_key
    tenant_id = tenant.id
    root = tmp_path / "storage"
    candidate, document = await seed(db_session, tenant_id, root)
    source = """import asyncio,signal,sys,uuid
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from meyar.models.candidate_document import CandidateDocument
from meyar.services.storage_authority import storage_writer
from meyar.storage.local import LocalFilesystemStorage
async def run():
 e=create_async_engine(sys.argv[1]);f=async_sessionmaker(e)
 async with f() as db:
  t=uuid.UUID(sys.argv[3]);await storage_writer(db,t)
  s=LocalFilesystemStorage(sys.argv[2])
  if sys.argv[4]=='save': await s.save(tenant_id=t,content=b'SYNTHETIC ORPHAN')
  else:
   await s.stage_delete(tenant_id=t,storage_key=sys.argv[5])
   await db.execute(delete(CandidateDocument).where(CandidateDocument.id==uuid.UUID(sys.argv[6])))
   if sys.argv[4]=='stage_after_commit': await db.commit()
  print('KILL_POINT',flush=True);signal.pause()
asyncio.run(run())
"""
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            source,
            TEST_DATABASE_URL,
            str(root),
            str(tenant_id),
            boundary,
            document.storage_key,
            str(document.id),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        marker = await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), 10)
        assert marker == "KILL_POINT\n"
        os.kill(child.pid, signal.SIGKILL)
        await asyncio.wait_for(asyncio.to_thread(child.wait), 10)
        assert child.returncode == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
    # Authoritative lock acquisition, rather than elapsed time, decides recovery.
    for _ in range(10):
        result = await sweep(db_session, tenant_id, root, apply=False)
        if not result.busy:
            break
        await asyncio.sleep(0.01)
    assert not result.busy and result.unresolved == 0
    before = set(root.rglob("*"))
    result = await sweep(db_session, tenant_id, root, apply=False)
    assert set(root.rglob("*")) == before
    assert (
        result.counts.get(
            "STAGED_RESTORED"
            if boundary == "stage_before_commit"
            else "STAGED_PURGED"
            if boundary == "stage_after_commit"
            else "ORPHAN_REMOVED"
        )
        == 1
    )
    result = await sweep(db_session, tenant_id, root)
    assert result.exit_code == 0
    if boundary == "stage_after_commit":
        assert not (root / document.storage_key).exists()
        assert await db_session.get(CandidateDocument, document.id, populate_existing=True) is None
    else:
        assert (root / document.storage_key).read_bytes() == CONTENT
    assert not list((root / ".trash").rglob("*.json")) if (root / ".trash").exists() else True
    again = await sweep(db_session, tenant_id, root)
    assert again.exit_code == 0
    assert not any(
        k in again.counts
        for k in (
            "ORPHAN_REMOVED",
            "STAGED_PURGED",
            "STAGED_RESTORED",
        )
    )


async def test_cleanup_refuses_live_writer_and_preserves_pending_original(
    db_session, tenant_and_key, tmp_path
):
    tenant, _, _ = tenant_and_key
    tenant_id = tenant.id
    engine = create_async_engine(TEST_DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as writer:
            await storage_writer(writer, tenant_id)
            key = await LocalFilesystemStorage(str(tmp_path)).save(
                tenant_id=tenant_id,
                content=CONTENT,
            )
            result = await sweep(db_session, tenant_id, tmp_path)
            assert result.busy and result.exit_code == 1
            assert (tmp_path / key).read_bytes() == CONTENT
            await writer.rollback()
        result = await sweep(db_session, tenant_id, tmp_path)
        assert result.counts["ORPHAN_REMOVED"] == 1
    finally:
        await engine.dispose()


@pytest.mark.parametrize("kind", ["document", "photo"])
async def test_staged_restore_conflict_refuses_overwrite_and_keeps_journal(
    db_session, tenant_and_key, tmp_path, kind
):
    tenant, _, _ = tenant_and_key
    tenant_id = tenant.id
    _, document = await seed(db_session, tenant_id, tmp_path)
    storage = LocalFilesystemStorage(str(tmp_path))
    if kind == "photo":
        # An unreferenced derived object is purged; no ownership from filenames.
        photo = LocalPhotoStorage(str(tmp_path))
        key = await photo.save(tenant_id=tenant_id, content=CONTENT)
        staged = await photo.stage_delete(tenant_id=tenant_id, storage_key=key)
        result = await sweep(db_session, tenant_id, tmp_path)
        assert result.counts["STAGED_PURGED"] == 1
        assert not (tmp_path / staged.trash_ref).exists()
        return
    staged = await storage.stage_delete(tenant_id=tenant_id, storage_key=document.storage_key)
    (tmp_path / document.storage_key).write_bytes(b"SYNTHETIC CONFLICT")
    result = await sweep(db_session, tenant_id, tmp_path)
    assert result.unresolved == 1 and result.exit_code == 2
    assert (tmp_path / staged.trash_ref).exists()
    assert (tmp_path / staged.trash_ref).with_suffix(".json").exists()
    assert (tmp_path / document.storage_key).read_bytes() == b"SYNTHETIC CONFLICT"


async def test_legacy_trash_content_authority_and_tenant_isolation(
    db_session, tenant_and_key, tmp_path
):
    tenant, _, _ = tenant_and_key
    tenant_id = tenant.id
    _, document = await seed(db_session, tenant_id, tmp_path)
    staged = await LocalFilesystemStorage(str(tmp_path)).stage_delete(
        tenant_id=tenant_id,
        storage_key=document.storage_key,
    )
    (tmp_path / staged.trash_ref).with_suffix(".json").unlink()  # actual pre-journal format
    foreign = uuid.uuid4()
    other = await LocalFilesystemStorage(str(tmp_path)).save(tenant_id=foreign, content=CONTENT)
    result = await sweep(db_session, tenant_id, tmp_path)
    assert result.exit_code == 0 and result.counts["LEGACY_REFERENCE_RESTORED"] == 1
    assert (tmp_path / document.storage_key).read_bytes() == CONTENT
    assert (tmp_path / other).read_bytes() == CONTENT


async def test_bounded_policy_and_explicit_empty_candidate_audit_retention(
    db_session, tenant_and_key, tmp_path
):
    tenant, _, _ = tenant_and_key
    tenant_id = tenant.id
    now = datetime.now(UTC)
    candidate = await create_candidate(db_session, tenant_id=tenant_id)
    candidate.created_at = now - timedelta(days=90)
    old = AuditEvent(
        tenant_id=tenant_id,
        event_type="SYNTHETIC_OLD",
        event_metadata={},
        created_at=now - timedelta(days=90),
    )
    marker = AuditEvent(
        tenant_id=tenant_id,
        event_type="DEMO_TENANT_BOOTSTRAPPED",
        event_metadata={},
        created_at=now - timedelta(days=90),
    )
    db_session.add_all([old, marker])
    await db_session.commit()
    candidate_id, marker_id = candidate.id, marker.id
    result = await sweep(
        db_session,
        tenant_id,
        tmp_path,
        apply=False,
        policy=MaintenancePolicy(audit_days=30, empty_candidate_days=30, limit=1),
    )
    assert not result.complete and result.exit_code == 1
    assert await db_session.get(Candidate, candidate_id) is not None
    result = await sweep(
        db_session,
        tenant_id,
        tmp_path,
        policy=MaintenancePolicy(audit_days=30, empty_candidate_days=30),
    )
    assert result.counts == {"AUDIT_RETIRED": 1, "EMPTY_CANDIDATE_RETIRED": 1}
    retained = await db_session.scalar(select(AuditEvent.id).where(AuditEvent.id == marker_id))
    assert retained == marker_id


async def test_symlink_namespace_refused_without_touching_foreign_tree(
    db_session, tenant_and_key, tmp_path
):
    tenant, _, _ = tenant_and_key
    tenant_id = tenant.id
    root, other = tmp_path / "root", tmp_path / "other"
    root.mkdir()
    (other / tenant_id.hex).mkdir(parents=True)
    target = other / tenant_id.hex / uuid.uuid4().hex
    target.write_bytes(CONTENT)
    (root / "photo").symlink_to(other, target_is_directory=True)
    result = await sweep(db_session, tenant_id, root)
    assert result.unresolved == 1 and target.read_bytes() == CONTENT


async def test_expired_session_cleanup_preserves_live_session_and_durable_conversation(
    db_session, tenant_and_user, tmp_path
):
    from meyar.models.agent_conversation import AgentConversationSessionContext
    from meyar.models.browser_session import BrowserSession
    from meyar.services.agent_conversation_repo import (
        OwnerPrincipal,
        create_conversation,
    )
    from meyar.services.browser_session_repo import create_browser_session

    tenant, user, _, membership = tenant_and_user
    tenant_id = tenant.id
    expired, _ = await create_browser_session(
        db_session,
        user_id=user.id,
        tenant_membership_id=membership.id,
        ttl_hours=8,
    )
    live, _ = await create_browser_session(
        db_session,
        user_id=user.id,
        tenant_membership_id=membership.id,
        ttl_hours=8,
    )
    expired.expires_at = datetime.now(UTC) - timedelta(days=20)
    conversation = await create_conversation(
        db_session,
        owner=OwnerPrincipal(
            tenant_id,
            user.id,
            membership.id,
        ),
    )
    context = AgentConversationSessionContext(
        tenant_id=tenant_id,
        conversation_id=conversation.id,
        browser_session_id=expired.id,
    )
    db_session.add(context)
    await db_session.commit()
    expired_id, live_id, conversation_id = expired.id, live.id, conversation.id
    result = await sweep(db_session, tenant_id, tmp_path, policy=MaintenancePolicy(session_days=7))
    assert result.counts["SESSION_RETIRED"] == 1
    assert (
        await db_session.scalar(
            select(BrowserSession.id).where(
                BrowserSession.id == expired_id,
            )
        )
        is None
    )
    assert (
        await db_session.scalar(
            select(BrowserSession.id).where(
                BrowserSession.id == live_id,
            )
        )
        == live_id
    )
    from meyar.models.agent_conversation import AgentConversation

    assert (
        await db_session.scalar(
            select(AgentConversation.id).where(
                AgentConversation.id == conversation_id,
            )
        )
        == conversation_id
    )
    assert (
        await db_session.scalar(
            select(AgentConversationSessionContext.id).where(
                AgentConversationSessionContext.conversation_id == conversation_id,
            )
        )
        is None
    )


async def test_global_auth_history_requires_separate_explicit_policy(
    db_session, tenant_and_key, tmp_path
):
    from meyar.models.auth_security_event import AuthSecurityEvent

    tenant_id = tenant_and_key[0].id
    row = AuthSecurityEvent(
        outcome_code="LOGIN_REJECTED", created_at=datetime.now(UTC) - timedelta(days=90)
    )
    db_session.add(row)
    await db_session.commit()
    row_id = row.id
    await sweep(db_session, tenant_id, tmp_path, policy=MaintenancePolicy(audit_days=30))
    assert (
        await db_session.scalar(
            select(AuthSecurityEvent.id).where(
                AuthSecurityEvent.id == row_id,
            )
        )
        == row_id
    )
    result = await sweep(
        db_session, tenant_id, tmp_path, policy=MaintenancePolicy(global_auth_event_days=30)
    )
    assert result.counts["GLOBAL_AUTH_EVENT_RETIRED"] == 1
    assert (
        await db_session.scalar(
            select(AuthSecurityEvent.id).where(
                AuthSecurityEvent.id == row_id,
            )
        )
        is None
    )


async def test_bounded_storage_scan_cursor_and_action_budget(db_session, tenant_and_key, tmp_path):
    tenant_id = tenant_and_key[0].id
    _, document = await seed(db_session, tenant_id, tmp_path)
    for _ in range(3):
        await LocalFilesystemStorage(str(tmp_path)).save(tenant_id=tenant_id, content=CONTENT)
    observed = 0
    offset = 0
    for _ in range(4):
        result = await sweep(
            db_session,
            tenant_id,
            tmp_path,
            apply=False,
            policy=MaintenancePolicy(orphan_age_seconds=0, scan_limit=1, scan_offset=offset),
        )
        observed += result.counts.get("ORPHAN_REMOVED", 0)
        offset = result.next_scan_offset
        if offset == 0:
            break
    assert observed == 3 and result.complete
    result = await sweep(
        db_session, tenant_id, tmp_path, policy=MaintenancePolicy(orphan_age_seconds=0, limit=1)
    )
    assert result.counts["ORPHAN_REMOVED"] == 1 and not result.complete
    assert (tmp_path / document.storage_key).read_bytes() == CONTENT


async def test_malformed_journal_refused_and_no_foreign_path_touched(
    db_session, tenant_and_key, tmp_path
):
    import json

    tenant_id = tenant_and_key[0].id
    directory = tmp_path / ".trash" / tenant_id.hex
    directory.mkdir(parents=True)
    journal = directory / f"{uuid.uuid4().hex}.json"
    journal.write_text(json.dumps({"storage_key": "../../SYNTHETIC_PRIVATE"}))
    result = await sweep(db_session, tenant_id, tmp_path)
    assert result.exit_code == 2 and result.unresolved == 1
    assert journal.exists()
    assert "SYNTHETIC_PRIVATE" not in str(result)


async def test_conversation_retention_refuses_live_reservation_and_live_session(
    db_session, tenant_and_user, tmp_path
):
    from meyar.models.agent_conversation import AgentConversation, AgentConversationSessionContext
    from meyar.services.agent_conversation_repo import OwnerPrincipal, create_conversation
    from meyar.services.browser_session_repo import create_browser_session

    tenant, user, _, membership = tenant_and_user
    tenant_id = tenant.id
    owner = OwnerPrincipal(tenant_id, user.id, membership.id)
    disposable = await create_conversation(db_session, owner=owner)
    reserved = await create_conversation(db_session, owner=owner)
    live = await create_conversation(db_session, owner=owner)
    now = datetime.now(UTC)
    for conversation in (disposable, reserved, live):
        conversation.updated_at = now - timedelta(days=90)
    reserved.active_turn_id = uuid.uuid4()
    reserved.active_turn_expires_at = now + timedelta(minutes=30)
    session, _ = await create_browser_session(
        db_session, user_id=user.id, tenant_membership_id=membership.id, ttl_hours=8,
    )
    db_session.add(AgentConversationSessionContext(
        tenant_id=tenant_id, conversation_id=live.id, browser_session_id=session.id,
    ))
    await db_session.commit()
    retired_id, reserved_id, live_id = disposable.id, reserved.id, live.id
    result = await sweep(db_session, tenant_id, tmp_path,
                         policy=MaintenancePolicy(conversation_days=30))
    assert result.counts["CONVERSATION_RETIRED"] == 1
    remaining = (await db_session.scalars(select(AgentConversation.id))).all()
    assert retired_id not in remaining and reserved_id in remaining and live_id in remaining


async def test_referenced_photo_restore_preserves_exact_bytes(
    db_session, tenant_and_key, tmp_path
):
    from meyar.models.candidate_photo_version import PHOTO_EXTRACTOR_VERSION, CandidatePhotoVersion

    tenant_id = tenant_and_key[0].id
    candidate, document = await seed(db_session, tenant_id, tmp_path)
    storage = LocalPhotoStorage(str(tmp_path))
    key = await storage.save(tenant_id=tenant_id, content=CONTENT)
    row = CandidatePhotoVersion(
        tenant_id=tenant_id, candidate_id=candidate.id, candidate_document_id=document.id,
        extractor_version=PHOTO_EXTRACTOR_VERSION, version_number=1, status="AVAILABLE",
        source_kind="PDF",
        source_locator="synthetic", derived_storage_key=key,
        derived_sha256=hashlib.sha256(CONTENT).hexdigest(), mime_type="image/jpeg",
        width=320, height=420,
    )
    db_session.add(row)
    await db_session.commit()
    await storage.stage_delete(tenant_id=tenant_id, storage_key=key)
    result = await sweep(db_session, tenant_id, tmp_path)
    assert result.exit_code == 0 and result.counts["STAGED_RESTORED"] == 1
    assert await storage.read(tenant_id=tenant_id, storage_key=key) == CONTENT
    assert (await sweep(db_session, tenant_id, tmp_path)).counts.get("ORPHAN_REMOVED", 0) == 0
