import logging
import uuid
from datetime import date
from importlib import resources

from fastapi import APIRouter, Depends, FastAPI, Form, Query, Request, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.templating import Jinja2Templates

from meyar.config import Settings, get_settings
from meyar.core.auth import authenticate_raw_api_key
from meyar.db import get_db
from meyar.embedding.dependency import get_embedding_provider, get_embedding_search_config
from meyar.embedding.provider import EmbeddingProvider, EmbeddingProviderError
from meyar.ingestion.validation import PDF_MIME
from meyar.llm.dependency import get_llm_provider
from meyar.llm.provider import LLMProvider
from meyar.scoring.batch import BatchRankingError, rank_candidates_for_job
from meyar.scoring.policy import ScoringPolicyError
from meyar.search.planner_service import plan_and_search_candidates
from meyar.search.schemas import EmbeddingSearchConfig
from meyar.search.service import SearchRequestError
from meyar.services.browser_session_repo import (
    create_browser_session,
    revoke_browser_session_by_id,
)
from meyar.services.candidate_document_repo import get_candidate_document
from meyar.storage.base import DocumentStorage
from meyar.storage.dependency import get_document_storage
from meyar.ui.auth import (
    UI_SESSION_COOKIE,
    UIAccessError,
    UIContext,
    require_ui_scopes,
    verify_csrf,
)
from meyar.ui.presentation import CRITERION_STATUS_LABELS, FIT_BAND_LABELS, STATE_LABELS
from meyar.ui.service import (
    ALLOWED_FOLDER_STATUSES,
    ALLOWED_PARSER_STATUSES,
    ALLOWED_PROFILE_STATUSES,
    UIServiceInputError,
    build_ranked_candidate_views,
    build_search_result_views,
    get_candidate_detail_view,
    list_candidate_library,
    list_job_views,
)
from meyar.ui.view_models import PlannerOutcomeView

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ui", tags=["internal-ui"], include_in_schema=False)

_ui_root = resources.files("meyar.ui")
_template_dir = _ui_root.joinpath("templates")
_static_dir = _ui_root.joinpath("static")
_jinja_env = Environment(
    loader=FileSystemLoader(str(_template_dir)),
    autoescape=select_autoescape(
        enabled_extensions=("html", "xml"), default_for_string=True, default=True
    ),
)
templates = Jinja2Templates(env=_jinja_env)
templates.env.globals.update(
    state_label=lambda value: STATE_LABELS.get(value, value),
    fit_label=lambda value: FIT_BAND_LABELS.get(value, value),
    criterion_label=lambda value: CRITERION_STATUS_LABELS.get(value, value),
)

_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)


class UISecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        if request.url.path == "/ui" or request.url.path.startswith("/ui/"):
            response.headers["Content-Security-Policy"] = _CSP
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Cache-Control"] = "no-store"
        return response


def _context(ctx: UIContext | None = None, **values: object) -> dict[str, object]:
    return {
        "authenticated": ctx is not None,
        "csrf_token": ctx.csrf_token if ctx else None,
        **values,
    }


def _render(
    request: Request,
    name: str,
    context: dict[str, object],
    *,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request, name=name, context=context, status_code=status_code
    )


def _clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        UI_SESSION_COOKIE,
        path="/ui",
        secure=settings.ui_cookie_secure,
        httponly=True,
        samesite="lax",
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    return _render(request, "login.html", _context())


@router.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    api_key: str = Form(...),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    credential = await authenticate_raw_api_key(db, api_key)
    if credential is None:
        await db.rollback()
        return _render(
            request,
            "login.html",
            _context(error="Daxilolma məlumatı etibarlı deyil."),
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    _session, raw_session_token = await create_browser_session(
        db, api_key_id=credential.id, ttl_hours=settings.ui_session_ttl_hours
    )
    await db.commit()
    response = RedirectResponse("/ui", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        UI_SESSION_COOKIE,
        raw_session_token,
        max_age=settings.ui_session_ttl_hours * 60 * 60,
        path="/ui",
        secure=settings.ui_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response


@router.post("/logout")
async def logout(
    csrf_token: str = Form(...),
    ctx: UIContext = Depends(require_ui_scopes()),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    verify_csrf(ctx.csrf_token, csrf_token)
    await revoke_browser_session_by_id(db, session_id=ctx.session_id)
    await db.commit()
    response = RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)
    _clear_session_cookie(response, settings)
    return response


@router.get("", response_class=HTMLResponse)
async def home(
    request: Request, ctx: UIContext = Depends(require_ui_scopes())
) -> HTMLResponse:
    return _render(request, "home.html", _context(ctx))


@router.post("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    query: str = Form(..., min_length=1, max_length=4000),
    as_of_date: date = Form(...),
    csrf_token: str = Form(...),
    ctx: UIContext = Depends(require_ui_scopes("candidates:read")),
    db: AsyncSession = Depends(get_db),
    llm: LLMProvider = Depends(get_llm_provider),
    embedding_provider: EmbeddingProvider = Depends(get_embedding_provider),
    embedding_config: EmbeddingSearchConfig = Depends(get_embedding_search_config),
) -> HTMLResponse:
    verify_csrf(ctx.csrf_token, csrf_token)
    try:
        planned = await plan_and_search_candidates(
            db,
            llm,
            tenant_id=ctx.tenant_id,
            natural_language_request=query,
            as_of_date=as_of_date,
            embedding_config=embedding_config,
            embedding_provider=embedding_provider,
        )
        search_response = planned.search_response
        result_views = (
            await build_search_result_views(
                db, tenant_id=ctx.tenant_id, response=search_response
            )
            if search_response
            else []
        )
        await db.commit()
    except (EmbeddingProviderError, SearchRequestError, SQLAlchemyError):
        await db.rollback()
        outcome = PlannerOutcomeView(
            outcome="INFRASTRUCTURE_FAILURE",
            title="Axtarış xidməti əlçatan deyil",
            message="Axtarış və ya verilənlər bazası xidməti hazırda əlçatan deyil.",
            executable=False,
            reason_codes=[],
            infrastructure_error=True,
        )
        return _render(
            request,
            "search_results.html",
            _context(ctx, query=query, as_of_date=as_of_date, outcome=outcome, results=[]),
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    from meyar.ui.presentation import planner_outcome_view

    outcome = planner_outcome_view(
        planned.plan,
        result_count=search_response.result_count if search_response else None,
    )
    return _render(
        request,
        "search_results.html",
        _context(
            ctx,
            query=query,
            as_of_date=as_of_date,
            outcome=outcome,
            results=result_views,
        ),
    )


@router.get("/library", response_class=HTMLResponse)
async def library(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1),
    parser_status: str | None = Query(default=None),
    profile_status: str | None = Query(default=None),
    folder_status: str | None = Query(default=None),
    ctx: UIContext = Depends(require_ui_scopes("candidates:read")),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    try:
        library_page = await list_candidate_library(
            db,
            tenant_id=ctx.tenant_id,
            page=page,
            page_size=page_size,
            parser_status=parser_status,
            profile_status=profile_status,
            folder_status=folder_status,
        )
    except UIServiceInputError:
        return _render(
            request,
            "error.html",
            _context(ctx, title="Yanlış filtr", message="Kitabxana filtri dəstəklənmir."),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    return _render(
        request,
        "library.html",
        _context(
            ctx,
            library=library_page,
            parser_status=parser_status,
            profile_status=profile_status,
            folder_status=folder_status,
            parser_statuses=sorted(ALLOWED_PARSER_STATUSES),
            profile_statuses=sorted(ALLOWED_PROFILE_STATUSES),
            folder_statuses=sorted(ALLOWED_FOLDER_STATUSES),
        ),
    )


@router.get("/candidates/{candidate_id}", response_class=HTMLResponse)
async def candidate_detail(
    request: Request,
    candidate_id: uuid.UUID,
    ctx: UIContext = Depends(require_ui_scopes("candidates:read")),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    candidate = await get_candidate_detail_view(
        db, tenant_id=ctx.tenant_id, candidate_id=candidate_id
    )
    if candidate is None:
        return _render(
            request,
            "error.html",
            _context(ctx, title="Tapılmadı", message="Namizəd tapılmadı."),
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return _render(request, "candidate_detail.html", _context(ctx, candidate=candidate))


def _original_document_filename(document_id: uuid.UUID, mime_type: str) -> str:
    extension = "pdf" if mime_type == PDF_MIME else "docx"
    return f"cv-{document_id.hex}.{extension}"


@router.get("/candidates/{candidate_id}/documents/{document_id}/original")
async def candidate_document_original(
    request: Request,
    candidate_id: uuid.UUID,
    document_id: uuid.UUID,
    ctx: UIContext = Depends(require_ui_scopes("candidates:read")),
    db: AsyncSession = Depends(get_db),
    storage: DocumentStorage = Depends(get_document_storage),
) -> Response:
    """Authorized original-CV retrieval. storage_key is always resolved
    server-side from the document row — the client supplies only the
    candidate/document UUIDs, never a storage key or filesystem path."""
    document = await get_candidate_document(
        db, tenant_id=ctx.tenant_id, candidate_id=candidate_id, document_id=document_id
    )
    if document is None:
        return _render(
            request,
            "error.html",
            _context(ctx, title="Tapılmadı", message="Sənəd tapılmadı."),
            status_code=status.HTTP_404_NOT_FOUND,
        )
    content = await storage.read(storage_key=document.storage_key)
    filename = _original_document_filename(document.id, document.mime_type)
    disposition = "inline" if document.mime_type == PDF_MIME else "attachment"
    return Response(
        content=content,
        media_type=document.mime_type,
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )


@router.get("/jobs", response_class=HTMLResponse)
async def jobs(
    request: Request,
    ctx: UIContext = Depends(require_ui_scopes("jobs:read")),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    return _render(
        request,
        "jobs.html",
        _context(ctx, jobs=await list_job_views(db, tenant_id=ctx.tenant_id)),
    )


@router.post("/jobs/{job_criteria_version_id}/rank", response_class=HTMLResponse)
async def rank_job(
    request: Request,
    job_criteria_version_id: uuid.UUID,
    evaluation_as_of_date: date = Form(...),
    csrf_token: str = Form(...),
    ctx: UIContext = Depends(
        require_ui_scopes("jobs:read", "candidates:read", "evaluations:write")
    ),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    verify_csrf(ctx.csrf_token, csrf_token)
    try:
        ranking = await rank_candidates_for_job(
            db,
            tenant_id=ctx.tenant_id,
            job_criteria_version_id=job_criteria_version_id,
            evaluation_as_of_date=evaluation_as_of_date,
        )
        results = await build_ranked_candidate_views(
            db, tenant_id=ctx.tenant_id, ranking=ranking
        )
        await db.commit()
    except BatchRankingError as exc:
        await db.rollback()
        code = (
            status.HTTP_404_NOT_FOUND
            if exc.code == "CRITERIA_VERSION_NOT_FOUND"
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        return _render(
            request,
            "error.html",
            _context(
                ctx,
                title="Reytinq icra edilmədi",
                message="Meyar versiyası istifadə edilə bilmədi.",
            ),
            status_code=code,
        )
    except (ScoringPolicyError, SQLAlchemyError):
        await db.rollback()
        return _render(
            request,
            "error.html",
            _context(
                ctx,
                title="Reytinq xidməti əlçatan deyil",
                message="Deterministik reytinq hazırda icra edilə bilmədi.",
            ),
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return _render(
        request,
        "ranking_results.html",
        _context(ctx, ranking=ranking, results=results),
    )


def install_ui(app: FastAPI) -> None:
    app.add_middleware(UISecurityHeadersMiddleware)
    app.mount("/ui/static", StaticFiles(directory=str(_static_dir)), name="ui-static")
    app.include_router(router)

    @app.exception_handler(UIAccessError)
    async def _ui_access_error(request: Request, exc: UIAccessError) -> Response:
        settings = get_settings()
        if exc.status_code == status.HTTP_303_SEE_OTHER:
            response = RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)
            if exc.clear_cookie:
                _clear_session_cookie(response, settings)
            return response
        return _render(
            request,
            "error.html",
            _context(title="Giriş qadağandır", message="Bu əməliyyat üçün icazəniz yoxdur."),
            status_code=exc.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> Response:
        if request.url.path == "/ui" or request.url.path.startswith("/ui/"):
            return _render(
                request,
                "error.html",
                _context(title="Yanlış məlumat", message="Forma məlumatlarını yoxlayın."),
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        from fastapi.exception_handlers import request_validation_exception_handler

        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if request.url.path == "/ui" or request.url.path.startswith("/ui/"):
            return _render(
                request,
                "error.html",
                _context(
                    title="Səhifə əlçatan deyil",
                    message="Sorğu icra edilə bilmədi.",
                ),
                status_code=exc.status_code,
            )
        return await http_exception_handler(request, exc)

    @app.exception_handler(Exception)
    async def _unexpected_error(request: Request, exc: Exception) -> Response:
        if request.url.path == "/ui" or request.url.path.startswith("/ui/"):
            logger.exception("Unhandled internal UI error")
            return _render(
                request,
                "error.html",
                _context(
                    title="Xidmət xətası",
                    message="Gözlənilməz xəta baş verdi. Daha sonra yenidən cəhd edin.",
                ),
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        raise exc
