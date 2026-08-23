from fastapi import APIRouter

from meyar.api.v1 import candidates, evaluations, health, jobs, search, usage

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(usage.router)
api_router.include_router(jobs.router)
api_router.include_router(candidates.router)
api_router.include_router(search.router)
api_router.include_router(evaluations.router)
