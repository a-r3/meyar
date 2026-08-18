from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.services.api_key_repo import create_api_key


async def test_health_is_public(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_usage_requires_auth(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/usage")
    assert resp.status_code == 401


async def test_usage_rejects_garbage_key(client: AsyncClient) -> None:
    resp = await client.get(
        "/api/v1/usage", headers={"Authorization": "Bearer meyar_test_not_a_real_key"}
    )
    assert resp.status_code == 401


async def test_usage_accepts_valid_key(client: AsyncClient, tenant_and_key) -> None:
    tenant, _api_key, plaintext = tenant_and_key
    resp = await client.get("/api/v1/usage", headers={"Authorization": f"Bearer {plaintext}"})
    assert resp.status_code == 200
    assert resp.json()["tenant_id"] == str(tenant.id)


async def test_revoked_key_is_rejected(
    client: AsyncClient, db_session: AsyncSession, tenant_and_key
) -> None:
    from meyar.models.api_key import ApiKey

    _tenant, api_key, plaintext = tenant_and_key
    await db_session.execute(
        ApiKey.__table__.update()
        .where(ApiKey.id == api_key.id)
        .values(revoked_at=datetime.now(UTC))
    )
    await db_session.commit()

    resp = await client.get("/api/v1/usage", headers={"Authorization": f"Bearer {plaintext}"})
    assert resp.status_code == 401


async def test_expired_key_is_rejected(
    client: AsyncClient, db_session: AsyncSession, tenant_and_key
) -> None:
    tenant, _api_key, _plaintext = tenant_and_key
    expired_key, expired_plaintext = await create_api_key(
        db_session,
        tenant_id=tenant.id,
        env="test",
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    await db_session.commit()

    resp = await client.get(
        "/api/v1/usage", headers={"Authorization": f"Bearer {expired_plaintext}"}
    )
    assert resp.status_code == 401


async def test_malformed_auth_header_rejected(client: AsyncClient, tenant_and_key) -> None:
    _tenant, _api_key, plaintext = tenant_and_key
    resp = await client.get("/api/v1/usage", headers={"Authorization": plaintext})
    assert resp.status_code == 401
