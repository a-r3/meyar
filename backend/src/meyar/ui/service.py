import hashlib
import json
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.agent.schemas import AgentActionType, AgentJobDraftToolResult, AgentTurnResult
from meyar.core.text import (
    combine_degree_and_field,
    fold_az_ascii,
    normalize_azerbaijani_case,
    slugify_criterion_label,
)
from meyar.evaluation.evaluators import evaluate_criterion
from meyar.evaluation.normalization import normalize_certification_name, normalize_text
from meyar.ingestion.parser import CanonicalDocumentContent
from meyar.models.candidate import Candidate
from meyar.models.candidate_document import (
    PARSER_STATUS_PARSE_FAILED,
    PARSER_STATUS_PARSED,
    PARSER_STATUS_PENDING,
    CandidateDocument,
)
from meyar.models.candidate_identity_version import CandidateIdentityVersion
from meyar.models.candidate_profile_version import (
    PROFILE_STATUS_COMPLETED,
    PROFILE_STATUS_FAILED,
    PROFILE_STATUS_MANUAL_REVIEW_REQUIRED,
    CandidateProfileVersion,
)
from meyar.models.evaluation import Evaluation
from meyar.models.folder_indexed_file import (
    INDEX_STATUS_FAILED,
    INDEX_STATUS_INDEXED,
    INDEX_STATUS_MISSING,
    FolderIndexedFile,
)
from meyar.models.job import JOB_STATUS_ACTIVE, Job
from meyar.models.job_criteria_version import JobCriteriaVersion
from meyar.schemas.candidate_profile import CandidateProfileExtraction, EvidenceRef
from meyar.schemas.criteria import CriterionIn, CriterionKind, CriterionType
from meyar.schemas.job import JobCreateRequest
from meyar.scoring.schemas import BatchRankingResult
from meyar.search.schemas import (
    CandidateSearchResponse,
    PreferredFilterMatch,
    RequiredFilterMatch,
)
from meyar.services.candidate_document_repo import (
    get_candidate_document,
    get_latest_canonical_document,
    list_candidate_documents,
)
from meyar.services.candidate_identity_repo import get_current_identity_version
from meyar.services.candidate_profile_repo import get_current_profile_version
from meyar.services.candidate_repo import get_candidate
from meyar.services.identity_authority import (
    get_current_identity_values,
    identity_values_from_version,
)
from meyar.services.profile_authority import (
    ProfileAuthorityError,
    authorize_profile_version,
    get_authorized_profile_version_by_id,
)
from meyar.ui.presentation import (
    AGENT_EVIDENCE_CATEGORY_LABELS,
    CRITERION_KIND_LABELS,
    agent_turn_outcome_message,
    criterion_explanation_az,
    join_nonempty,
    planner_outcome_view,
)
from meyar.ui.view_models import (
    AgentCandidateProfileView,
    AgentEvidenceMatchView,
    AgentEvidenceView,
    AgentJobDraftReviewView,
    AgentJobDraftView,
    AgentToolResultView,
    AgentTurnView,
    CandidateDetailView,
    CandidateDocumentPreviewView,
    CandidateDocumentView,
    CandidateLibraryItemView,
    CandidateLibraryPageView,
    CandidateSearchResultView,
    CriterionRowView,
    DocumentPreviewPageView,
    EvaluationHistoryView,
    EvidenceLocationView,
    JobView,
    ProfileFactView,
    RankedCandidateView,
    ScoreContributionView,
)

DEFAULT_LIBRARY_PAGE_SIZE = 25
MAX_LIBRARY_PAGE_SIZE = 50
ALLOWED_PARSER_STATUSES = frozenset(
    {PARSER_STATUS_PENDING, PARSER_STATUS_PARSED, PARSER_STATUS_PARSE_FAILED}
)
ALLOWED_PROFILE_STATUSES = frozenset(
    {PROFILE_STATUS_COMPLETED, PROFILE_STATUS_FAILED, PROFILE_STATUS_MANUAL_REVIEW_REQUIRED}
)
ALLOWED_FOLDER_STATUSES = frozenset(
    {INDEX_STATUS_INDEXED, INDEX_STATUS_FAILED, INDEX_STATUS_MISSING}
)


class UIServiceInputError(ValueError):
    pass


def _validated_filter(value: str | None, allowed: frozenset[str], label: str) -> str | None:
    normalized = value.strip().upper() if value else None
    if normalized and normalized not in allowed:
        raise UIServiceInputError(f"Unsupported {label} state.")
    return normalized


def _format_filter_match_label(category: str, value: str) -> str:
    """HR-facing phrasing for one matched search filter — never the raw
    internal category key a `RequiredFilterMatch`/`PreferredFilterMatch`
    carries (e.g. category="skill" reads as developer taxonomy, not HR
    language; see D-044, PR #42 owner UX correction). The matched value
    itself (a skill/certification/language/education name) is already
    self-descriptive to an HR reader, so most categories need no prefix
    at all — only the numeric experience-years category needs a unit
    appended to stay readable."""
    if category == "min_total_experience_years":
        return f"{value} il təcrübə"
    return value


def _evidence_views(
    evidence: list[EvidenceRef], *, snippets: bool, maximum: int = 4
) -> list[EvidenceLocationView]:
    """Deduplicate one immutable profile source by exact evidence occurrence.

    Page alone is never an identity: different quotes on one page remain
    visible.  Within the caller's already-authorized profile version, page,
    block and normalized verbatim quote identify the effective occurrence.
    """
    seen: set[tuple[int, int, str]] = set()
    views: list[EvidenceLocationView] = []
    for item in evidence:
        quote = item.quote[:240] if snippets else None
        normalized_quote = " ".join((quote or "").split()).casefold()
        key = (item.page, item.block_index, normalized_quote)
        if key in seen:
            continue
        seen.add(key)
        views.append(
            EvidenceLocationView(page=item.page, block_index=item.block_index, snippet=quote)
        )
        if len(views) >= maximum:
            break
    return views


def _facts(profile: CandidateProfileExtraction) -> dict[str, list[ProfileFactView]]:
    return {
        "skills": [
            ProfileFactView(
                title=item.name,
                detail=item.category,
                evidence=_evidence_views(item.evidence, snippets=True),
            )
            for item in profile.skills
        ],
        "experience": [
            ProfileFactView(
                title=item.title,
                detail=join_nonempty(
                    [
                        item.organization,
                        join_nonempty([item.start_date, item.end_date], separator=" — "),
                    ]
                ),
                evidence=_evidence_views(item.evidence, snippets=True),
            )
            for item in profile.employment_history
        ],
        "education": [
            ProfileFactView(
                title=combine_degree_and_field(item.degree, item.field_of_study)
                or "Təhsil məlumatı",
                detail=join_nonempty([item.institution, item.date]),
                evidence=_evidence_views(item.evidence, snippets=True),
            )
            for item in profile.education
        ],
        "languages": [
            ProfileFactView(
                title=item.language,
                detail=item.proficiency,
                evidence=_evidence_views(item.evidence, snippets=True),
            )
            for item in profile.languages
        ],
        "certifications": [
            ProfileFactView(
                title=item.name,
                detail=join_nonempty([item.issuer, item.date]),
                evidence=_evidence_views(item.evidence, snippets=True),
            )
            for item in profile.certifications
        ],
        "projects": [
            ProfileFactView(
                title=item.description,
                evidence=_evidence_views(item.evidence, snippets=True),
            )
            for item in profile.projects
        ],
    }


def _current_role_and_skills(
    profile: CandidateProfileExtraction, *, skill_limit: int = 5
) -> tuple[str | None, list[str]]:
    current_role = profile.employment_history[0].title if profile.employment_history else None
    top_skills = [item.name for item in profile.skills[:skill_limit]]
    return current_role, top_skills


def _library_profile_summary(
    profile: CandidateProfileExtraction | None,
) -> tuple[str | None, list[str], list[str]]:
    """HR-facing summary derived from already-fetched profile_content — no
    extra query. Returns (current_role, top_skills, languages)."""
    if profile is None:
        return None, [], []
    current_role, top_skills = _current_role_and_skills(profile)
    languages = [item.language for item in profile.languages]
    return current_role, top_skills, languages


async def list_candidate_library(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    page: int = 1,
    page_size: int = DEFAULT_LIBRARY_PAGE_SIZE,
    parser_status: str | None = None,
    profile_status: str | None = None,
    folder_status: str | None = None,
) -> CandidateLibraryPageView:
    """Read-only tenant library with operational browse ordering."""
    page = max(page, 1)
    page_size = min(max(page_size, 1), MAX_LIBRARY_PAGE_SIZE)
    parser_status = _validated_filter(parser_status, ALLOWED_PARSER_STATUSES, "parser")
    profile_status = _validated_filter(profile_status, ALLOWED_PROFILE_STATUSES, "profile")
    folder_status = _validated_filter(folder_status, ALLOWED_FOLDER_STATUSES, "folder")

    latest_profile = (
        select(
            CandidateProfileVersion.candidate_id,
            func.max(CandidateProfileVersion.version_number).label("max_version"),
        )
        .where(CandidateProfileVersion.tenant_id == tenant_id)
        .group_by(CandidateProfileVersion.candidate_id)
        .subquery()
    )
    conditions = [Candidate.tenant_id == tenant_id]
    if parser_status:
        conditions.append(
            exists().where(
                CandidateDocument.tenant_id == tenant_id,
                CandidateDocument.candidate_id == Candidate.id,
                CandidateDocument.parser_status == parser_status,
            )
        )
    if folder_status:
        conditions.append(
            exists().where(
                FolderIndexedFile.tenant_id == tenant_id,
                FolderIndexedFile.candidate_id == Candidate.id,
                FolderIndexedFile.index_status == folder_status,
            )
        )
    if profile_status:
        conditions.append(
            exists().where(
                CandidateProfileVersion.tenant_id == tenant_id,
                CandidateProfileVersion.candidate_id == Candidate.id,
                latest_profile.c.candidate_id == Candidate.id,
                CandidateProfileVersion.version_number == latest_profile.c.max_version,
                CandidateProfileVersion.status == profile_status,
            )
        )

    total = int(
        await db.scalar(select(func.count()).select_from(Candidate).where(*conditions)) or 0
    )
    candidates = list(
        (
            await db.execute(
                select(Candidate)
                .where(*conditions)
                .order_by(Candidate.created_at.desc(), Candidate.id.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    candidate_ids = [candidate.id for candidate in candidates]
    if not candidate_ids:
        return CandidateLibraryPageView(
            items=[],
            page=page,
            page_size=page_size,
            total=total,
            has_previous=page > 1,
            has_next=False,
        )

    profiles = list(
        (
            await db.execute(
                select(CandidateProfileVersion)
                .join(
                    latest_profile,
                    (CandidateProfileVersion.candidate_id == latest_profile.c.candidate_id)
                    & (CandidateProfileVersion.version_number == latest_profile.c.max_version),
                )
                .where(
                    CandidateProfileVersion.tenant_id == tenant_id,
                    CandidateProfileVersion.candidate_id.in_(candidate_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    latest_identity = (
        select(
            CandidateIdentityVersion.candidate_id,
            func.max(CandidateIdentityVersion.version_number).label("max_version"),
        )
        .where(CandidateIdentityVersion.tenant_id == tenant_id)
        .group_by(CandidateIdentityVersion.candidate_id)
        .subquery()
    )
    identities = list(
        (
            await db.execute(
                select(CandidateIdentityVersion)
                .join(
                    latest_identity,
                    (CandidateIdentityVersion.candidate_id == latest_identity.c.candidate_id)
                    & (CandidateIdentityVersion.version_number == latest_identity.c.max_version),
                )
                .where(
                    CandidateIdentityVersion.tenant_id == tenant_id,
                    CandidateIdentityVersion.candidate_id.in_(candidate_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    documents = list(
        (
            await db.execute(
                select(CandidateDocument).where(
                    CandidateDocument.tenant_id == tenant_id,
                    CandidateDocument.candidate_id.in_(candidate_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    folder_rows = list(
        (
            await db.execute(
                select(FolderIndexedFile).where(
                    FolderIndexedFile.tenant_id == tenant_id,
                    FolderIndexedFile.candidate_id.in_(candidate_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    profiles_by_candidate = {item.candidate_id: item for item in profiles}
    identities_by_candidate = {item.candidate_id: item for item in identities}
    parser_states: defaultdict[uuid.UUID, set[str]] = defaultdict(set)
    folder_states: defaultdict[uuid.UUID, set[str]] = defaultdict(set)
    for document in documents:
        parser_states[document.candidate_id].add(document.parser_status)
    for row in folder_rows:
        if row.candidate_id is not None:
            folder_states[row.candidate_id].add(row.index_status)

    items: list[CandidateLibraryItemView] = []
    for candidate in candidates:
        profile_version = profiles_by_candidate.get(candidate.id)
        profile = None
        profile_authorized = False
        if profile_version is not None:
            try:
                profile = await authorize_profile_version(db, version=profile_version)
                profile_authorized = True
            except ProfileAuthorityError:
                profile = None
        identity = await identity_values_from_version(
            db, version=identities_by_candidate.get(candidate.id)
        )
        current_role, top_skills, languages = _library_profile_summary(profile)
        items.append(
            CandidateLibraryItemView(
                candidate_id=candidate.id,
                created_at=candidate.created_at,
                full_name=identity.full_name,
                current_role=current_role,
                top_skills=top_skills,
                languages=languages,
                current_profile_version=profile_version.version_number if profile_version else None,
                current_profile_status=(
                    None
                    if profile_version is None
                    else (
                        "UNAVAILABLE"
                        if profile_version.status == PROFILE_STATUS_COMPLETED
                        and not profile_authorized
                        else profile_version.status
                    )
                ),
                parser_statuses=sorted(parser_states[candidate.id]),
                folder_index_statuses=sorted(folder_states[candidate.id]),
            )
        )
    return CandidateLibraryPageView(
        items=items,
        page=page,
        page_size=page_size,
        total=total,
        has_previous=page > 1,
        has_next=page * page_size < total,
    )


async def get_candidate_detail_view(
    db: AsyncSession, *, tenant_id: uuid.UUID, candidate_id: uuid.UUID
) -> CandidateDetailView | None:
    candidate = await get_candidate(db, tenant_id=tenant_id, candidate_id=candidate_id)
    if candidate is None:
        return None
    identity = await get_current_identity_version(
        db, tenant_id=tenant_id, candidate_id=candidate_id
    )
    identity_values = await identity_values_from_version(db, version=identity)
    profile_version = await get_current_profile_version(
        db, tenant_id=tenant_id, candidate_id=candidate_id
    )
    facts: dict[str, list[ProfileFactView]] = {
        key: []
        for key in (
            "skills",
            "experience",
            "education",
            "languages",
            "certifications",
            "projects",
        )
    }
    current_role: str | None = None
    professional_summary: str | None = None
    profile_authorized = False
    if profile_version is not None:
        try:
            profile = await authorize_profile_version(db, version=profile_version)
            profile_authorized = True
            facts = _facts(profile)
            current_role, top_skills = _current_role_and_skills(profile)
            professional_summary = join_nonempty([current_role, ", ".join(top_skills) or None])
        except ProfileAuthorityError:
            pass
    documents = await list_candidate_documents(db, tenant_id=tenant_id, candidate_id=candidate_id)
    evaluations = list(
        (
            await db.execute(
                select(Evaluation)
                .where(Evaluation.tenant_id == tenant_id, Evaluation.candidate_id == candidate_id)
                .order_by(Evaluation.created_at.desc(), Evaluation.id.asc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    job_titles = await _job_titles_by_id(
        db, tenant_id=tenant_id, job_ids=[evaluation.job_id for evaluation in evaluations]
    )
    evaluation_views: list[EvaluationHistoryView] = []
    for evaluation in evaluations:
        authorized_profile = await get_authorized_profile_version_by_id(
            db,
            tenant_id=tenant_id,
            profile_version_id=evaluation.candidate_profile_version_id,
        )
        evaluation_views.append(
            EvaluationHistoryView(
                evaluation_id=evaluation.id,
                job_id=evaluation.job_id,
                job_title=job_titles.get(evaluation.job_id),
                job_criteria_version_id=evaluation.job_criteria_version_id,
                evaluation_as_of_date=evaluation.evaluation_as_of_date,
                numeric_score=(
                    evaluation.numeric_score if authorized_profile is not None else None
                ),
                fit_band=evaluation.overall_result if authorized_profile is not None else None,
                status=evaluation.status if authorized_profile is not None else "UNAVAILABLE",
                created_at=evaluation.created_at,
            )
        )
    return CandidateDetailView(
        candidate_id=candidate.id,
        created_at=candidate.created_at,
        full_name=identity_values.full_name,
        email=identity_values.email,
        phone=identity_values.phone,
        current_role=current_role,
        professional_summary=professional_summary,
        identity_status=identity.status if identity else None,
        identity_version=identity.version_number if identity else None,
        profile_status=(
            None
            if profile_version is None
            else (
                "UNAVAILABLE"
                if profile_version.status == PROFILE_STATUS_COMPLETED and not profile_authorized
                else profile_version.status
            )
        ),
        profile_version=profile_version.version_number if profile_version else None,
        **facts,
        documents=[
            CandidateDocumentView(
                document_id=document.id,
                mime_type=document.mime_type,
                byte_size=document.byte_size,
                parser_status=document.parser_status,
                parser_name=document.parser_name,
                parser_version=document.parser_version,
                parse_error_code=document.parse_error_code,
                created_at=document.created_at,
            )
            for document in documents
        ],
        evaluations=evaluation_views,
    )


async def _job_titles_by_id(
    db: AsyncSession, *, tenant_id: uuid.UUID, job_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not job_ids:
        return {}
    rows = (
        await db.execute(
            select(Job.id, Job.title).where(Job.tenant_id == tenant_id, Job.id.in_(job_ids))
        )
    ).all()
    return {job_id: title for job_id, title in rows}


# A RequiredFilterMatch/PreferredFilterMatch.category -> the
# CandidateProfileExtraction list it was matched against — mirrors the
# exact matching semantics meyar.search.structured._skill_present/
# _certification_present/_language_present/_education_present already
# use to decide the match, so evidence shown under a matched requirement
# is always attributable to the SAME profile entry that caused the match
# (PR #42 owner correction, issue #33): an unrelated employment/education
# snippet must never appear under a skill-only match.
# min_total_experience_years is deliberately absent — it is an aggregate
# over the whole employment history with no single attributable entry;
# see _requirement_attributable_evidence below.
_FILTER_MATCH_PROFILE_CATEGORY: dict[str, str] = {
    "skill": "skills",
    "certification": "certifications",
    "language": "languages",
    "education": "education",
}


def _profile_item_matches_value(category: str, item: object, folded_value: str) -> bool:
    if category in ("skills", "certifications"):
        title = item.name  # type: ignore[attr-defined]
    elif category == "languages":
        title = item.language  # type: ignore[attr-defined]
    else:
        title = combine_degree_and_field(item.degree, item.field_of_study) or ""  # type: ignore[attr-defined]
    if category == "certifications":
        return normalize_text(title) == folded_value
    return fold_az_ascii(normalize_azerbaijani_case(title)) == folded_value


def _requirement_attributable_evidence(
    profile: CandidateProfileExtraction,
    matched: Iterable[RequiredFilterMatch | PreferredFilterMatch],
) -> list[EvidenceRef]:
    """Evidence shown under a search result's matched requirements must be
    attributable to those SPECIFIC requirements — never the candidate's
    whole-profile evidence pool (PR #42 owner correction, issue #33): a
    "Python" skill match must never surface unrelated education/employment
    snippets as if they proved Python. min_total_experience_years is a
    genuine aggregate over every employment_history entry, so each
    entry's own evidence is attributable to it — never a different
    category's evidence."""
    refs: list[EvidenceRef] = []
    for match in matched:
        if match.category == "min_total_experience_years":
            for entry in profile.employment_history:
                refs.extend(entry.evidence)
            continue
        category = _FILTER_MATCH_PROFILE_CATEGORY.get(match.category)
        if category is None:
            continue
        folded_value = (
            normalize_certification_name(match.value)
            if category == "certifications"
            else fold_az_ascii(normalize_azerbaijani_case(match.value))
        )
        for entry in getattr(profile, category):
            if _profile_item_matches_value(category, entry, folded_value):
                refs.extend(entry.evidence)
    return refs


async def build_search_result_views(
    db: AsyncSession, *, tenant_id: uuid.UUID, response: CandidateSearchResponse
) -> list[CandidateSearchResultView]:
    """Add presentation identity after Slice 8 has fixed result authority/order."""
    views: list[CandidateSearchResultView] = []
    for result in response.results:
        identity = await get_current_identity_values(
            db, tenant_id=tenant_id, candidate_id=result.candidate_id
        )
        profile_row_and_content = await get_authorized_profile_version_by_id(
            db, tenant_id=tenant_id, profile_version_id=result.candidate_profile_version_id
        )
        summary = None
        evidence: list[EvidenceLocationView] = []
        if profile_row_and_content is not None:
            _profile_row, profile = profile_row_and_content
            current_role, top_skills = _current_role_and_skills(profile)
            summary = join_nonempty([current_role, ", ".join(top_skills) or None])
            attributable_evidence = _requirement_attributable_evidence(
                profile,
                [*result.required_filters_matched, *result.preferred_filters_matched],
            )
            evidence = _evidence_views(attributable_evidence, snippets=True, maximum=4)
        views.append(
            CandidateSearchResultView(
                candidate_id=result.candidate_id,
                full_name=identity.full_name,
                rank=result.rank,
                relevance_score=result.relevance_score,
                structured_score=result.structured_score,
                semantic_score=result.semantic_score,
                profile_version_id=result.candidate_profile_version_id,
                required_matches=[
                    _format_filter_match_label(item.category, item.value)
                    for item in result.required_filters_matched
                ],
                preferred_matches=[
                    _format_filter_match_label(item.category, item.value)
                    for item in result.preferred_filters_matched
                ],
                professional_summary=summary,
                evidence=evidence,
            )
        )
    return views


def _criterion_row_view(criterion: CriterionIn, *, span_id: str | None = None) -> CriterionRowView:
    return CriterionRowView(
        kind=criterion.kind.value,
        kind_label=CRITERION_KIND_LABELS.get(criterion.kind.value, criterion.kind.value),
        requirement=criterion.label,
        min_years=f"{criterion.min_years:g}" if criterion.min_years is not None else "",
        required_level=criterion.required_level or "",
        weight=f"{criterion.weight:g}",
        span_id=span_id,
    )


async def _agent_candidate_profile_view(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    profile: CandidateProfileExtraction,
) -> AgentCandidateProfileView:
    identity = await get_current_identity_version(
        db, tenant_id=tenant_id, candidate_id=candidate_id
    )
    identity_values = await identity_values_from_version(db, version=identity)
    current_role, _top_skills = _current_role_and_skills(profile)
    facts = _facts(profile)
    return AgentCandidateProfileView(
        candidate_id=candidate_id,
        full_name=identity_values.full_name,
        current_role=current_role,
        **facts,
    )


def _search_presentation_key(view: AgentToolResultView) -> str:
    """Identify only search results that render identically for HR.

    The agent may retain multiple search tool calls for audit/provenance, but
    repeating the same effective candidates (or the same empty answer) is one
    user-visible result set.  Match labels, evidence, order, and semantic-score
    visibility remain part of the key so genuinely distinct result sets are
    never collapsed.
    """
    assert view.search_outcome is not None
    if not view.search_results:
        return "empty"
    return json.dumps(
        {
            "show_semantic_score": view.search_outcome.mode in ("SEMANTIC_ONLY", "HYBRID"),
            "results": [item.model_dump(mode="json") for item in view.search_results],
        },
        sort_keys=True,
    )


async def build_agent_turn_view(
    db: AsyncSession, *, tenant_id: uuid.UUID, result: AgentTurnResult
) -> AgentTurnView:
    """Composes the Slice 2 agent's typed, non-identity tool results
    (meyar.agent.schemas.AgentTurnResult) with HR-facing display identity
    — the ONLY place CandidateIdentity is resolved for agent output,
    strictly after the agent's own tenant-scoped tool dispatch has
    already run; this function never feeds anything back into a model
    prompt. See docs/DECISIONS.md D-035."""
    tool_result_views: list[AgentToolResultView] = []
    visible_search_presentations: set[str] = set()
    for tool_result in result.tool_results:
        if tool_result.tool_name == AgentActionType.SEARCH_CANDIDATES:
            assert tool_result.search is not None
            search_response = tool_result.search.response.search_response
            search_results = (
                await build_search_result_views(db, tenant_id=tenant_id, response=search_response)
                if search_response is not None
                else []
            )
            search_view = AgentToolResultView(
                tool_name=tool_result.tool_name.value,
                search_outcome=planner_outcome_view(
                    tool_result.search.response.plan,
                    result_count=search_response.result_count if search_response else None,
                ),
                search_results=search_results,
            )
            search_outcome = search_view.search_outcome
            assert search_outcome is not None
            if search_outcome.executable:
                presentation_key = _search_presentation_key(search_view)
                if presentation_key in visible_search_presentations:
                    continue
                visible_search_presentations.add(presentation_key)
            tool_result_views.append(search_view)
        elif tool_result.tool_name == AgentActionType.DRAFT_JOB_CRITERIA:
            assert tool_result.job_draft is not None
            draft = tool_result.job_draft
            tool_result_views.append(
                AgentToolResultView(
                    tool_name=tool_result.tool_name.value,
                    job_draft=build_agent_job_draft_view(draft),
                )
            )
        elif tool_result.tool_name == AgentActionType.GET_CANDIDATE_PROFILE:
            assert tool_result.profile is not None
            profile_result = tool_result.profile
            if not profile_result.found or profile_result.candidate_id is None:
                tool_result_views.append(
                    AgentToolResultView(
                        tool_name=tool_result.tool_name.value,
                        not_found_ref=profile_result.candidate_ref,
                    )
                )
                continue
            assert profile_result.profile is not None
            profile_view = await _agent_candidate_profile_view(
                db,
                tenant_id=tenant_id,
                candidate_id=profile_result.candidate_id,
                profile=profile_result.profile,
            )
            tool_result_views.append(
                AgentToolResultView(tool_name=tool_result.tool_name.value, profile=profile_view)
            )
        else:
            assert tool_result.evidence is not None
            evidence_result = tool_result.evidence
            if not evidence_result.found or evidence_result.candidate_id is None:
                tool_result_views.append(
                    AgentToolResultView(
                        tool_name=tool_result.tool_name.value,
                        not_found_ref=evidence_result.candidate_ref,
                    )
                )
                continue
            identity = await get_current_identity_version(
                db, tenant_id=tenant_id, candidate_id=evidence_result.candidate_id
            )
            identity_values = await identity_values_from_version(db, version=identity)
            match_views = [
                AgentEvidenceMatchView(
                    category_label=AGENT_EVIDENCE_CATEGORY_LABELS.get(
                        match.category, match.category
                    ),
                    title=match.title,
                    evidence=_evidence_views(match.evidence, snippets=True),
                )
                for match in evidence_result.matches
            ]
            tool_result_views.append(
                AgentToolResultView(
                    tool_name=tool_result.tool_name.value,
                    evidence=AgentEvidenceView(
                        candidate_id=evidence_result.candidate_id,
                        full_name=identity_values.full_name,
                        topic=evidence_result.topic,
                        matches=match_views,
                    ),
                )
            )
    return AgentTurnView(
        outcome=result.outcome.value,
        message=result.message,
        headline=_agent_turn_headline(result, tool_result_views),
        tool_results=tool_result_views,
    )


def agent_draft_requires_resolution(draft: AgentJobDraftToolResult) -> bool:
    """Single server-owned confirmability predicate for a pending job draft.

    Both the draft's presentation (whether confirm controls render) and its
    mutation authorization (whether ``POST .../confirm`` may persist)
    consult this exact rule — see ``build_agent_job_draft_view`` and
    ``authorize_agent_draft_confirmation``. A draft is unresolved, and
    therefore never confirmable, while an ambiguous result-count request
    has left ``result_limit_needs_review`` set (the placeholder
    ``result_limit`` must never become confirmation authority) or any
    ``needs_review`` item still carries unresolved allowed types."""
    return draft.result_limit_needs_review or any(
        item.allowed_types for item in draft.needs_review
    )


def build_agent_job_draft_view(draft: AgentJobDraftToolResult) -> AgentJobDraftView:
    return AgentJobDraftView(
        title=draft.title,
        draft_id=draft.draft_id,
        requested_result_limit=draft.requested_result_limit,
        result_limit=draft.result_limit,
        result_limit_was_bounded=draft.result_limit_was_bounded,
        must_have_rows=[
            _criterion_row_view(
                criterion,
                span_id=next(
                    item.span_id for item in draft.requirements if item.criterion_id == criterion.id
                ),
            )
            for criterion in draft.must_have
        ],
        preferred_rows=[
            _criterion_row_view(
                criterion,
                span_id=next(
                    item.span_id for item in draft.requirements if item.criterion_id == criterion.id
                ),
            )
            for criterion in draft.preferred
        ],
        unsupported_must_have=[
            item.requirement
            for item in draft.unsupported
            if item.criterion_type == CriterionType.MUST_HAVE
        ],
        unsupported_preferred=[
            item.requirement
            for item in draft.unsupported
            if item.criterion_type == CriterionType.PREFERRED
        ],
        needs_review=[
            AgentJobDraftReviewView(
                span_id=item.span_id,
                requirement=item.requirement,
                subject=item.subject,
                kind_label=(
                    CRITERION_KIND_LABELS.get(item.kind.value, item.kind.value)
                    if item.kind is not None
                    else None
                ),
                min_years=(str(item.min_years) if item.min_years is not None else ""),
                required_level=item.required_level or "",
                allowed_types=[value.value for value in item.allowed_types],
            )
            for item in draft.needs_review
        ],
        requires_resolution=agent_draft_requires_resolution(draft),
        prohibited_count=draft.prohibited_count,
        ungrounded_count=draft.ungrounded_count,
        unsupported_language=draft.unsupported_language is not None,
        result_limit_needs_review=draft.result_limit_needs_review,
        wrong_mode_guidance=draft.wrong_mode_guidance,
    )


def _agent_turn_headline(
    result: AgentTurnResult, tool_result_views: list[AgentToolResultView]
) -> str:
    """One deterministic, HR-facing leading sentence for a live turn —
    never a second, overlapping status banner alongside it (D-030
    conversational-UX requirement). Priority: fixed server-owned
    FINAL_ANSWER/CLARIFY copy or a D-038 grounded-synthesis sentence
    always wins (it IS the meaningful assistant message); otherwise a
    fixed, deterministic summary derived from the turn's own single most
    recent tool result; otherwise the generic per-outcome fallback."""
    if result.message:
        return result.message
    if tool_result_views:
        latest_view = tool_result_views[-1]
        if latest_view.tool_name == AgentActionType.SEARCH_CANDIDATES.value:
            outcome = latest_view.search_outcome
            assert outcome is not None
            if outcome.executable:
                count = len(latest_view.search_results)
                if count == 0:
                    return "Bu tələbə uyğun namizəd tapılmadı."
                # The leading matched requirement of the top result — built
                # purely from already-computed, HR-phrased filter matches
                # (never model-authored text) so the sentence names what
                # was actually searched for without a second LLM call.
                top = latest_view.search_results[0]
                combined_matches = top.required_matches + top.preferred_matches
                term = combined_matches[0] if combined_matches else None
                if term:
                    return f"{term} tələbinə uyğun {count} namizəd tapdım."
                return f"{count} namizəd tapdım."
            return outcome.message
        if latest_view.tool_name == AgentActionType.GET_CANDIDATE_PROFILE.value:
            profile = latest_view.profile
            if profile is not None:
                name = profile.full_name or "Namizəd"
                return f"{name} üçün profil məlumatları aşağıdadır."
        if latest_view.tool_name == AgentActionType.GET_CANDIDATE_EVIDENCE.value:
            evidence_view = latest_view.evidence
            if evidence_view is not None:
                name = evidence_view.full_name or "Namizəd"
                if evidence_view.matches:
                    topic_suffix = f" {evidence_view.topic}" if evidence_view.topic else ""
                    return f"{name} üzrə{topic_suffix} sübutlar aşağıdadır."
                # Explicit insufficient-evidence wording — never the
                # generic "Nəticələr aşağıdadır." filler for a real "no
                # evidence found" result (D-044, PR #42 owner UX
                # correction).
                return f"{name} üzrə bu mövzuda profildə açıq sübut yoxdur."
        if latest_view.tool_name == AgentActionType.DRAFT_JOB_CRITERIA.value:
            draft = latest_view.job_draft
            assert draft is not None
            if draft.wrong_mode_guidance:
                return (
                    "Bu mətn namizəd axtarışına bənzəyir. Namizəd axtarışı rejimindən "
                    "istifadə edin."
                )
            total = len(draft.must_have_rows) + len(draft.preferred_rows)
            unsupported_total = len(draft.unsupported_must_have) + len(draft.unsupported_preferred)
            if (
                total == 0
                and unsupported_total == 0
                and draft.prohibited_count == 0
                and draft.ungrounded_count == 0
                and not draft.needs_review
            ):
                return (
                    "Bu mətndən konkret tələb müəyyən edilmədi. Aşağıdan əl ilə "
                    "kriteriya əlavə edə bilərsiniz."
                )
            # All three notes are safe, generic HR-facing text — never the
            # matched sensitive term itself for prohibited_count, and never
            # the unconfirmed drafted text itself for ungrounded_count (see
            # AgentJobDraftToolResult docstring); unsupported_total's own
            # requirement text is disclosed only in the review rows below,
            # never restated in this one-line headline.
            notes = []
            if unsupported_total:
                notes.append(
                    f"{unsupported_total} tələb hazırda avtomatik qiymətləndirməyə daxil "
                    "edilmədi (aşağıda görünür)"
                )
            if draft.prohibited_count:
                notes.append(
                    f"{draft.prohibited_count} şəxsi/həssas tələb sıralamada istifadə edilmir; "
                    "elandan çıxarın və ya peşəkar tələblə əvəz edin"
                )
            if draft.ungrounded_count:
                notes.append(
                    f"{draft.ungrounded_count} tələb JD mətnində aydın təsdiqlənmədiyi üçün "
                    "çıxarıldı"
                )
            if draft.needs_review:
                notes.append(f"{len(draft.needs_review)} tələb dəqiqləşdirmə tələb edir")
            note_text = f" ({'; '.join(notes)}.)" if notes else ""
            return (
                f"Vakansiya qaralaması üçün {len(draft.must_have_rows)} mütləq və "
                f"{len(draft.preferred_rows)} üstünlük tələbi hazırlandı. Nəzərdən keçirin, "
                f"və təsdiqləyin.{note_text}"
            )
    return agent_turn_outcome_message(result.outcome.value, None)


async def list_job_views(
    db: AsyncSession, *, tenant_id: uuid.UUID, status: str = JOB_STATUS_ACTIVE
) -> list[JobView]:
    latest = (
        select(
            JobCriteriaVersion.job_id,
            func.max(JobCriteriaVersion.version_number).label("max_version"),
        )
        .where(JobCriteriaVersion.tenant_id == tenant_id)
        .group_by(JobCriteriaVersion.job_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(Job, JobCriteriaVersion)
            .outerjoin(latest, Job.id == latest.c.job_id)
            .outerjoin(
                JobCriteriaVersion,
                (JobCriteriaVersion.tenant_id == tenant_id)
                & (JobCriteriaVersion.job_id == Job.id)
                & (JobCriteriaVersion.version_number == latest.c.max_version),
            )
            .where(Job.tenant_id == tenant_id, Job.status == status)
            .order_by(Job.created_at.desc(), Job.id.asc())
        )
    ).all()
    views: list[JobView] = []
    for job, criteria in rows:
        must_have_labels: list[str] = []
        preferred_labels: list[str] = []
        if criteria:
            for item in criteria.criteria:
                label = item.get("label")
                if not label:
                    continue
                if item.get("type") == "MUST_HAVE":
                    must_have_labels.append(label)
                elif item.get("type") == "PREFERRED":
                    preferred_labels.append(label)
        views.append(
            JobView(
                job_id=job.id,
                title=job.title,
                status=job.status,
                archived_at=job.archived_at,
                created_at=job.created_at,
                current_criteria_version_id=criteria.id if criteria else None,
                current_criteria_version=criteria.version_number if criteria else None,
                criteria_count=len(criteria.criteria) if criteria else 0,
                must_have_labels=must_have_labels,
                preferred_labels=preferred_labels,
            )
        )
    return views


async def _criterion_labels_by_id(
    db: AsyncSession, *, tenant_id: uuid.UUID, job_criteria_version_id: uuid.UUID
) -> dict[str, str]:
    """HR-facing criterion label lookup for the ranking-contribution table
    (owner visual-inspection Blocker 3): the deterministic scoring engine's
    ``CriterionScoreContribution.criterion_id`` is an internal slug
    (e.g. ``aml_skill``) never meant for HR display — the human label
    lives only on the criteria version the job was ranked against."""
    version = (
        await db.execute(
            select(JobCriteriaVersion).where(
                JobCriteriaVersion.id == job_criteria_version_id,
                JobCriteriaVersion.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if version is None:
        return {}
    return {
        item["id"]: item["label"]
        for item in version.criteria
        if item.get("id") and item.get("label")
    }


async def _criteria_by_id(
    db: AsyncSession, *, tenant_id: uuid.UUID, job_criteria_version_id: uuid.UUID
) -> dict[str, CriterionIn]:
    version = (
        await db.execute(
            select(JobCriteriaVersion).where(
                JobCriteriaVersion.id == job_criteria_version_id,
                JobCriteriaVersion.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if version is None:
        return {}
    criteria: dict[str, CriterionIn] = {}
    for item in version.criteria:
        try:
            criterion = CriterionIn.model_validate(item)
        except ValidationError:
            continue
        criteria[criterion.id] = criterion
    return criteria


async def build_ranked_candidate_views(
    db: AsyncSession, *, tenant_id: uuid.UUID, ranking: BatchRankingResult
) -> list[RankedCandidateView]:
    """Add names only after Slice 10 has finalized rank and score."""
    criterion_labels = await _criterion_labels_by_id(
        db, tenant_id=tenant_id, job_criteria_version_id=ranking.job_criteria_version_id
    )
    criteria = await _criteria_by_id(
        db, tenant_id=tenant_id, job_criteria_version_id=ranking.job_criteria_version_id
    )
    views: list[RankedCandidateView] = []
    for result in ranking.results:
        identity = await get_current_identity_values(
            db, tenant_id=tenant_id, candidate_id=result.candidate_id
        )
        authorized = await get_authorized_profile_version_by_id(
            db,
            tenant_id=tenant_id,
            profile_version_id=result.candidate_profile_version_id,
        )
        profile = authorized[1] if authorized is not None else None
        contributions: list[ScoreContributionView] = []
        for item in result.score_explanation.criteria:
            criterion = criteria.get(item.criterion_id)
            label = criterion_labels.get(item.criterion_id, item.criterion_id)
            evidence = [
                EvidenceLocationView(page=ref.page, block_index=ref.block_index)
                for ref in item.evidence_references
            ]
            if criterion is not None and profile is not None:
                displayed_result = evaluate_criterion(
                    criterion,
                    profile,
                    evaluation_as_of_date=result.evaluation_as_of_date,
                )
                scored_locations = {(ref.page, ref.block_index) for ref in item.evidence_references}
                displayed_locations = {
                    (ref.page, ref.block_index) for ref in displayed_result.evidence
                }
                if (
                    displayed_result.status == item.status
                    and displayed_result.reason_code == item.reason_code
                    and displayed_locations == scored_locations
                ):
                    evidence = _evidence_views(displayed_result.evidence, snippets=True)
            # Even the truthful page-only fallback must not repeat an identical
            # location when an old score contains duplicate references.
            unique_evidence: list[EvidenceLocationView] = []
            seen_locations: set[tuple[int, int, str | None]] = set()
            for ref in evidence:
                key = (ref.page, ref.block_index, ref.snippet)
                if key not in seen_locations:
                    seen_locations.add(key)
                    unique_evidence.append(ref)
            contributions.append(
                ScoreContributionView(
                    criterion_id=item.criterion_id,
                    label=label,
                    criterion_kind=item.criterion_kind,
                    criterion_type=item.criterion_type,
                    weight=item.weight,
                    status=item.status,
                    factor=item.factor,
                    weighted_points=item.weighted_points,
                    reason_code=item.reason_code,
                    explanation=criterion_explanation_az(
                        reason_code=item.reason_code,
                        explanation=item.explanation,
                        label=label,
                    ),
                    manual_review_required=item.manual_review_required,
                    evidence=unique_evidence,
                )
            )
        views.append(
            RankedCandidateView(
                candidate_id=result.candidate_id,
                full_name=identity.full_name,
                rank=result.rank,
                numeric_score=result.numeric_score,
                fit_band=result.fit_band,
                evaluation_id=result.evaluation_id,
                evaluation_as_of_date=result.evaluation_as_of_date,
                evaluation_policy_version=result.evaluation_policy_version,
                scoring_policy_version=result.scoring_policy_version,
                contributions=contributions,
            )
        )
    return views


async def get_candidate_document_preview(
    db: AsyncSession, *, tenant_id: uuid.UUID, candidate_id: uuid.UUID, document_id: uuid.UUID
) -> CandidateDocumentPreviewView | None:
    """The truthful in-app 'CV-yə bax' surface: safe, already-parsed text
    (CanonicalDocument), never the original bytes and never a live-model
    call. Returns None only when the document itself doesn't belong to
    this tenant/candidate (caller renders 404); a document that parsed
    successfully but has no canonical text yet renders `available=False`
    instead of raising."""
    document = await get_candidate_document(
        db, tenant_id=tenant_id, candidate_id=candidate_id, document_id=document_id
    )
    if document is None:
        return None
    canonical = await get_latest_canonical_document(
        db, tenant_id=tenant_id, candidate_document_id=document_id
    )
    if canonical is None:
        return CandidateDocumentPreviewView(
            document_id=document.id,
            candidate_id=candidate_id,
            mime_type=document.mime_type,
            available=False,
        )
    content = CanonicalDocumentContent.model_validate(canonical.content)
    pages = [
        DocumentPreviewPageView(
            page=page.page,
            text="\n\n".join(block.text for block in page.blocks if block.text.strip()),
        )
        for page in content.pages
    ]
    return CandidateDocumentPreviewView(
        document_id=document.id,
        candidate_id=candidate_id,
        mime_type=document.mime_type,
        available=True,
        pages=pages,
    )


async def get_job_title_for_criteria_version(
    db: AsyncSession, *, tenant_id: uuid.UUID, job_criteria_version_id: uuid.UUID
) -> str | None:
    row = (
        await db.execute(
            select(Job.title)
            .join(JobCriteriaVersion, JobCriteriaVersion.job_id == Job.id)
            .where(
                JobCriteriaVersion.id == job_criteria_version_id,
                JobCriteriaVersion.tenant_id == tenant_id,
                Job.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    return row


# Owner visual-inspection Blocker 2/B — new-vacancy creation. The HR-facing
# form is a fixed set of rows (no JS row-adding, matching the rest of this
# JS-free /ui surface); empty rows (blank requirement) are silently skipped
# below, so HR only fills in as many criteria as the vacancy needs.
#
# One HR-facing "Tələb" (requirement) field per row, not separate internal
# "Ad"/"Dəyər" (label/value) fields: for SKILL/CERTIFICATION/EDUCATION/
# LANGUAGE criteria the same text the HR user types becomes BOTH the
# display label and the exact term the deterministic scorer matches
# against candidate-profile evidence — this makes it structurally
# impossible to construct a criterion whose displayed name and matched
# value disagree (the root cause of owner-reported Blocker A: a vacancy
# created through the previous two-field form persisted
# {"label": "Python", "value": "MUST_HAVE"} because the HR tester,
# confused by the Ad/Dəyər distinction, typed the requirement type into
# the wrong field — the deterministic scorer then correctly, and
# deterministically, found no candidate profile skill literally named
# "MUST_HAVE" and returned UNKNOWN; that was not a scoring bug). See
# docs/DECISIONS.md D-025.
CRITERION_ROW_COUNT = 4
CRITERION_KIND_OPTIONS: tuple[tuple[str, str], ...] = (
    (CriterionKind.SKILL.value, "Bacarıq"),
    (CriterionKind.CERTIFICATION.value, "Sertifikat"),
    (CriterionKind.EDUCATION.value, "Təhsil"),
    (CriterionKind.LANGUAGE.value, "Dil"),
    (CriterionKind.EXPERIENCE.value, "Təcrübə"),
    (CriterionKind.SKILL_EXPERIENCE.value, "Bacarıq üzrə təcrübə"),
    (CriterionKind.DOMAIN_EXPERIENCE.value, "Sahə təcrübəsi"),
)
DEFAULT_CRITERION_WEIGHT = "1"


@dataclass(frozen=True)
class CriterionRowInput:
    kind: str
    requirement: str
    min_years: str
    weight: str
    required_level: str = ""


def _first_pydantic_message(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "Meyar məlumatları etibarsızdır."
    raw = str(errors[0].get("msg", ""))
    return raw.removeprefix("Value error, ") or "Meyar məlumatları etibarsızdır."


def _parse_criterion_row(
    row: CriterionRowInput, *, criterion_type: CriterionType, used_ids: set[str]
) -> CriterionIn | None:
    requirement = row.requirement.strip()
    if not requirement:
        return None
    try:
        kind = CriterionKind(row.kind)
    except ValueError as exc:
        raise UIServiceInputError(f"'{requirement}' üçün meyar növü tanınmadı.") from exc

    min_years: float | None = None
    value: str | None = requirement
    if kind in (CriterionKind.EXPERIENCE, CriterionKind.SKILL_EXPERIENCE):
        raw_years = row.min_years.strip()
        if not raw_years:
            raise UIServiceInputError(f"'{requirement}' meyarı üçün illik təcrübəni daxil edin.")
        try:
            min_years = float(raw_years.replace(",", "."))
        except ValueError as exc:
            raise UIServiceInputError(
                f"'{requirement}' meyarı üçün illik təcrübə rəqəm olmalıdır."
            ) from exc
        if kind is CriterionKind.EXPERIENCE:
            value = None
    elif kind is CriterionKind.DOMAIN_EXPERIENCE and row.min_years.strip():
        try:
            min_years = float(row.min_years.strip().replace(",", "."))
        except ValueError as exc:
            raise UIServiceInputError(
                f"'{requirement}' meyarı üçün illik təcrübə rəqəm olmalıdır."
            ) from exc
    elif row.min_years.strip():
        # Kind-aware validation: "Təcrübə (il)" is only meaningful for an
        # EXPERIENCE criterion — a value typed there for SKILL/
        # CERTIFICATION/EDUCATION/LANGUAGE must never be silently dropped,
        # since that would mean the form accepted input it then ignored.
        raise UIServiceInputError(
            f"'{requirement}' meyarı üçün illik təcrübə sahəsi yalnız "
            "'Təcrübə' növü üçündür — bu sahəni boş buraxın və ya bacarıq/sahə "
            "üzrə uyğun təcrübə növünü seçin."
        )

    required_level = row.required_level.strip() or None
    if required_level is not None and kind is not CriterionKind.LANGUAGE:
        raise UIServiceInputError(
            f"'{requirement}' meyarı üçün səviyyə yalnız dil tələbinə aiddir."
        )

    raw_weight = row.weight.strip()
    try:
        weight = float(raw_weight.replace(",", ".")) if raw_weight else 1.0
    except ValueError as exc:
        raise UIServiceInputError(
            f"'{requirement}' meyarı üçün əhəmiyyət rəqəm olmalıdır."
        ) from exc

    try:
        return CriterionIn(
            id=slugify_criterion_label(requirement, used_ids),
            kind=kind,
            type=criterion_type,
            label=requirement,
            value=value,
            min_years=min_years,
            required_level=required_level,
            weight=weight,
        )
    except ValidationError as exc:
        raise UIServiceInputError(f"'{requirement}': {_first_pydantic_message(exc)}") from exc


def build_job_create_request(
    *,
    title: str,
    must_have_rows: list[CriterionRowInput],
    preferred_rows: list[CriterionRowInput],
) -> JobCreateRequest:
    """Pure form-parsing + validation, reusing the exact same
    ``CriterionIn``/``JobCreateRequest`` domain schemas the internal REST
    API's ``POST /api/v1/jobs`` validates against (see
    ``meyar.api.v1.jobs.post_job``) — no second criteria/scoring model."""
    stripped_title = title.strip()
    if not stripped_title:
        raise UIServiceInputError("Vakansiya başlığı boş ola bilməz.")

    used_ids: set[str] = set()
    criteria: list[CriterionIn] = []
    for criterion_type, rows in (
        (CriterionType.MUST_HAVE, must_have_rows),
        (CriterionType.PREFERRED, preferred_rows),
    ):
        for row in rows:
            criterion = _parse_criterion_row(row, criterion_type=criterion_type, used_ids=used_ids)
            if criterion is not None:
                criteria.append(criterion)

    if not criteria:
        raise UIServiceInputError("Ən azı bir Mütləq və ya Üstünlük meyarı daxil edin.")
    try:
        return JobCreateRequest(title=stripped_title, criteria=criteria)
    except ValidationError as exc:
        raise UIServiceInputError(_first_pydantic_message(exc)) from exc


def authorize_agent_draft_confirmation(
    *,
    draft: AgentJobDraftToolResult,
    request: JobCreateRequest,
    submitted_span_ids: list[str],
) -> None:
    """Permit exactly the unchanged server-authorized SCORABLE draft rows.

    Authorization is refused for the same server-owned reason the confirm
    UI never renders in the first place — ``agent_draft_requires_resolution``
    is the single confirmability rule shared by both. This closes the gap
    where a caller who already holds a valid draft_id (session/CSRF/tenant
    all otherwise legitimate) could POST directly to the confirm route: UI
    visibility is not authorization, so the mutation boundary re-checks the
    same predicate independently rather than trusting that the rendered
    page happened to hide the control."""
    if agent_draft_requires_resolution(draft):
        raise UIServiceInputError(
            "İnsan baxışı tələb edən sahələri dəqiqləşdirmədən sıralamanı təsdiqləmək olmaz."
        )
    if len(request.criteria) != len(submitted_span_ids):
        raise UIServiceInputError("Qaralama meyarlarının mənbə təsdiqi etibarsızdır.")
    expected_by_span = {
        result.span_id: next(
            (
                criterion
                for criterion in [*draft.must_have, *draft.preferred]
                if criterion.id == result.criterion_id
            ),
            None,
        )
        for result in draft.requirements
        if result.state.value == "SCORABLE" and result.criterion_id is not None
    }
    if len(request.criteria) != len(expected_by_span):
        raise UIServiceInputError(
            "Qaralamanın təsdiqli meyarları silinə və ya yeni meyarla əvəz edilə bilməz."
        )
    if set(submitted_span_ids) != set(expected_by_span):
        raise UIServiceInputError("Qaralama meyarlarının mənbə təsdiqi etibarsızdır.")
    if len(set(submitted_span_ids)) != len(submitted_span_ids):
        raise UIServiceInputError("Eyni mənbə tələbi birdən çox meyar yarada bilməz.")
    comparable_fields = (
        "kind",
        "type",
        "label",
        "value",
        "min_years",
        "required_level",
        "weight",
        "evidence_required",
        "manual_review_required",
    )
    for submitted, span_id in zip(request.criteria, submitted_span_ids, strict=True):
        expected = expected_by_span.get(span_id)
        if expected is None or any(
            getattr(submitted, field) != getattr(expected, field) for field in comparable_fields
        ):
            raise UIServiceInputError(
                "Qaralama meyarı mənbə tələbinin server təsdiqli forması ilə uyğun gəlmir."
            )


# Owner visual-inspection follow-up — Job lifecycle/duplicate-safety
# (D-028). Job titles are deliberately NOT unique (two vacancies may
# legitimately share a title), so accidental-duplicate protection instead
# compares a CANONICAL signature of (normalized title, normalized
# criteria) — never raw display text, never exposed to HR. Scoped to the
# /ui/jobs creation path only; POST /api/v1/jobs is unchanged.
JOB_DUPLICATE_MESSAGE = "Eyni tələblərlə aktiv vakansiya artıq mövcuddur."


def compute_job_duplicate_signature(title: str, criteria: list[CriterionIn]) -> str:
    """SHA-256 hex digest of a canonical (title, criteria) signature —
    order-independent (criteria are sorted before hashing) and
    display-text-independent (case/whitespace-normalized, and only the
    fields that actually affect matching — kind, MUST_HAVE/PREFERRED
    type, value, min_years, required_level, weight, and review/evidence
    policy — participate; the free-text label and
    the server-generated id never do, so two vacancies with the same
    underlying requirements are recognized as duplicates regardless of
    how their criteria happen to be labeled)."""

    def _normalized(text: str | None) -> str:
        return " ".join(normalize_azerbaijani_case(text or "").split())

    canonical_criteria = sorted(
        (
            criterion.kind.value,
            criterion.type.value,
            _normalized(criterion.value),
            round(criterion.min_years, 2) if criterion.min_years is not None else None,
            _normalized(criterion.required_level),
            round(criterion.weight, 2),
            criterion.evidence_required,
            criterion.manual_review_required,
        )
        for criterion in criteria
    )
    payload = json.dumps(
        {"title": _normalized(title), "criteria": canonical_criteria},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
