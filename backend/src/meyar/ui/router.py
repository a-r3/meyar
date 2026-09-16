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
from meyar.core.business_date import resolve_business_date
from meyar.core.password import hash_password, needs_rehash, verify_password
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
from meyar.services.audit_repo import ACTOR_HUMAN_USER, record_event
from meyar.services.browser_session_repo import (
    create_browser_session,
    revoke_browser_session_by_id,
)
from meyar.services.candidate_document_repo import get_candidate_document
from meyar.services.job_criteria_repo import create_criteria_version, get_criteria_version_by_id
from meyar.services.job_repo import archive_job, create_job, find_active_duplicate_job
from meyar.services.tenant_membership_repo import (
    get_membership_by_id,
    list_active_memberships_for_user,
)
from meyar.services.tenant_repo import get_tenant
from meyar.services.user_repo import get_user_by_username, set_password
from meyar.storage.base import DocumentStorage
from meyar.storage.dependency import get_document_storage
from meyar.ui.auth import (
    UI_SESSION_COOKIE,
    UIAccessError,
    UIContext,
    require_ui_scopes,
    verify_csrf,
)
from meyar.ui.pending_login import issue_pending_login_token, verify_pending_login_token
from meyar.ui.presentation import (
    CRITERION_KIND_LABELS,
    CRITERION_STATUS_LABELS,
    FIT_BAND_LABELS,
    JOB_STATUS_LABELS,
    STATE_LABELS,
    agent_turn_outcome_message,
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
    authorize_agent_draft_confirmation,
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

# A fixed, valid-shaped Argon2 hash verified against on an unknown
# username so that responding to "unknown user" costs roughly the same
# CPU time as "known user, wrong password" — the login endpoint must not
# leak username existence through a timing side channel either. This is
# not a real credential; it hashes a constant that is never treated as a
# password anywhere else.
_DUMMY_PASSWORD_HASH = hash_password("meyar-login-timing-decoy-never-a-real-password")

_GENERIC_LOGIN_ERROR = "İstifadəçi adı və ya parol yanlışdır."

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
    agent_turn_outcome_message=agent_turn_outcome_message,
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


def _issue_session_cookie(response: Response, raw_session_token: str, settings: Settings) -> None:
    response.set_cookie(
        UI_SESSION_COOKIE,
        raw_session_token,
        max_age=settings.ui_session_ttl_hours * 60 * 60,
        path="/ui",
        secure=settings.ui_cookie_secure,
        httponly=True,
        samesite="lax",
    )


async def _finalize_human_login(
    db: AsyncSession,
    settings: Settings,
    *,
    user_id: uuid.UUID,
    membership_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> Response:
    """The single place a real BrowserSession is minted for a human — both
    the direct single-membership login and the tenant-selection flow call
    this. Always issues a fresh random token (session-fixation prevention:
    no pre-existing/attacker-supplied cookie value is ever reused). Lands
    on MEYAR AI, not the classic search page — Slice 4 (issue #33, D-030)
    makes the agent the primary post-login HR surface; classic search
    remains one click away via the secondary nav."""
    _session, raw_session_token = await create_browser_session(
        db,
        user_id=user_id,
        tenant_membership_id=membership_id,
        ttl_hours=settings.ui_session_ttl_hours,
    )
    await record_event(
        db,
        tenant_id=tenant_id,
        event_type="ui.login.succeeded",
        actor_type=ACTOR_HUMAN_USER,
        actor_id=user_id,
    )
    await db.commit()
    response = RedirectResponse("/ui/agent", status_code=status.HTTP_303_SEE_OTHER)
    _issue_session_cookie(response, raw_session_token, settings)
    return response


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    return _render(request, "login.html", _context())


@router.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    # Passwords are exact opaque strings: verified byte-for-byte as
    # submitted, never normalized. A leading/trailing space is a valid,
    # distinct password character, not incidental noise to discard — only
    # `username` (a login identifier, not a secret) is trimmed. The actual
    # copy-paste-corruption risk this could be confused with is addressed
    # at its real source instead: every CLI-issued secret (see
    # meyar.cli._seed_demo/_create_tenant) is printed alone on its own
    # line, never sharing a line with label text that could soft-wrap.
    user = await get_user_by_username(db, username.strip())
    stored_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
    password_ok = verify_password(stored_hash, password)

    # Generic, identical failure for "no such user", "wrong password", and
    # "disabled user" — never lets a login attempt confirm a username
    # exists or distinguish why it failed. See docs/DECISIONS.md.
    if user is None or not password_ok or not user.is_active:
        await db.rollback()
        return _render(
            request,
            "login.html",
            _context(error=_GENERIC_LOGIN_ERROR),
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    if needs_rehash(user.password_hash):
        await set_password(db, user_id=user.id, plaintext_password=password)

    memberships = await list_active_memberships_for_user(db, user_id=user.id)
    if not memberships:
        await db.rollback()
        return _render(
            request,
            "login.html",
            _context(
                error=(
                    "Hesabınıza heç bir aktiv təşkilat girişi təyin edilməyib. "
                    "Administratorla əlaqə saxlayın."
                )
            ),
            status_code=status.HTTP_403_FORBIDDEN,
        )

    if len(memberships) == 1:
        return await _finalize_human_login(
            db,
            settings,
            user_id=user.id,
            membership_id=memberships[0].id,
            tenant_id=memberships[0].tenant_id,
        )

    # More than one active tenant membership: never silently pick one —
    # require an explicit, server-validated choice (see
    # meyar.ui.pending_login and the /login/select-tenant route below).
    # Every value needed below is captured before the rollback expires
    # these ORM instances — a post-rollback attribute access would
    # otherwise trigger an unawaited lazy-load (MissingGreenlet).
    user_id = user.id
    membership_ids_and_tenant_ids = [(m.id, m.tenant_id) for m in memberships]
    await db.rollback()
    token = issue_pending_login_token(secret=settings.pending_login_secret, user_id=user_id)
    tenant_options = []
    for membership_id, tenant_id in membership_ids_and_tenant_ids:
        tenant = await get_tenant(db, tenant_id)
        tenant_options.append((membership_id, tenant.name if tenant else str(tenant_id)))
    return _render(
        request,
        "select_tenant.html",
        _context(token=token, tenant_options=tenant_options),
    )


@router.post("/login/select-tenant", response_class=HTMLResponse)
async def select_tenant(
    request: Request,
    token: str = Form(...),
    membership_id: uuid.UUID = Form(...),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    user_id = verify_pending_login_token(secret=settings.pending_login_secret, token=token)
    if user_id is None:
        return _render(
            request,
            "login.html",
            _context(error=_GENERIC_LOGIN_ERROR),
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # The membership id is always re-validated live against the database
    # and checked to actually belong to the token's authenticated user and
    # be currently active — a tampered/foreign membership_id never
    # resolves, regardless of what the client submitted.
    membership = await get_membership_by_id(db, membership_id)
    if membership is None or membership.user_id != user_id or not membership.is_active:
        return _render(
            request,
            "login.html",
            _context(error=_GENERIC_LOGIN_ERROR),
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    return await _finalize_human_login(
        db,
        settings,
        user_id=user_id,
        membership_id=membership.id,
        tenant_id=membership.tenant_id,
    )


@router.post("/logout")
async def logout(
    csrf_token: str = Form(...),
    ctx: UIContext = Depends(require_ui_scopes()),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    verify_csrf(ctx.csrf_token, csrf_token)
    await revoke_browser_session_by_id(db, session_id=ctx.session_id)
    await record_event(
        db,
        tenant_id=ctx.tenant_id,
        event_type="ui.logout",
        actor_type=ACTOR_HUMAN_USER,
        actor_id=ctx.user_id,
    )
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
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    verify_csrf(ctx.csrf_token, csrf_token)
    # HR users never choose an evaluation date — the current date is
    # injected here, once, at the UI boundary, and passed explicitly
    # through the same deterministic API/CLI contract below. See
    # docs/DECISIONS.md D-023.
    as_of_date = resolve_business_date(settings.business_timezone)
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


def _agent_turn_log_views(conversation) -> list:
    """Replay only text rendered under the currently accepted server policy.

    Older SERVER_VALIDATED markers do not prove current display authority.
    Their text is replaced by fixed outcome copy, without mutating history.
    User turns remain the HR user's own untrusted text.
    """
    from meyar.services.agent_conversation_repo import (
        ASSISTANT_TEXT_AUTHORITY_SERVER,
        ASSISTANT_TEXT_AUTHORITY_VERSION,
    )
    from meyar.ui.view_models import AgentTurnLogView

    views = []
    for turn in conversation.turns:
        role = turn.get("role", "user")
        if role == "assistant":
            trusted_text = (
                turn.get("text")
                if (
                    turn.get("text_authority") == ASSISTANT_TEXT_AUTHORITY_SERVER
                    and turn.get("text_authority_version") == ASSISTANT_TEXT_AUTHORITY_VERSION
                )
                else None
            )
            text = agent_turn_outcome_message(turn.get("outcome", "ANSWERED"), trusted_text or None)
        else:
            text = turn.get("text", "")
        views.append(AgentTurnLogView(role=role, text=text))
    return views


@router.get("/agent", response_class=HTMLResponse)
async def agent_workspace(
    request: Request,
    ctx: UIContext = Depends(require_ui_scopes("candidates:read")),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    from meyar.services.agent_conversation_repo import get_or_create_conversation

    conversation = await get_or_create_conversation(
        db, tenant_id=ctx.tenant_id, browser_session_id=ctx.session_id
    )
    await db.commit()
    return _render(
        request,
        "agent.html",
        _context(
            ctx,
            history_turns=_agent_turn_log_views(conversation),
            latest=None,
            latest_user_message=None,
            kind_options=CRITERION_KIND_OPTIONS,
        ),
    )


@router.post("/agent", response_class=HTMLResponse)
async def agent_turn(
    request: Request,
    message: str = Form(..., min_length=1, max_length=4000),
    csrf_token: str = Form(...),
    # PR #42 owner correction (issue #33, D-043/D-044): the composer's
    # "Vakansiya elanını analiz et" mode option submits this fixed value
    # so the JD-drafting path is deterministic — never relying on a small
    # local model to infer DRAFT_JOB_CRITERIA routing from arbitrary
    # pasted text (D-042 point 6). Only this one literal value is ever
    # recognized; any other/absent value (the default "Adi söhbət" mode)
    # falls back to normal model-routed conversation, unchanged.
    intent: str | None = Form(default=None, max_length=32),
    ctx: UIContext = Depends(require_ui_scopes("candidates:read")),
    db: AsyncSession = Depends(get_db),
    llm: LLMProvider = Depends(get_llm_provider),
    embedding_provider: EmbeddingProvider = Depends(get_embedding_provider),
    embedding_config: EmbeddingSearchConfig = Depends(get_embedding_search_config),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    verify_csrf(ctx.csrf_token, csrf_token)
    from meyar.agent.schemas import AgentActionType
    from meyar.agent.service import run_agent_turn
    from meyar.services.agent_conversation_repo import (
        get_or_create_conversation,
        sync_last_turn_display_text,
    )
    from meyar.ui.service import build_agent_turn_view

    conversation = await get_or_create_conversation(
        db, tenant_id=ctx.tenant_id, browser_session_id=ctx.session_id
    )
    # Same "current date is a trusted-runtime value, never user/model
    # supplied" boundary as /ui/search (docs/DECISIONS.md D-023).
    as_of_date = resolve_business_date(settings.business_timezone)
    explicit_action = (
        AgentActionType.DRAFT_JOB_CRITERIA if intent == "draft_job_criteria" else None
    )
    try:
        result = await run_agent_turn(
            db,
            llm,
            tenant_id=ctx.tenant_id,
            conversation=conversation,
            user_message=message,
            as_of_date=as_of_date,
            embedding_config=embedding_config,
            embedding_provider=embedding_provider,
            max_tool_calls=settings.agent_max_tool_calls,
            max_context_turns=settings.agent_max_context_turns,
            explicit_action=explicit_action,
        )
        latest = await build_agent_turn_view(db, tenant_id=ctx.tenant_id, result=result)
        # D-045 (PR #42 owner correction, issue #33): make the persisted
        # turn text and the just-rendered live headline the same value, so
        # a later history re-render is byte-for-byte identical to what HR
        # saw live instead of falling back to a generic per-outcome
        # message — see sync_last_turn_display_text's own docstring.
        await sync_last_turn_display_text(db, conversation, text=latest.headline)
        await db.commit()
    except (EmbeddingProviderError, SearchRequestError, SQLAlchemyError):
        await db.rollback()
        from meyar.ui.view_models import AgentTurnView

        latest = AgentTurnView(
            outcome="AGENT_PROVIDER_FAILURE",
            message="MEYAR AI xidməti hazırda əlçatan deyil. Bir qədər sonra yenidən cəhd edin.",
            headline="MEYAR AI xidməti hazırda əlçatan deyil. Bir qədər sonra yenidən cəhd edin.",
        )
        conversation = await get_or_create_conversation(
            db, tenant_id=ctx.tenant_id, browser_session_id=ctx.session_id
        )
        # D-044 (PR #42 owner UX correction): nothing was persisted for
        # this failed attempt (the exception happened before
        # run_agent_turn's own _finish_turn), so `conversation.turns`
        # does not contain it — show the HR user's own just-submitted
        # text directly rather than losing it, still adjacent to its own
        # explanation (chat-hierarchy requirement) instead of history
        # being silently missing a turn.
        return _render(
            request,
            "agent.html",
            _context(
                ctx,
                history_turns=_agent_turn_log_views(conversation),
                latest=latest,
                latest_user_message=message,
                kind_options=CRITERION_KIND_OPTIONS,
            ),
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    # D-044: run_agent_turn always persists exactly one new (user,
    # assistant) pair via its own _finish_turn when it returns without
    # raising — split it off history so it renders once, adjacent to its
    # own rich `latest` cards, instead of duplicated as a plain text
    # bubble AND a rich block separated by the composer.
    all_turns = _agent_turn_log_views(conversation)
    history_turns = all_turns[:-2] if len(all_turns) >= 2 else []
    return _render(
        request,
        "agent.html",
        _context(
            ctx,
            history_turns=history_turns,
            latest=latest,
            latest_user_message=message,
            kind_options=CRITERION_KIND_OPTIONS,
        ),
    )


@router.post("/agent/reset", response_class=HTMLResponse)
async def agent_reset(
    request: Request,
    csrf_token: str = Form(...),
    ctx: UIContext = Depends(require_ui_scopes("candidates:read")),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """"Yeni söhbət" — clears this browser session's own server-held agent
    conversation state (turns + last_search_candidate_ids). Never affects
    another session, tenant, candidate, or job row."""
    verify_csrf(ctx.csrf_token, csrf_token)
    from meyar.services.agent_conversation_repo import (
        get_or_create_conversation,
        reset_conversation,
    )

    conversation = await get_or_create_conversation(
        db, tenant_id=ctx.tenant_id, browser_session_id=ctx.session_id
    )
    await reset_conversation(db, conversation)
    await record_event(
        db,
        tenant_id=ctx.tenant_id,
        event_type="agent.conversation.reset",
        actor_type=ACTOR_HUMAN_USER,
        actor_id=ctx.user_id,
    )
    await db.commit()
    return RedirectResponse("/ui/agent", status_code=status.HTTP_303_SEE_OTHER)


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
            actor_type=ACTOR_HUMAN_USER,
            actor_id=ctx.user_id,
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
        required_level=field("required_level"),
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
        required_level="",
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
    # PR #42 owner correction (issue #33): every HR role already holds
    # every one of these scopes together (meyar.core.roles — deliberately
    # flat, no partial-permission tier exists yet), so declaring them here
    # is honest-intent, not a functional access change. Needed because a
    # job created from the agent's JD-confirmation flow renders straight
    # into the ranking result below instead of a bare redirect.
    ctx: UIContext = Depends(
        require_ui_scopes("jobs:write", "jobs:read", "candidates:read", "evaluations:write")
    ),
    db: AsyncSession = Depends(get_db),
) -> Response:
    verify_csrf(ctx.csrf_token, csrf_token)
    form = await request.form()
    title = str(form.get("title", ""))
    must_have_rows = [_job_form_row(form, "must", i) for i in range(CRITERION_ROW_COUNT)]
    preferred_rows = [_job_form_row(form, "pref", i) for i in range(CRITERION_ROW_COUNT)]

    # Manual creation is a distinct operation. Agent provenance fields are
    # never ignored here: an agent review payload posted to this endpoint
    # cannot be reinterpreted as an unrestricted manual job creation.
    if any(
        key in {"from_agent_draft", "draft_id"} or "_span_id_" in key
        for key in form.keys()
    ):
        return _render(
            request,
            "job_new.html",
            _job_new_context(
                ctx,
                title=title,
                must_have_rows=must_have_rows,
                preferred_rows=preferred_rows,
                error=(
                    "Agent qaralaması yalnız öz təsdiq əməliyyatı ilə təsdiqlənə bilər."
                ),
            ),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

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
            # No API key is involved in a human UI-authored job — this
            # column already models "not machine-authored" as None; the
            # audit event below carries the accountable human actor.
            created_by_api_key_id=None,
        )
        await record_event(
            db,
            tenant_id=ctx.tenant_id,
            event_type="job.created",
            metadata={"job_id": str(job.id), "criteria_version": version.version_number},
            actor_type=ACTOR_HUMAN_USER,
            actor_id=ctx.user_id,
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


def _render_agent_confirmation_error(
    request: Request, ctx: UIContext, *, message: str, status_code: int
) -> HTMLResponse:
    return _render(
        request,
        "error.html",
        _context(ctx, title="Qaralama təsdiqlənmədi", message=message),
        status_code=status_code,
    )


@router.post("/agent/drafts/{draft_id}/resolve", response_class=HTMLResponse)
async def resolve_agent_job_draft_review(
    request: Request,
    draft_id: uuid.UUID,
    csrf_token: str = Form(...),
    span_id: str = Form(..., pattern=r"^req-\d{4}$"),
    criterion_type: str = Form(..., max_length=16),
    ctx: UIContext = Depends(require_ui_scopes("jobs:write", "candidates:read")),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Persist one canonical, server-declared human-review resolution."""
    from meyar.agent.service import resolve_job_draft_review_modality
    from meyar.schemas.criteria import CriterionType
    from meyar.services.agent_conversation_repo import (
        get_conversation_for_update_by_session,
        get_pending_job_draft,
        replace_pending_job_draft,
    )
    from meyar.ui.service import build_agent_job_draft_view
    from meyar.ui.view_models import AgentToolResultView, AgentTurnView

    verify_csrf(ctx.csrf_token, csrf_token)
    try:
        resolved_type = CriterionType(criterion_type)
        conversation = await get_conversation_for_update_by_session(
            db, tenant_id=ctx.tenant_id, browser_session_id=ctx.session_id
        )
        if conversation is None:
            raise UIServiceInputError("Qaralama bu sessiyada tapılmadı.")
        pending = get_pending_job_draft(conversation, draft_id=draft_id)
        if pending is None:
            raise UIServiceInputError("Qaralama bu sessiyada tapılmadı.")
        resolved = resolve_job_draft_review_modality(
            pending, span_id=span_id, criterion_type=resolved_type
        )
        await replace_pending_job_draft(db, conversation, draft=resolved)
        await record_event(
            db,
            tenant_id=ctx.tenant_id,
            event_type="agent.draft.review_resolved",
            metadata={"span_id": span_id, "criterion_type": resolved_type.value},
            actor_type=ACTOR_HUMAN_USER,
            actor_id=ctx.user_id,
        )
        await db.commit()
    except (ValueError, UIServiceInputError) as exc:
        await db.rollback()
        return _render_agent_confirmation_error(
            request, ctx, message=str(exc), status_code=status.HTTP_422_UNPROCESSABLE_CONTENT
        )

    all_turns = _agent_turn_log_views(conversation)
    latest_user_message = all_turns[-2].text if len(all_turns) >= 2 else ""
    latest = AgentTurnView(
        outcome="ANSWERED_FROM_TOOL_RESULT",
        message=None,
        headline=(
            "Dəqiqləşdirmə serverdə yoxlanıldı və yadda saxlanıldı. "
            "Tələbləri təsdiqləyib namizədləri sıralaya bilərsiniz."
        ),
        tool_results=[
            AgentToolResultView(
                tool_name="DRAFT_JOB_CRITERIA",
                job_draft=build_agent_job_draft_view(resolved),
            )
        ],
    )
    return _render(
        request,
        "agent.html",
        _context(
            ctx,
            history_turns=all_turns[:-2] if len(all_turns) >= 2 else [],
            latest=latest,
            latest_user_message=latest_user_message,
            kind_options=CRITERION_KIND_OPTIONS,
        ),
    )


@router.post("/agent/drafts/{draft_id}/confirm", response_class=HTMLResponse)
async def confirm_agent_job_draft(
    request: Request,
    draft_id: uuid.UUID,
    csrf_token: str = Form(...),
    ctx: UIContext = Depends(
        require_ui_scopes("jobs:write", "jobs:read", "candidates:read", "evaluations:write")
    ),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Confirm one session-held canonical draft, then rank separately.

    The route itself establishes agent-confirmation provenance. Browser rows
    are checked as proposals against the locked server draft and never select
    the authorization mode. Confirmation commits before ranking and retains a
    durable result link so replay/retry cannot create another Job or version.
    """
    from meyar.agent.schemas import ConfirmedAgentJobDraft
    from meyar.services.agent_conversation_repo import (
        get_confirmed_job_draft,
        get_conversation_for_update_by_session,
        get_pending_job_draft,
        mark_pending_job_draft_confirmed,
    )
    from meyar.services.agent_draft_confirmation_repo import (
        create_draft_confirmation,
        get_draft_confirmation,
    )

    verify_csrf(ctx.csrf_token, csrf_token)
    evaluation_as_of_date = resolve_business_date(settings.business_timezone)
    form = await request.form()
    must_have_rows = [_job_form_row(form, "must", i) for i in range(CRITERION_ROW_COUNT)]
    preferred_rows = [_job_form_row(form, "pref", i) for i in range(CRITERION_ROW_COUNT)]

    try:
        conversation = await get_conversation_for_update_by_session(
            db, tenant_id=ctx.tenant_id, browser_session_id=ctx.session_id
        )
        durable_confirmation = await get_draft_confirmation(
            db,
            tenant_id=ctx.tenant_id,
            browser_session_id=ctx.session_id,
            draft_id=draft_id,
        )
        if durable_confirmation is not None:
            # Conversation JSON is optional UI state only. Confirmation identity
            # comes from the independent server-owned row; the render below
            # reloads result policy and disclosures from the immutable criteria
            # version.
            transcript_confirmation = (
                get_confirmed_job_draft(conversation, draft_id=draft_id)
                if conversation is not None
                else None
            )
            transcript_agrees = (
                transcript_confirmation is not None
                and transcript_confirmation.job_id == durable_confirmation.job_id
                and transcript_confirmation.criteria_version_id
                == durable_confirmation.criteria_version_id
            )
            confirmed = ConfirmedAgentJobDraft(
                draft_id=durable_confirmation.draft_id,
                job_id=durable_confirmation.job_id,
                criteria_version_id=durable_confirmation.criteria_version_id,
                unsupported_requirements=(
                    transcript_confirmation.unsupported_requirements
                    if transcript_agrees and transcript_confirmation is not None
                    else []
                ),
                needs_review_requirements=(
                    transcript_confirmation.needs_review_requirements
                    if transcript_agrees and transcript_confirmation is not None
                    else []
                ),
            )
            # Release any conversation row lock before potentially expensive
            # ranking. Replay resolves to the database-owned identity.
            await db.commit()
            return await _render_job_ranking(
                request,
                ctx,
                db,
                job_criteria_version_id=confirmed.criteria_version_id,
                unsupported_requirements=confirmed.unsupported_requirements,
                needs_review_requirements=confirmed.needs_review_requirements,
                confirmation_succeeded=True,
                evaluation_as_of_date=evaluation_as_of_date,
            )

        if conversation is None:
            raise UIServiceInputError(
                "Qaralama təsdiqi tapılmadı; elanı yenidən analiz edin."
            )

        pending = get_pending_job_draft(conversation, draft_id=draft_id)
        if pending is None:
            raise UIServiceInputError(
                "Qaralama təsdiqi tapılmadı və ya bu sessiyaya aid deyil; "
                "elanı yenidən analiz edin."
            )

        canonical_title = pending.title or "Vakansiya qaralaması"
        if str(form.get("title", "")).strip() != canonical_title.strip():
            raise UIServiceInputError(
                "Qaralama başlığı serverdə saxlanmış təsdiqli forma ilə uyğun gəlmir."
            )
        job_request = build_job_create_request(
            title=canonical_title,
            must_have_rows=must_have_rows,
            preferred_rows=preferred_rows,
        )
        submitted_span_ids = [
            str(form.get(f"{prefix}_span_id_{index}", ""))
            for prefix, rows in (("must", must_have_rows), ("pref", preferred_rows))
            for index, row in enumerate(rows)
            if row.requirement.strip()
        ]
        authorize_agent_draft_confirmation(
            draft=pending,
            request=job_request,
            submitted_span_ids=submitted_span_ids,
        )

        duplicate_signature = compute_job_duplicate_signature(
            job_request.title, job_request.criteria
        )
        if (
            await find_active_duplicate_job(
                db, tenant_id=ctx.tenant_id, duplicate_signature=duplicate_signature
            )
            is not None
        ):
            await db.rollback()
            return _render_agent_confirmation_error(
                request,
                ctx,
                message=JOB_DUPLICATE_MESSAGE,
                status_code=status.HTTP_409_CONFLICT,
            )

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
            criteria=[criterion.model_dump(mode="json") for criterion in job_request.criteria],
            created_by_api_key_id=None,
            unsupported_requirements=[item.requirement for item in pending.unsupported],
            needs_review_requirements=[item.requirement for item in pending.needs_review],
            result_limit=pending.result_limit,
            eligible_only=True,
        )
        await record_event(
            db,
            tenant_id=ctx.tenant_id,
            event_type="job.created",
            metadata={"job_id": str(job.id), "criteria_version": version.version_number},
            actor_type=ACTOR_HUMAN_USER,
            actor_id=ctx.user_id,
        )
        confirmation = ConfirmedAgentJobDraft(
            draft_id=draft_id,
            job_id=job.id,
            criteria_version_id=version.id,
            result_limit=pending.result_limit,
            unsupported_requirements=[item.requirement for item in pending.unsupported],
            needs_review_requirements=[item.requirement for item in pending.needs_review],
        )
        await create_draft_confirmation(
            db,
            tenant_id=ctx.tenant_id,
            browser_session_id=ctx.session_id,
            draft_id=draft_id,
            job_id=job.id,
            criteria_version_id=version.id,
        )
        await mark_pending_job_draft_confirmed(
            db, conversation, confirmation=confirmation
        )
        await db.commit()
    except UIServiceInputError as exc:
        await db.rollback()
        return _render_agent_confirmation_error(
            request,
            ctx,
            message=str(exc),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    except IntegrityError:
        await db.rollback()
        return _render_agent_confirmation_error(
            request,
            ctx,
            message=JOB_DUPLICATE_MESSAGE,
            status_code=status.HTTP_409_CONFLICT,
        )
    except (ValueError, SQLAlchemyError):
        await db.rollback()
        return _render_agent_confirmation_error(
            request,
            ctx,
            message=(
                "Qaralama təhlükəsiz şəkildə təsdiqlənə bilmədi. "
                "Elanı yenidən analiz edin."
            ),
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return await _render_job_ranking(
        request,
        ctx,
        db,
        job_criteria_version_id=confirmation.criteria_version_id,
        unsupported_requirements=confirmation.unsupported_requirements,
        needs_review_requirements=confirmation.needs_review_requirements,
        confirmation_succeeded=True,
        evaluation_as_of_date=evaluation_as_of_date,
    )


async def _render_job_ranking(
    request: Request,
    ctx: UIContext,
    db: AsyncSession,
    *,
    job_criteria_version_id: uuid.UUID,
    evaluation_as_of_date: date,
    unsupported_requirements: list[str] | None = None,
    needs_review_requirements: list[str] | None = None,
    confirmation_succeeded: bool = False,
) -> HTMLResponse:
    """Shared by the manual "Namizədləri sırala" action (rank_job) and
    the dedicated agent-draft confirmation path — one
    deterministic ranking render, never duplicated. Same UI-boundary rule
    as /search: no manual date input; today's date is injected here and
    threaded explicitly into the deterministic ranking service (D-023).

    Unsupported and review-required source requirements are loaded from
    the immutable criteria version. They remain visible on every later
    reload/re-rank but never become JobCriterion rows and never affect a
    score. The optional arguments only preserve the already-built view if
    the version lookup itself fails."""
    try:
        criteria_version = await get_criteria_version_by_id(
            db, tenant_id=ctx.tenant_id, criteria_version_id=job_criteria_version_id
        )
        if criteria_version is not None:
            unsupported_requirements = list(criteria_version.unsupported_requirements)
            needs_review_requirements = list(criteria_version.needs_review_requirements)
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
        if confirmation_succeeded:
            return _render(
                request,
                "ranking_retry.html",
                _context(
                    ctx,
                    job_criteria_version_id=job_criteria_version_id,
                ),
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if exc.code == "JOB_ARCHIVED":
            return _render(
                request,
                "error.html",
                _context(
                    ctx,
                    title="Vakansiya arxivləşdirilib",
                    message=(
                        "Bu vakansiya arxivləşdirilib və artıq yeni reytinq üçün "
                        "istifadə edilə bilməz."
                    ),
                ),
                status_code=status.HTTP_409_CONFLICT,
            )
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
        if confirmation_succeeded:
            return _render(
                request,
                "ranking_retry.html",
                _context(
                    ctx,
                    job_criteria_version_id=job_criteria_version_id,
                ),
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
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
        _context(
            ctx,
            ranking=ranking,
            results=results,
            job_title=job_title,
            unsupported_requirements=unsupported_requirements or [],
            needs_review_requirements=needs_review_requirements or [],
            canonical_ranking_url=f"/ui/jobs/{job_criteria_version_id}/ranking",
        ),
    )


@router.get("/jobs/{job_criteria_version_id}/ranking", response_class=HTMLResponse)
async def get_job_ranking(
    request: Request,
    job_criteria_version_id: uuid.UUID,
    ctx: UIContext = Depends(
        require_ui_scopes("jobs:read", "candidates:read", "evaluations:write")
    ),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Stable, reload-safe ranking URL for a confirmed criteria version."""
    return await _render_job_ranking(
        request,
        ctx,
        db,
        job_criteria_version_id=job_criteria_version_id,
        evaluation_as_of_date=resolve_business_date(settings.business_timezone),
    )


@router.post("/jobs/{job_criteria_version_id}/rank", response_class=HTMLResponse)
async def rank_job(
    request: Request,
    job_criteria_version_id: uuid.UUID,
    csrf_token: str = Form(...),
    ctx: UIContext = Depends(
        require_ui_scopes("jobs:read", "candidates:read", "evaluations:write")
    ),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    verify_csrf(ctx.csrf_token, csrf_token)
    return await _render_job_ranking(
        request,
        ctx,
        db,
        job_criteria_version_id=job_criteria_version_id,
        evaluation_as_of_date=resolve_business_date(settings.business_timezone),
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
