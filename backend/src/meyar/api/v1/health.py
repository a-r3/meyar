from fastapi import APIRouter, Depends, Response, status

from meyar.config import Settings, get_settings
from meyar.core.readiness import readiness_reasons
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
    """DB/schema, local-model and storage readiness; independent of liveness.
    Failures expose closed codes, never configuration or candidate data."""
    snapshot = probe_saturation(
        settings,
        admission=current_inference_admission(),
        pool=get_engine().sync_engine.pool,
    )
    reasons = [reason.value for reason in snapshot.reasons]
    if not reasons:
        reasons = await readiness_reasons(get_engine(), settings)
    if not reasons:
        return {"status": "ready"}
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "not_ready", "reasons": reasons}
