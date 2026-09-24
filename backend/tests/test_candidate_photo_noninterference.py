"""Fixed synthetic before/after proof that photo creation changes no suitability authority."""

import json
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.embedding.serializer import build_professional_embedding_text, compute_source_sha256
from meyar.ingestion.parsers.local_text_parser import LocalTextParser
from meyar.models.candidate_document import CandidateDocument
from meyar.models.candidate_embedding_version import CandidateEmbeddingVersion
from meyar.models.candidate_profile_version import CandidateProfileVersion
from meyar.models.job import Job
from meyar.scoring.batch import rank_candidates_for_job
from meyar.search.planner_service import plan_candidate_search
from meyar.search.schemas import (
    CandidateSearchRequest,
    EmbeddingSearchConfig,
    RequiredFilters,
    SearchMode,
)
from meyar.search.service import search_candidates
from meyar.services.candidate_photo_service import process_photo_for_document
from meyar.services.demo_seed_service import (
    _demo_candidates,
    _DemoEmbeddingProvider,
    _DemoLLMProvider,
    _identity_extraction,
    _profile_extraction,
    seed_demo,
)
from meyar.services.job_criteria_repo import get_current_criteria_version
from meyar.storage.local import LocalFilesystemStorage
from meyar.storage.photo import LocalPhotoStorage


async def test_photo_creation_cannot_change_search_plan_embedding_or_ranking(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalFilesystemStorage(str(tmp_path / "storage"))
    summary = await seed_demo(
        db_session,
        storage,
        LocalTextParser(),
        max_bytes=10 * 1024 * 1024,
        max_profile_input_chars=20000,
        max_identity_input_chars=20000,
        max_embedding_input_chars=20000,
        evaluation_as_of_date=date(2026, 1, 1),
    )
    await db_session.commit()
    embedding = await db_session.scalar(
        select(CandidateEmbeddingVersion).where(
            CandidateEmbeddingVersion.tenant_id == summary.tenant_id
        )
    )
    assert embedding is not None
    config = EmbeddingSearchConfig(
        provider=embedding.provider,
        model_name=embedding.model_name,
        model_revision=embedding.model_revision,
        serializer_version=embedding.serializer_version,
        embedding_dimensions=embedding.embedding_dimensions,
    )
    provider = _DemoEmbeddingProvider(vector=[0.9, 0.1, 0.0, 0.0])
    spec = _demo_candidates()[0]
    llm = _DemoLLMProvider(_profile_extraction(spec), _identity_extraction(spec))
    job = await db_session.scalar(select(Job).where(Job.tenant_id == summary.tenant_id))
    assert job is not None
    criteria = await get_current_criteria_version(
        db_session, tenant_id=summary.tenant_id, job_id=job.id
    )
    assert criteria is not None
    requests = [
        CandidateSearchRequest(
            mode=SearchMode.STRUCTURED_ONLY,
            required_filters=RequiredFilters(skills=["Java"]),
        ),
        CandidateSearchRequest(
            mode=SearchMode.SEMANTIC_ONLY,
            semantic_query="backend developer",
            embedding_config=config,
        ),
        CandidateSearchRequest(
            mode=SearchMode.HYBRID,
            semantic_query="backend developer",
            embedding_config=config,
            required_filters=RequiredFilters(skills=["Java"]),
        ),
    ]

    async def snapshot() -> dict:
        search = [
            (
                await search_candidates(
                    db_session,
                    tenant_id=summary.tenant_id,
                    request=request,
                    embedding_provider=provider,
                )
            ).model_dump(mode="json")
            for request in requests
        ]
        ranking = await rank_candidates_for_job(
            db_session,
            tenant_id=summary.tenant_id,
            job_criteria_version_id=criteria.id,
            evaluation_as_of_date=date(2026, 1, 1),
        )
        plan = await plan_candidate_search(
            db_session,
            llm,
            tenant_id=summary.tenant_id,
            natural_language_request="Java bilən namizədləri göstər.",
            as_of_date=date(2026, 1, 1),
            embedding_config=config,
        )
        profiles = (
            await db_session.scalars(
                select(CandidateProfileVersion)
                .where(CandidateProfileVersion.tenant_id == summary.tenant_id)
                .order_by(CandidateProfileVersion.id)
            )
        ).all()
        embeddings = (
            await db_session.scalars(
                select(CandidateEmbeddingVersion)
                .where(CandidateEmbeddingVersion.tenant_id == summary.tenant_id)
                .order_by(CandidateEmbeddingVersion.id)
            )
        ).all()
        return {
            "search": search,
            "ranking": [
                (str(item.candidate_id), item.numeric_score, item.rank) for item in ranking.results
            ],
            "plan": plan.model_dump(mode="json"),
            "profiles": [item.profile_content for item in profiles],
            "embedding": [(item.source_sha256, list(item.embedding)) for item in embeddings],
                "serialized_hashes": [
                    compute_source_sha256(build_professional_embedding_text(item.profile_content))
                    if item.profile_content is not None else None
                    for item in profiles
                ],
        }

    before = await snapshot()
    documents = (
        await db_session.scalars(
            select(CandidateDocument).where(CandidateDocument.tenant_id == summary.tenant_id)
        )
    ).all()
    photos = LocalPhotoStorage(str(tmp_path / "storage"))
    derived_hashes = []
    for document in documents:
        outcome = await process_photo_for_document(
            db_session,
            storage,
            photos,
            tenant_id=summary.tenant_id,
            candidate_id=document.candidate_id,
            document_id=document.id,
        )
        assert outcome is not None and outcome.status == "AVAILABLE"
        derived_hashes.append(outcome.derived_sha256)
    assert len(derived_hashes) == 9
    after = await snapshot()
    assert after == before
    authoritative = json.dumps(after, default=str)
    assert "photo" not in authoritative.lower()
    assert all(digest not in authoritative for digest in derived_hashes)
