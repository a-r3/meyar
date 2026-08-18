"""Cross-tenant isolation tests. See docs/SECURITY_PRIVACY.md testing
priority #1: tenant A must never be able to access tenant B's resources."""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.services.api_key_repo import create_api_key
from meyar.services.tenant_repo import create_tenant


async def test_usage_never_leaks_another_tenants_data(
    client: AsyncClient, db_session: AsyncSession, tenant_and_key
) -> None:
    tenant_a, _key_a, plaintext_a = tenant_and_key
    tenant_b = await create_tenant(db_session, name="Tenant B")
    _key_b, plaintext_b = await create_api_key(db_session, tenant_id=tenant_b.id, env="test")
    await db_session.commit()

    resp_a = await client.get("/api/v1/usage", headers={"Authorization": f"Bearer {plaintext_a}"})
    resp_b = await client.get("/api/v1/usage", headers={"Authorization": f"Bearer {plaintext_b}"})

    assert resp_a.json()["tenant_id"] == str(tenant_a.id)
    assert resp_b.json()["tenant_id"] == str(tenant_b.id)
    assert resp_a.json()["tenant_id"] != resp_b.json()["tenant_id"]


async def test_tenant_cannot_use_another_tenants_key_identity(
    db_session: AsyncSession, tenant_and_key
) -> None:
    """The tenant identity returned by auth is derived solely from the
    matched API key's own tenant_id column — there is no client-suppliable
    tenant parameter anywhere in the auth path to spoof."""
    from meyar.services.api_key_repo import get_api_key_by_plaintext

    tenant_a, _key_a, plaintext_a = tenant_and_key
    resolved = await get_api_key_by_plaintext(db_session, plaintext_a)
    assert resolved is not None
    assert resolved.tenant_id == tenant_a.id
