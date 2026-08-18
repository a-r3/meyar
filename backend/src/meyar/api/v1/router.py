from fastapi import APIRouter

from meyar.api.v1 import health, usage

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(usage.router)
