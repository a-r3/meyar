import uuid
from collections import defaultdict

from pydantic import ValidationError
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

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
from meyar.models.job import Job
from meyar.models.job_criteria_version import JobCriteriaVersion
from meyar.schemas.candidate_identity import CandidateIdentityExtraction
from meyar.schemas.candidate_profile import CandidateProfileExtraction, EvidenceRef
from meyar.scoring.schemas import BatchRankingResult
from meyar.search.schemas import CandidateSearchResponse
from meyar.services.candidate_document_repo import list_candidate_documents
from meyar.services.candidate_identity_repo import get_current_identity_version
from meyar.services.candidate_profile_repo import (
    get_current_profile_version,
    get_profile_version_by_id,
)
from meyar.services.candidate_repo import get_candidate
from meyar.ui.presentation import join_nonempty
from meyar.ui.view_models import (
    CandidateDetailView,
    CandidateDocumentView,
    CandidateLibraryItemView,
    CandidateLibraryPageView,
    CandidateSearchResultView,
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


def _identity_values(version: CandidateIdentityVersion | None) -> tuple[str | None, ...]:
    if version is None or version.identity_content is None:
        return None, None, None
    try:
        identity = CandidateIdentityExtraction.model_validate(version.identity_content)
    except ValidationError:
        return None, None, None
    return (
        identity.full_name.value if identity.full_name else None,
        identity.email.value if identity.email else None,
        identity.phone.value if identity.phone else None,
    )


def _evidence_views(
    evidence: list[EvidenceRef], *, snippets: bool, maximum: int = 4
) -> list[EvidenceLocationView]:
    return [
        EvidenceLocationView(
            page=item.page,
            block_index=item.block_index,
            snippet=(item.quote[:240] if snippets else None),
        )
        for item in evidence[:maximum]
    ]


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
                title=join_nonempty([item.degree, item.field_of_study]) or "Təhsil məlumatı",
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
        profile = profiles_by_candidate.get(candidate.id)
        full_name, _email, _phone = _identity_values(identities_by_candidate.get(candidate.id))
        items.append(
            CandidateLibraryItemView(
                candidate_id=candidate.id,
                created_at=candidate.created_at,
                full_name=full_name,
                current_profile_version=profile.version_number if profile else None,
                current_profile_status=profile.status if profile else None,
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
    full_name, email, phone = _identity_values(identity)
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
    if profile_version and profile_version.profile_content is not None:
        try:
            facts = _facts(
                CandidateProfileExtraction.model_validate(profile_version.profile_content)
            )
        except ValidationError:
            pass
    documents = await list_candidate_documents(
        db, tenant_id=tenant_id, candidate_id=candidate_id
    )
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
    return CandidateDetailView(
        candidate_id=candidate.id,
        created_at=candidate.created_at,
        full_name=full_name,
        email=email,
        phone=phone,
        identity_status=identity.status if identity else None,
        identity_version=identity.version_number if identity else None,
        profile_status=profile_version.status if profile_version else None,
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
        evaluations=[
            EvaluationHistoryView(
                evaluation_id=evaluation.id,
                job_id=evaluation.job_id,
                job_criteria_version_id=evaluation.job_criteria_version_id,
                evaluation_as_of_date=evaluation.evaluation_as_of_date,
                numeric_score=evaluation.numeric_score,
                fit_band=evaluation.overall_result,
                status=evaluation.status,
                created_at=evaluation.created_at,
            )
            for evaluation in evaluations
        ],
    )


async def build_search_result_views(
    db: AsyncSession, *, tenant_id: uuid.UUID, response: CandidateSearchResponse
) -> list[CandidateSearchResultView]:
    """Add presentation identity after Slice 8 has fixed result authority/order."""
    views: list[CandidateSearchResultView] = []
    for result in response.results:
        identity = await get_current_identity_version(
            db, tenant_id=tenant_id, candidate_id=result.candidate_id
        )
        full_name, _email, _phone = _identity_values(identity)
        profile_row = await get_profile_version_by_id(
            db, tenant_id=tenant_id, profile_version_id=result.candidate_profile_version_id
        )
        summary = None
        evidence: list[EvidenceLocationView] = []
        if profile_row and profile_row.profile_content is not None:
            try:
                profile = CandidateProfileExtraction.model_validate(profile_row.profile_content)
                current_role = (
                    profile.employment_history[0].title
                    if profile.employment_history
                    else None
                )
                skill_names = ", ".join(item.name for item in profile.skills[:5]) or None
                summary = join_nonempty([current_role, skill_names])
                all_evidence = [
                    reference
                    for group in (
                        profile.skills,
                        profile.employment_history,
                        profile.education,
                        profile.certifications,
                        profile.languages,
                        profile.projects,
                    )
                    for item in group
                    for reference in item.evidence
                ]
                evidence = _evidence_views(all_evidence, snippets=True, maximum=4)
            except ValidationError:
                pass
        views.append(
            CandidateSearchResultView(
                candidate_id=result.candidate_id,
                full_name=full_name,
                rank=result.rank,
                relevance_score=result.relevance_score,
                structured_score=result.structured_score,
                semantic_score=result.semantic_score,
                profile_version_id=result.candidate_profile_version_id,
                required_matches=[
                    f"{item.category}: {item.value}" for item in result.required_filters_matched
                ],
                preferred_matches=[
                    f"{item.category}: {item.value}" for item in result.preferred_filters_matched
                ],
                professional_summary=summary,
                evidence=evidence,
            )
        )
    return views


async def list_job_views(db: AsyncSession, *, tenant_id: uuid.UUID) -> list[JobView]:
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
            .where(Job.tenant_id == tenant_id)
            .order_by(Job.created_at.desc(), Job.id.asc())
        )
    ).all()
    return [
        JobView(
            job_id=job.id,
            title=job.title,
            created_at=job.created_at,
            current_criteria_version_id=criteria.id if criteria else None,
            current_criteria_version=criteria.version_number if criteria else None,
            criteria_count=len(criteria.criteria) if criteria else 0,
        )
        for job, criteria in rows
    ]


async def build_ranked_candidate_views(
    db: AsyncSession, *, tenant_id: uuid.UUID, ranking: BatchRankingResult
) -> list[RankedCandidateView]:
    """Add names only after Slice 10 has finalized rank and score."""
    views: list[RankedCandidateView] = []
    for result in ranking.results:
        identity = await get_current_identity_version(
            db, tenant_id=tenant_id, candidate_id=result.candidate_id
        )
        full_name, _email, _phone = _identity_values(identity)
        views.append(
            RankedCandidateView(
                candidate_id=result.candidate_id,
                full_name=full_name,
                rank=result.rank,
                numeric_score=result.numeric_score,
                fit_band=result.fit_band,
                evaluation_id=result.evaluation_id,
                evaluation_as_of_date=result.evaluation_as_of_date,
                evaluation_policy_version=result.evaluation_policy_version,
                scoring_policy_version=result.scoring_policy_version,
                contributions=[
                    ScoreContributionView(
                        criterion_id=item.criterion_id,
                        criterion_kind=item.criterion_kind,
                        criterion_type=item.criterion_type,
                        weight=item.weight,
                        status=item.status,
                        factor=item.factor,
                        weighted_points=item.weighted_points,
                        reason_code=item.reason_code,
                        manual_review_required=item.manual_review_required,
                        evidence=[
                            EvidenceLocationView(page=ref.page, block_index=ref.block_index)
                            for ref in item.evidence_references
                        ],
                    )
                    for item in result.score_explanation.criteria
                ],
            )
        )
    return views
