"""Accountable-human JD draft use cases shared by the Jinja UI and the human
JSON API (issue #45, D-116).

Extracted from the former ``/ui/agent/drafts/{id}/resolve|confirm`` route
bodies without changing policy:

- the ONLY confirmable draft is the live server-held pending draft of the
  caller's own conversation + BrowserSession (``resolve_pending_draft_authority``);
- criteria are taken from that locked server draft; any caller-supplied
  criteria are *proposals* that must equal the server rows exactly;
- replay/idempotency authority is the independent ``AgentDraftConfirmation``
  row, never transcript state;
- confirmation commits before ranking and never depends on ranking succeeding;
- the ``HumanPrincipal`` is the accountable actor — an API key can never reach
  this module."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.agent.schemas import AgentJobDraftToolResult, ConfirmedAgentJobDraft
from meyar.agent.semantic_provenance import (
    SemanticProvenanceError,
    build_agent_semantic_provenance,
)
from meyar.agent.service import (
    exclude_blocking_review_requirement,
    resolve_job_draft_review_modality,
    resolve_job_draft_semantic_conflict,
)
from meyar.application.principal import HumanPrincipal
from meyar.models.agent_conversation import AgentConversation
from meyar.schemas.criteria import CriterionType
from meyar.schemas.job import JobCreateRequest
from meyar.services.agent_conversation_repo import (
    mark_pending_job_draft_confirmed,
    replace_pending_job_draft,
    resolve_pending_draft_authority,
)
from meyar.services.agent_draft_confirmation_repo import (
    create_draft_confirmation,
    get_draft_confirmation,
)
from meyar.services.agent_task_repo import complete_confirmed_draft_task
from meyar.services.audit_repo import record_event
from meyar.services.job_criteria_repo import create_criteria_version
from meyar.services.job_repo import create_job, find_active_duplicate_job
from meyar.ui.service import (
    JOB_DUPLICATE_MESSAGE,
    UIServiceInputError,
    agent_draft_requires_resolution,
    authorize_agent_draft_confirmation,
    compute_job_duplicate_signature,
)


class DraftRejectReason(StrEnum):
    """Closed machine reasons for a refused draft action."""

    INVALID_RESOLUTION = "INVALID_RESOLUTION"
    DRAFT_NOT_ACTIVE = "DRAFT_NOT_ACTIVE"
    REQUIRES_RESOLUTION = "REQUIRES_RESOLUTION"
    CRITERIA_MISMATCH = "CRITERIA_MISMATCH"
    PROVENANCE_UNVERIFIED = "PROVENANCE_UNVERIFIED"
    DUPLICATE_ACTIVE_JOB = "DUPLICATE_ACTIVE_JOB"
    UNAVAILABLE = "UNAVAILABLE"


class DraftRejected(Exception):
    """A refused draft action. ``message`` is fixed, privacy-safe HR copy."""

    def __init__(self, reason: DraftRejectReason, message: str, status_code: int) -> None:
        self.reason = reason
        self.message = message
        self.status_code = status_code
        super().__init__(reason.value)


_UNAVAILABLE_COPY = "Qaralama təhlükəsiz şəkildə təsdiqlənə bilmədi. Elanı yenidən analiz edin."


@dataclass(frozen=True)
class DraftReviewResolution:
    conversation: AgentConversation
    draft: AgentJobDraftToolResult
    can_act: bool


async def resolve_draft_review(
    db: AsyncSession,
    principal: HumanPrincipal,
    *,
    draft_id: uuid.UUID,
    span_id: str,
    criterion_type: str | None,
    decision: str | None,
    option: int | None,
) -> DraftReviewResolution:
    """Persist one canonical, server-declared human-review resolution.

    Exactly one of: a modality resolution (``criterion_type``) for a
    server-validated canonical shape, a semantic-conflict ``option``, or
    ``decision == "exclude"`` — HR's explicit acknowledgement that an
    unresolved MUST_HAVE source requirement is not part of automatic ranking.
    None accepts free text and none can turn arbitrary source text into a
    criterion."""
    try:
        exclude = decision == "exclude"
        chosen_paths = [exclude, criterion_type is not None, option is not None]
        if (decision is not None and not exclude) or chosen_paths.count(True) != 1:
            raise UIServiceInputError("Dəqiqləşdirmə seçimi etibarsızdır.")
        resolved_type = CriterionType(criterion_type) if criterion_type is not None else None
        # issue #80: live pending authority only — owner + this BrowserSession's
        # context pointer + transcript payload, all required.
        authority = await resolve_pending_draft_authority(
            db,
            owner=principal.owner(),
            browser_session_id=principal.session_id,
            draft_id=draft_id,
        )
        if authority is None:
            raise DraftRejected(
                DraftRejectReason.DRAFT_NOT_ACTIVE, "Qaralama bu sessiyada tapılmadı.", 422
            )
        conversation = authority.conversation
        if option is not None:
            resolved = resolve_job_draft_semantic_conflict(
                authority.draft, span_id=span_id, option_index=option
            )
            resolution_code = "CONFLICT_RESOLVED"
        elif resolved_type is None:
            resolved = exclude_blocking_review_requirement(authority.draft, span_id=span_id)
            resolution_code = "EXCLUDED_BY_REVIEWER"
        else:
            resolved = resolve_job_draft_review_modality(
                authority.draft, span_id=span_id, criterion_type=resolved_type
            )
            resolution_code = resolved_type.value
        await replace_pending_job_draft(db, authority, draft=resolved)
        await record_event(
            db,
            tenant_id=principal.tenant_id,
            event_type="agent.draft.review_resolved",
            metadata={
                "draft_id": str(resolved.draft_id),
                "span_id": span_id,
                "criterion_type": resolution_code,
                "semantic_policy_version": resolved.semantic_policy_version,
            },
            actor_type=principal.actor_type,
            actor_id=principal.actor_id,
        )
        await db.commit()
    except DraftRejected:
        await db.rollback()
        raise
    except (ValueError, UIServiceInputError) as exc:
        await db.rollback()
        raise DraftRejected(DraftRejectReason.INVALID_RESOLUTION, str(exc), 422) from None
    return DraftReviewResolution(
        conversation=conversation,
        draft=resolved,
        can_act=authority.session_context.active_pending_draft_id == resolved.draft_id,
    )


@dataclass(frozen=True)
class DraftProposal:
    """What a caller *says* it reviewed. Never authority: ``build`` receives the
    server's canonical title and returns the proposed request plus one source
    span id per proposed criterion; ``authorize_agent_draft_confirmation`` then
    requires it to equal the locked server rows exactly."""

    title: str
    build: Callable[[str], tuple[JobCreateRequest, list[str]]]


@dataclass(frozen=True)
class ConfirmedDraft:
    job_id: uuid.UUID
    criteria_version_id: uuid.UUID
    criteria_version_number: int | None
    result_limit: int
    unsupported_requirements: list[str]
    needs_review_requirements: list[str]
    replayed: bool


async def confirm_job_draft(
    db: AsyncSession,
    principal: HumanPrincipal,
    *,
    draft_id: uuid.UUID,
    proposal: DraftProposal | None = None,
) -> ConfirmedDraft:
    """Confirm one session-held canonical draft (accountable human action).

    Commits the Job + immutable criteria version + ``AgentDraftConfirmation``
    and returns; ranking is a separate, later step so confirmation never
    depends on it. A repeat of an already-confirmed draft in the same
    BrowserSession returns the durable result (``replayed=True``) without
    creating anything."""
    try:
        authority = await resolve_pending_draft_authority(
            db,
            owner=principal.owner(),
            browser_session_id=principal.session_id,
            draft_id=draft_id,
        )
        if authority is None:
            # Checked AFTER the lock attempt so a concurrent duplicate
            # confirmation observes the winner's committed row.
            durable = await get_draft_confirmation(
                db,
                tenant_id=principal.tenant_id,
                browser_session_id=principal.session_id,
                draft_id=draft_id,
            )
            if durable is not None:
                await db.commit()
                return ConfirmedDraft(
                    job_id=durable.job_id,
                    criteria_version_id=durable.criteria_version_id,
                    criteria_version_number=None,
                    result_limit=0,
                    unsupported_requirements=[],
                    needs_review_requirements=[],
                    replayed=True,
                )
            raise DraftRejected(
                DraftRejectReason.DRAFT_NOT_ACTIVE,
                "Qaralama təsdiqi tapılmadı və ya bu sessiyaya aid deyil; "
                "elanı yenidən analiz edin.",
                422,
            )
        pending = authority.draft
        if agent_draft_requires_resolution(pending):
            raise DraftRejected(
                DraftRejectReason.REQUIRES_RESOLUTION,
                "İnsan baxışı tələb edən sahələri dəqiqləşdirmədən sıralamanı təsdiqləmək olmaz.",
                422,
            )

        canonical_title = pending.title or "Vakansiya qaralaması"
        if proposal is not None:
            if proposal.title.strip() != canonical_title.strip():
                raise UIServiceInputError(
                    "Qaralama başlığı yadda saxlanmış təsdiqli forma ilə uyğun gəlmir."
                )
            try:
                job_request, submitted_span_ids = proposal.build(canonical_title)
            except ValidationError:
                raise UIServiceInputError(
                    "Qaralama meyarlarının təsdiqi etibarsızdır; elanı yenidən analiz edin."
                ) from None
        else:
            job_request = JobCreateRequest(
                title=canonical_title,
                criteria=[*pending.must_have, *pending.preferred],
            )
            # Primary (first) supporting span per criterion (issue #84).
            span_by_criterion: dict[str, str] = {}
            for item in pending.requirements:
                if item.criterion_id is not None and item.state.value == "SCORABLE":
                    span_by_criterion.setdefault(item.criterion_id, item.span_id)
            submitted_span_ids = []
            for criterion in job_request.criteria:
                span_id = span_by_criterion.get(criterion.id)
                if span_id is None:
                    raise UIServiceInputError(
                        "Qaralama meyarlarının təsdiqi etibarsızdır; elanı yenidən analiz edin."
                    )
                submitted_span_ids.append(span_id)
        authorize_agent_draft_confirmation(
            draft=pending,
            request=job_request,
            submitted_span_ids=submitted_span_ids,
        )
        # issue #84: durable semantic provenance, built fail-closed from the
        # locked server draft (never caller data) BEFORE anything is created.
        try:
            semantic_provenance = build_agent_semantic_provenance(pending, job_request.criteria)
        except SemanticProvenanceError as exc:
            raise DraftRejected(
                DraftRejectReason.PROVENANCE_UNVERIFIED,
                "Qaralama meyarlarının mənbə izi təsdiqlənmədi; elanı yenidən analiz edin.",
                422,
            ) from exc

        duplicate_signature = compute_job_duplicate_signature(
            job_request.title, job_request.criteria
        )
        if (
            await find_active_duplicate_job(
                db, tenant_id=principal.tenant_id, duplicate_signature=duplicate_signature
            )
            is not None
        ):
            await db.rollback()
            raise DraftRejected(
                DraftRejectReason.DUPLICATE_ACTIVE_JOB, JOB_DUPLICATE_MESSAGE, 409
            )

        job = await create_job(
            db,
            tenant_id=principal.tenant_id,
            title=job_request.title,
            duplicate_signature=duplicate_signature,
        )
        version = await create_criteria_version(
            db,
            tenant_id=principal.tenant_id,
            job_id=job.id,
            criteria=[criterion.model_dump(mode="json") for criterion in job_request.criteria],
            created_by_api_key_id=None,
            unsupported_requirements=[item.requirement for item in pending.unsupported],
            needs_review_requirements=[item.requirement for item in pending.needs_review],
            result_limit=pending.result_limit,
            eligible_only=True,
            agent_semantic_provenance=semantic_provenance.model_dump(mode="json"),
        )
        await record_event(
            db,
            tenant_id=principal.tenant_id,
            event_type="job.created",
            metadata={"job_id": str(job.id), "criteria_version": version.version_number},
            actor_type=principal.actor_type,
            actor_id=principal.actor_id,
        )
        # Privacy-safe correlation: proposal/draft → accountable human
        # confirmation → Job → CriteriaVersion. Ids and a count only.
        await record_event(
            db,
            tenant_id=principal.tenant_id,
            event_type="agent.draft.confirmed",
            metadata={
                "draft_id": str(draft_id),
                "job_id": str(job.id),
                "criteria_version_id": str(version.id),
                "criteria_version": version.version_number,
            },
            actor_type=principal.actor_type,
            actor_id=principal.actor_id,
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
            tenant_id=principal.tenant_id,
            browser_session_id=principal.session_id,
            draft_id=draft_id,
            job_id=job.id,
            criteria_version_id=version.id,
        )
        await mark_pending_job_draft_confirmed(db, authority, confirmation=confirmation)
        # Issue #88 T10: complete the lane-B task; lane A is never touched.
        await complete_confirmed_draft_task(
            db, session_context=authority.session_context, draft_id=draft_id
        )
        await db.commit()
    except DraftRejected:
        await db.rollback()
        raise
    except UIServiceInputError as exc:
        await db.rollback()
        reason = (
            DraftRejectReason.REQUIRES_RESOLUTION
            if "dəqiqləşdirmədən" in str(exc)
            else DraftRejectReason.CRITERIA_MISMATCH
        )
        raise DraftRejected(reason, str(exc), 422) from None
    except IntegrityError:
        await db.rollback()
        raise DraftRejected(
            DraftRejectReason.DUPLICATE_ACTIVE_JOB, JOB_DUPLICATE_MESSAGE, 409
        ) from None
    except (ValueError, SQLAlchemyError):
        await db.rollback()
        raise DraftRejected(DraftRejectReason.UNAVAILABLE, _UNAVAILABLE_COPY, 503) from None

    return ConfirmedDraft(
        job_id=job.id,
        criteria_version_id=version.id,
        criteria_version_number=version.version_number,
        result_limit=confirmation.result_limit,
        unsupported_requirements=confirmation.unsupported_requirements,
        needs_review_requirements=confirmation.needs_review_requirements,
        replayed=False,
    )
