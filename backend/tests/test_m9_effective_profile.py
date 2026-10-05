"""Issue #46 M-9: synthetic immutable attempts versus professional authority."""

import uuid
from datetime import date

import pytest
from fakes import FakeEmbeddingProvider
from search_helpers import seed_candidate_with_profile, synthetic_evidence
from test_ui_routes import local_ui_settings  # noqa: F401 - pytest fixture

from meyar.scoring.batch import rank_candidates_for_job
from meyar.search.schemas import CandidateSearchRequest, RequiredFilters, SearchMode
from meyar.search.service import search_candidates
from meyar.services.candidate_embedding_service import embed_candidate_profile
from meyar.services.candidate_profile_repo import (
    create_profile_version,
    get_current_profile_version,
)
from meyar.services.job_criteria_repo import create_criteria_version
from meyar.services.job_repo import create_job
from meyar.services.profile_authority import get_current_authorized_profile
from meyar.ui.service import get_candidate_detail_view


def profile_content(skill="Python"):
    return {
        "skills": [{"name": skill, "evidence": synthetic_evidence(skill)}],
        "employment_history": [],
        "education": [],
        "certifications": [],
        "languages": [],
        "projects": [],
    }


async def attempt(db, version, status="FAILED"):
    return await create_profile_version(
        db,
        tenant_id=version.tenant_id,
        candidate_id=version.candidate_id,
        candidate_document_id=version.candidate_document_id,
        canonical_document_id=version.canonical_document_id,
        source_sha256=version.source_sha256,
        schema_version=version.schema_version,
        prompt_version=version.prompt_version,
        model_provider="fake",
        model_name="fake",
        model_metadata={},
        status=status,
        error_code="SYNTHETIC_FAILURE",
    )


async def accepted_then_failed(db, tenant_id):
    candidate, accepted = await seed_candidate_with_profile(
        db,
        tenant_id=tenant_id,
        profile_content=profile_content(),
    )
    failed = await attempt(db, accepted)
    latest = await get_current_profile_version(db, tenant_id=tenant_id, candidate_id=candidate.id)
    assert latest.id == failed.id
    return candidate, accepted, failed



def _compat():
    from fakes import FakeEmbeddingProvider as _Provider

    from meyar.services.folder_reconciliation_service import resolve_embedding_compatibility

    return resolve_embedding_compatibility(_Provider(), None)

async def test_m9_authority_survives_failed_attempt(db_session, tenant_and_key):
    tenant, _, _ = tenant_and_key
    candidate, accepted, _ = await accepted_then_failed(db_session, tenant.id)
    authorized = await get_current_authorized_profile(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
    )
    assert authorized is not None
    assert authorized[0].id == accepted.id


async def test_m9_structured_search_survives_failed_attempt(db_session, tenant_and_key):
    tenant, _, _ = tenant_and_key
    _, accepted, _ = await accepted_then_failed(db_session, tenant.id)
    response = await search_candidates(
        db_session,
        tenant_id=tenant.id,
        request=CandidateSearchRequest(
            mode=SearchMode.STRUCTURED_ONLY,
            required_filters=RequiredFilters(skills=["Python"]),
        ),
    )
    assert response.result_count == 1
    assert response.results[0].candidate_profile_version_id == accepted.id


async def test_m9_ranking_survives_failed_attempt(db_session, tenant_and_key):
    tenant, _, _ = tenant_and_key
    _, accepted, _ = await accepted_then_failed(db_session, tenant.id)
    job = await create_job(db_session, tenant_id=tenant.id, title="Synthetic Python role")
    criteria = await create_criteria_version(
        db_session,
        tenant_id=tenant.id,
        job_id=job.id,
        created_by_api_key_id=None,
        criteria=[
            {
                "id": "python",
                "kind": "SKILL",
                "type": "MUST_HAVE",
                "label": "Python",
                "value": "Python",
                "weight": 1,
            }
        ],
    )
    response = await rank_candidates_for_job(
        db_session,
        tenant_id=tenant.id,
        job_criteria_version_id=criteria.id,
        evaluation_as_of_date=date(2026, 1, 1),
    )
    assert response.evaluated_count == 1
    assert response.results[0].candidate_profile_version_id == accepted.id


async def test_m9_embedding_survives_failed_attempt(db_session, tenant_and_key):
    tenant, _, _ = tenant_and_key
    candidate, accepted, _ = await accepted_then_failed(db_session, tenant.id)
    embedding, _ = await embed_candidate_profile(
        db_session,
        FakeEmbeddingProvider(),
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        max_input_chars=10000,
        compatibility=FakeEmbeddingProvider().compatibility,
    )
    assert embedding.candidate_profile_version_id == accepted.id


async def test_m9_detail_survives_failed_attempt(db_session, tenant_and_key):
    tenant, _, _ = tenant_and_key
    candidate, _, _ = await accepted_then_failed(db_session, tenant.id)
    detail = await get_candidate_detail_view(
        db_session, tenant_id=tenant.id, candidate_id=candidate.id
    )
    assert [fact.title for fact in detail.skills] == ["Python"]


async def test_m9_manual_review_preserves_effective_and_batched_selection(
    db_session, tenant_and_key
):
    from meyar.services.candidate_profile_repo import (
        get_effective_profile_version,
        get_effective_profile_versions_for_candidates,
        list_effective_profile_versions_for_tenant,
    )

    tenant, _, _ = tenant_and_key
    candidate, v1 = await seed_candidate_with_profile(
        db_session,
        tenant_id=tenant.id,
        profile_content=profile_content(),
    )
    v2 = await attempt(db_session, v1, "MANUAL_REVIEW_REQUIRED")
    selected = await get_effective_profile_version(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
    )
    assert selected.id == v1.id
    assert (
        await get_current_profile_version(
            db_session,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
        )
    ).id == v2.id
    assert (
        await get_effective_profile_versions_for_candidates(
            db_session,
            tenant_id=tenant.id,
            candidate_ids=[candidate.id],
        )
    )[candidate.id].id == v1.id
    assert [
        v.id
        for v in await list_effective_profile_versions_for_tenant(
            db_session,
            tenant_id=tenant.id,
        )
    ] == [v1.id]


async def test_m9_only_failure_has_no_authority_and_is_tenant_isolated(db_session, tenant_and_key):
    from meyar.services.candidate_profile_repo import (
        get_effective_profile_version,
        get_effective_profile_versions_for_candidates,
        list_effective_profile_versions_for_tenant,
    )
    from meyar.services.tenant_repo import create_tenant

    tenant, _, _ = tenant_and_key
    candidate, _ = await seed_candidate_with_profile(
        db_session,
        tenant_id=tenant.id,
        profile_content=None,
        status="FAILED",
    )
    assert (
        await get_current_authorized_profile(
            db_session,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
        )
        is None
    )
    assert await list_effective_profile_versions_for_tenant(db_session, tenant_id=tenant.id) == []
    other = await create_tenant(db_session, name="Synthetic other M9 tenant")
    foreign, _ = await seed_candidate_with_profile(
        db_session,
        tenant_id=other.id,
        profile_content=profile_content(),
    )
    assert (
        await get_effective_profile_version(
            db_session,
            tenant_id=tenant.id,
            candidate_id=foreign.id,
        )
        is None
    )
    assert (
        await get_effective_profile_versions_for_candidates(
            db_session,
            tenant_id=tenant.id,
            candidate_ids=[candidate.id, foreign.id],
        )
        == {}
    )
    assert (
        await get_effective_profile_versions_for_candidates(
            db_session,
            tenant_id=tenant.id,
            candidate_ids=[],
        )
        == {}
    )


@pytest.mark.parametrize("same_document", [False, True])
async def test_m9_newest_completed_invalid_evidence_fails_closed(
    db_session, tenant_and_key, same_document
):
    from search_helpers import seed_next_profile_version

    from meyar.services.candidate_profile_repo import get_effective_profile_version

    tenant, _, _ = tenant_and_key
    candidate, accepted, _ = await accepted_then_failed(db_session, tenant.id)
    unsupported = profile_content("Rust")
    unsupported["skills"][0]["evidence"] = synthetic_evidence("Python")
    if same_document:
        invalid = await create_profile_version(
            db_session,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
            candidate_document_id=accepted.candidate_document_id,
            canonical_document_id=accepted.canonical_document_id,
            source_sha256=accepted.source_sha256,
            schema_version=accepted.schema_version,
            prompt_version=accepted.prompt_version,
            model_provider="fake",
            model_name="fake",
            model_metadata={},
            status="COMPLETED",
            profile_content=unsupported,
        )
    else:
        invalid = await seed_next_profile_version(
            db_session,
            tenant_id=tenant.id,
            candidate=candidate,
            profile_content=unsupported,
        )
    assert (
        await get_effective_profile_version(
            db_session,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
        )
    ).id == invalid.id
    assert (
        await get_current_authorized_profile(
            db_session,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
        )
        is None
    )  # no scan back to v1
    response = await search_candidates(
        db_session,
        tenant_id=tenant.id,
        request=CandidateSearchRequest(mode=SearchMode.STRUCTURED_ONLY),
    )
    assert response.result_count == 0
    detail = await get_candidate_detail_view(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
    )
    assert detail.skills == [] and not detail.preserved_profile


async def test_m9_semantic_hybrid_exact_embedding_and_successful_switch(db_session, tenant_and_key):
    from search_helpers import seed_next_profile_version

    from meyar.embedding.serializer import SERIALIZER_VERSION
    from meyar.search.schemas import EmbeddingSearchConfig

    tenant, _, _ = tenant_and_key
    candidate, v1 = await seed_candidate_with_profile(
        db_session,
        tenant_id=tenant.id,
        profile_content=profile_content(),
    )
    provider = FakeEmbeddingProvider(dimensions=8, vector=[1.0] + [0.0] * 7)
    embedding, _ = await embed_candidate_profile(
        db_session,
        provider,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        max_input_chars=10000,
        compatibility=provider.compatibility,
    )
    failed = await attempt(db_session, v1)
    config = EmbeddingSearchConfig(
        provider=provider.provider_name,
        model_name=provider.model_name,
        model_revision=provider.model_revision,
        serializer_version=SERIALIZER_VERSION,
        embedding_dimensions=8,
    )

    async def search(mode):
        return await search_candidates(
            db_session,
            tenant_id=tenant.id,
            embedding_provider=provider,
            request=CandidateSearchRequest(
                mode=mode,
                semantic_query="Synthetic engineering",
                embedding_config=config,
            ),
        )

    for mode in (SearchMode.SEMANTIC_ONLY, SearchMode.HYBRID):
        result = await search(mode)
        assert result.result_count == 1
        assert result.results[0].candidate_profile_version_id == v1.id
        assert result.results[0].candidate_embedding_version_id == embedding.id
    v3 = await seed_next_profile_version(
        db_session,
        tenant_id=tenant.id,
        candidate=candidate,
        profile_content=profile_content("Rust"),
    )
    assert v3.version_number == 3
    assert (
        await get_current_authorized_profile(
            db_session,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
        )
    )[0].id == v3.id
    for mode in (SearchMode.SEMANTIC_ONLY, SearchMode.HYBRID):
        missing = await search(mode)
        assert missing.result_count == 0
        assert missing.excluded_missing_embedding_count == 1
    new_embedding, reused = await embed_candidate_profile(
        db_session,
        provider,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        max_input_chars=10000,
        compatibility=provider.compatibility,
    )
    assert not reused and new_embedding.candidate_profile_version_id == v3.id
    for mode in (SearchMode.SEMANTIC_ONLY, SearchMode.HYBRID):
        result = await search(mode)
        assert result.results[0].candidate_profile_version_id == v3.id
        assert result.results[0].candidate_embedding_version_id == new_embedding.id
    await db_session.refresh(v1)
    await db_session.refresh(failed)
    await db_session.refresh(embedding)
    assert v1.profile_content == profile_content()
    assert failed.status == "FAILED" and failed.profile_content is None
    assert embedding.candidate_profile_version_id == v1.id


async def test_m9_direct_score_matches_batch_and_preserves_history(
    db_session, client, tenant_and_key
):
    from sqlalchemy import select

    from meyar.models.evaluation import Evaluation

    tenant, _, plaintext = tenant_and_key
    candidate, v1 = await seed_candidate_with_profile(
        db_session,
        tenant_id=tenant.id,
        profile_content=profile_content(),
    )
    job = await create_job(db_session, tenant_id=tenant.id, title="Synthetic Python role")
    criteria = await create_criteria_version(
        db_session,
        tenant_id=tenant.id,
        job_id=job.id,
        created_by_api_key_id=None,
        criteria=[
            {
                "id": "python",
                "kind": "SKILL",
                "type": "MUST_HAVE",
                "label": "Python",
                "value": "Python",
                "weight": 1,
            }
        ],
    )
    original = await rank_candidates_for_job(
        db_session,
        tenant_id=tenant.id,
        job_criteria_version_id=criteria.id,
        evaluation_as_of_date=date(2026, 1, 1),
    )
    old_id = original.results[0].evaluation_id
    old = await db_session.get(Evaluation, old_id)
    old_content = old.score_explanation.copy()
    await attempt(db_session, v1)
    await db_session.commit()
    response = await client.post(
        f"/api/v1/jobs/{job.id}/criteria/1/score",
        headers={"Authorization": f"Bearer {plaintext}"},
        json={"candidate_id": str(candidate.id), "evaluation_as_of_date": "2026-01-02"},
    )
    assert response.status_code == 200
    assert response.json()["candidate_profile_version_id"] == str(v1.id)
    ranking = await rank_candidates_for_job(
        db_session,
        tenant_id=tenant.id,
        job_criteria_version_id=criteria.id,
        evaluation_as_of_date=date(2026, 1, 2),
    )
    assert str(ranking.results[0].evaluation_id) == response.json()["evaluation_id"]
    rows = list(
        await db_session.scalars(select(Evaluation).where(Evaluation.tenant_id == tenant.id))
    )
    assert len(rows) == 2 and all(row.candidate_profile_version_id == v1.id for row in rows)
    await db_session.refresh(old)
    assert old.score_explanation == old_content


async def test_m9_ui_facts_source_warning_and_operational_filter(
    db_session,
    client,
    tenant_and_user,
    local_ui_settings,  # noqa: F811 - imported pytest fixture
):
    from test_ui_routes import _login_and_csrf, _visible_text

    from meyar.ui.service import list_candidate_library

    tenant, user, password, _ = tenant_and_user
    candidate, v1, failed = await accepted_then_failed(db_session, tenant.id)
    await db_session.commit()
    detail = await get_candidate_detail_view(
        db_session, tenant_id=tenant.id, candidate_id=candidate.id
    )
    assert detail.profile_version == v1.version_number
    assert detail.latest_attempt_status == "FAILED" and detail.preserved_profile
    assert detail.skills[0].evidence[0].snippet == "Python"
    library = await list_candidate_library(db_session, tenant_id=tenant.id, profile_status="FAILED")
    assert library.total == 1
    assert library.items[0].current_profile_version == v1.version_number
    assert library.items[0].top_skills == ["Python"] and library.items[0].preserved_profile
    assert (
        await list_candidate_library(
            db_session,
            tenant_id=tenant.id,
            profile_status="COMPLETED",
        )
    ).items == []
    await _login_and_csrf(client, user.username, password)
    warning = "Son yenilənmə tamamlanmadı. Əvvəlki təsdiqlənmiş profil göstərilir."
    for path in (f"/ui/candidates/{candidate.id}", "/ui/library?profile_status=FAILED"):
        response = await client.get(path)
        assert response.status_code == 200
        visible = _visible_text(response.text)
        assert warning in visible and "Python" in visible
        for internal in (str(v1.id), str(failed.id), "SYNTHETIC_FAILURE", "fake-model"):
            assert internal not in visible


async def test_m9_changed_document_failure_then_retry_restores_authority(
    db_session,
    tenant_and_key,
):
    from fakes import FakeLLMProvider

    from meyar.extraction.service import extract_candidate_profile
    from meyar.llm.provider import ModelUnavailableError
    from meyar.schemas.candidate_identity import CandidateIdentityExtraction
    from meyar.schemas.candidate_profile import CandidateProfileExtraction
    from meyar.services.candidate_document_repo import (
        create_candidate_document,
        create_canonical_document,
    )
    from meyar.services.candidate_profile_repo import get_latest_profile_version_for_document
    from meyar.services.folder_indexed_file_repo import create_folder_indexed_file
    from meyar.services.folder_reconciliation_service import (
        _is_ready,
        _process_one_candidate_document,
    )
    from meyar.services.folder_source_repo import get_or_create_folder_source

    tenant, _, _ = tenant_and_key
    candidate, _ = await seed_candidate_with_profile(
        db_session,
        tenant_id=tenant.id,
        profile_content=profile_content(),
    )
    # Independent new source, with a real failure persisted by extraction.
    document = await create_candidate_document(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        original_filename="synthetic-new.pdf",
        mime_type="application/pdf",
        byte_size=100,
        sha256_hash="b" * 64,
        storage_key="synthetic-m9-new",
    )
    await create_canonical_document(
        db_session,
        tenant_id=tenant.id,
        candidate_document_id=document.id,
        parser_name="synthetic",
        parser_version="1",
        language=None,
        content={"pages": [{"page": 1, "blocks": [{"index": 0, "text": "Rust"}]}]},
    )
    failed = await extract_candidate_profile(
        db_session,
        FakeLLMProvider(error=ModelUnavailableError("synthetic outage")),
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        candidate_document=document,
        model_provider_name="fake",
        max_input_chars=10000,
    )
    assert failed.version_number == 2 and failed.status == "FAILED"
    assert (
        await get_latest_profile_version_for_document(
            db_session,
            tenant_id=tenant.id,
            candidate_document_id=document.id,
        )
    ).id == failed.id
    ready, latest = await _is_ready(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        candidate_document_id=document.id,
        compatibility=_compat(),
    )
    assert not ready and latest.id == failed.id
    assert (
        await get_current_authorized_profile(
            db_session,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
        )
    ) is None
    provider = FakeLLMProvider(
        extraction=CandidateProfileExtraction.model_validate(profile_content("Rust")),
        identity_extraction=CandidateIdentityExtraction(),
    )
    # Folder authority: the work item exists because a tracked path points at this exact
    # document (D-013 / D-021).
    source = await get_or_create_folder_source(
        db_session, tenant_id=tenant.id, root_path="synthetic-m9-source"
    )
    await create_folder_indexed_file(
        db_session, tenant_id=tenant.id, folder_source_id=source.id,
        relative_path="synthetic-new.pdf", document_type="PDF", byte_size=100,
        sha256_hash="b" * 64, index_status="INDEXED", candidate_id=candidate.id,
        candidate_document_id=document.id,
    )
    await db_session.commit()
    outcome = await _process_one_candidate_document(
        db_session,
        provider,
        FakeEmbeddingProvider(),
        tenant_id=tenant.id,
        folder_source_id=source.id,
        candidate_id=candidate.id,
        candidate_document_id=document.id,
        model_provider_name="fake",
        max_profile_input_chars=10000,
        max_identity_input_chars=10000,
        max_embedding_input_chars=10000,
        compatibility=_compat(),
    )
    assert outcome
    effective = await get_current_authorized_profile(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
    )
    assert effective[0].version_number == 3
    assert effective[0].candidate_document_id == document.id
    assert effective[1].skills[0].name == "Rust"
    ready, latest = await _is_ready(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        candidate_document_id=document.id,
        compatibility=_compat(),
    )
    assert ready and latest.id == effective[0].id
    response = await search_candidates(
        db_session,
        tenant_id=tenant.id,
        request=CandidateSearchRequest(
            mode=SearchMode.STRUCTURED_ONLY,
            required_filters=RequiredFilters(skills=["Rust"]),
        ),
    )
    assert response.result_count == 1
    assert response.results[0].candidate_profile_version_id == effective[0].id
    criteria = await synthetic_criteria(db_session, tenant.id)
    ranking = await rank_candidates_for_job(
        db_session,
        tenant_id=tenant.id,
        job_criteria_version_id=criteria.id,
        evaluation_as_of_date=date(2026, 1, 1),
    )
    assert ranking.evaluated_count == 1
    assert ranking.results[0].candidate_profile_version_id == effective[0].id
    await db_session.refresh(failed)
    assert failed.status == "FAILED" and failed.profile_content is None


@pytest.mark.parametrize("status", ["FAILED", "MANUAL_REVIEW_REQUIRED"])
@pytest.mark.parametrize("new_document", [False, True])
async def test_m9_identity_failure_respects_document_boundary(
    db_session, tenant_and_key, status, new_document
):
    from test_candidate_identity import _seed_candidate_with_identity_content

    from meyar.services.candidate_identity_repo import (
        create_identity_version,
        get_current_identity_version,
        get_effective_identity_version,
        get_effective_identity_versions_for_candidates,
    )
    from meyar.services.identity_authority import get_current_identity_values
    from meyar.ui.service import list_candidate_library

    tenant, _, _ = tenant_and_key
    candidate, document, canonical = await _seed_candidate_with_identity_content(
        db_session, tenant.id
    )
    fields = {
        "full_name": {
            "value": "Jane Synthetic Doe",
            "evidence": synthetic_evidence("Jane Synthetic Doe"),
        },
        "email": {
            "value": "jane.synthetic@example.com",
            "evidence": synthetic_evidence("Email: jane.synthetic@example.com"),
        },
        "phone": {"value": "+1-555-0100", "evidence": synthetic_evidence("Phone: +1-555-0100")},
    }
    kwargs = dict(
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        candidate_document_id=document.id,
        canonical_document_id=canonical.id,
        source_sha256="a" * 64,
        schema_version="candidate-identity-v1",
        prompt_version="synthetic",
        model_provider="fake",
        model_name="fake",
    )
    v1 = await create_identity_version(
        db_session, **kwargs, status="COMPLETED", identity_content=fields
    )
    if new_document:
        _, new_source = await new_document_attempt(db_session, v1, status)
        kwargs.update(
            candidate_document_id=new_source.candidate_document_id,
            canonical_document_id=new_source.canonical_document_id,
            source_sha256=new_source.source_sha256,
        )
    failed = await create_identity_version(db_session, **kwargs, status=status)
    assert (
        await get_current_identity_version(
            db_session,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
        )
    ).id == failed.id
    selected = await get_effective_identity_version(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
    )
    assert selected is None if new_document else selected.id == v1.id
    from test_agent_result_set_scale import count_queries

    with count_queries(db_session) as log:
        bounded = await get_effective_identity_versions_for_candidates(
            db_session,
            tenant_id=tenant.id,
            candidate_ids=[candidate.id],
        )
    assert log.count == 1
    assert bounded == {} if new_document else bounded[candidate.id].id == v1.id
    assert (
        await get_effective_identity_versions_for_candidates(
            db_session,
            tenant_id=tenant.id,
            candidate_ids=[],
        )
        == {}
    )
    values = await get_current_identity_values(
        db_session, tenant_id=tenant.id, candidate_id=candidate.id
    )
    assert (values.full_name, values.email, values.phone) == (
        (None, None, None)
        if new_document
        else ("Jane Synthetic Doe", "jane.synthetic@example.com", "+1-555-0100")
    )
    detail = await get_candidate_detail_view(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
    )
    assert detail.full_name == values.full_name
    assert detail.email == values.email and detail.phone == values.phone
    assert (await list_candidate_library(db_session, tenant_id=tenant.id)).items[
        0
    ].full_name == values.full_name
    # A new COMPLETED unsupported identity must fail closed, even with v1 present.
    from copy import deepcopy

    fields = deepcopy(fields)
    fields["full_name"]["value"] = "Unsupported Synthetic Person"
    await create_identity_version(db_session, **kwargs, status="COMPLETED", identity_content=fields)
    assert (
        await get_current_identity_values(
            db_session,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
        )
    ).full_name is None


async def test_m9_agent_profile_evidence_and_snapshot_preserved_then_stale(
    db_session, tenant_and_user
):
    from search_helpers import seed_next_profile_version
    from sqlalchemy import select
    from test_agent_result_set_snapshot import _structured_snapshot

    from meyar.agent.service import _dispatch_evidence, _dispatch_profile
    from meyar.models.agent_result_set import AgentResultSetMember
    from meyar.services.agent_result_set_repo import (
        ResultSetResolutionFailure,
        resolve_active_candidate_ref,
        validate_active_result_set_for_refinement,
    )

    tenant, session, context, result_set, members = await _structured_snapshot(
        db_session, tenant_and_user
    )
    candidate, v1 = members[0]
    snapshot = await db_session.scalar(
        select(AgentResultSetMember).where(
            AgentResultSetMember.result_set_id == result_set.id,
            AgentResultSetMember.ordinal == 1,
        )
    )
    before = (snapshot.candidate_profile_version_id, snapshot.candidate_embedding_version_id)
    await attempt(db_session, v1)
    profile_result, profile, failure = await _dispatch_profile(
        db_session,
        tenant_id=tenant.id,
        session_context=context,
        candidate_ref=1,
    )
    assert failure is None and profile_result.profile.found
    assert profile.skills[0].name == "Python"
    evidence_result, _, failure = await _dispatch_evidence(
        db_session,
        tenant_id=tenant.id,
        session_context=context,
        candidate_ref=1,
        evidence_topic="Python",
    )
    assert failure is None and evidence_result.evidence.found
    assert evidence_result.evidence.matches[0].evidence == profile.skills[0].evidence
    refinement = await validate_active_result_set_for_refinement(
        db_session,
        tenant_id=tenant.id,
        browser_session_id=session.id,
        session_context=context,
    )
    assert not isinstance(refinement, ResultSetResolutionFailure)
    await seed_next_profile_version(
        db_session,
        tenant_id=tenant.id,
        candidate=candidate,
        profile_content=profile_content("Rust"),
    )
    assert (
        await resolve_active_candidate_ref(
            db_session,
            tenant_id=tenant.id,
            browser_session_id=session.id,
            session_context=context,
            candidate_ref=1,
        )
        == ResultSetResolutionFailure.STALE
    )
    await db_session.refresh(snapshot)
    assert (
        snapshot.candidate_profile_version_id,
        snapshot.candidate_embedding_version_id,
    ) == before


@pytest.mark.parametrize("batch_size, canonical_queries", [(500, 1), (2, 3)])
@pytest.mark.parametrize("cross_document", [False, True])
async def test_m9_search_and_member_lookup_do_not_query_each_history(
    db_session, tenant_and_key, monkeypatch, batch_size, canonical_queries, cross_document
):
    from test_agent_result_set_scale import count_queries

    import meyar.search.service as search_module
    from meyar.services.candidate_profile_repo import get_effective_profile_versions_for_candidates
    from meyar.services.profile_authority import authorize_profile_versions

    monkeypatch.setattr(search_module, "AUTHORITY_BATCH_SIZE", batch_size)
    tenant, _, _ = tenant_and_key
    ids = []
    for index in range(5):
        candidate, v1 = await seed_candidate_with_profile(
            db_session,
            tenant_id=tenant.id,
            profile_content=profile_content(),
        )
        ids.append(candidate.id)
        for _ in range(5):
            await attempt(db_session, v1)
        if cross_document and index >= 3:
            await new_document_attempt(db_session, v1)
    expected = 3 if cross_document else 5
    with count_queries(db_session) as log:
        versions = await get_effective_profile_versions_for_candidates(
            db_session,
            tenant_id=tenant.id,
            candidate_ids=ids,
        )
        authorized = await authorize_profile_versions(
            db_session,
            tenant_id=tenant.id,
            versions=list(versions.values()),
        )
    assert log.count == 2
    assert len(versions) == expected
    assert all(version.version_number == 1 for version in versions.values())
    assert all(not isinstance(value, Exception) for value in authorized.values())
    with count_queries(db_session) as log:
        response = await search_candidates(
            db_session,
            tenant_id=tenant.id,
            request=CandidateSearchRequest(mode=SearchMode.STRUCTURED_ONLY),
        )
    assert response.result_count == expected
    expected_queries = 2 if cross_document and batch_size == 2 else canonical_queries
    assert log.touching("canonical_documents") == expected_queries
    assert log.touching("candidate_profile_versions") == 1


async def new_document_attempt(db, old, status="FAILED"):
    from meyar.services.candidate_document_repo import (
        create_candidate_document,
        create_canonical_document,
    )

    document = await create_candidate_document(
        db,
        tenant_id=old.tenant_id,
        candidate_id=old.candidate_id,
        original_filename="synthetic-new-source.pdf",
        mime_type="application/pdf",
        byte_size=100,
        sha256_hash="b" * 64,
        storage_key=f"synthetic-m9-new-source/{uuid.uuid4()}",
    )
    canonical = await create_canonical_document(
        db,
        tenant_id=old.tenant_id,
        candidate_document_id=document.id,
        parser_name="synthetic",
        parser_version="1",
        language=None,
        content={"pages": [{"page": 1, "blocks": [{"index": 0, "text": "Rust"}]}]},
    )
    failed = await create_profile_version(
        db,
        tenant_id=old.tenant_id,
        candidate_id=old.candidate_id,
        candidate_document_id=document.id,
        canonical_document_id=canonical.id,
        source_sha256="b" * 64,
        schema_version="candidate-profile-v1",
        prompt_version="synthetic",
        model_provider="fake",
        model_name="fake",
        model_metadata={},
        status=status,
        error_code="SYNTHETIC_FAILURE",
    )
    return document, failed


async def synthetic_criteria(db, tenant_id):
    job = await create_job(db, tenant_id=tenant_id, title="Synthetic role")
    return await create_criteria_version(
        db,
        tenant_id=tenant_id,
        job_id=job.id,
        created_by_api_key_id=None,
        criteria=[
            {
                "id": "python",
                "kind": "SKILL",
                "type": "MUST_HAVE",
                "label": "Python",
                "value": "Python",
                "weight": 1,
            }
        ],
    )


@pytest.mark.parametrize("status", ["FAILED", "MANUAL_REVIEW_REQUIRED"])
async def test_m9_new_document_has_no_effective_profile_in_any_selector(
    db_session,
    tenant_and_key,
    status,
):
    from meyar.services.candidate_profile_repo import (
        get_effective_profile_version,
        get_effective_profile_versions_for_candidates,
        list_effective_profile_versions_for_tenant,
    )
    from meyar.services.folder_reconciliation_service import _is_ready

    tenant, _, _ = tenant_and_key
    candidate, old = await seed_candidate_with_profile(
        db_session,
        tenant_id=tenant.id,
        profile_content=profile_content(),
    )
    document, failed = await new_document_attempt(db_session, old, status)
    assert (
        await get_current_profile_version(
            db_session,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
        )
    ).id == failed.id
    assert (
        await get_effective_profile_version(
            db_session,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
        )
        is None
    )
    assert (
        await get_effective_profile_versions_for_candidates(
            db_session,
            tenant_id=tenant.id,
            candidate_ids=[candidate.id],
        )
        == {}
    )
    assert await list_effective_profile_versions_for_tenant(db_session, tenant_id=tenant.id) == []
    assert (
        await get_current_authorized_profile(
            db_session,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
        )
        is None
    )
    ready, latest = await _is_ready(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        candidate_document_id=document.id,
        compatibility=_compat(),
    )
    assert not ready and latest.id == failed.id


@pytest.mark.parametrize("status", ["FAILED", "MANUAL_REVIEW_REQUIRED"])
@pytest.mark.parametrize(
    "mode", [SearchMode.STRUCTURED_ONLY, SearchMode.SEMANTIC_ONLY, SearchMode.HYBRID]
)
async def test_m9_new_document_excludes_old_search_and_embedding(
    db_session,
    tenant_and_key,
    status,
    mode,
):
    from meyar.embedding.serializer import SERIALIZER_VERSION
    from meyar.search.schemas import EmbeddingSearchConfig
    from meyar.services.candidate_embedding_service import EmbeddingPreconditionError

    tenant, _, _ = tenant_and_key
    candidate, old = await seed_candidate_with_profile(
        db_session,
        tenant_id=tenant.id,
        profile_content=profile_content(),
    )
    provider = FakeEmbeddingProvider(dimensions=8, vector=[1.0] + [0.0] * 7)
    embedding, _ = await embed_candidate_profile(
        db_session,
        provider,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        max_input_chars=10000,
        compatibility=provider.compatibility,
    )
    await new_document_attempt(db_session, old, status)
    config = None
    if mode != SearchMode.STRUCTURED_ONLY:
        config = EmbeddingSearchConfig(
            provider=provider.provider_name,
            model_name=provider.model_name,
            model_revision=provider.model_revision,
            serializer_version=SERIALIZER_VERSION,
            embedding_dimensions=8,
        )
    request = CandidateSearchRequest(
        mode=mode,
        required_filters=RequiredFilters(skills=["Python"]),
        semantic_query="Synthetic engineering" if config else None,
        embedding_config=config,
    )
    response = await search_candidates(
        db_session,
        tenant_id=tenant.id,
        request=request,
        embedding_provider=provider,
    )
    assert response.result_count == 0
    with pytest.raises(EmbeddingPreconditionError) as exc:
        await embed_candidate_profile(
            db_session,
            provider,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
            max_input_chars=10000,
            compatibility=provider.compatibility,
        )
    assert exc.value.code == "PROFILE_NOT_COMPLETED"
    await db_session.refresh(embedding)
    assert embedding.candidate_profile_version_id == old.id


@pytest.mark.parametrize("status", ["FAILED", "MANUAL_REVIEW_REQUIRED"])
async def test_m9_new_document_blocks_current_scoring_preserves_history(
    db_session,
    client,
    tenant_and_key,
    status,
):
    from meyar.models.evaluation import Evaluation
    from meyar.services.candidate_profile_repo import get_profile_version_by_id

    tenant, _, plaintext = tenant_and_key
    candidate, old = await seed_candidate_with_profile(
        db_session,
        tenant_id=tenant.id,
        profile_content=profile_content(),
    )
    criteria = await synthetic_criteria(db_session, tenant.id)
    original = await rank_candidates_for_job(
        db_session,
        tenant_id=tenant.id,
        job_criteria_version_id=criteria.id,
        evaluation_as_of_date=date(2026, 1, 1),
    )
    evaluation = await db_session.get(Evaluation, original.results[0].evaluation_id)
    before = evaluation.score_explanation.copy()
    await new_document_attempt(db_session, old, status)
    await db_session.commit()
    ranking = await rank_candidates_for_job(
        db_session,
        tenant_id=tenant.id,
        job_criteria_version_id=criteria.id,
        evaluation_as_of_date=date(2026, 1, 2),
    )
    assert ranking.evaluated_count == 0
    response = await client.post(
        f"/api/v1/jobs/{criteria.job_id}/criteria/1/score",
        headers={"Authorization": f"Bearer {plaintext}"},
        json={"candidate_id": str(candidate.id), "evaluation_as_of_date": "2026-01-02"},
    )
    assert response.status_code == 404
    await db_session.refresh(evaluation)
    assert evaluation.candidate_profile_version_id == old.id
    assert evaluation.score_explanation == before
    assert (
        await get_profile_version_by_id(
            db_session,
            tenant_id=tenant.id,
            profile_version_id=old.id,
        )
    ).profile_content == profile_content()
    detail = await get_candidate_detail_view(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
    )
    assert len(detail.evaluations) == 1


@pytest.mark.parametrize("status", ["FAILED", "MANUAL_REVIEW_REQUIRED"])
async def test_m9_new_document_stales_result_set_and_agent_facts(
    db_session,
    tenant_and_user,
    status,
):
    from sqlalchemy import select
    from test_agent_result_set_snapshot import _structured_snapshot

    from meyar.agent.service import _dispatch_evidence, _dispatch_profile
    from meyar.models.agent_result_set import AgentResultSetMember
    from meyar.services.agent_result_set_repo import (
        ResultSetResolutionFailure,
        resolve_active_candidate_ref,
        validate_active_result_set_for_refinement,
    )

    tenant, session, context, result_set, members = await _structured_snapshot(
        db_session, tenant_and_user
    )
    candidate, old = members[0]
    snapshot = await db_session.scalar(
        select(AgentResultSetMember).where(
            AgentResultSetMember.result_set_id == result_set.id,
            AgentResultSetMember.ordinal == 1,
        )
    )
    before = (snapshot.candidate_profile_version_id, snapshot.candidate_embedding_version_id)
    await new_document_attempt(db_session, old, status)
    assert (
        await resolve_active_candidate_ref(
            db_session,
            tenant_id=tenant.id,
            browser_session_id=session.id,
            session_context=context,
            candidate_ref=1,
        )
        == ResultSetResolutionFailure.STALE
    )
    assert (
        await validate_active_result_set_for_refinement(
            db_session,
            tenant_id=tenant.id,
            browser_session_id=session.id,
            session_context=context,
        )
        == ResultSetResolutionFailure.STALE
    )
    for dispatch, extra in (
        (_dispatch_profile, {}),
        (_dispatch_evidence, {"evidence_topic": "Python"}),
    ):
        _, profile, failure = await dispatch(
            db_session,
            tenant_id=tenant.id,
            session_context=context,
            candidate_ref=1,
            **extra,
        )
        assert failure == ResultSetResolutionFailure.STALE and profile is None
    await db_session.refresh(snapshot)
    assert (
        snapshot.candidate_profile_version_id,
        snapshot.candidate_embedding_version_id,
    ) == before


@pytest.mark.parametrize("status", ["FAILED", "MANUAL_REVIEW_REQUIRED"])
async def test_m9_new_document_ui_withholds_old_facts(
    db_session,
    client,
    tenant_and_user,
    local_ui_settings,  # noqa: F811
    status,
):
    from test_ui_routes import _login_and_csrf, _visible_text

    from meyar.ui.service import list_candidate_library

    tenant, user, password, _ = tenant_and_user
    candidate, old = await seed_candidate_with_profile(
        db_session,
        tenant_id=tenant.id,
        profile_content=profile_content(),
    )
    _, failed = await new_document_attempt(db_session, old, status)
    await db_session.commit()
    detail = await get_candidate_detail_view(
        db_session, tenant_id=tenant.id, candidate_id=candidate.id
    )
    assert detail.skills == [] and not detail.preserved_profile
    assert detail.latest_attempt_status == status
    library = await list_candidate_library(db_session, tenant_id=tenant.id, profile_status=status)
    assert library.items[0].top_skills == [] and not library.items[0].preserved_profile
    await _login_and_csrf(client, user.username, password)
    for path in (f"/ui/candidates/{candidate.id}", f"/ui/library?profile_status={status}"):
        response = await client.get(path)
        assert response.status_code == 200
        visible = _visible_text(response.text)
        assert "Python" not in visible
        for internal in (str(old.id), str(failed.id), "SYNTHETIC_FAILURE", "fake-model"):
            assert internal not in visible
