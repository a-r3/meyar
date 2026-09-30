"""Process-local saturation probe for readiness (issue #85, D-089).

Reports only closed, non-sensitive codes: never queue contents, prompts,
model text, candidate data, user ids, or even exact queue/pool figures.
Liveness (``GET /api/v1/health``) deliberately does NOT use this — a busy
local model is a reason to stop routing new AI work to this process, never a
reason to restart it. The broader liveness/readiness design (DB/migration/
storage/model reachability) remains issue #46."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.pool import Pool, QueuePool

from meyar.config import Settings
from meyar.llm.concurrency import InferenceAdmission


class SaturationCode(StrEnum):
    INFERENCE_SATURATED = "INFERENCE_SATURATED"
    DB_POOL_SATURATED = "DB_POOL_SATURATED"


@dataclass(frozen=True)
class SaturationSnapshot:
    reasons: tuple[SaturationCode, ...]

    @property
    def ready(self) -> bool:
        return not self.reasons


def probe_saturation(
    settings: Settings,
    *,
    admission: InferenceAdmission | None,
    pool: Pool | None,
) -> SaturationSnapshot:
    """``INFERENCE_SATURATED``: every inference slot busy AND the bounded
    wait queue full, continuously for at least
    ``inference_saturation_grace_seconds`` (a momentarily full queue is
    normal backpressure). ``DB_POOL_SATURATED``: every pooled connection
    (``db_pool_size + db_max_overflow``) is checked out right now, so the
    next DB-backed request would wait on ``db_pool_timeout_seconds``."""
    reasons: list[SaturationCode] = []
    if admission is not None:
        snapshot = admission.snapshot()
        if (
            snapshot.saturated
            and snapshot.saturated_for_seconds >= settings.inference_saturation_grace_seconds
        ):
            reasons.append(SaturationCode.INFERENCE_SATURATED)
    if isinstance(pool, QueuePool):
        if pool.checkedout() >= settings.db_pool_size + settings.db_max_overflow:
            reasons.append(SaturationCode.DB_POOL_SATURATED)
    return SaturationSnapshot(reasons=tuple(reasons))
