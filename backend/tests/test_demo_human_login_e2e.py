"""Regression coverage for the Slice 1 demo-human-login blocker: a
correct, freshly-generated `seed-demo` password was being rejected by
`/ui/login`. Root cause was NOT the hashing/generation logic (which was
already correct) but the login route not tolerating incidental
leading/trailing whitespace the way it already did for `username` —
exactly the kind of corruption a terminal-copied secret can pick up.
Fixed in meyar.ui.router.login (password.strip() before verification,
consistently reused for the needs_rehash persistence path too) and in
meyar.cli (the secret is now printed alone on its own line, never
sharing a line with descriptive text, to reduce the chance of that
corruption happening in the first place).

Exercises the real HTTP /ui/login path end to end — not just the
repository/hashing functions — using meyar.services.demo_seed_service
directly (the same safe hook the CLI itself calls), never parsing
secrets out of captured terminal output."""

from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.config import Settings, get_settings
from meyar.core.password import verify_password
from meyar.ingestion.parsers.local_text_parser import LocalTextParser
from meyar.main import app
from meyar.models.audit_event import AuditEvent
from meyar.models.user import User
from meyar.services.demo_seed_service import DEMO_USER_USERNAME, reset_demo, seed_demo
from meyar.services.user_repo import get_user_by_username
from meyar.storage.local import LocalFilesystemStorage

MAX_BYTES = 10 * 1024 * 1024
MAX_INPUT_CHARS = 20000


@pytest.fixture
def local_ui_settings() -> Settings:
    settings = Settings(ui_cookie_secure=False)
    app.dependency_overrides[get_settings] = lambda: settings
    return settings


async def _seed(db_session: AsyncSession, tmp_path: Path):
    storage = LocalFilesystemStorage(root=str(tmp_path / "storage"))
    parser = LocalTextParser()
    return await seed_demo(
        db_session,
        storage,
        parser,
        max_bytes=MAX_BYTES,
        max_profile_input_chars=MAX_INPUT_CHARS,
        max_identity_input_chars=MAX_INPUT_CHARS,
        max_embedding_input_chars=MAX_INPUT_CHARS,
    )


async def test_freshly_seeded_demo_credential_logs_in_over_real_http(
    client: AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
    local_ui_settings: Settings,
) -> None:
    """The exact bug report: seed-demo's printed username+password must
    work immediately against the real /ui/login HTTP path, and the
    resulting session must actually authorize a protected route."""
    summary = await _seed(db_session, tmp_path)
    await db_session.commit()
    assert summary.human_username == DEMO_USER_USERNAME

    response = await client.post(
        "/ui/login",
        data={"username": summary.human_username, "password": summary.human_temp_password},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui"
    assert response.cookies.get("meyar_ui_session") is not None

    library = await client.get("/ui/library")
    assert library.status_code == 200


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda p: p + "\n",
        lambda p: p + " ",
        lambda p: " " + p,
        lambda p: "  " + p + "  \n",
    ],
    ids=["trailing-newline", "trailing-space", "leading-space", "both"],
)
async def test_incidental_copy_paste_whitespace_around_password_still_logs_in(
    client: AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
    local_ui_settings: Settings,
    corrupt,
) -> None:
    """Regression for the actual reported blocker: a stray newline/space
    picked up when copying the password out of a terminal (a realistic
    outcome of soft-wrapped terminal text) must not turn a correct
    password into a rejected one."""
    summary = await _seed(db_session, tmp_path)
    await db_session.commit()

    response = await client.post(
        "/ui/login",
        data={"username": summary.human_username, "password": corrupt(summary.human_temp_password)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui"


async def test_incidental_whitespace_around_username_still_logs_in(
    client: AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
    local_ui_settings: Settings,
) -> None:
    summary = await _seed(db_session, tmp_path)
    await db_session.commit()

    response = await client.post(
        "/ui/login",
        data={
            "username": f"  {summary.human_username}  ",
            "password": summary.human_temp_password,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


async def test_genuinely_wrong_password_still_rejected(
    client: AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
    local_ui_settings: Settings,
) -> None:
    """The whitespace-tolerance fix must not weaken real rejection."""
    summary = await _seed(db_session, tmp_path)
    await db_session.commit()

    response = await client.post(
        "/ui/login",
        data={"username": summary.human_username, "password": "definitely-not-the-password"},
        follow_redirects=False,
    )
    assert response.status_code == 401


async def test_api_key_is_never_accepted_as_the_human_password(
    client: AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
    local_ui_settings: Settings,
) -> None:
    summary = await _seed(db_session, tmp_path)
    await db_session.commit()
    assert summary.api_key_plaintext is not None

    response = await client.post(
        "/ui/login",
        data={"username": summary.human_username, "password": summary.api_key_plaintext},
        follow_redirects=False,
    )
    assert response.status_code == 401


async def test_reseed_rotation_invalidates_old_password_over_real_http(
    client: AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
    local_ui_settings: Settings,
) -> None:
    first = await _seed(db_session, tmp_path)
    await db_session.commit()

    second = await _seed(db_session, tmp_path)
    await db_session.commit()
    assert second.human_temp_password != first.human_temp_password

    stale = await client.post(
        "/ui/login",
        data={"username": first.human_username, "password": first.human_temp_password},
        follow_redirects=False,
    )
    assert stale.status_code == 401

    fresh = await client.post(
        "/ui/login",
        data={"username": second.human_username, "password": second.human_temp_password},
        follow_redirects=False,
    )
    assert fresh.status_code == 303


async def test_reset_then_reseed_cycle_produces_a_working_login(
    client: AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
    local_ui_settings: Settings,
) -> None:
    """Covers the reset_demo bug fixed alongside this one: a reset that
    orphaned the demo User row would have made this reseed's credential
    unusable (or raised DemoTenantAmbiguousError on the next seed)."""
    await _seed(db_session, tmp_path)
    await db_session.commit()

    deleted = await reset_demo(db_session)
    await db_session.commit()
    assert deleted is True

    reseeded = await _seed(db_session, tmp_path)
    await db_session.commit()

    response = await client.post(
        "/ui/login",
        data={"username": reseeded.human_username, "password": reseeded.human_temp_password},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui"


async def test_no_plaintext_password_persisted_and_no_credential_in_audit(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    summary = await _seed(db_session, tmp_path)
    await db_session.commit()

    user_row = (
        await db_session.execute(select(User).where(User.username == summary.human_username))
    ).scalar_one()
    assert user_row.password_hash != summary.human_temp_password
    assert summary.human_temp_password not in user_row.password_hash
    assert verify_password(user_row.password_hash, summary.human_temp_password)

    events = (
        await db_session.execute(
            select(AuditEvent).where(AuditEvent.tenant_id == summary.tenant_id)
        )
    ).scalars().all()
    for event in events:
        assert summary.human_temp_password not in str(event.event_metadata)
        assert summary.api_key_plaintext not in str(event.event_metadata)


async def test_disabled_demo_user_is_still_correctly_rejected_after_the_fix(
    client: AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
    local_ui_settings: Settings,
) -> None:
    """The whitespace-tolerance fix must not accidentally bypass the
    live is_active recheck it sits next to."""
    summary = await _seed(db_session, tmp_path)
    await db_session.commit()

    user = await get_user_by_username(db_session, summary.human_username)
    assert user is not None
    await db_session.execute(
        User.__table__.update().where(User.id == user.id).values(is_active=False)
    )
    await db_session.commit()

    response = await client.post(
        "/ui/login",
        data={"username": summary.human_username, "password": summary.human_temp_password},
        follow_redirects=False,
    )
    assert response.status_code == 401
