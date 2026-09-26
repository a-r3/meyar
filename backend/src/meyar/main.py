from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from meyar.api.docs import mount_docs_assets
from meyar.api.v1.router import api_router
from meyar.config import get_settings
from meyar.ops.host_config import load_host_settings
from meyar.ui.router import install_ui


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Uvicorn must fail before serving requests when production settings are
    # unsafe, even if the operator skipped meyar-ops config-verify.
    settings = get_settings()
    cwd = Path.cwd()
    if cwd.name == "config" and cwd.parent.name == "shared":
        # The PR5 service runs here. Ambient launchd MEYAR_* variables must
        # not silently override the operator-owned production config file.
        host_settings = load_host_settings(cwd.parent.parent)
        if settings.model_dump() != host_settings.model_dump():
            raise ValueError("Host production configuration was overridden by environment.")
    yield


OPENAPI_TAGS = [
    {"name": "health", "description": "Unauthenticated liveness check."},
    {
        "name": "usage",
        "description": "Tenant-scoped, authenticated usage totals (all-time counters).",
    },
    {
        "name": "jobs",
        "description": "Job postings and their immutable, versioned matching criteria.",
    },
    {
        "name": "candidates",
        "description": (
            "Candidate records, CV document ingestion, and identity/profile-enriched "
            "candidate detail."
        ),
    },
    {
        "name": "search",
        "description": (
            "Structured/hybrid candidate search and natural-language search planning."
        ),
    },
    {
        "name": "evaluations",
        "description": "Deterministic single-candidate JD scoring and batch ranking.",
    },
]

app = FastAPI(
    title="MEYAR",
    description=(
        "Internal AI Candidate Intelligence & CV Search Platform — internal HR REST "
        "API for an approved internal bank-controlled system."
    ),
    version="1.0.0",
    openapi_tags=OPENAPI_TAGS,
    docs_url=None,
    lifespan=_lifespan,
)
app.include_router(api_router)
mount_docs_assets(app)
install_ui(app)
