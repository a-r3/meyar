"""Bounded read-only agent orchestration (issue #31 D-035; Agent Core v2,
issue #88 slices A–C, D-092 / D-095).

Human session -> deterministic pre-router -> (server-built plan | ONE local
Ollama ``agent-plan-v1`` proposal) -> pure Layer-1 validation with source
grounding -> bounded ordered capability execution (no post-tool model
re-decision) -> deterministic, server-authority rendering -> Phase B. The
LLM proposes; it never becomes scoring, authorization, evidence or
persistence authority (docs/DECISIONS.md D-030/D-031).

The model never authors executable text or numbers: it quotes the user's
own current message and the server resolves offsets and parses counts and
ordinals itself (D-092 §10.2). Every tool call stays tenant-scoped; a
candidate ordinal is always resolved against this conversation's OWN
BrowserSession-bound ``active_result_set_id`` (issues #49/#80; see
``meyar.services.agent_result_set_repo.resolve_active_candidate_ref``)."""

import hashlib
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.agent.canonical_requirements import (
    JD_SEMANTIC_POLICY_VERSION,
    CanonicalRequirement,
    canonicalize_requirements,
    is_canonical_subject,
    semantic_identity,
    semantic_parameters,
    subject_grounded_in_span,
)
from meyar.agent.capabilities import (
    AGENT_PLAN_SCHEMA_VERSION,
    CAPABILITY_PLAN_SCHEMA_VERSION,
    CAPABILITY_REGISTRY,
    MAX_PLAN_STEPS,
    CapabilityName,
    ExecutablePlan,
    ExecutionContext,
    PlanExecution,
    PlanOrigin,
    PlanRejection,
    PlanRejectionCode,
    PlanStatus,
    ResultSetContext,
    ResultSetStatus,
    ValidationContext,
    capability_audit_metadata,
    execute_plan,
    offered_capabilities,
    validate_plan,
)
from meyar.agent.capabilities.contracts import (
    AgentPlanContext,
    AgentPlanProposal,
    ContextTurn,
    GroundedField,
    ModelClarificationCode,
    OfferedCapability,
    PlanKind,
)
from meyar.agent.capabilities.provenance import plan_validated_metadata
from meyar.agent.capabilities.server_plans import result_limit_plan, search_plan, vacancy_plan
from meyar.agent.capabilities.validator import (
    pre_existing_result_set_requirements,
    validate_model_reply,
)
from meyar.agent.clarification_schemas import (
    ALLOWED_ANSWERS,
    MAX_CLARIFICATION_ATTEMPTS,
    ClarificationAnswer,
    ClarificationProposalValue,
    ClarificationStatus,
    ClarificationType,
    ExpiryReason,
    RejectionReason,
    ResolutionSource,
    SupersededReason,
    TaskStatus,
    TaskType,
)
from meyar.agent.dialogue import (
    CLARIFICATION_ATTEMPTS_EXHAUSTED_COPY,
    CLARIFICATION_STALE_COPY,
    ClarificationClassifierError,
    ClarificationRejectedError,
    ClarificationTransition,
    DialogueCommit,
    LaneBChange,
    NewClarification,
    SourceSlotOutcome,
    bound_source_text,
    clarification_payload,
    classify_vacancy_source_message,
    eligible_search_or_vacancy_source,
    is_clear_new_task,
    liveness_failure,
    match_answer_label,
    retry_clarification,
    search_or_vacancy_clarification,
    vacancy_source_clarification,
)
from meyar.agent.intent_routing import (
    AMBIGUOUS_SEARCH_OR_JOB_COPY,
    ENTRY_ROUTING_POLICY_VERSION,
    INPUT_STRUCTURE_CLARIFICATION_COPY,
    JOB_SOURCE_REQUIRED_COPY,
    AgentEntryRoute,
    AgentRoutedAction,
    route_agent_entry,
)
from meyar.agent.prompts import AGENT_PROMPT_VERSION, JD_CRITERIA_DRAFT_PROMPT_VERSION
from meyar.agent.schemas import (
    AGENT_POLICY_VERSION,
    MAX_AGENT_SEARCH_QUERY_LENGTH,
    MAX_CANDIDATE_REF,
    MAX_JD_REQUIREMENT_SPANS,
    AgentActionType,
    AgentEvidenceToolResult,
    AgentJobDraftToolResult,
    AgentProfileToolResult,
    AgentRefineToolResult,
    AgentResponseCode,
    AgentSearchToolResult,
    AgentToolResult,
    AgentTurnOutcome,
    AgentTurnResult,
    EvidenceMatchItem,
    GroundedCaveat,
    GroundedFact,
    GroundedSelection,
    JDCriteriaDraft,
    JDDraftCriterionKind,
    NeedsReviewJDCriterionItem,
    RequirementSpan,
    RequirementSpanResult,
    RequirementSpanState,
    SemanticConflictResolution,
    SemanticCriterionAmendment,
    SemanticCriterionAmendmentField,
    SemanticModelProvenance,
    SemanticParameters,
    SemanticRequirementState,
    SemanticReviewDecision,
    SemanticReviewDecisionKind,
    SupportedInputLanguage,
    UnsupportedJDCriterionItem,
    encode_amendment_value,
    pending_draft_payload,
)
from meyar.agent.semantic_requirements import (
    SemanticAnalysis,
    analyze_hr_text,
    explicit_vacancy_title,
    is_non_professional_requirement,
)
from meyar.agent.turn_boundary import ConversationSnapshot, TurnSessionState
from meyar.core.domain_terms import canonicalize_domain
from meyar.core.result_count import is_result_count_only
from meyar.core.text import (
    combine_degree_and_field,
    fold_az_ascii,
    normalize_azerbaijani_case,
    slugify_criterion_label,
)
from meyar.embedding.provider import EmbeddingProvider
from meyar.evaluation.normalization import normalize_skill_name
from meyar.llm.provider import (
    LLMProvider,
    LLMProviderError,
    LLMResultProvenance,
    ModelSchemaInvalidError,
    ModelTimeoutError,
    ModelUnavailableError,
)
from meyar.models.agent_conversation import AgentConversation, AgentConversationSessionContext
from meyar.schemas.candidate_profile import CandidateProfileExtraction
from meyar.schemas.criteria import (
    CriterionIn,
    CriterionKind,
    CriterionType,
    find_prohibited_term,
)
from meyar.search.planner_service import plan_and_search_candidates, plan_candidate_search
from meyar.search.schemas import (
    CandidateSearchRequest,
    CandidateSearchResponse,
    CandidateSearchResult,
    EmbeddingSearchConfig,
    PreferredFilters,
    SearchMode,
)
from meyar.services.agent_conversation_repo import (
    ASSISTANT_TEXT_AUTHORITY_SERVER,
    ASSISTANT_TEXT_AUTHORITY_VERSION,
    apply_title_kind_transition,
    get_active_pending_job_draft,
    save_conversation_turns,
)
from meyar.services.agent_result_set_repo import (
    RefinementResult,
    ResultSetInspection,
    ResultSetResolutionFailure,
    active_result_set_size,
    create_result_set_from_refinement,
    create_result_set_from_search,
    inspect_active_result_set,
    record_reference_rejection,
    record_refinement_rejection,
    resolve_active_candidate_ref,
    validate_active_result_set_for_refinement,
)
from meyar.services.agent_task_repo import (
    LockedDialogue,
    apply_dialogue_commit,
    lock_dialogue_rows,
    read_live_clarification,
)
from meyar.services.audit_repo import record_event
from meyar.services.profile_authority import get_current_authorized_profile

# One bounded repair for the clarification classifier (A2) and for the
# single agent-plan-v1 proposal per turn (D-092 §10.3).
MAX_DECISION_ATTEMPTS = 2
MAX_PLAN_PROPOSAL_ATTEMPTS = 2
# Bounds how much of one candidate's profile a single GET_CANDIDATE_EVIDENCE
# call surfaces — a generous, but not unbounded, response even for a broad
# (topic-less) request. See _dispatch_evidence.
MAX_EVIDENCE_MATCHES = 30
# Bounded retry for DRAFT_JOB_CRITERIA's own drafting call, matching the
# D-038 grounded-synthesis precedent (_synthesize_grounded_answer).
MAX_JD_DRAFT_ATTEMPTS = 2
# Issue #88 slice B: the scope POST /ui/agent itself requires; service-level
# callers without a UIContext validate capability plans against it.
DEFAULT_AGENT_TURN_SCOPES = frozenset({"candidates:read"})
# D-092 §11.3: fixed HR copy for a Layer-1 plan rejection. Codes, capability
# names and ids go to audit only.
PLAN_REJECTED_COPY = "Bu sorğunu təhlükəsiz icra edə bilmədim; zəhmət olmasa dəqiqləşdirin."


def _fold(text: str) -> str:
    return fold_az_ascii(normalize_azerbaijani_case(text))


def normalize_message_newlines(text: str) -> str:
    """One canonical newline form (LF) for an HR message (issue #84).

    Browser textareas submit CRLF; transport must never change semantics.
    Applied once at the agent-turn boundary BEFORE routing, span offsets,
    hashing, provenance and the transcript are produced, so every one of
    them refers to the same canonical source text."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _normalize_source_text(text: str) -> str:
    return " ".join(_fold(text).split())


_TITLE_STOPWORDS = frozenset(
    {"a", "an", "the", "ucun", "uzre", "axtaririq", "role", "rol", "vakansiya"}
)


def _title_is_attributed(title: str, jd_text: str) -> bool:
    title_tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", _normalize_source_text(title))
        if token not in _TITLE_STOPWORDS
    ]
    source_tokens = set(re.findall(r"[a-z0-9]+", _normalize_source_text(jd_text)))
    return bool(title_tokens) and all(token in source_tokens for token in title_tokens)


_AGENT_RESPONSE_TEXT: dict[AgentResponseCode, str] = {
    AgentResponseCode.GREETING: (
        "Salam! Namizəd axtarışı, profil sübutları və vakansiya meyarları ilə bağlı "
        "kömək edə bilərəm."
    ),
    AgentResponseCode.ACKNOWLEDGEMENT: "Sorğu tamamlandı.",
    AgentResponseCode.NEED_MORE_DETAIL: (
        "Sorğunu bir qədər dəqiqləşdirin: namizəd axtarışı, profil/sübut baxışı və ya "
        "vakansiya meyarı hazırlamaq istədiyinizi qeyd edin."
    ),
    AgentResponseCode.CANDIDATE_REFERENCE_REQUIRED: (
        "Əvvəlcə namizədləri axtarın, sonra nəticə sırasındakı namizədi göstərin."
    ),
    AgentResponseCode.UNSUPPORTED_REQUEST: (
        "Bu sorğu mövcud MEYAR alətləri ilə təhlükəsiz şəkildə icra edilmir."
    ),
    AgentResponseCode.HIRING_DECISION_REQUIRES_HUMAN: (
        "MEYAR sübutları və deterministik qiymətləndirməni təqdim edir; işə qəbul "
        "qərarını səlahiyyətli insan verir."
    ),
    AgentResponseCode.RESULT_CONTEXT_REQUIRED: (
        "Əvvəlcə namizəd axtarışı aparın, sonra cari nəticələr üzərində əməliyyat "
        "edə bilərsiniz (məsələn, \"ilk üçü\" və ya \"bunlardan SQL bilənlər\")."
    ),
}


def _agent_response_text(code: AgentResponseCode) -> str:
    return _AGENT_RESPONSE_TEXT[code]


def _outcome_for_resolution_failure(
    failure: ResultSetResolutionFailure | None,
) -> AgentTurnOutcome:
    """Maps a candidate_ref resolution failure (issue #49) to the turn
    outcome HR sees. STALE/EXPIRED get their own truthful outcome (re-run
    the search); every other reason (no active result set, cross-tenant/
    cross-session/cross-epoch mismatch, out-of-range ordinal, or the
    candidate losing authorization) is the same CANDIDATE_REF_NOT_FOUND
    outcome the ordinal-memory mechanism always used — deliberately not
    distinguished further outward, so cross-tenant/cross-session probing
    can never learn anything from the outcome text (see
    meyar.services.agent_result_set_repo.ResultSetResolutionFailure)."""
    if failure in (
        ResultSetResolutionFailure.STALE,
        ResultSetResolutionFailure.UNSUPPORTED_SNAPSHOT_POLICY,
    ):
        return AgentTurnOutcome.RESULT_SET_STALE
    if failure == ResultSetResolutionFailure.EXPIRED:
        return AgentTurnOutcome.RESULT_SET_EXPIRED
    return AgentTurnOutcome.CANDIDATE_REF_NOT_FOUND


def _outcome_and_message_for_refinement_failure(
    failure: ResultSetResolutionFailure,
) -> tuple[AgentTurnOutcome, str | None]:
    """Maps a REFINE_CANDIDATE_RESULTS active-result-set validation
    failure (issue #49 PR49-2) to the turn outcome/message HR sees.
    STALE/EXPIRED get the same truthful, dedicated outcome candidate_ref
    resolution uses (re-run the search); every other reason (no active
    result set at all, or a cross-tenant/cross-session/cross-epoch
    mismatch) collapses to one deterministic clarification — deliberately
    not distinguished further outward, mirroring
    _outcome_for_resolution_failure's own fail-closed discipline."""
    if failure in (
        ResultSetResolutionFailure.STALE,
        ResultSetResolutionFailure.UNSUPPORTED_SNAPSHOT_POLICY,
    ):
        return AgentTurnOutcome.RESULT_SET_STALE, None
    if failure == ResultSetResolutionFailure.EXPIRED:
        return AgentTurnOutcome.RESULT_SET_EXPIRED, None
    return (
        AgentTurnOutcome.CLARIFICATION_REQUESTED,
        _agent_response_text(AgentResponseCode.RESULT_CONTEXT_REQUIRED),
    )


async def _dispatch_search(
    db: AsyncSession,
    llm: LLMProvider,
    *,
    tenant_id: uuid.UUID,
    session_context: TurnSessionState,
    previous_result_set_id: uuid.UUID | None,
    natural_language_request: str,
    as_of_date: date,
    embedding_config: EmbeddingSearchConfig,
    embedding_provider: EmbeddingProvider | None,
) -> tuple[AgentToolResult, uuid.UUID | None]:
    """Forwards the SERVER-resolved search source (issue #88: the
    capability step's WHOLE_MESSAGE, exact grounded quotes, or bound resume
    source — never model-authored text), unmodified, into the existing
    frozen NL search-planner pipeline (D-031) — this module never
    re-implements filter extraction, prohibited-attribute checks, or the
    no-silent-weakening rule; it only reuses them. On an executable
    search, persists a brand-new server-owned AgentResultSet (issue #49)
    and returns its id; a non-executable/failed search never clears a
    prior valid active_result_set_id — only a successful search replaces
    it (returns None to signal "keep the existing pointer")."""
    planned = await plan_and_search_candidates(
        db,
        llm,
        tenant_id=tenant_id,
        natural_language_request=natural_language_request,
        as_of_date=as_of_date,
        embedding_config=embedding_config,
        embedding_provider=embedding_provider,
    )
    tool_result = AgentToolResult(
        tool_name=AgentActionType.SEARCH_CANDIDATES,
        search=AgentSearchToolResult(response=planned),
    )
    if planned.plan.executable and planned.search_response is not None:
        result_set = await create_result_set_from_search(
            db,
            tenant_id=tenant_id,
            browser_session_id=session_context.browser_session_id,
            session_context=session_context,
            planned=planned,
            previous_result_set_id=previous_result_set_id,
        )
        return tool_result, result_set.id
    return tool_result, None


async def _dispatch_profile(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_ref: int,
    session_context: TurnSessionState,
) -> tuple[AgentToolResult, CandidateProfileExtraction | None, ResultSetResolutionFailure | None]:
    """``candidate_ref`` is the server-parsed ordinal (D-092 §10.2).
    Returns (tool result, the raw validated profile when found, the
    resolution failure reason when not) — the profile is handed back
    separately so run_agent_turn can build grounded-answer facts (D-037/
    D-038) without a second, redundant DB fetch. candidate_ref resolution
    (issue #49) is entirely owned by
    meyar.services.agent_result_set_repo.resolve_active_candidate_ref —
    this function never resolves an ordinal itself."""
    resolved = await resolve_active_candidate_ref(
        db,
        tenant_id=tenant_id,
        browser_session_id=session_context.browser_session_id,
        session_context=session_context,
        candidate_ref=candidate_ref,
    )
    if isinstance(resolved, ResultSetResolutionFailure):
        return (
            AgentToolResult(
                tool_name=AgentActionType.GET_CANDIDATE_PROFILE,
                profile=AgentProfileToolResult(candidate_ref=candidate_ref, found=False),
            ),
            None,
            resolved,
        )
    candidate_id = resolved.candidate_id
    authorized = await get_current_authorized_profile(
        db, tenant_id=tenant_id, candidate_id=candidate_id
    )
    if authorized is None:
        return (
            AgentToolResult(
                tool_name=AgentActionType.GET_CANDIDATE_PROFILE,
                profile=AgentProfileToolResult(
                    candidate_ref=candidate_ref,
                    candidate_id=candidate_id,
                    found=False,
                    profile_status=None,
                ),
            ),
            None,
            None,
        )
    version, profile = authorized
    return (
        AgentToolResult(
            tool_name=AgentActionType.GET_CANDIDATE_PROFILE,
            profile=AgentProfileToolResult(
                candidate_ref=candidate_ref,
                candidate_id=candidate_id,
                found=True,
                profile_status=version.status,
                profile=profile,
            ),
        ),
        profile,
        None,
    )


# (category key, item -> topic-matchable title) — mirrors the same six
# CandidateProfileExtraction categories meyar.ui.service._facts presents,
# duplicated deliberately (not imported from meyar.ui) so this module
# never depends on the presentation layer.
_EVIDENCE_CATEGORIES = (
    ("skills", lambda item: item.name),
    (
        "employment_history",
        lambda item: item.title + (f" — {item.organization}" if item.organization else ""),
    ),
    (
        "education",
        lambda item: (
            combine_degree_and_field(item.degree, item.field_of_study)
            or (item.institution or "Təhsil")
        ),
    ),
    ("certifications", lambda item: item.name),
    ("languages", lambda item: item.language),
    ("projects", lambda item: item.description),
    # Slice 3 (issue #32): surfaces the same skill/domain grounding the
    # deterministic evaluators consume, so HR can ask "evidence for Java"
    # or "evidence for AML" and get the attributable-period items back.
    ("skill_experience", lambda item: item.skill_name),
    ("domain_experience", lambda item: item.domain),
)


@dataclass
class RefineDispatchResult:
    """Exactly one of (tool_result, resolution_failure, rejection_message)
    is set on any given call to _dispatch_refine — see its docstring."""

    tool_result: AgentToolResult | None = None
    new_result_set_id: uuid.UUID | None = None
    resolution_failure: ResultSetResolutionFailure | None = None
    # Set when the filter source could not be safely interpreted/
    # executed as a deterministic structured filter (non-executable plan,
    # or one that would require semantic/hybrid behavior) — fixed,
    # server-owned copy, never the raw planner rejection reason or the
    # HR's own filter text.
    rejection_message: str | None = None


def _preferred_filters_are_empty(filters: PreferredFilters) -> bool:
    """True only when a STRUCTURED_ONLY plan's preferred_filters carries no
    soft-ranking signal at all. current-result refinement (issue #49
    PR49-2) supports required (hard eligibility) filters only — it never
    reranks — so a populated preferred_filters must reject the WHOLE
    refinement rather than silently execute only the required portion. See
    _dispatch_refine."""
    return not (
        filters.skills
        or filters.certifications
        or filters.languages
        or filters.education
        or filters.min_total_experience_years is not None
        or filters.skill_experience
        or filters.domain_experience
        or filters.language_levels
    )


async def _dispatch_refine(
    db: AsyncSession,
    llm: LLMProvider,
    *,
    tenant_id: uuid.UUID,
    session_context: TurnSessionState,
    filter_query: str | None,
    limit: int | None,
    as_of_date: date,
    embedding_config: EmbeddingSearchConfig,
) -> RefineDispatchResult:
    """REFINE_CANDIDATE_RESULTS dispatch (issue #49 PR49-2, hardened by an
    independent-audit correction). Authority order:

    1. The active result set is PRE-validated (meyar.services.
       agent_result_set_repo.validate_active_result_set_for_refinement)
       BEFORE the planner is ever invoked — a stale/expired/missing/
       foreign-session active context must fail with its own truthful
       reason regardless of planner health, never surfaced as a generic
       planner-unavailable clarification.
    2. When ``filter_query`` (the SERVER-resolved filter source — never
       model-authored text) is set, it is forwarded UNMODIFIED into
       the existing frozen NL search-planner pipeline (meyar.search.
       planner_service.plan_candidate_search — planning only, never
       meyar.search.service.search_candidates itself, so this never
       becomes a fresh tenant-wide search) to derive a validated,
       deterministic RequiredFilters shape — the exact same prohibited-
       attribute/no-silent-weakening discipline SEARCH_CANDIDATES already
       gets, reused rather than reimplemented. A non-executable plan, one
       that would require SEMANTIC_ONLY/HYBRID mode, or one carrying ANY
       populated preferred_filters (a STRUCTURED_ONLY plan may legitimately
       have one — current-result refinement supports required filters
       only, never reranking) fails closed with a fixed clarification — it
       is never silently downgraded/partially executed and never run as a
       global search (docs/DECISIONS.md D-084).
    3. The planner's own validated explicit-limit interpretation
       (plan.interpretation.result_limit/used_default_limit) is reconciled
       against ``limit`` (the server-parsed count) into one effective_limit: the planner's own
       default search limit never becomes an implicit refinement
       truncation, and a genuine conflict between the two is rejected
       rather than silently resolved one way.

    All actual membership/ordering/limit authority beyond that is
    meyar.services.agent_result_set_repo.create_result_set_from_refinement
    — including its own internal revalidation (TOCTOU defense-in-depth,
    kept alongside the pre-validation above) — this function never
    computes membership itself."""
    validated_active_result_set = await validate_active_result_set_for_refinement(
        db,
        tenant_id=tenant_id,
        browser_session_id=session_context.browser_session_id,
        session_context=session_context,
    )
    if isinstance(validated_active_result_set, ResultSetResolutionFailure):
        return RefineDispatchResult(resolution_failure=validated_active_result_set)

    filter_request: CandidateSearchRequest | None = None
    planner_explicit_limit: int | None = None
    if filter_query is not None:
        plan = await plan_candidate_search(
            db,
            llm,
            tenant_id=tenant_id,
            natural_language_request=filter_query,
            as_of_date=as_of_date,
            embedding_config=embedding_config,
        )
        if not plan.executable or plan.search_request is None:
            return RefineDispatchResult(
                rejection_message=_agent_response_text(AgentResponseCode.UNSUPPORTED_REQUEST)
            )
        if plan.search_request.mode != SearchMode.STRUCTURED_ONLY:
            return RefineDispatchResult(
                rejection_message=_agent_response_text(AgentResponseCode.UNSUPPORTED_REQUEST)
            )
        if (
            # semantic_query/embedding_config are already implied None by
            # STRUCTURED_ONLY's own CandidateSearchRequest validator — kept
            # as an explicit authority check here rather than assumed.
            plan.search_request.semantic_query is not None
            or plan.search_request.embedding_config is not None
            or not _preferred_filters_are_empty(plan.search_request.preferred_filters)
        ):
            return RefineDispatchResult(
                rejection_message=_agent_response_text(AgentResponseCode.UNSUPPORTED_REQUEST)
            )
        filter_request = plan.search_request
        planner_explicit_limit = (
            None if plan.interpretation.used_default_limit else plan.interpretation.result_limit
        )
        if planner_explicit_limit is not None and planner_explicit_limit > MAX_CANDIDATE_REF:
            # A refinement limit can never exceed the same bound a
            # candidate_ref ordinal itself is bounded to (MAX_CANDIDATE_REF)
            # — an HR-text count the search policy itself could
            # produce (up to MAX_SEARCH_LIMIT=100) but a refinement could
            # never honor fails closed here rather than crashing on the
            # narrower AgentRefineToolResult.requested_limit bound below.
            return RefineDispatchResult(
                rejection_message=_agent_response_text(AgentResponseCode.UNSUPPORTED_REQUEST)
            )

    if (
        planner_explicit_limit is not None
        and limit is not None
        and limit != planner_explicit_limit
    ):
        # The planner's validated count of the filter source and the
        # server-parsed limit quote disagree — the server must never
        # silently pick one interpretation over the other.
        return RefineDispatchResult(
            rejection_message=_agent_response_text(AgentResponseCode.UNSUPPORTED_REQUEST)
        )
    effective_limit = limit if planner_explicit_limit is None else planner_explicit_limit

    outcome = await create_result_set_from_refinement(
        db,
        tenant_id=tenant_id,
        browser_session_id=session_context.browser_session_id,
        session_context=session_context,
        filter_request=filter_request,
        requested_limit=effective_limit,
    )
    if isinstance(outcome, ResultSetResolutionFailure):
        return RefineDispatchResult(resolution_failure=outcome)

    assert isinstance(outcome, RefinementResult)
    search_mode = SearchMode(outcome.result_set.search_mode)
    response = CandidateSearchResponse(
        mode=search_mode,
        policy_version=outcome.result_set.search_policy_version,
        results=[
            CandidateSearchResult(
                candidate_id=member.candidate_id,
                rank=member.ordinal,
                mode=search_mode,
                relevance_score=member.relevance_score,
                structured_score=member.structured_score,
                semantic_score=member.semantic_score,
                # Deliberately empty — a refined member's matched-filter
                # provenance is not re-derived for presentation in this
                # PR; the candidate card still renders identity/summary
                # normally, just without the "Məcburi uyğunluqlar" badge.
                required_filters_matched=[],
                preferred_filters_matched=[],
                candidate_profile_version_id=member.candidate_profile_version_id,
                candidate_embedding_version_id=member.candidate_embedding_version_id,
                search_policy_version=outcome.result_set.search_policy_version,
            )
            for member in outcome.members
        ],
        result_count=len(outcome.members),
        eligible_profile_count=outcome.source_result_count,
        compatible_embedding_count=0,
        excluded_missing_embedding_count=0,
        limit=effective_limit or outcome.source_result_count,
        embedding_config=None,
    )
    tool_result = AgentToolResult(
        tool_name=AgentActionType.REFINE_CANDIDATE_RESULTS,
        refine=AgentRefineToolResult(
            response=response,
            source_result_count=outcome.source_result_count,
            has_filter=filter_request is not None,
            requested_limit=effective_limit,
            limit_truncated=outcome.limit_truncated,
        ),
    )
    return RefineDispatchResult(
        tool_result=tool_result, new_result_set_id=outcome.result_set.id
    )


async def _dispatch_evidence(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_ref: int,
    evidence_topic: str | None,
    session_context: TurnSessionState,
) -> tuple[AgentToolResult, CandidateProfileExtraction | None, ResultSetResolutionFailure | None]:
    """``candidate_ref`` is the server-parsed ordinal and ``evidence_topic``
    the server-resolved exact topic slice of the message (or None for all
    evidence) — D-092 §10.2 rules 5/6.
    Returns (tool result, the raw validated profile when found, the
    resolution failure reason when not) — see _dispatch_profile's
    docstring; the same profile backs D-037/D-038 grounded-answer
    synthesis for both tools identically (never the raw evidence quote
    text, which stays server-rendered-only, never model input)."""
    resolved = await resolve_active_candidate_ref(
        db,
        tenant_id=tenant_id,
        browser_session_id=session_context.browser_session_id,
        session_context=session_context,
        candidate_ref=candidate_ref,
    )
    if isinstance(resolved, ResultSetResolutionFailure):
        return (
            AgentToolResult(
                tool_name=AgentActionType.GET_CANDIDATE_EVIDENCE,
                evidence=AgentEvidenceToolResult(
                    candidate_ref=candidate_ref,
                    found=False,
                ),
            ),
            None,
            resolved,
        )
    candidate_id = resolved.candidate_id
    authorized = await get_current_authorized_profile(
        db, tenant_id=tenant_id, candidate_id=candidate_id
    )
    if authorized is None:
        return (
            AgentToolResult(
                tool_name=AgentActionType.GET_CANDIDATE_EVIDENCE,
                evidence=AgentEvidenceToolResult(
                    candidate_ref=candidate_ref,
                    candidate_id=candidate_id,
                    found=False,
                    profile_status=None,
                ),
            ),
            None,
            None,
        )
    version, profile = authorized

    topic_folded = _fold(evidence_topic) if evidence_topic else None
    matches: list[EvidenceMatchItem] = []
    resolved_topic: str | None = None
    for category, title_fn in _EVIDENCE_CATEGORIES:
        for item in getattr(profile, category):
            title = title_fn(item)
            if topic_folded is not None:
                title_folded = _fold(title)
                if topic_folded not in title_folded and title_folded not in topic_folded:
                    continue
                if resolved_topic is None:
                    resolved_topic = title
            matches.append(
                EvidenceMatchItem(category=category, title=title, evidence=item.evidence)
            )
            if len(matches) >= MAX_EVIDENCE_MATCHES:
                break
        if len(matches) >= MAX_EVIDENCE_MATCHES:
            break

    return (
        AgentToolResult(
            tool_name=AgentActionType.GET_CANDIDATE_EVIDENCE,
            evidence=AgentEvidenceToolResult(
                candidate_ref=candidate_ref,
                candidate_id=candidate_id,
                found=True,
                profile_status=version.status,
                topic=resolved_topic,
                matches=matches,
            ),
        ),
        profile,
        None,
    )


# (category key, item -> (fact title, fact detail)) — the fact source
# for D-038 grounded-answer synthesis. Deliberately built from the SAME
# already-extracted, already-schema-validated CandidateProfileExtraction
# fields _EVIDENCE_CATEGORIES above uses for topic matching — never raw
# evidence.quote CV text, so the second-stage selection prompt's input
# surface stays exactly as narrow as the rest of this module's (D-030/
# D-031).
MAX_GROUNDED_FACTS = 30
MAX_SYNTHESIS_ATTEMPTS = 2


def _employment_detail(item) -> str | None:  # noqa: ANN001 - profile item, no shared base type
    end = "hazırda davam edir" if item.is_current else item.end_date
    parts = [part for part in (item.start_date, end) if part]
    return " — ".join(parts) if parts else None


_PROFILE_FACT_CATEGORIES = (
    ("skills", lambda item: (item.name, item.category)),
    (
        "employment_history",
        lambda item: (
            item.title + (f" — {item.organization}" if item.organization else ""),
            _employment_detail(item),
        ),
    ),
    (
        "education",
        lambda item: (
            combine_degree_and_field(item.degree, item.field_of_study)
            or (item.institution or "Təhsil"),
            item.date,
        ),
    ),
    ("certifications", lambda item: (item.name, item.date)),
    ("languages", lambda item: (item.language, item.proficiency)),
    ("projects", lambda item: (item.description, None)),
)


def _build_profile_facts(profile: CandidateProfileExtraction) -> list[GroundedFact]:
    """Flattens a candidate's profile into a small, bounded, indexed fact
    list for D-038 grounded-answer synthesis — the ONLY data the second
    model call ever sees about this candidate."""
    facts: list[GroundedFact] = []
    for category, fact_fn in _PROFILE_FACT_CATEGORIES:
        for item in getattr(profile, category):
            title, detail = fact_fn(item)
            facts.append(GroundedFact(id=len(facts), category=category, title=title, detail=detail))
            if len(facts) >= MAX_GROUNDED_FACTS:
                return facts

    # skill_experience/domain_experience (issue #32) reference an
    # employment_history index for CONTEXT ONLY (which job the claim
    # belongs to) — the detail shown here is always the item's OWN
    # attributable start_date/end_date/is_current (via _employment_detail,
    # duck-typed on those same three fields), never the linked employment
    # entry's full period; that entry's dates must never be presented as
    # if they were the skill/domain's own duration (see docs/DECISIONS.md).
    for item in profile.skill_experience:
        if len(facts) >= MAX_GROUNDED_FACTS:
            return facts
        entry = profile.employment_history[item.employment_index]
        title = f"{item.skill_name} — {entry.title}" if entry.title else item.skill_name
        facts.append(
            GroundedFact(
                id=len(facts),
                category="skill_experience",
                title=title,
                detail=_employment_detail(item),
            )
        )

    for item in profile.domain_experience:
        if len(facts) >= MAX_GROUNDED_FACTS:
            return facts
        facts.append(
            GroundedFact(
                id=len(facts),
                category="domain_experience",
                title=item.domain,
                detail=_employment_detail(item),
            )
        )

    return facts


# category -> a fixed AZ clause template. Every span of the resulting
# clause is EITHER one of these literal strings OR a verbatim
# fact.title/fact.detail value — there is no channel through which
# model-authored text can enter the rendered sentence, so an unsupported
# non-numeric claim (e.g. "managed a team") is structurally impossible,
# not merely checked-for (D-038; see docs/DECISIONS.md).
def _render_fact_clause(fact: GroundedFact) -> str:
    suffix = f" ({fact.detail})" if fact.detail else ""
    if fact.category == "skills":
        return f"{fact.title} bacarığı"
    if fact.category == "employment_history":
        return f"{fact.title}{suffix} mövqeyində çalışıb"
    if fact.category == "education":
        return f"{fact.title}{suffix} təhsili"
    if fact.category == "certifications":
        return f"{fact.title}{suffix} sertifikatı"
    if fact.category == "languages":
        return f"{fact.title}{suffix} dil bilgisi"
    if fact.category == "projects":
        return f"{fact.title} layihəsi"
    if fact.category == "skill_experience":
        return f"{fact.title}{suffix} təcrübəsi"
    if fact.category == "domain_experience":
        return f"{fact.title}{suffix} sahəsində təcrübə"
    return fact.title  # pragma: no cover - every real category is handled above


def render_grounded_answer(selection: GroundedSelection, facts: list[GroundedFact]) -> str | None:
    """Independently re-checks a model-produced GroundedSelection against
    the exact facts it was given, then builds the displayed sentence
    ENTIRELY server-side from those facts' own values — the model never
    authors any part of the final text. Rejects (returns None, meaning
    "fall back to the deterministic message") when a cited fact id was
    never actually supplied, or when nothing was selected and no caveat
    was set (nothing to say). See D-038."""
    valid_ids = {fact.id for fact in facts}
    if not set(selection.used_facts).issubset(valid_ids):
        return None
    facts_by_id = {fact.id: fact for fact in facts}
    seen: set[int] = set()
    ordered_facts = []
    for fact_id in selection.used_facts:
        if fact_id in seen:
            continue
        seen.add(fact_id)
        ordered_facts.append(facts_by_id[fact_id])
    if not ordered_facts and selection.caveat is None:
        return None

    sentences: list[str] = []
    if ordered_facts:
        clauses = [_render_fact_clause(fact) for fact in ordered_facts]
        sentences.append("Məlum faktlar: " + "; ".join(clauses) + ".")
    if selection.caveat == GroundedCaveat.DURATION_NOT_PROVEN:
        # Explicitly scoped to "bu mövzu üzrə" (regarding this topic/skill)
        # — not a bare "no concrete duration", which read as though no
        # dated evidence existed at all even when the facts sentence right
        # above it already cites dated employment history. The caveat
        # means the ASKED-ABOUT skill/topic's own duration is unproven,
        # never that the CV has no dates (PR #42 owner correction, issue
        # #33, D-045).
        sentences.append("Mövcud sübut bu mövzu üzrə konkret təcrübə müddətini əsaslandırmır.")
    return " ".join(sentences)


async def _synthesize_grounded_answer(
    llm: LLMProvider, *, question: str, facts: list[GroundedFact]
) -> str | None:
    """Returns a server-built grounded answer, or None when selection is
    unavailable/fails/doesn't validate — callers must treat None exactly
    like "no model framing available" (the existing deterministic
    fallback), never as a turn failure (D-036/D-038: a successful tool
    result is never downgraded to an error because this optional step
    didn't pan out)."""
    if not facts:
        return None
    for attempt in range(1, MAX_SYNTHESIS_ATTEMPTS + 1):
        try:
            selection, _provenance = await llm.select_grounded_facts(
                question=question, facts=facts, repair=attempt > 1
            )
        except ModelSchemaInvalidError:
            continue
        except (ModelTimeoutError, ModelUnavailableError, LLMProviderError):
            return None
        rendered = render_grounded_answer(selection, facts)
        if rendered is not None:
            return rendered
    return None


def _canonical_display_subject(kind: JDDraftCriterionKind, subject: str) -> str:
    if kind == JDDraftCriterionKind.DOMAIN_EXPERIENCE:
        canonical = "banking" if subject == "bank" else canonicalize_domain(subject)
        if subject.isupper():
            return subject
        if canonical != _normalize_source_text(subject):
            return canonical.title()
        return subject[:1].upper() + subject[1:]
    normalized = (
        normalize_skill_name(subject)
        if kind
        in (
            JDDraftCriterionKind.SKILL,
            JDDraftCriterionKind.SKILL_EXPERIENCE,
        )
        else subject
    )
    return normalized[:1].upper() + normalized[1:]


def _build_authorized_semantic_draft(
    *,
    jd_text: str,
    title: str | None,
    canonical: list[CanonicalRequirement],
    source_spans: list[RequirementSpan],
    requested_result_limit: int | None,
    result_limit: int,
    result_limit_was_bounded: bool,
    result_limit_needs_review: bool,
    ungrounded_count: int = 0,
    unsupported_language: SupportedInputLanguage | None = None,
    wrong_mode_guidance: bool = False,
    source_sha256: str | None = None,
    semantic_prompt_version: str | None = None,
    semantic_model: SemanticModelProvenance | None = None,
    rejected_proposal_count: int = 0,
) -> AgentToolResult:
    """Construct the public draft only from validated canonical requirements.

    Issue #84: every CriterionIn here comes from a ``CanonicalRequirement``
    whose subject is canonical and source-grounded (meyar.agent.
    canonical_requirements). A raw grammatical remainder can never reach a
    CriterionIn; unresolved spans stay visible as review items, and an
    unresolved explicit MUST_HAVE blocks confirmation (``blocking``).
    """
    spans_by_id = {span.span_id: span for span in source_spans}
    used_ids: set[str] = set()
    # Issue #84 collision policy: at most one CriterionIn per canonical
    # identity; exact-duplicate spans all point at it (one weight).
    criteria_by_identity: dict[tuple[str, str], CriterionIn] = {}
    conflict_members: dict[int, list[CanonicalRequirement]] = {}
    for item in canonical:
        if (
            item.conflict_group is not None
            and item.interpretation_state == SemanticRequirementState.NEEDS_HUMAN_REVIEW
        ):
            conflict_members.setdefault(item.conflict_group, []).append(item)
    must_have: list[CriterionIn] = []
    preferred: list[CriterionIn] = []
    unsupported: list[UnsupportedJDCriterionItem] = []
    needs_review: list[NeedsReviewJDCriterionItem] = []
    results: list[RequirementSpanResult] = []
    prohibited_count = 0

    for item in canonical:
        span = spans_by_id[item.span_id]
        state = item.interpretation_state
        criterion: CriterionIn | None = None
        shape_validated = item.shape_validated
        duplicate_of_existing = False
        if state == SemanticRequirementState.SCORABLE:
            assert item.kind is not None
            assert item.criterion_type is not None
            kind = CriterionKind(item.kind.value)
            subject = item.canonical_subject or span.text
            if item.kind == JDDraftCriterionKind.DOMAIN_EXPERIENCE:
                subject = _canonical_display_subject(item.kind, subject)
            value = None if kind == CriterionKind.EXPERIENCE else subject
            identity = semantic_identity(item.kind, item.canonical_subject)
            existing = criteria_by_identity.get(identity)
            if existing is not None and semantic_parameters(
                existing.type, existing.min_years, existing.required_level
            ) == semantic_parameters(item.criterion_type, item.min_years, item.required_level):
                criterion = existing
                duplicate_of_existing = True
            elif existing is not None:  # pragma: no cover - collision policy invariant
                raise RuntimeError("Semantic collision reached criterion materialization.")
            else:
                try:
                    criterion = CriterionIn(
                        id=slugify_criterion_label(subject, used_ids),
                        kind=kind,
                        type=item.criterion_type,
                        label=subject,
                        value=value,
                        min_years=item.min_years,
                        required_level=item.required_level,
                        weight=1.0,
                    )
                    criteria_by_identity[identity] = criterion
                except ValidationError:
                    state = SemanticRequirementState.NEEDS_HUMAN_REVIEW
                    shape_validated = False

        if criterion is not None:
            if not duplicate_of_existing:
                bucket = must_have if criterion.type == CriterionType.MUST_HAVE else preferred
                bucket.append(criterion)
        elif item.conflict_group is not None and item.conflict_group in conflict_members:
            members = conflict_members[item.conflict_group]
            if members[0].span_id == item.span_id:
                assert item.kind is not None and item.canonical_subject is not None
                options: list[SemanticParameters] = []
                for member in members:
                    assert member.criterion_type is not None
                    option = semantic_parameters(
                        member.criterion_type, member.min_years, member.required_level
                    )
                    if option not in options:
                        options.append(option)
                joined = " · ".join(spans_by_id[member.span_id].text for member in members)
                needs_review.append(
                    NeedsReviewJDCriterionItem(
                        requirement=joined[:500],
                        criterion_type=None,
                        span_id=item.span_id,
                        kind=item.kind,
                        subject=(
                            _canonical_display_subject(item.kind, item.canonical_subject)
                            if item.kind == JDDraftCriterionKind.DOMAIN_EXPERIENCE
                            else item.canonical_subject
                        ),
                        blocking=True,
                        conflict_span_ids=[member.span_id for member in members],
                        conflict_options=options,
                    )
                )
        elif state == SemanticRequirementState.PROHIBITED:
            prohibited_count += 1
        elif state == SemanticRequirementState.UNSUPPORTED and item.criterion_type is not None:
            unsupported.append(
                UnsupportedJDCriterionItem(
                    requirement=span.text,
                    criterion_type=item.criterion_type,
                )
            )
        else:
            reviewable_shape = bool(
                state == SemanticRequirementState.NEEDS_HUMAN_REVIEW
                and shape_validated
                and item.kind is not None
                and item.canonical_subject
            )
            blocking = bool(
                state == SemanticRequirementState.NEEDS_HUMAN_REVIEW
                and item.criterion_type == CriterionType.MUST_HAVE
            )
            needs_review.append(
                NeedsReviewJDCriterionItem(
                    requirement=span.text,
                    criterion_type=item.criterion_type,
                    span_id=span.span_id if (reviewable_shape or blocking) else None,
                    kind=item.kind if reviewable_shape else None,
                    subject=item.canonical_subject if reviewable_shape else None,
                    min_years=item.min_years,
                    required_level=item.required_level,
                    allowed_types=(
                        [CriterionType.MUST_HAVE, CriterionType.PREFERRED]
                        if reviewable_shape and item.criterion_type is None
                        else []
                    ),
                    blocking=blocking,
                )
            )

        public_state = RequirementSpanState(state.value)
        results.append(
            RequirementSpanResult(
                span_id=span.span_id,
                start_offset=span.start_offset,
                end_offset=span.end_offset,
                text=None if public_state == RequirementSpanState.PROHIBITED else span.text,
                normalized=(
                    None if public_state == RequirementSpanState.PROHIBITED else span.normalized
                ),
                state=public_state,
                criterion_type=item.criterion_type,
                criterion_id=criterion.id if criterion is not None else None,
                interpretation_source=(
                    item.interpretation_source.persisted()
                    if criterion is not None or shape_validated
                    else None
                ),
            )
        )

    safe_title = title if title and _title_is_attributed(title, jd_text) else "Vakansiya qaralaması"
    return AgentToolResult(
        tool_name=AgentActionType.DRAFT_JOB_CRITERIA,
        job_draft=AgentJobDraftToolResult(
            title=safe_title,
            draft_id=uuid.uuid4(),
            requested_result_limit=requested_result_limit,
            result_limit=result_limit,
            result_limit_was_bounded=result_limit_was_bounded,
            result_limit_needs_review=result_limit_needs_review,
            must_have=must_have,
            preferred=preferred,
            unsupported=unsupported,
            needs_review=needs_review,
            ungrounded_count=ungrounded_count,
            prohibited_count=prohibited_count,
            requirements=results,
            unsupported_language=unsupported_language,
            wrong_mode_guidance=wrong_mode_guidance,
            semantic_policy_version=JD_SEMANTIC_POLICY_VERSION,
            source_sha256=source_sha256,
            semantic_prompt_version=semantic_prompt_version,
            semantic_model=semantic_model,
            rejected_proposal_count=rejected_proposal_count,
            source_jd_text=jd_text if source_sha256 is not None else None,
        ),
    )


def _span_hints(analysis: SemanticAnalysis) -> dict[str, dict[str, str]]:
    """Safe deterministic hints for the local model (structure only)."""
    hints: dict[str, dict[str, str]] = {}
    for semantic in analysis.requirements:
        hint: dict[str, str] = {}
        if semantic.criterion_type is not None:
            hint["modality"] = semantic.criterion_type.value
        if semantic.criterion_family is not None:
            hint["family"] = semantic.criterion_family.value
        if semantic.min_years is not None:
            hint["min_years"] = f"{semantic.min_years:g}"
        if semantic.required_level is not None:
            hint["required_level"] = semantic.required_level
        hints[semantic.requirement_span_id] = hint
    return hints


async def _dispatch_draft_job_criteria(llm: LLMProvider, *, jd_text: str) -> AgentToolResult | None:
    """Interpret a JD through the canonical, source-bound server boundary.

    Issue #84 (policy ``JD_SEMANTIC_POLICY_VERSION``): the deterministic
    analysis owns spans, offsets, modality, prohibited/unsupported
    classification, duration and level. The local model (via LLMProvider) is
    asked only for canonical professional subjects of those exact spans; every
    proposal is server-validated (meyar.agent.canonical_requirements) and can
    close only a subject-normalization gap. Provider failure never erases a
    requirement and never degrades to a malformed scorable subject.
    Unsupported languages fail closed before inference.
    """
    analysis = analyze_hr_text(jd_text)
    source_sha256 = hashlib.sha256(jd_text.encode("utf-8")).hexdigest()
    if analysis.language == SupportedInputLanguage.UNSUPPORTED:
        return _build_authorized_semantic_draft(
            jd_text=jd_text,
            title=None,
            canonical=[],
            source_spans=[],
            source_sha256=source_sha256,
            requested_result_limit=analysis.result_count.requested,
            result_limit=analysis.result_count.effective,
            result_limit_was_bounded=analysis.result_count.was_bounded,
            result_limit_needs_review=analysis.result_count_needs_review,
            unsupported_language=SupportedInputLanguage.UNSUPPORTED,
        )

    folded_jd = _fold(jd_text)
    raw_has_prohibited_text = find_prohibited_term(jd_text) is not None
    simple_search_in_vacancy_mode = bool(
        not raw_has_prohibited_text
        and re.search(
            r"\b(?:show|find|display|list|return|search\s+for|goster\w*|tap\w*|"
            r"cixart\w*|axtar\w*)\b",
            folded_jd,
        )
        and re.search(
            r"\b(?:candidates?|applicants?|profiles?|results?|namized\w*|netice\w*)\b",
            folded_jd,
        )
        and not re.search(
            r"\b(?:vacancy|job\s+description|role\s+requirements?|vakansiya|"
            r"vezife\s+telebleri)\b",
            folded_jd,
        )
        and not re.search(
            r"\b(?:required|mandatory|preferred|optional|must|teleb\w*|mutleq\w*|"
            r"ustunluk\w*|mecburi\w*|vacib\w*)\b",
            folded_jd,
        )
    )
    if simple_search_in_vacancy_mode:
        return _build_authorized_semantic_draft(
            jd_text=jd_text,
            title=None,
            canonical=[],
            source_spans=[],
            requested_result_limit=analysis.result_count.requested,
            result_limit=analysis.result_count.effective,
            result_limit_was_bounded=analysis.result_count.was_bounded,
            result_limit_needs_review=analysis.result_count_needs_review,
            wrong_mode_guidance=True,
        )

    if len(analysis.spans) > MAX_JD_REQUIREMENT_SPANS:
        # Fail closed instead of raising: a JD with more material requirement
        # spans than one bounded draft can carry is never partially scored.
        return _overflow_jd_draft(jd_text=jd_text, analysis=analysis, source_sha256=source_sha256)

    draft: JDCriteriaDraft | None = None
    model_provenance: LLMResultProvenance | None = None
    if not raw_has_prohibited_text:
        hints = _span_hints(analysis)
        for attempt in range(1, MAX_JD_DRAFT_ATTEMPTS + 1):
            try:
                draft, model_provenance = await llm.draft_job_criteria(
                    jd_text,
                    requirement_spans=analysis.spans,
                    span_hints=hints,
                    repair=attempt > 1,
                )
                break
            except ModelSchemaInvalidError:
                continue
            except (ModelTimeoutError, ModelUnavailableError, LLMProviderError):
                break

    proposals = (
        [
            item
            for item in [*draft.must_have, *draft.preferred]
            if not is_result_count_only(item.requirement)
        ]
        if draft is not None
        else None
    )
    canonicalization = canonicalize_requirements(
        spans=analysis.spans, semantics=analysis.requirements, proposals=proposals
    )
    # An explicit ``Vakansiya:`` header is exact source title data and wins
    # over any model title; the model title is only a grounded fallback.
    explicit_title = explicit_vacancy_title(jd_text, analysis)
    return _build_authorized_semantic_draft(
        jd_text=jd_text,
        title=explicit_title or (draft.title if draft is not None else None),
        canonical=canonicalization.requirements,
        source_spans=analysis.spans,
        requested_result_limit=analysis.result_count.requested,
        result_limit=analysis.result_count.effective,
        result_limit_was_bounded=analysis.result_count.was_bounded,
        result_limit_needs_review=analysis.result_count_needs_review,
        # Disclosure only: source-unbound model proposals are counted, never
        # redisplayed and never material.
        ungrounded_count=canonicalization.ungrounded_proposal_count,
        source_sha256=source_sha256,
        # Only a successful, schema-valid local-model result is provenance
        # (prompt + model identity); raw model output is never retained.
        semantic_prompt_version=(
            JD_CRITERIA_DRAFT_PROMPT_VERSION
            if draft is not None and model_provenance is not None
            else None
        ),
        semantic_model=(
            SemanticModelProvenance(
                provider=model_provenance.provider,
                model_name=model_provenance.model_name,
                model_revision=model_provenance.model_revision,
            )
            if draft is not None and model_provenance is not None
            else None
        ),
        rejected_proposal_count=canonicalization.rejected_proposal_count,
    )


def _overflow_jd_draft(
    *, jd_text: str, analysis: SemanticAnalysis, source_sha256: str
) -> AgentToolResult:
    """Too many material spans for one bounded draft: nothing is scored and
    HR is asked to split the vacancy (never a partial, silently truncated
    criterion set)."""
    first, last = analysis.spans[0], analysis.spans[-1]
    return AgentToolResult(
        tool_name=AgentActionType.DRAFT_JOB_CRITERIA,
        job_draft=AgentJobDraftToolResult(
            title="Vakansiya qaralaması",
            draft_id=uuid.uuid4(),
            requested_result_limit=analysis.result_count.requested,
            result_limit=analysis.result_count.effective,
            result_limit_was_bounded=analysis.result_count.was_bounded,
            result_limit_needs_review=analysis.result_count_needs_review,
            needs_review=[
                NeedsReviewJDCriterionItem(
                    requirement=(
                        f"Elanda {len(analysis.spans)} tələb var; bir qaralama ən çox "
                        f"{MAX_JD_REQUIREMENT_SPANS} tələbi emal edir. Elanı hissələrə bölün."
                    ),
                    criterion_type=CriterionType.MUST_HAVE,
                    span_id="req-0001",
                    blocking=True,
                )
            ],
            requirements=[
                RequirementSpanResult(
                    span_id="req-0001",
                    start_offset=first.start_offset,
                    end_offset=last.end_offset,
                    text=jd_text[first.start_offset : last.end_offset][:4000],
                    normalized=None,
                    state=RequirementSpanState.NEEDS_HUMAN_REVIEW,
                    criterion_type=CriterionType.MUST_HAVE,
                )
            ],
            semantic_policy_version=JD_SEMANTIC_POLICY_VERSION,
            source_sha256=source_sha256,
        ),
    )


def jd_draft_audit_metadata(draft: AgentJobDraftToolResult) -> dict[str, object]:
    """Structural-only audit facts for one draft (no text, no model output)."""
    states = [item.state for item in draft.requirements]
    return {
        "semantic_policy_version": draft.semantic_policy_version,
        "scorable_count": states.count(RequirementSpanState.SCORABLE),
        "review_count": states.count(RequirementSpanState.NEEDS_HUMAN_REVIEW),
        "unsupported_count": states.count(RequirementSpanState.UNSUPPORTED),
        "prohibited_count": states.count(RequirementSpanState.PROHIBITED),
        "rejected_proposal_count": draft.rejected_proposal_count,
        "model_result_accepted": draft.semantic_model is not None,
        "blocking_review_count": sum(
            1 for item in draft.needs_review if item.blocking and not item.acknowledged_excluded
        ),
    }


def resolve_job_draft_review_modality(
    draft: AgentJobDraftToolResult,
    *,
    span_id: str,
    criterion_type: CriterionType,
) -> AgentJobDraftToolResult:
    """Resolve only a server-declared modality ambiguity for one span.

    Issue #84: a modality click can mint a CriterionIn only when the review
    item carries a server-validated canonical shape AND that canonical
    subject still re-validates against the exact source span. Arbitrary
    source text ("Bakıda") can never become scoring authority this way."""
    matches = [item for item in draft.needs_review if item.span_id == span_id]
    if len(matches) != 1:
        raise ValueError("Reviewable source requirement was not found.")
    review = matches[0]
    if criterion_type not in review.allowed_types:
        raise ValueError("That review resolution is not allowed for this requirement.")
    if review.kind is None or review.subject is None:
        raise ValueError("Reviewable source requirement has no canonical criterion shape.")
    result_matches = [item for item in draft.requirements if item.span_id == span_id]
    if (
        len(result_matches) != 1
        or result_matches[0].state != RequirementSpanState.NEEDS_HUMAN_REVIEW
    ):
        raise ValueError("Source requirement is not awaiting review.")
    source_text = result_matches[0].text or ""
    if (
        review.kind == JDDraftCriterionKind.OTHER
        or is_non_professional_requirement(source_text)
        or not is_canonical_subject(review.kind, review.subject)
        or (
            review.kind != JDDraftCriterionKind.EXPERIENCE
            and not subject_grounded_in_span(review.kind, review.subject, source_text)
        )
        or find_prohibited_term(source_text, review.subject)
    ):
        raise ValueError("Reviewable source requirement has no canonical criterion shape.")

    existing = _criterion_with_identity(draft, review.kind, review.subject)
    if existing is not None:
        # Issue #84: never a second weight for the same canonical requirement.
        if semantic_parameters(
            existing.type, existing.min_years, existing.required_level
        ) != semantic_parameters(criterion_type, review.min_years, review.required_level):
            raise ValueError("The same requirement already exists with different parameters.")
        return draft.model_copy(
            update={
                "needs_review": [item for item in draft.needs_review if item.span_id != span_id],
                "requirements": [
                    item.model_copy(
                        update={
                            "state": RequirementSpanState.SCORABLE,
                            "criterion_type": criterion_type,
                            "criterion_id": existing.id,
                        }
                    )
                    if item.span_id == span_id
                    else item
                    for item in draft.requirements
                ],
                "review_decisions": _with_review_decision(
                    draft,
                    span_id=span_id,
                    decision=SemanticReviewDecisionKind(criterion_type.value),
                ),
            }
        )
    used_ids = {criterion.id for criterion in [*draft.must_have, *draft.preferred]}
    criterion = CriterionIn(
        id=slugify_criterion_label(review.subject, used_ids),
        kind=CriterionKind(review.kind.value),
        type=criterion_type,
        label=review.subject,
        value=None if review.kind == JDDraftCriterionKind.EXPERIENCE else review.subject,
        min_years=review.min_years,
        required_level=review.required_level,
        weight=1.0,
    )
    requirements = [
        item.model_copy(
            update={
                "state": RequirementSpanState.SCORABLE,
                "criterion_type": criterion_type,
                "criterion_id": criterion.id,
            }
        )
        if item.span_id == span_id
        else item
        for item in draft.requirements
    ]
    updates: dict[str, object] = {
        "needs_review": [item for item in draft.needs_review if item.span_id != span_id],
        "requirements": requirements,
        "review_decisions": _with_review_decision(
            draft, span_id=span_id, decision=SemanticReviewDecisionKind(criterion_type.value)
        ),
    }
    if criterion_type == CriterionType.MUST_HAVE:
        updates["must_have"] = [*draft.must_have, criterion]
    else:
        updates["preferred"] = [*draft.preferred, criterion]
    return draft.model_copy(update=updates)


def _criterion_identity(criterion: CriterionIn) -> tuple[str, str]:
    return semantic_identity(JDDraftCriterionKind(criterion.kind.value), criterion.value)


def _criterion_with_identity(
    draft: AgentJobDraftToolResult, kind: JDDraftCriterionKind, subject: str | None
) -> CriterionIn | None:
    identity = semantic_identity(kind, subject)
    return next(
        (
            criterion
            for criterion in [*draft.must_have, *draft.preferred]
            if _criterion_identity(criterion) == identity
        ),
        None,
    )


def resolve_job_draft_semantic_conflict(
    draft: AgentJobDraftToolResult, *, span_id: str, option_index: int
) -> AgentJobDraftToolResult:
    """HR chooses ONE server-declared parameter set for a semantic conflict
    (issue #84). The chosen set becomes one CriterionIn supported by every
    conflicting span; the choice is recorded for durable provenance. No
    value is ever chosen automatically."""
    matches = [
        item for item in draft.needs_review if item.span_id == span_id and item.conflict_options
    ]
    if len(matches) != 1:
        raise ValueError("Conflicting source requirement was not found.")
    review = matches[0]
    if review.acknowledged_excluded:
        raise ValueError("That review resolution is not allowed for this requirement.")
    if not 0 <= option_index < len(review.conflict_options):
        raise ValueError("That review resolution is not allowed for this requirement.")
    assert review.kind is not None and review.subject is not None
    conflict_spans = set(review.conflict_span_ids)
    results = [item for item in draft.requirements if item.span_id in conflict_spans]
    if len(results) != len(conflict_spans) or any(
        item.state != RequirementSpanState.NEEDS_HUMAN_REVIEW or not item.text
        for item in results
    ):
        raise ValueError("Source requirement is not awaiting review.")
    if review.kind != JDDraftCriterionKind.EXPERIENCE and not all(
        subject_grounded_in_span(review.kind, review.subject, item.text or "") for item in results
    ):
        raise ValueError("Reviewable source requirement has no canonical criterion shape.")
    if _criterion_with_identity(draft, review.kind, review.subject) is not None:
        raise ValueError("The same requirement already exists with different parameters.")
    chosen = review.conflict_options[option_index]
    used_ids = {criterion.id for criterion in [*draft.must_have, *draft.preferred]}
    criterion = CriterionIn(
        id=slugify_criterion_label(review.subject, used_ids),
        kind=CriterionKind(review.kind.value),
        type=chosen.criterion_type,
        label=review.subject,
        value=None if review.kind == JDDraftCriterionKind.EXPERIENCE else review.subject,
        min_years=chosen.min_years,
        required_level=chosen.required_level,
        weight=1.0,
    )
    updates: dict[str, object] = {
        "needs_review": [item for item in draft.needs_review if item.span_id != span_id],
        "requirements": [
            item.model_copy(
                update={
                    "state": RequirementSpanState.SCORABLE,
                    "criterion_type": chosen.criterion_type,
                    "criterion_id": criterion.id,
                }
            )
            if item.span_id in conflict_spans
            else item
            for item in draft.requirements
        ],
        "conflict_resolutions": [
            *draft.conflict_resolutions,
            SemanticConflictResolution(
                span_ids=list(review.conflict_span_ids),
                options=list(review.conflict_options),
                chosen_index=option_index,
            ),
        ],
    }
    if chosen.criterion_type == CriterionType.MUST_HAVE:
        updates["must_have"] = [*draft.must_have, criterion]
    else:
        updates["preferred"] = [*draft.preferred, criterion]
    return draft.model_copy(update=updates)


def exclude_blocking_review_requirement(
    draft: AgentJobDraftToolResult, *, span_id: str
) -> AgentJobDraftToolResult:
    """HR explicitly acknowledges that one unresolved MUST_HAVE source
    requirement is NOT part of automatic ranking (issue #84).

    Never mints a CriterionIn: the requirement stays disclosed as
    review-required on the draft and on the persisted criteria version."""
    matches = [item for item in draft.needs_review if item.span_id == span_id]
    if len(matches) != 1:
        raise ValueError("Reviewable source requirement was not found.")
    review = matches[0]
    if not review.blocking or review.acknowledged_excluded:
        raise ValueError("That review resolution is not allowed for this requirement.")
    return draft.model_copy(
        update={
            "needs_review": [
                item.model_copy(update={"acknowledged_excluded": True})
                if item.span_id == span_id
                else item
                for item in draft.needs_review
            ],
            "review_decisions": _with_review_decision(
                draft,
                span_id=span_id,
                decision=SemanticReviewDecisionKind.EXCLUDED_BY_REVIEWER,
            ),
        }
    )


def _with_review_decision(
    draft: AgentJobDraftToolResult, *, span_id: str, decision: SemanticReviewDecisionKind
) -> list[SemanticReviewDecision]:
    """The final explicit human decision per span (issue #84): persisted with
    the confirmed criteria version, so it survives transcript retention."""
    return [
        *[item for item in draft.review_decisions if item.span_id != span_id],
        SemanticReviewDecision(span_id=span_id, decision=decision),
    ]


_FOLLOWUP_RE = re.compile(
    r"(?i)\b(?:yox|et|dəyiş|deyis|saxla|make|change|instead|not\s+mandatory|"
    r"məcburi\s+etmə|mecburi\s+etme|required|preferred|ustunluk|mecburi)\b"
)
_CONTEXT_ONLY_TARGET_RE = re.compile(
    r"(?i)\b(?:(?:this|that|the)\s+(?:(?:required|preferred)\s+)?"
    r"(?:requirement|criterion)|(?:bu|hemin)\s+teleb\w*)\b"
)


def _requested_followup_modality(text: str) -> CriterionType | None:
    """Resolve an explicit modality mutation without guessing from keywords."""
    folded = _fold(text)
    preferred = r"(?:preferred|ustunluk\w*)"
    required = (
        r"(?:required|mandatory|must(?:\s+have)?|mecburi\w*|mutleq\w*|esas\s+teleb)"
    )
    # In "X instead of Y", X is the requested state. In "from X to Y"
    # and Azerbaijani "X yox, Y", Y is the requested state.
    instead = re.search(r"(?i)\binstead\s+of\b", folded)
    if instead:
        prefix = folded[: instead.start()]
        if re.search(rf"\b{preferred}\b", prefix):
            return CriterionType.PREFERRED
        if re.search(rf"\b{required}\b", prefix):
            return CriterionType.MUST_HAVE
    destination = re.search(r"(?i)\b(?:to|yox\s*,?)\s+(?P<value>.+)$", folded)
    if destination:
        value = destination.group("value")
        if re.search(rf"\b{required}\b", value):
            return CriterionType.MUST_HAVE
        if re.search(rf"\b{preferred}\b", value):
            return CriterionType.PREFERRED
    matches = [
        (match.start(), CriterionType.MUST_HAVE)
        for match in re.finditer(rf"(?i)\b{required}\b", folded)
    ] + [
        (match.start(), CriterionType.PREFERRED)
        for match in re.finditer(rf"(?i)\b{preferred}\b", folded)
    ]
    if re.search(r"(?i)\b(?:make|change|et\w*|cevir\w*|olsun|saxla\w*)\b", folded) and matches:
        return max(matches, key=lambda item: item[0])[1]
    if re.search(rf"(?i)\bnot\s+{required}\b", folded) and re.search(
        rf"(?i)\b{preferred}\b", folded
    ):
        return CriterionType.PREFERRED
    return None


def _modified_requirement_results(
    draft: AgentJobDraftToolResult,
    *,
    criterion_id: str,
    criterion_type: CriterionType | None = None,
) -> list[RequirementSpanResult]:
    return [
        item.model_copy(
            update={"criterion_type": criterion_type}
            if item.criterion_id == criterion_id and criterion_type is not None
            else {}
        )
        for item in draft.requirements
    ]


def _apply_pending_draft_followup(
    draft: AgentJobDraftToolResult, user_message: str
) -> AgentJobDraftToolResult | None:
    """Apply only four bounded, source-explicit draft edits.

    The target must be uniquely identifiable in the pending server-held draft;
    otherwise no change is made and the caller asks HR to clarify/re-analyse.
    """
    folded = _fold(user_message)
    if not _FOLLOWUP_RE.search(folded):
        return None

    # Result limit: "10 yox, 5 nəfər göstər" / "change 10 to 5 candidates".
    values = [int(value) for value in re.findall(r"(?<!\w)\d{1,4}(?!\w)", folded)]
    has_result_noun = re.search(r"\b(?:nefer|namized\w*|candidates?|results?|goster)\b", folded)
    if len(values) >= 2 and has_result_noun:
        old, new = values[0], values[-1]
        if old != draft.result_limit:
            return None
        effective = min(max(new, 1), 100)
        return draft.model_copy(
            update={
                "draft_id": uuid.uuid4(),
                "requested_result_limit": new,
                "result_limit": effective,
                "result_limit_was_bounded": effective != new,
                "result_limit_needs_review": False,
                "modification_source_text": user_message,
            }
        )

    all_criteria = [*draft.must_have, *draft.preferred]

    def is_mentioned(criterion: CriterionIn) -> bool:
        aliases = {_fold(criterion.label), _fold(criterion.value or "")}
        if (
            criterion.kind == CriterionKind.DOMAIN_EXPERIENCE
            and _fold(criterion.value or "") == "banking"
        ):
            aliases.add("bank")
        if criterion.kind == CriterionKind.LANGUAGE and _fold(criterion.value or "") == "english":
            aliases.add("ingilis")
        return any(
            alias and re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", folded) for alias in aliases
        )

    requested_type = _requested_followup_modality(user_message)
    targets = [criterion for criterion in all_criteria if is_mentioned(criterion)]
    levels = re.findall(r"(?i)\b(?:a1|a2|b1|b2|c1|c2)\b", user_message)
    numeric = [
        float(value.replace(",", ".")) for value in re.findall(r"\d+(?:[.,]\d+)?", folded)
    ]
    if not targets and len(levels) >= 2:
        # "B2-ni C1 et": the old value itself identifies the target, but
        # only when exactly one criterion currently carries it.
        targets = [
            criterion
            for criterion in all_criteria
            if criterion.kind == CriterionKind.LANGUAGE
            and (criterion.required_level or "").casefold() == levels[0].casefold()
        ]
    elif not targets and len(numeric) >= 2:
        # "3 ili 5 et": likewise, exactly one criterion with that duration.
        targets = [
            criterion
            for criterion in all_criteria
            if criterion.kind in (CriterionKind.SKILL_EXPERIENCE, CriterionKind.DOMAIN_EXPERIENCE)
            and criterion.min_years == numeric[0]
        ]
    if (
        not targets
        and requested_type is not None
        and _CONTEXT_ONLY_TARGET_RE.search(folded)
    ):
        # A context-only phrase such as "make this preferred requirement
        # required" is safe only when the pending server-owned draft has
        # exactly one criterion in the source modality.  More than one is
        # genuinely ambiguous and must continue to fail truthfully.
        targets = [criterion for criterion in all_criteria if criterion.type != requested_type]
    if len(targets) != 1:
        return None
    target = targets[0]
    target_span = next(
        (item.span_id for item in draft.requirements if item.criterion_id == target.id), None
    )
    if target_span is None:
        return None

    if target.kind == CriterionKind.LANGUAGE and len(levels) >= 2:
        if (
            target.required_level is None
            or target.required_level.casefold() != levels[0].casefold()
        ):
            return None
        return _amended_draft(
            draft,
            target=target,
            span_id=target_span,
            field=SemanticCriterionAmendmentField.REQUIRED_LEVEL,
            new_value=levels[-1].upper(),
            user_message=user_message,
        )

    if target.kind in (CriterionKind.SKILL_EXPERIENCE, CriterionKind.DOMAIN_EXPERIENCE):
        if len(numeric) >= 2 and target.min_years == numeric[0]:
            return _amended_draft(
                draft,
                target=target,
                span_id=target_span,
                field=SemanticCriterionAmendmentField.MIN_YEARS,
                new_value=numeric[-1],
                user_message=user_message,
            )

    if requested_type is not None and requested_type != target.type:
        return _amended_draft(
            draft,
            target=target,
            span_id=target_span,
            field=SemanticCriterionAmendmentField.CRITERION_TYPE,
            new_value=requested_type,
            user_message=user_message,
        )
    # An explicit no-op or ambiguous direction fails truthfully. It must not
    # mint a fresh draft id that implies a mutation was applied.
    return None


_AMENDED_ATTRIBUTE = {
    SemanticCriterionAmendmentField.CRITERION_TYPE: "type",
    SemanticCriterionAmendmentField.MIN_YEARS: "min_years",
    SemanticCriterionAmendmentField.REQUIRED_LEVEL: "required_level",
}


def _amended_draft(
    draft: AgentJobDraftToolResult,
    *,
    target: CriterionIn,
    span_id: str,
    field: SemanticCriterionAmendmentField,
    new_value: object,
    user_message: str,
) -> AgentJobDraftToolResult | None:
    """Apply ONE bounded field change to ONE resolved criterion and append an
    immutable, ordered, server-owned amendment (issue #84). The amendment is
    the durable explanation of why the confirmed value differs from the
    source span; the whole ordered chain is persisted, never only the last."""
    attribute = _AMENDED_ATTRIBUTE[field]
    previous = getattr(target, attribute)
    if previous is None:
        return None
    try:
        replacement = CriterionIn.model_validate(
            {**target.model_dump(), attribute: new_value}
        )
        amendment = SemanticCriterionAmendment(
            sequence=len(draft.amendments) + 1,
            criterion_id=target.id,
            span_id=span_id,
            field=field,
            previous_value=encode_amendment_value(field, previous),
            new_value=encode_amendment_value(field, new_value),
            source_text=user_message,
            source_sha256=hashlib.sha256(user_message.encode("utf-8")).hexdigest(),
        )
    except ValidationError:
        return None
    others_must = [item for item in draft.must_have if item.id != target.id]
    others_pref = [item for item in draft.preferred if item.id != target.id]
    if field == SemanticCriterionAmendmentField.CRITERION_TYPE:
        to_must = replacement.type == CriterionType.MUST_HAVE
        must_have = [*others_must, replacement] if to_must else others_must
        preferred = others_pref if to_must else [*others_pref, replacement]
    else:
        must_have = [replacement if item.id == target.id else item for item in draft.must_have]
        preferred = [replacement if item.id == target.id else item for item in draft.preferred]
    return draft.model_copy(
        update={
            "draft_id": uuid.uuid4(),
            "must_have": must_have,
            "preferred": preferred,
            "requirements": (
                _modified_requirement_results(
                    draft, criterion_id=target.id, criterion_type=replacement.type
                )
                if field == SemanticCriterionAmendmentField.CRITERION_TYPE
                else list(draft.requirements)
            ),
            "modification_source_text": user_message,
            "amendments": [*draft.amendments, amendment],
        }
    )


def _configured_provenance(llm: LLMProvider) -> LLMResultProvenance:
    return LLMResultProvenance(
        provider=llm.provider_name, model_name=llm.model_name, model_revision=llm.model_revision
    )


def _build_result(
    *,
    outcome: AgentTurnOutcome,
    message: str | None,
    tool_results: list[AgentToolResult],
    tool_call_count: int,
    provenance: LLMResultProvenance,
) -> AgentTurnResult:
    return AgentTurnResult(
        outcome=outcome,
        message=message,
        tool_results=tool_results,
        tool_call_count=tool_call_count,
        agent_policy_version=AGENT_POLICY_VERSION,
        prompt_version=AGENT_PROMPT_VERSION,
        model_provider=provenance.provider,
        model_name=provenance.model_name,
        model_revision=provenance.model_revision,
    )


@dataclass(frozen=True)
class AgentTurnCommit:
    """The complete, not-yet-persisted outcome of one orchestration turn
    (issue #85). Built from snapshots only; applied to the live ORM rows by
    ``apply_agent_turn_commit`` — in the router, only after Phase B has
    re-locked and revalidated every piece of authority."""

    result: AgentTurnResult
    user_turn: dict
    assistant_turn: dict
    active_result_set_id: uuid.UUID | None
    active_pending_draft_id: uuid.UUID | None
    # Issue #88 slice A: staged lane-A/lane-B task + clarification changes,
    # written only by Phase B (meyar.services.agent_task_repo).
    dialogue: DialogueCommit = DialogueCommit()


@dataclass(frozen=True)
class ClarificationButton:
    """The two closed-choice form fields as posted. Untrusted: they must
    equal the live pointer and an allowed value or the request is rejected
    (A2) — never reinterpreted as text."""

    clarification_id: uuid.UUID | None
    choice: str | None


@dataclass
class _TurnStage:
    """Per-turn staging of server-issued transcript ids and dialogue state."""

    assistant_turn_id: uuid.UUID
    transition: ClarificationTransition | None = None
    new_clarification: NewClarification | None = None
    clarification_payload: dict | None = None
    draft_amendment: bool = False


def _resolved_task_status(result: AgentTurnResult) -> TaskStatus:
    """T2/T3/T4: the resumed capability's own truthful outcome decides."""
    if any(item.job_draft is not None for item in result.tool_results):
        return TaskStatus.WAITING_CONFIRMATION
    if any(
        item.search is not None and item.search.response.plan.executable
        for item in result.tool_results
    ):
        return TaskStatus.COMPLETED
    return TaskStatus.FAILED_SAFE


def _finish_turn(
    session_context: TurnSessionState,
    *,
    user_turn: dict,
    result: AgentTurnResult,
    stage: _TurnStage | None = None,
) -> AgentTurnCommit:
    """Builds this turn's own (outcome, message) transcript entry as a first pass — the
    router's own sync_last_turn_display_text (D-045) overwrites ``text``
    with the actual rendered headline once one is available, right after
    this call returns. This first pass alone still guarantees a past turn
    is never blank even when ``result.message`` is None: redisplaying it
    later always falls back through the same deterministic outcome->text
    mapping the live turn uses (meyar.ui.presentation.
    agent_turn_outcome_message) — see D-036. D-045 exists because that
    fallback text is not always what the live turn actually showed (a
    tool-result turn's live headline is a richer, separately computed
    sentence — meyar.ui.service._agent_turn_headline); this call cannot
    compute that richer headline itself, since it runs before the
    router's post-tenant-lookup view-building step."""
    assistant_turn: dict[str, object] = {
        "role": "assistant",
        "text": result.message or "",
        "outcome": result.outcome.value,
        "text_authority": ASSISTANT_TEXT_AUTHORITY_SERVER,
        "text_authority_version": ASSISTANT_TEXT_AUTHORITY_VERSION,
    }
    if stage is not None:
        # Issue #88 (D-092 §4.1): stable server-issued id for every NEW entry.
        assistant_turn["turn_id"] = str(stage.assistant_turn_id)
        if stage.clarification_payload is not None:
            assistant_turn["clarification"] = stage.clarification_payload
    lane_b = LaneBChange.NONE
    pending_drafts = [
        tool_result.job_draft
        for tool_result in result.tool_results
        if tool_result.job_draft is not None
    ]
    if pending_drafts:
        # issue #80: the transcript only holds the server-side payload; the
        # live pending AUTHORITY is the session context pointer below. A
        # modified draft (new draft_id) moves the pointer, so the old id
        # can never be confirmed again, and a relogin/new BrowserSession
        # context starts with no pointer at all. Browser fields never
        # recreate either.
        latest_draft = pending_drafts[-1]
        assistant_turn["pending_job_draft"] = pending_draft_payload(latest_draft)
        session_context.active_pending_draft_id = latest_draft.draft_id
        lane_b = (
            LaneBChange.AMENDED
            if stage is not None and stage.draft_amendment
            else LaneBChange.NEW_DRAFT
        )
    transition = stage.transition if stage is not None else None
    if (
        transition is not None
        and transition.status == ClarificationStatus.RESOLVED
        and transition.task_status is None
    ):
        transition = replace(transition, task_status=_resolved_task_status(result))
    return AgentTurnCommit(
        result=result,
        user_turn=user_turn,
        assistant_turn=assistant_turn,
        active_result_set_id=session_context.active_result_set_id,
        active_pending_draft_id=session_context.active_pending_draft_id,
        dialogue=DialogueCommit(
            transition=transition,
            new_clarification=stage.new_clarification if stage is not None else None,
            lane_b=lane_b,
        ),
    )


async def apply_agent_turn_commit(
    db: AsyncSession,
    conversation: AgentConversation,
    session_context: AgentConversationSessionContext,
    *,
    tenant_id: uuid.UUID,
    commit: AgentTurnCommit,
    submission_id: uuid.UUID | None = None,
    locked_dialogue: LockedDialogue | None = None,
    clarification_ttl_seconds: int = 1800,
) -> AgentTurnResult:
    """Persist one turn's outcome onto the LOCKED, (re)validated live rows:
    append the (user, assistant) pair to the CURRENT transcript, set this
    BrowserSession's live ResultSet/pending-draft pointers, apply the staged
    task/clarification changes (issue #88), apply the title transition, and
    audit completion. The caller commits.

    ``locked_dialogue`` comes from the router's Phase B (rows locked before
    the submission row). Service-level callers without one lock here; such
    callers have no #87 submission, so a fresh opaque id is used as the
    unique creation provenance."""
    if (
        session_context.conversation_id != conversation.id
        or session_context.tenant_id != tenant_id
        or conversation.tenant_id != tenant_id
    ):
        raise ValueError("Session context does not belong to this conversation.")
    dialogue = commit.dialogue
    dialogue_changes = dialogue.touches_lane_a or dialogue.lane_b != LaneBChange.NONE
    if dialogue_changes and locked_dialogue is None:
        # Before the transcript append: liveness is re-verified against the
        # transcript the turn was resolved on (§12.2 step 2).
        locked_dialogue = await lock_dialogue_rows(
            db, conversation=conversation, session_context=session_context, dialogue=dialogue
        )
    # Durable storage bound (MAX_PERSISTED_AGENT_TURNS) — deliberately NOT
    # the model context window; see save_conversation_turns.
    await save_conversation_turns(
        db,
        conversation,
        turns=[*conversation.turns, commit.user_turn, commit.assistant_turn],
    )
    session_context.active_result_set_id = commit.active_result_set_id
    session_context.active_pending_draft_id = commit.active_pending_draft_id
    if dialogue_changes:
        assert locked_dialogue is not None
        await apply_dialogue_commit(
            db, conversation=conversation, session_context=session_context,
            locked=locked_dialogue, dialogue=dialogue,
            submission_id=submission_id or uuid.uuid4(),
            clarification_ttl_seconds=clarification_ttl_seconds,
        )
    apply_title_kind_transition(conversation, commit.result)
    await db.flush()
    await record_event(
        db,
        tenant_id=tenant_id,
        event_type="agent.turn.completed",
        metadata={
            "outcome": commit.result.outcome.value,
            "tool_call_count": commit.result.tool_call_count,
        },
    )
    return commit.result


async def run_agent_turn(
    db: AsyncSession,
    llm: LLMProvider,
    *,
    tenant_id: uuid.UUID,
    conversation: AgentConversation,
    session_context: AgentConversationSessionContext,
    user_message: str,
    as_of_date: date,
    embedding_config: EmbeddingSearchConfig,
    embedding_provider: EmbeddingProvider | None,
    max_tool_calls: int,
    max_context_turns: int,
    principal_scopes: frozenset[str] = DEFAULT_AGENT_TURN_SCOPES,
) -> AgentTurnResult:
    """Single-transaction form: the caller already holds the durable
    conversation row lock for the whole turn and commits/rolls back. Used by
    service-level callers/tests. The HTTP route instead uses
    ``execute_agent_turn`` inside meyar.agent.turn_boundary's phased
    lifecycle so no DB connection is held during local inference (#85)."""
    commit = await execute_agent_turn(
        db,
        llm,
        tenant_id=tenant_id,
        conversation=ConversationSnapshot.of(conversation),
        session_context=TurnSessionState.of(session_context),
        user_message=user_message,
        as_of_date=as_of_date,
        embedding_config=embedding_config,
        embedding_provider=embedding_provider,
        max_tool_calls=max_tool_calls,
        max_context_turns=max_context_turns,
        principal_scopes=principal_scopes,
    )
    return await apply_agent_turn_commit(
        db, conversation, session_context, tenant_id=tenant_id, commit=commit
    )


@dataclass(frozen=True)
class _Resume:
    """A resolved clarification resumes ONE server-built capability over the
    server-owned source text — never the answer text (§6.6)."""

    answer: ClarificationAnswer
    source_text: str


@dataclass(frozen=True)
class _DialogueOutcome:
    result: AgentTurnResult | None = None
    resume: _Resume | None = None


async def _classify_clarification_answer(
    llm: LLMProvider, clarification_type: ClarificationType, answer_text: str
) -> ClarificationProposalValue:
    """§6.4 step 4. Only the answer text, the type and the closed allowed
    codes reach the model. Any infrastructure or contract failure after the
    one repair is ``ClarificationClassifierError`` — never UNCLEAR (A2).
    A busy admission gate propagates as AgentInferenceBusyError (#85)."""
    allowed = [answer.value for answer in ALLOWED_ANSWERS[clarification_type]]
    for attempt in range(1, MAX_DECISION_ATTEMPTS + 1):
        try:
            proposal, _provenance = await llm.resolve_clarification_answer(
                clarification_type=clarification_type.value,
                allowed_answers=allowed,
                answer_text=answer_text,
                repair=attempt > 1,
            )
        except ModelSchemaInvalidError:
            continue
        except LLMProviderError:
            raise ClarificationClassifierError() from None
        value = proposal.value
        if value in (ClarificationProposalValue.NEW_REQUEST, ClarificationProposalValue.UNCLEAR):
            return value
        if value.value in allowed:
            return value
        raise ClarificationClassifierError()
    raise ClarificationClassifierError()


def _fixed_clarification_result(
    llm: LLMProvider, message: str
) -> AgentTurnResult:
    return _build_result(
        outcome=AgentTurnOutcome.CLARIFICATION_REQUESTED,
        message=message,
        tool_results=[],
        tool_call_count=0,
        provenance=_configured_provenance(llm),
    )


async def _resolve_dialogue_state(
    db: AsyncSession,
    llm: LLMProvider,
    *,
    tenant_id: uuid.UUID,
    conversation: ConversationSnapshot,
    session_context: TurnSessionState,
    message: str,
    button: ClarificationButton | None,
    user_turn_id: uuid.UUID,
    stage: _TurnStage,
) -> _DialogueOutcome:
    """§6.4 in its fixed order with a live pointer (or a posted button).

    Stages exactly one lane-A transition; writes nothing (Phase B does).
    Raises ``ClarificationRejectedError`` for a stale/foreign/mismatched
    button and ``ClarificationClassifierError`` for a classifier failure —
    both abandon the turn with no transition and nothing appended (A2)."""
    pointer = session_context.active_clarification_id
    live = None
    if pointer is not None and session_context.context_id is not None:
        live = await read_live_clarification(
            db, tenant_id=tenant_id, context_id=session_context.context_id,
            clarification_id=pointer,
        )
    choice: ClarificationAnswer | None = None
    if button is not None:
        # Foreign, cross-tenant, stale and missing ids are indistinguishable.
        if button.clarification_id is None or pointer is None or live is None or (
            button.clarification_id != pointer
        ):
            raise ClarificationRejectedError(RejectionReason.NOT_ACTIVE)
        allowed = ALLOWED_ANSWERS[ClarificationType(live.current.clarification_type)]
        if button.choice not in {answer.value for answer in allowed}:
            raise ClarificationRejectedError(RejectionReason.INVALID_CHOICE)
        choice = ClarificationAnswer(button.choice)
    if pointer is None:
        return _DialogueOutcome()
    stale_result = _fixed_clarification_result(llm, CLARIFICATION_STALE_COPY)
    if live is None:
        stage.transition = ClarificationTransition(
            clarification_id=pointer, task_id=None, status=ClarificationStatus.EXPIRED,
            task_status=None, requires_live=False, expiry_reason=ExpiryReason.STALE,
        )
        return _DialogueOutcome(result=stale_result)
    current = live.current
    failure = (
        ExpiryReason.STALE
        if live.predecessor_ambiguous
        else liveness_failure(
            conversation.turns, current, live.predecessor, now=datetime.now(UTC),
            context_id=session_context.context_id,  # type: ignore[arg-type]
            context_epoch=session_context.context_epoch,
        )
    )
    if failure is not None:
        # T8 (§6.2 / scenario D): expire, fixed "resend" copy, no execution.
        stage.transition = ClarificationTransition(
            clarification_id=current.id, task_id=current.task_id,
            status=ClarificationStatus.EXPIRED, task_status=TaskStatus.EXPIRED,
            requires_live=False, expiry_reason=failure,
        )
        return _DialogueOutcome(result=stale_result)
    clarification_type = ClarificationType(current.clarification_type)

    def resolve(
        answer: ClarificationAnswer, source: ResolutionSource, source_text: str
    ) -> _DialogueOutcome:
        stage.transition = ClarificationTransition(
            clarification_id=current.id, task_id=current.task_id,
            status=ClarificationStatus.RESOLVED, task_status=None, requires_live=True,
            resolved_value=answer, resolution_source=source, task_type=TaskType(answer.value),
        )
        return _DialogueOutcome(resume=_Resume(answer=answer, source_text=source_text))

    def new_task() -> _DialogueOutcome:
        # T7: supersede; the message is then handled as a normal new turn.
        stage.transition = ClarificationTransition(
            clarification_id=current.id, task_id=current.task_id,
            status=ClarificationStatus.SUPERSEDED, task_status=TaskStatus.CANCELLED,
            requires_live=True, superseded_reason=SupersededReason.NEW_TASK,
        )
        return _DialogueOutcome()

    def unclear() -> _DialogueOutcome:
        if current.attempt >= MAX_CLARIFICATION_ATTEMPTS:
            # T6: no attempt 3, no capability.
            stage.transition = ClarificationTransition(
                clarification_id=current.id, task_id=current.task_id,
                status=ClarificationStatus.EXPIRED, task_status=TaskStatus.FAILED_SAFE,
                requires_live=True, expiry_reason=ExpiryReason.ATTEMPTS,
            )
            return _DialogueOutcome(
                result=_fixed_clarification_result(llm, CLARIFICATION_ATTEMPTS_EXHAUSTED_COPY)
            )
        # T5: same task, same original source binding, attempt + 1 (A2 chain).
        stage.transition = ClarificationTransition(
            clarification_id=current.id, task_id=current.task_id,
            status=ClarificationStatus.SUPERSEDED, task_status=None, requires_live=True,
            superseded_reason=SupersededReason.UNCLEAR,
        )
        stage.new_clarification = retry_clarification(
            current, user_turn_id=user_turn_id, question_turn_id=stage.assistant_turn_id
        )
        stage.clarification_payload = clarification_payload(clarification_type)
        return _DialogueOutcome(
            result=_fixed_clarification_result(
                llm,
                AMBIGUOUS_SEARCH_OR_JOB_COPY
                if clarification_type == ClarificationType.SEARCH_OR_VACANCY
                else JOB_SOURCE_REQUIRED_COPY,
            )
        )

    if clarification_type == ClarificationType.SEARCH_OR_VACANCY:
        source_text = bound_source_text(conversation.turns, current)
        if choice is not None:
            return resolve(choice, ResolutionSource.BUTTON, source_text)
        label = match_answer_label(message)
        if label is not None:
            return resolve(label, ResolutionSource.LABEL, source_text)
        if is_clear_new_task(message, route_agent_entry(message)):
            return new_task()
        proposal = await _classify_clarification_answer(llm, clarification_type, message)
        if proposal == ClarificationProposalValue.NEW_REQUEST:
            return new_task()
        if proposal == ClarificationProposalValue.UNCLEAR:
            return unclear()
        return resolve(ClarificationAnswer(proposal.value), ResolutionSource.MODEL, source_text)
    # VACANCY_SOURCE_REQUIRED (A2): deterministic only, no model call.
    slot, slot_source = classify_vacancy_source_message(message, route_agent_entry(message))
    if slot == SourceSlotOutcome.SOURCE:
        assert slot_source is not None
        return resolve(
            ClarificationAnswer.VACANCY_ANALYSIS, ResolutionSource.SOURCE_MESSAGE, slot_source
        )
    if slot == SourceSlotOutcome.NEW_TASK:
        return new_task()
    return unclear()


_STATUS_BY_STRUCTURAL_FAILURE = {
    ResultSetResolutionFailure.EXPIRED: ResultSetStatus.EXPIRED,
    ResultSetResolutionFailure.UNSUPPORTED_SNAPSHOT_POLICY: ResultSetStatus.STALE,
}
_RESULT_SET_REJECTIONS = frozenset(
    {
        PlanRejectionCode.RESULT_CONTEXT_REQUIRED,
        PlanRejectionCode.RESULT_SET_STALE,
        PlanRejectionCode.RESULT_SET_EXPIRED,
        PlanRejectionCode.CANDIDATE_REF_OUT_OF_RANGE,
    }
)
# HUMAN_ACTION_ONLY affordances (D-092 §8.2): fixed server copy pointing at
# the EXISTING authenticated CSRF form — nothing executes in the turn.
_AFFORDANCE_COPY = {
    CapabilityName.CREATE_JOB: (
        "Vakansiya söhbətdə avtomatik yaradılmır. Yaratmaq üçün aktiv vakansiya "
        "qaralamasını nəzərdən keçirib onun təsdiq formasından istifadə edin."
    ),
    CapabilityName.RANK_JOB_CANDIDATES: (
        "Namizədlər söhbətdə avtomatik sıralanmır. Bu sessiyada təsdiqlənmiş vakansiyanın "
        "sıralama formasından istifadə edin."
    ),
}


class AgentPlanProviderError(RuntimeError):
    """The one ``propose_agent_plan`` call (or its repair) failed on
    infrastructure (timeout, unavailable, transport). #85: the turn is
    ABANDONED — no transcript, no plan execution, no pointer/task/
    clarification change, no consumed clarification attempt."""


def _assistant_context_text(turn: Mapping) -> str:
    """Assistant transcript text may name a candidate (D-045 stores the
    rendered headline), so the model projection carries only the closed
    outcome code — identity stays structurally absent (D-092 §15)."""
    outcome = turn.get("outcome")
    code = outcome if outcome in AgentTurnOutcome.__members__ else "UNKNOWN"
    return f"[assistant outcome: {code}]"


def build_plan_context(
    *,
    turns: list[dict],
    max_context_turns: int,
    offered: tuple[CapabilityName, ...],
    max_plan_steps: int,
    active_result_context_present: bool,
    available_ref_count: int,
    pending_vacancy_confirmation: bool,
) -> AgentPlanContext:
    """D-092 §15: the typed allow-list projection — the ENTIRE input of
    ``propose_agent_plan``. Only the last ``max_context_turns`` (role, text)
    pairs (never the durable transcript), closed capability names with
    server descriptions, presence flags and ordinal numbers. No id, identity,
    scope, token, audit data or date can be expressed by its types. Lane A
    is always absent or just superseded when a model plan is requested
    (§11.1), so ``waiting_clarification`` is null."""
    recent: list[ContextTurn] = []
    for turn in turns[-max_context_turns:] if max_context_turns > 0 else []:
        if turn.get("role") == "user":
            recent.append(ContextTurn(role="user", text=str(turn.get("text", ""))[:4000]))
        elif turn.get("role") == "assistant":
            recent.append(ContextTurn(role="assistant", text=_assistant_context_text(turn)))
    return AgentPlanContext(
        recent_turns=recent,
        available_capabilities=[
            OfferedCapability(name=name, description=CAPABILITY_REGISTRY[name].description)
            for name in offered
        ],
        max_plan_steps=max_plan_steps,
        active_result_context_present=active_result_context_present,
        available_candidate_refs=list(range(1, min(available_ref_count, MAX_CANDIDATE_REF) + 1)),
        waiting_clarification=None,
        pending_vacancy_confirmation=pending_vacancy_confirmation,
    )


async def _propose_agent_plan(
    llm: LLMProvider, context: AgentPlanContext
) -> tuple[AgentPlanProposal | None, LLMResultProvenance]:
    """Exactly ONE orchestration proposal per turn plus at most one repair
    (D-092 §10.3). ``None`` = still schema-invalid after the repair
    (MALFORMED_MODEL_OUTPUT, scenario H). Infrastructure failure raises
    ``AgentPlanProviderError``; a busy admission gate propagates as
    AgentInferenceBusyError (#85) — both abandon the turn."""
    for attempt in range(1, MAX_PLAN_PROPOSAL_ATTEMPTS + 1):
        try:
            return await llm.propose_agent_plan(context=context, repair=attempt > 1)
        except ModelSchemaInvalidError:
            continue
        except LLMProviderError:
            raise AgentPlanProviderError() from None
    return None, _configured_provenance(llm)


async def _build_validation_context(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    session_context: TurnSessionState,
    proposal: object,
    source_text: str,
    principal_scopes: frozenset[str],
    max_tool_calls: int,
) -> tuple[ValidationContext, ResultSetInspection]:
    """The Layer-1 ValidationContext from read-only, audit-free server
    authority. The pre-existing ResultSet is inspected for exactly the
    server-parsed ordinals / whole snapshot the plan consumes (#86)."""
    ordinals, whole_snapshot = pre_existing_result_set_requirements(
        proposal, source_text=source_text
    )
    inspection = await inspect_active_result_set(
        db,
        tenant_id=tenant_id,
        browser_session_id=session_context.browser_session_id,
        session_context=session_context,
        ordinals=ordinals,
        whole_snapshot=whole_snapshot,
    )
    if inspection.failure is None:
        result_set = ResultSetContext(
            ResultSetStatus.VALID,
            member_count=inspection.member_count,
            stale_ordinals=inspection.stale_ordinals,
            snapshot_stale=inspection.snapshot_stale,
        )
    else:
        result_set = ResultSetContext(
            _STATUS_BY_STRUCTURAL_FAILURE.get(inspection.failure, ResultSetStatus.NONE)
        )
    return (
        ValidationContext(
            principal_scopes=principal_scopes,
            pre_existing_result_set=result_set,
            pending_draft_live=session_context.active_pending_draft_id is not None,
            # No session-confirmed-job lookup exists in the turn: a RANK
            # proposal is never offered and fails closed as
            # CONFIRMATION_REQUIRED (D-095).
            confirmed_job_in_session=False,
            max_tool_calls=max_tool_calls,
        ),
        inspection,
    )


async def _result_set_rejection_result(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    session_context: TurnSessionState,
    proposal: object,
    rejection: PlanRejection,
    inspection: ResultSetInspection,
    provenance: LLMResultProvenance,
) -> AgentTurnResult | None:
    """A Layer-1 ResultSet-family rejection keeps TODAY's truthful outward
    behaviour — the same outcome, message, not-found card and
    ``agent.result_set.reference_rejected``/``refine_rejected`` audit the
    executor's own check produced — while ZERO executors run. The ordinal is
    the server-parsed one carried by the rejection. ``None`` for any other
    rejection."""
    if rejection.code not in _RESULT_SET_REJECTIONS or rejection.step_index is None:
        return None
    assert isinstance(proposal, Mapping)
    raw_step = proposal["steps"][rejection.step_index]
    capability = CapabilityName(raw_step["capability"])
    if rejection.code == PlanRejectionCode.CANDIDATE_REF_OUT_OF_RANGE:
        failure = ResultSetResolutionFailure.ORDINAL_OUT_OF_RANGE
    elif inspection.failure is not None:
        failure = inspection.failure
    else:
        failure = ResultSetResolutionFailure.STALE
    if capability == CapabilityName.REFINE_RESULTS:
        await record_refinement_rejection(
            db, tenant_id=tenant_id, session_context=session_context, failure=failure
        )
        outcome, message = _outcome_and_message_for_refinement_failure(failure)
        return _build_result(
            outcome=outcome, message=message, tool_results=[], tool_call_count=0,
            provenance=provenance,
        )
    candidate_ref = rejection.candidate_ref
    assert candidate_ref is not None
    await record_reference_rejection(
        db, tenant_id=tenant_id, session_context=session_context, failure=failure,
        candidate_ref=candidate_ref,
    )
    card = (
        AgentToolResult(
            tool_name=AgentActionType.GET_CANDIDATE_PROFILE,
            profile=AgentProfileToolResult(candidate_ref=candidate_ref, found=False),
        )
        if capability == CapabilityName.GET_CANDIDATE_PROFILE
        else AgentToolResult(
            tool_name=AgentActionType.GET_CANDIDATE_EVIDENCE,
            evidence=AgentEvidenceToolResult(candidate_ref=candidate_ref, found=False),
        )
    )
    return _build_result(
        outcome=_outcome_for_resolution_failure(failure),
        message=None,
        tool_results=[card],
        tool_call_count=0,
        provenance=provenance,
    )


def _vacancy_proposal_clarification(
    llm: LLMProvider,
    *,
    session_context: TurnSessionState,
    user_message: str,
    user_turn_id: uuid.UUID,
    stage: _TurnStage,
) -> AgentTurnResult:
    """D-092 §6.1 / #79: a MODEL vacancy proposal (an ANALYZE_VACANCY step or
    CLARIFY(SEARCH_OR_VACANCY)) never drafts. It becomes the server-typed,
    resumable SEARCH_OR_VACANCY clarification when the text is
    requirement-shaped; otherwise fixed NEED_MORE_DETAIL copy."""
    if session_context.context_id is not None and eligible_search_or_vacancy_source(
        user_message
    ):
        stage.new_clarification = search_or_vacancy_clarification(
            message=user_message, user_turn_id=user_turn_id,
            question_turn_id=stage.assistant_turn_id,
        )
        stage.clarification_payload = clarification_payload(
            stage.new_clarification.clarification_type
        )
        return _fixed_clarification_result(llm, AMBIGUOUS_SEARCH_OR_JOB_COPY)
    return _fixed_clarification_result(
        llm, _agent_response_text(AgentResponseCode.NEED_MORE_DETAIL)
    )


async def _plan_rejection_result(
    db: AsyncSession,
    llm: LLMProvider,
    *,
    tenant_id: uuid.UUID,
    session_context: TurnSessionState,
    proposal: object,
    rejection: PlanRejection,
    inspection: ResultSetInspection,
    user_message: str,
    user_turn_id: uuid.UUID,
    stage: _TurnStage,
    provenance: LLMResultProvenance,
) -> AgentTurnResult:
    """Layer 1 rejected the whole plan: ZERO executors ran. HR sees fixed
    copy per code family — never the code, a capability name or an id."""
    result_set_rejection = await _result_set_rejection_result(
        db, tenant_id=tenant_id, session_context=session_context, proposal=proposal,
        rejection=rejection, inspection=inspection, provenance=provenance,
    )
    if result_set_rejection is not None:
        return result_set_rejection
    if rejection.code == PlanRejectionCode.NOT_MODEL_PROPOSABLE:
        # The deterministic entry route is the only authority that can reach
        # drafting (#79).
        await record_event(
            db,
            tenant_id=tenant_id,
            event_type="agent.entry.action_rejected",
            metadata={
                "routing_source": "MODEL",
                "routed_action": AgentRoutedAction.CLARIFY.value,
                "routing_policy_version": ENTRY_ROUTING_POLICY_VERSION,
            },
        )
        return _vacancy_proposal_clarification(
            llm, session_context=session_context, user_message=user_message,
            user_turn_id=user_turn_id, stage=stage,
        )
    if rejection.code == PlanRejectionCode.REFERENCE_NOT_GROUNDED:
        # §10.2 rule 5: the existing "which candidate?" / result-context copy.
        code = (
            AgentResponseCode.CANDIDATE_REFERENCE_REQUIRED
            if rejection.grounded_field == GroundedField.REFERENCE
            else AgentResponseCode.RESULT_CONTEXT_REQUIRED
        )
        message = _agent_response_text(code)
    else:
        message = PLAN_REJECTED_COPY
    return _build_result(
        outcome=AgentTurnOutcome.CLARIFICATION_REQUESTED,
        message=message,
        tool_results=[],
        tool_call_count=0,
        provenance=provenance,
    )


async def _plan_execution_result(
    db: AsyncSession,
    llm: LLMProvider,
    *,
    tenant_id: uuid.UUID,
    plan: ExecutablePlan,
    execution: PlanExecution,
    user_message: str,
    provenance: LLMResultProvenance,
) -> AgentTurnResult:
    """Deterministic rendering of an executed plan. There is NO post-tool
    "what next" model call: the plan was fixed before step 1 (D-092 §5).
    Per-step audit keeps today's semantics: search/profile/evidence always
    audit ``agent.tool.executed`` once run; a refinement only on success; a
    vacancy draft ``agent.tool.executed`` or ``agent.tool.failed``."""
    tool_results: list[AgentToolResult] = []
    tool_calls_made = 0
    for step, outcome in zip(plan.steps, execution.outcomes, strict=False):
        definition = CAPABILITY_REGISTRY[step.capability]
        action = definition.legacy_tool_name
        assert action is not None
        audit_capability = capability_audit_metadata(definition)
        if step.capability == CapabilityName.ANALYZE_VACANCY and outcome.tool_result is None:
            await record_event(
                db, tenant_id=tenant_id, event_type="agent.tool.failed",
                metadata={"tool_name": action.value, **audit_capability},
            )
            continue
        if step.capability == CapabilityName.REFINE_RESULTS and not outcome.succeeded:
            continue
        assert outcome.tool_result is not None
        tool_calls_made += 1
        tool_results.append(outcome.tool_result)
        metadata: dict[str, object] = {
            "tool_name": action.value,
            "tool_call_index": tool_calls_made,
            **audit_capability,
        }
        if outcome.tool_result.job_draft is not None:
            metadata.update(jd_draft_audit_metadata(outcome.tool_result.job_draft))
        await record_event(
            db, tenant_id=tenant_id, event_type="agent.tool.executed", metadata=metadata
        )

    if execution.status == PlanStatus.INCOMPLETE:
        return plan_incomplete_result(llm, execution)

    last = execution.outcomes[-1]
    if execution.status == PlanStatus.STEP_FAILED:
        # Single-step plan: the step's own truthful outcome, exactly as today.
        if last.capability == CapabilityName.ANALYZE_VACANCY:
            return _build_result(
                outcome=AgentTurnOutcome.JOB_DRAFT_FAILED, message=None, tool_results=[],
                tool_call_count=0, provenance=provenance,
            )
        if last.capability == CapabilityName.REFINE_RESULTS:
            # A rejection means "the active result set is untouched".
            if last.rejection_message is not None:
                return _build_result(
                    outcome=AgentTurnOutcome.CLARIFICATION_REQUESTED,
                    message=last.rejection_message, tool_results=[], tool_call_count=0,
                    provenance=provenance,
                )
            failure_outcome, failure_message = _outcome_and_message_for_refinement_failure(
                last.resolution_failure  # type: ignore[arg-type]
            )
            return _build_result(
                outcome=failure_outcome, message=failure_message, tool_results=[],
                tool_call_count=0, provenance=provenance,
            )
        if last.capability in (
            CapabilityName.GET_CANDIDATE_PROFILE,
            CapabilityName.GET_CANDIDATE_EVIDENCE,
        ):
            return _build_result(
                outcome=_outcome_for_resolution_failure(last.resolution_failure),
                message=None, tool_results=tool_results, tool_call_count=tool_calls_made,
                provenance=provenance,
            )
        # SEARCH_CANDIDATES: a truthful non-executable planner result is the
        # whole answer, as today.
        return _build_result(
            outcome=AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT, message=None,
            tool_results=tool_results, tool_call_count=tool_calls_made, provenance=provenance,
        )

    # COMPLETED. A found profile/evidence as the final step gets one bounded
    # D-038 grounded synthesis over that candidate's own accepted facts (a
    # rendering aid, not an orchestration decision); None falls back to the
    # deterministic message (D-036).
    synthesized_message: str | None = None
    if (
        last.capability
        in (CapabilityName.GET_CANDIDATE_PROFILE, CapabilityName.GET_CANDIDATE_EVIDENCE)
        and last.matched_profile is not None
    ):
        synthesized_message = await _synthesize_grounded_answer(
            llm, question=user_message, facts=_build_profile_facts(last.matched_profile)
        )
    return _build_result(
        outcome=AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT,
        message=synthesized_message,
        tool_results=tool_results,
        tool_call_count=tool_calls_made,
        provenance=provenance,
    )


async def execute_agent_turn(
    db: AsyncSession,
    llm: LLMProvider,
    *,
    tenant_id: uuid.UUID,
    conversation: ConversationSnapshot,
    session_context: TurnSessionState,
    user_message: str,
    as_of_date: date,
    embedding_config: EmbeddingSearchConfig,
    embedding_provider: EmbeddingProvider | None,
    max_tool_calls: int,
    max_context_turns: int,
    clarification_button: ClarificationButton | None = None,
    principal_scopes: frozenset[str] = DEFAULT_AGENT_TURN_SCOPES,
) -> AgentTurnCommit:
    """One bounded orchestration turn over SNAPSHOTS (issue #85). Never
    persists a mutation to any candidate/job/evaluation row, and never
    writes the transcript or live pointers itself: it returns an
    ``AgentTurnCommit`` for ``apply_agent_turn_commit``. Its own DB writes
    are append-only audit events and new server-owned AgentResultSet rows,
    which are inert until a committed live pointer references them.
    ``browser_session_id``/``context_epoch``/``active_result_set_id``/
    ``active_pending_draft_id`` come ONLY from ``session_context`` — never
    inferred from the transcript (issue #80).

    The high-level JD/search boundary is decided here from the raw message by
    ``route_agent_entry``.  No form field or model proposal can authorize JD
    drafting: confirmed JDs bypass the orchestration model, ambiguous source
    text receives fixed clarification, and an unexpected model-proposed
    DRAFT_JOB_CRITERIA action fails closed.  An explicit new candidate search
    also bypasses the orchestration decision: the server builds the typed
    SEARCH_CANDIDATES action with the user's own text, the existing validated
    planner/search path runs, and the validated tool result ends the turn."""
    if (
        session_context.conversation_id != conversation.id
        or session_context.tenant_id != tenant_id
        or conversation.tenant_id != tenant_id
    ):
        # issue #80: the live context must belong to exactly this durable
        # conversation and tenant — never mix one conversation's transcript
        # with another's ResultSet/pending-draft authority.
        raise ValueError("Session context does not belong to this conversation.")
    # Issue #84: canonical source text for everything below (transcript,
    # routing, spans/offsets, hashing, provenance, the local model).
    user_message = normalize_message_newlines(user_message)
    # Issue #88 (D-092 §4.1): stable server-issued ids for the two NEW
    # transcript entries; never model- or client-authored.
    user_turn_id = uuid.uuid4()
    stage = _TurnStage(assistant_turn_id=uuid.uuid4())
    user_turn: dict = {"role": "user", "text": user_message, "turn_id": str(user_turn_id)}
    turns: list[dict] = [*conversation.turns, user_turn]

    def finish(result: AgentTurnResult) -> AgentTurnCommit:
        return _finish_turn(session_context, user_turn=user_turn, result=result, stage=stage)

    # [1] Dialogue-state resolution (§6.4, lane A) runs FIRST — before the
    # lane-B pending-draft amendment branch (§4.4 rule 5).
    resume: _Resume | None = None
    if clarification_button is not None or session_context.active_clarification_id is not None:
        dialogue_outcome = await _resolve_dialogue_state(
            db, llm, tenant_id=tenant_id, conversation=conversation,
            session_context=session_context, message=user_message,
            button=clarification_button, user_turn_id=user_turn_id, stage=stage,
        )
        if dialogue_outcome.result is not None:
            return finish(dialogue_outcome.result)
        resume = dialogue_outcome.resume
    # Live pending authority only (session_context.active_pending_draft_id);
    # historical transcript payloads alone are never actionable.
    pending_draft = get_active_pending_job_draft(conversation, session_context)
    if (
        resume is None
        and pending_draft is not None
        and _FOLLOWUP_RE.search(_fold(user_message))
    ):
        modified = _apply_pending_draft_followup(pending_draft, user_message)
        stage.draft_amendment = modified is not None
        provenance = _configured_provenance(llm)
        if modified is None:
            result = _build_result(
                outcome=AgentTurnOutcome.CLARIFICATION_REQUESTED,
                message=(
                    "Qaralama dəyişikliyi təhlükəsiz şəkildə tətbiq edilmədi. Dəyişəcək "
                    "meyarı və əvvəlki/yeni dəyəri dəqiq yazın və ya vakansiyanı "
                    "yenidən analiz edin."
                ),
                tool_results=[],
                tool_call_count=0,
                provenance=provenance,
            )
        else:
            result = _build_result(
                outcome=AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT,
                message="Vakansiya qaralaması yeni, mənbəyə bağlı versiya kimi yeniləndi.",
                tool_results=[
                    AgentToolResult(
                        tool_name=AgentActionType.DRAFT_JOB_CRITERIA,
                        job_draft=modified,
                    )
                ],
                tool_call_count=0,
                provenance=provenance,
            )
        return finish(result)

    # Every capability invocation is a validated plan executed through the
    # registry (D-092 §5). A forced or resumed route yields a SERVER plan over
    # its server-owned source text; otherwise exactly ONE model plan proposal
    # is requested below.
    server_plan: tuple[dict, str] | None = None
    if resume is not None:
        # §6.6: the resolved answer maps to exactly one server-built step over
        # the server-owned source (bound U1 span, or the qualifying current
        # message for SOURCE_MESSAGE) — the same FORCE_* paths as today. The
        # answer words themselves are never search or JD input.
        if resume.answer == ClarificationAnswer.VACANCY_ANALYSIS:
            server_plan = (
                vacancy_plan(start=0, end=len(resume.source_text)),
                resume.source_text,
            )
        elif resume.answer == ClarificationAnswer.CANDIDATE_SEARCH:
            if len(resume.source_text) > MAX_AGENT_SEARCH_QUERY_LENGTH:
                # T4: a truthful capability failure (the forced search input
                # is bounded); the task fails safe and nothing executes.
                return finish(
                    _fixed_clarification_result(llm, INPUT_STRUCTURE_CLARIFICATION_COPY)
                )
            server_plan = (search_plan(), resume.source_text)
    else:
        entry_routing = route_agent_entry(user_message)
        await record_event(
            db,
            tenant_id=tenant_id,
            event_type="agent.entry.routed",
            metadata={
                "routing_source": entry_routing.routing_source.value,
                "routed_action": entry_routing.routed_action.value,
                "routing_policy_version": ENTRY_ROUTING_POLICY_VERSION,
            },
        )
        clarification_copy = {
            AgentEntryRoute.CLARIFY_AMBIGUOUS: AMBIGUOUS_SEARCH_OR_JOB_COPY,
            AgentEntryRoute.CLARIFY_JOB_SOURCE_REQUIRED: JOB_SOURCE_REQUIRED_COPY,
            AgentEntryRoute.CLARIFY_INPUT_STRUCTURE: INPUT_STRUCTURE_CLARIFICATION_COPY,
        }.get(entry_routing.route)
        if clarification_copy is not None:
            # §6.1: only the two server-typed clarifications become resumable
            # (T1); CLARIFY_INPUT_STRUCTURE stays non-resumable fixed copy.
            if session_context.context_id is not None:
                if (
                    entry_routing.route == AgentEntryRoute.CLARIFY_AMBIGUOUS
                    and eligible_search_or_vacancy_source(user_message)
                ):
                    stage.new_clarification = search_or_vacancy_clarification(
                        message=user_message, user_turn_id=user_turn_id,
                        question_turn_id=stage.assistant_turn_id,
                    )
                elif entry_routing.route == AgentEntryRoute.CLARIFY_JOB_SOURCE_REQUIRED:
                    stage.new_clarification = vacancy_source_clarification(
                        user_turn_id=user_turn_id, question_turn_id=stage.assistant_turn_id
                    )
                if stage.new_clarification is not None:
                    stage.clarification_payload = clarification_payload(
                        stage.new_clarification.clarification_type
                    )
            return finish(_fixed_clarification_result(llm, clarification_copy))

        if entry_routing.route == AgentEntryRoute.FORCE_JOB_DRAFT:
            # Confirmed JDs bypass the orchestration model entirely. The
            # exact user-owned source (offsets into user_message), never
            # rewritten; drafting stays source-bound and review-only.
            start = entry_routing.draft_source_start
            end = entry_routing.draft_source_end
            server_plan = (
                vacancy_plan(
                    start=0 if start is None or end is None else start,
                    end=len(user_message) if start is None or end is None else end,
                ),
                user_message,
            )
        elif entry_routing.route == AgentEntryRoute.FORCE_CANDIDATE_SEARCH:
            # Explicit new searches bypass the orchestration model: the
            # user's own text reaches the planner.
            server_plan = (search_plan(), user_message)
        elif (
            entry_routing.route == AgentEntryRoute.FORCE_RESULT_LIMIT
            and entry_routing.result_limit is not None
        ):
            # Count-only current-result follow-up ("ilk 3"): the server
            # supplies the typed limit; the existing #49 refinement dispatch
            # validates the active ResultSet and rejects truthfully.
            server_plan = (result_limit_plan(entry_routing.result_limit), user_message)

    provenance = _configured_provenance(llm)
    offered: frozenset[CapabilityName] | None = None
    if server_plan is not None:
        proposal, source_text = server_plan
        origin = PlanOrigin.SERVER
        schema_version = CAPABILITY_PLAN_SCHEMA_VERSION
    else:
        # [3] MODEL_ROUTED (D-092 §5): leave the DB, ONE local-model plan
        # proposal (+ at most one repair) over the typed allow-list
        # projection with the registry-derived per-call capability subset.
        offered_names = offered_capabilities(
            principal_scopes=principal_scopes,
            result_context_present=session_context.active_result_set_id is not None,
            pending_draft_live=session_context.active_pending_draft_id is not None,
            confirmed_job_in_session=False,
        )
        available_ref_count = await active_result_set_size(
            db,
            tenant_id=tenant_id,
            browser_session_id=session_context.browser_session_id,
            session_context=session_context,
        )
        plan_context = build_plan_context(
            turns=turns,
            max_context_turns=max_context_turns,
            offered=offered_names,
            max_plan_steps=max(1, min(MAX_PLAN_STEPS, max_tool_calls)),
            active_result_context_present=session_context.active_result_set_id is not None,
            available_ref_count=available_ref_count,
            pending_vacancy_confirmation=session_context.active_pending_draft_id is not None,
        )
        model_proposal, provenance = await _propose_agent_plan(llm, plan_context)
        if model_proposal is None:
            return finish(
                _build_result(
                    outcome=AgentTurnOutcome.MALFORMED_MODEL_OUTPUT, message=None,
                    tool_results=[], tool_call_count=0, provenance=provenance,
                )
            )
        if model_proposal.kind != PlanKind.PLAN:
            reply = validate_model_reply(model_proposal)
            if isinstance(reply, PlanRejection):
                await record_event(
                    db, tenant_id=tenant_id, event_type="agent.plan.rejected",
                    metadata={
                        "reason_code": reply.code.value,
                        "schema_version": AGENT_PLAN_SCHEMA_VERSION,
                    },
                )
                return finish(
                    _build_result(
                        outcome=AgentTurnOutcome.CLARIFICATION_REQUESTED,
                        message=PLAN_REJECTED_COPY, tool_results=[], tool_call_count=0,
                        provenance=provenance,
                    )
                )
            if reply.clarification_code == ModelClarificationCode.SEARCH_OR_VACANCY:
                return finish(
                    _vacancy_proposal_clarification(
                        llm, session_context=session_context, user_message=user_message,
                        user_turn_id=user_turn_id, stage=stage,
                    )
                )
            if reply.kind == PlanKind.CLARIFY:
                assert reply.clarification_code is not None
                return finish(
                    _build_result(
                        outcome=AgentTurnOutcome.CLARIFICATION_REQUESTED,
                        message=_agent_response_text(
                            AgentResponseCode(reply.clarification_code.value)
                        ),
                        tool_results=[], tool_call_count=0, provenance=provenance,
                    )
                )
            assert reply.response_code is not None
            return finish(
                _build_result(
                    outcome=AgentTurnOutcome.ANSWERED,
                    message=_agent_response_text(reply.response_code),
                    tool_results=[], tool_call_count=0, provenance=provenance,
                )
            )
        proposal = model_proposal.model_dump(mode="json")
        source_text = user_message
        origin = PlanOrigin.MODEL
        schema_version = AGENT_PLAN_SCHEMA_VERSION
        offered = frozenset(offered_names)

    # [4] Layer 1 on a ValidationContext freshly read (read-only, audit-free)
    # immediately before validation — including the pre-existing ResultSet,
    # inspected for exactly what this plan consumes (#86).
    validation_context, inspection = await _build_validation_context(
        db,
        tenant_id=tenant_id,
        session_context=session_context,
        proposal=proposal,
        source_text=source_text,
        principal_scopes=principal_scopes,
        max_tool_calls=max_tool_calls,
    )
    validated = validate_plan(
        proposal,
        origin=origin,
        source_text=source_text,
        ctx=validation_context,
        offered=offered,
    )
    if isinstance(validated, PlanRejection):
        # Whole-plan rejection: ZERO capabilities executed.
        await record_event(
            db,
            tenant_id=tenant_id,
            event_type="agent.plan.rejected",
            metadata={"reason_code": validated.code.value, "schema_version": schema_version},
        )
        return finish(
            await _plan_rejection_result(
                db, llm, tenant_id=tenant_id, session_context=session_context,
                proposal=proposal, rejection=validated, inspection=inspection,
                user_message=user_message, user_turn_id=user_turn_id, stage=stage,
                provenance=provenance,
            )
        )

    # D-092 §19.2 attempt provenance: closed keys only, no text/ids.
    await record_event(
        db,
        tenant_id=tenant_id,
        event_type="agent.plan.validated",
        metadata=plan_validated_metadata(validated),
    )
    if not validated.steps:
        # Only HUMAN_ACTION_ONLY affordances (§8.2): nothing executes; HR is
        # pointed at the existing authenticated CSRF form.
        affordance = validated.affordances[0]
        return finish(
            _build_result(
                outcome=AgentTurnOutcome.ANSWERED,
                message=_AFFORDANCE_COPY[affordance.capability],
                tool_results=[], tool_call_count=0, provenance=provenance,
            )
        )

    # [5] `for step in validated_plan.steps` (bounded by Layer 1): Layer 2 +
    # executor + atomic activation. No model re-decision between steps.
    execution = await execute_plan(
        validated,
        ExecutionContext(
            db=db,
            llm=llm,
            tenant_id=tenant_id,
            session_context=session_context,
            as_of_date=as_of_date,
            embedding_config=embedding_config,
            embedding_provider=embedding_provider,
        ),
    )
    return finish(
        await _plan_execution_result(
            db, llm, tenant_id=tenant_id, plan=validated, execution=execution,
            user_message=user_message, provenance=provenance,
        )
    )


def plan_incomplete_result(llm: LLMProvider, execution: PlanExecution) -> AgentTurnResult:
    """§11.3: a multi-step plan that did not complete. Fixed truthful copy
    (presentation), no active result cards, nothing activated — the caller
    leaves the previous live pointers exactly as they were."""
    assert execution.status == PlanStatus.INCOMPLETE
    return _build_result(
        outcome=AgentTurnOutcome.PLAN_INCOMPLETE,
        message=None,
        tool_results=[],
        tool_call_count=len(execution.outcomes),
        provenance=_configured_provenance(llm),
    )
