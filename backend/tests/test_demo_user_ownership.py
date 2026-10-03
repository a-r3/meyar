"""S3: synthetic identities must never confer ownership of a shared User."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from test_demo_seed import _seed

from meyar.config import Settings, get_settings
from meyar.core.password import verify_password
from meyar.main import app
from meyar.models.api_key import ApiKey
from meyar.models.audit_event import AuditEvent
from meyar.models.tenant import Tenant
from meyar.models.tenant_membership import TenantMembership
from meyar.models.user import User
from meyar.services.api_key_repo import create_api_key
from meyar.services.audit_repo import record_event
from meyar.services.browser_session_repo import create_browser_session, get_browser_session_by_token
from meyar.services.candidate_repo import create_candidate
from meyar.services.demo_seed_service import (
    DEMO_TENANT_MARKER_EVENT,
    DEMO_TENANT_NAME,
    DEMO_USER_USERNAME,
    DemoTenantAmbiguousError,
    reset_demo,
)
from meyar.services.tenant_membership_repo import create_membership
from meyar.services.tenant_repo import create_tenant
from meyar.services.user_repo import create_user, get_user_by_username

HUMAN_MARKER = "DEMO_HUMAN_BOOTSTRAPPED"
PASSWORD = "synthetic-original-password"


async def _legacy(db, *, marked=False, shared=False):
    tenant = await create_tenant(db, name=DEMO_TENANT_NAME)
    await record_event(db, tenant_id=tenant.id, event_type=DEMO_TENANT_MARKER_EVENT)
    candidate = await create_candidate(db, tenant_id=tenant.id)
    user = await create_user(db, username=DEMO_USER_USERNAME, plaintext_password=PASSWORD)
    membership = await create_membership(
        db, user_id=user.id, tenant_id=tenant.id, role="HR_USER"
    )
    if marked:
        await record_event(
            db, tenant_id=tenant.id, event_type=HUMAN_MARKER, metadata={"user_id": str(user.id)}
        )
    other = other_membership = session_token = None
    if shared:
        other = await create_tenant(db, name="Synthetic unrelated tenant")
        other_membership = await create_membership(
            db, user_id=user.id, tenant_id=other.id, role="HR_USER"
        )
        _, session_token = await create_browser_session(
            db, user_id=user.id, tenant_membership_id=other_membership.id, ttl_hours=1
        )
    await db.commit()
    return tenant, candidate, user, membership, other, other_membership, session_token


def _security(user):
    return user.password_hash, user.security_version, user.is_active, user.updated_at


async def _assert_login(client, summary):
    app.dependency_overrides[get_settings] = lambda: Settings(ui_cookie_secure=False)
    response = await client.post(
        "/ui/login",
        data={"username": summary.human_username, "password": summary.human_temp_password},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert (await client.get("/ui/library")).status_code == 200


async def test_new_seed_marks_exact_created_human_and_never_logs_plaintext(
    db_session, tmp_path, caplog
):
    summary = await _seed(db_session, tmp_path)
    user = await get_user_by_username(db_session, summary.human_username)
    markers = list((await db_session.scalars(select(AuditEvent).where(
        AuditEvent.tenant_id == summary.tenant_id, AuditEvent.event_type == HUMAN_MARKER
    ))).all())
    assert len(markers) == 1
    assert markers[0].event_metadata == {"user_id": str(user.id)}
    assert verify_password(user.password_hash, summary.human_temp_password)
    keys = list((await db_session.scalars(select(ApiKey))).all())
    assert len(keys) == 1
    persisted = str([row.event_metadata for row in await db_session.scalars(select(AuditEvent))])
    persisted += user.password_hash + keys[0].key_hash + caplog.text
    assert summary.human_temp_password not in persisted
    assert summary.api_key_plaintext not in persisted


async def test_outside_username_collision_preserves_security_and_gets_usable_demo_login(
    db_session, tmp_path, client
):
    other = await create_tenant(db_session, name="Synthetic outside tenant")
    user = await create_user(db_session, username=DEMO_USER_USERNAME, plaintext_password=PASSWORD)
    member = await create_membership(
        db_session, user_id=user.id, tenant_id=other.id, role="HR_USER"
    )
    _, token = await create_browser_session(
        db_session, user_id=user.id, tenant_membership_id=member.id, ttl_hours=1
    )
    key, _ = await create_api_key(db_session, tenant_id=other.id, env="test")
    await db_session.commit()
    before = _security(user), member.security_version, key.key_hash
    summary = await _seed(db_session, tmp_path)
    await db_session.commit()
    await db_session.refresh(user)
    await db_session.refresh(member)
    await db_session.refresh(key)
    assert (_security(user), member.security_version, key.key_hash) == before
    assert key.revoked_at is None and member.is_active
    assert (await get_browser_session_by_token(db_session, token)).revoked_at is None
    assert summary.human_username != DEMO_USER_USERNAME
    await _assert_login(client, summary)


async def test_inactive_other_membership_still_prevents_demo_user_mutation(db_session, tmp_path):
    tenant, _, user, _, _, other_member, _ = await _legacy(
        db_session, marked=True, shared=True
    )
    other_member.is_active = False
    await db_session.commit()
    before = _security(user)
    summary = await _seed(db_session, tmp_path)
    await db_session.commit()
    assert summary.human_username != user.username
    await db_session.refresh(user)
    assert _security(user) == before
    assert await reset_demo(db_session)
    await db_session.commit()
    assert await db_session.get(Tenant, tenant.id) is None
    assert await db_session.get(User, user.id) is not None
    assert await db_session.get(TenantMembership, other_member.id) is not None


async def test_marked_empty_dataset_seed_revokes_only_demo_keys(db_session, tmp_path):
    tenant = await create_tenant(db_session, name=DEMO_TENANT_NAME)
    await record_event(db_session, tenant_id=tenant.id, event_type=DEMO_TENANT_MARKER_EVENT)
    old, _ = await create_api_key(db_session, tenant_id=tenant.id, env="test")
    other = await create_tenant(db_session, name="Synthetic outside key tenant")
    outside, _ = await create_api_key(db_session, tenant_id=other.id, env="test")
    await db_session.commit()
    summary = await _seed(db_session, tmp_path)
    await db_session.commit()
    await db_session.refresh(old)
    await db_session.refresh(outside)
    assert old.revoked_at is not None and outside.revoked_at is None
    active = list((await db_session.scalars(select(ApiKey).where(
        ApiKey.tenant_id == tenant.id, ApiKey.revoked_at.is_(None)
    ))).all())
    assert len(active) == 1 and active[0].prefix == summary.api_key_prefix


async def test_legacy_unmarked_demo_member_is_preserved_with_usable_replacement(
    db_session, tmp_path, client: AsyncClient
):
    tenant, candidate, user, membership, *_ = await _legacy(db_session)
    _, stale_token = await create_browser_session(
        db_session, user_id=user.id, tenant_membership_id=membership.id, ttl_hours=1
    )
    await db_session.commit()
    before = _security(user)
    summary = await _seed(db_session, tmp_path)
    await db_session.commit()
    await db_session.refresh(user)
    await db_session.refresh(membership)
    assert _security(user) == before
    assert summary.tenant_id == tenant.id and summary.already_seeded
    assert summary.candidates_created == 0
    assert await db_session.get(type(candidate), candidate.id) is not None
    assert summary.human_username != user.username
    assert not membership.is_active
    assert (await get_browser_session_by_token(db_session, stale_token)).revoked_at is not None
    await _assert_login(client, summary)
    second = await _seed(db_session, tmp_path)
    await db_session.commit()
    assert second.human_username == summary.human_username
    replacement = await get_user_by_username(db_session, second.human_username)
    assert verify_password(replacement.password_hash, second.human_temp_password)
    assert not verify_password(replacement.password_hash, summary.human_temp_password)


@pytest.mark.parametrize("marked", [False, True])
async def test_shared_demo_member_reseed_preserves_unrelated_session_and_identity(
    db_session, tmp_path, client, marked
):
    tenant, _, user, membership, _, other_membership, token = await _legacy(
        db_session, marked=marked, shared=True
    )
    before = _security(user)
    other_before = other_membership.security_version
    summary = await _seed(db_session, tmp_path)
    await db_session.commit()
    await db_session.refresh(user)
    await db_session.refresh(other_membership)
    assert _security(user) == before
    assert other_membership.is_active and other_membership.security_version == other_before
    assert (await get_browser_session_by_token(db_session, token)).revoked_at is None
    assert summary.human_username != user.username
    client.cookies.set("meyar_ui_session", token, path="/ui")
    assert (await client.get("/ui/library")).status_code == 200
    await _assert_login(client, summary)
    # Replacement is itself marked/exclusive; a repeat must rotate it only.
    repeated = await _seed(db_session, tmp_path)
    await db_session.commit()
    assert repeated.human_username == summary.human_username
    assert await reset_demo(db_session)
    await db_session.commit()
    assert await db_session.get(Tenant, tenant.id) is None
    assert await db_session.get(User, user.id) is not None
    assert await db_session.get(TenantMembership, other_membership.id) is not None
    assert (await get_browser_session_by_token(db_session, token)).revoked_at is None
    await db_session.refresh(user)
    assert _security(user) == before


@pytest.mark.parametrize("marked", [False, True])
@pytest.mark.parametrize("shared", [False, True])
async def test_reset_deletes_only_marked_exclusive_human(db_session, marked, shared):
    tenant, _, user, _, _, other_membership, token = await _legacy(
        db_session, marked=marked, shared=shared
    )
    user_id, tenant_id, before = user.id, tenant.id, _security(user)
    assert await reset_demo(db_session)
    await db_session.commit()
    assert await db_session.get(Tenant, tenant_id) is None
    survivor = await db_session.get(User, user_id)
    if marked and not shared:
        assert survivor is None
    else:
        assert survivor is not None and _security(survivor) == before
    if shared:
        assert await db_session.get(TenantMembership, other_membership.id) is not None
        assert (await get_browser_session_by_token(db_session, token)).revoked_at is None


@pytest.mark.parametrize("metadata", [
    "duplicate", "conflicting", "invalid", "missing_user", "dangling_link", "duplicate_user_link",
])
@pytest.mark.parametrize("operation", ["seed", "reset"])
async def test_ambiguous_human_markers_fail_before_any_mutation(
    db_session, tmp_path, metadata, operation
):
    tenant, _, user, membership, *_ = await _legacy(db_session, marked=True)
    _, key = await create_api_key(db_session, tenant_id=tenant.id, env="test")
    extra = {"user_id": str(user.id)}
    if metadata in {"conflicting", "missing_user"}:
        extra = {"user_id": str(uuid.uuid4())}
    if metadata == "invalid":
        extra = {"user_id": "invalid"}
    if metadata == "dangling_link":
        extra["previous_marker_id"] = str(uuid.uuid4())
    if metadata == "duplicate_user_link":
        marker = (await db_session.scalars(select(AuditEvent).where(
            AuditEvent.event_type == HUMAN_MARKER
        ))).one()
        extra["previous_marker_id"] = str(marker.id)
    if metadata in {"invalid", "missing_user"}:
        marker = (await db_session.scalars(select(AuditEvent).where(
            AuditEvent.event_type == HUMAN_MARKER
        ))).one()
        marker.event_metadata = extra
    else:
        await record_event(db_session, tenant_id=tenant.id, event_type=HUMAN_MARKER, metadata=extra)
    await db_session.commit()
    before = _security(user)
    with pytest.raises(DemoTenantAmbiguousError):
        if operation == "seed":
            await _seed(db_session, tmp_path)
        else:
            await reset_demo(db_session)
    # Deliberately inspect without rollback: no partial destructive work is allowed.
    await db_session.refresh(user)
    await db_session.refresh(membership)
    assert _security(user) == before and membership.is_active
    assert await db_session.get(Tenant, tenant.id) is not None
    assert (await db_session.scalars(select(ApiKey).where(ApiKey.revoked_at.is_(None)))).one()
    assert key
