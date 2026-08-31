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
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
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
from meyar.models.job import JOB_STATUS_ACTIVE, JOB_STATUS_ARCHIVED
from meyar.scoring.batch import BatchRankingError, rank_candidates_for_job
from meyar.scoring.policy import ScoringPolicyError
from meyar.search.planner_policy import find_skill_specific_duration_mention
from meyar.search.planner_schemas import PlannerOutcome, PlannerReasonCode
from meyar.search.planner_service import plan_and_search_candidates
from meyar.search.schemas import EmbeddingSearchConfig
from meyar.search.service import SearchRequestError
from meyar.services.audit_repo import record_event
from meyar.services.browser_session_repo import (
    create_browser_session,
    revoke_browser_session_by_id,
)
from meyar.services.candidate_document_repo import get_candidate_document
from meyar.services.job_criteria_repo import create_criteria_version
from meyar.services.job_repo import archive_job, create_job, find_active_duplicate_job
from meyar.storage.base import DocumentStorage
from meyar.storage.dependency import get_document_storage
from meyar.ui.auth import (
    UI_SESSION_COOKIE,
    UIAccessError,
    UIContext,
    require_ui_scopes,
    verify_csrf,
)
from meyar.ui.presentation import (
    CRITERION_KIND_LABELS,
    CRITERION_STATUS_LABELS,
    FIT_BAND_LABELS,
    JOB_STATUS_LABELS,
    STATE_LABELS,
    readiness_label,
    readiness_state,
)
from meyar.ui.service import (
    ALLOWED_FOLDER_STATUSES,
    ALLOWED_PARSER_STATUSES,
    ALLOWED_PROFILE_STATUSES,
    CRITERION_KIND_OPTIONS,
    CRITERION_ROW_COUNT,
    DEFAULT_CRITERION_WEIGHT,
    JOB_DUPLICATE_MESSAGE,
    CriterionRowInput,
    UIServiceInputError,
    build_job_create_request,
    build_ranked_candidate_views,
    build_search_result_views,
    compute_job_duplicate_signature,
    get_candidate_detail_view,
    get_candidate_document_preview,
    get_job_title_for_criteria_version,
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
    criterion_kind_label=lambda value: CRITERION_KIND_LABELS.get(value, value),
    job_status_label=lambda value: JOB_STATUS_LABELS.get(value, value),
    readiness_label=readiness_label,
    readiness_state=readiness_state,
)

_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)


class UISecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
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
async def home(request: Request, ctx: UIContext = Depends(require_ui_scopes())) -> HTMLResponse:
    return _render(request, "home.html", _context(ctx))


@router.post("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    query: str = Form(..., min_length=1, max_length=4000),
    csrf_token: str = Form(...),
    ctx: UIContext = Depends(require_ui_scopes("candidates:read")),
    db: AsyncSession = Depends(get_db),
    llm: LLMProvider = Depends(get_llm_provider),
    embedding_provider: EmbeddingProvider = Depends(get_embedding_provider),
    embedding_config: EmbeddingSearchConfig = Depends(get_embedding_search_config),
) -> HTMLResponse:
    verify_csrf(ctx.csrf_token, csrf_token)
    # HR users never choose an evaluation date — the current date is
    # injected here, once, at the UI boundary, and passed explicitly
    # through the same deterministic API/CLI contract below. See
    # docs/DECISIONS.md D-023.
    as_of_date = date.today()
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
            await build_search_result_views(db, tenant_id=ctx.tenant_id, response=search_response)
            if search_response
            else []
        )
        await db.commit()
    except (EmbeddingProviderError, SearchRequestError, SQLAlchemyError):
        await db.rollback()
        outcome = PlannerOutcomeView(
            outcome="INFRASTRUCTURE_FAILURE",
            title="Axtarış xidməti əlçatan deyil",
            message="Axtarış xidməti hazırda əlçatan deyil. Bir qədər sonra yenidən cəhd edin.",
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

    plan = planned.plan
    if (
        plan.outcome == PlannerOutcome.UNSUPPORTED_SEMANTICS
        and PlannerReasonCode.SKILL_SPECIFIC_EXPERIENCE_DURATION_UNSUPPORTED in plan.reason_codes
    ):
        # A skill-SPECIFIC duration claim ("N years IN skill X") cannot be
        # proven from CandidateProfile evidence (docs/DECISIONS.md D-027)
        # — never silently weakened into "skill + total experience".
        # Offer the HR user that weaker-but-honest alternative explicitly
        # instead of a generic failure page; it only ever executes if
        # they click through, which resubmits an unambiguous rephrasing
        # of their own request via the normal /ui/search flow below.
        clarification = find_skill_specific_duration_mention(query)
        if clarification is not None:
            skill, years = clarification
            years_text = f"{years:g}"
            return _render(
                request,
                "search_clarification.html",
                _context(
                    ctx,
                    query=query,
                    skill=skill,
                    years=years_text,
                    confirmed_query=(
                        f"{skill} bilən və ümumi iş təcrübəsi {years_text} il olan"
                    ),
                ),
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
    # Always a true download ("Originalı yüklə" in the UI): the safe,
    # already-parsed in-app view is the separate /preview route below.
    # A browser-inline PDF response here would silently turn the "download"
    # action into an "open" action for PDFs only — see docs/DECISIONS.md D-023.
    return Response(
        content=content,
        media_type=document.mime_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/candidates/{candidate_id}/documents/{document_id}/preview",
    response_class=HTMLResponse,
)
async def candidate_document_preview(
    request: Request,
    candidate_id: uuid.UUID,
    document_id: uuid.UUID,
    ctx: UIContext = Depends(require_ui_scopes("candidates:read")),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """The truthful 'CV-yə bax' in-app view: already-parsed, safe text
    (never the original bytes, never a live-model call). Same tenant
    authorization as the original-document route."""
    preview = await get_candidate_document_preview(
        db, tenant_id=ctx.tenant_id, candidate_id=candidate_id, document_id=document_id
    )
    if preview is None:
        return _render(
            request,
            "error.html",
            _context(ctx, title="Tapılmadı", message="Sənəd tapılmadı."),
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return _render(
        request,
        "candidate_document_preview.html",
        _context(ctx, preview=preview),
    )


@router.get("/jobs", response_class=HTMLResponse)
async def jobs(
    request: Request,
    status_filter: str = Query(default=JOB_STATUS_ACTIVE, alias="status"),
    ctx: UIContext = Depends(require_ui_scopes("jobs:read")),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    normalized_status = status_filter.strip().upper()
    if normalized_status not in (JOB_STATUS_ACTIVE, JOB_STATUS_ARCHIVED):
        return _render(
            request,
            "error.html",
            _context(ctx, title="Yanlış filtr", message="Vakansiya filtri dəstəklənmir."),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    return _render(
        request,
        "jobs.html",
        _context(
            ctx,
            jobs=await list_job_views(db, tenant_id=ctx.tenant_id, status=normalized_status),
            status_filter=normalized_status,
        ),
    )


@router.post("/jobs/{job_id}/archive", response_class=HTMLResponse)
async def archive_job_route(
    request: Request,
    job_id: uuid.UUID,
    csrf_token: str = Form(...),
    ctx: UIContext = Depends(require_ui_scopes("jobs:write")),
    db: AsyncSession = Depends(get_db),
) -> Response:
    verify_csrf(ctx.csrf_token, csrf_token)
    try:
        job = await archive_job(db, tenant_id=ctx.tenant_id, job_id=job_id)
        if job is None:
            await db.rollback()
            return _render(
                request,
                "error.html",
                _context(ctx, title="Tapılmadı", message="Vakansiya tapılmadı."),
                status_code=status.HTTP_404_NOT_FOUND,
            )
        await record_event(
            db,
            tenant_id=ctx.tenant_id,
            event_type="job.archived",
            metadata={"job_id": str(job.id)},
        )
        await db.commit()
    except SQLAlchemyError:
        await db.rollback()
        return _render(
            request,
            "error.html",
            _context(
                ctx,
                title="Vakansiya arxivləşdirilmədi",
                message=(
                    "Verilənlər bazası hazırda əlçatan deyil. "
                    "Bir qədər sonra yenidən cəhd edin."
                ),
            ),
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return RedirectResponse("/ui/jobs", status_code=status.HTTP_303_SEE_OTHER)


def _job_form_row(form: object, prefix: str, index: int) -> CriterionRowInput:
    def field(name: str) -> str:
        value = form.get(f"{prefix}_{name}_{index}")  # type: ignore[attr-defined]
        return value if isinstance(value, str) else ""

    return CriterionRowInput(
        kind=field("kind"),
        requirement=field("requirement"),
        min_years=field("min_years"),
        weight=field("weight"),
    )


def _job_new_context(
    ctx: UIContext,
    *,
    title: str,
    must_have_rows: list[CriterionRowInput],
    preferred_rows: list[CriterionRowInput],
    error: str | None,
) -> dict[str, object]:
    return _context(
        ctx,
        title=title,
        must_have_rows=must_have_rows,
        preferred_rows=preferred_rows,
        kind_options=CRITERION_KIND_OPTIONS,
        error=error,
    )


@router.get("/jobs/new", response_class=HTMLResponse)
async def job_new_form(
    request: Request, ctx: UIContext = Depends(require_ui_scopes("jobs:write"))
) -> HTMLResponse:
    empty_row = CriterionRowInput(
        kind=CRITERION_KIND_OPTIONS[0][0],
        requirement="",
        min_years="",
        weight=DEFAULT_CRITERION_WEIGHT,
    )
    return _render(
        request,
        "job_new.html",
        _job_new_context(
            ctx,
            title="",
            must_have_rows=[empty_row] * CRITERION_ROW_COUNT,
            preferred_rows=[empty_row] * CRITERION_ROW_COUNT,
            error=None,
        ),
    )


@router.post("/jobs", response_class=HTMLResponse)
async def create_job_route(
    request: Request,
    csrf_token: str = Form(...),
    ctx: UIContext = Depends(require_ui_scopes("jobs:write")),
    db: AsyncSession = Depends(get_db),
) -> Response:
    verify_csrf(ctx.csrf_token, csrf_token)
    form = await request.form()
    title = str(form.get("title", ""))
    must_have_rows = [_job_form_row(form, "must", i) for i in range(CRITERION_ROW_COUNT)]
    preferred_rows = [_job_form_row(form, "pref", i) for i in range(CRITERION_ROW_COUNT)]

    try:
        job_request = build_job_create_request(
            title=title, must_have_rows=must_have_rows, preferred_rows=preferred_rows
        )
    except UIServiceInputError as exc:
        return _render(
            request,
            "job_new.html",
            _job_new_context(
                ctx,
                title=title,
                must_have_rows=must_have_rows,
                preferred_rows=preferred_rows,
                error=str(exc),
            ),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    duplicate_signature = compute_job_duplicate_signature(
        job_request.title, job_request.criteria
    )
    # Fast, friendly pre-check for the common (non-racing) case — the
    # actual concurrency-safe guard against a double-submit race is the
    # partial unique index on (tenant_id, duplicate_signature) WHERE
    # status='ACTIVE' (see the IntegrityError handling below), not this
    # check alone. See docs/DECISIONS.md D-028.
    existing_duplicate = await find_active_duplicate_job(
        db, tenant_id=ctx.tenant_id, duplicate_signature=duplicate_signature
    )
    if existing_duplicate is not None:
        return _render(
            request,
            "job_new.html",
            _job_new_context(
                ctx,
                title=title,
                must_have_rows=must_have_rows,
                preferred_rows=preferred_rows,
                error=JOB_DUPLICATE_MESSAGE,
            ),
            status_code=status.HTTP_409_CONFLICT,
        )

    try:
        # Reuses the exact same domain services as the internal REST API's
        # POST /api/v1/jobs (meyar.api.v1.jobs.post_job) — one job/criteria
        # creation path for both surfaces, one deterministic scoring model.
        job = await create_job(
            db,
            tenant_id=ctx.tenant_id,
            title=job_request.title,
            duplicate_signature=duplicate_signature,
        )
        version = await create_criteria_version(
            db,
            tenant_id=ctx.tenant_id,
            job_id=job.id,
            criteria=[c.model_dump(mode="json") for c in job_request.criteria],
            created_by_api_key_id=ctx.api_key_id,
        )
        await record_event(
            db,
            tenant_id=ctx.tenant_id,
            event_type="job.created",
            metadata={"job_id": str(job.id), "criteria_version": version.version_number},
        )
        await db.commit()
    except IntegrityError:
        # A concurrent double-submit raced past the pre-check above and
        # hit the DB-level partial unique index — the actual guard, not
        # just this application-level check. Same friendly message.
        await db.rollback()
        return _render(
            request,
            "job_new.html",
            _job_new_context(
                ctx,
                title=title,
                must_have_rows=must_have_rows,
                preferred_rows=preferred_rows,
                error=JOB_DUPLICATE_MESSAGE,
            ),
            status_code=status.HTTP_409_CONFLICT,
        )
    except SQLAlchemyError:
        await db.rollback()
        return _render(
            request,
            "error.html",
            _context(
                ctx,
                title="Vakansiya yaradıla bilmədi",
                message=(
                    "Verilənlər bazası hazırda əlçatan deyil. Bir qədər sonra yenidən cəhd edin."
                ),
            ),
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return RedirectResponse("/ui/jobs", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/jobs/{job_criteria_version_id}/rank", response_class=HTMLResponse)
async def rank_job(
    request: Request,
    job_criteria_version_id: uuid.UUID,
    csrf_token: str = Form(...),
    ctx: UIContext = Depends(
        require_ui_scopes("jobs:read", "candidates:read", "evaluations:write")
    ),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    verify_csrf(ctx.csrf_token, csrf_token)
    # Same UI-boundary rule as /search — no manual date input; today's
    # date is injected here and threaded explicitly into the deterministic
    # ranking service. See docs/DECISIONS.md D-023.
    evaluation_as_of_date = date.today()
    try:
        ranking = await rank_candidates_for_job(
            db,
            tenant_id=ctx.tenant_id,
            job_criteria_version_id=job_criteria_version_id,
            evaluation_as_of_date=evaluation_as_of_date,
        )
        results = await build_ranked_candidate_views(db, tenant_id=ctx.tenant_id, ranking=ranking)
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
    job_title = await get_job_title_for_criteria_version(
        db, tenant_id=ctx.tenant_id, job_criteria_version_id=job_criteria_version_id
    )
    return _render(
        request,
        "ranking_results.html",
        _context(ctx, ranking=ranking, results=results, job_title=job_title),
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
