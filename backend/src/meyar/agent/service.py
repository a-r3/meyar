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
conversation's OWN server-held ``last_search_candidate_ids`` (see
``_resolve_candidate_ref``), so the model's own memory of what it was
shown is never the authority for which candidate a tool call touches."""

import uuid
from datetime import date

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.agent.prompts import AGENT_PROMPT_VERSION, AGENT_SYSTEM_PROMPT
from meyar.agent.schemas import (
    AGENT_POLICY_VERSION,
    AgentActionType,
    AgentDecision,
    AgentEvidenceToolResult,
    AgentJobDraftToolResult,
    AgentProfileToolResult,
    AgentSearchToolResult,
    AgentToolResult,
    AgentTurnOutcome,
    AgentTurnResult,
    DroppedJDCriterionReason,
    EvidenceMatchItem,
    GroundedCaveat,
    GroundedFact,
    GroundedSelection,
    JDCriteriaDraft,
    JDDraftCriterionItem,
    JDDraftCriterionKind,
    UnsupportedJDCriterionItem,
)
from meyar.core.text import (
    combine_degree_and_field,
    fold_az_ascii,
    normalize_azerbaijani_case,
    slugify_criterion_label,
)
from meyar.embedding.provider import EmbeddingProvider
from meyar.llm.provider import (
    LLMProvider,
    LLMProviderError,
    LLMResultProvenance,
    ModelSchemaInvalidError,
    ModelTimeoutError,
    ModelUnavailableError,
)
from meyar.models.agent_conversation import AgentConversation
from meyar.models.candidate_profile_version import PROFILE_STATUS_COMPLETED
from meyar.schemas.candidate_profile import CandidateProfileExtraction
from meyar.schemas.criteria import (
    CriterionIn,
    CriterionKind,
    CriterionType,
    ProhibitedCriterionError,
)
from meyar.search.planner_service import plan_and_search_candidates
from meyar.search.schemas import EmbeddingSearchConfig
from meyar.services.agent_conversation_repo import save_conversation_state
from meyar.services.audit_repo import record_event
from meyar.services.candidate_profile_repo import get_current_profile_version

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


# Real-Ollama acceptance testing for Slice 4 (issue #33) surfaced a genuine
# small-local-model defect: instead of authoring a real clarifying
# question, the model can copy one of AGENT_SYSTEM_PROMPT's own English
# instructional sentences verbatim into AgentDecision.message — a raw
# planner-internals leak into HR-facing text, which D-035's "the model's
# message is safe to show verbatim" boundary did not anticipate. Guarded
# structurally (an exact/near-exact containment check against the fixed
# prompt text, not a fuzzy heuristic) rather than by a prompt instruction
# alone, matching this module's D-038 precedent — a leak is treated
# exactly like schema-invalid output: bounded retry, then the existing
# deterministic MALFORMED_MODEL_OUTPUT fallback.
_MIN_PROMPT_LEAK_LENGTH = 40


def _looks_like_prompt_leak(message: str) -> bool:
    stripped = message.strip()
    return len(stripped) >= _MIN_PROMPT_LEAK_LENGTH and stripped in AGENT_SYSTEM_PROMPT


def _resolve_candidate_ref(
    *, last_search_candidate_ids: list[str], candidate_ref: int
) -> uuid.UUID | None:
    """The ONLY place a candidate_ref (a small model-produced ordinal)
    becomes a real candidate_id — resolved purely against this
    conversation's own server-held state, never against anything the
    model asserts about a candidate_id directly (the model is never shown
    one). An out-of-range/stale ordinal simply fails to resolve."""
    index = candidate_ref - 1
    if index < 0 or index >= len(last_search_candidate_ids):
        return None
    try:
        return uuid.UUID(last_search_candidate_ids[index])
    except ValueError:
        return None


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
    decision: AgentDecision,
    as_of_date: date,
    embedding_config: EmbeddingSearchConfig,
    embedding_provider: EmbeddingProvider | None,
) -> tuple[AgentToolResult, list[str] | None]:
    """Forwards decision.search_query, unmodified, into the existing
    frozen NL search-planner pipeline (D-031) — this module never
    re-implements filter extraction, prohibited-attribute checks, or the
    no-silent-weakening rule; it only reuses them."""
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
        updated_ids = [
            str(item.candidate_id)
            for item in sorted(planned.search_response.results, key=lambda r: r.rank)
        ]
        return tool_result, updated_ids
    # A non-executable/failed search never clears a prior valid
    # candidate_ref table — only a successful search replaces it.
    return tool_result, None


async def _dispatch_profile(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    decision: AgentDecision,
    last_search_candidate_ids: list[str],
) -> tuple[AgentToolResult, CandidateProfileExtraction | None]:
    """Returns (tool result, the raw validated profile when found) — the
    profile is handed back separately so run_agent_turn can build grounded
    -answer facts (D-037/D-038) without a second, redundant DB fetch."""
    assert decision.candidate_ref is not None
    candidate_id = _resolve_candidate_ref(
        last_search_candidate_ids=last_search_candidate_ids,
        candidate_ref=decision.candidate_ref,
    )
    if candidate_id is None:
        return (
            AgentToolResult(
                tool_name=AgentActionType.GET_CANDIDATE_PROFILE,
                profile=AgentProfileToolResult(candidate_ref=decision.candidate_ref, found=False),
            ),
            None,
        )
    version = await get_current_profile_version(db, tenant_id=tenant_id, candidate_id=candidate_id)
    if (
        version is None
        or version.status != PROFILE_STATUS_COMPLETED
        or version.profile_content is None
    ):
        return (
            AgentToolResult(
                tool_name=AgentActionType.GET_CANDIDATE_PROFILE,
                profile=AgentProfileToolResult(
                    candidate_ref=decision.candidate_ref,
                    candidate_id=candidate_id,
                    found=False,
                    profile_status=version.status if version else None,
                ),
            ),
            None,
        )
    try:
        profile = CandidateProfileExtraction.model_validate(version.profile_content)
    except ValidationError:
        return (
            AgentToolResult(
                tool_name=AgentActionType.GET_CANDIDATE_PROFILE,
                profile=AgentProfileToolResult(
                    candidate_ref=decision.candidate_ref,
                    candidate_id=candidate_id,
                    found=False,
                    profile_status=version.status,
                ),
            ),
            None,
        )
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


async def _dispatch_evidence(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    decision: AgentDecision,
    last_search_candidate_ids: list[str],
) -> tuple[AgentToolResult, CandidateProfileExtraction | None]:
    """Returns (tool result, the raw validated profile when found) — see
    _dispatch_profile's docstring; the same profile backs D-037/D-038
    grounded-answer synthesis for both tools identically (never the raw
    evidence quote text, which stays server-rendered-only, never model
    input)."""
    assert decision.candidate_ref is not None
    candidate_id = _resolve_candidate_ref(
        last_search_candidate_ids=last_search_candidate_ids,
        candidate_ref=decision.candidate_ref,
    )
    if candidate_id is None:
        return (
            AgentToolResult(
                tool_name=AgentActionType.GET_CANDIDATE_EVIDENCE,
                evidence=AgentEvidenceToolResult(
                    candidate_ref=decision.candidate_ref,
                    found=False,
                    topic=decision.evidence_topic,
                ),
            ),
            None,
        )
    version = await get_current_profile_version(db, tenant_id=tenant_id, candidate_id=candidate_id)
    if (
        version is None
        or version.status != PROFILE_STATUS_COMPLETED
        or version.profile_content is None
    ):
        return (
            AgentToolResult(
                tool_name=AgentActionType.GET_CANDIDATE_EVIDENCE,
                evidence=AgentEvidenceToolResult(
                    candidate_ref=decision.candidate_ref,
                    candidate_id=candidate_id,
                    found=False,
                    profile_status=version.status if version else None,
                    topic=decision.evidence_topic,
                ),
            ),
            None,
        )
    try:
        profile = CandidateProfileExtraction.model_validate(version.profile_content)
    except ValidationError:
        return (
            AgentToolResult(
                tool_name=AgentActionType.GET_CANDIDATE_EVIDENCE,
                evidence=AgentEvidenceToolResult(
                    candidate_ref=decision.candidate_ref,
                    candidate_id=candidate_id,
                    found=False,
                    profile_status=version.status,
                    topic=decision.evidence_topic,
                ),
            ),
            None,
        )

    topic_folded = _fold(decision.evidence_topic) if decision.evidence_topic else None
    matches: list[EvidenceMatchItem] = []
    for category, title_fn in _EVIDENCE_CATEGORIES:
        for item in getattr(profile, category):
            title = title_fn(item)
            if topic_folded is not None:
                title_folded = _fold(title)
                if topic_folded not in title_folded and title_folded not in topic_folded:
                    continue
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
                topic=decision.evidence_topic,
                matches=matches,
            ),
        ),
        profile,
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


def _build_criterion_from_draft_item(
    item: JDDraftCriterionItem, *, criterion_type: CriterionType, used_ids: set[str]
) -> tuple[CriterionIn | None, DroppedJDCriterionReason | None]:
    """Re-validates one MODEL-PRODUCED draft item into a real CriterionIn —
    the exact same schema/prohibited-attribute denylist the manual
    vacancy-creation form and the REST API already enforce (D-031 point 4:
    an agent tool-dispatch layer is a new PRODUCER of arguments, never a
    new validator). Returns ``(None, reason)`` on validation failure —
    never a silent drop (PR #42 owner correction, issue #33): the caller
    discloses a PROHIBITED reason as a count only (the matched text itself
    must never be redisplayed), and an UNSUPPORTED reason with the
    original, already-confirmed-non-sensitive requirement text. Mirrors
    meyar.ui.service._parse_criterion_row's kind-aware value/min_years
    shape, but reports instead of raising since this is a best-effort
    DRAFT, not a form submission.

    ``item.kind == JDDraftCriterionKind.OTHER`` is routed straight to
    UNSUPPORTED here, deterministically — never via an incidental
    CriterionIn validation failure — because OTHER, by construction, is
    the model's own explicit signal that this requirement does not fit
    any evaluator-supported kind (D-045); building a CriterionIn from it
    would either fail unpredictably or, worse, misclassify a genuinely
    unsupported requirement into a supported (and therefore scored)
    criterion, which the deterministic scorer must never do."""
    if item.kind == JDDraftCriterionKind.OTHER:
        return None, DroppedJDCriterionReason.UNSUPPORTED
    kind = CriterionKind(item.kind.value)
    value = None if kind == CriterionKind.EXPERIENCE else item.requirement
    min_years = item.min_years if kind == CriterionKind.EXPERIENCE else None
    try:
        criterion = CriterionIn(
            id=slugify_criterion_label(item.requirement, used_ids),
            kind=kind,
            type=criterion_type,
            label=item.requirement,
            value=value,
            min_years=min_years,
            weight=item.weight,
        )
        return criterion, None
    except ValidationError as exc:
        # A model_validator raising ProhibitedCriterionError (itself a
        # ValueError subclass) is always re-wrapped by pydantic into a
        # generic ValidationError before it reaches this except clause —
        # the original exception survives only inside each error's own
        # ``ctx["error"]`` (verified against pydantic 2.11's actual
        # behavior, not merely assumed). Unwrap it there to tell a
        # sensitive-attribute match apart from every other validation
        # failure (D-043, PR #42 owner correction, issue #33).
        if any(
            isinstance(error.get("ctx", {}).get("error"), ProhibitedCriterionError)
            for error in exc.errors()
        ):
            return None, DroppedJDCriterionReason.PROHIBITED
        return None, DroppedJDCriterionReason.UNSUPPORTED


async def _dispatch_draft_job_criteria(
    llm: LLMProvider, *, jd_text: str
) -> AgentToolResult | None:
    """Returns None only when the drafting call itself never produced a
    usable JDCriteriaDraft (repeated schema-invalid output, or a
    provider failure) — the caller turns that into AgentTurnOutcome.
    JOB_DRAFT_FAILED with no tool_results, mirroring the existing
    AGENT_PROVIDER_FAILURE/MALFORMED_MODEL_OUTPUT precedent (D-036). A
    successful call always returns a real AgentToolResult, even with zero
    criteria."""
    draft: JDCriteriaDraft | None = None
    for attempt in range(1, MAX_JD_DRAFT_ATTEMPTS + 1):
        try:
            draft, _provenance = await llm.draft_job_criteria(jd_text, repair=attempt > 1)
            break
        except ModelSchemaInvalidError:
            continue
        except (ModelTimeoutError, ModelUnavailableError, LLMProviderError):
            return None
    if draft is None:
        return None

    used_ids: set[str] = set()
    must_have: list[CriterionIn] = []
    preferred: list[CriterionIn] = []
    unsupported: list[UnsupportedJDCriterionItem] = []
    prohibited_count = 0
    for items, criterion_type, bucket in (
        (draft.must_have, CriterionType.MUST_HAVE, must_have),
        (draft.preferred, CriterionType.PREFERRED, preferred),
    ):
        for item in items:
            criterion, reason = _build_criterion_from_draft_item(
                item, criterion_type=criterion_type, used_ids=used_ids
            )
            if criterion is not None:
                bucket.append(criterion)
            elif reason == DroppedJDCriterionReason.PROHIBITED:
                prohibited_count += 1
            else:
                unsupported.append(
                    UnsupportedJDCriterionItem(
                        requirement=item.requirement, criterion_type=criterion_type
                    )
                )

    return AgentToolResult(
        tool_name=AgentActionType.DRAFT_JOB_CRITERIA,
        job_draft=AgentJobDraftToolResult(
            title=draft.title,
            must_have=must_have,
            preferred=preferred,
            unsupported=unsupported,
            prohibited_count=prohibited_count,
        ),
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
    *,
    tenant_id: uuid.UUID,
    turns: list[dict],
    last_search_candidate_ids: list[str],
    max_context_turns: int,
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
    turns = [
        *turns,
        {"role": "assistant", "text": result.message or "", "outcome": result.outcome.value},
    ][-max_context_turns:]
    await save_conversation_state(
        db,
        conversation,
        turns=turns,
        last_search_candidate_ids=last_search_candidate_ids,
    )
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
    user_message: str,
    as_of_date: date,
    embedding_config: EmbeddingSearchConfig,
    embedding_provider: EmbeddingProvider | None,
    max_tool_calls: int,
    max_context_turns: int,
    explicit_action: AgentActionType | None = None,
) -> AgentTurnResult:
    """One bounded orchestration turn. Never persists a mutation to any
    candidate/job/evaluation row — only this conversation's own
    session-scoped state (turns, last_search_candidate_ids). Caller is
    responsible for the surrounding db.commit()/rollback().

    ``explicit_action``: PR #42 owner correction (issue #33, D-043/D-044)
    — an explicit first-class UI affordance (the composer's "Vakansiya
    elanını analiz et" mode) lets HR pin this turn's action
    deterministically, bypassing ``llm.decide_agent_action``
    entirely for the first decision so a small local model's unreliable
    intent routing (documented D-042 point 6) can never misroute a pasted
    JD to SEARCH_CANDIDATES. Only ``AgentActionType.DRAFT_JOB_CRITERIA`` is
    supported today — the caller (meyar.ui.router) is the only source of
    this value, never the model or an arbitrary client-supplied string."""
    if explicit_action is not None and explicit_action != AgentActionType.DRAFT_JOB_CRITERIA:
        raise ValueError(f"Unsupported explicit_action: {explicit_action}")
    turns: list[dict] = [*conversation.turns, {"role": "user", "text": user_message}]
    last_search_candidate_ids = list(conversation.last_search_candidate_ids)
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
        if explicit_action is not None:
            # Deterministic first-class path: no model call, no routing
            # ambiguity — see the explicit_action docstring above. Only
            # ever taken once (DRAFT_JOB_CRITERIA is always turn-terminal,
            # see below), so clearing it here is defensive, not load-bearing.
            decision = AgentDecision(action=explicit_action)
            explicit_action = None
        else:
            decision = None
            for attempt in range(1, MAX_DECISION_ATTEMPTS + 1):
                try:
                    decision, provenance = await llm.decide_agent_action(
                        recent_turns=[
                            (t["role"], t["text"]) for t in turns[-max_context_turns:]
                        ],
                        last_tool_result_summary=last_tool_summary,
                        available_candidate_refs=list(
                            range(1, len(last_search_candidate_ids) + 1)
                        ),
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
                        tenant_id=tenant_id,
                        turns=turns,
                        last_search_candidate_ids=last_search_candidate_ids,
                        max_context_turns=max_context_turns,
                        result=result,
                    )
                if decision.action in (
                    AgentActionType.FINAL_ANSWER,
                    AgentActionType.CLARIFY,
                ) and _looks_like_prompt_leak(decision.message or ""):
                    decision = None
                    continue
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
                tenant_id=tenant_id,
                turns=turns,
                last_search_candidate_ids=last_search_candidate_ids,
                max_context_turns=max_context_turns,
                result=result,
            )

        if decision.action in (AgentActionType.FINAL_ANSWER, AgentActionType.CLARIFY):
            outcome = (
                AgentTurnOutcome.ANSWERED
                if decision.action == AgentActionType.FINAL_ANSWER
                else AgentTurnOutcome.CLARIFICATION_REQUESTED
            )
            result = _build_result(
                outcome=outcome,
                message=decision.message,
                tool_results=tool_results,
                tool_call_count=tool_calls_made,
                provenance=provenance,
            )
            return await _finish_turn(
                db,
                conversation,
                tenant_id=tenant_id,
                turns=turns,
                last_search_candidate_ids=last_search_candidate_ids,
                max_context_turns=max_context_turns,
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
                tenant_id=tenant_id,
                turns=turns,
                last_search_candidate_ids=last_search_candidate_ids,
                max_context_turns=max_context_turns,
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
                    tenant_id=tenant_id,
                    turns=turns,
                    last_search_candidate_ids=last_search_candidate_ids,
                    max_context_turns=max_context_turns,
                    result=result,
                )
            searched_queries.add(normalized_query)

        if decision.action == AgentActionType.DRAFT_JOB_CRITERIA:
            # Always turn-terminal, like GET_CANDIDATE_PROFILE/EVIDENCE —
            # unlike those, the dispatch call itself can genuinely fail
            # (a real LLM call, not a deterministic DB lookup), so it is
            # handled as its own branch rather than forced into the
            # uniform tool_result/matched_profile shape below.
            job_draft_result = await _dispatch_draft_job_criteria(llm, jd_text=user_message)
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
                    tenant_id=tenant_id,
                    turns=turns,
                    last_search_candidate_ids=last_search_candidate_ids,
                    max_context_turns=max_context_turns,
                    result=result,
                )
            tool_calls_made += 1
            tool_results.append(job_draft_result)
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
                tenant_id=tenant_id,
                turns=turns,
                last_search_candidate_ids=last_search_candidate_ids,
                max_context_turns=max_context_turns,
                result=result,
            )

        matched_profile: CandidateProfileExtraction | None = None
        if decision.action == AgentActionType.SEARCH_CANDIDATES:
            tool_result, updated_ids = await _dispatch_search(
                db,
                llm,
                tenant_id=tenant_id,
                decision=decision,
                as_of_date=as_of_date,
                embedding_config=embedding_config,
                embedding_provider=embedding_provider,
            )
            if updated_ids is not None:
                last_search_candidate_ids = updated_ids
        elif decision.action == AgentActionType.GET_CANDIDATE_PROFILE:
            tool_result, matched_profile = await _dispatch_profile(
                db,
                tenant_id=tenant_id,
                decision=decision,
                last_search_candidate_ids=last_search_candidate_ids,
            )
        else:
            tool_result, matched_profile = await _dispatch_evidence(
                db,
                tenant_id=tenant_id,
                decision=decision,
                last_search_candidate_ids=last_search_candidate_ids,
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
            # real model-authored FINAL_ANSWER message (guaranteed
            # non-None by AgentDecision's own shape validator). A
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
                    else AgentTurnOutcome.CANDIDATE_REF_NOT_FOUND
                ),
                message=synthesized_message,
                tool_results=tool_results,
                tool_call_count=tool_calls_made,
                provenance=provenance,
            )
            return await _finish_turn(
                db,
                conversation,
                tenant_id=tenant_id,
                turns=turns,
                last_search_candidate_ids=last_search_candidate_ids,
                max_context_turns=max_context_turns,
                result=result,
            )
