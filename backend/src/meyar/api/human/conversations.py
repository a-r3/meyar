"""Human JSON: session, conversations, turns and JD draft actions (issue #45).

Thin adapter over ``meyar.application.conversations`` / ``.drafts`` — the same
operations the Jinja UI calls. Authority is the BrowserSession cookie
principal; mutations additionally require the ``X-CSRF-Token`` header."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.agent.service import ClarificationButton
from meyar.api.human.auth import (
    get_human_context,
    require_human_mutation,
    require_human_scopes,
)
from meyar.api.responses import error_responses
from meyar.application.conversations import (
    NOT_RUN_PRESENTATION,
    NotRunReason,
    TurnDisposition,
    canonical_turn_message,
    conversation_state,
    issue_turn_submission,
    list_conversations,
    open_conversation,
    start_new_conversation,
    submit_conversation_turn,
    turn_log_entries,
)
from meyar.application.drafts import (
    DraftProposal,
    DraftRejected,
    DraftRejectReason,
    confirm_job_draft,
    resolve_draft_review,
)
from meyar.application.errors import (
    ApplicationError,
    ErrorCode,
    not_found,
    validation_failed,
)
from meyar.config import Settings, get_settings
from meyar.db import get_db
from meyar.embedding.dependency import get_embedding_provider, get_embedding_search_config
from meyar.embedding.provider import EmbeddingProvider
from meyar.llm.dependency import get_llm_provider
from meyar.llm.provider import LLMProvider
from meyar.models.agent_conversation import AgentConversationTitleKind
from meyar.schemas.api_human import (
    ActiveClarificationOut,
    ChoiceOut,
    ConfirmDraftRequest,
    ConfirmDraftResponse,
    ConversationDetailOut,
    ConversationListOut,
    ConversationSummaryOut,
    ResolveDraftRequest,
    ResolveDraftResponse,
    SearchOutcomeOut,
    SessionOut,
    SubmissionOut,
    TurnRequest,
    TurnResponse,
    TurnResultOut,
    TurnStatus,
)
from meyar.schemas.job import JobCreateRequest
from meyar.schemas.read_models import AgentToolResultView, AgentTurnView
from meyar.search.planner_schemas import PlannerOutcome
from meyar.search.schemas import EmbeddingSearchConfig
from meyar.ui.auth import UIContext
from meyar.ui.service import build_agent_job_draft_view

router = APIRouter(prefix="/ui/api/v1", tags=["human-conversations"])

_ERROR_RESPONSES = error_responses(401, 403, 404, 422)
_HumanRead = Depends(require_human_scopes("candidates:read"))


@router.get("/session", response_model=SessionOut, responses=_ERROR_RESPONSES)
async def get_session(ctx: UIContext = Depends(get_human_context)) -> SessionOut:
    """The server-derived principal and the CSRF token to use on mutations."""
    return SessionOut(
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        membership_id=ctx.membership_id,
        role=ctx.role,
        scopes=sorted(ctx.scopes),
        csrf_token=ctx.csrf_token,
    )


async def _detail(
    db: AsyncSession, ctx: UIContext, conversation
) -> ConversationDetailOut:  # noqa: ANN001
    state = await conversation_state(db, ctx, conversation)
    clarification = state.active_clarification
    return ConversationDetailOut(
        id=conversation.id,
        title_kind=conversation.title_kind,
        updated_at=conversation.updated_at,
        turns=turn_log_entries(conversation),
        active_clarification=(
            ActiveClarificationOut(
                id=clarification.id,
                choices=[ChoiceOut(value=c.value, label=c.label) for c in clarification.choices],
            )
            if clarification is not None
            else None
        ),
        active_draft=(
            build_agent_job_draft_view(state.active_draft).model_copy(update={"can_act": True})
            if state.active_draft is not None
            else None
        ),
    )


@router.get("/conversations", response_model=ConversationListOut, responses=_ERROR_RESPONSES)
async def list_my_conversations(
    page: Annotated[int, Query(ge=1, le=1000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=50)] = 20,
    ctx: UIContext = _HumanRead,
    db: AsyncSession = Depends(get_db),
) -> ConversationListOut:
    """Bounded, newest-first history of the caller's OWN conversations."""
    result = await list_conversations(db, ctx, page=page, page_size=page_size)
    return ConversationListOut(
        items=[
            ConversationSummaryOut(
                id=i.id,
                title_kind=AgentConversationTitleKind(i.title_kind),
                updated_at=i.updated_at,
            )
            for i in result.items
        ],
        page=result.page,
        page_size=result.page_size,
        has_next=result.has_next,
    )


@router.post(
    "/conversations",
    status_code=status.HTTP_201_CREATED,
    response_model=ConversationDetailOut,
    responses=_ERROR_RESPONSES,
)
async def create_conversation_endpoint(
    ctx: UIContext = Depends(require_human_mutation("candidates:read")),
    db: AsyncSession = Depends(get_db),
) -> ConversationDetailOut:
    """Start a NEW conversation (the previous one stays in history)."""
    conversation = await start_new_conversation(db, ctx)
    return await _detail(db, ctx, conversation)


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetailOut,
    responses=_ERROR_RESPONSES,
)
async def get_conversation(
    conversation_id: uuid.UUID,
    ctx: UIContext = _HumanRead,
    db: AsyncSession = Depends(get_db),
) -> ConversationDetailOut:
    conversation = await open_conversation(db, ctx, conversation_id=conversation_id)
    if conversation is None:
        raise not_found("Conversation not found.")
    return await _detail(db, ctx, conversation)


@router.post(
    "/conversations/{conversation_id}/submissions",
    status_code=status.HTTP_201_CREATED,
    response_model=SubmissionOut,
    responses=_ERROR_RESPONSES,
)
async def create_submission(
    conversation_id: uuid.UUID,
    ctx: UIContext = Depends(require_human_mutation("candidates:read")),
    db: AsyncSession = Depends(get_db),
) -> SubmissionOut:
    """Issue the single-use identity that makes one turn submission replay-safe."""
    submission = await issue_turn_submission(db, ctx, conversation_id=conversation_id)
    if submission is None:
        raise not_found("Conversation not found.")
    return SubmissionOut(
        submission_id=submission.id,
        conversation_id=conversation_id,
        expires_at=submission.expires_at,
    )


def _turn_result(view: AgentToolResultView) -> TurnResultOut:
    from meyar.agent.schemas import AgentActionType

    tool = AgentActionType(view.tool_name)
    search = None
    if view.search_outcome is not None:
        outcome = view.search_outcome
        search = SearchOutcomeOut(
            outcome=PlannerOutcome(outcome.outcome),
            executable=outcome.executable,
            message=outcome.message,
            result_count=outcome.result_count,
            infrastructure_error=outcome.infrastructure_error,
        )
    return TurnResultOut(
        tool=tool,
        search=search,
        candidates=view.search_results,
        profile=view.profile,
        evidence=view.evidence,
        job_draft=view.job_draft,
        refine=view.refine_summary,
        candidate_reference_not_found=view.not_found_ref,
    )


def _turn_response(latest: AgentTurnView, conversation_id: uuid.UUID) -> TurnResponse:
    from meyar.agent.schemas import AgentTurnOutcome

    return TurnResponse(
        status=TurnStatus.COMPLETED,
        conversation_id=conversation_id,
        outcome=AgentTurnOutcome(latest.outcome),
        message=latest.headline,
        results=[_turn_result(item) for item in latest.tool_results],
    )


def _not_run_error(reason: NotRunReason) -> ApplicationError:
    presentation = NOT_RUN_PRESENTATION[reason]
    if reason == NotRunReason.BUSY:
        code = ErrorCode.RETRYABLE_BUSY
    elif reason == NotRunReason.PROVIDER_FAILURE:
        code = ErrorCode.SERVICE_UNAVAILABLE
    else:
        code = ErrorCode.CONFLICT
    return ApplicationError(
        code,
        status_code=presentation.status_code,
        reason=reason.value,
        message=presentation.copy,
        retryable=presentation.retryable,
    )


@router.post(
    "/conversations/{conversation_id}/turns",
    response_model=TurnResponse,
    # A planner outcome is NOT an error: it is a 200 answer. 409/503 mean the
    # turn did not run and is retryable per error.retryable.
    responses=error_responses(401, 403, 404, 409, 422, 503),
)
async def post_turn(
    conversation_id: uuid.UUID,
    body: TurnRequest,
    request: Request,
    ctx: UIContext = Depends(require_human_mutation("candidates:read")),
    db: AsyncSession = Depends(get_db),
    llm: LLMProvider = Depends(get_llm_provider),
    embedding_provider: EmbeddingProvider = Depends(get_embedding_provider),
    embedding_config: EmbeddingSearchConfig = Depends(get_embedding_search_config),
    settings: Settings = Depends(get_settings),
):
    message = canonical_turn_message(body.message)
    if message is None:
        raise validation_failed("MESSAGE_LENGTH", "The message must be 1-4000 characters.")
    if (body.clarification_id is None) != (body.clarification_choice is None):
        raise validation_failed("CLARIFICATION_INCOMPLETE")
    button = (
        ClarificationButton(
            clarification_id=body.clarification_id, choice=body.clarification_choice
        )
        if body.clarification_id is not None
        else None
    )
    outcome = await submit_conversation_turn(
        db, ctx,
        request=request, settings=settings, llm=llm,
        embedding_provider=embedding_provider, embedding_config=embedding_config,
        message=message, conversation_id=conversation_id,
        submission_id=body.submission_id, clarification_button=button,
    )
    disposition = outcome.disposition
    if disposition == TurnDisposition.PRINCIPAL_REVOKED:
        raise ApplicationError(
            ErrorCode.AUTHENTICATION_REQUIRED, status_code=401, clear_session_cookie=True
        )
    if disposition == TurnDisposition.NOT_FOUND:
        raise not_found("Conversation not found.")
    if disposition == TurnDisposition.CLIENT_DISCONNECTED:
        return Response(status_code=499)
    if disposition == TurnDisposition.NOT_RUN:
        assert outcome.not_run is not None
        raise _not_run_error(outcome.not_run)
    if disposition == TurnDisposition.REPLAYED:
        return TurnResponse(status=TurnStatus.REPLAYED, conversation_id=conversation_id)
    assert outcome.latest is not None
    return _turn_response(outcome.latest, conversation_id)


_DRAFT_STATUS = {
    DraftRejectReason.DRAFT_NOT_ACTIVE: (ErrorCode.NOT_FOUND, 404),
    DraftRejectReason.DUPLICATE_ACTIVE_JOB: (ErrorCode.CONFLICT, 409),
    DraftRejectReason.UNAVAILABLE: (ErrorCode.SERVICE_UNAVAILABLE, 503),
}


def _draft_error(exc: DraftRejected) -> ApplicationError:
    code, http_status = _DRAFT_STATUS.get(exc.reason, (ErrorCode.VALIDATION_FAILED, 422))
    return ApplicationError(
        code,
        status_code=http_status,
        reason=exc.reason.value,
        message=exc.message,
        retryable=exc.reason == DraftRejectReason.UNAVAILABLE,
    )


@router.post(
    "/drafts/{draft_id}/resolve",
    response_model=ResolveDraftResponse,
    responses=_ERROR_RESPONSES,
)
async def resolve_draft(
    draft_id: uuid.UUID,
    body: ResolveDraftRequest,
    ctx: UIContext = Depends(require_human_mutation("jobs:write", "candidates:read")),
    db: AsyncSession = Depends(get_db),
) -> ResolveDraftResponse:
    """Record one explicit human review decision on the live pending draft."""
    try:
        resolution = await resolve_draft_review(
            db, ctx, draft_id=draft_id, span_id=body.span_id,
            criterion_type=body.criterion_type, decision=body.decision, option=body.option,
        )
    except DraftRejected as exc:
        raise _draft_error(exc) from None
    return ResolveDraftResponse(
        draft=build_agent_job_draft_view(resolution.draft).model_copy(
            update={"can_act": resolution.can_act}
        )
    )


@router.post(
    "/drafts/{draft_id}/confirm",
    response_model=ConfirmDraftResponse,
    responses=error_responses(401, 403, 404, 409, 422, 503),
)
async def confirm_draft(
    draft_id: uuid.UUID,
    body: ConfirmDraftRequest,
    ctx: UIContext = Depends(
        require_human_mutation("jobs:write", "jobs:read", "candidates:read", "evaluations:write")
    ),
    db: AsyncSession = Depends(get_db),
) -> ConfirmDraftResponse:
    """Accountable human confirmation of the live server-held draft. Creates the
    Job + immutable criteria version and returns; it never ranks inline."""
    proposal: DraftProposal | None = None
    if body.reviewed is not None:
        reviewed = body.reviewed

        def _build(canonical_title: str) -> tuple[JobCreateRequest, list[str]]:
            return (
                JobCreateRequest(
                    title=canonical_title, criteria=[c.criterion for c in reviewed.criteria]
                ),
                [c.span_id for c in reviewed.criteria],
            )

        proposal = DraftProposal(title=reviewed.title, build=_build)
    try:
        confirmed = await confirm_job_draft(db, ctx, draft_id=draft_id, proposal=proposal)
    except DraftRejected as exc:
        raise _draft_error(exc) from None
    return ConfirmDraftResponse(
        job_id=confirmed.job_id,
        criteria_version_id=confirmed.criteria_version_id,
        criteria_version_number=confirmed.criteria_version_number,
        replayed=confirmed.replayed,
        result_limit=confirmed.result_limit or None,
        unsupported_requirements=confirmed.unsupported_requirements,
        needs_review_requirements=confirmed.needs_review_requirements,
    )
