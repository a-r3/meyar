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

import re
import uuid
from datetime import date

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.agent.jd_authority import explicit_modality, segment_requirement_spans
from meyar.agent.prompts import AGENT_PROMPT_VERSION
from meyar.agent.schemas import (
    AGENT_POLICY_VERSION,
    AgentActionType,
    AgentDecision,
    AgentEvidenceToolResult,
    AgentJobDraftToolResult,
    AgentProfileToolResult,
    AgentResponseCode,
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
    NeedsReviewJDCriterionItem,
    RequirementSpan,
    RequirementSpanResult,
    RequirementSpanState,
    SemanticRequirement,
    SemanticRequirementState,
    SupportedInputLanguage,
    UnsupportedJDCriterionItem,
)
from meyar.agent.semantic_requirements import analyze_hr_text
from meyar.core.domain_terms import DOMAIN_SYNONYMS, canonicalize_domain
from meyar.core.result_count import extract_result_count_intent, is_result_count_only
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
from meyar.models.agent_conversation import AgentConversation
from meyar.schemas.candidate_profile import CandidateProfileExtraction
from meyar.schemas.criteria import (
    CriterionIn,
    CriterionKind,
    CriterionType,
    ProhibitedCriterionError,
    find_prohibited_term,
)
from meyar.search.planner_service import plan_and_search_candidates
from meyar.search.schemas import EmbeddingSearchConfig
from meyar.services.agent_conversation_repo import (
    ASSISTANT_TEXT_AUTHORITY_SERVER,
    ASSISTANT_TEXT_AUTHORITY_VERSION,
    save_conversation_state,
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


_REQUIRED_CUE_RE = re.compile(
    r"\b(required|must|mandatory|minimum|teleb\w*|mutleq\w*|vacib\w*|olmalidir|olmalidi|[a-z]+m[ae]lidir)\b",
    re.IGNORECASE,
)
_PREFERRED_CUE_RE = re.compile(
    r"\b(preferred|nice\s+to\s+have|plus|ustunluk\w*|arzuolunan\w*)\b", re.IGNORECASE
)
_DURATION_CUE_RE = re.compile(r"\b(years?|yrs?|il|tecrube|experience)\b", re.IGNORECASE)
_GENERAL_EXPERIENCE_RE = re.compile(
    r"\b(total|overall|general|umumi)\b.*\b(experience|tecrube)\b|"
    r"\b(experience|tecrube)\b.*\b(total|overall|general|umumi)\b",
    re.IGNORECASE,
)
_LANGUAGE_LEVEL_RE = re.compile(
    r"\b(?:a1|a2|b1|b2|c1|c2|beginner|elementary|intermediate|advanced|fluent|native|serbest)\b",
    re.IGNORECASE,
)
_CERTIFICATION_CUE_RE = re.compile(r"\b(certificat\w*|sertifikat\w*)\b", re.IGNORECASE)
_EDUCATION_CUE_RE = re.compile(
    r"\b(education|degree|bachelor|master|university|tehsil\w*|bakalavr\w*|magistr\w*)\b",
    re.IGNORECASE,
)
_LANGUAGE_CUE_RE = re.compile(
    r"\b(language|proficiency|dil\w*|english|ingilis\w*|russian|rusca|azerbaijani|azerbaycan\w*)\b",
    re.IGNORECASE,
)


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
}


def _agent_response_text(code: AgentResponseCode) -> str:
    return _AGENT_RESPONSE_TEXT[code]


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
                ),
            ),
            None,
        )
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


def _canonical_subject(span: RequirementSpan) -> str:
    """Extract the complete source subject without fuzzy token matching."""
    subject = span.normalized
    subject = re.sub(r"\bis\s+(?:a\s+)?plus\b", " ", subject)
    subject = _REQUIRED_CUE_RE.sub(" ", subject)
    subject = _PREFERRED_CUE_RE.sub(" ", subject)
    subject = re.sub(r"\b\d+(?:[.,]\d+)?\s*(?:years?|yrs?|il)\b", " ", subject)
    subject = _LANGUAGE_LEVEL_RE.sub(" ", subject)
    subject = re.sub(
        r"\b(?:experience|tecrube\w*|proficiency|level|knowledge|bilik\w*|bacariq\w*|"
        r"olunur|olaraq|candidate|namized|total|overall|general|umumi)\b",
        " ",
        subject,
    )
    return " ".join(re.findall(r"[a-z0-9+#.]+", subject)).strip()


def _source_number(span: RequirementSpan) -> float | None:
    values = re.findall(r"(?<![\w.])\d+(?:[.,]\d+)?(?![\w.])", span.normalized)
    if len(values) != 1:
        return None
    return float(values[0].replace(",", "."))


def _source_language_level(span: RequirementSpan) -> str | None:
    match = _LANGUAGE_LEVEL_RE.search(span.normalized)
    return match.group(0).casefold() if match else None


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


def _canonicalize_supported_draft_shape(
    item: JDDraftCriterionItem, *, span: RequirementSpan
) -> JDDraftCriterionItem:
    """Map legacy model shapes onto fields owned by the canonical span.

    This adapter cannot add source authority: it only runs after span_id
    resolution, and every resulting field is revalidated against that exact
    occurrence by _canonical_binding_result.
    """
    subject = _canonical_subject(span)
    folded = span.normalized
    has_experience = bool(re.search(r"\b(?:experience|tecrube\w*)\b", folded))
    has_duration = bool(_DURATION_CUE_RE.search(folded) and re.search(r"\d", folded))
    source_level = _source_language_level(span)
    is_language = bool(_LANGUAGE_CUE_RE.search(folded) or source_level)
    is_general_experience = bool(_GENERAL_EXPERIENCE_RE.search(folded))

    if is_language and item.kind == JDDraftCriterionKind.LANGUAGE and subject:
        if source_level is not None and (
            item.required_level is None
            and source_level not in _normalize_source_text(item.requirement)
        ):
            return item
        return item.model_copy(
            update={
                "requirement": _canonical_display_subject(item.kind, subject),
                "required_level": source_level.upper() if source_level else None,
                "min_years": None,
            }
        )
    if (has_experience or has_duration) and not is_general_experience and subject:
        expected = _expected_experience_kind(subject)
        if expected is not None and item.kind in {
            JDDraftCriterionKind.SKILL,
            JDDraftCriterionKind.EXPERIENCE,
            JDDraftCriterionKind.SKILL_EXPERIENCE,
            JDDraftCriterionKind.DOMAIN_EXPERIENCE,
        }:
            source_number = _source_number(span) if has_duration else None
            if has_duration and item.min_years != source_number:
                return item
            if not has_duration and item.min_years is not None:
                return item
            if item.kind == JDDraftCriterionKind.SKILL and item.min_years is None:
                return item
            if (
                item.kind == JDDraftCriterionKind.SKILL
                and expected == JDDraftCriterionKind.DOMAIN_EXPERIENCE
            ):
                return item
            # A legacy generic EXPERIENCE row is adaptable only when its own
            # label still names the canonical subject. This prevents the old
            # unsafe "total experience + separate skill" decomposition from
            # being recombined by coincidence.
            if item.kind == JDDraftCriterionKind.EXPERIENCE and not re.search(
                rf"(?<!\w){re.escape(subject)}(?!\w)",
                _normalize_source_text(item.requirement),
            ):
                return item
            return item.model_copy(
                update={
                    "kind": expected,
                    "requirement": _canonical_display_subject(expected, subject),
                    "min_years": source_number,
                    "required_level": None,
                }
            )
    return item


def _review_item_for_supported_ambiguity(
    item: JDDraftCriterionItem, *, span: RequirementSpan
) -> NeedsReviewJDCriterionItem | None:
    """Expose only a bounded ambiguity HR is authorized to resolve."""
    if explicit_modality(span.text) is not None:
        return None
    if item.kind != JDDraftCriterionKind.LANGUAGE:
        return None
    subject = _canonical_subject(span)
    level = _source_language_level(span)
    if not subject or level is None:
        return None
    return NeedsReviewJDCriterionItem(
        requirement=span.text,
        span_id=span.span_id,
        kind=JDDraftCriterionKind.LANGUAGE,
        subject=_canonical_display_subject(JDDraftCriterionKind.LANGUAGE, subject),
        required_level=level.upper(),
        allowed_types=[CriterionType.MUST_HAVE, CriterionType.PREFERRED],
    )


def _subjects_match(kind: JDDraftCriterionKind, drafted: str, canonical: str) -> bool:
    if not canonical:
        return False
    if kind in (JDDraftCriterionKind.SKILL, JDDraftCriterionKind.SKILL_EXPERIENCE):
        return normalize_skill_name(_normalize_source_text(drafted)) == normalize_skill_name(
            canonical
        )
    if kind == JDDraftCriterionKind.DOMAIN_EXPERIENCE:
        drafted_domain = "banking" if _normalize_source_text(drafted) == "bank" else drafted
        canonical_domain = "banking" if canonical == "bank" else canonical
        return canonicalize_domain(drafted_domain) == canonicalize_domain(canonical_domain)
    return _normalize_source_text(drafted) == _normalize_source_text(canonical)


def _expected_experience_kind(subject: str) -> JDDraftCriterionKind | None:
    if not subject:
        return None
    if subject == "bank" or canonicalize_domain(subject) in DOMAIN_SYNONYMS:
        return JDDraftCriterionKind.DOMAIN_EXPERIENCE
    return JDDraftCriterionKind.SKILL_EXPERIENCE


def _canonical_binding_result(
    item: JDDraftCriterionItem,
    *,
    criterion_type: CriterionType,
    span: RequirementSpan,
) -> DroppedJDCriterionReason | None:
    """Validate an item against the complete server-owned occurrence.

    ``None`` means the item can proceed to CriterionIn construction.
    Unsupported means the source semantics are valid but the current agent
    review form cannot round-trip them. Every other mismatch fails closed to
    human review; source_text is intentionally never consulted.
    """
    if span.segmentation_needs_review:
        return DroppedJDCriterionReason.NEEDS_HUMAN_REVIEW
    source_type = explicit_modality(span.text)
    if source_type is None or source_type != criterion_type.value:
        return DroppedJDCriterionReason.NEEDS_HUMAN_REVIEW

    folded = span.normalized
    subject = _canonical_subject(span)
    has_experience = bool(re.search(r"\b(?:experience|tecrube\w*)\b", folded))
    has_duration = bool(_DURATION_CUE_RE.search(folded) and re.search(r"\d", folded))
    source_number = _source_number(span)
    source_level = _source_language_level(span)
    is_language = bool(_LANGUAGE_CUE_RE.search(folded) or source_level)
    is_certification = bool(_CERTIFICATION_CUE_RE.search(folded))
    is_education = bool(_EDUCATION_CUE_RE.search(folded))
    is_general_experience = bool(_GENERAL_EXPERIENCE_RE.search(folded))

    if item.kind == JDDraftCriterionKind.OTHER:
        return DroppedJDCriterionReason.UNSUPPORTED

    if (has_experience or has_duration) and not is_general_experience:
        expected = _expected_experience_kind(subject)
        if (
            expected is None
            or item.kind != expected
            or not _subjects_match(item.kind, item.requirement, subject)
        ):
            return DroppedJDCriterionReason.NEEDS_HUMAN_REVIEW
        if has_duration:
            if source_number is None or item.min_years != source_number:
                return DroppedJDCriterionReason.NEEDS_HUMAN_REVIEW
        elif item.min_years is not None:
            return DroppedJDCriterionReason.NEEDS_HUMAN_REVIEW
        return None

    if is_language:
        if item.kind != JDDraftCriterionKind.LANGUAGE or not _subjects_match(
            item.kind, item.requirement, subject
        ):
            return DroppedJDCriterionReason.NEEDS_HUMAN_REVIEW
        if source_level is not None:
            if (
                item.required_level is None
                or _normalize_source_text(item.required_level) != source_level
            ):
                return DroppedJDCriterionReason.NEEDS_HUMAN_REVIEW
            return None
        if item.required_level is not None:
            return DroppedJDCriterionReason.NEEDS_HUMAN_REVIEW
    elif is_certification:
        if item.kind != JDDraftCriterionKind.CERTIFICATION or not _subjects_match(
            item.kind, item.requirement, subject
        ):
            return DroppedJDCriterionReason.NEEDS_HUMAN_REVIEW
    elif is_education:
        if item.kind != JDDraftCriterionKind.EDUCATION or not _subjects_match(
            item.kind, item.requirement, subject
        ):
            return DroppedJDCriterionReason.NEEDS_HUMAN_REVIEW
    elif is_general_experience:
        if (
            item.kind != JDDraftCriterionKind.EXPERIENCE
            or not has_duration
            or source_number is None
            or item.min_years != source_number
        ):
            return DroppedJDCriterionReason.NEEDS_HUMAN_REVIEW
    else:
        if item.kind != JDDraftCriterionKind.SKILL or not _subjects_match(
            item.kind, item.requirement, subject
        ):
            return DroppedJDCriterionReason.NEEDS_HUMAN_REVIEW

    if item.min_years is not None and item.kind != JDDraftCriterionKind.EXPERIENCE:
        return DroppedJDCriterionReason.NEEDS_HUMAN_REVIEW
    if item.required_level is not None and item.kind != JDDraftCriterionKind.LANGUAGE:
        return DroppedJDCriterionReason.NEEDS_HUMAN_REVIEW
    return None


def _build_criterion_from_draft_item(
    item: JDDraftCriterionItem,
    *,
    criterion_type: CriterionType,
    used_ids: set[str],
    source_span: RequirementSpan | None,
) -> tuple[CriterionIn | None, DroppedJDCriterionReason | None]:
    """Re-validates one MODEL-PRODUCED draft item into a real CriterionIn —
    the exact same schema/prohibited-attribute denylist the manual
    vacancy-creation form and the REST API already enforce (D-031 point 4:
    an agent tool-dispatch layer is a new PRODUCER of arguments, never a
    new validator). Returns ``(None, reason)`` on validation failure —
    never a silent drop (PR #42 owner correction, issue #33): the caller
    discloses a PROHIBITED reason as a count only (the matched text itself
    must never be redisplayed), an UNGROUNDED reason as a count only (the
    text was never confirmed to actually be in HR's JD — D-046), and an
    UNSUPPORTED reason with the original, already-confirmed-non-sensitive,
    already-confirmed-grounded requirement text. Mirrors
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
    criterion, which the deterministic scorer must never do.

    A missing/unknown server span id gates every disclosure/scoring outcome
    as UNGROUNDED. A resolved item is checked only against that complete
    canonical occurrence. Prohibition authority comes exclusively from the
    raw JD/canonical server span in the dispatch boundary below; no
    model-authored field, including source_text, can manufacture or remove it.
    """
    if source_span is None:
        return None, DroppedJDCriterionReason.UNGROUNDED
    binding_reason = _canonical_binding_result(
        item, criterion_type=criterion_type, span=source_span
    )
    if binding_reason is not None:
        return None, binding_reason

    criterion: CriterionIn | None
    reason: DroppedJDCriterionReason | None
    if item.kind == JDDraftCriterionKind.OTHER:
        criterion, reason = None, DroppedJDCriterionReason.UNSUPPORTED
    else:
        kind = CriterionKind(item.kind.value)
        value = None if kind == CriterionKind.EXPERIENCE else item.requirement
        try:
            criterion = CriterionIn(
                id=slugify_criterion_label(item.requirement, used_ids),
                kind=kind,
                type=criterion_type,
                label=item.requirement,
                value=value,
                min_years=item.min_years,
                required_level=item.required_level,
                # Weight is deterministic product policy, not a JD field the
                # model is allowed to author.
                weight=1.0,
            )
            reason = None
        except ValidationError as exc:
            # A model_validator raising ProhibitedCriterionError (itself a
            # ValueError subclass) is always re-wrapped by pydantic into a
            # generic ValidationError before it reaches this except
            # clause — the original exception survives only inside each
            # error's own ``ctx["error"]`` (verified against pydantic
            # 2.11's actual behavior, not merely assumed). Unwrap it there
            # to tell a sensitive-attribute match apart from every other
            # validation failure (D-043, PR #42 owner correction, issue
            # #33).
            if any(
                isinstance(error.get("ctx", {}).get("error"), ProhibitedCriterionError)
                for error in exc.errors()
            ):
                return None, DroppedJDCriterionReason.PROHIBITED
            criterion, reason = None, DroppedJDCriterionReason.UNSUPPORTED
    return criterion, reason


def _build_authorized_semantic_draft(
    *,
    jd_text: str,
    title: str | None,
    requirements: list[SemanticRequirement],
    source_spans: list[RequirementSpan],
    requested_result_limit: int | None,
    result_limit: int,
    result_limit_was_bounded: bool,
    result_limit_needs_review: bool,
    ungrounded_count: int = 0,
    unsupported_language: SupportedInputLanguage | None = None,
    wrong_mode_guidance: bool = False,
) -> AgentToolResult:
    """Construct the public draft only from server-owned source slots.

    Model output is intentionally absent from every material field here.  The
    model may have proposed a classification upstream, but a criterion reaches
    this function only through an exact source occurrence and the deterministic
    semantic state assigned to it.
    """
    spans_by_id = {span.span_id: span for span in source_spans}
    used_ids: set[str] = set()
    must_have: list[CriterionIn] = []
    preferred: list[CriterionIn] = []
    unsupported: list[UnsupportedJDCriterionItem] = []
    needs_review: list[NeedsReviewJDCriterionItem] = []
    results: list[RequirementSpanResult] = []
    prohibited_count = 0

    for semantic in requirements:
        span = spans_by_id[semantic.requirement_span_id]
        state = semantic.state
        # Post-parse safety scan: neither a model family nor a normalized
        # subject can override protected text in the canonical source span.
        if find_prohibited_term(span.text, semantic.normalized_subject or ""):
            state = SemanticRequirementState.PROHIBITED

        criterion: CriterionIn | None = None
        if state == SemanticRequirementState.SCORABLE:
            assert semantic.criterion_family is not None
            assert semantic.criterion_type is not None
            kind = CriterionKind(semantic.criterion_family.value)
            subject = semantic.normalized_subject or span.text
            if semantic.criterion_family == JDDraftCriterionKind.DOMAIN_EXPERIENCE:
                subject = _canonical_display_subject(semantic.criterion_family, subject)
            value = None if kind == CriterionKind.EXPERIENCE else subject
            try:
                criterion = CriterionIn(
                    id=slugify_criterion_label(subject, used_ids),
                    kind=kind,
                    type=semantic.criterion_type,
                    label=subject,
                    value=value,
                    min_years=semantic.min_years,
                    required_level=semantic.required_level,
                    weight=1.0,
                )
            except ValidationError:
                state = SemanticRequirementState.NEEDS_HUMAN_REVIEW

        if criterion is not None:
            bucket = must_have if criterion.type == CriterionType.MUST_HAVE else preferred
            bucket.append(criterion)
        elif state == SemanticRequirementState.PROHIBITED:
            prohibited_count += 1
        elif state == SemanticRequirementState.UNSUPPORTED and semantic.criterion_type is not None:
            unsupported.append(
                UnsupportedJDCriterionItem(
                    requirement=span.text,
                    criterion_type=semantic.criterion_type,
                )
            )
        else:
            needs_review.append(
                NeedsReviewJDCriterionItem(
                    requirement=span.text,
                    criterion_type=semantic.criterion_type,
                    span_id=(
                        span.span_id
                        if state == SemanticRequirementState.NEEDS_HUMAN_REVIEW
                        and semantic.criterion_family is not None
                        and semantic.normalized_subject
                        else None
                    ),
                    kind=(
                        semantic.criterion_family
                        if state == SemanticRequirementState.NEEDS_HUMAN_REVIEW
                        and semantic.normalized_subject
                        else None
                    ),
                    subject=(
                        semantic.normalized_subject
                        if state == SemanticRequirementState.NEEDS_HUMAN_REVIEW
                        and semantic.normalized_subject
                        else None
                    ),
                    min_years=semantic.min_years,
                    required_level=semantic.required_level,
                    allowed_types=(
                        [CriterionType.MUST_HAVE, CriterionType.PREFERRED]
                        if state == SemanticRequirementState.NEEDS_HUMAN_REVIEW
                        and semantic.criterion_type is None
                        and semantic.criterion_family not in (None, JDDraftCriterionKind.OTHER)
                        else []
                    ),
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
                criterion_type=semantic.criterion_type,
                criterion_id=criterion.id if criterion is not None else None,
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
        ),
    )


def _build_authorized_model_draft(
    *, jd_text: str, draft: JDCriteriaDraft, source_spans: list[RequirementSpan]
) -> AgentToolResult:
    """Reconcile a model proposal against server-owned source occurrences."""
    safe_title = (
        draft.title
        if draft.title and _title_is_attributed(draft.title, jd_text)
        else "Vakansiya qaralaması"
    )
    used_ids: set[str] = set()
    must_have: list[CriterionIn] = []
    preferred: list[CriterionIn] = []
    unsupported: list[UnsupportedJDCriterionItem] = []
    needs_review: list[NeedsReviewJDCriterionItem] = []
    spans_by_id = {span.span_id: span for span in source_spans}
    span_states: dict[str, RequirementSpanState] = {}
    span_criterion_ids: dict[str, str] = {}
    raw_prohibited_spans = {
        span.span_id for span in source_spans if find_prohibited_term(span.text)
    }
    raw_jd_has_prohibited_text = find_prohibited_term(jd_text) is not None
    prohibited_count = max(len(raw_prohibited_spans), int(raw_jd_has_prohibited_text))
    span_states.update(
        {span_id: RequirementSpanState.PROHIBITED for span_id in raw_prohibited_spans}
    )
    ungrounded_count = 0
    for items, drafted_type in (
        (draft.must_have, CriterionType.MUST_HAVE),
        (draft.preferred, CriterionType.PREFERRED),
    ):
        for item in items:
            source_span = spans_by_id.get(item.span_id)
            if source_span is None and is_result_count_only(item.requirement):
                continue
            if source_span is not None and source_span.span_id in raw_prohibited_spans:
                continue
            if source_span is not None and item.span_id in span_states:
                ungrounded_count += 1
                continue
            canonical_item = (
                _canonicalize_supported_draft_shape(item, span=source_span)
                if source_span is not None
                else item
            )
            source_modality = explicit_modality(source_span.text) if source_span else None
            criterion_type = (
                CriterionType(source_modality)
                if source_modality is not None
                and canonical_item.kind
                in {
                    JDDraftCriterionKind.SKILL_EXPERIENCE,
                    JDDraftCriterionKind.DOMAIN_EXPERIENCE,
                }
                else drafted_type
            )
            criterion, reason = _build_criterion_from_draft_item(
                canonical_item,
                criterion_type=criterion_type,
                used_ids=used_ids,
                source_span=source_span,
            )
            if criterion is not None:
                bucket = must_have if criterion_type == CriterionType.MUST_HAVE else preferred
                bucket.append(criterion)
                assert source_span is not None
                span_states[source_span.span_id] = RequirementSpanState.SCORABLE
                span_criterion_ids[source_span.span_id] = criterion.id
            elif reason == DroppedJDCriterionReason.PROHIBITED:
                source_not_already_counted = (
                    source_span is not None and source_span.span_id not in raw_prohibited_spans
                ) or (source_span is None and not raw_jd_has_prohibited_text)
                if source_not_already_counted:
                    prohibited_count += 1
            elif reason == DroppedJDCriterionReason.UNGROUNDED:
                ungrounded_count += 1
            elif reason == DroppedJDCriterionReason.NEEDS_HUMAN_REVIEW:
                if source_span is not None:
                    span_states[source_span.span_id] = RequirementSpanState.NEEDS_HUMAN_REVIEW
                    supported_review = _review_item_for_supported_ambiguity(
                        canonical_item, span=source_span
                    )
                    if supported_review is not None:
                        needs_review.append(supported_review)
            else:
                assert source_span is not None
                unsupported.append(
                    UnsupportedJDCriterionItem(
                        requirement=source_span.text,
                        criterion_type=criterion_type,
                    )
                )
                span_states[source_span.span_id] = RequirementSpanState.UNSUPPORTED

    requirement_results: list[RequirementSpanResult] = []
    for source_span in source_spans:
        inferred_value = explicit_modality(source_span.text)
        inferred_type = CriterionType(inferred_value) if inferred_value is not None else None
        state = span_states.get(source_span.span_id, RequirementSpanState.NEEDS_HUMAN_REVIEW)
        if state == RequirementSpanState.NEEDS_HUMAN_REVIEW and not any(
            item.span_id == source_span.span_id for item in needs_review
        ):
            needs_review.append(
                NeedsReviewJDCriterionItem(
                    requirement=source_span.text, criterion_type=inferred_type
                )
            )
        requirement_results.append(
            RequirementSpanResult(
                span_id=source_span.span_id,
                start_offset=source_span.start_offset,
                end_offset=source_span.end_offset,
                text=None if state == RequirementSpanState.PROHIBITED else source_span.text,
                normalized=(
                    None if state == RequirementSpanState.PROHIBITED else source_span.normalized
                ),
                state=state,
                criterion_type=inferred_type,
                criterion_id=span_criterion_ids.get(source_span.span_id),
            )
        )

    result_count = extract_result_count_intent(jd_text)
    return AgentToolResult(
        tool_name=AgentActionType.DRAFT_JOB_CRITERIA,
        job_draft=AgentJobDraftToolResult(
            title=safe_title,
            draft_id=uuid.uuid4(),
            requested_result_limit=result_count.requested,
            result_limit=result_count.effective,
            result_limit_was_bounded=result_count.was_bounded,
            must_have=must_have,
            preferred=preferred,
            unsupported=unsupported,
            needs_review=needs_review,
            ungrounded_count=ungrounded_count,
            prohibited_count=prohibited_count,
            requirements=requirement_results,
        ),
    )


async def _dispatch_draft_job_criteria(llm: LLMProvider, *, jd_text: str) -> AgentToolResult | None:
    """Interpret a JD through source-bound server authority.

    Real local inference is still used for interpretation/title assistance,
    but provider failure cannot erase requirements the server can safely
    ground. Unsupported languages fail closed before inference and therefore
    before any criterion is created.
    """
    analysis = analyze_hr_text(jd_text)
    if analysis.language == SupportedInputLanguage.UNSUPPORTED:
        return _build_authorized_semantic_draft(
            jd_text=jd_text,
            title=None,
            requirements=[],
            source_spans=[],
            requested_result_limit=analysis.result_count.requested,
            result_limit=analysis.result_count.effective,
            result_limit_was_bounded=analysis.result_count.was_bounded,
            result_limit_needs_review=analysis.result_count_needs_review,
            unsupported_language=SupportedInputLanguage.UNSUPPORTED,
        )

    # Preserve the established model/source reconciliation contract for a
    # successful model response.  The semantic boundary is an independent
    # safe fallback and is also shared with ordinary search; it must not turn
    # a contradictory model proposal into a silently different criterion.
    source_spans = segment_requirement_spans(jd_text)
    folded_jd = _fold(jd_text)
    raw_has_prohibited_text = find_prohibited_term(jd_text) is not None
    simple_search_in_vacancy_mode = bool(
        not raw_has_prohibited_text
        and (
            not analysis.requirements
            or (
                len(analysis.requirements) == 1
                and analysis.requirements[0].criterion_family == JDDraftCriterionKind.SKILL
                and analysis.requirements[0].min_years is None
                and analysis.requirements[0].required_level is None
            )
        )
        and re.search(
            r"\b(?:show|find|display|list|return|search\s+for|goster\w*|tap\w*|"
            r"cixart\w*|axtar\w*)\b",
            folded_jd,
        )
        and re.search(r"\b(?:candidates?|applicants?|namized\w*)\b", folded_jd)
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
    draft: JDCriteriaDraft | None = None
    if not raw_has_prohibited_text and not simple_search_in_vacancy_mode:
        for attempt in range(1, MAX_JD_DRAFT_ATTEMPTS + 1):
            try:
                draft, _provenance = await llm.draft_job_criteria(
                    jd_text, requirement_spans=source_spans, repair=attempt > 1
                )
                break
            except ModelSchemaInvalidError:
                continue
            except (ModelTimeoutError, ModelUnavailableError, LLMProviderError):
                break

    known_span_ids = {span.span_id for span in source_spans}
    ungrounded_count = (
        sum(
            item.span_id not in known_span_ids and not is_result_count_only(item.requirement)
            for item in [*draft.must_have, *draft.preferred]
        )
        if draft is not None
        else 0
    )
    return _build_authorized_semantic_draft(
        jd_text=jd_text,
        title=draft.title if draft is not None else None,
        requirements=[] if simple_search_in_vacancy_mode else analysis.requirements,
        source_spans=[] if simple_search_in_vacancy_mode else analysis.spans,
        requested_result_limit=analysis.result_count.requested,
        result_limit=analysis.result_count.effective,
        result_limit_was_bounded=analysis.result_count.was_bounded,
        result_limit_needs_review=analysis.result_count_needs_review,
        # This is disclosure only: model proposals never participate in
        # material authority. Count genuinely source-unbound proposals, but
        # exclude result-count intent because it is intentionally separate
        # from canonical requirement spans.
        ungrounded_count=ungrounded_count,
        wrong_mode_guidance=simple_search_in_vacancy_mode,
    )


def resolve_job_draft_review_modality(
    draft: AgentJobDraftToolResult,
    *,
    span_id: str,
    criterion_type: CriterionType,
) -> AgentJobDraftToolResult:
    """Resolve only a server-declared modality ambiguity for one span."""
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
    }
    if criterion_type == CriterionType.MUST_HAVE:
        updates["must_have"] = [*draft.must_have, criterion]
    else:
        updates["preferred"] = [*draft.preferred, criterion]
    return draft.model_copy(update=updates)


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


def _latest_pending_job_draft(conversation: AgentConversation) -> AgentJobDraftToolResult | None:
    for turn in reversed(conversation.turns):
        payload = turn.get("pending_job_draft")
        if not isinstance(payload, dict):
            continue
        try:
            return AgentJobDraftToolResult.model_validate(payload)
        except ValueError:
            continue
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

    levels = re.findall(r"(?i)\b(?:a1|a2|b1|b2|c1|c2)\b", user_message)
    if target.kind == CriterionKind.LANGUAGE and len(levels) >= 2:
        if (
            target.required_level is None
            or target.required_level.casefold() != levels[0].casefold()
        ):
            return None
        replacement = target.model_copy(update={"required_level": levels[-1].upper()})
        return draft.model_copy(
            update={
                "draft_id": uuid.uuid4(),
                "must_have": [
                    replacement if item.id == target.id else item for item in draft.must_have
                ],
                "preferred": [
                    replacement if item.id == target.id else item for item in draft.preferred
                ],
                "modification_source_text": user_message,
            }
        )

    if target.kind in (CriterionKind.SKILL_EXPERIENCE, CriterionKind.DOMAIN_EXPERIENCE):
        numeric = [
            float(value.replace(",", ".")) for value in re.findall(r"\d+(?:[.,]\d+)?", folded)
        ]
        if len(numeric) >= 2 and target.min_years == numeric[0]:
            replacement = target.model_copy(update={"min_years": numeric[-1]})
            return draft.model_copy(
                update={
                    "draft_id": uuid.uuid4(),
                    "must_have": [
                        replacement if item.id == target.id else item for item in draft.must_have
                    ],
                    "preferred": [
                        replacement if item.id == target.id else item for item in draft.preferred
                    ],
                    "modification_source_text": user_message,
                }
            )

    if requested_type is not None and requested_type != target.type:
        replacement = target.model_copy(update={"type": requested_type})
        updated_must_have = [item for item in draft.must_have if item.id != target.id]
        updated_preferred = [item for item in draft.preferred if item.id != target.id]
        if requested_type == CriterionType.MUST_HAVE:
            updated_must_have.append(replacement)
        else:
            updated_preferred.append(replacement)
        return draft.model_copy(
            update={
                "draft_id": uuid.uuid4(),
                "must_have": updated_must_have,
                "preferred": updated_preferred,
                "requirements": _modified_requirement_results(
                    draft, criterion_id=target.id, criterion_type=requested_type
                ),
                "modification_source_text": user_message,
            }
        )
    # An explicit no-op or ambiguous direction fails truthfully. It must not
    # mint a fresh draft id that implies a mutation was applied.
    return None


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
        # Session/tenant-scoped server authority used by the dedicated
        # draft-confirmation operation. Browser fields never recreate it.
        assistant_turn["pending_job_draft"] = pending_drafts[-1].model_dump(mode="json")
    turns = [*turns, assistant_turn][-max_context_turns:]
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
    pending_draft = _latest_pending_job_draft(conversation)
    if (
        explicit_action is None
        and pending_draft is not None
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
            tenant_id=tenant_id,
            turns=turns,
            last_search_candidate_ids=last_search_candidate_ids,
            max_context_turns=max_context_turns,
            result=result,
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
                        recent_turns=[(t["role"], t["text"]) for t in turns[-max_context_turns:]],
                        last_tool_result_summary=last_tool_summary,
                        available_candidate_refs=list(range(1, len(last_search_candidate_ids) + 1)),
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
