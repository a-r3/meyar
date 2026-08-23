"""Slice 12 — fully offline Swagger UI. FastAPI's default `/docs` pulls
`swagger-ui-bundle.js`/`swagger-ui.css` from `cdn.jsdelivr.net`, a real
gap for bank-controlled/offline infrastructure. This module serves the
same assets from the locally-vendored `swagger-ui-bundle` package instead
— zero runtime network dependency. See docs/DECISIONS.md."""

from fastapi import APIRouter, FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from swagger_ui_bundle import swagger_ui_path

DOCS_ASSETS_MOUNT = "/docs-assets"

router = APIRouter(include_in_schema=False)


@router.get("/docs", include_in_schema=False)
async def offline_swagger_ui() -> HTMLResponse:
    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title="MEYAR — API Docs",
        swagger_js_url=f"{DOCS_ASSETS_MOUNT}/swagger-ui-bundle.js",
        swagger_css_url=f"{DOCS_ASSETS_MOUNT}/swagger-ui.css",
        swagger_favicon_url=f"{DOCS_ASSETS_MOUNT}/favicon-32x32.png",
    )


def mount_docs_assets(app: FastAPI) -> None:
    app.mount(DOCS_ASSETS_MOUNT, StaticFiles(directory=str(swagger_ui_path)), name="docs-assets")
    app.include_router(router)
