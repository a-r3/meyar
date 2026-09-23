"""Ops-specific configuration, deliberately kept separate from
`meyar.config.Settings` (candidate/API runtime configuration) — meyar-ops
is operationally separate tooling, not a business-logic surface, even
though it reuses the same `pydantic-settings` conventions and the same
`MEYAR_` env boundary via its own `MEYAR_OPS_` prefix."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class OpsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MEYAR_OPS_", env_file=".env", extra="ignore")

    # Preflight disk-space gate. Conservative MVP default for a
    # single-host deployment; not a provisioning topology decision.
    min_free_disk_mb: int = Field(default=2048, ge=0)

    # Optional override for the Alembic config file location. Defaults to
    # `backend/alembic.ini` resolved relative to this installed package's
    # own source tree (works for the current editable/dev checkout).
    # A future packaged/deployed release layout may not carry the source
    # tree alongside the installed package — that install layout is
    # deferred to the artifact-building PR; this override exists so a
    # deployed host can point at the right file without code changes.
    alembic_ini_path: str | None = None


@lru_cache
def get_ops_settings() -> OpsSettings:
    return OpsSettings()


def resolve_alembic_ini_path(settings: OpsSettings | None = None) -> Path | None:
    """Best-effort, non-raising resolution of alembic.ini. Returns None
    (never raises) when it cannot be located — callers must turn that
    into a truthful Finding, not an uncaught exception."""
    settings = settings or get_ops_settings()
    if settings.alembic_ini_path:
        path = Path(settings.alembic_ini_path)
        return path if path.is_file() else None
    # backend/src/meyar/ops/config.py -> parents[3] == backend/
    candidate = Path(__file__).resolve().parents[3] / "alembic.ini"
    return candidate if candidate.is_file() else None
