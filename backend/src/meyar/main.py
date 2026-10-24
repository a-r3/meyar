from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from meyar.api.body_limit import DocumentUploadBodyLimitMiddleware
from meyar.api.docs import mount_docs_assets
from meyar.api.openapi import install_openapi_security
from meyar.api.response_policy import APIResponsePolicyMiddleware
from meyar.api.v1.router import api_router
from meyar.config import Settings, get_settings
from meyar.ops.host_config import load_runtime_host_settings
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
        host_settings = load_runtime_host_settings()
        if settings.model_dump() != host_settings.model_dump():
            raise ValueError("Host production configuration was overridden by environment.")
    yield


OPENAPI_TAGS = [
    {
        "name": "health",
        "description": "Liveness and bounded DB/schema/local-model/storage readiness.",
    },
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
        "description": (
            "Deterministic single-candidate JD scoring, batch ranking and persisted "
            "evaluations."
        ),
    },
    {
        "name": "human-conversations",
        "description": (
            "Accountable-human JSON: session, conversations, turns and JD draft review / "
            "confirmation. Session cookie + X-CSRF-Token; API keys are never accepted."
        ),
    },
    {
        "name": "human-candidates",
        "description": "Candidate library, detail, original CV and retry (human session).",
    },
    {
        "name": "human-jobs",
        "description": "Job lifecycle and criteria provenance (human session).",
    },
    {
        "name": "human-evaluations",
        "description": "Scoring, ranking and evaluations (human session).",
    },
]

def create_app(settings: Settings | None = None) -> FastAPI:
    """Fix documentation exposure at construction, from trusted runtime config."""
    settings = settings or get_settings()
    docs_enabled = settings.env != "production"
    application = FastAPI(
        title="MEYAR",
        description=(
            "Internal AI Candidate Intelligence & CV Search Platform — internal HR REST "
            "API for an approved internal bank-controlled system.\n\n"
            "Two authentication models, never mixed: machine clients use an API key "
            "(`ApiKeyBearer`, `/api/v1`); accountable humans use the interactive-login "
            "BrowserSession cookie (`HumanSessionCookie`) plus an `X-CSRF-Token` header on "
            "every state-changing request (`HumanCsrfToken`, `/ui/api/v1`). Only the human "
            "surface can own conversations or confirm a job draft. Errors use the "
            "`ErrorEnvelope` contract on all issue-#45 routes."
        ),
        version="1.0.0",
        openapi_tags=OPENAPI_TAGS,
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_enabled else None,
        lifespan=_lifespan,
    )
    application.include_router(api_router)
    if docs_enabled:
        mount_docs_assets(application)
    # Bound before multipart parsing, inside the existing safe-error boundary.
    # Response policies do not consume the request body.
    application.add_middleware(DocumentUploadBodyLimitMiddleware)
    install_ui(application)
    install_openapi_security(application)
    # Covers early body-limit refusals as well as normal and safe-error responses.
    application.add_middleware(APIResponsePolicyMiddleware)
    return application


app = create_app()
