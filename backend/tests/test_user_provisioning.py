"""Slice 1 — Human Identity & Dual Access (issue #30): the minimal
provisioning surface (meyar.services.user_repo /
meyar.services.tenant_membership_repo, backing the `meyar` CLI's
create-user/add-membership/set-password/disable-user/enable-user/
disable-membership/enable-membership commands). Exercises the repository
layer directly — the CLI itself only prompts interactively (getpass) and
delegates to these same functions; see meyar.cli."""

from meyar.core.password import verify_password
from meyar.core.roles import VALID_ROLES, is_valid_role, permissions_for_role
from meyar.services.tenant_membership_repo import (
    create_membership,
    get_membership_for_user_and_tenant,
    list_active_memberships_for_user,
    set_membership_active,
)
from meyar.services.tenant_repo import create_tenant
from meyar.services.user_repo import (
    create_user,
    get_user_by_id,
    get_user_by_username,
    set_password,
    set_user_active,
)


async def test_create_user_then_add_membership_grants_active_access(db_session) -> None:
    tenant = await create_tenant(db_session, name="Provisioning-Tenant")
    user = await create_user(
        db_session, username="new-hr-user", plaintext_password="operator-set-password-1"
    )
    membership = await create_membership(
        db_session, user_id=user.id, tenant_id=tenant.id, role="HR_USER"
    )
    await db_session.commit()

    fetched = await get_user_by_username(db_session, "new-hr-user")
    assert fetched is not None
    assert fetched.is_active
    memberships = await list_active_memberships_for_user(db_session, user_id=user.id)
    assert [m.id for m in memberships] == [membership.id]


async def test_new_user_has_no_tenant_access_until_membership_granted(db_session) -> None:
    user = await create_user(
        db_session, username="unaffiliated-user", plaintext_password="password-value-1"
    )
    await db_session.commit()

    memberships = await list_active_memberships_for_user(db_session, user_id=user.id)
    assert memberships == []


async def test_set_password_replaces_the_hash(db_session) -> None:
    user = await create_user(
        db_session, username="rotate-me", plaintext_password="first-password-value-1"
    )
    await db_session.commit()

    await set_password(db_session, user_id=user.id, plaintext_password="second-password-value-2")
    await db_session.commit()

    refreshed = await get_user_by_id(db_session, user.id)
    assert refreshed is not None
    assert not verify_password(refreshed.password_hash, "first-password-value-1")
    assert verify_password(refreshed.password_hash, "second-password-value-2")


async def test_disable_user_then_enable_user_round_trips(db_session) -> None:
    user = await create_user(
        db_session, username="toggle-user", plaintext_password="password-value-1"
    )
    await db_session.commit()

    await set_user_active(db_session, user_id=user.id, is_active=False)
    await db_session.commit()
    disabled = await get_user_by_id(db_session, user.id)
    assert disabled is not None and disabled.is_active is False

    await set_user_active(db_session, user_id=user.id, is_active=True)
    await db_session.commit()
    enabled = await get_user_by_id(db_session, user.id)
    assert enabled is not None and enabled.is_active is True


async def test_disable_membership_then_enable_membership_round_trips(db_session) -> None:
    tenant = await create_tenant(db_session, name="Membership-Toggle-Tenant")
    user = await create_user(
        db_session, username="membership-toggle-user", plaintext_password="password-value-1"
    )
    membership = await create_membership(
        db_session, user_id=user.id, tenant_id=tenant.id, role="HR_USER"
    )
    await db_session.commit()

    await set_membership_active(db_session, membership_id=membership.id, is_active=False)
    await db_session.commit()
    disabled = await get_membership_for_user_and_tenant(
        db_session, user_id=user.id, tenant_id=tenant.id
    )
    assert disabled is not None and disabled.is_active is False
    active_only = await list_active_memberships_for_user(db_session, user_id=user.id)
    assert active_only == []

    await set_membership_active(db_session, membership_id=membership.id, is_active=True)
    await db_session.commit()
    active_only_again = await list_active_memberships_for_user(db_session, user_id=user.id)
    assert [m.id for m in active_only_again] == [membership.id]


async def test_disabling_one_tenants_membership_does_not_affect_another(db_session) -> None:
    """A user with multiple tenant memberships: disabling one must leave
    the others untouched — schema and repo layer never assume a
    single-tenant user (see docs/DECISIONS.md)."""
    tenant_a = await create_tenant(db_session, name="Multi-Tenant-A")
    tenant_b = await create_tenant(db_session, name="Multi-Tenant-B")
    user = await create_user(
        db_session, username="multi-tenant-user", plaintext_password="password-value-1"
    )
    membership_a = await create_membership(
        db_session, user_id=user.id, tenant_id=tenant_a.id, role="HR_USER"
    )
    await create_membership(db_session, user_id=user.id, tenant_id=tenant_b.id, role="HR_USER")
    await db_session.commit()

    await set_membership_active(db_session, membership_id=membership_a.id, is_active=False)
    await db_session.commit()

    active = await list_active_memberships_for_user(db_session, user_id=user.id)
    assert len(active) == 1
    assert active[0].tenant_id == tenant_b.id


def test_role_constants_are_valid_and_unknown_role_grants_no_permissions() -> None:
    assert is_valid_role("HR_USER")
    assert is_valid_role("ADMIN")
    assert not is_valid_role("SOMETHING_ELSE")
    assert VALID_ROLES == frozenset({"HR_USER", "ADMIN"})
    assert permissions_for_role("SOMETHING_ELSE") == frozenset()
    assert permissions_for_role("HR_USER")  # non-empty
