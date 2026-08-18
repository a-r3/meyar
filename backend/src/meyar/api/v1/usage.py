from fastapi import APIRouter, Depends

from meyar.core.auth import TenantContext, get_current_tenant

router = APIRouter(tags=["usage"])


@router.get("/usage")
async def get_usage(ctx: TenantContext = Depends(get_current_tenant)) -> dict:
    """Protected endpoint proving API-key auth → tenant context. Real
    per-period counters land with rate limiting/metering (later slice);
    for now this proves the auth boundary and always reflects only the
    caller's own tenant, never a client-suppliable one."""
    return {
        "tenant_id": str(ctx.tenant_id),
        "candidates_count": 0,
        "evaluations_count": 0,
        "period": "all_time",
    }
