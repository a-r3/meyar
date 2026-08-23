from fastapi import FastAPI

from meyar.api.docs import mount_docs_assets
from meyar.api.v1.router import api_router
from meyar.ui.router import install_ui

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
)
app.include_router(api_router)
mount_docs_assets(app)
install_ui(app)
