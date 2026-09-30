"""Slice 2 — bounded read-only agent orchestration loop (issue #31, D-035).

Human session -> local Ollama agent -> typed MEYAR tools -> existing
tenant-scoped search/profile/evidence services -> grounded response. The
LLM interprets/orchestrates; it never becomes scoring, authorization,
evidence, or persistence authority (see docs/DECISIONS.md D-030/D-031).

Every ``AgentDecision`` the model produces is untrusted input and passes
through the same discipline any other LLM-produced tool argument does:
typed schema validation (meyar.agent.schemas), tenant-scoped service calls
(never a client/model-supplied tenant id), and — for SEARCH_CANDIDATES —
the existing frozen NL search-planner pipeline's prohibited-attribute and
no-silent-weakening rules (D-027, D-031). A ``candidate_ref`` is never
trusted as a raw candidate_id: it is always resolved against this
conversation's OWN BrowserSession-bound live context
``active_result_set_id`` (issues #49/#80; see
``meyar.services.agent_result_set_repo.resolve_active_candidate_ref``), so
the model's own memory of what it was shown is never the authority for
which candidate a tool call touches."""

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import date

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
    MAX_CANDIDATE_REF,
    MAX_JD_REQUIREMENT_SPANS,
    AgentActionType,
    AgentDecision,
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
    is_non_professional_requirement,
)
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
    ResultSetResolutionFailure,
    active_result_set_size,
    create_result_set_from_refinement,
    create_result_set_from_search,
    resolve_active_candidate_ref,
    validate_active_result_set_for_refinement,
)
from meyar.services.audit_repo import record_event
from meyar.services.profile_authority import get_current_authorized_profile

MAX_DECISION_ATTEMPTS = 2
# Bounds how much of one candidate's profile a single GET_CANDIDATE_EVIDENCE
# call surfaces — a generous, but not unbounded, response even for a broad
# (topic-less) request. See _dispatch_evidence.
MAX_EVIDENCE_MATCHES = 30
# Bounded retry for DRAFT_JOB_CRITERIA's own drafting call, matching the
# D-038 grounded-synthesis precedent (_synthesize_grounded_answer).
MAX_JD_DRAFT_ATTEMPTS = 2


def _fold(text: str) -> str:
    return fold_az_ascii(normalize_azerbaijani_case(text))


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
    if failure == ResultSetResolutionFailure.STALE:
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
    if failure == ResultSetResolutionFailure.STALE:
        return AgentTurnOutcome.RESULT_SET_STALE, None
    if failure == ResultSetResolutionFailure.EXPIRED:
        return AgentTurnOutcome.RESULT_SET_EXPIRED, None
    return (
        AgentTurnOutcome.CLARIFICATION_REQUESTED,
        _agent_response_text(AgentResponseCode.RESULT_CONTEXT_REQUIRED),
    )


def _summarize_tool_result(result: AgentToolResult) -> dict:
    """Small, bounded, non-identity JSON fed back into the model's own
    next-step context — counts/flags only, never evidence quotes or
    profile facts, so the model cannot lift ungrounded text from here
    into a later ``message``."""
    if result.tool_name == AgentActionType.SEARCH_CANDIDATES:
        assert result.search is not None
        plan = result.search.response.plan
        response = result.search.response.search_response
        return {
            "tool": "SEARCH_CANDIDATES",
            "executable": plan.executable,
            "outcome": plan.outcome.value,
            "result_count": response.result_count if response else 0,
        }
    if result.tool_name == AgentActionType.GET_CANDIDATE_PROFILE:
        assert result.profile is not None
        return {
            "tool": "GET_CANDIDATE_PROFILE",
            "candidate_ref": result.profile.candidate_ref,
            "found": result.profile.found,
        }
    assert result.evidence is not None
    return {
        "tool": "GET_CANDIDATE_EVIDENCE",
        "candidate_ref": result.evidence.candidate_ref,
        "found": result.evidence.found,
        "match_count": len(result.evidence.matches),
    }


async def _dispatch_search(
    db: AsyncSession,
    llm: LLMProvider,
    *,
    tenant_id: uuid.UUID,
    session_context: AgentConversationSessionContext,
    previous_result_set_id: uuid.UUID | None,
    decision: AgentDecision,
    as_of_date: date,
    embedding_config: EmbeddingSearchConfig,
    embedding_provider: EmbeddingProvider | None,
) -> tuple[AgentToolResult, uuid.UUID | None]:
    """Forwards decision.search_query, unmodified, into the existing
    frozen NL search-planner pipeline (D-031) — this module never
    re-implements filter extraction, prohibited-attribute checks, or the
    no-silent-weakening rule; it only reuses them. On an executable
    search, persists a brand-new server-owned AgentResultSet (issue #49)
    and returns its id; a non-executable/failed search never clears a
    prior valid active_result_set_id — only a successful search replaces
    it (returns None to signal "keep the existing pointer")."""
    assert decision.search_query is not None
    planned = await plan_and_search_candidates(
        db,
        llm,
        tenant_id=tenant_id,
        natural_language_request=decision.search_query,
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
    decision: AgentDecision,
    session_context: AgentConversationSessionContext,
) -> tuple[AgentToolResult, CandidateProfileExtraction | None, ResultSetResolutionFailure | None]:
    """Returns (tool result, the raw validated profile when found, the
    resolution failure reason when not) — the profile is handed back
    separately so run_agent_turn can build grounded-answer facts (D-037/
    D-038) without a second, redundant DB fetch. candidate_ref resolution
    (issue #49) is entirely owned by
    meyar.services.agent_result_set_repo.resolve_active_candidate_ref —
    this function never resolves an ordinal itself."""
    assert decision.candidate_ref is not None
    resolved = await resolve_active_candidate_ref(
        db,
        tenant_id=tenant_id,
        browser_session_id=session_context.browser_session_id,
        session_context=session_context,
        candidate_ref=decision.candidate_ref,
    )
    if isinstance(resolved, ResultSetResolutionFailure):
        return (
            AgentToolResult(
                tool_name=AgentActionType.GET_CANDIDATE_PROFILE,
                profile=AgentProfileToolResult(candidate_ref=decision.candidate_ref, found=False),
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
                    candidate_ref=decision.candidate_ref,
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
                candidate_ref=decision.candidate_ref,
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
    # Set when decision.filter_query could not be safely interpreted/
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
    session_context: AgentConversationSessionContext,
    decision: AgentDecision,
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
    2. When decision.filter_query is set, it is forwarded UNMODIFIED into
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
       against decision.limit into one effective_limit: the planner's own
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
    if decision.filter_query is not None:
        plan = await plan_candidate_search(
            db,
            llm,
            tenant_id=tenant_id,
            natural_language_request=decision.filter_query,
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
            # candidate_ref ordinal itself is bounded to (see AgentDecision.
            # limit) — an HR-text count the search policy itself could
            # produce (up to MAX_SEARCH_LIMIT=100) but a refinement could
            # never honor fails closed here rather than crashing on the
            # narrower AgentRefineToolResult.requested_limit bound below.
            return RefineDispatchResult(
                rejection_message=_agent_response_text(AgentResponseCode.UNSUPPORTED_REQUEST)
            )

    if (
        planner_explicit_limit is not None
        and decision.limit is not None
        and decision.limit != planner_explicit_limit
    ):
        # The HR text's own explicit count (validated by the planner) and
        # the small orchestrator's own limit field disagree — the server
        # must never silently pick one interpretation over the other.
        return RefineDispatchResult(
            rejection_message=_agent_response_text(AgentResponseCode.UNSUPPORTED_REQUEST)
        )
    effective_limit = decision.limit if planner_explicit_limit is None else planner_explicit_limit

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
    decision: AgentDecision,
    session_context: AgentConversationSessionContext,
) -> tuple[AgentToolResult, CandidateProfileExtraction | None, ResultSetResolutionFailure | None]:
    """Returns (tool result, the raw validated profile when found, the
    resolution failure reason when not) — see _dispatch_profile's
    docstring; the same profile backs D-037/D-038 grounded-answer
    synthesis for both tools identically (never the raw evidence quote
    text, which stays server-rendered-only, never model input)."""
    assert decision.candidate_ref is not None
    resolved = await resolve_active_candidate_ref(
        db,
        tenant_id=tenant_id,
        browser_session_id=session_context.browser_session_id,
        session_context=session_context,
        candidate_ref=decision.candidate_ref,
    )
    if isinstance(resolved, ResultSetResolutionFailure):
        return (
            AgentToolResult(
                tool_name=AgentActionType.GET_CANDIDATE_EVIDENCE,
                evidence=AgentEvidenceToolResult(
                    candidate_ref=decision.candidate_ref,
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
                    candidate_ref=decision.candidate_ref,
                    candidate_id=candidate_id,
                    found=False,
                    profile_status=None,
                ),
            ),
            None,
            None,
        )
    version, profile = authorized

    topic_folded = _fold(decision.evidence_topic) if decision.evidence_topic else None
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
                candidate_ref=decision.candidate_ref,
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
    return _build_authorized_semantic_draft(
        jd_text=jd_text,
        title=draft.title if draft is not None else None,
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


async def _finish_turn(
    db: AsyncSession,
    conversation: AgentConversation,
    session_context: AgentConversationSessionContext,
    *,
    tenant_id: uuid.UUID,
    turns: list[dict],
    result: AgentTurnResult,
) -> AgentTurnResult:
    """Persists this turn's own (outcome, message) as a first pass — the
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
    # Durable storage bound (MAX_PERSISTED_AGENT_TURNS) — deliberately NOT
    # the model context window; see save_conversation_turns.
    await save_conversation_turns(db, conversation, turns=[*turns, assistant_turn])
    apply_title_kind_transition(conversation, result)
    await db.flush()
    await record_event(
        db,
        tenant_id=tenant_id,
        event_type="agent.turn.completed",
        metadata={
            "outcome": result.outcome.value,
            "tool_call_count": result.tool_call_count,
        },
    )
    return result


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
) -> AgentTurnResult:
    """One bounded orchestration turn. Never persists a mutation to any
    candidate/job/evaluation row — only this durable conversation's own
    transcript/title kind and its BrowserSession-bound live
    ``session_context`` (active_result_set_id, active_pending_draft_id).
    ``browser_session_id``/``context_epoch``/``active_result_set_id``/
    ``active_pending_draft_id`` come ONLY from ``session_context`` — never
    inferred from the transcript (issue #80). Caller holds the durable
    conversation row lock and is responsible for commit/rollback.

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
    turns: list[dict] = [*conversation.turns, {"role": "user", "text": user_message}]
    # Live pending authority only (session_context.active_pending_draft_id);
    # historical transcript payloads alone are never actionable.
    pending_draft = get_active_pending_job_draft(conversation, session_context)
    if (
        pending_draft is not None
        and _FOLLOWUP_RE.search(_fold(user_message))
    ):
        modified = _apply_pending_draft_followup(pending_draft, user_message)
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
        return await _finish_turn(
            db,
            conversation,
            session_context,
            tenant_id=tenant_id,
            turns=turns,
            result=result,
        )

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
        result = _build_result(
            outcome=AgentTurnOutcome.CLARIFICATION_REQUESTED,
            message=clarification_copy,
            tool_results=[],
            tool_call_count=0,
            provenance=_configured_provenance(llm),
        )
        return await _finish_turn(
            db,
            conversation,
            session_context,
            tenant_id=tenant_id,
            turns=turns,
            result=result,
        )

    server_authorized_draft = entry_routing.route == AgentEntryRoute.FORCE_JOB_DRAFT
    # Exact user-owned source (offsets into user_message), never rewritten.
    job_draft_source = entry_routing.draft_source(user_message)
    server_authorized_search = entry_routing.route == AgentEntryRoute.FORCE_CANDIDATE_SEARCH
    # Search forced by the entry router is turn-terminal once the existing
    # planner/search path returns: no second orchestration guess is needed.
    search_turn_terminal = server_authorized_search
    # Count-only current-result follow-up ("ilk 3"): the server supplies the
    # typed limit; the existing #49 refinement dispatch validates the active
    # ResultSet and rejects truthfully when there is none.
    server_result_limit = (
        entry_routing.result_limit
        if entry_routing.route == AgentEntryRoute.FORCE_RESULT_LIMIT
        else None
    )
    tool_results: list[AgentToolResult] = []
    last_tool_summary: dict | None = None
    tool_calls_made = 0
    provenance = _configured_provenance(llm)
    # Scoped to this one turn only (never persisted): a small local model
    # sometimes re-issues an identical SEARCH_CANDIDATES call instead of
    # recognizing the request is already answered by its own prior result —
    # found via real-Ollama Slice 2 acceptance testing (PR #40), where this
    # produced a contradictory-looking TOOL_CALL_LIMIT_EXCEEDED banner
    # stacked above several duplicated result blocks. Guarded structurally
    # rather than only by prompt instruction, matching this module's D-038
    # precedent.
    searched_queries: set[str] = set()

    while True:
        decision: AgentDecision | None
        draft_authorized_for_decision = server_authorized_draft
        if server_authorized_draft:
            # Confirmed JDs bypass the orchestration model entirely. Drafting
            # itself remains source-bound and review-only in
            # _dispatch_draft_job_criteria below.
            decision = AgentDecision(action=AgentActionType.DRAFT_JOB_CRITERIA)
            server_authorized_draft = False
        elif server_authorized_search:
            # Explicit new searches bypass the orchestration model's action
            # classification. The user's text is forwarded unmodified into
            # the existing validated NL planner in _dispatch_search.
            decision = AgentDecision(
                action=AgentActionType.SEARCH_CANDIDATES, search_query=user_message
            )
            server_authorized_search = False
        elif server_result_limit is not None:
            decision = AgentDecision(
                action=AgentActionType.REFINE_CANDIDATE_RESULTS, limit=server_result_limit
            )
            server_result_limit = None
        else:
            # Two separate advisory facts are sent to the model. Pointer
            # presence says only that a result context exists, including a
            # valid zero-member or stale/expired context; it is deliberately
            # non-authoritative. The validated count says which ordinals may
            # currently be referenced. Real authority remains the independent
            # server validation in refinement/profile/evidence dispatch.
            active_result_context_present = session_context.active_result_set_id is not None
            available_ref_count = await active_result_set_size(
                db,
                tenant_id=tenant_id,
                browser_session_id=session_context.browser_session_id,
                session_context=session_context,
            )
            decision = None
            for attempt in range(1, MAX_DECISION_ATTEMPTS + 1):
                try:
                    decision, provenance = await llm.decide_agent_action(
                        # Model context window only — never the whole
                        # durable transcript (issue #80).
                        recent_turns=[(t["role"], t["text"]) for t in turns[-max_context_turns:]],
                        last_tool_result_summary=last_tool_summary,
                        active_result_context_present=active_result_context_present,
                        available_candidate_refs=list(range(1, available_ref_count + 1)),
                        repair=attempt > 1,
                    )
                except ModelSchemaInvalidError:
                    decision = None
                    continue
                except (ModelTimeoutError, ModelUnavailableError, LLMProviderError):
                    # A follow-up "what next" decision failing after a tool
                    # already returned a real, grounded result is never a
                    # fatal turn failure — only the optional closing framing
                    # is missing (see D-036). A failure on the FIRST decision
                    # (tool_results still empty) remains a genuine failure.
                    result = _build_result(
                        outcome=(
                            AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT
                            if tool_results
                            else AgentTurnOutcome.AGENT_PROVIDER_FAILURE
                        ),
                        message=None,
                        tool_results=tool_results,
                        tool_call_count=tool_calls_made,
                        provenance=provenance,
                    )
                    return await _finish_turn(
                        db,
                        conversation,
                        session_context,
                        tenant_id=tenant_id,
                        turns=turns,
                        result=result,
                    )
                break

        if decision is None:
            result = _build_result(
                outcome=(
                    AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT
                    if tool_results
                    else AgentTurnOutcome.MALFORMED_MODEL_OUTPUT
                ),
                message=None,
                tool_results=tool_results,
                tool_call_count=tool_calls_made,
                provenance=provenance,
            )
            return await _finish_turn(
                db,
                conversation,
                session_context,
                tenant_id=tenant_id,
                turns=turns,
                result=result,
            )

        if (
            decision.action == AgentActionType.DRAFT_JOB_CRITERIA
            and not draft_authorized_for_decision
        ):
            # The deterministic entry route above is the only authority that
            # can reach the drafting branch. A DRAFT_JOB_CRITERIA proposal
            # made during normal model routing is not reinterpreted as search
            # or another action; it receives fixed clarification and has no
            # tool/business side effect.
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
            result = _build_result(
                outcome=AgentTurnOutcome.CLARIFICATION_REQUESTED,
                message=AMBIGUOUS_SEARCH_OR_JOB_COPY,
                tool_results=tool_results,
                tool_call_count=tool_calls_made,
                provenance=provenance,
            )
            return await _finish_turn(
                db,
                conversation,
                session_context,
                tenant_id=tenant_id,
                turns=turns,
                result=result,
            )

        if decision.action in (AgentActionType.FINAL_ANSWER, AgentActionType.CLARIFY):
            assert decision.response_code is not None
            outcome = (
                AgentTurnOutcome.ANSWERED
                if decision.action == AgentActionType.FINAL_ANSWER
                else AgentTurnOutcome.CLARIFICATION_REQUESTED
            )
            result = _build_result(
                outcome=outcome,
                # Once a tool has run, its validated result owns the
                # answer headline. The closed response code is only useful
                # for zero-tool generic conversation/clarification.
                message=(None if tool_results else _agent_response_text(decision.response_code)),
                tool_results=tool_results,
                tool_call_count=tool_calls_made,
                provenance=provenance,
            )
            return await _finish_turn(
                db,
                conversation,
                session_context,
                tenant_id=tenant_id,
                turns=turns,
                result=result,
            )

        if tool_calls_made >= max_tool_calls:
            result = _build_result(
                outcome=AgentTurnOutcome.TOOL_CALL_LIMIT_EXCEEDED,
                message=None,
                tool_results=tool_results,
                tool_call_count=tool_calls_made,
                provenance=provenance,
            )
            return await _finish_turn(
                db,
                conversation,
                session_context,
                tenant_id=tenant_id,
                turns=turns,
                result=result,
            )

        if decision.action == AgentActionType.SEARCH_CANDIDATES:
            assert decision.search_query is not None
            normalized_query = _fold(decision.search_query)
            if normalized_query in searched_queries:
                # Already answered by an identical search this same turn —
                # finalize on the existing results instead of repeating (or
                # worse, eventually hitting TOOL_CALL_LIMIT_EXCEEDED, which
                # would co-render a "simplify your query" message above
                # results that already fully answer the very same query).
                result = _build_result(
                    outcome=AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT,
                    message=None,
                    tool_results=tool_results,
                    tool_call_count=tool_calls_made,
                    provenance=provenance,
                )
                return await _finish_turn(
                    db,
                    conversation,
                    session_context,
                    tenant_id=tenant_id,
                    turns=turns,
                    result=result,
                )
            searched_queries.add(normalized_query)

        if decision.action == AgentActionType.DRAFT_JOB_CRITERIA:
            # Always turn-terminal, like GET_CANDIDATE_PROFILE/EVIDENCE —
            # unlike those, the dispatch call itself can genuinely fail
            # (a real LLM call, not a deterministic DB lookup), so it is
            # handled as its own branch rather than forced into the
            # uniform tool_result/matched_profile shape below.
            job_draft_result = await _dispatch_draft_job_criteria(llm, jd_text=job_draft_source)
            if job_draft_result is None:
                await record_event(
                    db,
                    tenant_id=tenant_id,
                    event_type="agent.tool.failed",
                    metadata={"tool_name": decision.action.value},
                )
                result = _build_result(
                    outcome=AgentTurnOutcome.JOB_DRAFT_FAILED,
                    message=None,
                    tool_results=tool_results,
                    tool_call_count=tool_calls_made,
                    provenance=provenance,
                )
                return await _finish_turn(
                    db,
                    conversation,
                    session_context,
                    tenant_id=tenant_id,
                    turns=turns,
                    result=result,
                )
            tool_calls_made += 1
            tool_results.append(job_draft_result)
            assert job_draft_result.job_draft is not None
            await record_event(
                db,
                tenant_id=tenant_id,
                event_type="agent.tool.executed",
                metadata={
                    "tool_name": decision.action.value,
                    "tool_call_index": tool_calls_made,
                    **jd_draft_audit_metadata(job_draft_result.job_draft),
                },
            )
            result = _build_result(
                outcome=AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT,
                message=None,
                tool_results=tool_results,
                tool_call_count=tool_calls_made,
                provenance=provenance,
            )
            return await _finish_turn(
                db,
                conversation,
                session_context,
                tenant_id=tenant_id,
                turns=turns,
                result=result,
            )

        if decision.action == AgentActionType.REFINE_CANDIDATE_RESULTS:
            # Always turn-terminal (issue #49 PR49-2, docs/DECISIONS.md
            # D-084) — never let the model keep looping and accidentally
            # launch a fresh search after a valid refinement. Handled as
            # its own branch (not the uniform tool_result/matched_profile
            # shape below): a rejection here means "the active result set
            # is untouched", never a candidate_ref resolution outcome.
            refine_dispatch = await _dispatch_refine(
                db,
                llm,
                tenant_id=tenant_id,
                session_context=session_context,
                decision=decision,
                as_of_date=as_of_date,
                embedding_config=embedding_config,
            )
            if refine_dispatch.rejection_message is not None:
                result = _build_result(
                    outcome=AgentTurnOutcome.CLARIFICATION_REQUESTED,
                    message=refine_dispatch.rejection_message,
                    tool_results=tool_results,
                    tool_call_count=tool_calls_made,
                    provenance=provenance,
                )
                return await _finish_turn(
                    db,
                    conversation,
                    session_context,
                    tenant_id=tenant_id,
                    turns=turns,
                    result=result,
                )
            if refine_dispatch.resolution_failure is not None:
                failure_outcome, failure_message = _outcome_and_message_for_refinement_failure(
                    refine_dispatch.resolution_failure
                )
                result = _build_result(
                    outcome=failure_outcome,
                    message=failure_message,
                    tool_results=tool_results,
                    tool_call_count=tool_calls_made,
                    provenance=provenance,
                )
                return await _finish_turn(
                    db,
                    conversation,
                    session_context,
                    tenant_id=tenant_id,
                    turns=turns,
                    result=result,
                )
            assert (
                refine_dispatch.tool_result is not None
                and refine_dispatch.new_result_set_id is not None
            )
            tool_calls_made += 1
            tool_results.append(refine_dispatch.tool_result)
            # Eagerly synced (not deferred to _finish_turn), same as a
            # successful SEARCH_CANDIDATES result set switch above — the
            # committed active pointer must be the derived set the moment
            # this turn ends.
            session_context.active_result_set_id = refine_dispatch.new_result_set_id
            await record_event(
                db,
                tenant_id=tenant_id,
                event_type="agent.tool.executed",
                metadata={"tool_name": decision.action.value, "tool_call_index": tool_calls_made},
            )
            result = _build_result(
                outcome=AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT,
                message=None,
                tool_results=tool_results,
                tool_call_count=tool_calls_made,
                provenance=provenance,
            )
            return await _finish_turn(
                db,
                conversation,
                session_context,
                tenant_id=tenant_id,
                turns=turns,
                result=result,
            )

        matched_profile: CandidateProfileExtraction | None = None
        resolution_failure: ResultSetResolutionFailure | None = None
        if decision.action == AgentActionType.SEARCH_CANDIDATES:
            previous_result_set_id = session_context.active_result_set_id
            tool_result, new_result_set_id = await _dispatch_search(
                db,
                llm,
                tenant_id=tenant_id,
                session_context=session_context,
                previous_result_set_id=previous_result_set_id,
                decision=decision,
                as_of_date=as_of_date,
                embedding_config=embedding_config,
                embedding_provider=embedding_provider,
            )
            if new_result_set_id is not None:
                # Kept in sync eagerly (not deferred to _finish_turn) so a
                # later GET_CANDIDATE_PROFILE/EVIDENCE call within this
                # SAME turn, and this turn's own available_candidate_refs
                # computation, both see the freshly created result set
                # immediately — mirrors the old local-variable semantics
                # of last_search_candidate_ids exactly.
                session_context.active_result_set_id = new_result_set_id
        elif decision.action == AgentActionType.GET_CANDIDATE_PROFILE:
            tool_result, matched_profile, resolution_failure = await _dispatch_profile(
                db,
                tenant_id=tenant_id,
                decision=decision,
                session_context=session_context,
            )
        else:
            tool_result, matched_profile, resolution_failure = await _dispatch_evidence(
                db,
                tenant_id=tenant_id,
                decision=decision,
                session_context=session_context,
            )

        tool_calls_made += 1
        tool_results.append(tool_result)
        last_tool_summary = _summarize_tool_result(tool_result)
        await record_event(
            db,
            tenant_id=tenant_id,
            event_type="agent.tool.executed",
            metadata={"tool_name": decision.action.value, "tool_call_index": tool_calls_made},
        )

        if search_turn_terminal and decision.action == AgentActionType.SEARCH_CANDIDATES:
            # Server-authorized search: the validated planner/search result
            # (executable, empty, or a truthful non-executable plan) is the
            # whole answer.
            result = _build_result(
                outcome=AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT,
                message=None,
                tool_results=tool_results,
                tool_call_count=tool_calls_made,
                provenance=provenance,
            )
            return await _finish_turn(
                db,
                conversation,
                session_context,
                tenant_id=tenant_id,
                turns=turns,
                result=result,
            )

        # GET_CANDIDATE_PROFILE / GET_CANDIDATE_EVIDENCE are always
        # turn-terminal, found or not: each is already a complete,
        # self-contained, evidence-grounded answer to one specific
        # question, so no further model judgment is spent (or risked) on
        # it. Only SEARCH_CANDIDATES loops back — the model may still
        # decide to look at a specific result (a second tool call, bounded
        # by max_tool_calls) or close the turn with FINAL_ANSWER/CLARIFY.
        if decision.action in (
            AgentActionType.GET_CANDIDATE_PROFILE,
            AgentActionType.GET_CANDIDATE_EVIDENCE,
        ):
            found = (
                tool_result.profile.found
                if tool_result.profile is not None
                else tool_result.evidence.found  # type: ignore[union-attr]
            )
            # Never plain ANSWERED here — that outcome is reserved for a
            # FINAL_ANSWER closed response code rendered as fixed server
            # copy. A
            # successful profile/evidence lookup has no model framing at
            # all — instead, when found, attempt one bounded D-038
            # grounded-answer synthesis over this candidate's own facts;
            # a None result (unavailable, invalid, or ungrounded) falls
            # back to the existing deterministic message exactly as
            # before (D-036) — never a turn failure.
            synthesized_message: str | None = None
            if found and matched_profile is not None:
                synthesized_message = await _synthesize_grounded_answer(
                    llm,
                    question=user_message,
                    facts=_build_profile_facts(matched_profile),
                )
            result = _build_result(
                outcome=(
                    AgentTurnOutcome.ANSWERED_FROM_TOOL_RESULT
                    if found
                    else _outcome_for_resolution_failure(resolution_failure)
                ),
                message=synthesized_message,
                tool_results=tool_results,
                tool_call_count=tool_calls_made,
                provenance=provenance,
            )
            return await _finish_turn(
                db,
                conversation,
                session_context,
                tenant_id=tenant_id,
                turns=turns,
                result=result,
            )
