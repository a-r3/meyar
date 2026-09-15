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


# --- D-046 (PR #42 owner correction, issue #33): JD requirement grounding ---
#
# Real-Ollama acceptance testing surfaced a genuine content-fabrication
# defect distinct from D-042/D-045's routing/classification findings: a
# short or underspecified JD reliably gets a plausible-sounding but wholly
# unstated requirement "filled in" from the model's own prior/training
# knowledge about what a role "typically" requires (e.g. "İngilis dili"
# fabricated onto a JD that never mentions language; the reported
# "Passing an exam" fabricated onto a JD that only states travel
# readiness). The JD_CRITERIA_DRAFT_SYSTEM_PROMPT already instructs "only
# include a requirement that is actually stated in the text — never
# invent one," and this is unreliable at this model size, matching the
# project's established practice (D-042 point 6) of not expecting a
# stronger prompt alone to fix a small-model behavior — so this is a
# deterministic post-hoc check, not another prompt iteration.
#
# Generic connector/particle words carry no grounding signal either way
# (they appear in nearly every requirement sentence regardless of actual
# content) and are excluded before comparison.
_GROUNDING_STOPWORDS = frozenset(
    {
        "ve",
        "ya",
        "ki",
        "bir",
        "bu",
        "da",
        "de",
        "ile",
        "ucun",
        "uzre",
        "olan",
        "olmaq",
        "olmalidir",
        "olmalidi",
        "etmek",
        "edir",
        "gore",
        "haqqinda",
        "arasinda",
        "hem",
        "yalniz",
        "cox",
        "daha",
        "kimi",
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "for",
        "in",
        "on",
        "with",
    }
)


def _grounding_tokens(text: str) -> list[str]:
    """Folded, stopword-filtered word/number tokens used only for the
    requirement-grounding check below — deliberately coarser than
    slugify_criterion_label (which must produce a stable id, not a
    comparison signal)."""
    folded = _fold(text)
    words = re.findall(r"[a-z0-9]+", folded)
    return [word for word in words if word not in _GROUNDING_STOPWORDS]


def _is_requirement_grounded_in_jd_text(requirement: str, jd_text: str) -> bool:
    """Deterministic lexical check that a model-drafted requirement is
    actually traceable to the JD's own text, rather than fabricated. Fold-
    tolerant (Azerbaijani suffix variance survives fold_az_ascii/
    normalize_azerbaijani_case, e.g. "ezamiyyətə" vs "ezamiyyət") and
    requires at least half of the requirement's own non-connector words to
    appear as a substring of the JD text. A requirement with no comparable
    content word at all (only connector words, or an empty token set)
    is treated as ungrounded — a real JD requirement always names at
    least one concrete term (a skill, a duration, a credential, an
    activity), so the absence of any such term is itself signal, not a
    false negative to guard against."""
    tokens = _grounding_tokens(requirement)
    if not tokens:
        return False
    jd_folded = _fold(jd_text)
    matched = sum(1 for token in tokens if token in jd_folded)
    return matched * 2 >= len(tokens)


# Issue #44: source fragments are the unit of JD factual authority.  The
# splitter is deliberately bounded and conservative: explicit sentences,
# semicolon/newline clauses, and bullet rows with requirement cues become
# reviewable units.  It does not try to perform general entailment.
_SOURCE_SPLIT_RE = re.compile(r"(?:\r?\n)+|(?<=[.!?;])\s+")
_REQUIREMENT_CUE_RE = re.compile(
    r"\b(required|requirement|must|mandatory|minimum|preferred|nice\s+to\s+have|"
    r"experience|years?|proficiency|level|degree|certificat(?:e|ion)|language|"
    r"license|licence|willing|available|teleb\w*|mutleq\w*|vacib\w*|ustunluk\w*|arzuolunan\w*|"
    r"tecrube|il|dil|sertifikat|bakalavr|magistr|[a-z]+m[ae]lidir)\b",
    re.IGNORECASE,
)
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
_FIELD_WORDS = _GROUNDING_STOPWORDS | frozenset(
    {
        "required",
        "requirement",
        "must",
        "mandatory",
        "minimum",
        "preferred",
        "nice",
        "have",
        "experience",
        "years",
        "year",
        "yrs",
        "proficiency",
        "level",
        "teleb",
        "mutleq",
        "vacib",
        "olmalidir",
        "olmalidi",
        "ustunluk",
        "arzuolunan",
        "tecrube",
        "il",
        "knowledge",
        "knowledgeable",
        "bacariq",
        "bilik",
        "olunur",
        "olaraq",
        "lazimdir",
        "namized",
        "candidate",
    }
)


def _normalize_source_text(text: str) -> str:
    return " ".join(_fold(text).split())


def _extract_source_requirement_spans(jd_text: str) -> list[str]:
    spans: list[str] = []
    for raw in _SOURCE_SPLIT_RE.split(jd_text):
        span = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", raw).strip(" \t.;")
        if not span:
            continue
        folded = _normalize_source_text(span)
        is_bullet = bool(re.match(r"^\s*(?:[-*•]|\d+[.)])\s*", raw))
        if is_bullet or _REQUIREMENT_CUE_RE.search(folded) or _LANGUAGE_LEVEL_RE.search(folded):
            spans.append(span)
    return spans


def _source_span_index(source_text: str, spans: list[str]) -> int | None:
    needle = _normalize_source_text(source_text)
    if not needle:
        return None
    for index, span in enumerate(spans):
        haystack = _normalize_source_text(span)
        if needle == haystack or needle in haystack:
            return index
    return None


def _criterion_type_from_source(source_text: str) -> CriterionType | None:
    folded = _normalize_source_text(source_text)
    required = bool(_REQUIRED_CUE_RE.search(folded))
    preferred = bool(_PREFERRED_CUE_RE.search(folded))
    if required == preferred:
        return None
    return CriterionType.MUST_HAVE if required else CriterionType.PREFERRED


def _number_is_attributed(value: float, source_text: str) -> bool:
    for raw in re.findall(r"(?<![\w.])\d+(?:[.,]\d+)?(?![\w.])", source_text):
        try:
            if abs(float(raw.replace(",", ".")) - value) < 1e-9:
                return True
        except ValueError:
            continue
    return False


def _material_tokens_are_attributed(value: str, source_text: str) -> bool:
    tokens = [token for token in _grounding_tokens(value) if token not in _FIELD_WORDS]
    source_tokens = set(_grounding_tokens(source_text))

    def attributed(token: str) -> bool:
        return any(
            token == source_token
            or (
                min(len(token), len(source_token)) >= 4
                and (token.startswith(source_token) or source_token.startswith(token))
            )
            for source_token in source_tokens
        )

    return bool(tokens) and all(attributed(token) for token in tokens)


def _source_scope_is_preserved(item: JDDraftCriterionItem, source_text: str) -> bool:
    values = f"{item.requirement} {item.required_level or ''}"
    value_tokens = _grounding_tokens(values)

    def is_generic(token: str) -> bool:
        return (
            token in _FIELD_WORDS
            or token.isdigit()
            or token.startswith(("teleb", "mutleq", "vacib", "ustunluk", "arzuolunan"))
            or token.endswith(("malidir", "melidir", "malidi", "melidi"))
        )

    source_subject = [token for token in _grounding_tokens(source_text) if not is_generic(token)]
    return all(
        any(
            token == value_token
            or (
                min(len(token), len(value_token)) >= 4
                and (token.startswith(value_token) or value_token.startswith(token))
            )
            for value_token in value_tokens
        )
        for token in source_subject
    )


def _field_binding_is_valid(
    item: JDDraftCriterionItem, *, criterion_type: CriterionType, source_text: str
) -> bool:
    """Validate material semantics against one attributable source span.

    This is intentionally lexical and structural, not NLI: every subject/
    level token and number must occur in the same source fragment, modality
    must be explicit, and a qualified duration cannot be converted to total
    experience or bare presence.
    """
    if _criterion_type_from_source(source_text) != criterion_type:
        return False
    if not _material_tokens_are_attributed(item.requirement, source_text):
        return False
    if item.kind != JDDraftCriterionKind.OTHER and not _source_scope_is_preserved(
        item, source_text
    ):
        return False

    folded = _normalize_source_text(source_text)
    has_duration = bool(_DURATION_CUE_RE.search(folded) and re.search(r"\d", folded))
    if item.min_years is not None:
        if not has_duration or not _number_is_attributed(item.min_years, source_text):
            return False
    if item.required_level is not None:
        if not _material_tokens_are_attributed(item.required_level, source_text):
            return False
    elif item.kind == JDDraftCriterionKind.LANGUAGE and _LANGUAGE_LEVEL_RE.search(folded):
        return False

    if item.kind == JDDraftCriterionKind.EXPERIENCE:
        if item.min_years is None:
            # A source that states experience without a duration is real but
            # not representable by CriterionIn.EXPERIENCE; let schema
            # validation classify it UNSUPPORTED. If the source did state a
            # number, omission of that number is a material-field mismatch.
            return not has_duration
        # An explicit general/total marker is always sufficient. Otherwise
        # only a span with no named subject beyond duration/modality words is
        # general experience. "5 years Python experience" is therefore not.
        if not _GENERAL_EXPERIENCE_RE.search(folded):
            remaining = [
                t
                for t in _grounding_tokens(source_text)
                if t not in _FIELD_WORDS and not t.isdigit()
            ]
            if remaining:
                return False
    elif item.kind in (
        JDDraftCriterionKind.SKILL_EXPERIENCE,
        JDDraftCriterionKind.DOMAIN_EXPERIENCE,
    ):
        if item.min_years is None:
            return not has_duration
        if not has_duration:
            return False
    elif item.min_years is not None or has_duration:
        # Never discard or transfer a duration by accepting only the bare
        # subject under a non-duration kind.
        return False

    if item.kind != JDDraftCriterionKind.LANGUAGE and item.required_level is not None:
        return False
    if item.kind == JDDraftCriterionKind.SKILL and (
        _CERTIFICATION_CUE_RE.search(folded)
        or _EDUCATION_CUE_RE.search(folded)
        or _LANGUAGE_CUE_RE.search(folded)
    ):
        return False
    if item.kind == JDDraftCriterionKind.CERTIFICATION and not _CERTIFICATION_CUE_RE.search(folded):
        return False
    if item.kind == JDDraftCriterionKind.EDUCATION and not _EDUCATION_CUE_RE.search(folded):
        return False
    if item.kind == JDDraftCriterionKind.LANGUAGE and not (
        _LANGUAGE_CUE_RE.search(folded) or _LANGUAGE_LEVEL_RE.search(folded)
    ):
        return False
    return True


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


def _build_criterion_from_draft_item(
    item: JDDraftCriterionItem,
    *,
    criterion_type: CriterionType,
    used_ids: set[str],
    source_text: str | None,
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

    The grounding check (D-046) runs LAST and gates BOTH remaining
    disclosure outcomes uniformly: a valid CriterionIn (which would be
    shown as a real, scored criterion) and an UNSUPPORTED classification
    (which discloses the requirement's own text to HR as a real JD
    requirement the system merely cannot score) — a requirement reaching
    either of those without a lexical trace in the JD text is reclassified
    UNGROUNDED instead, count-only, never disclosed. PROHIBITED is an
    unconditional early return above and is never reordered or gated by
    this check — a doubly-bad item (both fabricated and sensitive) is
    still counted PROHIBITED, exactly as before this check existed."""
    # Prohibition is classification-independent and runs before OTHER or
    # any other kind branch. A model cannot relabel sensitive text into a
    # safe bucket. Check every model-authored field that can carry it.
    if find_prohibited_term(item.requirement, item.source_text, item.required_level or ""):
        return None, DroppedJDCriterionReason.PROHIBITED
    if source_text is None:
        return None, DroppedJDCriterionReason.UNGROUNDED
    if not _field_binding_is_valid(item, criterion_type=criterion_type, source_text=source_text):
        return None, DroppedJDCriterionReason.NEEDS_HUMAN_REVIEW

    # These evaluator fields cannot currently survive the agent review
    # form unchanged: it has no required_level control and exposes only
    # general EXPERIENCE among duration kinds. Keep them visible and
    # unscored instead of silently erasing or weakening their semantics.
    if item.required_level is not None or item.kind in (
        JDDraftCriterionKind.SKILL_EXPERIENCE,
        JDDraftCriterionKind.DOMAIN_EXPERIENCE,
    ):
        return None, DroppedJDCriterionReason.UNSUPPORTED

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


async def _dispatch_draft_job_criteria(llm: LLMProvider, *, jd_text: str) -> AgentToolResult | None:
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
    safe_title = (
        draft.title
        if draft.title and _material_tokens_are_attributed(draft.title, jd_text)
        else "Vakansiya qaralaması"
    )

    used_ids: set[str] = set()
    must_have: list[CriterionIn] = []
    preferred: list[CriterionIn] = []
    unsupported: list[UnsupportedJDCriterionItem] = []
    needs_review: list[NeedsReviewJDCriterionItem] = []
    source_spans = _extract_source_requirement_spans(jd_text)
    covered_source_spans: set[int] = set()
    raw_prohibited_spans = {
        index for index, span in enumerate(source_spans) if find_prohibited_term(span)
    }
    raw_jd_has_prohibited_text = find_prohibited_term(jd_text) is not None
    prohibited_count = max(len(raw_prohibited_spans), int(raw_jd_has_prohibited_text))
    covered_source_spans.update(raw_prohibited_spans)
    ungrounded_count = 0
    for items, criterion_type, bucket in (
        (draft.must_have, CriterionType.MUST_HAVE, must_have),
        (draft.preferred, CriterionType.PREFERRED, preferred),
    ):
        for item in items:
            source_index = _source_span_index(item.source_text, source_spans)
            source_text = item.source_text if source_index is not None else None
            covers_whole_span = bool(
                source_index is not None
                and _normalize_source_text(item.source_text)
                == _normalize_source_text(source_spans[source_index])
            )
            criterion, reason = _build_criterion_from_draft_item(
                item,
                criterion_type=criterion_type,
                used_ids=used_ids,
                source_text=source_text,
            )
            if criterion is not None:
                bucket.append(criterion)
                assert source_index is not None
                if covers_whole_span and source_index is not None:
                    covered_source_spans.add(source_index)
            elif reason == DroppedJDCriterionReason.PROHIBITED:
                if (
                    source_index is not None
                    and source_index not in raw_prohibited_spans
                ) or (source_index is None and not raw_jd_has_prohibited_text):
                    prohibited_count += 1
                if covers_whole_span and source_index is not None:
                    covered_source_spans.add(source_index)
            elif reason == DroppedJDCriterionReason.UNGROUNDED:
                ungrounded_count += 1
            elif reason == DroppedJDCriterionReason.NEEDS_HUMAN_REVIEW:
                review_text = source_text or item.source_text
                if not find_prohibited_term(review_text) and all(
                    existing.requirement != review_text for existing in needs_review
                ):
                    needs_review.append(
                        NeedsReviewJDCriterionItem(
                            requirement=review_text, criterion_type=criterion_type
                        )
                    )
                if covers_whole_span and source_index is not None:
                    covered_source_spans.add(source_index)
            else:
                unsupported.append(
                    UnsupportedJDCriterionItem(
                        requirement=source_text or item.requirement,
                        criterion_type=criterion_type,
                    )
                )
                if covers_whole_span and source_index is not None:
                    covered_source_spans.add(source_index)

    # Reconcile source requirements after processing the model draft. A
    # material source span the model omitted is retained for HR review and
    # never becomes a CriterionIn or score input.
    for index, source_span in enumerate(source_spans):
        if index in covered_source_spans:
            continue
        inferred_type = _criterion_type_from_source(source_span)
        needs_review.append(
            NeedsReviewJDCriterionItem(requirement=source_span, criterion_type=inferred_type)
        )

    return AgentToolResult(
        tool_name=AgentActionType.DRAFT_JOB_CRITERIA,
        job_draft=AgentJobDraftToolResult(
            title=safe_title,
            must_have=must_have,
            preferred=preferred,
            unsupported=unsupported,
            needs_review=needs_review,
            ungrounded_count=ungrounded_count,
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
        {
            "role": "assistant",
            "text": result.message or "",
            "outcome": result.outcome.value,
            "text_authority": ASSISTANT_TEXT_AUTHORITY_SERVER,
            "text_authority_version": ASSISTANT_TEXT_AUTHORITY_VERSION,
        },
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
