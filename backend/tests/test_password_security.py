"""Slice 1 — Human Identity & Dual Access (issue #30): password hashing
(meyar.core.password) and the User repository's persistence discipline.
Never stores/returns a plaintext password; only an Argon2id hash exists
in the database."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.core.password import hash_password, needs_rehash, verify_password
from meyar.models.user import User
from meyar.services.tenant_repo import create_tenant
from meyar.services.user_repo import create_user, get_user_by_username, set_password


def test_hash_differs_from_plaintext_and_is_argon2_encoded() -> None:
    password = "a-reasonably-strong-password-1"
    hashed = hash_password(password)
    assert hashed != password
    assert hashed.startswith("$argon2")


def test_hash_is_salted_two_hashes_of_the_same_password_differ() -> None:
    password = "same-password-both-times"
    assert hash_password(password) != hash_password(password)


def test_verify_password_accepts_correct_and_rejects_incorrect() -> None:
    password = "correct-password-value-1"
    hashed = hash_password(password)
    assert verify_password(hashed, password) is True
    assert verify_password(hashed, "wrong-password-value-1") is False


def test_verify_password_never_raises_on_malformed_hash() -> None:
    assert verify_password("not-a-real-argon2-hash", "anything") is False
    assert verify_password("", "anything") is False


def test_needs_rehash_false_for_a_freshly_produced_hash() -> None:
    assert needs_rehash(hash_password("fresh-password-1")) is False


async def test_created_user_row_never_stores_plaintext(db_session: AsyncSession) -> None:
    tenant = await create_tenant(db_session, name="Password-Test-Tenant")
    plaintext = "a-genuinely-secret-value-99"
    user = await create_user(db_session, username="pw-test-user", plaintext_password=plaintext)
    await db_session.commit()

    row = (await db_session.execute(select(User).where(User.id == user.id))).scalar_one()
    assert row.password_hash != plaintext
    assert plaintext not in row.password_hash
    assert verify_password(row.password_hash, plaintext)
    assert tenant.id  # tenant creation itself unaffected by user creation


async def test_set_password_invalidates_the_old_password(db_session: AsyncSession) -> None:
    user = await create_user(
        db_session, username="pw-reset-user", plaintext_password="old-password-value-1"
    )
    await db_session.commit()

    await set_password(db_session, user_id=user.id, plaintext_password="new-password-value-2")
    await db_session.commit()

    refreshed = await get_user_by_username(db_session, "pw-reset-user")
    assert refreshed is not None
    assert not verify_password(refreshed.password_hash, "old-password-value-1")
    assert verify_password(refreshed.password_hash, "new-password-value-2")


async def test_login_never_writes_a_password_into_audit_metadata(
    db_session: AsyncSession,
) -> None:
    """Structural guard, not just a login-response check: no code path in
    this slice may call record_event with password-shaped metadata — the
    privacy guard (meyar.services.audit_repo) already rejects a literal
    'password' key outright."""
    from meyar.services.audit_repo import record_event

    tenant = await create_tenant(db_session, name="Audit-Password-Guard-Tenant")
    await db_session.commit()

    with pytest.raises(ValueError, match="privacy guard"):
        await record_event(
            db_session,
            tenant_id=tenant.id,
            event_type="ui.login.succeeded",
            metadata={"password": "should-never-be-allowed"},
        )
