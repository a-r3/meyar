import re
import uuid
from collections import defaultdict
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.core.text import fold_az_ascii
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
from meyar.models.job import Job
from meyar.models.job_criteria_version import JobCriteriaVersion
from meyar.schemas.candidate_identity import CandidateIdentityExtraction
from meyar.schemas.candidate_profile import CandidateProfileExtraction, EvidenceRef
from meyar.schemas.criteria import CriterionIn, CriterionKind, CriterionType
from meyar.schemas.job import JobCreateRequest
from meyar.scoring.schemas import BatchRankingResult
from meyar.search.schemas import CandidateSearchResponse
from meyar.services.candidate_document_repo import (
    get_candidate_document,
    get_latest_canonical_document,
    list_candidate_documents,
)
from meyar.services.candidate_identity_repo import get_current_identity_version
from meyar.services.candidate_profile_repo import (
    get_current_profile_version,
    get_profile_version_by_id,
)
from meyar.services.candidate_repo import get_candidate
from meyar.ui.presentation import join_nonempty
from meyar.ui.view_models import (
    CandidateDetailView,
    CandidateDocumentPreviewView,
    CandidateDocumentView,
    CandidateLibraryItemView,
    CandidateLibraryPageView,
    CandidateSearchResultView,
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


def _current_role_and_skills(
    profile: CandidateProfileExtraction, *, skill_limit: int = 5
) -> tuple[str | None, list[str]]:
    current_role = profile.employment_history[0].title if profile.employment_history else None
    top_skills = [item.name for item in profile.skills[:skill_limit]]
    return current_role, top_skills


def _library_profile_summary(
    profile_version: CandidateProfileVersion | None,
) -> tuple[str | None, list[str], list[str]]:
    """HR-facing summary derived from already-fetched profile_content — no
    extra query. Returns (current_role, top_skills, languages)."""
    if profile_version is None or profile_version.profile_content is None:
        return None, [], []
    try:
        profile = CandidateProfileExtraction.model_validate(profile_version.profile_content)
    except ValidationError:
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
        profile = profiles_by_candidate.get(candidate.id)
        full_name, _email, _phone = _identity_values(identities_by_candidate.get(candidate.id))
        current_role, top_skills, languages = _library_profile_summary(profile)
        items.append(
            CandidateLibraryItemView(
                candidate_id=candidate.id,
                created_at=candidate.created_at,
                full_name=full_name,
                current_role=current_role,
                top_skills=top_skills,
                languages=languages,
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
    current_role: str | None = None
    professional_summary: str | None = None
    if profile_version and profile_version.profile_content is not None:
        try:
            profile = CandidateProfileExtraction.model_validate(profile_version.profile_content)
            facts = _facts(profile)
            current_role, top_skills = _current_role_and_skills(profile)
            professional_summary = join_nonempty([current_role, ", ".join(top_skills) or None])
        except ValidationError:
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
    return CandidateDetailView(
        candidate_id=candidate.id,
        created_at=candidate.created_at,
        full_name=full_name,
        email=email,
        phone=phone,
        current_role=current_role,
        professional_summary=professional_summary,
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
                job_title=job_titles.get(evaluation.job_id),
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
                current_role, top_skills = _current_role_and_skills(profile)
                summary = join_nonempty([current_role, ", ".join(top_skills) or None])
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


async def build_ranked_candidate_views(
    db: AsyncSession, *, tenant_id: uuid.UUID, ranking: BatchRankingResult
) -> list[RankedCandidateView]:
    """Add names only after Slice 10 has finalized rank and score."""
    criterion_labels = await _criterion_labels_by_id(
        db, tenant_id=tenant_id, job_criteria_version_id=ranking.job_criteria_version_id
    )
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
                        label=criterion_labels.get(item.criterion_id, item.criterion_id),
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
)
DEFAULT_CRITERION_WEIGHT = "1"

_CRITERION_ID_FALLBACK = "meyar"


@dataclass(frozen=True)
class CriterionRowInput:
    kind: str
    requirement: str
    min_years: str
    weight: str


def _slugify_criterion_label(label: str, used_ids: set[str]) -> str:
    """A stable, ASCII-only criterion id derived from the HR-entered
    requirement text. The HR user never types or sees a raw id/UUID (owner
    visual-inspection Blocker 2: 'no raw UUID entry by the HR user') — it
    exists only as the deterministic policy engine's internal join key."""
    ascii_text = fold_az_ascii(label).lower()
    base = re.sub(r"[^a-z0-9]+", "_", ascii_text).strip("_")[:60] or _CRITERION_ID_FALLBACK
    candidate = base
    suffix = 2
    while candidate in used_ids:
        candidate = f"{base}_{suffix}"[:64]
        suffix += 1
    used_ids.add(candidate)
    return candidate


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
    if kind is CriterionKind.EXPERIENCE:
        raw_years = row.min_years.strip()
        if not raw_years:
            raise UIServiceInputError(f"'{requirement}' meyarı üçün illik təcrübəni daxil edin.")
        try:
            min_years = float(raw_years.replace(",", "."))
        except ValueError as exc:
            raise UIServiceInputError(
                f"'{requirement}' meyarı üçün illik təcrübə rəqəm olmalıdır."
            ) from exc
        value = None
    elif row.min_years.strip():
        # Kind-aware validation: "Təcrübə (il)" is only meaningful for an
        # EXPERIENCE criterion — a value typed there for SKILL/
        # CERTIFICATION/EDUCATION/LANGUAGE must never be silently dropped,
        # since that would mean the form accepted input it then ignored.
        raise UIServiceInputError(
            f"'{requirement}' meyarı üçün illik təcrübə sahəsi yalnız "
            "'Təcrübə' növü üçündür — bu sahəni boş buraxın və ya növü "
            "'Təcrübə' olaraq dəyişin."
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
            id=_slugify_criterion_label(requirement, used_ids),
            kind=kind,
            type=criterion_type,
            label=requirement,
            value=value,
            min_years=min_years,
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
