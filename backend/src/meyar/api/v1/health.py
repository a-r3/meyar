from fastapi import APIRouter, Depends, Response, status

from meyar.config import Settings, get_settings
from meyar.core.saturation import probe_saturation
from meyar.db import get_engine
from meyar.llm.concurrency import current_inference_admission

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    """Liveness only: the process is up. Never reflects AI/DB load — a busy
    local model must not trigger a process restart (issue #85, D-089)."""
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(
    response: Response, settings: Settings = Depends(get_settings)
) -> dict:
    """Minimal process-local readiness (issue #85; the full readiness design
    is issue #46). ``503`` with closed reason codes while the process is
    seriously saturated (inference gate persistently full, or DB pool
    exhausted); ``200`` otherwise. Reads no database and no candidate data."""
    snapshot = probe_saturation(
        settings,
        admission=current_inference_admission(),
        pool=get_engine().sync_engine.pool,
    )
    if snapshot.ready:
        return {"status": "ready"}
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "not_ready", "reasons": [reason.value for reason in snapshot.reasons]}
