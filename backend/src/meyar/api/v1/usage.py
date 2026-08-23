from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.core.auth import TenantContext, get_current_tenant
from meyar.db import get_db
from meyar.services.candidate_repo import count_candidates_for_tenant
from meyar.services.evaluation_repo import count_evaluations_for_tenant

router = APIRouter(tags=["usage"])


@router.get("/usage")
async def get_usage(
    ctx: TenantContext = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Protected endpoint proving API-key auth -> tenant context. Real,
    tenant-scoped totals — never a client-suppliable tenant, never a
    cross-tenant aggregate. Rate-limiting/period-bucketed metering remains
    a later slice; `period` is always "all_time" for now."""
    candidates_count = await count_candidates_for_tenant(db, tenant_id=ctx.tenant_id)
    evaluations_count = await count_evaluations_for_tenant(db, tenant_id=ctx.tenant_id)
    return {
        "tenant_id": str(ctx.tenant_id),
        "candidates_count": candidates_count,
        "evaluations_count": evaluations_count,
        "period": "all_time",
    }
