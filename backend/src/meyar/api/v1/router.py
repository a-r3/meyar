from fastapi import APIRouter

from meyar.api.contracts import AuthPolicy, build_contract_router
from meyar.api.v1 import candidates, evaluations, health, jobs, search, usage
from meyar.core.auth import require_scope

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(usage.router)
api_router.include_router(jobs.router)
api_router.include_router(candidates.router)
api_router.include_router(search.router)
api_router.include_router(evaluations.router)
api_router.include_router(
    build_contract_router(
        AuthPolicy(read=require_scope, write=require_scope, channel="API"),
        tags={"candidates": "candidates", "jobs": "jobs", "evaluations": "evaluations"},
    )
)
