import hashlib
import logging
import uuid
from datetime import date
from importlib import resources

from fastapi import APIRouter, Depends, FastAPI, Form, Query, Request, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.templating import Jinja2Templates

from meyar.agent.service import ClarificationButton, normalize_message_newlines
from meyar.api.diagnostic_errors import DiagnosticErrorMiddleware
from meyar.api.errors import (
    application_error_handler,
    http_error_response,
    internal_error_response,
    is_human_json_path,
    unauthenticated_response,
    validation_error_response,
)
from meyar.api.human.router import human_router
from meyar.application.conversations import (
    NOT_RUN_PRESENTATION,
    TurnDisposition,
    canonical_turn_message,
    open_conversation,
    start_new_conversation,
    submit_conversation_turn,
    turn_log_entries,
)
from meyar.application.drafts import (
    DraftProposal,
    DraftRejected,
    confirm_job_draft,
    resolve_draft_review,
)
from meyar.application.errors import ApplicationError
from meyar.config import Settings, get_settings
from meyar.core.business_date import resolve_business_date
from meyar.core.password import hash_password, needs_rehash, verify_password
from meyar.db import get_db
from meyar.diagnostics import log_failure
from meyar.embedding.db_release import DbReleasingEmbeddingProvider
from meyar.embedding.dependency import get_embedding_provider, get_embedding_search_config
from meyar.embedding.provider import (
    EmbeddingBusyError,
    EmbeddingProvider,
    EmbeddingProviderError,
)
from meyar.ingestion.validation import PDF_MIME
from meyar.llm.dependency import get_llm_provider
from meyar.llm.provider import LLMProvider
from meyar.models.job import JOB_STATUS_ACTIVE, JOB_STATUS_ARCHIVED
from meyar.schemas.job import JobCreateRequest
from meyar.scoring.batch import BatchRankingError, rank_candidates_for_job
from meyar.scoring.policy import ScoringPolicyError
from meyar.search.planner_policy import find_skill_specific_duration_mention
from meyar.search.planner_schemas import PlannerOutcome, PlannerReasonCode
from meyar.search.planner_service import plan_and_search_candidates
from meyar.search.schemas import EmbeddingSearchConfig
from meyar.search.service import SearchRequestError
from meyar.services.audit_repo import ACTOR_HUMAN_USER, record_event
from meyar.services.auth_security_event_repo import (
    LOGIN_REJECTED,
    NO_ACTIVE_MEMBERSHIP,
    PENDING_TOKEN_INVALID,
    TENANT_SELECTION_INVALID,
    record_auth_failure,
)
from meyar.services.browser_session_repo import (
    create_browser_session,
    revoke_browser_session_by_id,
)
from meyar.services.candidate_document_repo import get_candidate_document
from meyar.services.candidate_photo_service import PLACEHOLDER_JPEG, current_presentable_photo
from meyar.services.candidate_repo import get_candidate
from meyar.services.job_criteria_repo import create_criteria_version, get_criteria_version_by_id
from meyar.services.job_repo import archive_job, create_job, find_active_duplicate_job
from meyar.services.tenant_authority import (
    TenantInactiveError,
    require_active_tenant,
    tenant_is_active,
)
from meyar.services.tenant_membership_repo import (
    get_membership_by_id,
    list_active_memberships_for_user,
)
from meyar.services.tenant_repo import get_tenant
from meyar.services.user_repo import get_user_by_id, get_user_by_username, set_password
from meyar.storage.base import DocumentStorage
from meyar.storage.dependency import get_document_storage, get_photo_storage
from meyar.storage.photo import LocalPhotoStorage
from meyar.ui.agent_workspace import build_agent_workspace_context
from meyar.ui.auth import (
    UI_SESSION_COOKIE,
    UIAccessError,
    UIContext,
    require_ui_scopes,
    resolve_ui_context,
    verify_csrf,
)
from meyar.ui.pending_login import issue_pending_login_token, verify_pending_login_token
from meyar.ui.presentation import (
    CRITERION_KIND_LABELS,
    CRITERION_STATUS_LABELS,
    FIT_BAND_LABELS,
    INFERENCE_BUSY_TEXT,
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
    agent_draft_requires_resolution,
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
from meyar.ui.view_models import AgentTurnView, PlannerOutcomeView

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ui", tags=["internal-ui"], include_in_schema=False)
_FORM_TRANSPORT_LIMIT = 8192  # 4000 LF textarea chars can arrive as 8000 CRLF chars.


def _canonical_hr_text(value: str) -> str | None:
    return canonical_turn_message(value)


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
    await require_active_tenant(db, tenant_id, lock=True)
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
    user = await get_user_by_username(db, username.strip(), for_update=True)
    stored_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
    password_ok = verify_password(stored_hash, password)

    # Generic, identical failure for "no such user", "wrong password", and
    # "disabled user" — never lets a login attempt confirm a username
    # exists or distinguish why it failed. See docs/DECISIONS.md.
    if user is None or not password_ok or not user.is_active:
        failed_user_id = user.id if user is not None else None
        await db.rollback()
        await record_auth_failure(
            db, outcome_code=LOGIN_REJECTED, user_id=failed_user_id
        )
        await db.commit()
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
        failed_user_id = user.id
        await db.rollback()
        await record_auth_failure(db, outcome_code=NO_ACTIVE_MEMBERSHIP, user_id=failed_user_id)
        await db.commit()
        return _render(
            request,
            "login.html",
            _context(error=_GENERIC_LOGIN_ERROR),
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    if len(memberships) == 1:
        # User was locked first; Tenant must precede the Membership lock.
        if not await tenant_is_active(db, memberships[0].tenant_id, lock=True):
            await db.rollback()
            return _render(
                request, "login.html", _context(error=_GENERIC_LOGIN_ERROR),
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        selected_membership = await get_membership_by_id(
            db, memberships[0].id, for_update=True
        )
        if (
            selected_membership is None or not selected_membership.is_active
            or selected_membership.user_id != user.id
        ):
            failed_user_id = user.id
            await db.rollback()
            await record_auth_failure(
                db, outcome_code=NO_ACTIVE_MEMBERSHIP, user_id=failed_user_id
            )
            await db.commit()
            return _render(
                request, "login.html", _context(error=_GENERIC_LOGIN_ERROR),
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        return await _finalize_human_login(
            db,
            settings,
            user_id=user.id,
            membership_id=selected_membership.id,
            tenant_id=selected_membership.tenant_id,
        )

    # More than one active tenant membership: never silently pick one —
    # require an explicit, server-validated choice (see
    # meyar.ui.pending_login and the /login/select-tenant route below).
    # Capture the verified stamps before ending this transaction. Commit
    # any password rehash/revocation so the signed claim describes durable
    # security state; rolling it back would make the freshly issued claim stale.
    user_id = user.id
    user_security_version = user.security_version
    membership_ids_and_tenant_ids = [(m.id, m.tenant_id) for m in memberships]
    membership_versions = {m.id: m.security_version for m in memberships}
    await db.commit()
    token = issue_pending_login_token(
        secret=settings.pending_login_secret, user_id=user_id,
        user_security_version=user_security_version, membership_versions=membership_versions,
    )
    tenant_options = []
    for membership_id, tenant_id in membership_ids_and_tenant_ids:
        tenant = await get_tenant(db, tenant_id)
        if tenant is not None and await tenant_is_active(db, tenant_id):
            tenant_options.append((membership_id, tenant.name))
    return _render(
        request,
        "select_tenant.html",
        _context(token=token, tenant_options=tenant_options),
    )


@router.post("/login/select-tenant", response_class=HTMLResponse)
async def select_tenant(
    request: Request,
    token: str = Form(...),
    membership_id: str = Form(...),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    claim = verify_pending_login_token(secret=settings.pending_login_secret, token=token)
    if claim is None:
        await record_auth_failure(db, outcome_code=PENDING_TOKEN_INVALID)
        await db.commit()
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
    try:
        selected_membership_id = uuid.UUID(membership_id)
    except ValueError:
        selected_membership_id = None
    user = await get_user_by_id(db, claim.user_id, for_update=True)
    selected = (
        await get_membership_by_id(db, selected_membership_id)
        if selected_membership_id is not None else None
    )
    tenant_active = (
        selected is not None
        and await tenant_is_active(db, selected.tenant_id, lock=True)
    )
    membership = (
        await get_membership_by_id(db, selected_membership_id, for_update=True)
        if selected_membership_id is not None else None
    )
    if (
        user is None or not user.is_active
        or user.security_version != claim.user_security_version
        or membership is None or membership.user_id != claim.user_id
        or not membership.is_active or not tenant_active
        or claim.membership_versions.get(membership.id) != membership.security_version
    ):
        await db.rollback()
        await record_auth_failure(
            db, outcome_code=TENANT_SELECTION_INVALID,
            user_id=claim.user_id if user is not None else None,
        )
        await db.commit()
        return _render(
            request,
            "login.html",
            _context(error=_GENERIC_LOGIN_ERROR),
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    return await _finalize_human_login(
        db,
        settings,
        user_id=claim.user_id,
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
    query: str = Form(default="", max_length=_FORM_TRANSPORT_LIMIT),
    csrf_token: str = Form(...),
    ctx: UIContext = Depends(require_ui_scopes("candidates:read")),
    db: AsyncSession = Depends(get_db),
    llm: LLMProvider = Depends(get_llm_provider),
    embedding_provider: EmbeddingProvider = Depends(get_embedding_provider),
    embedding_config: EmbeddingSearchConfig = Depends(get_embedding_search_config),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    verify_csrf(ctx.csrf_token, csrf_token)
    canonical_query = _canonical_hr_text(query)
    if canonical_query is None:
        return _render(
            request, "error.html",
            _context(ctx, title="Yanlış məlumat", message="Mətn 1–4000 simvol olmalıdır."),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    query = canonical_query
    # HR users never choose an evaluation date — the current date is
    # injected here, once, at the UI boundary, and passed explicitly
    # through the same deterministic API/CLI contract below. See
    # docs/DECISIONS.md D-023.
    as_of_date = resolve_business_date(settings.business_timezone)
    # Issue #85 (D-089): end the read-only auth transaction so this request
    # does not keep a pooled connection checked out while the NL planner
    # waits for / runs local inference (the planner itself touches the DB
    # only after the model returns, to append its audit event).
    await db.commit()
    try:
        planned = await plan_and_search_candidates(
            db,
            llm,
            tenant_id=ctx.tenant_id,
            natural_language_request=query,
            as_of_date=as_of_date,
            embedding_config=embedding_config,
            embedding_provider=DbReleasingEmbeddingProvider(embedding_provider, db),
        )
        search_response = planned.search_response
        result_views = (
            await build_search_result_views(db, tenant_id=ctx.tenant_id, response=search_response)
            if search_response
            else []
        )
        await db.commit()
    except (EmbeddingProviderError, SearchRequestError, SQLAlchemyError) as exc:
        await db.rollback()
        if isinstance(exc, EmbeddingBusyError):
            # Issue #85: shared local-inference gate busy — transient, not an
            # unavailable service.
            title, message = INFERENCE_BUSY_TEXT
            outcome = PlannerOutcomeView(
                outcome="INFERENCE_BUSY",
                title=title,
                message=message,
                executable=False,
                reason_codes=[],
                infrastructure_error=True,
            )
        else:
            outcome = PlannerOutcomeView(
                outcome="INFRASTRUCTURE_FAILURE",
                title="Axtarış xidməti əlçatan deyil",
                message=(
                    "Axtarış xidməti hazırda əlçatan deyil. Bir qədər sonra yenidən cəhd edin."
                ),
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


# The transcript replay policy lives in the shared application boundary; the
# private name stays as the adapter's established vocabulary.
_agent_turn_log_views = turn_log_entries


def _conversation_owner(ctx: UIContext):  # noqa: ANN202 - OwnerPrincipal (lazy import)
    """The durable-conversation owner is ALWAYS the live, re-derived UIContext
    principal (issue #80) — never a stored or client-supplied owner."""
    return ctx.owner()


def _render_conversation_not_found(request: Request, ctx: UIContext) -> HTMLResponse:
    """Identical for a foreign-tenant, foreign-user, foreign-membership, or
    nonexistent conversation id — no existence/ownership/title/timestamp
    distinction is ever revealed."""
    return _render(
        request,
        "error.html",
        _context(ctx, title="Tapılmadı", message="Söhbət tapılmadı."),
        status_code=status.HTTP_404_NOT_FOUND,
    )


async def _render_agent_turn_not_run(
    request: Request,
    ctx: UIContext,
    db: AsyncSession,
    settings: Settings,
    *,
    owner,  # noqa: ANN001 - OwnerPrincipal (lazy import)
    conversation_id: uuid.UUID,
    message: str,
    outcome: str,
    copy: str,
    status_code: int,
) -> HTMLResponse:
    from meyar.services.agent_conversation_repo import get_owned_conversation

    reloaded = await get_owned_conversation(db, owner=owner, conversation_id=conversation_id)
    if reloaded is None:
        return _render_conversation_not_found(request, ctx)
    latest = AgentTurnView(outcome=outcome, message=copy, headline=copy)
    return await _render_agent_workspace(
        request, ctx, db, settings, conversation=reloaded,
        history_turns=_agent_turn_log_views(reloaded), latest=latest,
        latest_user_message=message, status_code=status_code,
    )


async def _render_agent_workspace(
    request: Request,
    ctx: UIContext,
    db: AsyncSession,
    settings: Settings,
    *,
    conversation: object,
    history_turns: list,
    latest: AgentTurnView | None = None,
    latest_user_message: str | None = None,
    composer_message: str = "",
    composer_error: str | None = None,
    completed_turn: bool = False,
    page: int = 1,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    from meyar.models.agent_conversation import AgentConversation

    assert isinstance(conversation, AgentConversation)
    await require_active_tenant(db, ctx.tenant_id)
    latest_draft_ids = frozenset(
        result.job_draft.draft_id
        for result in latest.tool_results
        if result.job_draft is not None
    ) if latest is not None else frozenset()
    workspace = await build_agent_workspace_context(
        db, ctx=ctx, settings=settings, conversation=conversation,
        latest_draft_ids=latest_draft_ids, page=page,
    )
    from meyar.services.agent_submission_repo import issue_submission

    submission = await issue_submission(
        db, owner=_conversation_owner(ctx), browser_session_id=ctx.session_id,
        conversation=conversation,
    )
    await db.commit()
    return _render(
        request,
        "agent.html",
        _context(
            ctx,
            conversation_id=conversation.id,
            history_turns=history_turns,
            latest=latest,
            latest_user_message=latest_user_message,
            composer_message=composer_message,
            composer_error=composer_error,
            completed_turn=completed_turn,
            submission_id=submission.id,
            kind_options=CRITERION_KIND_OPTIONS,
            **workspace,
        ),
        status_code=status_code,
    )


@router.get("/agent", response_class=HTMLResponse)
async def agent_workspace(
    request: Request,
    conversation: uuid.UUID | None = Query(default=None),
    page: int = Query(default=1, ge=1, le=1000),
    ctx: UIContext = Depends(require_ui_scopes("candidates:read")),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    selected = await open_conversation(db, ctx, conversation_id=conversation)
    if selected is None:
        return _render_conversation_not_found(request, ctx)
    return await _render_agent_workspace(
        request, ctx, db, settings, conversation=selected,
        history_turns=_agent_turn_log_views(selected), page=page,
    )


@router.post("/agent", response_class=HTMLResponse)
async def agent_turn(
    request: Request,
    message: str = Form(default="", max_length=_FORM_TRANSPORT_LIMIT),
    csrf_token: str = Form(...),
    conversation_id: uuid.UUID | None = Form(default=None),
    submission_id: uuid.UUID = Form(...),
    # Issue #88 (D-092 §6.4 step 1): closed-choice clarification button.
    # Untrusted proposals only; rejected unless they equal the live pointer.
    clarification_id: uuid.UUID | None = Form(default=None),
    clarification_choice: str | None = Form(default=None, max_length=64),
    ctx: UIContext = Depends(require_ui_scopes("candidates:read")),
    db: AsyncSession = Depends(get_db),
    llm: LLMProvider = Depends(get_llm_provider),
    embedding_provider: EmbeddingProvider = Depends(get_embedding_provider),
    embedding_config: EmbeddingSearchConfig = Depends(get_embedding_search_config),
    settings: Settings = Depends(get_settings),
) -> Response:
    verify_csrf(ctx.csrf_token, csrf_token)
    canonical_message = _canonical_hr_text(message)
    if canonical_message is None:
        from meyar.services.agent_conversation_repo import (
            get_owned_conversation,
            resolve_or_create_current_conversation,
        )

        owner = ctx.owner()
        selected = (
            await get_owned_conversation(db, owner=owner, conversation_id=conversation_id)
            if conversation_id is not None else
            await resolve_or_create_current_conversation(
                db, owner=owner, browser_session_id=ctx.session_id
            )
        )
        if selected is None:
            await db.rollback()
            return _render_conversation_not_found(request, ctx)
        await db.commit()
        return await _render_agent_workspace(
            request, ctx, db, settings, conversation=selected,
            history_turns=_agent_turn_log_views(selected),
            composer_message=normalize_message_newlines(message),
            composer_error="Mətn 1–4000 simvol olmalıdır.",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    clarification_button = (
        ClarificationButton(clarification_id=clarification_id, choice=clarification_choice)
        if clarification_id is not None or clarification_choice is not None
        else None
    )
    outcome = await submit_conversation_turn(
        db, ctx,
        request=request, settings=settings, llm=llm,
        embedding_provider=embedding_provider, embedding_config=embedding_config,
        message=canonical_message, conversation_id=conversation_id,
        submission_id=submission_id, clarification_button=clarification_button,
    )
    if outcome.disposition == TurnDisposition.PRINCIPAL_REVOKED:
        raise UIAccessError(status.HTTP_303_SEE_OTHER, clear_cookie=True)
    if outcome.disposition == TurnDisposition.NOT_FOUND:
        return _render_conversation_not_found(request, ctx)
    if outcome.disposition == TurnDisposition.REPLAYED:
        return RedirectResponse(
            f"/ui/agent?conversation={outcome.conversation_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if outcome.disposition == TurnDisposition.CLIENT_DISCONNECTED:
        return HTMLResponse("", status_code=499)
    if outcome.disposition == TurnDisposition.NOT_RUN:
        assert outcome.not_run is not None and outcome.conversation_id is not None
        presentation = NOT_RUN_PRESENTATION[outcome.not_run]
        return await _render_agent_turn_not_run(
            request, ctx, db, settings, owner=ctx.owner(),
            conversation_id=outcome.conversation_id, message=canonical_message,
            outcome=presentation.outcome, copy=presentation.copy,
            status_code=presentation.status_code,
        )

    # D-044: the application operation always persists exactly one new (user,
    # assistant) pair when it completes — split it off history so it renders
    # once, adjacent to its own rich `latest` cards, instead of duplicated as
    # a plain text bubble AND a rich block separated by the composer.
    assert outcome.conversation is not None and outcome.latest is not None
    all_turns = _agent_turn_log_views(outcome.conversation)
    history_turns = all_turns[:-2] if len(all_turns) >= 2 else []
    return await _render_agent_workspace(
        request, ctx, db, settings, conversation=outcome.conversation,
        history_turns=history_turns, latest=outcome.latest,
        latest_user_message=canonical_message, completed_turn=True,
    )


@router.post("/agent/reset", response_class=HTMLResponse)
async def agent_reset(
    request: Request,
    csrf_token: str = Form(...),
    ctx: UIContext = Depends(require_ui_scopes("candidates:read")),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """"Yeni söhbət" (issue #80) — creates a NEW durable AgentConversation
    (title_kind NEW, empty transcript) plus a clean live context for THIS
    BrowserSession (no ResultSet, ordinal, pending draft, or scoring state).
    The previous conversation is never deleted or cleared and stays
    reachable as history; another session's current conversation is never
    switched."""
    verify_csrf(ctx.csrf_token, csrf_token)
    conversation = await start_new_conversation(db, ctx)
    return RedirectResponse(
        f"/ui/agent?conversation={conversation.id}", status_code=status.HTTP_303_SEE_OTHER
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


@router.get("/candidates/{candidate_id}/photo", include_in_schema=False)
async def candidate_photo(
    candidate_id: uuid.UUID,
    ctx: UIContext = Depends(require_ui_scopes("candidates:read")),
    db: AsyncSession = Depends(get_db),
    photo_storage: LocalPhotoStorage = Depends(get_photo_storage),
) -> Response:
    candidate = await get_candidate(db, tenant_id=ctx.tenant_id, candidate_id=candidate_id)
    if candidate is None:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    photo = await current_presentable_photo(
        db, tenant_id=ctx.tenant_id, candidate_id=candidate_id
    )
    content = PLACEHOLDER_JPEG
    if photo is not None and photo.derived_storage_key is not None:
        try:
            stored = await photo_storage.read(
                tenant_id=ctx.tenant_id, storage_key=photo.derived_storage_key
            )
            if hashlib.sha256(stored).hexdigest() == photo.derived_sha256:
                content = stored
            else:
                logger.warning("Derived candidate photo integrity check failed")
        except (OSError, ValueError):
            logger.warning("Derived candidate photo could not be read")
    return Response(
        content=content, media_type="image/jpeg",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; img-src 'self'",
        },
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
    criterion_type: str | None = Form(default=None, max_length=16),
    decision: str | None = Form(default=None, max_length=16),
    option: int | None = Form(default=None, ge=0, le=15),
    ctx: UIContext = Depends(require_ui_scopes("jobs:write", "candidates:read")),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Persist one canonical, server-declared human-review resolution (the
    policy lives in ``meyar.application.drafts.resolve_draft_review``)."""
    from meyar.ui.service import build_agent_job_draft_view
    from meyar.ui.view_models import AgentToolResultView, AgentTurnView

    verify_csrf(ctx.csrf_token, csrf_token)
    try:
        resolution = await resolve_draft_review(
            db, ctx, draft_id=draft_id, span_id=span_id,
            criterion_type=criterion_type, decision=decision, option=option,
        )
    except DraftRejected as exc:
        return _render_agent_confirmation_error(
            request, ctx, message=exc.message, status_code=exc.status_code
        )
    conversation, resolved = resolution.conversation, resolution.draft

    all_turns = _agent_turn_log_views(conversation)
    latest_user_message = all_turns[-2].text if len(all_turns) >= 2 else ""
    latest = AgentTurnView(
        outcome="ANSWERED_FROM_TOOL_RESULT",
        message=None,
        headline=(
            "Dəqiqləşdirmə yadda saxlanıldı. "
            "Tələbləri təsdiqləyib namizədləri sıralaya bilərsiniz."
            if not agent_draft_requires_resolution(resolved)
            else "Dəqiqləşdirmə yadda saxlanıldı. Qalan tələbləri də nəzərdən keçirin."
        ),
        tool_results=[
            AgentToolResultView(
                tool_name="DRAFT_JOB_CRITERIA",
                job_draft=build_agent_job_draft_view(resolved),
            )
        ],
    )
    draft_view = latest.tool_results[0].job_draft
    assert draft_view is not None
    draft_view.can_act = resolution.can_act
    return await _render_agent_workspace(
        request, ctx, db, settings, conversation=conversation,
        history_turns=all_turns[:-2] if len(all_turns) >= 2 else [],
        latest=latest, latest_user_message=latest_user_message,
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

    The accountable confirmation policy lives in
    ``meyar.application.drafts.confirm_job_draft``; this route only parses
    the (backward-compatible) browser rows into an untrusted *proposal* that
    the shared operation checks against the locked server draft. Confirmation
    commits before ranking and retains a durable result link so
    replay/retry cannot create another Job or version.
    """
    verify_csrf(ctx.csrf_token, csrf_token)
    evaluation_as_of_date = resolve_business_date(settings.business_timezone)
    form = await request.form()

    proposal: DraftProposal | None = None
    if "title" in form or any(key.startswith(("must_", "pref_")) for key in form):
        # Backward-compatible handling for already-open review pages and
        # explicit tamper regressions. New protected review pages submit no
        # criterion authority at all.
        def _build(canonical_title: str) -> tuple[JobCreateRequest, list[str]]:
            must_have_rows = [_job_form_row(form, "must", i) for i in range(CRITERION_ROW_COUNT)]
            preferred_rows = [_job_form_row(form, "pref", i) for i in range(CRITERION_ROW_COUNT)]
            job_request = build_job_create_request(
                title=canonical_title,
                must_have_rows=must_have_rows,
                preferred_rows=preferred_rows,
            )
            span_ids = [
                str(form.get(f"{prefix}_span_id_{index}", ""))
                for prefix, rows in (("must", must_have_rows), ("pref", preferred_rows))
                for index, row in enumerate(rows)
                if row.requirement.strip()
            ]
            return job_request, span_ids

        proposal = DraftProposal(title=str(form.get("title", "")), build=_build)

    try:
        confirmed = await confirm_job_draft(db, ctx, draft_id=draft_id, proposal=proposal)
    except DraftRejected as exc:
        return _render_agent_confirmation_error(
            request, ctx, message=exc.message, status_code=exc.status_code
        )
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
    app.mount("/ui/static", StaticFiles(directory=str(_static_dir)), name="ui-static")
    app.include_router(router)
    app.include_router(human_router)

    app.add_exception_handler(ApplicationError, application_error_handler)  # type: ignore[arg-type]

    @app.exception_handler(TenantInactiveError)
    async def _tenant_access_error(request: Request, exc: TenantInactiveError) -> Response:
        if is_human_json_path(request.url.path):
            return unauthenticated_response()
        if not request.url.path.startswith("/ui"):
            from fastapi.responses import JSONResponse

            return JSONResponse(
                {"detail": "Invalid or missing API key."}, status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        if request.url.path.startswith("/ui/login"):
            response = _render(
                request, "login.html", _context(error=_GENERIC_LOGIN_ERROR),
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
            _clear_session_cookie(response, get_settings())
            return response
        return await _ui_access_error(
            request, UIAccessError(status.HTTP_303_SEE_OTHER, clear_cookie=True)
        )

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
        if is_human_json_path(request.url.path):
            return validation_error_response()
        if request.url.path == "/ui" or request.url.path.startswith("/ui/"):
            resolved_ctx: UIContext | None = None
            provider = app.dependency_overrides.get(get_db, get_db)
            session_source = provider()
            try:
                validation_db = await anext(session_source)
                try:
                    resolved_ctx = await resolve_ui_context(request, validation_db)
                except UIAccessError:
                    pass
            finally:
                await session_source.aclose()
            return _render(
                request,
                "error.html",
                _context(
                    resolved_ctx, title="Yanlış məlumat", message="Forma məlumatlarını yoxlayın."
                ),
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        # Pydantic's default errors include raw input and exception context.
        return JSONResponse({"detail": "Invalid request."}, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if is_human_json_path(request.url.path):
            return http_error_response(exc)
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
        log_failure(logger, component="http", code="UNEXPECTED_ERROR", exc=exc)
        if is_human_json_path(request.url.path):
            return internal_error_response()
        if request.url.path == "/ui" or request.url.path.startswith("/ui/"):
            return _render(
                request,
                "error.html",
                _context(
                    title="Xidmət xətası",
                    message="Gözlənilməz xəta baş verdi. Daha sonra yenidən cəhd edin.",
                ),
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return JSONResponse({"detail": "Internal server error."}, status_code=500)

    app.add_middleware(DiagnosticErrorMiddleware, handler=_unexpected_error)
    # Keep UI headers around both successful responses and safe failures.
    app.add_middleware(UISecurityHeadersMiddleware)
